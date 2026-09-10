# V1 Context Engine acceptance

Status: **implementation passed on offline fixtures; real-book V1 benchmark not yet accepted**.

## Implemented and verified

- Scoped Glossary with `candidate / approved / deprecated` states and mandatory evidence for approved entries.
- Versioned Style rules and deterministic scope precedence: unit, section, chapter, document, domain.
- Term-candidate extraction that excludes URLs and common function words; extraction never approves entries.
- Explicit neighboring-unit retrieval with a trace explaining each selection or budget trim.
- Semantic entries with source evidence; unknown evidence IDs and missing required dependencies block execution.
- Conservative Context Router budget: required items are never silently removed.
- Exact Translation Memory reuse only when source, languages, context signature, Glossary snapshot, Style snapshot, QA status and human-confirmed status all match.
- Dependency graph and transitive invalidation report.
- Changed context snapshots replace an accepted task's cache identity and return it to processing.
- Offline ablation report keeps token evidence while explicitly marking quality comparison as not run.

## Local evidence

- Automated test suite passes 23 contract, scope conflict, evidence, budget trim, missing dependency, exact TM, transitive invalidation, persistence and cache-refresh cases.
- Offline V1 demo selected two approved document-level Style rules, used 150 conservative context tokens within a 600-token budget and completed with zero provider cost.
- Demo ablation reports V0 context 42 estimated tokens versus V1 context 150 estimated tokens. This is expected because V1 adds two rules; it is not evidence of better quality or lower total cost.

## Not run and not passed

- Real `dm.pdf` terminology candidates and human approval.
- Source-grounded chapter definitions and on-demand summaries.
- Development-set context ablation with translation quality comparison.
- Frozen B1/B2 blind comparison and full-chapter run.
- BabelDOC rendering, which remains a V0 external blocker.

## Stop condition applied

No automatic summary generation and no paid V1 translation were started because the real source PDF, approved sample mapping and a new V1 benchmark budget were not supplied. The implementation preserves this missing evidence instead of manufacturing terminology or quality results from the demo fixture.
