"""取芯回次孔深可行区间求解引擎。

深度单位：整数毫米；所有区间左闭右开。

模型
----
一个回次有 n 个按顺序排列的岩芯段，段实长 L_j。相邻段 j-1 与 j 之间的
缺失长度 g_j 满足 low_j <= g_j <= high_j（共 n-1 个变量）。
令 P_0 = 0，P_j = g_1 + ... + g_j（前 j 个缺失量之和），则

    段 j 起点孔深  s_j = 回次起点 + P_j
    段 j 终点孔深  e_j = s_j + L_j

回次起止孔深之差必须容纳全部岩芯：P_{n-1} <= (终孔 - 起始) - 各段实长之和，
即顶部（或终孔处）剩余缺失为非负自由量。数据员录入的段起点实测孔深
等价于等式约束 P_j = 实测值 - 回次起始。

对每个前缀 P_j 分别求所有可行排布下的最小值/最大值，即可得到各段、各样品
起止孔深的可行范围。
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field


class EngineError(Exception):
    """引擎层错误基类（信息为中文，可直接展示给数据员）。"""


class ValidationError(EngineError):
    """录入数据本身不合法。"""


class InfeasibleError(EngineError):
    """约束无可行解。"""


@dataclass(frozen=True)
class SampleInput:
    sample_no: str
    start: int  # 段内相对起点（含），毫米
    end: int  # 段内相对终点（不含），毫米
    assay: str  # 化验值，原样保存字符串


@dataclass(frozen=True)
class SegmentInput:
    length: int
    samples: list[SampleInput] = field(default_factory=list)


@dataclass(frozen=True)
class GapInput:
    low: int
    high: int


@dataclass(frozen=True)
class MeasuredStart:
    seg_index: int
    depth: int


@dataclass(frozen=True)
class RunInput:
    run_id: str
    start_depth: int
    end_depth: int
    segments: list[SegmentInput]
    gaps: list[GapInput]
    boundaries: list[int]
    layers: list[str]
    measured_starts: list[MeasuredStart] = field(default_factory=list)


@dataclass(frozen=True)
class SampleResult:
    sample_no: str
    assay: str
    seg_index: int
    start_lo: int
    start_hi: int
    end_lo: int
    end_hi: int
    layer: str | None  # 所有可行排布中均落入同一地层时为层位名，否则 None
    reason: str  # 未入仓时的原因，入仓时为空串


@dataclass(frozen=True)
class RunResult:
    run_id: str
    start_depth: int
    end_depth: int
    seg_start_lo: list[int]
    seg_start_hi: list[int]
    seg_end_lo: list[int]
    seg_end_hi: list[int]
    samples: list[SampleResult]
    total_gap_low: int
    total_gap_high: int


# --------------------------------------------------------------------------- #
# 录入解析与校验
# --------------------------------------------------------------------------- #
def _as_int(value, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        # 兼容 "123" 这类字符串录入，但拒绝 1.5、None、布尔
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise ValidationError(f"{field_name}必须是整数毫米值，收到：{value!r}")
    return value


def _as_nonneg_int(value, field_name: str) -> int:
    v = _as_int(value, field_name)
    if v < 0:
        raise ValidationError(f"{field_name}不能为负数，收到：{v}")
    return v


def parse_payload(payload: dict) -> RunInput:
    """把页面提交的 JSON/dict 解析并全量校验为 RunInput。"""
    if not isinstance(payload, dict):
        raise ValidationError("提交内容必须是 JSON 对象")

    run_id = str(payload.get("run_id", "")).strip()
    if not run_id:
        raise ValidationError("回次编号 run_id 不能为空")

    start_depth = _as_nonneg_int(payload.get("start_depth"), "回次起始孔深")
    end_depth = _as_nonneg_int(payload.get("end_depth"), "回次终止孔深")
    if start_depth >= end_depth:
        raise ValidationError(
            f"回次终止孔深（{end_depth}）必须大于起始孔深（{start_depth}）"
        )

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValidationError("岩芯段 segments 至少要有 1 段")

    segments: list[SegmentInput] = []
    sample_nos: set[str] = set()
    for idx, raw_seg in enumerate(raw_segments):
        if not isinstance(raw_seg, dict):
            raise ValidationError(f"第 {idx + 1} 个岩芯段格式不正确")
        length = _as_nonneg_int(raw_seg.get("length"), f"第 {idx + 1} 段岩芯实长")
        if length <= 0:
            raise ValidationError(f"第 {idx + 1} 段岩芯实长必须为正数，收到：{length}")

        raw_samples = raw_seg.get("samples", [])
        if not isinstance(raw_samples, list):
            raise ValidationError(f"第 {idx + 1} 段的样品列表格式不正确")
        samples: list[SampleInput] = []
        for s_idx, raw_s in enumerate(raw_samples):
            if not isinstance(raw_s, dict):
                raise ValidationError(f"第 {idx + 1} 段第 {s_idx + 1} 个样品格式不正确")
            sample_no = str(raw_s.get("sample_no", "")).strip()
            if not sample_no:
                raise ValidationError(f"第 {idx + 1} 段第 {s_idx + 1} 个样品缺少样品编号")
            if sample_no in sample_nos:
                raise ValidationError(f"样品编号重复：{sample_no}")
            s_start = _as_nonneg_int(raw_s.get("start"), f"样品 {sample_no} 段内起点")
            s_end = _as_nonneg_int(raw_s.get("end"), f"样品 {sample_no} 段内终点")
            if not (0 <= s_start < s_end <= length):
                raise ValidationError(
                    f"样品 {sample_no} 的段内位置不合法：需满足 0 <= 起点 < 终点 <= 段长"
                    f"（{length}），收到起点 {s_start}、终点 {s_end}"
                )
            assay = raw_s.get("assay", "")
            samples.append(
                SampleInput(sample_no, s_start, s_end, "" if assay is None else str(assay))
            )
            sample_nos.add(sample_no)
        segments.append(SegmentInput(length, samples))

    n = len(segments)
    raw_gaps = payload.get("gaps", [])
    if not isinstance(raw_gaps, list):
        raise ValidationError("缺失段 gaps 必须是数组")
    if len(raw_gaps) != n - 1:
        raise ValidationError(
            f"{n} 个岩芯段之间应有 {n - 1} 个缺失段约束，实际收到 {len(raw_gaps)} 个"
        )
    gaps: list[GapInput] = []
    for idx, raw_g in enumerate(raw_gaps):
        if not isinstance(raw_g, dict):
            raise ValidationError(f"第 {idx + 1} 个缺失段格式不正确")
        low = _as_nonneg_int(raw_g.get("low"), f"第 {idx + 1} 个缺失段下限")
        high = _as_nonneg_int(raw_g.get("high"), f"第 {idx + 1} 个缺失段上限")
        if low > high:
            raise ValidationError(
                f"第 {idx + 1} 个缺失段下限（{low}）不能大于上限（{high}）"
            )
        gaps.append(GapInput(low, high))

    raw_boundaries = payload.get("boundaries", [])
    if not isinstance(raw_boundaries, list):
        raise ValidationError("地层分界 boundaries 必须是数组")
    boundaries: list[int] = []
    for idx, raw_b in enumerate(raw_boundaries):
        b = _as_nonneg_int(raw_b, f"第 {idx + 1} 个地层分界深度")
        if not (start_depth < b < end_depth):
            raise ValidationError(
                f"地层分界 {b} 必须严格位于回次起止孔深（{start_depth}, {end_depth}）之间"
            )
        if boundaries and b <= boundaries[-1]:
            raise ValidationError("地层分界必须严格递增且不能重复")
        boundaries.append(b)

    raw_layers = payload.get("layers", [])
    if not isinstance(raw_layers, list) or not raw_layers:
        raise ValidationError("地层名称 layers 不能为空")
    if len(raw_layers) != len(boundaries) + 1:
        raise ValidationError(
            f"地层名称数量（{len(raw_layers)}）应为地层分界数量（{len(boundaries)}）加 1"
        )
    layers = [str(x).strip() for x in raw_layers]
    if any(not name for name in layers):
        raise ValidationError("地层名称不能为空字符串")

    raw_measured = payload.get("measured_starts", [])
    if not isinstance(raw_measured, list):
        raise ValidationError("实测段起点 measured_starts 必须是数组")
    measured: list[MeasuredStart] = []
    seen_seg: set[int] = set()
    for idx, raw_m in enumerate(raw_measured):
        if not isinstance(raw_m, dict):
            raise ValidationError(f"第 {idx + 1} 个实测起点格式不正确")
        seg_index = _as_nonneg_int(raw_m.get("seg_index"), "实测起点的岩芯段序号 seg_index")
        if seg_index >= n:
            raise ValidationError(
                f"实测起点段序号 {seg_index} 超出范围（共 {n} 段，序号 0..{n - 1}）"
            )
        if seg_index in seen_seg:
            raise ValidationError(f"第 {seg_index + 1} 段的实测起点重复录入")
        depth = _as_nonneg_int(raw_m.get("depth"), f"第 {seg_index + 1} 段实测起点孔深")
        if depth < start_depth:
            raise ValidationError(
                f"第 {seg_index + 1} 段实测起点孔深 {depth} 小于回次起始孔深 {start_depth}"
            )
        if depth >= end_depth:
            raise ValidationError(
                f"第 {seg_index + 1} 段实测起点孔深 {depth} 不小于回次终止孔深 {end_depth}"
            )
        measured.append(MeasuredStart(seg_index, depth))
        seen_seg.add(seg_index)

    total_core = sum(seg.length for seg in segments)
    if total_core > end_depth - start_depth:
        raise ValidationError(
            f"岩芯实长合计 {total_core} mm 超过回次进尺 {end_depth - start_depth} mm，"
            "即使没有任何缺失也放不下"
        )

    return RunInput(run_id, start_depth, end_depth, segments, gaps, boundaries, layers, measured)


# --------------------------------------------------------------------------- #
# 可行区间求解
# --------------------------------------------------------------------------- #
def solve(run: RunInput) -> RunResult:
    """根据 RunInput 推导全部可行排布下各段/各样品孔深的最小、最大值。"""
    n = len(run.segments)
    m = n - 1  # 缺失变量个数
    total_core = sum(seg.length for seg in run.segments)
    total_span = run.end_depth - run.start_depth
    total_gap_max = total_span - total_core  # P_m 的上限

    # 前缀 P 的等式锚点：P_0 = 0 恒成立，实测点补充等式约束
    anchors: dict[int, int] = {0: 0}
    for ms in run.measured_starts:
        anchors[ms.seg_index] = ms.depth - run.start_depth

    # 每个前缀 P_j 的可行范围 [lo, hi]
    p_lo = [0] * n
    p_hi = [0] * n

    anchor_indices = sorted(anchors)
    for block_pos in range(len(anchor_indices) - 1):
        j, k = anchor_indices[block_pos], anchor_indices[block_pos + 1]
        # 等式块：P_j、P_k 均被钉死，中间是 g_{j+1}..g_k，索引 j..k-1
        fixed_lo = sum(run.gaps[t].low for t in range(j, k))
        fixed_hi = sum(run.gaps[t].high for t in range(j, k))
        delta = anchors[k] - anchors[j]
        if not (fixed_lo <= delta <= fixed_hi):
            raise InfeasibleError(
                _anchor_conflict_msg(run, j, k, anchors[j], anchors[k], fixed_lo, fixed_hi, delta)
            )
        # 块内逐点求紧边界（q = P_{t+1} - P_j，块总量 delta 已固定）。
        # 下界取最大：本块变量全取下限、为其后变量保留 high 空间；
        # 上界取最小：当前变量取自身 high 其余取 low、为其后变量保留下限。
        p_lo[j] = p_hi[j] = anchors[j]
        running_lo = 0
        for t in range(j, k):
            running_lo += run.gaps[t].low
            remain_low = sum(run.gaps[u].low for u in range(t + 1, k))
            remain_high = sum(run.gaps[u].high for u in range(t + 1, k))
            q_lo = max(running_lo, delta - remain_high)
            q_hi = min(running_lo - run.gaps[t].low + run.gaps[t].high, delta - remain_low)
            p_lo[t + 1] = anchors[j] + q_lo
            p_hi[t + 1] = anchors[j] + q_hi
            if q_lo > q_hi:  # 理论上已被块级可行性检查覆盖，保险处理
                raise InfeasibleError(
                    f"约束无解：第 {t + 2} 段起点在相邻两个实测点之间没有可行排布。"
                )

    # 末端开放块：最后一个锚点 j 到 P_m，P_m 只有上限
    j = anchor_indices[-1]
    p_lo[j] = p_hi[j] = anchors[j]
    tail_low = sum(run.gaps[t].low for t in range(j, m))
    if anchors[j] + tail_low > total_gap_max:
        raise InfeasibleError(
            f"约束无解（当前供数未做任何改动）：第 {j + 1} 段实测起点之后各缺失段下限合计 "
            f"{tail_low} mm，而该实测点（{run.start_depth + anchors[j]} mm）到回次终孔之间"
            f"最多只能再容纳 {total_gap_max - anchors[j]} mm 的缺失。"
        )
    block_low = block_high = 0
    for t in range(j, m):
        block_low += run.gaps[t].low
        block_high += run.gaps[t].high
        # 相对锚点 P_j 的偏移 q = P_{t+1} - P_j。
        # 线性规划下部分和的紧边界：
        #   下界 = 本块变量全取下限（Σlow），同时不能挤占后续变量的 low；
        #   上界 = 本块变量全取上限（Σhigh）与“总预算为后续变量保留 low”取小。
        q_lo = block_low
        cap_by_highs = block_high
        cap_by_tail = total_gap_max - anchors[j] - sum(
            run.gaps[u].low for u in range(t + 1, m)
        )
        q_hi = min(cap_by_highs, cap_by_tail)
        p_lo[t + 1] = anchors[j] + q_lo
        p_hi[t + 1] = anchors[j] + q_hi
        if q_lo > q_hi:
            raise InfeasibleError(
                f"约束无解：第 {t + 2} 段起点的可行下界 "
                f"{run.start_depth + p_lo[t + 1]} mm 高于可行上界 "
                f"{run.start_depth + p_hi[t + 1]} mm。"
            )

    # 换算为绝对孔深，并推导样品可行范围与层位
    seg_start_lo = [run.start_depth + v for v in p_lo]
    seg_start_hi = [run.start_depth + v for v in p_hi]
    seg_end_lo = [seg_start_lo[i] + run.segments[i].length for i in range(n)]
    seg_end_hi = [seg_start_hi[i] + run.segments[i].length for i in range(n)]

    sample_results: list[SampleResult] = []
    for j, seg in enumerate(run.segments):
        for sample in seg.samples:
            start_lo = seg_start_lo[j] + sample.start
            start_hi = seg_start_hi[j] + sample.start
            end_lo = seg_end_lo[j] - (seg.length - sample.end)
            end_hi = seg_end_hi[j] - (seg.length - sample.end)
            layer, reason = _classify_layer(
                run, sample.sample_no, start_lo, start_hi, end_lo, end_hi
            )
            sample_results.append(
                SampleResult(
                    sample.sample_no,
                    sample.assay,
                    j,
                    start_lo,
                    start_hi,
                    end_lo,
                    end_hi,
                    layer,
                    reason,
                )
            )

    total_gap_low = sum(g.low for g in run.gaps)
    return RunResult(
        run.run_id,
        run.start_depth,
        run.end_depth,
        seg_start_lo,
        seg_start_hi,
        seg_end_lo,
        seg_end_hi,
        sample_results,
        total_gap_low,
        total_gap_max,
    )


def _anchor_conflict_msg(run, j, k, pj, pk, fixed_lo, fixed_hi, delta) -> str:
    left = "回次起始孔深" if j == 0 else f"第 {j + 1} 段实测起点"
    left_depth = run.start_depth + pj
    right_depth = run.start_depth + pk
    return (
        "约束无解（当前供数未做任何改动）："
        f"{left}（{left_depth} mm）与第 {k + 1} 段实测起点（{right_depth} mm）之间，"
        f"各缺失段允许的合计缺失为 [{fixed_lo}, {fixed_hi}] mm，"
        f"但两点孔深之差为 {delta} mm，不存在满足上下限的排布。"
    )


def _layer_name(run: RunInput, depth: int) -> str:
    """按左闭右开规则确定单个深度点所属地层。"""
    return run.layers[bisect_right(run.boundaries, depth)]


def _classify_layer(
    run: RunInput,
    sample_no: str,
    start_lo: int,
    start_hi: int,
    end_lo: int,
    end_hi: int,
) -> tuple[str | None, str]:
    """判断样品在所有可行排布中是否完整落入同一地层。

    样品可能占据的全部整数孔深为 [start_lo, end_hi - 1]；
    该范围内若不存在地层分界，则层位唯一。
    """
    first_layer = _layer_name(run, start_lo)
    last_layer = _layer_name(run, end_hi - 1)
    if first_layer == last_layer:
        return first_layer, ""
    crossed = [b for b in run.boundaries if start_lo <= b <= end_hi - 1]
    return None, (
        f"样品 {sample_no} 的可行孔深范围跨越地层分界 {crossed} mm，"
        f"可能层位为 {first_layer} 至 {last_layer}，未写进入仓 CSV"
    )
