# Versioning

This project uses Semantic Versioning for software snapshots and keeps it separate from the roadmap stage names in the design plan.

- `MAJOR`: incompatible change to a declared stable public contract. The project will remain on major version `0` until the real PDF translation and rendering contract is accepted as stable.
- `MINOR`: backward-compatible capability added during `0.x` development.
- `PATCH`: backward-compatible fix that adds no new capability.

Snapshot mapping:

| SemVer tag | Roadmap content | Commit |
|---|---|---|
| `v0.1.0` | Core translation runtime and DeepSeek capability check | `fbf2333` |
| `v0.2.0` | Offline V1 Context Engine | `70abf21` |
| `v0.3.0` | Real-PDF benchmark registration and BabelDOC one-page spike | stable snapshot |
| `v0.4.0-rc.1` | Three-page two-pass offline PDF smoke candidate | release candidate |
| `v0.4.0` | Real DeepSeek three-page PDF smoke, deterministic marker repair and visual QA | stable snapshot |
| `v0.5.0-rc.1` | Semantic paragraph reconstruction, target-language resegmentation, exact short-text location and reusable capture | release candidate |
| `v0.5.0` | Real DeepSeek three-page semantic translation, deterministic cross-slot marker repair and visual QA | latest stable snapshot |
| `v0.6.0-rc.1` | Real-PDF context graph, complete provider-attempt accounting, bounded retry and development structural ablation | release candidate |
| `v0.6.0-rc.2` | Controlled development A/B harness, adaptive output ceilings, truncation recovery and blind-review export | release candidate |
| `v0.6.0-rc.5` | Paid development A/B complete; source/context isolation and marker-safety fixes; blind review pending | release candidate |
| `v0.7.0-rc.1` | Context-first whole-PDF entry, capture identity gate and non-text visual-primitives audit | release candidate |
| `v0.7.0-rc.2` | Fail-closed source-page text-operation editor; one-paragraph untouched-element proof | release candidate |
| `v0.7.0-rc.3` | Offline automatic source-segment mapping for a complete semantic group, immutable number/symbol gate, one-paragraph automatic placement proof | release candidate |
| `v0.7.0-rc.4` | Verifiable native formula/footnote literal hints in translation requests, cost/cache identity, and unresolved-content review gate | release candidate |
| `v0.7.0-rc.5` | Two-line Chinese reflow around unmoved terminal digits; in-stream insertion restores copied-text order without changing pixels | release candidate |
| `v0.7.0-rc.6` | Reject a target operation that begins with an immutable source formula or symbol glyph | release candidate |
| `v0.7.0-rc.7` | Trace complete glyph slices per PDF text operation, handle coalesced traces, and retain line-break evidence in mixed-text matching | release candidate |
| `v0.7.0-rc.8` | Exactly map one high-confidence mixed PDF line to native prose spans while leaving its inline formula and variable glyphs unchanged | release candidate |
| `v0.7.0-rc.9` | Interleave Chinese operations with unchanged source formula/variable operations; prove visual and copied-text order on one real page | release candidate |
| `v0.7.0-rc.10` | Replace the full 2.2 opening semantic paragraph in three Chinese lines while retaining its original footnote marker and all outside content | release candidate |
| `v0.7.0-rc.11` | Complete user-selected PDF run, explicit review drafts, per-page source/target images and fail-closed review bundle | working candidate |

| `v0.7.0-rc.12` | Quality-first context repair: cross-page word reconstruction, column-aware neighbors, preceding-tail excerpts and globally split injection | working candidate |

Intermediate v0.6 rc.3 and rc.4 were experimental local run revisions, not Git snapshots. The `v` prefix is a Git tag convention. Python packaging normalizes candidate suffixes, so tag `v0.7.0-rc.10` is stored as package version `0.7.0rc10`.

| `v0.7.0-rc.13` | Source-native assets, approved terminology locks, source-bound review context and accepted18-page Chinese/bilingual CS224n delivery | local working candidate; not tagged or pushed |
