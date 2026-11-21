"""地勘取芯数据入仓演示网页（FastAPI）。

路由：
  GET  /                 回次列表
  GET  /import           导入新回次（粘贴 JSON 或上传 JSON 文件）
  POST /import
  GET  /runs/{run_id}    详情：段/样品可行范围、入仓清单、化验单与复原结果、更正表单
  POST /runs/{run_id}/correct   更正实测段起点（携带页面版本号）
  GET  /runs/{run_id}/csv       下载生效的入仓 CSV
  POST /runs/{run_id}/slips                      按单号录入混样化验单
  POST /runs/{run_id}/slips/{slip_no}/correct    按单号更正混样化验单
  POST /runs/{run_id}/slips/{slip_no}/withdraw   按单号撤销混样化验单
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .assay import ContradictionError, SlipValidationError
from .engine import EngineError, ValidationError
from .storage import StaleVersionError, Store

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("APP_DATA_DIR", BASE_DIR.parent / "data"))
DEMO_PATH = BASE_DIR / "demo_payload.json"
DEMO_SLIPS_PATH = BASE_DIR / "demo_slips.json"

app = FastAPI(title="地勘取芯数据入仓演示")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
store = Store(DATA_DIR / "runs.db")


def _seed_demo_if_requested() -> None:
    """APP_SEED_DEMO=1 且库内无回次时，载入演示回次与演示化验单。"""
    if os.environ.get("APP_SEED_DEMO") != "1":
        return
    try:
        if store.list_runs():
            return
        payload = json.loads(DEMO_PATH.read_text(encoding="utf-8"))
        store.save_new_run(payload)
        demo_slips = json.loads(DEMO_SLIPS_PATH.read_text(encoding="utf-8"))
        for s in demo_slips:
            store.create_slip(
                payload["run_id"], s["slip_no"], s["sample_a"], s["sample_b"], s["mean"]
            )
        print(f"演示数据已载入：回次 {payload['run_id']}，化验单 {len(demo_slips)} 张")
    except Exception as exc:  # 种子失败不影响应用启动（如并发重复种子）
        print(f"演示数据载入跳过：{exc}")


_seed_demo_if_requested()


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
            "assay": store.assay_view(run_id),
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


# --------------------------------------------------------------------------- #
# 混样化验单：按单号录入 / 更正 / 撤销
# --------------------------------------------------------------------------- #
def _slip_redirect(run_id: str, exc: Exception, action: str) -> RedirectResponse:
    """化验单变更失败：说明原因，并明确当前化验单与复原结果保持不变。"""
    if isinstance(exc, ContradictionError):
        msg = f"{action}失败（与现行数据矛盾）：{exc}当前化验单和复原结果未做任何改动。"
    elif isinstance(exc, SlipValidationError):
        msg = f"{action}失败：{exc}当前化验单和复原结果未做任何改动。"
    else:
        msg = f"{action}失败：{exc}"
    return _flash_redirect(f"/runs/{run_id}", "error", msg)


@app.post("/runs/{run_id}/slips")
def slip_create(
    run_id: str,
    slip_no: str = Form(""),
    sample_a: str = Form(""),
    sample_b: str = Form(""),
    mean: str = Form(""),
):
    try:
        store.create_slip(run_id, slip_no, sample_a, sample_b, mean)
    except KeyError:
        return _flash_redirect("/", "error", f"回次 {run_id} 不存在")
    except (ContradictionError, SlipValidationError) as exc:
        return _slip_redirect(run_id, exc, "化验单录入")
    return _flash_redirect(
        f"/runs/{run_id}",
        "ok",
        f"化验单 {slip_no.strip()} 录入成功，复原结果已按现行化验单重算。",
    )


@app.post("/runs/{run_id}/slips/{slip_no}/correct")
def slip_correct(
    run_id: str,
    slip_no: str,
    sample_a: str = Form(""),
    sample_b: str = Form(""),
    mean: str = Form(""),
):
    try:
        store.correct_slip(run_id, slip_no, sample_a, sample_b, mean)
    except KeyError:
        return _flash_redirect("/", "error", f"回次 {run_id} 不存在")
    except (ContradictionError, SlipValidationError) as exc:
        return _slip_redirect(run_id, exc, f"化验单 {slip_no} 更正")
    return _flash_redirect(
        f"/runs/{run_id}",
        "ok",
        f"化验单 {slip_no} 更正成功，复原结果已按现行化验单重算。",
    )


@app.post("/runs/{run_id}/slips/{slip_no}/withdraw")
def slip_withdraw(run_id: str, slip_no: str):
    try:
        store.withdraw_slip(run_id, slip_no)
    except KeyError:
        return _flash_redirect("/", "error", f"回次 {run_id} 不存在")
    except SlipValidationError as exc:
        return _slip_redirect(run_id, exc, f"化验单 {slip_no} 撤销")
    return _flash_redirect(
        f"/runs/{run_id}",
        "ok",
        f"化验单 {slip_no} 已撤销，复原结果已重算；不再唯一的样品已移出复原清单。",
    )
