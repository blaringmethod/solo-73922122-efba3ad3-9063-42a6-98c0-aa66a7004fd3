"""SQLite 持久化与入仓 CSV 生成。

更正实测起点后整份回次结果重新计算，并在单个事务内整份替换该回次的
供数表记录；若新约束无解，事务不提交，旧供数原样保留。
"""

from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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
        import json

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
        import json

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
        import json

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
    def _insert_run(self, conn, run_input: RunInput, result: RunResult,
                    payload: dict, version: int, now: str):
        import json

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
