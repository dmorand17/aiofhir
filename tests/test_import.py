"""Importing aiofhir must not pull in LiveKit or FastAPI."""

from __future__ import annotations

import subprocess
import sys


def test_import_aiofhir_loads_no_livekit_or_fastapi() -> None:
    script = """
import sys
import aiofhir

def loaded(name: str) -> list[str]:
    return [mod for mod in sys.modules if mod == name or mod.startswith(name + ".")]

assert loaded("livekit") == []
assert loaded("fastapi") == []
assert loaded("jwt") == []
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout


def test_version_comes_from_the_package_metadata() -> None:
    from importlib.metadata import version

    import aiofhir

    assert aiofhir.__version__ == version("aiofhir") == "0.1.1"
