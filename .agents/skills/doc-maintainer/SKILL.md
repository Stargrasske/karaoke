---
name: doc-maintainer
description: >-
  Maintains, syncs, and audits project documentation, API references (mkdocstrings),
  MkDocs site navigation, and Makefile targets. Use when updating docs after code changes,
  resolving documentation drift, or running docs verification pipelines.
---

# Documentation Maintainer Runbook (`doc-maintainer`)

This skill defines the procedures and guidelines for maintaining, synchronizing, and auditing documentation in the Karaoke project using cost-efficient operations.

## Guiding Principles

1. **Cost Efficiency First**: Use lower-tier / fast models (e.g. `flash`) for documentation synchronization, docstring formatting, and file auditing rather than expensive reasoning models.
2. **Single Source of Truth**: Code docstrings in `src/karaoke/` generate the API reference via `mkdocstrings`. Keep docstrings accurate, concise, and up to date with implementation changes.
3. **Strict Validation**: Always verify that the documentation builds cleanly without warnings using `make docs` (`mkdocs build --strict`).

---

## Workflow Steps

### 1. Audit Documentation Drift
Run the automated documentation auditor:
```bash
make docs-audit
# Or directly: python scripts/doc_sync.py --check
```
This checks:
- Any Python module in `src/karaoke/*.py` missing from `docs/api.md`.
- Any orphaned `.md` file in `docs/` missing from `mkdocs.yml` navigation.
- Any Makefile target missing from `docs/makefile-targets.md`.

### 2. Synchronize Automatically
If drift is detected in API modules or Makefile targets, run:
```bash
make docs-sync
# Or directly: python scripts/doc_sync.py --fix
```
This automatically:
- Injects missing Python module autodoc blocks into `docs/api.md` with formatted section headings.
- Regenerates `docs/makefile-targets.md` matching `make help`.

### 3. Review Navigation for New Documents
If new `.md` files were added under `docs/`:
- Open `mkdocs.yml`.
- Place the document under the appropriate category (`Architecture`, `Storage & Data`, `Modes`, `Plans & Design`, etc.).
- Ensure paths are relative to `docs/`.

### 4. Build and Verify
Run the strict MkDocs build to verify all cross-links, Mermaid diagrams, and autodoc directives:
```bash
.venv/bin/mkdocs build --strict
```

### 5. Git & PR Automation (When Applicable)
When creating documentation pull requests:
- Create a dedicated topic branch:
  ```bash
  git checkout -b agent/docs-<feature-or-date>
  ```
- Make targeted commits following Conventional Commits (`docs: update API reference for <module>`).
- Open a PR using GitHub CLI:
  ```bash
  gh pr create --title "docs: <summary>" --body "Automated documentation synchronization via doc_maintainer."
  ```
- Check CI status:
  ```bash
  gh pr checks
  ```
