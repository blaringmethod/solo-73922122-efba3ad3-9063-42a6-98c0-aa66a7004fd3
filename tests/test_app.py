"""存储与 Web 全流程测试：导入、取数、更正、过期拒绝、无解回滚、CSV。"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    # 延迟导入，确保 Store 使用临时目录
    import importlib

    import app.main as main
    importlib.reload(main)
    return TestClient(main.app, follow_redirects=False)


DEMO_PAYLOAD = {
    "run_id": "RUN-T",
    "start_depth": 10000,
    "end_depth": 12500,
    "segments": [
        {"length": 500, "samples": [
            {"sample_no": "S1", "start": 20, "end": 100, "assay": "0.12"},
        ]},
        {"length": 600, "samples": [
            {"sample_no": "S2", "start": 50, "end": 120, "assay": "0.31"},
        ]},
        {"length": 700, "samples": [
            {"sample_no": "S3", "start": 500, "end": 650, "assay": "1.05"},
        ]},
    ],
    "gaps": [{"low": 100, "high": 300}, {"low": 100, "high": 300}],
    "boundaries": [11200],
    "layers": ["A", "B"],
    "measured_starts": [],
}


def _import(client, payload=DEMO_PAYLOAD):
    return client.post("/import", data={"payload_text": json.dumps(payload, ensure_ascii=False)})


def test_full_import_and_query_flow(client):
    resp = _import(client)
    assert resp.status_code == 303 and resp.headers["location"] == "/runs/RUN-T"

    list_resp = client.get("/")
    assert "RUN-T" in list_resp.text and "v1" in list_resp.text

    detail = client.get("/runs/RUN-T")
    assert detail.status_code == 200
    # S1 入仓（A 层），S3 跨层暂不入仓并展示可行范围
    assert "写进入仓 CSV" in detail.text
    assert "暂不入仓" in detail.text and "11200" in detail.text
    assert "[10700, 11100]" in detail.text

    csv_resp = client.get("/runs/RUN-T/csv")
    assert csv_resp.status_code == 200
    text = csv_resp.text
    assert "样品编号" in text and "S1" in text and "S3" not in text
    # 供数表含全部稳定同层样品（S1、S2），跨层的 S3 不写入
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 3  # 表头 + S1 + S2
    assert "S2" in text and "S3" not in text


def test_correction_updates_whole_supply_and_bumps_version(client):
    _import(client)
    # 用两个锚点把排布钉死，S3 随之完整落入 A 层
    resp = client.post(
        "/runs/RUN-T/correct",
        data={
            "expected_version": 1,
            "seg_index": "1\n2",
            "depth": "10300\n10400",
            "note": "补测两个段起点",
        },
    )
    assert resp.status_code == 303
    detail = client.get("/runs/RUN-T").text
    assert "v2" in detail and "补测两个段起点" in detail
    # S3 现在固定为 [10900,11050)，应入仓 A（页面分两列展示起止范围）
    csv_text = client.get("/runs/RUN-T/csv").text
    assert "S3" in csv_text and "A" in csv_text
    assert "10900" in detail and "11050" in detail


def test_stale_version_rejected_and_supply_kept(client):
    _import(client)
    # 先做一次成功更正 → 版本 2
    client.post(
        "/runs/RUN-T/correct",
        data={"expected_version": 1, "seg_index": "1", "depth": "10200", "note": "第一次"},
    )
    # 再拿过期的版本 1 提交
    resp = client.post(
        "/runs/RUN-T/correct",
        data={"expected_version": 1, "seg_index": "1", "depth": "10250", "note": "过期提交"},
    )
    assert resp.status_code == 303
    flash_page = client.get("/runs/RUN-T").text
    assert "页面版本已过期" in flash_page
    # 库内仍是第一次更正的结果：实测点表展示 10200，且更正历史只有 v1→v2 一条
    assert "10200" in flash_page
    assert "v1 → v2" in flash_page and "v2 → v3" not in flash_page
    assert "v2" in flash_page


def test_infeasible_correction_rolls_back(client):
    _import(client)
    csv_before = client.get("/runs/RUN-T/csv").text
    resp = client.post(
        "/runs/RUN-T/correct",
        data={"expected_version": 1, "seg_index": "1", "depth": "10999", "note": "坏数据"},
    )
    assert resp.status_code == 303
    page = client.get("/runs/RUN-T").text
    assert "约束无解" in page and "当前供数未做任何改动" in page
    # 版本未增长、供数未变、无更正历史
    assert "v1" in page
    assert client.get("/runs/RUN-T/csv").text == csv_before
    assert "暂无更正记录" in page


def test_import_bad_payload_persists_nothing(client):
    bad = dict(DEMO_PAYLOAD)
    bad["gaps"] = [{"low": 999, "high": 1000}, {"low": 0, "high": 0}]  # 下限就放不下
    resp = client.post("/import", data={"payload_text": json.dumps(bad)})
    assert resp.status_code == 303
    home = client.get("/").text
    assert "RUN-T" not in home
    # 正常数据仍可导入
    assert _import(client).status_code == 303


def test_json_file_upload(client):
    files = {"payload_file": ("run.json", json.dumps(DEMO_PAYLOAD).encode(), "application/json")}
    resp = client.post("/import", data={"payload_text": ""}, files=files)
    assert resp.status_code == 303
    assert client.get("/runs/RUN-T").status_code == 200
