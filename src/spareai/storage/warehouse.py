"""Analytical store. SQLite by default (zero install); every result row is
stamped with a run_id so history is kept and runs are auditable.

To move to PostgreSQL / SQL Server later, swap the connection for a
SQLAlchemy engine - the table layout stays the same.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pandas as pd

RESULT_TABLES = (
    "recommendations", "policy", "classification", "forecast",
    "monthly_demand", "dq_issues", "model_metrics", "backtest_summary",
    "backtest_detail", "stockout_validation", "reliability",
)


class Warehouse:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
                    status TEXT, as_of_date TEXT, source TEXT,
                    row_counts TEXT, summary TEXT, error TEXT)""")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---------------------------------------------------------------- runs
    def start_run(self, as_of: str, source: str) -> str:
        run_id = datetime.now().strftime("RUN-%Y%m%d-%H%M%S-%f")[:-3]
        with self.connect() as con:
            con.execute("INSERT INTO runs(run_id, started_at, status, as_of_date, source)"
                        " VALUES (?,?,?,?,?)",
                        (run_id, datetime.now().isoformat(timespec="seconds"),
                         "RUNNING", as_of, source))
        return run_id

    def finish_run(self, run_id: str, status: str, row_counts: dict | None = None,
                   summary: dict | None = None, error: str | None = None) -> None:
        with self.connect() as con:
            con.execute("UPDATE runs SET finished_at=?, status=?, row_counts=?, summary=?, error=?"
                        " WHERE run_id=?",
                        (datetime.now().isoformat(timespec="seconds"), status,
                         json.dumps(row_counts or {}), json.dumps(summary or {}, default=str),
                         error, run_id))

    def latest_run(self) -> dict | None:
        with self.connect() as con:
            con.row_factory = sqlite3.Row
            row = con.execute("SELECT * FROM runs WHERE status='SUCCESS' "
                              "ORDER BY started_at DESC, run_id DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        d["summary"] = json.loads(d["summary"] or "{}")
        d["row_counts"] = json.loads(d["row_counts"] or "{}")
        return d

    def runs(self, limit: int = 50) -> pd.DataFrame:
        with self.connect() as con:
            return pd.read_sql("SELECT run_id, started_at, finished_at, status, as_of_date,"
                               " source, error FROM runs ORDER BY started_at DESC LIMIT ?",
                               con, params=(limit,))

    # -------------------------------------------------------------- tables
    def write(self, table: str, df: pd.DataFrame, run_id: str | None = None,
              replace: bool = False) -> None:
        out = df.copy()
        for c in out.columns:
            if pd.api.types.is_datetime64_any_dtype(out[c]):
                out[c] = out[c].dt.strftime("%Y-%m-%d")
        if run_id is not None:
            out.insert(0, "run_id", run_id)
        with self.connect() as con:
            if not replace and run_id is not None:
                exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                                     (table,)).fetchone()
                if exists:
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    if set(out.columns) != cols:      # schema evolved -> rebuild
                        con.execute(f"DROP TABLE {table}")
            out.to_sql(table, con, if_exists="replace" if replace else "append", index=False)
            if run_id is not None:
                con.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_run ON {table}(run_id)")

    def read(self, table: str, run_id: str | None = None, where: str = "",
             params: tuple = ()) -> pd.DataFrame:
        sql = f"SELECT * FROM {table}"
        conds, args = [], []
        if run_id:
            conds.append("run_id = ?")
            args.append(run_id)
        if where:
            conds.append(where)
            args.extend(params)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        with self.connect() as con:
            try:
                return pd.read_sql(sql, con, params=tuple(args))
            except Exception:
                return pd.DataFrame()

    def purge_runs(self, keep: int = 20) -> None:
        """Retention: keep the latest N runs' results."""
        with self.connect() as con:
            old = [r[0] for r in con.execute(
                "SELECT run_id FROM runs ORDER BY started_at DESC LIMIT -1 OFFSET ?", (keep,))]
            for t in RESULT_TABLES:
                exists = con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()
                if exists and old:
                    con.executemany(f"DELETE FROM {t} WHERE run_id=?", [(r,) for r in old])
            con.executemany("DELETE FROM runs WHERE run_id=?", [(r,) for r in old])
