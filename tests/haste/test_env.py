"""Only the repo-root .env is loaded; a leftover haste/.env is reported, not read."""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

import pytest

from haste import env

HASTE_DIR = Path(env.__file__).parent
SCRIPTS = ("seed.py", "smoke.py", "check_policy.py", "record_fixtures.py")


def test_paths_point_at_the_repo_root() -> None:
    assert env.ENV_PATH == env.ROOT / ".env"
    assert env.IGNORED_ENV_PATH == HASTE_DIR / ".env"
    assert (env.ROOT / "pyproject.toml").exists()


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_do_not_load_dotenv_themselves(name: str) -> None:
    """Every script goes through haste.env, so no other .env can sneak back in."""
    source = (HASTE_DIR / name).read_text()
    tree = ast.parse(source)
    imports = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert "dotenv" not in imports
    assert '"haste" / ".env"' not in source


def test_load_env_reads_only_the_given_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_env = tmp_path / ".env"
    root_env.write_text("AIOFHIR_TEST_ROOT=root\n")
    haste_dir = tmp_path / "haste"
    haste_dir.mkdir()
    (haste_dir / ".env").write_text("AIOFHIR_TEST_ROOT=stale\nAIOFHIR_TEST_HASTE=stale\n")
    monkeypatch.delenv("AIOFHIR_TEST_ROOT", raising=False)
    monkeypatch.delenv("AIOFHIR_TEST_HASTE", raising=False)
    env.load_env(root_env)
    assert os.environ["AIOFHIR_TEST_ROOT"] == "root"
    assert "AIOFHIR_TEST_HASTE" not in os.environ


def test_warns_once_when_haste_env_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stray = tmp_path / ".env"
    stray.write_text("HASTE_TOKEN=do-not-print-me\n")
    monkeypatch.setattr(env, "_warned", set())
    with caplog.at_level(logging.WARNING, logger="haste.env"):
        assert env.warn_ignored_env(stray) is True
        assert env.warn_ignored_env(stray) is False
    warnings = [r for r in caplog.records if r.name == "haste.env"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "is ignored" in message
    assert "repo-root .env" in message
    assert "delete it" in message
    assert "do-not-print-me" not in caplog.text
    assert "HASTE_TOKEN" not in caplog.text


def test_no_warning_without_haste_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(env, "_warned", set())
    with caplog.at_level(logging.WARNING, logger="haste.env"):
        assert env.warn_ignored_env(tmp_path / ".env") is False
    assert not caplog.records


def test_start_script_loads_root_then_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(env, "load_env", lambda: calls.append("load"))
    monkeypatch.setattr(env, "configure_cli_logging", lambda: calls.append("logging"))

    def warn() -> bool:
        calls.append("warn")
        return False

    monkeypatch.setattr(env, "warn_ignored_env", warn)
    env.start_script()
    assert calls == ["load", "logging", "warn"]
