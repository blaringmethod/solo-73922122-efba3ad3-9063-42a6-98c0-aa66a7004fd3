"""引擎求解器单元测试：可行区间、实测锚点、地层判定与无解场景。"""

import pytest

from app.engine import (
    InfeasibleError,
    ValidationError,
    parse_payload,
    solve,
)

DEMO = {
    "run_id": "R1",
    "start_depth": 10000,
    "end_depth": 12500,
    "segments": [
        {"length": 500, "samples": [
            {"sample_no": "S1", "start": 20, "end": 100, "assay": "0.12"},
            {"sample_no": "S2", "start": 400, "end": 480, "assay": "0.08"},
        ]},
        {"length": 600, "samples": [
            {"sample_no": "S3", "start": 50, "end": 120, "assay": "0.31"},
            {"sample_no": "S4", "start": 400, "end": 460, "assay": "0.27"},
        ]},
        {"length": 700, "samples": [
            {"sample_no": "S5", "start": 500, "end": 650, "assay": "1.05"},
        ]},
    ],
    "gaps": [{"low": 100, "high": 300}, {"low": 100, "high": 300}],
    "boundaries": [11200],
    "layers": ["A", "B"],
    "measured_starts": [],
}


def test_open_bounds_ranges():
    result = solve(parse_payload(DEMO))
    # P1 ∈ [100,300]（受 g1 自身上限约束），P2 ∈ [200,600]（总缺失上限 = 2500-1800 = 700）
    assert result.seg_start_lo == [10000, 10100, 10200]
    assert result.seg_start_hi == [10000, 10300, 10600]
    assert result.seg_end_hi[2] == 11300  # 最后一段终点最大 11300，顶部至少留 1200
    assert result.total_gap_high == 700

    by_no = {s.sample_no: s for s in result.samples}
    # S1 段内 [20,100)，起点随段起点整体平移
    assert (by_no["S1"].start_lo, by_no["S1"].start_hi) == (10020, 10020)
    assert (by_no["S1"].end_lo, by_no["S1"].end_hi) == (10100, 10100)
    # S5 段内 [500,650)，位于段 2：起点 [10700,11100]，终点 [10850,11250)
    assert (by_no["S5"].start_lo, by_no["S5"].start_hi) == (10700, 11100)
    assert (by_no["S5"].end_lo, by_no["S5"].end_hi) == (10850, 11250)


def test_layer_classification_crossing_and_fixed():
    result = solve(parse_payload(DEMO))
    by_no = {s.sample_no: s for s in result.samples}
    # S1/S2 永远在 A；S5 跨越 11200 分界，层位不唯一
    assert by_no["S1"].layer == "A" and by_no["S2"].layer == "A"
    assert by_no["S5"].layer is None and "11200" in by_no["S5"].reason
    layers = {s.sample_no: s.layer for s in result.samples}
    assert layers["S3"] == "A" and layers["S4"] == "A"


def test_measured_anchor_narrows_and_fixes_layer():
    payload = dict(DEMO)
    payload["measured_starts"] = [{"seg_index": 1, "depth": 10300}]
    result = solve(parse_payload(payload))
    # P1 被钉为 300：段 1 起点固定；段 2 起点受 g2 ∈ [100,300] 约束为 [10400,10600]
    assert result.seg_start_lo[1] == result.seg_start_hi[1] == 10300
    assert result.seg_start_lo[2] == 10400 and result.seg_start_hi[2] == 10600
    by_no = {s.sample_no: s for s in result.samples}
    # S5 此时范围 [10900,11100]..[11050,11250) 仍跨分界 → 仍不入仓
    assert by_no["S5"].layer is None

    payload["measured_starts"] = [
        {"seg_index": 1, "depth": 10300},
        {"seg_index": 2, "depth": 10400},
    ]
    result = solve(parse_payload(payload))
    by_no = {s.sample_no: s for s in result.samples}
    # 段 2 固定在 10400：S5 = [10900,11050)，完整落入 A
    assert by_no["S5"].layer == "A"
    assert result.seg_start_lo == result.seg_start_hi == [10000, 10300, 10400]


def test_infeasible_anchor_between_two_measurements():
    payload = dict(DEMO)
    # P1 = 500 超出 g1 ∈ [100,300]
    payload["measured_starts"] = [{"seg_index": 1, "depth": 10500}]
    with pytest.raises(InfeasibleError, match="约束无解"):
        solve(parse_payload(payload))


def test_infeasible_tail_overflow():
    payload = dict(DEMO)
    # 段 2 起点钉到 10600（P2=600），其后段长 700 → 11300 尚可；
    # 钉到 11000（P2=1000）超过总缺失上限 700 → 无解
    payload["measured_starts"] = [{"seg_index": 2, "depth": 11000}]
    with pytest.raises(InfeasibleError):
        solve(parse_payload(payload))


