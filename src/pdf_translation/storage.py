from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schemas import QualityReport, TranslationResult


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def _init(self) -> None:
        with self.connect() as con:
            con.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, config_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS tasks(run_id TEXT NOT NULL, unit_id TEXT NOT NULL, cache_key TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT, quality_json TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(run_id, unit_id));
            CREATE UNIQUE INDEX IF NOT EXISTS task_identity ON tasks(run_id, cache_key);
            CREATE TABLE IF NOT EXISTS cache(cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, unit_id TEXT NOT NULL, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER, source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, unit_id TEXT, event TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)

    def begin_run(self, run_id: str, input_hash: str, config_hash: str) -> None:
        with self.connect() as con:
            row = con.execute("SELECT input_hash, config_hash FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row and (row["input_hash"] != input_hash or row["config_hash"] != config_hash):
                raise ValueError("resume refused: input or config hash changed")
            con.execute("INSERT OR IGNORE INTO runs(run_id,input_hash,config_hash) VALUES(?,?,?)", (run_id,input_hash,config_hash))

    def prepare(self, run_id: str, unit_id: str, cache_key: str) -> None:
        with self.connect() as con:
            row = con.execute("SELECT cache_key FROM tasks WHERE run_id=? AND unit_id=?", (run_id, unit_id)).fetchone()
            if row is None:
                con.execute("INSERT INTO tasks(run_id,unit_id,cache_key,state) VALUES(?,?,?,'prepared')", (run_id,unit_id,cache_key))
            elif row["cache_key"] != cache_key:
                con.execute("UPDATE tasks SET cache_key=?,state='prepared',result_json=NULL,quality_json=NULL,updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND unit_id=?", (cache_key,run_id,unit_id))

    def state(self, run_id: str, unit_id: str) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT state FROM tasks WHERE run_id=? AND unit_id=?", (run_id,unit_id)).fetchone()
            return row["state"] if row else None

    def cached(self, cache_key: str) -> TranslationResult | None:
        with self.connect() as con:
            row = con.execute("SELECT result_json FROM cache WHERE cache_key=?", (cache_key,)).fetchone()
            return TranslationResult.model_validate_json(row["result_json"]) if row else None

    def result(self, run_id: str, unit_id: str) -> TranslationResult | None:
        with self.connect() as con:
            row = con.execute("SELECT result_json FROM tasks WHERE run_id=? AND unit_id=?", (run_id, unit_id)).fetchone()
            return TranslationResult.model_validate_json(row["result_json"]) if row and row["result_json"] else None

    def task_cache_key(self, run_id: str, unit_id: str) -> str | None:
        with self.connect() as con:
            row = con.execute("SELECT cache_key FROM tasks WHERE run_id=? AND unit_id=?", (run_id, unit_id)).fetchone()
            return row["cache_key"] if row else None

    def commit(self, run_id: str, result: TranslationResult, report: QualityReport, cache_key: str, cached: bool = False) -> None:
        state = "accepted" if report.status == "pass" else report.status
        result_json = result.model_dump_json()
        ledger_cost = 0.0 if cached else result.usage.cost_usd
        ledger_input = 0 if cached else result.usage.input_tokens
        ledger_output = 0 if cached else result.usage.output_tokens
        with self.connect() as con:
            con.execute("UPDATE tasks SET state=?,result_json=?,quality_json=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND unit_id=?", (state,result_json,report.model_dump_json(),run_id,result.unit_id))
            if not cached and result.finish == "complete":
                con.execute("INSERT OR REPLACE INTO cache(cache_key,result_json) VALUES(?,?)", (cache_key,result_json))
            con.execute("INSERT INTO ledger(run_id,unit_id,cost_usd,input_tokens,output_tokens,source) VALUES(?,?,?,?,?,?)", (run_id,result.unit_id,ledger_cost,ledger_input,ledger_output,"local_cache" if cached else result.usage.usage_source))

    def spent(self, run_id: str) -> float:
        with self.connect() as con:
            return float(con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ledger WHERE run_id=?", (run_id,)).fetchone()[0])

    def summary(self, run_id: str) -> dict:
        with self.connect() as con:
            states = {row["state"]: row["n"] for row in con.execute("SELECT state,COUNT(*) n FROM tasks WHERE run_id=? GROUP BY state", (run_id,))}
            usage = con.execute("SELECT COALESCE(SUM(cost_usd),0) cost,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,SUM(CASE WHEN source='local_cache' THEN 1 ELSE 0 END) cache_hits FROM ledger WHERE run_id=?", (run_id,)).fetchone()
            return {"run_id": run_id, "states": states, "cost_usd": usage["cost"], "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"], "cache_hits": usage["cache_hits"] or 0}

    def quality_reports(self, run_id: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT unit_id,state,quality_json FROM tasks WHERE run_id=? ORDER BY unit_id", (run_id,)).fetchall()
            return [{"unit_id": row["unit_id"], "state": row["state"], "quality": json.loads(row["quality_json"]) if row["quality_json"] else None} for row in rows]
