from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"
MARKER_PATTERN = r"^⟦P:[A-Za-z0-9_.:-]+⟧$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(StrictModel):
    uri: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)
    media_type: str = Field(min_length=1)


class SourceRef(StrictModel):
    block_id: str = Field(min_length=1)
    page_index: int = Field(ge=0)
    bbox: tuple[float, float, float, float]
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)

    @model_validator(mode="after")
    def valid_range(self) -> "SourceRef":
        x0, y0, x1, y1 = self.bbox
        if x1 < x0 or y1 < y0:
            raise ValueError("bbox must satisfy x1 >= x0 and y1 >= y0")
        if self.char_end < self.char_start:
            raise ValueError("char_end must be >= char_start")
        return self


class ProtectedSpan(StrictModel):
    occurrence_id: str
    marker: str = Field(pattern=MARKER_PATTERN)
    kind: Literal["formula", "code", "symbol", "url", "citation", "other"]
    original: ArtifactRef
    source_ref: SourceRef


class Block(StrictModel):
    block_id: str
    kind: Literal["heading", "paragraph", "caption", "footnote", "table_cell", "formula", "code", "figure", "other"]
    source_text: str
    section_path: tuple[str, ...] = ()
    reading_order: int = Field(ge=0)
    source_refs: tuple[SourceRef, ...]
    native_il_ref: str


class DocumentIR(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    document_id: str
    source: ArtifactRef
    page_count: int = Field(gt=0)
    parser_revision: str
    parser_config_hash: str = Field(pattern=SHA256_PATTERN)
    native_il: ArtifactRef
    blocks: tuple[Block, ...]


class TranslationUnit(StrictModel):
    unit_id: str
    document_id: str
    ir_revision: str
    unit_type: str
    source_lang: str
    target_lang: str
    source_text: str
    source_hash: str = Field(pattern=SHA256_PATTERN)
    section_path: tuple[str, ...] = ()
    source_refs: tuple[SourceRef, ...] = ()
    protected: tuple[ProtectedSpan, ...] = ()
    protected_literal_hints: dict[str, str] = Field(default_factory=dict)
    neighbor_ids: tuple[str, ...] = ()
    dependency_ids: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def literal_hints_reference_protected_markers(self) -> "TranslationUnit":
        if not set(self.protected_literal_hints) <= {span.marker for span in self.protected}:
            raise ValueError("protected literal hints must reference declared protected markers")
        return self


class ContextItem(StrictModel):
    item_id: str
    category: Literal["local", "definition", "summary", "glossary", "tm", "style"]
    content: str
    source_ids: tuple[str, ...] = ()
    revision: str
    required: bool
    selection_reason: str
    term_source: str | None = None
    term_target: str | None = None


class ContextPacket(StrictModel):
    unit_id: str
    router_revision: str
    memory_snapshot_id: str
    items: tuple[ContextItem, ...] = ()
    token_count: int = Field(ge=0)
    token_counter_id: str
    token_count_is_estimate: bool
    token_budget: int = Field(gt=0)
    missing_dependencies: tuple[str, ...] = ()
    packet_hash: str = Field(pattern=SHA256_PATTERN)


class ModelSpec(StrictModel):
    provider: str
    model_id: str
    model_revision: str | None = None
    reasoning_effort: str | None = None
    max_output_tokens: int = Field(gt=0)
    prompt_revision: str
    adapter_revision: str
    pricing_revision: str


class TranslationRequest(StrictModel):
    request_id: str
    unit: TranslationUnit
    context: ContextPacket
    model: ModelSpec
    cache_key: str = Field(pattern=SHA256_PATTERN)


class TranslationDraft(StrictModel):
    unit_id: str
    translated_text: str
    uncertainties: tuple[str, ...] = ()


class Usage(StrictModel):
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    usage_source: Literal["provider", "estimated", "unavailable"]


class TermReplacement(StrictModel):
    marker: str
    source_term: str
    target_term: str
    start: int
    end: int


class TermLockEvidence(StrictModel):
    provider_text: str
    replacements: tuple[TermReplacement, ...]


class TranslationResult(StrictModel):
    request_id: str
    unit_id: str
    draft: TranslationDraft | None
    finish: Literal["complete", "truncated", "refused", "error"]
    provider_response_id: str | None = None
    model: ModelSpec
    usage: Usage
    raw_response: ArtifactRef | None = None
    terminology_evidence: TermLockEvidence | None = None


class QualityIssue(StrictModel):
    issue_id: str
    category: str
    severity: Literal["critical", "major", "minor"]
    evidence_refs: tuple[str, ...]
    explanation: str


class QualityReport(StrictModel):
    unit_id: str
    result_hash: str = Field(pattern=SHA256_PATTERN)
    policy_revision: str
    status: Literal["pass", "needs_review", "blocked"]
    issues: tuple[QualityIssue, ...]
    checks_run: tuple[str, ...]
