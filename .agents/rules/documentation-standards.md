# Documentation Standards and Sync Guidelines

1. **Docstrings**:
   - Every public function, class, and module in `src/karaoke/` should include a concise docstring.
   - Describe arguments, return types, and exceptions when non-obvious.
   - Modules in `src/karaoke/` are indexed into `docs/api.md` via `mkdocstrings`.

2. **Drift Prevention**:
   - Whenever adding or renaming Python modules, run `make docs-sync`.
   - Whenever adding new Makefile targets with `##`, run `make docs-sync`.
   - Whenever adding new Markdown documentation in `docs/`, link it in `mkdocs.yml` under `nav:`.

3. **Validation**:
   - Run `make docs-audit` or `python scripts/doc_sync.py --check` before committing documentation changes.
   - Ensure `mkdocs build --strict` exits cleanly with zero errors.
