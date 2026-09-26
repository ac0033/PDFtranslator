from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .schemas import QualityReport, TranslationResult, Usage


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
            CREATE TABLE IF NOT EXISTS provider_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                unit_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                error_category TEXT,
                provider_response_id TEXT,
                cost_usd REAL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                max_output_tokens INTEGER,
                attempted_result_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, unit_id, request_id, attempt_no)
            );
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, unit_id TEXT, event TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(provider_attempts)")}
            if "max_output_tokens" not in columns:
                con.execute("ALTER TABLE provider_attempts ADD COLUMN max_output_tokens INTEGER")
            if "attempted_result_json" not in columns:
                con.execute("ALTER TABLE provider_attempts ADD COLUMN attempted_result_json TEXT")

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

    def mark_exhausted(self, run_id: str, unit_id: str) -> None:
        """Pin a unit whose provider attempts are exhausted so reruns skip it."""
        with self.connect() as con:
            con.execute(
                "UPDATE tasks SET state='exhausted', updated_at=CURRENT_TIMESTAMP "
                "WHERE run_id=? AND unit_id=? AND result_json IS NULL",
                (run_id, unit_id),
            )

    def restore_exhausted_with_result(self, run_id: str) -> list[str]:
        """Repair stale exhausted state when an existing result already passed quality checks."""
        restored: list[str] = []
        with self.connect() as con:
            rows = con.execute("SELECT unit_id,quality_json FROM tasks WHERE run_id=? AND state='exhausted' "
                               "AND result_json IS NOT NULL AND quality_json IS NOT NULL", (run_id,)).fetchall()
            for row in rows:
                status = json.loads(row['quality_json']).get('status')
                if status not in {'pass', 'needs_review'}:
                    continue
                con.execute("UPDATE tasks SET state=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND unit_id=?",
                            ('accepted' if status == 'pass' else 'needs_review', run_id, row['unit_id']))
                con.execute("INSERT INTO events(run_id,unit_id,event,detail) VALUES(?,?,?,?)",
                            (run_id, row['unit_id'], 'restore_existing_qualified_result', '{}'))
                restored.append(row['unit_id'])
        return restored

    def clear_exhausted(self, run_id: str) -> list[str]:
        """Let exhausted units be attempted again, and say which ones.

        A unit is pinned as exhausted so a rerun does not pay for the same
        rejection twice. That is right while the rules stay the same and wrong
        the moment one is corrected: the verdict was about a rule that no longer
        exists, but the unit stays untranslatable and its pages stay unrendered.
        Clearing the recorded attempts too is the point - the runner refuses a
        unit that has already used its attempt budget.
        """
        with self.connect() as con:
            units = [
                row["unit_id"] for row in
                con.execute("SELECT unit_id FROM tasks WHERE run_id=? AND state='exhausted'", (run_id,))
            ]
            if units:
                placeholders = ",".join("?" * len(units))
                con.execute(f"DELETE FROM tasks WHERE run_id=? AND unit_id IN ({placeholders})", (run_id, *units))
                con.execute(f"DELETE FROM provider_attempts WHERE run_id=? AND unit_id IN ({placeholders})", (run_id, *units))
            return units

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

    def record_provider_attempt(
        self,
        run_id: str,
        unit_id: str,
        request_id: str,
        attempt_no: int,
        outcome: str,
        usage: Usage,
        *,
        max_output_tokens: int,
        attempted_result_json: str | None = None,
        error_category: str | None = None,
        provider_response_id: str | None = None,
    ) -> None:
        """Persist every external attempt before downstream validation can fail."""
        source = f"provider_{outcome}:{usage.usage_source}"
        with self.connect() as con:
            con.execute(
                "INSERT INTO provider_attempts(run_id,unit_id,request_id,attempt_no,outcome,error_category,provider_response_id,cost_usd,input_tokens,output_tokens,max_output_tokens,attempted_result_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, unit_id, request_id, attempt_no, outcome, error_category, provider_response_id, usage.cost_usd, usage.input_tokens, usage.output_tokens, max_output_tokens, attempted_result_json),
            )
            con.execute(
                "INSERT INTO ledger(run_id,unit_id,cost_usd,input_tokens,output_tokens,source) VALUES(?,?,?,?,?,?)",
                (run_id, unit_id, usage.cost_usd, usage.input_tokens, usage.output_tokens, source),
            )
            detail = json.dumps(
                {"attempt_no": attempt_no, "outcome": outcome, "error_category": error_category, "provider_response_id": provider_response_id, "max_output_tokens": max_output_tokens},
                ensure_ascii=False,
            )
            con.execute(
                "INSERT INTO events(run_id,unit_id,event,detail) VALUES(?,?,?,?)",
                (run_id, unit_id, "provider_attempt", detail),
            )

    def provider_attempt_count(self, run_id: str, unit_id: str) -> int:
        """Count content retries, not account outages that never returned a draft."""
        with self.connect() as con:
            return int(con.execute(
                "SELECT COUNT(*) FROM provider_attempts WHERE run_id=? AND unit_id=? "
                "AND (error_category IS NULL OR (error_category NOT LIKE 'http_%' AND error_category != 'APIStatusError'))",
                (run_id, unit_id),
            ).fetchone()[0])

    def requeue_provider_failures(self, run_id: str) -> list[str]:
        """Unpin only units whose entire failure history is transport/account errors.

        Unlike clear_exhausted, keep the attempts and ledger for audit. Call
        only after connectivity/funds are verified: a retry while the provider
        is still unavailable would simply pause the book again.
        """
        with self.connect() as con:
            rows = con.execute(
                "SELECT t.unit_id FROM tasks t WHERE t.run_id=? AND t.state='exhausted' AND t.result_json IS NULL "
                "AND EXISTS (SELECT 1 FROM provider_attempts a WHERE a.run_id=t.run_id AND a.unit_id=t.unit_id) "
                "AND NOT EXISTS (SELECT 1 FROM provider_attempts a WHERE a.run_id=t.run_id AND a.unit_id=t.unit_id "
                "AND (a.outcome!='error' OR (COALESCE(a.error_category,'')!='APIStatusError' AND COALESCE(a.error_category,'') NOT LIKE 'http_%')))",
                (run_id,),
            ).fetchall()
            units = [row['unit_id'] for row in rows]
            for unit_id in units:
                con.execute("UPDATE tasks SET state='prepared',updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND unit_id=?", (run_id, unit_id))
                con.execute("INSERT INTO events(run_id,unit_id,event,detail) VALUES(?,?,?,?)",
                            (run_id, unit_id, 'provider_outage_requeued', '{\"reason\":\"external provider access restored\"}'))
            return units

    def provider_attempts(self, run_id: str) -> list[dict]:
        with self.connect() as con:
            return [dict(row) for row in con.execute("SELECT * FROM provider_attempts WHERE run_id=? ORDER BY id", (run_id,))]

    def last_provider_attempt(self, run_id: str, unit_id: str) -> dict | None:
        with self.connect() as con:
            row = con.execute("SELECT * FROM provider_attempts WHERE run_id=? AND unit_id=? ORDER BY id DESC LIMIT 1", (run_id, unit_id)).fetchone()
            return dict(row) if row else None

    def commit(self, run_id: str, result: TranslationResult, report: QualityReport, cache_key: str, cached: bool = False, source: str | None = None, record_usage: bool = True) -> None:
        state = "accepted" if report.status == "pass" else report.status
        result_json = result.model_dump_json()
        ledger_cost = 0.0 if cached else result.usage.cost_usd
        ledger_input = 0 if cached else result.usage.input_tokens
        ledger_output = 0 if cached else result.usage.output_tokens
        with self.connect() as con:
            con.execute("UPDATE tasks SET state=?,result_json=?,quality_json=?,updated_at=CURRENT_TIMESTAMP WHERE run_id=? AND unit_id=?", (state,result_json,report.model_dump_json(),run_id,result.unit_id))
            if not cached and result.finish == "complete" and report.status == "pass":
                con.execute("INSERT OR REPLACE INTO cache(cache_key,result_json) VALUES(?,?)", (cache_key,result_json))
            if record_usage:
                ledger_source = source or ("local_cache" if cached else result.usage.usage_source)
                con.execute("INSERT INTO ledger(run_id,unit_id,cost_usd,input_tokens,output_tokens,source) VALUES(?,?,?,?,?,?)", (run_id,result.unit_id,ledger_cost,ledger_input,ledger_output,ledger_source))

    def alias_cache_key(self, run_id: str, unit_id: str, base_cache_key: str, result: TranslationResult, report_status: str) -> None:
        """Point a unit back at its first-attempt request key after a retry-modified request succeeded."""
        with self.connect() as con:
            if report_status == "pass":
                con.execute("INSERT OR REPLACE INTO cache(cache_key,result_json) VALUES(?,?)", (base_cache_key, result.model_dump_json()))
            con.execute("UPDATE tasks SET cache_key=? WHERE run_id=? AND unit_id=?", (base_cache_key, run_id, unit_id))

    def record_usage(self, run_id: str, subject: str, usage: Usage, source: str) -> None:
        """Book a model call that is not a unit's translation (review, layout repair).

        It counts against the same budget as translation; ``subject`` names what
        the call was about, e.g. ``page-0008``.
        """
        with self.connect() as con:
            con.execute("INSERT INTO ledger(run_id,unit_id,cost_usd,input_tokens,output_tokens,source) VALUES(?,?,?,?,?,?)",
                        (run_id, subject, usage.cost_usd or 0.0, usage.input_tokens, usage.output_tokens, source))

    def spent(self, run_id: str) -> float:
        with self.connect() as con:
            return float(con.execute("SELECT COALESCE(SUM(cost_usd),0) FROM ledger WHERE run_id=?", (run_id,)).fetchone()[0])

    def summary(self, run_id: str) -> dict:
        with self.connect() as con:
            states = {row["state"]: row["n"] for row in con.execute("SELECT state,COUNT(*) n FROM tasks WHERE run_id=? GROUP BY state", (run_id,))}
            usage = con.execute("SELECT COALESCE(SUM(cost_usd),0) cost,COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,SUM(CASE WHEN source='local_cache' THEN 1 ELSE 0 END) cache_hits FROM ledger WHERE run_id=?", (run_id,)).fetchone()
            attempts = con.execute("SELECT COUNT(*) total, SUM(CASE WHEN outcome!='success' THEN 1 ELSE 0 END) failed FROM provider_attempts WHERE run_id=?", (run_id,)).fetchone()
            return {"run_id": run_id, "states": states, "cost_usd": usage["cost"], "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"], "cache_hits": usage["cache_hits"] or 0, "provider_attempts": attempts["total"] or 0, "failed_provider_attempts": attempts["failed"] or 0}

    def results(self, run_id: str) -> dict[str, TranslationResult]:
        """Read every committed result at once; per-unit reads do not scale."""
        with self.connect() as con:
            rows = con.execute("SELECT unit_id,result_json FROM tasks WHERE run_id=? AND result_json IS NOT NULL", (run_id,)).fetchall()
            return {row["unit_id"]: TranslationResult.model_validate_json(row["result_json"]) for row in rows}

    def quality_reports(self, run_id: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT unit_id,state,quality_json FROM tasks WHERE run_id=? ORDER BY unit_id", (run_id,)).fetchall()
            return [{"unit_id": row["unit_id"], "state": row["state"], "quality": json.loads(row["quality_json"]) if row["quality_json"] else None} for row in rows]
