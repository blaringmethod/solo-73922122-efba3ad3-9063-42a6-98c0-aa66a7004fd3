"""SQLite 持久化与入仓 CSV 生成。

更正实测起点后整份回次结果重新计算，并在单个事务内整份替换该回次的
供数表记录；若新约束无解，事务不提交，旧供数原样保留。

混样化验单（assay_slips）的录入/更正/撤销同样在单个 BEGIN IMMEDIATE
事务内完成：先把变更应用到“当前全部有效化验单”的完整集合上重新联立求解，
存在矛盾（无非负解）则整体回滚，当前化验单与复原结果原样保留。BEGIN
IMMEDIATE 在事务开始即取得写锁，并发请求被串行化，后提交的事务读到的是
已生效的最新集合，因此相互矛盾的化验单不可能同时生效。复原结果不落库，
每次读取时由同一事务快照内的化验单与供数载荷现算，保证页面上的化验单
与结果始终对应同一份有效数据。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .assay import (
    AssayAnalysis,
    Slip,
    SlipValidationError,
    analyze,
    fmt_decimal,
    parse_nonneg_decimal,
    validate_slip,
)
from .engine import RunInput, RunResult, parse_payload, solve

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    version      INTEGER NOT NULL,
    start_depth  INTEGER NOT NULL,
    end_depth    INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measured_starts (
    run_id     TEXT NOT NULL,
    seg_index  INTEGER NOT NULL,
    depth      INTEGER NOT NULL,
    PRIMARY KEY (run_id, seg_index),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS supply_rows (
    run_id      TEXT NOT NULL,
    sample_no   TEXT NOT NULL,
    assay       TEXT NOT NULL,
    layer       TEXT NOT NULL,
    seg_index   INTEGER NOT NULL,
    start_lo    INTEGER NOT NULL,
    start_hi    INTEGER NOT NULL,
    end_lo      INTEGER NOT NULL,
    end_hi      INTEGER NOT NULL,
    PRIMARY KEY (run_id, sample_no),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS correction_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    from_version INTEGER NOT NULL,
    to_version   INTEGER NOT NULL,
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assay_slips (
    run_id     TEXT NOT NULL,
    slip_no    TEXT NOT NULL,
    sample_a   TEXT NOT NULL,
    sample_b   TEXT NOT NULL,
    mean_text  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, slip_no),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS assay_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    action     TEXT NOT NULL,
    slip_no    TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

CSV_HEADER = [
    "回次编号",
    "样品编号",
    "化验值",
    "层位",
    "段序号",
    "起点孔深最小_mm",
    "起点孔深最大_mm",
    "终点孔深最小_mm",
    "终点孔深最大_mm",
]


@dataclass
class RunRecord:
    run_id: str
    version: int
    start_depth: int
    end_depth: int
    payload: dict
    updated_at: str
    result: RunResult
    measured_starts: list[tuple[int, int]]


@dataclass
class SlipRow:
    """库内现行有效的一张化验单（含回显用的派生文本）。"""

    slip_no: str
    sample_a: str
    sample_b: str
    mean_text: str
    total_text: str  # 2 × 混样均值，即两原样之和
    created_at: str
    updated_at: str


@dataclass
class AssayView:
    """详情页化验单区块的一次性快照：化验单与复原结果来自同一事务读取。"""

    run_id: str
    slips: list[SlipRow]
    analysis: AssayAnalysis
    sample_assays: list[tuple[str, str]]  # 按段内顺序的 (样品编号, 化验值原文)
    log: list[sqlite3.Row]


def _sample_assays(payload: dict) -> list[tuple[str, str]]:
    """从供数载荷中按段内顺序取出 (样品编号, 化验值原文)，空白表示缺测。"""
    out: list[tuple[str, str]] = []
    for seg in payload.get("segments", []):
        for s in seg.get("samples", []):
            assay = s.get("assay")
            out.append((str(s.get("sample_no", "")), "" if assay is None else str(assay)))
    return out


def _row_to_slip(row: SlipRow) -> Slip:
    return Slip(
        row.slip_no,
        row.sample_a,
        row.sample_b,
        parse_nonneg_decimal(row.mean_text, "混样均值"),
        row.mean_text,
    )


def _total_text(mean_text: str) -> str:
    return fmt_decimal(parse_nonneg_decimal(mean_text, "混样均值") * 2)


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------------ #
    def list_runs(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT run_id, version, start_depth, end_depth, updated_at "
                    "FROM runs ORDER BY run_id"
                )
            )

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload"])
            measured = list(
                conn.execute(
                    "SELECT seg_index, depth FROM measured_starts "
                    "WHERE run_id = ? ORDER BY seg_index",
                    (run_id,),
                )
            )
        run_input = parse_payload(payload)
        result = solve(run_input)
        return RunRecord(
            run_id=row["run_id"],
            version=row["version"],
            start_depth=row["start_depth"],
            end_depth=row["end_depth"],
            payload=payload,
            updated_at=row["updated_at"],
            result=result,
            measured_starts=[(m["seg_index"], m["depth"]) for m in measured],
        )

    # ------------------------------------------------------------------ #
    def save_new_run(self, payload: dict) -> RunRecord:
        """导入新回次：先全量校验与求解，再在事务内写入。"""
        run_input = parse_payload(payload)
        result = solve(run_input)  # 无解会抛 InfeasibleError，库内不产生任何数据
        now = _now()
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", (run_input.run_id,)
            ).fetchone()
            if exists:
                raise ValueError(f"回次 {run_input.run_id} 已存在，请在详情页更正")
            self._insert_run(conn, run_input, result, payload, version=1, now=now)
            conn.commit()
        record = self.get_run(run_input.run_id)
        assert record is not None
        return record

    def correct_run(
        self, run_id: str, expected_version: int, measured_starts: list[dict], note: str
    ) -> RunRecord:
        """更正实测段起点并整份重算供数。

        expected_version 与库内版本不一致时抛 StaleVersionError；
        新约束无解时抛 InfeasibleError，事务回滚、旧供数保留。
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["version"] != expected_version:
                raise StaleVersionError(
                    f"页面版本已过期：页面基于版本 {expected_version}，"
                    f"库内当前版本为 {row['version']}，请刷新详情页后重新更正。"
                    "当前供数未做任何改动。"
                )

            payload = json.loads(row["payload"])
            # 复用 parse_payload 的全部校验逻辑
            payload["measured_starts"] = measured_starts
            run_input = parse_payload(payload)
            result = solve(run_input)  # 无解 -> 抛异常 -> 事务回滚

            now = _now()
            new_version = expected_version + 1
            conn.execute("DELETE FROM supply_rows WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM measured_starts WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            self._insert_run(conn, run_input, result, payload, new_version, now)
            conn.execute(
                "INSERT INTO correction_log (run_id, from_version, to_version, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, expected_version, new_version, note or "更正实测起点", now),
            )
            conn.commit()
        record = self.get_run(run_id)
        assert record is not None
        return record

    def correction_history(self, run_id: str) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM correction_log WHERE run_id = ? ORDER BY id",
                    (run_id,),
                )
            )

    # ------------------------------------------------------------------ #
    # 混样化验单：录入 / 更正 / 撤销 / 快照读取
    # ------------------------------------------------------------------ #
    def assay_view(self, run_id: str) -> AssayView | None:
        """在一个事务快照内读取化验单与供数载荷，并据同一快照现算复原结果。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT payload FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            payload = json.loads(row["payload"])
            sample_assays = _sample_assays(payload)
            slips = self._fetch_slip_rows(conn, run_id)
            log = list(
                conn.execute(
                    "SELECT * FROM assay_log WHERE run_id = ? ORDER BY id DESC LIMIT 20",
                    (run_id,),
                )
            )
            conn.commit()
        finally:
            conn.close()
        analysis = analyze(sample_assays, [_row_to_slip(r) for r in slips])
        return AssayView(run_id, slips, analysis, sample_assays, log)

    def create_slip(self, run_id: str, slip_no: str, sample_a: str,
                    sample_b: str, mean_text: str) -> None:
        def apply(conn, sample_assays, slips):
            slip = validate_slip(
                slip_no, sample_a, sample_b, mean_text, {s for s, _ in sample_assays}
            )
            if any(s.slip_no == slip.slip_no for s in slips):
                raise SlipValidationError(
                    f"单号 {slip.slip_no} 已存在，如需修改请对该单做“更正”。"
                )
            self._check_consistent(sample_assays, slips + [slip])
            now = _now()
            conn.execute(
                "INSERT INTO assay_slips "
                "(run_id, slip_no, sample_a, sample_b, mean_text, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, slip.slip_no, slip.sample_a, slip.sample_b,
                 slip.mean_text, now, now),
            )
            self._log_slip(conn, run_id, "录入", slip.slip_no,
                           f"{slip.sample_a} + {slip.sample_b}，混样均值 {slip.mean_text}", now)

        self._slip_transaction(run_id, apply)

    def correct_slip(self, run_id: str, slip_no: str, sample_a: str,
                     sample_b: str, mean_text: str) -> None:
        no = (slip_no or "").strip()

        def apply(conn, sample_assays, slips):
            if not any(s.slip_no == no for s in slips):
                raise SlipValidationError(
                    f"单号 {no} 不存在，无法更正（如需新单请使用“录入”）。"
                )
            slip = validate_slip(
                no, sample_a, sample_b, mean_text, {s for s, _ in sample_assays}
            )
            new_slips = [slip if s.slip_no == no else s for s in slips]
            self._check_consistent(sample_assays, new_slips)
            now = _now()
            conn.execute(
                "UPDATE assay_slips SET sample_a = ?, sample_b = ?, mean_text = ?, "
                "updated_at = ? WHERE run_id = ? AND slip_no = ?",
                (slip.sample_a, slip.sample_b, slip.mean_text, now, run_id, no),
            )
            self._log_slip(conn, run_id, "更正", no,
                           f"改为 {slip.sample_a} + {slip.sample_b}，混样均值 {slip.mean_text}",
                           now)

        self._slip_transaction(run_id, apply)

    def withdraw_slip(self, run_id: str, slip_no: str) -> None:
        no = (slip_no or "").strip()

        def apply(conn, sample_assays, slips):
            if not any(s.slip_no == no for s in slips):
                raise SlipValidationError(f"单号 {no} 不存在，无法撤销。")
            # 撤单只是去掉一条约束，不会产生矛盾，无需重新联立校验
            now = _now()
            conn.execute(
                "DELETE FROM assay_slips WHERE run_id = ? AND slip_no = ?",
                (run_id, no),
            )
            self._log_slip(conn, run_id, "撤销", no, "化验单已撤销，复原结果按现行化验单重算", now)

        self._slip_transaction(run_id, apply)

    # ------------------------------------------------------------------ #
    def _slip_transaction(self, run_id: str, apply) -> None:
        """在 BEGIN IMMEDIATE 事务内应用一次化验单变更。

        事务开始即取得写锁，并发的变更请求被串行化；每个事务都基于
        已生效的最新化验单集合做全量一致性校验，相互矛盾的化验单
        不可能同时生效。校验失败或字段不合法时整体回滚。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            payload = json.loads(row["payload"])
            sample_assays = _sample_assays(payload)
            slips = [_row_to_slip(r) for r in self._fetch_slip_rows(conn, run_id)]
            apply(conn, sample_assays, slips)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _check_consistent(sample_assays, slips) -> None:
        """全量联立现行化验单与已有原样值；矛盾时抛 ContradictionError。"""
        analyze(sample_assays, slips)

    @staticmethod
    def _fetch_slip_rows(conn, run_id: str) -> list[SlipRow]:
        rows = conn.execute(
            "SELECT slip_no, sample_a, sample_b, mean_text, created_at, updated_at "
            "FROM assay_slips WHERE run_id = ? ORDER BY slip_no",
            (run_id,),
        ).fetchall()
        return [
            SlipRow(
                r["slip_no"], r["sample_a"], r["sample_b"], r["mean_text"],
                _total_text(r["mean_text"]), r["created_at"], r["updated_at"],
            )
            for r in rows
        ]

    @staticmethod
    def _log_slip(conn, run_id: str, action: str, slip_no: str,
                  detail: str, now: str) -> None:
        conn.execute(
            "INSERT INTO assay_log (run_id, action, slip_no, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, action, slip_no, detail, now),
        )

    # ------------------------------------------------------------------ #
    def _insert_run(self, conn, run_input: RunInput, result: RunResult,
                    payload: dict, version: int, now: str):
        conn.execute(
            "INSERT INTO runs (run_id, version, start_depth, end_depth, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                run_input.run_id,
                version,
                run_input.start_depth,
                run_input.end_depth,
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        conn.executemany(
            "INSERT INTO measured_starts (run_id, seg_index, depth) VALUES (?, ?, ?)",
            [(run_input.run_id, m.seg_index, m.depth) for m in run_input.measured_starts],
        )
        conn.executemany(
            "INSERT INTO supply_rows "
            "(run_id, sample_no, assay, layer, seg_index, start_lo, start_hi, end_lo, end_hi) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_input.run_id,
                    s.sample_no,
                    s.assay,
                    s.layer,
                    s.seg_index,
                    s.start_lo,
                    s.start_hi,
                    s.end_lo,
                    s.end_hi,
                )
                for s in result.samples
                if s.layer is not None
            ],
        )

    # ------------------------------------------------------------------ #
    def supply_csv_bytes(self, run_id: str) -> bytes:
        """读取库内生效供数表并生成入仓 CSV（utf-8-sig，便于 Excel 打开）。"""
        with self._connect() as conn:
            rows = list(
                conn.execute(
                    "SELECT * FROM supply_rows WHERE run_id = ? ORDER BY sample_no",
                    (run_id,),
                )
            )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(CSV_HEADER)
        for r in rows:
            writer.writerow(
                [
                    r["run_id"],
                    r["sample_no"],
                    r["assay"],
                    r["layer"],
                    r["seg_index"],
                    r["start_lo"],
                    r["start_hi"],
                    r["end_lo"],
                    r["end_hi"],
                ]
            )
        return "﻿".encode("utf-8") + buf.getvalue().encode("utf-8")

    def supply_csv(self, run_id: str) -> str:
        return self.supply_csv_bytes(run_id).decode("utf-8-sig")


class StaleVersionError(Exception):
    """页面携带的版本号与库内不一致，更正请求被拒绝。"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
