"""Usage tracking for people who pay per token instead of subscribing.

A subscription exposes one number q_console can simply read: "you are at X% of
this week's plan". An API key exposes no such number - there is no plan and no
weekly window, only spend that accrues until the monthly invoice. So this module
reports the one thing that is both knowable and honest:

    spend so far this calendar month, against a budget the user set

Where the spend comes from, best source first:

1. ADMIN COST API (`/v1/organizations/cost_report`). Real billed cost, every
   machine and every app on the organization. Needs an Admin key
   (``sk-ant-admin01-...``), which Anthropic issues to organizations only - a
   personal account cannot create one - so this is opt-in and never assumed.
2. LOCAL SESSION LOGS. Claude Code writes every response's token usage to
   ~/.claude/projects, and Codex writes its own to ~/.codex/sessions. Priced at
   the public list rates in ``claude_code.PRICING``, that is an accurate figure
   for work done through the CLI on THIS machine - which is what q_console is
   watching anyway. It cannot see API calls made from anywhere else, and the
   note says so.

Codex is reported in tokens rather than dollars: q_console has never carried an
OpenAI price table, and inventing one would be a guess printed as a fact.
"""

from __future__ import annotations

import calendar
import datetime as _dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from . import claude_code, codex, config, store

COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
TIMEOUT_SEC = 8

# Where an Anthropic key may be sitting, in the order Claude Code itself would
# find one. Values are read only to learn THAT a key exists (and, for the admin
# key, to send it); no key is ever written into q_console's cache.
API_KEY_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
ADMIN_KEY_ENV = ("ANTHROPIC_ADMIN_KEY",)
GATEWAY_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


# ── auth mode ───────────────────────────────────────────────────────────────

def _settings_env(path: str) -> dict:
    """Claude Code's settings.json can carry env vars, including the API key."""
    try:
        with open(config.expand(path), encoding="utf-8") as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        return {}
    env = blob.get("env") if isinstance(blob, dict) else None
    return env if isinstance(env, dict) else {}


def _oauth_token(cfg: dict):
    path = config.expand(cfg.get("claude_credentials_file") or
                         "~/.claude/.credentials.json")
    try:
        with open(path, encoding="utf-8") as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        return None
    oauth = (blob or {}).get("claudeAiOauth") or {}
    return oauth.get("accessToken") or None


