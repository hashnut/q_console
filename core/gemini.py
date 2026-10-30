"""Read Gemini quota through Antigravity's non-generating /usage command.

The signed-in CLI owns authentication. q_console never copies its credentials.
Only Gemini groups are exposed; Antigravity's Claude/GPT quota is unrelated to
the Claude Code and Codex accounts shown by the other providers.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from . import config
from .plan_usage import _iso_epoch


def extract_usage(report: dict) -> list[dict]:
    command = report.get("command") or {}
    if (not isinstance(command, dict) or report.get("status") != "SUCCESS"
            or command.get("name") != "usage" or report.get("num_turns", 0) != 0):
        raise ValueError("사용량 명령 실패 · Antigravity 로그인을 확인하세요")
    data = command.get("data")
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise ValueError("Gemini 한도 응답 없음")
    limits = []
    for group in groups:
        if not isinstance(group, dict) or "gemini" not in str(group.get("name", "")).lower():
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            fraction = bucket.get("remaining_fraction")
            if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
                continue
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                continue
            key = str(bucket.get("id") or "")
            if not key:
                continue
            window = bucket.get("window")
            label = "5시간 사용량" if window == "5h" else "주간 사용량" if window == "weekly" else "사용량"
            if key != "gemini-weekly" and key != "gemini-5h":
                label = "%s · %s" % (group["name"], label)
            limits.append({"window_key": key, "window_label": label,
                           "used": round((1 - fraction) * 100, 6),
                           "resets_at": _iso_epoch(bucket.get("reset_time"))})
    if not limits:
        raise ValueError("Gemini 계정 한도 미제공")
    # Shared Gemini quotas come first, followed by any separate model allowances.
    order = {"gemini-weekly": 0, "gemini-5h": 1}
    return sorted(limits, key=lambda value: order.get(value["window_key"], 2))


@contextmanager
def _hidden_desktop():
    """Keep even an unexpected CLI sign-in window off the user's desktop."""
    if os.name != "nt":
        yield None
        return
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.CreateDesktopW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                   ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    user.CreateDesktopW.restype = ctypes.c_void_p
    user.CloseDesktop.argtypes = [ctypes.c_void_p]
    name = "q_console_gemini_" + uuid.uuid4().hex
    desktop = user.CreateDesktopW(name, None, None, 0, 0x10000000, None)
    if not desktop:
        raise OSError("Gemini 백그라운드 데스크톱 생성 실패")
    startup = subprocess.STARTUPINFO()
    startup.lpDesktop = "WinSta0\\" + name
    try:
        yield startup
    finally:
        user.CloseDesktop(desktop)


def _run_usage(executable: str) -> dict:
    env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    version = subprocess.run([executable, "--version"], capture_output=True, timeout=3,
                             env=env, stdin=subprocess.DEVNULL, check=False,
                             creationflags=0x08000000 if os.name == "nt" else 0)
    match = re.fullmatch(rb"v?(\d+)\.(\d+)\.(\d+)\s*", version.stdout)
    if version.returncode or not match or tuple(map(int, match.groups())) < (1, 1, 11):
        raise RuntimeError("Antigravity CLI 1.1.11 이상으로 업데이트하세요")
    with tempfile.TemporaryDirectory(prefix="q-console-gemini-") as directory, _hidden_desktop() as startup:
        # Antigravity's own /usage report makes zero model turns. Its JSON
        # command payload is authoritative; human response text is not parsed.
        result = subprocess.run(
            [executable, "-p", "/usage", "--output-format", "json", "--print-timeout", "20s"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=directory, env=env, startupinfo=startup, timeout=30,
            creationflags=0x08000000 if os.name == "nt" else 0, check=False)
    if result.returncode:
        raise RuntimeError("Antigravity 조회 실패 · agy에서 로그인 상태를 확인하세요")
    if len(result.stdout) > 1024 * 1024:
        raise ValueError("Gemini 응답 크기 초과")
    report = json.loads(result.stdout)
    if not isinstance(report, dict):
        raise ValueError("Gemini 응답 형식 오류")
    return report


def collect(cfg: dict) -> dict:
    executable = config.expand(cfg.get("gemini_cli_path") or "~/.gemini/bin/agy.exe")
    try:
        if not Path(executable).is_file():
            raise RuntimeError("Antigravity CLI 미설치 · agy 설치와 로그인이 필요합니다")
        limits = extract_usage(_run_usage(executable))
        note = "Antigravity /usage 계정 실측 · " + " · ".join(
            "%s %.1f%% 사용" % (value["window_label"], value["used"]) for value in limits)
        return {"status": "ok", "note": note, "limits": limits, "source": "antigravity-cli"}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # A report does not identify its account. Never carry a previous CLI
        # account's quota into an unverified login or changed account.
        return {"status": "unavailable", "source": "antigravity-cli", "limits": [],
                "note": "Gemini 조회 실패 · Antigravity CLI(agy)의 설치·로그인·네트워크를 확인하세요"}
