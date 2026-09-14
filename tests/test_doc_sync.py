"""Tests for scripts/doc_sync.py documentation drift auditor."""

from __future__ import annotations

from pathlib import Path
import sys

# Add repo root to sys.path so scripts can be imported
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.doc_sync import (
    find_repo_root,
    get_python_modules,
    get_api_doc_modules,
    get_mkdocs_nav,
    get_doc_files,
    get_makefile_targets,
    audit_documentation,
    human_readable_title,
    fix_makefile_targets_doc,
)


def test_find_repo_root():
    root = find_repo_root()
    assert (root / "mkdocs.yml").exists()
    assert (root / "src" / "karaoke").exists()


def test_get_python_modules():
    root = find_repo_root()
    modules = get_python_modules(root)
    assert "cli" in modules
    assert "player" in modules
    assert "localcache" in modules
    assert "__init__" not in modules


def test_get_api_doc_modules():
    root = find_repo_root()
    modules = get_api_doc_modules(root)
    assert isinstance(modules, list)
    assert "cli" in modules
    assert "player" in modules


def test_get_mkdocs_nav():
    root = find_repo_root()
    nav_files, raw_lines = get_mkdocs_nav(root)
    assert isinstance(nav_files, list)
    assert "index.md" in nav_files
    assert "api.md" in nav_files
    assert len(raw_lines) > 0


def test_get_doc_files():
    root = find_repo_root()
    doc_files = get_doc_files(root)
    assert "index.md" in doc_files
    assert "api.md" in doc_files
    # Generated directory should be excluded
    assert not any(f.startswith("generated/") for f in doc_files)


def test_get_makefile_targets():
    root = find_repo_root()
    targets = get_makefile_targets(root)
    assert "help" in targets
    assert "docs" in targets
    assert "test" in targets


def test_human_readable_title():
    assert human_readable_title("admin_tui") == "Admin & Operator TUI"
    assert human_readable_title("custom_unknown_module") == "Custom Unknown Module"


def test_audit_documentation():
    root = find_repo_root()
    audit = audit_documentation(root)
    assert "drift_detected" in audit
    assert "total_python_modules" in audit
    assert audit["total_python_modules"] > 0
    assert "documented_api_modules" in audit
    assert "unlinked_docs" in audit
    assert "missing_makefile_targets" in audit
