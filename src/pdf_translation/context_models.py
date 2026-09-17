from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .hashing import stable_hash
from .schemas import StrictModel


ScopeKind = Literal["unit", "section", "chapter", "document", "domain"]


class Scope(StrictModel):
    kind: ScopeKind
    value: str = Field(min_length=1)


class GlossaryEntry(StrictModel):
    term_id: str
    source_term: str = Field(min_length=1)
    target_term: str = Field(min_length=1)
    sense: str
    scope: Scope
    source_ids: tuple[str, ...]
    version: str
    status: Literal["candidate", "approved", "deprecated"] = "candidate"
    exceptions: tuple[str, ...] = ()
    forbidden_target_terms: tuple[str, ...] = ()
    accepted_target_variants: tuple[str, ...] = ()

    @model_validator(mode="after")
    def approved_entry_has_evidence(self) -> "GlossaryEntry":
        if self.status == "approved" and not self.source_ids:
            raise ValueError("approved glossary entry requires source evidence")
        return self


class StyleRule(StrictModel):
    rule_id: str
    content: str = Field(min_length=1)
    scope: Scope
    priority: int = 100
    version: str
    status: Literal["candidate", "approved", "deprecated"] = "approved"
    expected_source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def source_binding_has_unit_scope(self) -> "StyleRule":
        if self.expected_source_hash is not None and self.scope.kind != "unit":
            raise ValueError("source-bound feedback requires unit scope")
        return self


class SemanticEntry(StrictModel):
    semantic_id: str
    concept: str = Field(min_length=1)
    scope: Scope
    evidence_text: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    source_ids: tuple[str, ...] = Field(min_length=1)
    dependency_ids: tuple[str, ...] = ()
    revision: str
    status: Literal["candidate", "approved", "revoked"] = "candidate"


class TranslationMemoryEntry(StrictModel):
    tm_id: str
    source_text: str
    target_text: str
    source_lang: str
    target_lang: str
    context_signature: str
    glossary_snapshot_id: str
    style_snapshot_id: str
    qa_status: Literal["unreviewed", "passed", "failed"]
    human_status: Literal["candidate", "confirmed", "rejected"] = "candidate"

    @property
    def source_hash(self) -> str:
        return stable_hash(self.source_text)


class TermCandidate(StrictModel):
    source_term: str
    occurrence_count: int = Field(gt=0)
    source_ids: tuple[str, ...]


class SelectionTrace(StrictModel):
    item_id: str
    selected: bool
    reason: str
    estimated_tokens: int = Field(ge=0)


class ContextBuildResult(StrictModel):
    packet_hash: str
    traces: tuple[SelectionTrace, ...]


class ContextBundle(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    glossary: tuple[GlossaryEntry, ...] = ()
    styles: tuple[StyleRule, ...] = ()
    semantics: tuple[SemanticEntry, ...] = ()
    translation_memory: tuple[TranslationMemoryEntry, ...] = ()


class InvalidationReport(StrictModel):
    changed_ids: tuple[str, ...]
    affected_unit_ids: tuple[str, ...]
    report_hash: str


def make_invalidation_report(changed_ids: set[str], dependency_edges: dict[str, set[str]]) -> InvalidationReport:
    queue = list(changed_ids)
    visited = set(changed_ids)
    affected: set[str] = set()
    while queue:
        changed = queue.pop()
        for consumer in dependency_edges.get(changed, set()):
            if consumer.startswith("unit:"):
                affected.add(consumer.removeprefix("unit:"))
            elif consumer not in visited:
                visited.add(consumer)
                queue.append(consumer)
    payload = {"changed_ids": sorted(changed_ids), "affected_unit_ids": sorted(affected)}
    return InvalidationReport(**payload, report_hash=stable_hash(payload))
