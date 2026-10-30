"""Read the organization-wide Console balance with an app-owned web login.

WebView2 owns the login and its encrypted browser profile. Python only receives
the whitelisted billing response, never browser cookies or session tokens.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

from . import config

CONNECTION_FILE = "claude-console-connection.json"
BALANCE_FILE = "claude-console-balance.json"


def _read(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _org_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return ""


def _minor(value, exponent=2):
    if isinstance(value, bool) or isinstance(exponent, bool):
        raise ValueError("invalid balance")
    if not isinstance(exponent, int) or not 0 <= exponent <= 4:
        raise ValueError("invalid balance exponent")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid balance") from None
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError("invalid balance")
    result = float(number / (10 ** exponent))
    if abs(result) > 1e12:
        raise ValueError("invalid balance")
    return result


def parse_balance(payload):
    """Use the top-level balance, never a rounded display or one grant."""
    if not isinstance(payload, dict) or payload.get("currency", "USD") != "USD":
        raise ValueError("unsupported balance")
    balance = payload.get("balance") or {}
    credits = balance.get("credits") if isinstance(balance, dict) else None
    if isinstance(credits, dict) and "amount_minor" in credits:
        remaining = _minor(credits["amount_minor"], credits.get("exponent", 2))
    elif "amount" in payload:
        remaining = _minor(payload["amount"])
    else:
        raise ValueError("missing balance")
    # Grant details are optional and never replace the authoritative total.
    total = 0.0
    found_grant = False
    for grant in (payload.get("tranches") or []) + (payload.get("promo_tranches") or []):
        if not isinstance(grant, dict):
            continue
        granted = (grant.get("granted") or {}).get("credits") or {}
        try:
            total += (_minor(granted["amount_minor"], granted.get("exponent", 2))
                      if "amount_minor" in granted
                      else _minor(grant["granted_amount_minor_units"]))
            found_grant = True
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    expiry = payload.get("next_expires_at")
    if expiry:
        try:
            dt.datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
        except ValueError:
            expiry = None
    return {"remaining": remaining, "total": total if found_grant else None,
            "expiry": str(expiry) if expiry else None, "currency": "USD"}


def _host_script():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return str(root / "ui" / "console-credit.ps1")


def _run_host(org_id, connect=False):
    config.ensure_home()
    fd, path = tempfile.mkstemp(prefix="console-response-", suffix=".json", dir=config.APP_HOME)
    os.close(fd)
    args = ["powershell.exe", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass"]
    if not connect:
        args += ["-WindowStyle", "Hidden"]
    args += ["-File", _host_script(),
            "-AppHome", config.APP_HOME, "-OutputPath", path]
    if org_id:
        args += ["-OrgId", org_id]
    if connect:
        args += ["-Connect"]
    native_host = str(Path(_host_script()).with_name("console-credit-host.exe"))
    if os.path.isfile(native_host):
        args = [native_host, "-AppHome", config.APP_HOME, "-OutputPath", path]
        if org_id:
            args += ["-OrgId", org_id]
        if connect:
            args += ["-Connect"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_")}
    try:
        subprocess.run(args, capture_output=True, timeout=900 if connect else 35,
                       creationflags=0x08000000 if os.name == "nt" else 0, env=env,
                       check=False)
        return _read(path) or {"status": "unavailable", "error": "host_error"}
    except subprocess.TimeoutExpired:
        return {"status": "unavailable", "error": "timeout"}
    except OSError:
        return {"status": "unavailable", "error": "host_error"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def connection(cfg):
    saved = _read(os.path.join(config.APP_HOME, CONNECTION_FILE))
    return _org_id(cfg.get("claude_console_org_id") or saved.get("org_id"))


def connect(cfg, now):
    response = _run_host(connection(cfg), connect=True)
    if response.get("status") != "ok":
        return False
    # The UI helper validates API capability and saves only non-secret org metadata.
    return collect(cfg, now, response=response).get("status") == "ok"


ERRORS = {
    "login_required": "Claude Console 로그인 필요 · 트레이에서 Claude Console 연결",
    "auth": "Claude Console 세션 만료 · 다시 연결 필요",
    "forbidden": "선택한 조직의 잔액 조회 권한 없음",
    "no_api_org": "이 계정에 Claude API 조직이 없음",
    "org_mismatch": "연결된 계정에 선택한 API 조직이 없음",
    "timeout": "Claude Console 응답 시간 초과",
    "busy": "Claude Console 연결 진행 중",
    "rate_limit": "Claude Console 요청 제한 · 잠시 후 재조회",
    "network": "Claude Console 네트워크 조회 실패",
    "invalid": "Claude Console 잔액 응답 형식 변경",
    "host_error": "Claude Console 조회 시작 실패",
    "cancelled": "Claude Console 연결 취소",
}


def collect(cfg, now, response=None):
    """No local estimate or manual anchor may replace a failed web read."""
    org = connection(cfg)
    path = os.path.join(config.APP_HOME, BALANCE_FILE)
    previous = _read(path)
    if previous.get("organization_id") != org:
        previous = {}
    base = {"source": "console", "status": "unavailable", "has_credits": True,
            "remaining": None, "spent": None, "total": None, "expiry": None,
            "currency": "USD", "measured_at": None, "organization_id": org}
    if response is None and previous.get("next_refresh_at", 0) > now:
        cached = dict(base, **{k: v for k, v in previous.items() if k != "next_refresh_at"})
        measured = cached.get("measured_at")
        if measured is not None and not 0 <= now - measured <= int(cfg.get("stale_max_age_sec") or 86400):
            cached.update(remaining=None, measured_at=None, status="unavailable")
        return cached
    if response is None:
        response = _run_host(org) if org else {"error": "login_required"}
    error = response.get("error") or "invalid"
    if response.get("status") == "ok" and _org_id(response.get("org_id")) == org and org:
        try:
            parsed = parse_balance(response.get("payload"))
            measured = int(response.get("received_at") or now)
            metadata = _read(os.path.join(config.APP_HOME, CONNECTION_FILE))
            name = response.get("org_name") or (metadata.get("org_name") if metadata.get("org_id") == org else "")
            result = dict(base, **parsed, status="ok", measured_at=measured,
                          organization_name=str(name or ""), error="")
            config.write_atomic(path, json.dumps(dict(result, next_refresh_at=now + 60), ensure_ascii=False))
            return result
        except (ValueError, TypeError, OverflowError):
            error = "invalid"
    elif response.get("status") == "ok":
        error = "org_mismatch"
    error = error if error in ERRORS else "network"
    result = dict(base, error=ERRORS[error])
    measured = previous.get("measured_at")
    if isinstance(measured, (int, float)) and 0 <= now - measured <= int(cfg.get("stale_max_age_sec") or 86400):
        for key in ("remaining", "total", "expiry", "measured_at", "organization_name"):
            result[key] = previous.get(key)
        result["status"] = "stale"
    # Auth failures need an explicit reconnect; retries stay quiet and bounded.
    delay = 300 if error in ("auth", "forbidden", "rate_limit") else 60
    config.write_atomic(path, json.dumps(dict(result, next_refresh_at=now + delay), ensure_ascii=False))
    return result
