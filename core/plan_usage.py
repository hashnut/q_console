"""Read current plan percentages from the signed-in desktop tools.

These are account-level values, not estimates reconstructed from local token
logs. Credentials are read from the files owned by Claude Code and Codex for
each refresh, used only as Authorization headers, and never copied into the
q_console cache.

If an endpoint or credential is unavailable, the caller gets ``None`` rather
than a stale local-log percentage.
"""

from __future__ import annotations

import datetime as _dt
import json
import urllib.error
import urllib.request

from . import config

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TIMEOUT_SEC = 8


def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def _get_json(url: str, headers: dict) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not include the response body: auth failures can contain account
        # details, and q_console only needs a short status message.
        raise RuntimeError("HTTP %d" % exc.code) from None
    except urllib.error.URLError:
        raise RuntimeError("network unavailable") from None
    if not isinstance(value, dict):
        raise RuntimeError("unexpected response")
    return value


def _iso_epoch(value) -> int | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return int(_dt.datetime.fromisoformat(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return None


def _claude_limit(data: dict, kind: str, model_name: str | None = None) -> dict:
    """Extract a current Claude limit, preferring the client's limits array."""
    for item in data.get("limits") or []:
        if not isinstance(item, dict) or item.get("kind") != kind:
            continue
        if model_name is not None:
            scope = item.get("scope") or {}
            model = scope.get("model") or {}
            display = str(model.get("display_name") or "")
            if display.casefold() != model_name.casefold():
                continue
        return {
            "used": _number(item.get("percent")),
            "resets_at": _iso_epoch(item.get("resets_at")),
        }
    return {"used": None, "resets_at": None}


def extract_claude(data: dict) -> dict:
    all_models = _claude_limit(data, "weekly_all")
    fable = _claude_limit(data, "weekly_scoped", "Fable")

    # Compatibility with older Claude Code responses that predate limits[].
    if all_models["used"] is None:
        legacy = data.get("seven_day") or {}
        all_models = {
            "used": _number(legacy.get("utilization")),
            "resets_at": _iso_epoch(legacy.get("resets_at")),
        }
    if fable["used"] is None:
        legacy = data.get("seven_day_overage_included") or {}
        fable = {
            "used": _number(legacy.get("utilization")),
            "resets_at": _iso_epoch(legacy.get("resets_at")),
        }
    return {"all_models": all_models, "fable": fable}


def collect_claude(cfg: dict) -> dict:
    path = config.expand(cfg.get("claude_credentials_file") or
                         "~/.claude/.credentials.json")
    try:
        credentials = _load_json(path)
        oauth = credentials.get("claudeAiOauth") or {}
        token = oauth.get("accessToken")
        if not token:
            raise RuntimeError("Claude login not found")
        data = _get_json(CLAUDE_USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "q_console/1.0 (Claude Code usage)",
        })
        limits = extract_claude(data)
        return {"status": "ok", "note": "Claude 계정 Usage 실측", **limits}
    except (OSError, ValueError, RuntimeError) as exc:
        return {
            "status": "unavailable",
            "note": "Claude 사용률 확인 실패: %s" % exc,
            "all_models": {"used": None, "resets_at": None},
            "fable": {"used": None, "resets_at": None},
        }


def extract_codex(data: dict) -> dict:
    rate_limit = data.get("rate_limit") or {}
    candidates = []
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        seconds = int(window.get("limit_window_seconds") or 0)
        candidates.append((seconds, window))

    # The Usage screen's weekly bar is the seven-day account window. Prefer
    # that exact duration; otherwise use the longest reported account window.
    weekly = next((window for seconds, window in candidates
                   if seconds >= 6 * 86400), None)
    if weekly is None and candidates:
        weekly = max(candidates, key=lambda row: row[0])[1]
    weekly = weekly or {}
    reset_at = weekly.get("reset_at")
    try:
        reset_at = int(reset_at) if reset_at is not None else None
    except (TypeError, ValueError, OverflowError):
        reset_at = None
    return {
        "weekly": {
            "used": _number(weekly.get("used_percent")),
            "resets_at": reset_at,
        },
        "plan": data.get("plan_type"),
    }


def collect_codex(cfg: dict) -> dict:
    path = config.expand(cfg.get("codex_auth_file") or "~/.codex/auth.json")
    try:
        credentials = _load_json(path)
        tokens = credentials.get("tokens") or {}
        token = tokens.get("access_token")
        account_id = tokens.get("account_id")
        if not token or not account_id:
            raise RuntimeError("Codex login not found")
        data = _get_json(CODEX_USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "ChatGPT-Account-Id": str(account_id),
            "Accept": "application/json",
            "User-Agent": "q_console/1.0 (Codex usage)",
        })
        current = extract_codex(data)
        return {"status": "ok", "note": "Codex 계정 Usage 실측", **current}
    except (OSError, ValueError, RuntimeError) as exc:
        return {
            "status": "unavailable",
            "note": "Codex 사용률 확인 실패: %s" % exc,
            "weekly": {"used": None, "resets_at": None},
            "plan": None,
        }
