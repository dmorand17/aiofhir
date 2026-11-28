"""AIOFHIR_LOG_LEVEL sets aiofhir's log level on import, without taking over app logging.

Each case runs in a fresh interpreter, because the variable is read on import.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _run(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if k not in ("AIOFHIR_LOG_LEVEL", "LOG_UTC")}
    return subprocess.run(
        [sys.executable, "-c", script],
        env={**clean, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_script_with_no_logging_sees_redacted_debug_lines() -> None:
    result = _run(
        "import logging, aiofhir\n"
        "logging.getLogger('aiofhir.client').debug('GET Patient/pt-secret with Bearer abc.def')\n",
        AIOFHIR_LOG_LEVEL="debug",
    )
    assert result.returncode == 0, result.stderr
    lines = result.stderr.splitlines()
    assert len(lines) == 1
    assert "DEBUG aiofhir.client: GET Patient/[id]" in lines[0]
    assert "pt-secret" not in result.stderr and "abc.def" not in result.stderr
    assert lines[0][:4].isdigit() and "T" in lines[0][:11]  # ISO 8601 timestamp


def test_an_app_that_configures_logging_later_gets_each_line_once_in_its_format() -> None:
    result = _run(
        "import logging, aiofhir\n"
        "logging.basicConfig(format='APP %(name)s %(message)s')\n"
        "logging.getLogger('aiofhir.client').info('hello from aiofhir')\n",
        AIOFHIR_LOG_LEVEL="INFO",
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.splitlines() == ["APP aiofhir.client hello from aiofhir"]


def test_the_level_applies_to_aiofhir_only() -> None:
    result = _run(
        "import logging, aiofhir\n"
        "print(logging.getLogger('aiofhir').level, logging.getLogger().level,"
        " logging.getLogger('httpx').level)\n",
        AIOFHIR_LOG_LEVEL="DEBUG",
    )
    assert result.stdout.split() == ["10", "30", "0"]


def test_without_the_variable_nothing_changes() -> None:
    result = _run(
        "import logging, aiofhir\n"
        "log = logging.getLogger('aiofhir')\n"
        "print(log.level, [type(h).__name__ for h in log.handlers])\n"
        "logging.getLogger('aiofhir.client').info('should not print')\n"
    )
    assert result.stdout.strip() == "0 ['NullHandler']"
    assert result.stderr == ""


def test_an_unknown_level_is_ignored_with_a_warning() -> None:
    result = _run(
        "import logging, aiofhir\nprint(logging.getLogger('aiofhir').level)\n",
        AIOFHIR_LOG_LEVEL="chatty",
    )
    assert result.stdout.strip() == "0"
    assert "AIOFHIR_LOG_LEVEL" in result.stderr and "chatty" in result.stderr
