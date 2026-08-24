"""Build the account-usage model consumed by the tray and renderer.

Only three percentages are exposed:

* Claude Code weekly usage (all models)
* Fable weekly scoped usage
* Codex weekly usage

All three come from the current authenticated account endpoints. Local token
logs and list-price estimates are deliberately not used as quota values.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt

from . import plan_usage
from .util import clamp_pct, fmt_dur, now_ms, tone_for


def _limit(key: str, label: str, value: dict, now: int, warn: int) -> dict:
    used = clamp_pct(value.get("used"))
    resets_at = value.get("resets_at")
    reset_in = max(0, resets_at - now) if resets_at else None
    return {
        "key": key,
        "label": label,
        "used": used,
        "measured": True,
        "primary_text": ("%.0f%%" % used) if used is not None else "--",
        "budget_text": "계정 실측",
        "sub": "계정 Usage 실측",
        "reset_in": reset_in,
        "reset_text": fmt_dur(reset_in) if resets_at else "--",
        "bar_tone": tone_for(used, warn),
    }


def _provider(provider_id: str, label: str, raw: dict, value: dict,
              now: int, warn: int, plan=None) -> dict:
    return {
        "id": provider_id,
        "label": label,
        "status": raw.get("status") or "unavailable",
        "note": raw.get("note") or "",
        "limits": [_limit("week", "주간 사용량", value, now, warn)],
        "plan": plan,
        "last_ms": None,
        "models": [],
        "heat": None,
        "trend": [],
        "windows": {},
    }


def _read_current(cfg: dict) -> tuple[dict, dict]:
    # Both calls are independent and timeout-bounded. Running them together
    # prevents a disconnected network from making refresh wait twice.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(plan_usage.collect_claude, cfg)
        codex_future = pool.submit(plan_usage.collect_codex, cfg)
        return claude_future.result(), codex_future.result()


def _verdict(providers: list[dict]) -> tuple[str, str]:
    values = []
    for provider in providers:
        value = provider["limits"][0].get("used")
        values.append("%s %s" % (
            provider["label"], "--" if value is None else "%.0f%%" % value))
    known = [p["limits"][0].get("used") for p in providers
             if p["limits"][0].get("used") is not None]
    if not known:
        return "계정 사용률을 불러오지 못했습니다", "degraded"
    maximum = max(known)
    mode = "blocked" if maximum >= 95 else "warning" if maximum >= 80 else "relaxed"
    return "계정 사용률 · " + " · ".join(values), mode


def build(cfg: dict) -> dict:
    now = int(now_ms() / 1000)
    warn = int(cfg.get("warning_used_percent") or 80)
    claude, codex = _read_current(cfg)
    providers = [
        _provider("claude-code", "Claude Code", claude,
                  claude.get("all_models") or {}, now, warn),
        _provider("fable", "Fable", claude,
                  claude.get("fable") or {}, now, warn),
        _provider("codex", "Codex", codex,
                  codex.get("weekly") or {}, now, warn, codex.get("plan")),
    ]
    verdict, mode = _verdict(providers)
    stamp = _dt.datetime.fromtimestamp(now)

    summary = []
    for provider in providers:
        value = provider["limits"][0].get("used")
        summary.append("%s %s" % (
            provider["label"], "--" if value is None else "%.0f%%" % value))

    return {
        "generated_at_ms": now * 1000,
        "poll_interval_sec": 1800,
        "quota_axis_scope": "authenticated account usage",
        "pattern_axis_scope": None,
        "config_status": "ok",
        "config_error": None,
        "config_values": dict(cfg),
        "hover_line": verdict,
        "hover_mode": mode,
        "summary_lines": summary,
        "providers": providers,
        "generated_stamp": stamp.strftime("%m-%d %H:%M"),
        "detail_text": text_report(providers, stamp),
        "gui_model": {
            "banner": {"text": verdict, "mode": mode, "tone": mode, "age_text": ""},
            "config_error": False,
            "config_error_text": "",
            "providers": [
                {
                    "id": p["id"], "label": p["label"], "status": p["status"],
                    "status_tone": "relaxed" if p["status"] == "ok" else "unavailable",
                    "note": p["note"],
                    "accounts": [{"label": p["label"], "limits": p["limits"]}],
                    "heatmap": None, "recommend": None,
                }
                for p in providers
            ],
            "generated_stamp": stamp.strftime("%m-%d %H:%M"),
        },
    }


def text_report(providers: list[dict], stamp: _dt.datetime) -> str:
    lines = ["q_console  %s" % stamp.strftime("%Y-%m-%d %H:%M"), ""]
    for provider in providers:
        limit = provider["limits"][0]
        used = "--" if limit["used"] is None else "%.1f%%" % limit["used"]
        lines.append("%-12s %6s  reset %s" % (
            provider["label"], used, limit["reset_text"]))
        if provider["status"] != "ok":
            lines.append("  %s" % provider["note"])
    lines.extend(["", "모든 퍼센트는 현재 로그인 계정의 Usage 응답값입니다."])
    return "\n".join(lines)
