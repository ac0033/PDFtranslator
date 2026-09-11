# PDF translation runtime architecture

The implemented vertical path is:

`manifest -> TranslationUnit -> ContextPacket -> TranslationRequest -> Provider -> TranslationResult -> quality gates -> SQLite state/cache/ledger`

## Boundaries

- PDF inspection is local and read-only. PyMuPDF records hash, page count, dimensions, rotation and extractable character count.
- BabelDOC 0.6.4 owns parsing and write-back. A one-page synthetic injection spike has passed conditionally; the general `render` command remains guarded until real run results can be mapped back into BabelDOC output.
- The translator receives only one explicit unit plus its explicit context. The OpenAI adapter does not use a conversation or previous response ID and sets `store=false`.
- Runtime-generated request IDs, usage and provider response IDs are not accepted from model output.
- Local cache identity includes the complete unit, context packet and model specification.

## State and recovery

Each run freezes an input hash and configuration hash. Reusing a run ID with changed inputs or configuration is rejected. Accepted, blocked and review-required units are not resubmitted on resume. SQLite uses WAL mode and commits result, quality decision and ledger entry in one transaction.

## Known document boundary

Three real source PDFs are registered locally and `dm.pdf` has frozen sample sets. Page 42 passed a synthetic mono/dual write-back check. Real translated-text injection, exact source bounding boxes, accepted-sample mapping, broader layout review and complete bilingual rendering remain open gates.

## V1 context path

`TranslationUnit -> applicable scopes -> approved Style/Glossary -> explicit semantic dependencies -> optional neighbors -> exact confirmed TM -> budget selection -> ContextPacket`

Scope precedence is `unit > section > chapter > document > domain`. Conflicting approved terminology at the same scope blocks packet construction. Required style, glossary and semantic dependencies cannot be trimmed to satisfy the context budget. Optional neighbors and exact confirmed TM are trimmed last.

Every packet records selection reasons, source IDs, revisions, a memory snapshot hash and a conservative token estimate. Changing selected context changes the translation cache key; the state store returns the unit to `prepared` instead of silently retaining an accepted result from an obsolete context snapshot.