def test_equality_block_partial_sums_are_tight():
    """两个实测点之间总量固定时，中间前缀同时受前后变量上下限挤占。"""
    payload = {
        "run_id": "R4",
        "start_depth": 2000,
        "end_depth": 3000,
        "segments": [{"length": 100}, {"length": 100}, {"length": 100}],
        "gaps": [{"low": 10, "high": 50}, {"low": 20, "high": 80}],
        "boundaries": [],
        "layers": ["X"],
        # 钉死 P0=0、P2=60：g1+g2=60，g1∈[10,50]、g2∈[20,80]
        # P 只累计缺失量，故第 3 段（seg_index=2）实测起点 = 2000 + P2 = 2060；
        # 该段实际孔深起点还要加上前两段实长 200，即 2260
        "measured_starts": [{"seg_index": 2, "depth": 2060}],
    }
    result = solve(parse_payload(payload))
    # P1=g1 ∈ [max(10, 60-80)=10, min(50, 60-20)=40]
    assert result.seg_start_lo == [2000, 2010, 2060]
    assert result.seg_start_hi == [2000, 2040, 2060]


def test_single_segment_run():
    """只有一个岩芯段时没有缺失变量，所有孔深完全确定。"""
    payload = {
        "run_id": "R5",
        "start_depth": 5000,
        "end_depth": 5800,
        "segments": [{"length": 300, "samples": [
            {"sample_no": "U1", "start": 0, "end": 300, "assay": "9"},
        ]}],
        "gaps": [],
        "boundaries": [5300],
        "layers": ["上", "下"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    assert result.seg_start_lo == result.seg_start_hi == [5000]
    assert result.seg_end_lo == result.seg_end_hi == [5300]
    # 样品 [5000,5300) 终点恰为分界，左闭右开下完整属“上”层
    assert result.samples[0].layer == "上"
    assert result.total_gap_high == 500  # 顶部 500 mm 自由缺失


def test_zero_gap_bounds_allowed():
    """缺失上下限均为 0 是合法录入，表示段间无缺失。"""
    payload = {
        "run_id": "R6",
        "start_depth": 0,
        "end_depth": 500,
        "segments": [{"length": 100}, {"length": 100}],
        "gaps": [{"low": 0, "high": 0}],
        "boundaries": [],
        "layers": ["X"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    # 段起点只累计缺失量（均为 0），段实长体现在终点
    assert result.seg_start_lo == result.seg_start_hi == [0, 0]
    assert result.seg_end_lo == result.seg_end_hi == [100, 100]


def test_partial_sum_bounds_are_tight():
    """构造 3 个缺失变量，验证部分和上下限可同时取到（紧边界）。"""
    payload = {
        "run_id": "R2",
        "start_depth": 0,
        "end_depth": 1000,
        "segments": [{"length": 100}, {"length": 100}, {"length": 100}, {"length": 100}],
        "gaps": [
            {"low": 10, "high": 50},
            {"low": 20, "high": 80},
            {"low": 30, "high": 120},
        ],
        "boundaries": [],
        "layers": ["X"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    # 总缺失上限 = 1000-400 = 600
    # P1: lo=10, hi=min(50, 600-20-30=550)=50
    # P2: lo=30, hi=min(130, 600-30=570)=130
    # P3: lo=60, hi=min(250, 600)=250
    assert result.seg_start_lo == [0, 10, 30, 60]
    assert result.seg_start_hi == [0, 50, 130, 250]


def test_boundary_value_uses_half_open_intervals():
    """样品终点恰为分界点时不算跨层（左闭右开）。"""
    payload = {
        "run_id": "R3",
        "start_depth": 0,
        "end_depth": 500,
        "segments": [{"length": 200, "samples": [
            {"sample_no": "T1", "start": 100, "end": 200, "assay": "x"},
        ]}],
        "gaps": [],
        "boundaries": [200],
        "layers": ["L1", "L2"],
        "measured_starts": [],
    }
    result = solve(parse_payload(payload))
    assert result.samples[0].layer == "L1"
    # 样品起点恰为分界点时归入 L2
    payload["segments"][0]["samples"][0] = {
        "sample_no": "T1", "start": 0, "end": 200, "assay": "x"
    }
    payload["boundaries"] = [100]
    result = solve(parse_payload(payload))
    assert result.samples[0].layer is None  # [0,200) 跨越 100


def test_measured_depth_outside_run_rejected():
    """实测起点落在回次区间之外属于录入校验错误。"""
    payload = dict(DEMO)
    payload["measured_starts"] = [{"seg_index": 1, "depth": 12500}]  # 等于终孔
    with pytest.raises(ValidationError, match="不小于回次终止孔深"):
        parse_payload(payload)
    payload["measured_starts"] = [{"seg_index": 1, "depth": 9999}]  # 小于起始
    with pytest.raises(ValidationError, match="小于回次起始孔深"):
        parse_payload(payload)


def test_validation_errors():
    with pytest.raises(ValidationError):
        parse_payload({**DEMO, "segments": []})
    with pytest.raises(ValidationError):
        parse_payload({**DEMO, "gaps": [{"low": 300, "high": 100}, {"low": 0, "high": 0}]})
    bad = dict(DEMO)
    bad["segments"] = [dict(DEMO["segments"][0])]
    bad["gaps"] = []
    bad["boundaries"] = []
    bad["layers"] = ["A", "B"]  # 无分界却给两个层位
    with pytest.raises(ValidationError):
        parse_payload(bad)
    bad2 = dict(DEMO)
    bad2["segments"] = [{**seg, "samples": [
        {"sample_no": "BAD", "start": 400, "end": 400, "assay": "1"}
    ]} for seg in [DEMO["segments"][0]]]
    bad2["gaps"] = []
    with pytest.raises(ValidationError):
        parse_payload(bad2)
