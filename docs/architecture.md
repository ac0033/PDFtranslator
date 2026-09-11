# PDF translation runtime architecture

The implemented vertical path is:

`manifest -> TranslationUnit -> ContextPacket -> TranslationRequest -> Provider -> TranslationResult -> quality gates -> SQLite state/cache/ledger`

## Boundaries

- PDF inspection is local and read-only. PyMuPDF records hash, page count, dimensions, rotation and extractable character count.
- BabelDOC 0.6.4 owns parsing and write-back. `pdf-smoke` runs it twice per selected page: the capture pass creates stable runtime units, then the injection pass matches by exact captured content instead of callback order and writes accepted results back. The legacy general `render` command remains guarded.
- The translator receives only one explicit unit plus its explicit context. The OpenAI adapter does not use a conversation or previous response ID and sets `store=false`.
- Runtime-generated request IDs, usage and provider response IDs are not accepted from model output.
- Local cache identity includes the complete unit, context packet and model specification.

## State and recovery

Each run freezes an input hash and configuration hash. Reusing a run ID with changed inputs or configuration is rejected. Accepted, blocked and review-required units are not resubmitted on resume. SQLite uses WAL mode and commits result, quality decision and ledger entry in one transaction.

## Known document boundary

Three real source PDFs are registered locally and `dm.pdf` has frozen sample sets. Physical pages 42, 51 and 577 pass the real DeepSeek capture/runtime/injection path and merge into three-page mono/dual artifacts. Formula-only and code-placeholder units bypass the model exactly. Exact region-level source bounding boxes remain incomplete for 42 of 78 units, BabelDOC TOC migration still warns, and formula-internal English plus mixed-script spacing remain layout limitations.

## V1 context path

`TranslationUnit -> applicable scopes -> approved Style/Glossary -> explicit semantic dependencies -> optional neighbors -> exact confirmed TM -> budget selection -> ContextPacket`

Scope precedence is `unit > section > chapter > document > domain`. Conflicting approved terminology at the same scope blocks packet construction. Required style, glossary and semantic dependencies cannot be trimmed to satisfy the context budget. Optional neighbors and exact confirmed TM are trimmed last.

Every packet records selection reasons, source IDs, revisions, a memory snapshot hash and a conservative token estimate. Changing selected context changes the translation cache key; the state store returns the unit to `prepared` instead of silently retaining an accepted result from an obsolete context snapshot.
