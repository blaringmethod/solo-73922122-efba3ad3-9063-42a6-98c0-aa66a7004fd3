"""地勘取芯数据入仓演示网页（FastAPI）。

路由：
  GET  /                 回次列表
  GET  /import           导入新回次（粘贴 JSON 或上传 JSON 文件）
  POST /import
  GET  /runs/{run_id}    详情：段/样品可行范围、入仓清单、更正表单
  POST /runs/{run_id}/correct   更正实测段起点（携带页面版本号）
  GET  /runs/{run_id}/csv       下载生效的入仓 CSV
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .engine import EngineError, ValidationError
from .storage import StaleVersionError, Store

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", BASE_DIR.parent / "data"))
DEMO_PATH = BASE_DIR / "demo_payload.json"

app = FastAPI(title="地勘取芯数据入仓演示")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
store = Store(DATA_DIR / "runs.db")


def _flash_redirect(url: str, kind: str, message: str) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    # Cookie 头只能走 latin-1，中文提示先 URL 编码
    resp.set_cookie("flash_kind", kind, max_age=10)
    resp.set_cookie("flash_message", quote(message), max_age=10)
    return resp


def _pop_flash(request: Request) -> dict:
    kind = request.cookies.get("flash_kind")
    message = request.cookies.get("flash_message")
    return {"kind": kind, "message": unquote(message) if message else None}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    resp = templates.TemplateResponse(
        request,
        "index.html",
        {"runs": store.list_runs(), "flash": _pop_flash(request)},
    )
    if request.cookies.get("flash_message"):
        resp.delete_cookie("flash_kind")
        resp.delete_cookie("flash_message")
    return resp


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    demo_text = DEMO_PATH.read_text(encoding="utf-8")
    resp = templates.TemplateResponse(
        request,
        "import.html",
        {"demo_text": demo_text, "flash": _pop_flash(request)},
    )
    if request.cookies.get("flash_message"):
        resp.delete_cookie("flash_kind")
        resp.delete_cookie("flash_message")
    return resp


@app.post("/import")
async def import_submit(
    request: Request,
    payload_text: str = Form(""),
    payload_file: UploadFile | None = None,
):
    try:
        raw_text: str | None = payload_text.strip() or None
        if payload_file is not None and payload_file.filename:
            raw_bytes = await payload_file.read()
            raw_text = raw_bytes.decode("utf-8").strip() or None
        if not raw_text:
            raise ValidationError("请粘贴 JSON 内容或选择 JSON 文件")
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）")
        record = store.save_new_run(payload)
    except EngineError as exc:
        return _flash_redirect("/import", "error", f"导入失败，未写入任何数据。{exc}")
    except ValueError as exc:
        return _flash_redirect("/import", "error", str(exc))
    return _flash_redirect(
        f"/runs/{record.run_id}", "ok", f"回次 {record.run_id} 导入成功，版本 1 已生效。"
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: str):
    record = store.get_run(run_id)
    if record is None:
        return _flash_redirect("/", "error", f"回次 {run_id} 不存在")
    resp = templates.TemplateResponse(
        request,
        "detail.html",
        {
            "record": record,
            "history": store.correction_history(run_id),
            "flash": _pop_flash(request),
        },
    )
    if request.cookies.get("flash_message"):
        resp.delete_cookie("flash_kind")
        resp.delete_cookie("flash_message")
    return resp


@app.post("/runs/{run_id}/correct")
async def correct_submit(
    run_id: str,
    expected_version: int = Form(...),
    seg_index: str = Form(""),
    depth: str = Form(""),
    note: str = Form(""),
):
    # 两个多行文本框按行对齐，每行一个实测点
    measured: list[dict] = []
    indices = [x.strip() for x in seg_index.splitlines() if x.strip()]
    depths = [x.strip() for x in depth.splitlines() if x.strip()]
    if len(indices) != len(depths):
        return _flash_redirect(
            f"/runs/{run_id}",
            "error",
            f"更正失败：实测点段序号行数（{len(indices)}）与孔深行数（{len(depths)}）不一致。"
            "当前供数未做任何改动。",
        )
    try:
        for raw_idx, raw_depth in zip(indices, depths):
            if not raw_idx.lstrip("-").isdigit() or not raw_depth.lstrip("-").isdigit():
                raise ValidationError(
                    f"实测点（{raw_idx}, {raw_depth}）必须都是整数，段序号从 0 开始"
                )
            measured.append({"seg_index": int(raw_idx), "depth": int(raw_depth)})
        store.correct_run(run_id, expected_version, measured, note.strip())
    except StaleVersionError as exc:
        return _flash_redirect(f"/runs/{run_id}", "error", str(exc))
    except EngineError as exc:
        # 无解等错误信息本身已说明“当前供数保留”，直接透传引擎原因
        return _flash_redirect(f"/runs/{run_id}", "error", str(exc))
    except KeyError:
        return _flash_redirect("/", "error", f"回次 {run_id} 不存在")
    return _flash_redirect(
        f"/runs/{run_id}", "ok", f"回次 {run_id} 更正成功，供数表已整份更新至新版本。"
    )


@app.get("/runs/{run_id}/csv")
def download_csv(run_id: str):
    if store.get_run(run_id) is None:
        return JSONResponse({"error": f"回次 {run_id} 不存在"}, status_code=404)
    csv_bytes = store.supply_csv_bytes(run_id)
    return Response(
        content=csv_bytes,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="supply_{run_id}.csv"'},
    )
