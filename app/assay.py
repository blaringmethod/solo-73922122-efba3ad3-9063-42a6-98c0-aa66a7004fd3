"""两两等量混样化验单的精确推算引擎。

模型
----
回次内每个原样样品对应一个非负十进制变量 x_i。已有原样值（化验值非空白）
直接钉死 x_i；每张化验单给出一条等式约束：

    x_a + x_b = 2 × 混样均值      （等量混样，混样均值为两值的算术平均数）

所有参与推算的数值一律用 decimal.Decimal 精确处理，不接受负数与科学计数法。

把样品看作顶点、化验单看作边，每个连通块独立求解：

- 块内含已有值顶点：从已有值出发沿边传播，块内全部唯一确定；
  传播冲突或推出负值即矛盾（改动被拒绝）。
- 块内无已有值：块内变量可写成 c ± t（t 为全块共享的自由量）。
  块内出现奇数环时 t 被唯一钉死；否则 t 由非负约束限定区间 [lo, hi]：
  lo > hi 为矛盾；lo == hi 时全块取值唯一；lo < hi 时块内取值不唯一。

缺测样品（化验值空白）在所有非负解中取值唯一的，列入复原清单并给出复原值；
其余缺测样品标为待补测。非空白但无法解析为非负十进制的原样值不参与推算，
按缺测处理并单独给出提示。
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from decimal import Decimal


class AssayError(Exception):
    """化验单推算层错误基类（信息为中文，可直接展示给数据员）。"""


class SlipValidationError(AssayError):
    """化验单字段本身不合法。"""


class ContradictionError(AssayError):
    """与现行化验单、已有原样值联立后不存在非负解。"""


_DECIMAL_RE = re.compile(r"^\d+(\.\d+)?$")
MAX_SLIP_NO_LEN = 40
MAX_MEAN_LEN = 40


def fmt_decimal(d: Decimal) -> str:
    """定点显示并去掉多余的末尾零，不用科学计数法。"""
    if d == 0:
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def parse_nonneg_decimal(text, field_name: str) -> Decimal:
    """把录入文本解析为非负十进制数；不合法时抛 SlipValidationError。"""
    t = (text or "").strip()
    if not t:
        raise SlipValidationError(f"{field_name}不能为空。")
    if len(t) > MAX_MEAN_LEN:
        raise SlipValidationError(f"{field_name}位数过长（超过 {MAX_MEAN_LEN} 个字符）。")
    if not _DECIMAL_RE.match(t):
        raise SlipValidationError(
            f"{field_name}必须是非负十进制数（如 0、1.25），收到：{t!r}。"
        )
    return Decimal(t)


@dataclass(frozen=True)
class Slip:
    """一张两两等量混样化验单：x_a + x_b = 2 × mean。"""

    slip_no: str
    sample_a: str
    sample_b: str
    mean: Decimal
    mean_text: str  # 录入原文，用于回显

    @property
    def total(self) -> Decimal:
        return self.mean * 2

    @property
    def total_text(self) -> str:
        return fmt_decimal(self.total)


def validate_slip(slip_no, sample_a, sample_b, mean_text, sample_nos) -> Slip:
    """校验录入字段并构造 Slip；sample_nos 为本回次全部原样编号集合。"""
    no = (slip_no or "").strip()
    if not no:
        raise SlipValidationError("单号不能为空。")
    if len(no) > MAX_SLIP_NO_LEN:
        raise SlipValidationError(f"单号过长（超过 {MAX_SLIP_NO_LEN} 个字符）。")
    if any(ch.isspace() or ch == "/" for ch in no):
        raise SlipValidationError(f"单号不能包含空白或斜杠，收到：{no!r}。")
    a = (sample_a or "").strip()
    b = (sample_b or "").strip()
    if a not in sample_nos:
        raise SlipValidationError(f"原样编号 {a!r} 不是本回次的样品。")
    if b not in sample_nos:
        raise SlipValidationError(f"原样编号 {b!r} 不是本回次的样品。")
    if a == b:
        raise SlipValidationError(
            f"一张化验单需填写两个不同的原样编号，收到相同的 {a!r}。"
        )
    mean = parse_nonneg_decimal(mean_text, "混样均值")
    return Slip(no, a, b, mean, (mean_text or "").strip())


# --------------------------------------------------------------------------- #
# 推算结果
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KnownValue:
    sample_no: str
    text: str  # 已有原样值原文


@dataclass(frozen=True)
class InvalidAssay:
    sample_no: str
    raw: str  # 非空白但无法解析，未参与推算


@dataclass(frozen=True)
class RecoveredSample:
    sample_no: str
    value: Decimal
    value_text: str
    basis: str  # 取值唯一的依据说明


@dataclass(frozen=True)
class PendingSample:
    sample_no: str
    reason: str


@dataclass
class AssayAnalysis:
    known: list[KnownValue]
    invalid: list[InvalidAssay]
    recovered: list[RecoveredSample]  # 取值唯一的缺测样品
    pending: list[PendingSample]  # 待补测的缺测样品


def analyze(sample_assays: list[tuple[str, str]], slips: list[Slip]) -> AssayAnalysis:
    """结合现行化验单与已有原样值，推算缺测样品的唯一复原值。

    sample_assays 为按段内顺序排列的 (样品编号, 化验值原文) 列表，空白表示缺测。
    系统不存在非负解时抛 ContradictionError（存储层据此拒绝改动）。
    """
    known: dict[str, Decimal] = {}
    known_text: dict[str, str] = {}
    invalid: list[InvalidAssay] = []
    missing: list[str] = []
    for no, raw in sample_assays:
        t = (raw or "").strip()
        if not t:
            missing.append(no)
        elif _DECIMAL_RE.match(t):
            known[no] = Decimal(t)
            known_text[no] = t
        else:
            invalid.append(InvalidAssay(no, raw))
            missing.append(no)  # 不参与推算，按缺测处理并在页面提示
    missing_set = set(missing)

    # 建图：样品为顶点，化验单为边
    adj: dict[str, list[tuple[str, Decimal, Slip]]] = {}
    for slip in slips:
        total = slip.total
        adj.setdefault(slip.sample_a, []).append((slip.sample_b, total, slip))
        adj.setdefault(slip.sample_b, []).append((slip.sample_a, total, slip))

    recovered: dict[str, RecoveredSample] = {}
    pending_reason: dict[str, str] = {}
    visited: set[str] = set()
    for start in adj:
        if start in visited:
            continue
        comp: list[str] = []
        stack = [start]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            comp.append(n)
            stack.extend(m for m, _, _ in adj[n] if m not in visited)

        values, kind, info = _solve_component(comp, adj, known)
        slip_nos = info["slip_nos"]
        if kind == "free":
            ranges = info["ranges"]
            for n in comp:
                if n in missing_set:
                    lo, hi = ranges[n]
                    pending_reason[n] = (
                        f"化验单 {'、'.join(slip_nos)} 不足以唯一确定："
                        f"该样品可在 [{fmt_decimal(lo)}, {fmt_decimal(hi)}] 内取值"
                    )
        else:
            anchors = sorted(n for n in comp if n in known)
            basis = _basis_text(kind, anchors, known_text, slip_nos)
            for n in comp:
                if n in missing_set:
                    recovered[n] = RecoveredSample(
                        n, values[n], fmt_decimal(values[n]), basis
                    )

    for n in missing:
        if n not in adj and n not in recovered:
            pending_reason[n] = "没有任何化验单涉及该样品，无法推算"

    return AssayAnalysis(
        known=[KnownValue(n, known_text[n]) for n, _ in sample_assays if n in known],
        invalid=invalid,
        recovered=[recovered[n] for n in missing if n in recovered],
        pending=[PendingSample(n, pending_reason[n]) for n in missing if n in pending_reason],
    )


def _basis_text(kind: str, anchors: list[str], known_text: dict[str, str],
                slip_nos: list[str]) -> str:
    slips_str = "、".join(slip_nos)
    if kind == "pin":
        anchor_str = "、".join(f"{n}={known_text[n]}" for n in anchors)
        return f"已有原样值 {anchor_str} 与化验单 {slips_str} 联立，取值唯一"
    if kind == "cycle":
        return f"化验单 {slips_str} 构成闭环，联立求解取值唯一"
    return f"化验单 {slips_str} 加非负约束，可行区间缩为一点，取值唯一"


def _solve_component(comp: list[str], adj, known: dict[str, Decimal]):
    """求解一个连通块。

    返回 (values, kind, info)：
    - values 非空时块内全部唯一确定（kind 为 pin/cycle/bound）；
    - kind 为 free 时 values 为 None，info["ranges"] 给出各顶点可行范围。
    不存在非负解时抛 ContradictionError。
    """
    slip_nos = sorted({slip.slip_no for n in comp for _, _, slip in adj[n]})
    pins = [n for n in comp if n in known]

    if pins:
        values: dict[str, Decimal] = {}
        prov: dict[str, str] = {}
        dq = deque()
        for p in pins:
            values[p] = known[p]
            prov[p] = "已有原样值"
            dq.append(p)
        while dq:
            u = dq.popleft()
            for v, total, slip in adj[u]:
                expect = total - values[u]
                if v in values:
                    if values[v] != expect:
                        raise ContradictionError(
                            f"化验单 {slip.slip_no}"
                            f"（{slip.sample_a} + {slip.sample_b} = {fmt_decimal(total)}）"
                            f"与现有数据矛盾：{u} 已确定为 {fmt_decimal(values[u])}"
                            f"（{prov[u]}），{v} 已确定为 {fmt_decimal(values[v])}"
                            f"（{prov[v]}），两者之和为 "
                            f"{fmt_decimal(values[u] + values[v])}，"
                            f"不等于该单要求的 {fmt_decimal(total)}。"
                        )
                else:
                    values[v] = expect
                    prov[v] = f"化验单 {slip.slip_no} 结合 {u}={fmt_decimal(values[u])}"
                    dq.append(v)
        for n, val in values.items():
            if val < 0:
                raise ContradictionError(
                    f"按现行化验单与已有原样值推算，{n} = {fmt_decimal(val)}，"
                    f"违反非负约束（{prov[n]}）。"
                )
        return values, "pin", {"slip_nos": slip_nos}

    # 块内无已有值：value = c + s·t，s ∈ {+1, -1}，t 为全块共享自由量
    root = comp[0]
    expr: dict[str, tuple[Decimal, int]] = {root: (Decimal(0), 1)}
    parent: dict[str, tuple[str, Slip]] = {}
    t_value: Decimal | None = None
    dq = deque([root])
    while dq:
        u = dq.popleft()
        cu, su = expr[u]
        for v, total, slip in adj[u]:
            need_c, need_s = total - cu, -su
            if v not in expr:
                expr[v] = (need_c, need_s)
                parent[v] = (u, slip)
                dq.append(v)
                continue
            cv, sv = expr[v]
            if sv == need_s:
                if cv != need_c:
                    cyc = _cycle_slip_nos(parent, u, v, slip)
                    raise ContradictionError(
                        f"化验单 {'、'.join(cyc)} 构成闭环且相互矛盾："
                        f"沿闭环推算 {v} 的两种结果始终相差 "
                        f"{fmt_decimal(abs(cv - need_c))}，"
                        "无论其余样品取何值都无法同时满足这些化验单。"
                    )
            else:
                # 奇数环：c_v + s_v·t = need_c + need_s·t，钉死 t 并折算为常数
                t_value = (need_c - cv) / (sv - need_s)
                for n2, (c2, s2) in list(expr.items()):
                    expr[n2] = (c2 + s2 * t_value, 0)

    if t_value is not None:
        values = {n: c for n, (c, s) in expr.items()}
        for n, val in values.items():
            if val < 0:
                raise ContradictionError(
                    f"化验单 {'、'.join(slip_nos)} 构成闭环，"
                    f"唯一确定 {n} = {fmt_decimal(val)}，违反非负约束。"
                )
        return values, "cycle", {"slip_nos": slip_nos}

    # 纯二分块：t ∈ [lo, hi]（由非负约束夹出）
    lo = hi = None
    lo_node = hi_node = ""
    for n, (c, s) in expr.items():
        if s == 1:
            if lo is None or -c > lo:
                lo, lo_node = -c, n
        else:
            if hi is None or c < hi:
                hi, hi_node = c, n
    if lo > hi:
        raise ContradictionError(
            f"化验单 {'、'.join(slip_nos)} 在非负约束下无解："
            f"保证 {lo_node} ≥ 0 需要自由量 ≥ {fmt_decimal(lo)}，"
            f"保证 {hi_node} ≥ 0 需要自由量 ≤ {fmt_decimal(hi)}，两者不能同时成立。"
        )
    if lo == hi:
        values = {n: c + s * lo for n, (c, s) in expr.items()}
        return values, "bound", {"slip_nos": slip_nos}
    ranges = {}
    for n, (c, s) in expr.items():
        ranges[n] = (c + lo, c + hi) if s == 1 else (c - hi, c - lo)
    return None, "free", {"slip_nos": slip_nos, "ranges": ranges}


def _cycle_slip_nos(parent: dict, u: str, v: str, cur_slip: Slip) -> list[str]:
    """沿 BFS 父指针收集冲突环上的化验单单号（含当前单）。"""
    anc: set[str] = set()
    x = u
    while x is not None:
        anc.add(x)
        x = parent.get(x, (None, None))[0]
    nos = [cur_slip.slip_no]
    x = v
    while x not in anc:
        px, pslip = parent[x]
        nos.append(pslip.slip_no)
        x = px
    y = u
    while y != x:
        py, pslip = parent[y]
        nos.append(pslip.slip_no)
        y = py
    seen: list[str] = []
    for no in nos:
        if no not in seen:
            seen.append(no)
    return seen
