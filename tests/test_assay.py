"""混样化验单推算引擎单元测试：精确十进制、唯一性判定、矛盾检测。"""

from decimal import Decimal

import pytest

from app.assay import (
    ContradictionError,
    SlipValidationError,
    analyze,
    fmt_decimal,
    parse_nonneg_decimal,
    validate_slip,
)

SAMPLES = [("A", "1.0"), ("B", ""), ("C", ""), ("D", ""), ("E", "")]
NOS = {s for s, _ in SAMPLES}


def slip(no, a, b, mean):
    return validate_slip(no, a, b, mean, NOS)


def recovered_map(analysis):
    return {r.sample_no: r.value for r in analysis.recovered}


def pending_map(analysis):
    return {p.sample_no: p.reason for p in analysis.pending}


# --------------------------------------------------------------------------- #
# 数值解析与显示
# --------------------------------------------------------------------------- #
def test_parse_nonneg_decimal_accepts_plain_decimal():
    assert parse_nonneg_decimal("0", "f") == Decimal("0")
    assert parse_nonneg_decimal(" 1.25 ", "f") == Decimal("1.25")
    assert parse_nonneg_decimal("000.500", "f") == Decimal("0.500")


@pytest.mark.parametrize("bad", ["", "  ", "-1", "1.", ".5", "1e3", "abc", "1,2", "NaN"])
def test_parse_nonneg_decimal_rejects_bad_input(bad):
    with pytest.raises(SlipValidationError):
        parse_nonneg_decimal(bad, "混样均值")


def test_fmt_decimal_strips_trailing_zeros_without_exponent():
    assert fmt_decimal(Decimal("0.10")) == "0.1"
    assert fmt_decimal(Decimal("2")) == "2"
    assert fmt_decimal(Decimal("0")) == "0"
    assert fmt_decimal(Decimal("1E+3")) == "1000"


def test_decimal_arithmetic_is_exact_not_float():
    # 0.1 与 0.3 这类值按十进制精确处理，避免浮点误差
    samples = [("X", "0.1"), ("Y", ""), ("Z", "")]
    nos = {s for s, _ in samples}
    s1 = validate_slip("H1", "X", "Y", "0.15", nos)  # Y = 0.30 - 0.1
    s2 = validate_slip("H2", "Y", "Z", "0.25", nos)  # Z = 0.50 - Y
    res = analyze(samples, [s1, s2])
    got = recovered_map(res)
    assert got["Y"] == Decimal("0.2") and got["Z"] == Decimal("0.3")
    texts = {r.sample_no: r.value_text for r in res.recovered}
    assert texts == {"Y": "0.2", "Z": "0.3"}


# --------------------------------------------------------------------------- #
# 字段校验
# --------------------------------------------------------------------------- #
def test_validate_slip_rejects_same_sample_twice():
    with pytest.raises(SlipValidationError, match="两个不同的原样编号"):
        validate_slip("H1", "A", "A", "1.0", NOS)


def test_validate_slip_rejects_unknown_sample():
    with pytest.raises(SlipValidationError, match="不是本回次的样品"):
        validate_slip("H1", "A", "ZZ", "1.0", NOS)


def test_validate_slip_rejects_empty_or_long_slip_no():
    with pytest.raises(SlipValidationError, match="单号不能为空"):
        validate_slip("  ", "A", "B", "1.0", NOS)
    with pytest.raises(SlipValidationError, match="单号过长"):
        validate_slip("X" * 41, "A", "B", "1.0", NOS)


def test_validate_slip_rejects_slip_no_with_space_or_slash():
    # 单号要出现在更正/撤销的 URL 路径中，空白与斜杠会破坏路由
    with pytest.raises(SlipValidationError, match="空白或斜杠"):
        validate_slip("H 1", "A", "B", "1.0", NOS)
    with pytest.raises(SlipValidationError, match="空白或斜杠"):
        validate_slip("H/1", "A", "B", "1.0", NOS)


# --------------------------------------------------------------------------- #
# 唯一性判定
# --------------------------------------------------------------------------- #
def test_chain_from_known_value_recovers_all():
    # A=1.0 已知；A+B=3.0 -> B=2.0；B+C=5.0 -> C=3.0
    a = analyze(SAMPLES, [slip("H1", "A", "B", "1.5"), slip("H2", "B", "C", "2.5")])
    got = recovered_map(a)
    assert got == {"B": Decimal("2.0"), "C": Decimal("3.0")}
    pend = pending_map(a)
    assert set(pend) == {"D", "E"}
    assert "没有任何化验单涉及" in pend["D"]


def test_odd_cycle_pins_values_without_known():
    # A+B=2, B+C=4, A+C=6 -> A=2, B=0, C=4（无已知值，奇环钉死）
    samples = [("A", ""), ("B", ""), ("C", "")]
    nos = {s for s, _ in samples}
    slips = [
        validate_slip("H1", "A", "B", "1", nos),
        validate_slip("H2", "B", "C", "2", nos),
        validate_slip("H3", "A", "C", "3", nos),
    ]
    a = analyze(samples, slips)
    got = recovered_map(a)
    assert got == {"A": Decimal("2"), "B": Decimal("0"), "C": Decimal("4")}
    assert all("闭环" in r.basis for r in a.recovered)


