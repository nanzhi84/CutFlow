# Spec Questions

## ArtifactKind supplement: uploaded.file / import.mapping

M2a keeps the existing persisted ArtifactKind values `uploaded.file` and `import.mapping` for clean-slate continuity, but they are no longer unregistered compatibility strays.

Proposal for spec 32.1/32.2:

- `uploaded.file` maps to `UploadedFileArtifact`.
- `import.mapping` maps to `ImportMappingArtifact`.

Both values should remain in the final ArtifactKind enum unless a later migration explicitly replaces persisted upload/import references.
