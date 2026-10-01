"""Build the account-usage model consumed by the tray and renderer.

Three providers are exposed:

* Claude Code weekly usage (all models)
* Claude Code current session (five-hour) usage, as a second Claude row
* Fable weekly scoped usage
* Codex weekly usage

Enterprise usage-based seats show monthly account spend and its actual cap.
Codex reset credits are separate counts, never percentages or spend budgets.

All three come from the current authenticated account endpoints. Local token
logs and list-price estimates are deliberately not used as quota values.

Claude OAuth is renewed before its expiry or once after an HTTP 401. If a
live read still fails because of authentication or network trouble, the previous
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

# Short names for secondary windows in one-line summaries.
SUMMARY_KEY = {"session": "5h"}


def _limit(key: str, label: str, value: dict, now: int, warn: int,
           measured_at: int | None = None, stale: bool = False) -> dict:
    used = clamp_pct(value.get("used"))
    resets_at = value.get("resets_at")
    reset_in = max(0, resets_at - now) if resets_at else None
    stale = bool(stale and (used is not None or value.get("amount") is not None))
    unit = value.get("unit")
    account_spend = bool(value.get("account_spend"))
    if account_spend:
        currency = value.get("currency") or "USD"
        amount, budget = value.get("amount"), value.get("budget")
        primary = _account_money(amount, currency)
        budget_text = "한도 %s" % _account_money(budget, currency) if budget is not None else "한도 미제공"
        sub = "%s / %s · 계정 Usage 실측" % (primary, budget_text)
        if stale:
            sub += " · %s 마지막 실측" % _stamp_text(measured_at)
    elif unit:
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
        "measured": not unit or account_spend,
        "stale": stale,
        "unit": unit,
        "amount": value.get("amount"),
        "budget": value.get("budget"),
        "account_spend": account_spend,
        "currency": value.get("currency"),
        "period": _dt.datetime.fromtimestamp(now).strftime("%Y-%m") if account_spend else None,
        # Epoch seconds of the account read this percentage came from, so a
        # later refresh can tell how old a carried value is.
        "measured_at": measured_at if used is not None or value.get("amount") is not None else None,
        "resets_at": resets_at,
        "primary_text": primary,
        "budget_text": budget_text,
        "sub": sub,
        "reset_in": reset_in,
        "reset_text": fmt_dur(reset_in) if resets_at else "--",
        "bar_tone": tone_for(used, warn),
    }


def _account_money(amount, currency) -> str:
    if amount is None:
        return "--"
    return ("$%.2f" % amount) if currency == "USD" else "%s %.2f" % (currency, amount)


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
            prior[provider.get("id")] = dict(limits[0], provider_plan=provider.get("plan"))
        # Secondary windows (Claude's five-hour session) are keyed by window so
        # each one carries forward against its own reset time.
        for limit in limits[1:]:
            if isinstance(limit, dict) and limit.get("key"):
                prior["%s/%s" % (provider.get("id"), limit["key"])] = dict(
                    limit, provider_plan=provider.get("plan"))
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
    if limit["used"] is not None or limit.get("amount") is not None or not isinstance(prior, dict):
        return limit
    if bool(prior.get("account_spend")) != bool(limit.get("account_spend")):
        return limit
    if prior.get("key") != limit["key"]:
        return limit
    if limit.get("account_spend") and prior.get("period") != limit.get("period"):
        return limit
    if not limit.get("account_spend") and bool(prior.get("unit")) != bool(api_mode):
        return limit
    used = clamp_pct(prior.get("used"))
    if used is None and prior.get("amount") is None:
        return limit
    measured_at = prior.get("measured_at")
    if not isinstance(measured_at, (int, float)) or now - measured_at > max_age:
        return limit
    resets_at = prior.get("resets_at")
    if resets_at and resets_at <= now:
        return limit  # the window rolled over; the old percentage is void
    carried = {"used": used, "resets_at": resets_at}
    for field in ("unit", "amount", "budget", "billed", "account_spend", "currency"):
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


def _session_limit(raw: dict, prior, now: int, warn: int, max_age: int):
    """Keep the subscription session visible even when its value is unknown."""
    if raw.get("mode") == "api_key":
        return None
    value = raw.get("session") or {}
    limit = _limit("session", value.get("window_label") or "현재 세션",
                   value, now, warn, measured_at=now)
    limit = _carry_forward(limit, prior, now, warn, max_age,
                           api_mode=raw.get("mode") == "api_key")
    return limit


def _read_claude(cfg: dict, now: int, retry=None) -> dict:
    """Subscription percentage when signed in; month-to-date spend on a key.

    A live subscription always wins - its number is a real plan limit, and the
    budget gauge must never quietly stand in for one.
    """
    mode = api_key_usage.claude_auth_mode(cfg)
    if mode["mode"] == "api_key":
        return api_key_usage.collect_claude_api(cfg, now, mode)
    result = plan_usage.collect_claude(cfg, retry=retry, now=now)
    if mode["mode"] == "none" and result.get("status") != "ok":
        result = dict(result, note="Claude 로그인/API 키 없음")
    return result


def _read_codex(cfg: dict, now: int) -> dict:
    if api_key_usage.codex_auth_mode(cfg)["mode"] == "api_key":
        return api_key_usage.collect_codex_api(cfg, now)
    return plan_usage.collect_codex(cfg)


def _read_current(cfg: dict, now: int, previous=None) -> tuple[dict, dict]:
    # Both calls are independent and timeout-bounded. Running them together
    # prevents a disconnected network from making refresh wait twice.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        previous = previous if isinstance(previous, dict) else {}
        previous_claude = next((p for p in previous.get("providers", [])
                                if p.get("id") == "claude-code"), {})
        claude_future = pool.submit(_read_claude, cfg, now, previous_claude.get("usage_retry"))
        codex_future = pool.submit(_read_codex, cfg, now)
        return claude_future.result(), codex_future.result()


def _pct_text(limit: dict) -> str:
    """Percent for the one-line summaries; a trailing * means carried forward."""
    used = limit.get("used")
    if used is None:
        return (limit.get("primary_text") or "--") + ("*" if limit.get("stale") else "")
    return "%.0f%%%s" % (used, "*" if limit.get("stale") else "")


def _verdict(providers: list[dict]) -> tuple[str, str]:
    values = [_provider_summary(p) for p in providers]
    # Any window can be the one that blocks work: a full five-hour session
    # stops Claude even while the weekly bar is low.
    known = [limit.get("used") for p in providers for limit in p["limits"]
             if limit.get("used") is not None]
    if not known:
        return "계정 사용률을 불러오지 못했습니다", "degraded"
    maximum = max(known)
    mode = "blocked" if maximum >= 95 else "warning" if maximum >= 80 else "relaxed"
    return "계정 사용률 · " + " · ".join(values), mode


def _provider_summary(provider: dict) -> str:
    text = "%s %s" % (provider["label"], _pct_text(provider["limits"][0]))
    for limit in provider["limits"][1:]:
        text += " / %s %s" % (SUMMARY_KEY.get(limit["key"], limit["label"]),
                              _pct_text(limit))
    return text


def build(cfg: dict, previous=None) -> dict:
    """Assemble the snapshot. ``previous`` is the last written one, if any, and
    is used only to carry a percentage through a failed read."""
    now = int(now_ms() / 1000)
    warn = int(cfg.get("warning_used_percent") or 80)
    max_age = int(cfg.get("stale_max_age_sec") or STALE_MAX_AGE_SEC)
    claude, codex = _read_current(cfg, now, previous)
    prior = _prior_limits(previous)
    specs = [
        ("claude-code", "Claude Enterprise" if claude.get("plan") == "enterprise" else "Claude Code",
         claude, claude.get("all_models") or {}, claude.get("plan")),
        ("fable", "Fable", claude, claude.get("fable") or {}, claude.get("plan")),
        ("codex", "Codex", codex, codex.get("weekly") or {}, codex.get("plan")),
    ]
    providers = []
    for provider_id, label, raw, value, plan in specs:
        limit = _limit(value.get("window_key") or "week",
                       value.get("window_label") or "주간 사용량",
                       value, now, warn, measured_at=now)
        previous_limit = prior.get(provider_id)
        if plan is not None and previous_limit and previous_limit.get("provider_plan") != plan:
            previous_limit = None
        limit = _carry_forward(limit, previous_limit, now, warn,
                               max_age, api_mode=raw.get("mode") == "api_key")
        provider = _provider(provider_id, label, raw, limit, plan)
        if provider_id == "claude-code" and raw.get("usage_retry"):
            provider["usage_retry"] = raw["usage_retry"]
        if provider_id == "claude-code" and not limit.get("unit"):
            session_prior = prior.get("claude-code/session")
            if plan is not None and session_prior and session_prior.get("provider_plan") != plan:
                session_prior = None
            session = _session_limit(raw, session_prior, now, warn, max_age)
            if session:
                provider["limits"].append(session)
        if provider_id == "codex":
            provider["reset_credits"] = raw.get("reset_credits") or {
                "available_count": None, "applicable_available_count": None}
        elif provider_id == "claude-code" and raw.get("reset_credits") is not None:
            # Absent in API-key mode and for accounts that are not offered resets.
            provider["reset_credits"] = raw["reset_credits"]
        if provider.get("reset_credits") is not None:
            provider["reset_credit_tip"] = reset_credit_tooltip(provider["reset_credits"], now)
        providers.append(provider)
    verdict, mode = _verdict(providers)
    stamp = _dt.datetime.fromtimestamp(now)

    summary = [_provider_summary(p) + (" · " + reset_credit_text(p["reset_credits"])
                                       if p.get("reset_credits") is not None else "")
               for p in providers]

    return {
        "generated_at_ms": now * 1000,
        "poll_interval_sec": 60,
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
        for extra in provider["limits"][1:]:
            extra_used = ("--" if extra["used"] is None else "%.1f%%%s" % (
                extra["used"], "*" if extra.get("stale") else ""))
            lines.append("  %-10s %6s  reset %s" % (
                extra["label"], extra_used, extra["reset_text"]))
        if provider["status"] != "ok":
            lines.append("  %s" % provider["note"])
        if provider.get("reset_credits") is not None:
            lines.append("  " + reset_credit_text(provider["reset_credits"]))
            if provider.get("reset_credit_tip"):
                lines.append("  " + provider["reset_credit_tip"])
    lines.append("")
    if any(p["limits"][0].get("unit") and not p["limits"][0].get("account_spend") for p in providers):
        # API-key mode: the percentage is a budget gauge, not a plan limit, and
        # the report has to say so or it reads like an account number.
        lines.append("API 키 모드 · 퍼센트는 이번 달 사용량 ÷ 설정한 예산입니다.")
        lines.append("예산 변경: q_console --set-budget claude=<USD> | codex=<tokens>")
    else:
        lines.append("사용률과 Enterprise 금액은 현재 로그인 계정의 Usage 응답값입니다.")
    if any(p["limits"][0].get("stale") for p in providers):
        lines.append("* 는 이번 조회 실패로 직전 실측값을 유지한 항목입니다.")
    return "\n".join(lines)


def reset_credit_tooltip(credits, now: int) -> str:
    """Hover text for reset credits that can be used now."""
    credits = credits or {}
    if not credits.get("applicable_available_count"):
        return ""
    expiries = credits.get("expires_at")
    if expiries is None:
        return "리셋권 만료 시각 미확인"
    labels = credits.get("expiry_labels") or []
    rows = []
    for index, at in enumerate(expiries):
        if at <= now:
            continue
        label = labels[index] if index < len(labels) and labels[index] else ""
        rows.append("%s%s (%s 남음)" % (
            label + " " if label else "",
            _dt.datetime.fromtimestamp(at).strftime("%m-%d %H:%M"), fmt_dur(at - now)))
    if not rows:
        return "리셋권 만료 시각 미제공"
    return "리셋권 만료 · " + ", ".join(rows)


def reset_credit_text(credits) -> str:
    credits = credits or {}
    applicable = credits.get("applicable_available_count")
    return "리셋권 %s" % ("--" if applicable is None else "%d개" % applicable)
