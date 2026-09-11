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

The `v` prefix is a Git tag convention. Python packaging normalizes candidate suffixes, so tag `v0.6.0-rc.2` is stored as package version `0.6.0rc2`.
