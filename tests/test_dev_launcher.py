"""Safe unit tests for the local development launcher."""

import importlib.util
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev.py"
_SPEC = importlib.util.spec_from_file_location("karaoke_dev_launcher", _SCRIPT)
assert _SPEC and _SPEC.loader
dev = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = dev
_SPEC.loader.exec_module(dev)


def test_kind_run_modes_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dev.py", "--with-kind"])
    assert dev.parse_args().with_kind is True

    monkeypatch.setattr(sys, "argv", ["dev.py", "--deploy-kind"])
    assert dev.parse_args().deploy_kind is True


def test_kind_context_guard_rejects_non_kind_context(monkeypatch):
    monkeypatch.setenv("KUBE_CONTEXT", "production")
    with pytest.raises(RuntimeError, match="only kind-\\* contexts"):
        dev.deploy_kind()


def test_launcher_uses_the_canonical_postgres_schema():
    schema = dev._postgres_schema()
    assert "CREATE TABLE IF NOT EXISTS tracks" in schema
    assert "CREATE TABLE IF NOT EXISTS recordings" in schema
    assert "DROP TABLE" not in schema.upper()


def test_pg_url_prefers_process_environment_to_project_dotenv(
        monkeypatch, tmp_path):
    (tmp_path / ".env").write_text(
        "KARAOKE_PG_URL='postgresql://from-dotenv'\n", encoding="utf-8"
    )
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setenv("KARAOKE_PG_URL", "postgresql://from-environment")
    assert dev._configured_pg_url() == "postgresql://from-environment"

    monkeypatch.delenv("KARAOKE_PG_URL")
    assert dev._configured_pg_url() == "postgresql://from-dotenv"
