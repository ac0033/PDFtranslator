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
| `v0.4.0-rc.1` | Three-page two-pass PDF smoke candidate; awaiting paid language/layout acceptance | current candidate |

The `v` prefix is a Git tag convention. Python packaging normalizes the candidate suffix, so tag `v0.4.0-rc.1` is stored as package version `0.4.0rc1`.
