"""混样化验单的 Web 全流程、持久化与并发测试。"""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app.assay import ContradictionError, SlipValidationError
from app.storage import Store

PAYLOAD = {
    "run_id": "RUN-T",
    "start_depth": 10000,
    "end_depth": 12500,
    "segments": [
        {"length": 500, "samples": [
            {"sample_no": "S1", "start": 20, "end": 100, "assay": "0.12"},
            {"sample_no": "S2", "start": 400, "end": 480, "assay": ""},
        ]},
        {"length": 600, "samples": [
            {"sample_no": "S3", "start": 50, "end": 120, "assay": "0.31"},
            {"sample_no": "S4", "start": 400, "end": 460, "assay": ""},
        ]},
        {"length": 700, "samples": [
            {"sample_no": "S5", "start": 500, "end": 650, "assay": ""},
        ]},
    ],
    "gaps": [{"low": 100, "high": 300}, {"low": 100, "high": 300}],
    "boundaries": [11200],
    "layers": ["A", "B"],
    "measured_starts": [],
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    import importlib

    import app.main as main
    importlib.reload(main)
    return TestClient(main.app, follow_redirects=False)


def _import(client):
    return client.post("/import", data={"payload_text": json.dumps(PAYLOAD, ensure_ascii=False)})


def _create(client, no, a, b, mean):
    return client.post(
        "/runs/RUN-T/slips",
        data={"slip_no": no, "sample_a": a, "sample_b": b, "mean": mean},
    )


def _seed_three_slips(client):
    _import(client)
    assert _create(client, "H001", "S1", "S2", "0.10").status_code == 303
    assert _create(client, "H002", "S2", "S4", "0.175").status_code == 303
    assert _create(client, "H003", "S4", "S5", "0.66").status_code == 303


# --------------------------------------------------------------------------- #
# Web 全流程
# --------------------------------------------------------------------------- #
def test_create_slips_recovers_missing_values(client):
    _seed_three_slips(client)
    page = client.get("/runs/RUN-T").text
    # 链式复原：S2=0.08、S4=0.27、S5=1.05，全部唯一
    assert "取值唯一，已复原" in page
    for value in ("0.08", "0.27", "1.05"):
        assert value in page
    assert "没有待补测的缺测样品" in page
    # 化验单与关系和（2×均值）同页展示
    assert "H001" in page and "0.2" in page and "0.35" in page and "1.32" in page


def test_contradictory_create_rejected_and_state_kept(client):
    _seed_three_slips(client)
    before = client.get("/runs/RUN-T").text
    # S1=0.12、S3=0.31 均已知，要求 S1+S3=20 与已有值矛盾
    resp = _create(client, "H009", "S1", "S3", "10")
    assert resp.status_code == 303
    page = client.get("/runs/RUN-T").text
    assert "矛盾" in page and "未做任何改动" in page
    # 被拒绝的单不生效（错误提示中会提到该单号，但化验单表里没有它）
    assert "/slips/H009/withdraw" not in page
    # 复原结果与之前完全一致
    assert "0.08" in page and "0.27" in page and "1.05" in page
    assert "没有待补测的缺测样品" in page
    assert before.count("H001") >= 1


def test_negative_value_contradiction_explained(client):
    _import(client)
    # S1=0.12 已知，要求 S1+S2=0.1 -> S2=-0.02
    _create(client, "H001", "S1", "S2", "0.05")
    page = client.get("/runs/RUN-T").text
    assert "非负约束" in page and "未做任何改动" in page
    assert "/slips/H001/withdraw" not in page


def test_correct_slip_recomputes_results(client):
    _seed_three_slips(client)
    resp = client.post(
        "/runs/RUN-T/slips/H003/correct",
        data={"sample_a": "S4", "sample_b": "S5", "mean": "0.665"},
    )
    assert resp.status_code == 303
    page = client.get("/runs/RUN-T").text
    assert "1.06" in page  # S5 = 1.33 - 0.27
    assert "更正成功" in page


def test_correct_to_contradiction_rejected_and_old_slip_kept(client):
    _seed_three_slips(client)
    # 把 H001 均值改成 5 -> S2=9.88 -> S4=0.35-9.88<0，违反非负约束
    resp = client.post(
        "/runs/RUN-T/slips/H001/correct",
        data={"sample_a": "S1", "sample_b": "S2", "mean": "5"},
    )
    assert resp.status_code == 303
    page = client.get("/runs/RUN-T").text
    assert "非负约束" in page and "未做任何改动" in page
    # 旧单原样保留，复原结果不变
    assert "0.10" in page and "0.08" in page and "1.05" in page


def test_withdraw_removes_no_longer_unique_values(client):
    _seed_three_slips(client)
    resp = client.post("/runs/RUN-T/slips/H002/withdraw")
    assert resp.status_code == 303
    page = client.get("/runs/RUN-T").text
    assert "已撤销" in page
    # S2 仍由 H001 唯一复原；S4、S5 不再唯一，移出复原清单、标为待补测
    assert "0.08" in page
    assert "待补测" in page
    recovered_section = page.split("取值唯一，已复原")[1].split("待补测")[0]
    assert "S4" not in recovered_section and "S5" not in recovered_section
    pending_section = page.split("待补测")[1]
    assert "S4" in pending_section and "S5" in pending_section


def test_withdraw_nonexistent_slip_rejected(client):
    _import(client)
    client.post("/runs/RUN-T/slips/H404/withdraw")
    page = client.get("/runs/RUN-T").text
    assert "不存在，无法撤销" in page


def test_invalid_slip_fields_rejected(client):
    _import(client)
    _create(client, "H001", "S1", "S2", "0.10")
    # 单号重复（flash 一次只带一条，逐个提交后立即查看）
    _create(client, "H001", "S1", "S3", "0.2")
    assert "已存在" in client.get("/runs/RUN-T").text
    # 相同样品、未知样品、非法均值
    _create(client, "H002", "S2", "S2", "0.1")
    assert "两个不同的原样编号" in client.get("/runs/RUN-T").text
    _create(client, "H003", "S1", "ZZ", "0.1")
    assert "不是本回次的样品" in client.get("/runs/RUN-T").text
    _create(client, "H004", "S1", "S2", "-1")
    assert "非负十进制" in client.get("/runs/RUN-T").text
    # 只有第一张单生效
    page = client.get("/runs/RUN-T").text
    assert "/slips/H001/withdraw" in page
    for no in ("H002", "H003", "H004"):
        assert f"/slips/{no}/withdraw" not in page


def test_slip_routes_unknown_run(client):
    resp = _create(client, "H1", "S1", "S2", "1")
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    home = client.get("/").text
    assert "不存在" in home


# --------------------------------------------------------------------------- #
# 持久化：重启后仍可查询
# --------------------------------------------------------------------------- #
def test_store_persists_slips_across_reopen(tmp_path):
    db = tmp_path / "runs.db"
    store = Store(db)
    store.save_new_run(PAYLOAD)
    store.create_slip("RUN-T", "H001", "S1", "S2", "0.10")
    store.create_slip("RUN-T", "H002", "S2", "S4", "0.175")

    # 模拟重启：用同一数据库文件新建 Store
    store2 = Store(db)
    view = store2.assay_view("RUN-T")
    assert [s.slip_no for s in view.slips] == ["H001", "H002"]
    got = {r.sample_no: r.value_text for r in view.analysis.recovered}
    assert got == {"S2": "0.08", "S4": "0.27"}

    store2.withdraw_slip("RUN-T", "H002")
    store3 = Store(db)
    view3 = store3.assay_view("RUN-T")
    assert [s.slip_no for s in view3.slips] == ["H001"]
    got3 = {r.sample_no: r.value_text for r in view3.analysis.recovered}
    assert got3 == {"S2": "0.08"}
    assert [p.sample_no for p in view3.analysis.pending] == ["S4", "S5"]


def test_assay_view_matches_same_effective_data(tmp_path):
    # 页面区块的化验单与复原结果来自同一事务快照
    store = Store(tmp_path / "runs.db")
    store.save_new_run(PAYLOAD)
    store.create_slip("RUN-T", "H001", "S1", "S2", "0.10")
    view = store.assay_view("RUN-T")
    assert len(view.slips) == 1
    assert view.slips[0].total_text == "0.2"
    assert {r.sample_no for r in view.analysis.recovered} == {"S2"}
    # 更正后快照同步变化
    store.correct_slip("RUN-T", "H001", "S1", "S2", "0.20")
    view2 = store.assay_view("RUN-T")
    assert view2.slips[0].mean_text == "0.20"
    assert {r.sample_no: r.value_text for r in view2.analysis.recovered} == {"S2": "0.28"}


# --------------------------------------------------------------------------- #
# 并发：相互矛盾的化验单不能同时生效
# --------------------------------------------------------------------------- #
def test_concurrent_contradictory_slips_cannot_both_commit(tmp_path):
    store = Store(tmp_path / "runs.db")
    store.save_new_run(PAYLOAD)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(no, mean):
        barrier.wait()
        try:
            store.create_slip("RUN-T", no, "S2", "S4", mean)
            outcomes.append((no, "ok"))
        except ContradictionError:
            outcomes.append((no, "conflict"))

    # 两张单各自可行，但 S2+S4=2 与 S2+S4=4 不能同时成立
    t1 = threading.Thread(target=worker, args=("W1", "1.0"))
    t2 = threading.Thread(target=worker, args=("W2", "2.0"))
    t1.start(); t2.start(); t1.join(); t2.join()

    oks = [o for o in outcomes if o[1] == "ok"]
    conflicts = [o for o in outcomes if o[1] == "conflict"]
    assert len(oks) == 1 and len(conflicts) == 1
    view = store.assay_view("RUN-T")
    assert len(view.slips) == 1
    assert view.slips[0].slip_no == oks[0][0]


def test_concurrent_same_slip_no_only_one_commits(tmp_path):
    store = Store(tmp_path / "runs.db")
    store.save_new_run(PAYLOAD)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(mean):
        barrier.wait()
        try:
            store.create_slip("RUN-T", "DUP", "S1", "S2", mean)
            outcomes.append("ok")
        except SlipValidationError:
            outcomes.append("duplicate")

    t1 = threading.Thread(target=worker, args=("0.10",))
    t2 = threading.Thread(target=worker, args=("0.20",))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(outcomes) == ["duplicate", "ok"]
    assert len(store.assay_view("RUN-T").slips) == 1