def test_free_component_is_pending_with_range():
    # 单个无锚点混样：A+B=4，两者都在 [0,4] 内不唯一
    samples = [("A", ""), ("B", "")]
    nos = set(dict(samples))
    a = analyze(samples, [validate_slip("H1", "A", "B", "2", nos)])
    assert not a.recovered
    pend = pending_map(a)
    assert "[0, 4]" in pend["A"] and "[0, 4]" in pend["B"]


def test_degenerate_zero_sum_recovers_zeros():
    # A+B=0 且非负 -> A=B=0，区间缩为一点
    samples = [("A", ""), ("B", "")]
    nos = set(dict(samples))
    a = analyze(samples, [validate_slip("H1", "A", "B", "0", nos)])
    got = recovered_map(a)
    assert got == {"A": Decimal("0"), "B": Decimal("0")}
    assert all("缩为一点" in r.basis for r in a.recovered)


def test_components_are_independent():
    # A=1.0 钉死 B；C、D 构成自由块；E 无单
    a = analyze(SAMPLES, [slip("H1", "A", "B", "1.5"), slip("H2", "C", "D", "2")])
    got = recovered_map(a)
    assert got == {"B": Decimal("2.0")}
    assert set(pending_map(a)) == {"C", "D", "E"}


def test_invalid_known_assay_treated_as_missing_with_warning():
    samples = [("A", "abc"), ("B", "")]
    nos = set(dict(samples))
    a = analyze(samples, [validate_slip("H1", "A", "B", "0", nos)])
    assert [i.sample_no for i in a.invalid] == ["A"]
    assert not a.known
    # A 按缺测处理，可被复原
    assert recovered_map(a) == {"A": Decimal("0"), "B": Decimal("0")}


# --------------------------------------------------------------------------- #
# 矛盾检测
# --------------------------------------------------------------------------- #
def test_known_pair_conflict_raises():
    # A=1.0 已知，C 改为已知 1.0：A+C=2 与单据 A+C=6 矛盾
    samples = [("A", "1.0"), ("C", "1.0"), ("B", "")]
    nos = set(dict(samples))
    with pytest.raises(ContradictionError, match="不等于该单要求的"):
        analyze(samples, [validate_slip("H1", "A", "C", "3", nos)])


def test_negative_propagation_raises():
    # A=5 已知，A+B=2 -> B=-3，违反非负约束
    samples = [("A", "5"), ("B", "")]
    nos = set(dict(samples))
    with pytest.raises(ContradictionError, match="非负约束"):
        analyze(samples, [validate_slip("H1", "A", "B", "1", nos)])


def test_even_cycle_contradiction_raises():
    # A+B=2, B+C=2, C+D=2, D+A=4：偶环交替和不等
    samples = [("A", ""), ("B", ""), ("C", ""), ("D", "")]
    nos = set(dict(samples))
    slips = [
        validate_slip("H1", "A", "B", "1", nos),
        validate_slip("H2", "B", "C", "1", nos),
        validate_slip("H3", "C", "D", "1", nos),
        validate_slip("H4", "D", "A", "2", nos),
    ]
    with pytest.raises(ContradictionError, match="闭环"):
        analyze(samples, slips)


def test_even_cycle_consistent_is_free_not_contradiction():
    # 与上例相同但交替和相等：A+B=2, B+C=2, C+D=2, D+A=2 -> 一致但不唯一
    samples = [("A", ""), ("B", ""), ("C", ""), ("D", "")]
    nos = set(dict(samples))
    slips = [
        validate_slip("H1", "A", "B", "1", nos),
        validate_slip("H2", "B", "C", "1", nos),
        validate_slip("H3", "C", "D", "1", nos),
        validate_slip("H4", "D", "A", "1", nos),
    ]
    a = analyze(samples, slips)
    assert not a.recovered
    assert set(pending_map(a)) == {"A", "B", "C", "D"}


def test_odd_cycle_negative_raises():
    # A+B=2, B+C=2, A+C=10 -> A=C=5, B=-3，违反非负约束
    samples = [("A", ""), ("B", ""), ("C", "")]
    nos = set(dict(samples))
    slips = [
        validate_slip("H1", "A", "B", "1", nos),
        validate_slip("H2", "B", "C", "1", nos),
        validate_slip("H3", "A", "C", "5", nos),
    ]
    with pytest.raises(ContradictionError, match="非负约束"):
        analyze(samples, slips)


def test_free_component_empty_interval_raises():
    # 无锚点链：A+B=1（B=1-A），B+C=2（C=1+A），C+D=0.5（D=-0.5-A 恒为负），
    # 非负约束夹出的自由量区间为空
    samples = [("A", ""), ("B", ""), ("C", ""), ("D", "")]
    nos = set(dict(samples))
    slips = [
        validate_slip("H1", "A", "B", "0.5", nos),   # A+B=1
        validate_slip("H2", "B", "C", "1", nos),     # B+C=2 -> C=1+A
        validate_slip("H3", "C", "D", "0.25", nos),  # C+D=0.5 -> D=-0.5-A
    ]
    with pytest.raises(ContradictionError, match="非负约束下无解"):
        analyze(samples, slips)
