# V0 architecture

The implemented vertical path is:

`manifest -> TranslationUnit -> ContextPacket -> TranslationRequest -> Provider -> TranslationResult -> quality gates -> SQLite state/cache/ledger`

## Boundaries

- PDF inspection is local and read-only. PyMuPDF records hash, page count, dimensions, rotation and extractable character count.
- BabelDOC owns future parsing and write-back. V0 currently guards `render` as unavailable instead of claiming compatibility before the one-page spike.
- The translator receives only one explicit unit plus its explicit context. The OpenAI adapter does not use a conversation or previous response ID and sets `store=false`.
- Runtime-generated request IDs, usage and provider response IDs are not accepted from model output.
- Local cache identity includes the complete unit, context packet and model specification.

## State and recovery

Each run freezes an input hash and configuration hash. Reusing a run ID with changed inputs or configuration is rejected. Accepted, blocked and review-required units are not resubmitted on resume. SQLite uses WAL mode and commits result, quality decision and ledger entry in one transaction.

## Known V0 boundary

The repository did not receive the real source PDF, accepted sample or budget. Therefore the BabelDOC adapter, real translation and bilingual rendering remain unverified external gates.

