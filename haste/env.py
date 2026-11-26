"""The one .env the Haste scripts read: the repo root's.

``haste/.env`` used to be a fallback. It is ignored now, and a script that
finds one logs a warning so stale values (an expired ``HASTE_TOKEN``, say)
cannot hide there. The warning names the file only; its values are never read.
"""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

from aiofhir.log import configure_cli_logging

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
IGNORED_ENV_PATH = ROOT / "haste" / ".env"
logger = logging.getLogger("haste.env")
_warned: set[Path] = set()


def load_env(path: Path = ENV_PATH) -> None:
    """Load the repo-root .env. Values already in the environment win."""
    load_dotenv(path)


def warn_ignored_env(path: Path = IGNORED_ENV_PATH) -> bool:
    """Log one warning per process when the old haste/.env still exists."""
    if path in _warned or not path.exists():
        return False
    _warned.add(path)
    logger.warning(
        "%s is ignored. Move any values you still need to the repo-root .env, then delete it.",
        _display(path),
    )
    return True


def start_script() -> None:
    """Load the root .env, set up logging (LOG_LEVEL may come from .env), then warn."""
    load_env()
    configure_cli_logging()
    warn_ignored_env()


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)
