"""Where the app keeps its data.

When running from source, that is the project folder. When running as a bundled
PyInstaller exe, the exe unpacks itself to a temp folder that is deleted on exit,
so config/logs/credentials must live elsewhere: next to the exe if that folder is
writable (portable use), otherwise under %APPDATA% (e.g. an install in Program
Files, which is read-only for normal users).
"""

import os
import sys
from pathlib import Path

APP_NAME = "GmailAutoSender"


def _is_writable(directory: Path) -> bool:
    probe = directory / ".write_test"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if _is_writable(exe_dir):
            return exe_dir
        appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        fallback = Path(appdata) / APP_NAME if appdata else Path.home() / f".{APP_NAME}"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return Path(__file__).resolve().parent


BASE_DIR = _resolve_base_dir()

CONFIG_PATH = BASE_DIR / "config.json"
TEMPLATES_PATH = BASE_DIR / "templates.json"
CREDENTIALS_PATH = BASE_DIR / "gmail_credentials.json"
LOG_DIR = BASE_DIR / "logs"
