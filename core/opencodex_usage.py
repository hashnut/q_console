"""Read Claude quota through OpenCodex, leaving OAuth refresh to its owner.

The official CLI uses the running proxy's account-scoped quota cache and refresh
coordination. Only account metadata is read locally to fence account switches.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess

from . import config

MAX_AGE_SEC = 120


def _identity(home: Path) -> tuple[str, str | None]:
    with (home / "auth.json").open(encoding="utf-8") as handle:
        data = json.load(handle)
    accounts = (data.get("anthropic") or {})
    selected = accounts.get("activeAccountId")
    row = next((a for a in accounts.get("accounts", []) if a.get("id") == selected), None)
    if not row or row.get("paused") or row.get("needsReauth"):
        raise ValueError("OpenCodex 구독 로그인 확인 필요")
    credential = row.get("credential") or {}
    # Deliberately exclude access/refresh tokens: login and rotation belong to ocx.
    scope = json.dumps([str(home), selected, accounts.get("selectionRevision"),
                        row.get("loginId"), credential.get("accountId")])
    return hashlib.sha256(scope.encode()).hexdigest(), credential.get("plan")


def _cli_entry(cfg: dict) -> str:
    if cfg.get("opencodex_cli_file"):
        return config.expand(cfg["opencodex_cli_file"])
    shim = shutil.which("ocx.cmd") or shutil.which("ocx")
    prefixes = [Path(shim).parent] if shim else []
    if os.environ.get("APPDATA"):
        prefixes.append(Path(os.environ["APPDATA"]) / "npm")
    if os.environ.get("LOCALAPPDATA"):
        prefixes.append(Path(os.environ["LOCALAPPDATA"]) / "WhyDuck/OpenCodexSetup/npm")
    for prefix in prefixes:
        candidate = prefix / "node_modules/@bitkyc08/opencodex/bin/ocx.mjs"
        if candidate.is_file():
            return str(candidate)
    raise OSError("OpenCodex CLI 미설치")


def _quota(cfg: dict, home: Path) -> dict:
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        raise OSError("OpenCodex용 Node 실행 파일 없음")
    env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    env["OPENCODEX_HOME"] = str(home)
    result = subprocess.run([node, _cli_entry(cfg), "provider", "quota", "--json"],
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=20, check=False,
                            creationflags=0x08000000 if os.name == "nt" else 0)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise RuntimeError("OpenCodex quota 조회 실패")
    return json.loads(result.stdout)


def _epoch(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return int(value / 1000 if value > 100_000_000_000 else value) if value > 0 else None


def _limit(percent, reset, now):
    reset = _epoch(reset)
    used = (float(percent) if isinstance(percent, (int, float)) and not isinstance(percent, bool)
            and math.isfinite(percent) and 0 <= percent <= 100 else None)
    if reset is not None and reset <= now:
        used, reset = None, None
    return {"used": used, "resets_at": reset}


def extract(payload: dict, now: int) -> dict:
    reports = payload.get("reports")
    if not isinstance(reports, list):
        raise ValueError("OpenCodex quota 응답 형식 오류")
    report = next((x for x in reports if isinstance(x, dict) and x.get("provider") == "anthropic"
                   and x.get("source") == "anthropic:oauth-usage"), None)
    if not report or not isinstance(report.get("quota"), dict) or report.get("aggregation"):
        raise ValueError("OpenCodex 활성 구독 Usage 미제공")
    quota = report["quota"]
    measured_at = _epoch(quota.get("updatedAt"))
    if measured_at is None or not -30 <= now - measured_at <= MAX_AGE_SEC:
        raise ValueError("OpenCodex 최신 Usage 미수신")
    week = _limit(quota.get("weeklyPercent"), quota.get("weeklyResetAt"), now)
    session = _limit(quota.get("fiveHourPercent"), quota.get("fiveHourResetAt"), now)
    windows = quota.get("customWindows") or []
    if not isinstance(windows, list):
        raise ValueError("OpenCodex 모델 한도 형식 오류")
    fable_row = next((x for x in windows if isinstance(x, dict) and x.get("scope") == "model"
                      and str(x.get("label", "")).casefold() == "fable"), {})
    fable = _limit(fable_row.get("percent"), fable_row.get("resetAt"), now)
    if week["used"] is None and session["used"] is None:
        raise ValueError("OpenCodex 구독 사용률 미제공")
    return {"all_models": week, "session": session, "fable": fable, "measured_at": measured_at}


def collect(cfg: dict, now: int) -> dict:
    home = Path(config.expand(cfg.get("opencodex_home") or "~/.opencodex"))
    empty = {"used": None, "resets_at": None}
    base = {"source": "opencodex", "allow_carry": False, "reset_credits": None}
    try:
        before = _identity(home)
        value = extract(_quota(cfg, home), now)
        if _identity(home) != before:
            raise ValueError("OpenCodex 조회 중 계정 전환")
        return {**base, **value, "status": "ok", "plan": before[1],
                "usage_scope": before[0], "note": "OpenCodex 활성 Claude 구독 · 계정 Usage 실측"}
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError):
        return {**base, "status": "unavailable", "all_models": dict(empty), "session": dict(empty),
                "fable": dict(empty), "note": "OpenCodex 최신 Claude Usage 조회 실패 · 구독 로그인·중계기 상태 확인 필요"}
