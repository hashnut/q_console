"""Config + app home.

AppHome is the ONLY place this program writes: %LOCALAPPDATA%/q_console.
It is deliberately NOT the old %LOCALAPPDATA%/usage-view, so the original
gjc build and this one never fight over the same cache.
"""

from __future__ import annotations

import json
import os
import tempfile

APP_HOME = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "q_console"
)

CONFIG_PATH = os.path.join(APP_HOME, "config.json")
CACHE_PATH = os.path.join(APP_HOME, "usage-cache.json")
DETAIL_PATH = os.path.join(APP_HOME, "detail.html")
OVERLAY_PATH = os.path.join(APP_HOME, "overlay.html")
SCAN_PATH = os.path.join(APP_HOME, "scan-cache.json")

DEFAULTS = {
    # Read-only credentials owned and refreshed by the installed clients.
    # q_console never writes these files or copies their tokens into its cache.
    "claude_credentials_file": "~/.claude/.credentials.json",
    "codex_auth_file": "~/.codex/auth.json",

    "warning_used_percent": 80,
    "theme": "surfacer",
    "always_on_top": False,
    # First launch opens the compact always-on-top strip at screen bottom-right.
    "overlay_mode": True,
}

THEME_ALIASES = {
    "board-ember": "surfacer", "board-slate": "surfacer", "board-abyss": "phosphor",
    "billboard": "surfacer", "ledger": "surfacer", "shoreline": "surfacer",
    "orbit": "surfacer", "console": "phosphor", "mono": "surfacer",
    "midnight": "phosphor", "surfacer": "surfacer", "phosphor": "phosphor",
    "mini": "mini",
}


def ensure_home() -> str:
    os.makedirs(APP_HOME, exist_ok=True)
    return APP_HOME


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
        if isinstance(user, dict):
            for key, value in user.items():
                if key in DEFAULTS:
                    cfg[key] = value
    except (OSError, ValueError):
        pass
    cfg["theme"] = THEME_ALIASES.get(str(cfg.get("theme")), "surfacer")
    return cfg


def save(cfg: dict) -> None:
    """Atomic replace: the tray reads this file on every repaint."""
    ensure_home()
    keep = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    write_atomic(CONFIG_PATH, json.dumps(keep, indent=2, ensure_ascii=False))


def update(**kwargs) -> dict:
    cfg = load()
    cfg.update(kwargs)
    save(cfg)
    return cfg


def write_atomic(path: str, text: str) -> None:
    ensure_home()
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
