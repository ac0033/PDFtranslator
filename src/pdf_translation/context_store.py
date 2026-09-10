from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .context_models import GlossaryEntry, SemanticEntry, StyleRule, TranslationMemoryEntry


class ContextStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS glossary(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS style_rules(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS semantic_entries(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tm_entries(id TEXT PRIMARY KEY, source_hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS tm_source_hash ON tm_entries(source_hash);
            CREATE TABLE IF NOT EXISTS dependency_edges(dependency_id TEXT NOT NULL, consumer_id TEXT NOT NULL, PRIMARY KEY(dependency_id,consumer_id));
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _upsert(self, table: str, key: str, payload: str, source_hash: str | None = None) -> None:
        with self._connect() as con:
            if source_hash is None:
                con.execute(f"INSERT OR REPLACE INTO {table}(id,payload) VALUES(?,?)", (key, payload))
            else:
                con.execute(f"INSERT OR REPLACE INTO {table}(id,source_hash,payload) VALUES(?,?,?)", (key, source_hash, payload))

    def put_glossary(self, entry: GlossaryEntry) -> None:
        self._upsert("glossary", entry.term_id, entry.model_dump_json())

    def put_style(self, rule: StyleRule) -> None:
        self._upsert("style_rules", rule.rule_id, rule.model_dump_json())

    def put_semantic(self, entry: SemanticEntry) -> None:
        self._upsert("semantic_entries", entry.semantic_id, entry.model_dump_json())

    def put_tm(self, entry: TranslationMemoryEntry) -> None:
        self._upsert("tm_entries", entry.tm_id, entry.model_dump_json(), entry.source_hash)

    def _all(self, table: str, model):
        with self._connect() as con:
            return [model.model_validate_json(row[0]) for row in con.execute(f"SELECT payload FROM {table} ORDER BY id")]

    def glossary(self) -> list[GlossaryEntry]:
        return self._all("glossary", GlossaryEntry)

    def styles(self) -> list[StyleRule]:
        return self._all("style_rules", StyleRule)

    def semantics(self) -> list[SemanticEntry]:
        return self._all("semantic_entries", SemanticEntry)

    def exact_tm(self, source_hash: str) -> list[TranslationMemoryEntry]:
        with self._connect() as con:
            return [TranslationMemoryEntry.model_validate_json(row[0]) for row in con.execute("SELECT payload FROM tm_entries WHERE source_hash=? ORDER BY id", (source_hash,))]

    def replace_dependencies(self, consumer_id: str, dependency_ids: Iterable[str]) -> None:
        with self._connect() as con:
            con.execute("DELETE FROM dependency_edges WHERE consumer_id=?", (consumer_id,))
            con.executemany("INSERT INTO dependency_edges(dependency_id,consumer_id) VALUES(?,?)", ((item, consumer_id) for item in dependency_ids))

    def dependency_edges(self) -> dict[str, set[str]]:
        edges: dict[str, set[str]] = {}
        with self._connect() as con:
            for dependency, consumer in con.execute("SELECT dependency_id,consumer_id FROM dependency_edges"):
                edges.setdefault(dependency, set()).add(consumer)
        return edges
