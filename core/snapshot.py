"""Build the account-usage model consumed by the tray and renderer.

Only three percentages are exposed:

* Claude Code weekly usage (all models)
* Fable weekly scoped usage
* Codex weekly usage

All three come from the current authenticated account endpoints. Local token
logs and list-price estimates are deliberately not used as quota values.

When a live read fails - most often because Claude Code's OAuth access token
expired while the app was closed, which answers HTTP 401 - the previous
snapshot's percentage is carried forward and flagged stale rather than shown as
"--". A carried value is dropped once it can no longer be true: after its own
weekly window has reset, or after ``stale_max_age_sec``.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt

from . import api_key_usage, plan_usage
from .util import clamp_pct, fmt_dur, fmt_tokens, fmt_usd, now_ms, tone_for


# A carried-forward percentage stops standing in for a live read after this
# long, even if its weekly window has not reset yet.
STALE_MAX_AGE_SEC = 24 * 3600


def _limit(key: str, label: str, value: dict, now: int, warn: int,
           measured_at: int | None = None, stale: bool = False) -> dict:
    used = clamp_pct(value.get("used"))
    resets_at = value.get("resets_at")
    reset_in = max(0, resets_at - now) if resets_at else None
    stale = bool(stale and used is not None)
    unit = value.get("unit")
    if unit:
        # API-key mode: the percentage is spend against a budget the user set,
        # not a plan limit, so it must not wear the "실측" badge.
        primary, budget_text, sub = _budget_texts(value, unit)
    else:
        primary = ("%.0f%%" % used) if used is not None else "--"
        budget_text = "마지막 실측" if stale else "계정 실측"
        sub = ("%s 기준 마지막 실측값" % _stamp_text(measured_at)) if stale             else "계정 Usage 실측"
    return {
        "key": key,
        "label": label,
        "used": used,
        "measured": not unit,
        "stale": stale,
        "unit": unit,
        "amount": value.get("amount"),
        "budget": value.get("budget"),
        # Epoch seconds of the account read this percentage came from, so a
        # later refresh can tell how old a carried value is.
        "measured_at": measured_at if used is not None else None,
        "resets_at": resets_at,
        "primary_text": primary,
        "budget_text": budget_text,
        "sub": sub,
        "reset_in": reset_in,
        "reset_text": fmt_dur(reset_in) if resets_at else "--",
        "bar_tone": tone_for(used, warn),
    }


def _budget_texts(value: dict, unit: str) -> tuple:
    """Spend / budget / where the number came from, for a per-token account."""
    amount = value.get("amount")
    budget = value.get("budget")
    fmt = fmt_usd if unit == "usd" else fmt_tokens
    primary = fmt(amount) if amount is not None else "--"
    budget_text = ("예산 %s" % fmt(budget)) if budget else "예산 미설정"
    origin = ("이번 달 청구 실측" if value.get("billed")
              else "이번 달 · 로컬 세션 로그 환산")
    # The hero row shows only `sub`, so the spend itself has to live here or a
    # per-token user sees a percentage with no dollars behind it.
    sub = "%s / %s · %s" % (primary, budget_text, origin)
    return primary, budget_text, sub


def _stamp_text(epoch_sec) -> str:
    if not epoch_sec:
        return "이전"
    return _dt.datetime.fromtimestamp(epoch_sec).strftime("%m-%d %H:%M")


def _prior_limits(previous) -> dict:
    """{provider_id: limit} from the last written snapshot, for carry-forward."""
    prior = {}
    if not isinstance(previous, dict):
        return prior
    for provider in previous.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        limits = provider.get("limits") or []
        if limits and isinstance(limits[0], dict):
            prior[provider.get("id")] = limits[0]
    return prior


def _carry_forward(limit: dict, prior, now: int, warn: int, max_age: int,
                   api_mode: bool = False) -> dict:
    """Re-use the last good percentage when this refresh could not read one.

    Refused when the carried number could no longer be true: its own weekly
    window has already reset, it is older than ``max_age``, or it was measured
    in the other mode - a plan percentage and a budget percentage are different
    quantities, and one must never be shown wearing the other's label. In those
    cases the caller keeps the honest "--".
    """
    if limit["used"] is not None or not isinstance(prior, dict):
        return limit
    if bool(prior.get("unit")) != bool(api_mode):
        return limit
    used = clamp_pct(prior.get("used"))
    if used is None:
        return limit
    measured_at = prior.get("measured_at")
    if not isinstance(measured_at, (int, float)) or now - measured_at > max_age:
        return limit
    resets_at = prior.get("resets_at")
    if resets_at and resets_at <= now:
        return limit  # the window rolled over; the old percentage is void
    carried = {"used": used, "resets_at": resets_at}
    for field in ("unit", "amount", "budget", "billed"):
        if prior.get(field) is not None:
            carried[field] = prior[field]
    return _limit(limit["key"], limit["label"], carried, now, warn,
                  measured_at=int(measured_at), stale=True)


def _provider(provider_id: str, label: str, raw: dict, limit: dict,
              plan=None) -> dict:
    status = raw.get("status") or "unavailable"
    note = raw.get("note") or ""
    if limit.get("stale"):
        # The read failed, but the strip still shows a real number - say which
        # one, so a kept value is never mistaken for a fresh one.
        status = "stale"
        note = ("%s · %s 값 유지" % (note, _stamp_text(limit.get("measured_at")))
                if note else "%s 값 유지" % _stamp_text(limit.get("measured_at")))
    return {
        "id": provider_id,
        "label": label,
        "status": status,
        "note": note,
        "limits": [limit],
        "plan": plan,
        "last_ms": None,
        "models": [],
        "heat": None,
        "trend": [],
        "windows": {},
    }


def _read_claude(cfg: dict, now: int) -> dict:
    """Subscription percentage when signed in; month-to-date spend on a key.

    A live subscription always wins - its number is a real plan limit, and the
    budget gauge must never quietly stand in for one.
    """
    mode = api_key_usage.claude_auth_mode(cfg)
    if mode["mode"] == "api_key":
        return api_key_usage.collect_claude_api(cfg, now, mode)
    result = plan_usage.collect_claude(cfg)
    if mode["mode"] == "none" and result.get("status") != "ok":
        result = dict(result, note="Claude 로그인/API 키 없음")
    return result


def _read_codex(cfg: dict, now: int) -> dict:
    if api_key_usage.codex_auth_mode(cfg)["mode"] == "api_key":
        return api_key_usage.collect_codex_api(cfg, now)
    return plan_usage.collect_codex(cfg)


def _read_current(cfg: dict, now: int) -> tuple[dict, dict]:
    # Both calls are independent and timeout-bounded. Running them together
    # prevents a disconnected network from making refresh wait twice.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(_read_claude, cfg, now)
        codex_future = pool.submit(_read_codex, cfg, now)
        return claude_future.result(), codex_future.result()


def _pct_text(limit: dict) -> str:
    """Percent for the one-line summaries; a trailing * means carried forward."""
    used = limit.get("used")
    if used is None:
        return "--"
    return "%.0f%%%s" % (used, "*" if limit.get("stale") else "")


def _verdict(providers: list[dict]) -> tuple[str, str]:
    values = [("%s %s" % (p["label"], _pct_text(p["limits"][0])))
              for p in providers]
    known = [p["limits"][0].get("used") for p in providers
             if p["limits"][0].get("used") is not None]
    if not known:
        return "계정 사용률을 불러오지 못했습니다", "degraded"
    maximum = max(known)
    mode = "blocked" if maximum >= 95 else "warning" if maximum >= 80 else "relaxed"
    return "계정 사용률 · " + " · ".join(values), mode


def build(cfg: dict, previous=None) -> dict:
    """Assemble the snapshot. ``previous`` is the last written one, if any, and
    is used only to carry a percentage through a failed read."""
    now = int(now_ms() / 1000)
    warn = int(cfg.get("warning_used_percent") or 80)
    max_age = int(cfg.get("stale_max_age_sec") or STALE_MAX_AGE_SEC)
    claude, codex = _read_current(cfg, now)
    prior = _prior_limits(previous)
    specs = [
        ("claude-code", "Claude Code", claude, claude.get("all_models") or {}, None),
        ("fable", "Fable", claude, claude.get("fable") or {}, None),
        ("codex", "Codex", codex, codex.get("weekly") or {}, codex.get("plan")),
    ]
    providers = []
    for provider_id, label, raw, value, plan in specs:
        limit = _limit("week", "주간 사용량", value, now, warn, measured_at=now)
        limit = _carry_forward(limit, prior.get(provider_id), now, warn,
                               max_age, api_mode=raw.get("mode") == "api_key")
        providers.append(_provider(provider_id, label, raw, limit, plan))
    verdict, mode = _verdict(providers)
    stamp = _dt.datetime.fromtimestamp(now)

    summary = [("%s %s" % (p["label"], _pct_text(p["limits"][0])))
               for p in providers]

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
                    "status_tone": {"ok": "relaxed", "stale": "caution"}.get(
                        p["status"], "unavailable"),
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
        used = ("--" if limit["used"] is None
                else "%.1f%%%s" % (limit["used"], "*" if limit.get("stale") else ""))
        lines.append("%-12s %6s  reset %s%s" % (
            provider["label"], used, limit["reset_text"],
            ("  (%s / %s)" % (limit["primary_text"], limit["budget_text"]))
            if limit.get("unit") else ""))
        if provider["status"] != "ok":
            lines.append("  %s" % provider["note"])
    lines.append("")
    if any(p["limits"][0].get("unit") for p in providers):
        # API-key mode: the percentage is a budget gauge, not a plan limit, and
        # the report has to say so or it reads like an account number.
        lines.append("API 키 모드 · 퍼센트는 이번 달 사용량 ÷ 설정한 예산입니다.")
        lines.append("예산 변경: q_console --set-budget claude=<USD> | codex=<tokens>")
    else:
        lines.append("모든 퍼센트는 현재 로그인 계정의 Usage 응답값입니다.")
    if any(p["limits"][0].get("stale") for p in providers):
        lines.append("* 는 이번 조회 실패로 직전 실측값을 유지한 항목입니다.")
    return "\n".join(lines)
