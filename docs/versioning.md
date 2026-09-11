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
| `v0.3.0` | Real-PDF benchmark registration and BabelDOC one-page spike | current snapshot |

The `v` prefix is a Git tag convention; the package version itself is stored without the prefix.
