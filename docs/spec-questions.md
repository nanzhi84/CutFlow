# Spec Questions

## ArtifactKind supplement: uploaded.file / import.mapping

M2a keeps the existing persisted ArtifactKind values `uploaded.file` and `import.mapping` for clean-slate continuity, but they are no longer unregistered compatibility strays.

Proposal for spec 32.1/32.2:

- `uploaded.file` maps to `UploadedFileArtifact`.
- `import.mapping` maps to `ImportMappingArtifact`.

Both values should remain in the final ArtifactKind enum unless a later migration explicitly replaces persisted upload/import references.

## M5 CI hosting decision

M5 adds `.github/workflows/ci.yml` as the proposed GitHub Actions acceptance gate for unit, DB/Temporal integration, and frontend jobs. The repository currently has no confirmed remote/hosting policy in this clone.

Question for architecture/ops: keep GitHub Actions as the canonical hosted CI, or mirror the same jobs into the eventual hosting provider's pipeline while retaining `scripts/ci_gate.sh` as the local equivalent gate?

## Idempotency 重放状态码（2026-06-11，架构师裁决）

spec 32.11 明文「Idempotency-Key 命中：200，返回原 job/run」。M5 施工曾改为回放原始状态码（201/202），
与 spec 冲突，已裁决恢复 200 + `Idempotency-Replayed: true` 头。任何对该语义的修改需先改 spec。