def _opencodex_api_key() -> str | None:
    try:
        p = os.path.expanduser("~/.opencodex/config.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as handle:
                data = json.load(handle)
            prov = (data.get("providers") or {}).get("anthropic-apikey") or {}
            key = prov.get("apiKey")
            if key and isinstance(key, str) and key.strip():
                return key.strip()
    except Exception:
        pass
    return None


def claude_auth_mode(cfg: dict) -> dict:
    """-> {"mode": "subscription"|"api_key"|"none", "source", "admin_key"}

    A logged-in subscription wins: its percentage is a real plan limit, and a
    budget gauge must never quietly replace it.
    """
    admin_key = (cfg.get("anthropic_admin_key") or "").strip()
    for name in ADMIN_KEY_ENV:
        admin_key = admin_key or (os.environ.get(name) or "").strip()

    forced = (cfg.get("usage_mode") or "auto").strip().lower()
    if forced == "subscription":
        return {"mode": "subscription", "source": "설정에서 고정",
                "admin_key": admin_key}
    if forced == "api_key":
        return {"mode": "api_key", "source": "설정에서 고정",
                "admin_key": admin_key}

    if _oauth_token(cfg):
        return {"mode": "subscription", "source": "구독 로그인",
                "admin_key": admin_key}

    for name in API_KEY_ENV:
        if (os.environ.get(name) or "").strip():
            return {"mode": "api_key", "source": name, "admin_key": admin_key}
    for name in GATEWAY_ENV:
        if (os.environ.get(name) or "").strip() not in ("", "0", "false"):
            return {"mode": "api_key", "source": name, "admin_key": admin_key}
    settings = _settings_env(cfg.get("claude_settings_file") or
                             "~/.claude/settings.json")
    for name in API_KEY_ENV:
        if str(settings.get(name) or "").strip():
            return {"mode": "api_key", "source": "settings.json",
                    "admin_key": admin_key}
    if _opencodex_api_key():
        return {"mode": "api_key", "source": "OpenCodex API 키", "admin_key": admin_key}
    if admin_key:
        return {"mode": "api_key", "source": "Admin 키", "admin_key": admin_key}
    return {"mode": "none", "source": "", "admin_key": ""}


def codex_auth_mode(cfg: dict) -> dict:
    """Codex CLI stores subscription tokens and API keys in the same file."""
    forced = (cfg.get("usage_mode") or "auto").strip().lower()
    if forced in ("subscription", "api_key"):
        return {"mode": forced, "source": "설정에서 고정"}
    path = config.expand(cfg.get("codex_auth_file") or "~/.codex/auth.json")
    try:
        with open(path, encoding="utf-8") as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        blob = {}
    tokens = (blob or {}).get("tokens") or {}
    if tokens.get("access_token") and tokens.get("account_id"):
        return {"mode": "subscription", "source": "구독 로그인"}
    if str((blob or {}).get("OPENAI_API_KEY") or "").strip():
        return {"mode": "api_key", "source": "auth.json"}
    if (os.environ.get("OPENAI_API_KEY") or "").strip():
        return {"mode": "api_key", "source": "OPENAI_API_KEY"}
    return {"mode": "none", "source": ""}


# ── the billing month ───────────────────────────────────────────────────────

def month_window(now: int) -> tuple:
    """-> (start_epoch, next_month_epoch) for the local calendar month.

    API usage bills by the month, so that - not a rolling week - is the window
    a per-token user is actually spending against.
    """
    stamp = _dt.datetime.fromtimestamp(now)
    start = stamp.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days = calendar.monthrange(start.year, start.month)[1]
    return int(start.timestamp()), int((start + _dt.timedelta(days=days)).timestamp())


def _gauge(amount: float, budget: float) -> float | None:
    """Spend as a percentage of the budget. A budget of 0 means 'not set'."""
    if not budget or budget <= 0:
        return None
    return max(0.0, min(100.0, amount / float(budget) * 100.0))


# ── Anthropic: admin cost report ────────────────────────────────────────────

def _get_json(url: str, headers: dict) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %d" % exc.code) from None
    except urllib.error.URLError:
        raise RuntimeError("network unavailable") from None
    if not isinstance(value, dict):
        raise RuntimeError("unexpected response")
    return value


def _iso_utc(epoch_sec: int) -> str:
    return _dt.datetime.fromtimestamp(
        epoch_sec, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def admin_cost_usd(admin_key: str, start: int, end: int) -> float:
    """Billed cost for the window, in USD.

    `amount` arrives as a decimal string in the currency's lowest unit (cents),
    so every entry is divided by 100 - not doing that reports a $2 month as
    $200. Paginates because a month can exceed one page of daily buckets.
    """
    total_cents = 0.0
    page = None
    for _ in range(8):  # 31 daily buckets fit well inside this many pages
        url = ("%s?starting_at=%s&ending_at=%s&bucket_width=1d&limit=31"
               % (COST_REPORT_URL, _iso_utc(start), _iso_utc(end)))
        if page:
            url += "&page=%s" % urllib.parse.quote(page, safe="")
        data = _get_json(url, {
            "x-api-key": admin_key,
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
            "User-Agent": "q_console/1.0 (usage dashboard)",
        })
        for bucket in data.get("data") or []:
            for item in (bucket or {}).get("results") or []:
                try:
                    total_cents += float(item.get("amount") or 0)
                except (TypeError, ValueError):
                    continue
        if not data.get("has_more"):
            break
        page = data.get("next_page")
        if not page:
            break
    return total_cents / 100.0


# ── OpenCodex Anthropic API-key scanner ─────────────────────────────────────

def _opencodex_anthropic_api_providers() -> set[str]:
    """Provider IDs in ~/.opencodex/config.json that use an Anthropic API key."""
    providers = {"anthropic-apikey"}
    try:
        cfg_path = os.path.expanduser("~/.opencodex/config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, encoding="utf-8") as fh:
                data = json.load(fh)
            for name, prov in ((data or {}).get("providers") or {}).items():
                if not isinstance(prov, dict):
                    continue
                if prov.get("adapter") == "anthropic" and (
                    prov.get("authMode") == "key" or str(prov.get("apiKey") or "").strip()
                ):
                    providers.add(str(name))
    except Exception:
        pass
    return providers


def _parse_opencodex_chunk(handle, offset: int, api_providers: set[str]) -> tuple[int, list]:
    """Parse OpenCodex usage.jsonl from byte offset and return (new_offset, rows).

    Each row is [ts_sec, model, uncached_in, out, cache_read, cw5, cw1].
    Note that OpenCodex's `inputTokens` is inclusive of cache read and cache
    creation tokens, unlike Claude Code's raw transcript where `input_tokens`
    is uncached only.
    """
    rows = []
    handle.seek(offset)
    while True:
        pos = handle.tell()
        line = handle.readline()
        if not line:
            break
        if not line.endswith(b"\n") and not line.endswith(b"\r"):
            # Incomplete trailing line being written concurrently; rewind.
            handle.seek(pos)
            break
        if b"anthropic" not in line:
            continue
        try:
            row = json.loads(line.decode("utf-8", errors="ignore"))
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        prov = str(row.get("provider") or "")
        acct = str(row.get("accountLogLabel") or "")
        # Never match OAuth subscription traffic (`provider == "anthropic"` without API key label).
        if prov not in api_providers and not (prov.startswith("anthropic") and acct.startswith("k")):
            continue
        ts_ms = row.get("timestamp") or 0
        if not ts_ms:
            continue
        # Bill at completion: the Console deducts a request when it finishes,
        # and a long Opus turn can straddle the moment the balance was read.
        try:
            ts_ms += max(0, int(row.get("durationMs") or 0))
        except (TypeError, ValueError):
            pass
        u = row.get("usage") or {}
        if not isinstance(u, dict):
            continue
        inp_total = int(u.get("inputTokens") or 0)
        out = int(u.get("outputTokens") or 0)
        cread = int(u.get("cacheReadInputTokens") or 0)
        cwrite = int(u.get("cacheCreationInputTokens") or 0)
        cw1 = min(cwrite, int(u.get("cacheCreation1hInputTokens") or 0))
        cw5 = max(0, cwrite - cw1)
        uncached_inp = max(0, inp_total - cread - cwrite)
        model = str(row.get("model") or row.get("requestedModel") or "claude-sonnet-5-5")
        if "/" in model:
            model = model.split("/", 1)[-1]
        rows.append([int(ts_ms / 1000), model, uncached_inp, out, cread, cw5, cw1])
    return handle.tell(), rows


def _opencodex_anthropic_api_records() -> list:
    """Incrementally scan ~/.opencodex/usage.jsonl without losing older rows."""
    ocx_usage_path = os.path.expanduser("~/.opencodex/usage.jsonl")
    if not os.path.exists(ocx_usage_path):
        return []
    try:
        size = int(os.path.getsize(ocx_usage_path))
    except OSError:
        return []

    api_providers = _opencodex_anthropic_api_providers()
    # v2: rows carry completion time; older cached rows carried start time.
    scanner = store.Scanner("opencodex-v2")
    scanner._all.pop("opencodex", None)  # drop the start-time rows for good
    cached = scanner._mine.get(ocx_usage_path)
    prev_offset = 0
    records = []
    if isinstance(cached, dict):
        data = cached.get("data")
        if isinstance(data, dict):
            prev_offset = int(data.get("offset") or 0)
            prev_rows = data.get("rows")
            if isinstance(prev_rows, list) and 0 <= prev_offset <= size:
                records = list(prev_rows)
            else:
                prev_offset = 0

    if prev_offset == size and cached is not None:
        scanner.keep(ocx_usage_path)
        scanner.commit()
        return records

    try:
        with open(ocx_usage_path, "rb") as handle:
            new_offset, new_rows = _parse_opencodex_chunk(handle, prev_offset, api_providers)
    except OSError:
        return records

    records.extend(new_rows)
    scanner.put(ocx_usage_path, [size], {"offset": new_offset, "rows": records})
    scanner.commit()
    return records


def _opencodex_anthropic_api_spend(start: int, end: int | None = None) -> tuple[float, float]:
    """Return (total_usd, fable_usd) from OpenCodex Anthropic API-key rows since `start`."""
    total = fable = 0.0
    for row in _opencodex_anthropic_api_records():
        if not isinstance(row, list) or len(row) < 7:
            continue
        ts, model, uncached_inp, out, cread, cw5, cw1 = row[:7]
        if ts < start or (end is not None and ts >= end):
            continue
        cost = claude_code.price_of(model, uncached_inp, out, cread, cw5, cw1)
        total += cost
        if claude_code.is_fable(model):
            fable += cost
    return total, fable


# ── collectors ──────────────────────────────────────────────────────────────

def collect_claude_api(cfg: dict, now: int, mode: dict) -> dict:
    """Month-to-date Anthropic spend against the configured budget.

    Shaped like ``plan_usage.collect_claude`` so the snapshot builder can use
    either without caring which one ran.
    """
    budget = float(cfg.get("claude_api_budget_usd") or 0)
    start, end = month_window(now)
    start_date_str = cfg.get("claude_credit_start_date")
    if start_date_str:
        try:
            dt_start = _dt.datetime.fromisoformat(str(start_date_str).strip())
            start_epoch = int(dt_start.timestamp())
            if start_epoch <= now:
                start = max(start, start_epoch)
        except Exception:
            pass
    admin_key = mode.get("admin_key") or ""

    if admin_key:
        try:
            spent = admin_cost_usd(admin_key, start, end)
            return _claude_api_result(spent, None, budget, end,
                                      "Admin Cost API 실측 · 이번 달 청구액", True)
        except (OSError, ValueError, RuntimeError) as exc:
            note = "Admin Cost API 실패(%s) · 로컬 로그로 대체" % exc
    else:
        note = None

    local = claude_code.collect(cfg)
    total = fable = 0.0
    if local.get("status") == "ok":
        for record in local["records"]:
            if record["ts"] < start:
                continue
            total += record["cost"]
            if claude_code.is_fable(record["model"]):
                fable += record["cost"]
    ocx_total, ocx_fable = _opencodex_anthropic_api_spend(start, end)
    total += ocx_total + float(cfg.get("claude_api_base_spent_usd") or 0.0)
    fable += ocx_fable
    if local.get("status") != "ok" and total <= 0:
        return {
            "status": "unavailable", "mode": "api_key",
            "note": note or ("API 키 모드 · %s" % local.get("note")),
            "all_models": {"used": None, "resets_at": None},
            "fable": {"used": None, "resets_at": None},
        }
    return _claude_api_result(
        total, fable, budget, end,
        note or "API 키 모드 · 이 PC의 Claude Code 세션 로그 환산", False)


def _claude_api_result(total: float, fable, budget: float, end: int,
                       note: str, billed: bool) -> dict:
    if not budget or budget <= 0:
        note += " · 예산 미설정(claude_api_budget_usd)"
    remaining = max(0.0, budget - total) if budget else 0.0
    detail = {
        "used": _gauge(total, budget), "resets_at": end,
        "amount": total, "budget": budget, "remaining": remaining,
        "unit": "usd", "billed": billed,
    }
    if fable is None:
        # The cost report is not broken out per model here, so there is no
        # Fable-only figure to show - saying "--" beats splitting a guess.
        fable_detail = {"used": None, "resets_at": end, "amount": None,
                        "budget": budget, "unit": "usd", "billed": billed}
    else:
        fable_detail = {"used": _gauge(fable, budget), "resets_at": end,
                        "amount": fable, "budget": budget, "unit": "usd",
                        "billed": billed}
    return {"status": "ok", "note": note, "mode": "api_key",
            "all_models": detail, "fable": fable_detail}


def collect_codex_api(cfg: dict, now: int) -> dict:
    """Month-to-date Codex tokens against the configured token budget."""
    budget = float(cfg.get("codex_api_budget_tokens") or 0)
    start, end = month_window(now)
    local = codex.collect(cfg)
    if local.get("status") != "ok":
        return {
            "status": "unavailable", "mode": "api_key",
            "note": "API 키 모드 · %s" % local.get("note"),
            "weekly": {"used": None, "resets_at": None}, "plan": None,
        }
    tokens = 0
    for slot, values in (local.get("buckets") or {}).items():
        if int(slot) * 3600 >= start:
            tokens += values[0]
    if not budget or budget <= 0:
        note = ("API 키 모드 · 이 PC의 Codex 세션 로그 실측 · "
                "예산 미설정(codex_api_budget_tokens)")
    else:
        note = "API 키 모드 · 이 PC의 Codex 세션 로그 실측"
    return {
        "status": "ok", "note": note, "mode": "api_key",
        "weekly": {"used": _gauge(tokens, budget), "resets_at": end,
                   "amount": tokens, "budget": budget, "unit": "tokens",
                   "billed": False},
        "plan": "api",
    }


def credit_anchor(cfg: dict):
    """-> (balance_usd, epoch) the user read off the Console, or None.

    An individual Anthropic organization has no API that returns the prepaid
    balance (the Admin API is unavailable to individual accounts), so the
    Console figure the user types in is the only real number. Everything after
    it is this PC's logged usage priced at list rates.
    """
    try:
        balance = float(cfg.get("claude_credit_anchor_usd"))
        at = int(cfg.get("claude_credit_anchor_at") or 0)
    except (TypeError, ValueError):
        return None
    if balance < 0 or at <= 0:
        return None
    return balance, at


def _local_api_spend(cfg: dict, mode: dict, since: int) -> float:
    """API-key spend this PC logged at or after 'since', priced at list rates.

    Claude Code's own logs count only when Claude Code itself runs on an API
    key - with a subscription login they are plan usage, not credit spend.
    OpenCodex rows are filtered to its Anthropic API-key providers either way.
    """
    spent = 0.0
    if mode.get("mode") == "api_key":
        local = claude_code.collect(cfg)
        if local.get("status") == "ok":
            for record in local.get("records") or []:
                if record.get("ts", 0) >= since:
                    spent += record.get("cost", 0.0)
    ocx_spent, _ = _opencodex_anthropic_api_spend(since)
    return spent + ocx_spent


def claude_api_credit_status(cfg: dict, now: int) -> dict:
    """Anthropic API prepaid credit balance.

    Best source first: an Admin key's billed cost, then the Console balance the
    user entered minus this PC's usage since, then a pure local estimate.
    Prepaid credits do not reset with the calendar month, so no source here
    stops counting at a month boundary.
    """
    total = float(cfg.get("claude_api_credit_usd") or cfg.get("claude_api_budget_usd") or 200.0)
    base_spent = float(cfg.get("claude_api_base_spent_usd") or 0.0)
    expiry = str(cfg.get("claude_credit_expiry") or "2026-11-04").strip()
    mode = claude_auth_mode(cfg)
    result = {"total": total, "expiry": expiry, "currency": "USD",
              "has_credits": total > 0, "anchor_usd": None, "anchor_at": None,
              "since_anchor": None}

    anchor = credit_anchor(cfg)
    if anchor and anchor[1] <= now:
        balance, at = anchor
        since_anchor = _local_api_spend(cfg, mode, at)
        remaining = max(0.0, balance - since_anchor)
        result.update(source="anchor", remaining=remaining,
                      spent=max(0.0, total - remaining), anchor_usd=balance,
                      anchor_at=at, since_anchor=since_anchor)
        return result

    start, _ = month_window(now)
    start_date_str = cfg.get("claude_credit_start_date")
    if start_date_str:
        try:
            start_epoch = int(_dt.datetime.fromisoformat(str(start_date_str).strip()).timestamp())
            if start_epoch <= now:
                start = start_epoch
        except ValueError:
            pass

    admin_key = mode.get("admin_key") or ""
    if admin_key:
        try:
            spent = admin_cost_usd(admin_key, start, now + 86400)
            result.update(source="admin", spent=spent,
                          remaining=max(0.0, total - spent))
            return result
        except (OSError, ValueError, RuntimeError):
            pass

    spent = base_spent + _local_api_spend(cfg, mode, start)
    result.update(source="estimate", spent=spent,
                  remaining=max(0.0, total - spent))
    return result
