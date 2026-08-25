"""First-run install of the one thing q_console cannot ship itself.

The detail window is a WebView2 surface. Windows 11 carries the Evergreen
WebView2 Runtime in-box, but a copied folder on an older or stripped-down
machine may not have it, and today that turns the dashboard into an
"install this yourself" screen. This module removes that step: on first launch,
if the runtime is genuinely missing, fetch Microsoft's Evergreen bootstrapper
and run it silently.

Rules this follows, because it downloads and executes an installer:

* ONLY when the runtime is actually absent. A present runtime is never touched
  or upgraded - Microsoft's own updater owns that.
* ONCE. The attempt is recorded before the download starts, so a crash, a dead
  network, or a user who declines cannot turn every launch into another
  download. ``--install-webview2`` is the deliberate retry.
* VERIFIED before execution. The download must come from a Microsoft host over
  HTTPS, be a PE image, and carry a valid Authenticode signature naming
  Microsoft. Anything else is deleted unrun.
* UNPRIVILEGED. Run non-elevated the bootstrapper installs per-user, so this
  never raises a UAC prompt on a background launch.
* NON-BLOCKING. The tray, the icon and the hover summary all work without
  WebView2, so the install runs on a background thread and the app starts now.

Documented contract (learn.microsoft.com/microsoft-edge/webview2/concepts/
distribution): detect via the `pv` value under the EdgeUpdate client GUID;
install with `MicrosoftEdgeWebview2Setup.exe /silent /install`.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request

from . import config

# "Get the Link" from the WebView2 download page. Resolves to
# MicrosoftEdgeWebview2Setup.exe on Microsoft's delivery CDN.
BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
RUNTIME_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
MARKER_PATH = os.path.join(config.APP_HOME, "webview2-install.json")

DOWNLOAD_TIMEOUT_SEC = 60
INSTALL_TIMEOUT_SEC = 600          # the bootstrapper pulls ~150 MB
MAX_BYTES = 32 * 1024 * 1024       # the bootstrapper is ~2 MB; cap the write
ALLOWED_HOST_SUFFIXES = (
    ".microsoft.com", ".windowsupdate.com", ".msedge.net", ".azureedge.net",
)


# ── detection ───────────────────────────────────────────────────────────────

def runtime_version():
    """Installed Evergreen runtime version, or None.

    Mirrors webview2-host.ps1's check so the tray and this installer can never
    disagree about whether the runtime is there.
    """
    try:
        import winreg
    except ImportError:      # not Windows; nothing to install
        return None
    keys = (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % RUNTIME_GUID),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % RUNTIME_GUID),
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\EdgeUpdate\Clients\%s" % RUNTIME_GUID),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as handle:
                value = winreg.QueryValueEx(handle, "pv")[0]
        except OSError:
            continue
        value = str(value or "").strip()
        if value and value != "0.0.0.0":
            return value
    return None


# ── the once-only marker ────────────────────────────────────────────────────

def read_marker() -> dict:
    try:
        with open(MARKER_PATH, encoding="utf-8") as handle:
            blob = json.load(handle)
        return blob if isinstance(blob, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_marker(**fields) -> dict:
    marker = read_marker()
    marker.update(fields)
    marker["at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        config.write_atomic(MARKER_PATH, json.dumps(marker, indent=1,
                                                    ensure_ascii=False))
    except OSError:
        pass
    return marker


# ── download + verify ───────────────────────────────────────────────────────

def _host_allowed(url: str) -> bool:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in ALLOWED_HOST_SUFFIXES)


def _download(url: str, target: str) -> str:
    """-> the final URL, after redirects. Raises RuntimeError on anything odd."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "q_console/1.0 (WebView2 bootstrap)"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SEC) as response:
            final = response.url
            if not _host_allowed(final):
                raise RuntimeError("예상 밖 배포 호스트: %s" % final)
            payload = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %d" % exc.code) from None
    except urllib.error.URLError as exc:
        raise RuntimeError("네트워크 실패: %s" % exc.reason) from None
    if len(payload) > MAX_BYTES:
        raise RuntimeError("내려받은 파일이 너무 큼")
    if payload[:2] != b"MZ":
        raise RuntimeError("실행 파일이 아님")
    with open(target, "wb") as handle:
        handle.write(payload)
    return final


def _authenticode_ok(path: str) -> bool:
    """True only for a valid signature naming Microsoft.

    A downloaded installer is executed here, so an unsigned or third-party
    signed file is treated as a failed install, not a warning.
    """
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath %s;"
        "if ($s.Status -ne 'Valid') { exit 1 };"
        "if ($s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation')"
        " { exit 2 }; exit 0" % _ps_quote(path)
    )
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _ps_quote(text: str) -> str:
    return "'%s'" % str(text).replace("'", "''")


# ── the install ─────────────────────────────────────────────────────────────

def ensure_webview2(force: bool = False) -> dict:
    """Install the WebView2 runtime if it is missing. -> {"status", "detail"}

    status is one of: present, installed, failed, skipped, unsupported.
    """
    if os.name != "nt":
        return {"status": "unsupported", "detail": "Windows 전용"}

    version = runtime_version()
    if version:
        return {"status": "present", "detail": version}

    marker = read_marker()
    if marker.get("attempted") and not force:
        return {"status": "skipped",
                "detail": "이미 %s에 시도함 (%s) · 다시 시도하려면 "
                          "--install-webview2" % (marker.get("at"),
                                                  marker.get("result"))}

    config.ensure_home()
    _write_marker(attempted=True, result="시작", version=None)
    setup = os.path.join(config.APP_HOME, "MicrosoftEdgeWebview2Setup.exe")
    try:
        final = _download(BOOTSTRAPPER_URL, setup)
        if not _authenticode_ok(setup):
            raise RuntimeError("Microsoft 서명 검증 실패")
        # Non-elevated on purpose: that makes it a per-user install, so a
        # background launch never pops UAC at someone who did not click it.
        done = subprocess.run(
            [setup, "/silent", "/install"], capture_output=True,
            timeout=INSTALL_TIMEOUT_SEC,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        version = runtime_version()
        if version:
            _write_marker(result="설치 완료", version=version, source=final)
            return {"status": "installed", "detail": version}
        raise RuntimeError("설치 후에도 런타임이 없음 (exit %d)" % done.returncode)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        _write_marker(result="실패: %s" % exc)
        return {"status": "failed", "detail": str(exc)}
    finally:
        try:
            os.remove(setup)
        except OSError:
            pass


def ensure_webview2_async() -> None:
    """Run the check off the startup path.

    The tray icon, the hover summary and every percentage work without
    WebView2, so making the user wait on a ~150 MB download before the app
    appears would trade a working tray for a spinner.
    """
    if os.name != "nt" or runtime_version():
        return
    if read_marker().get("attempted"):
        return
    thread = threading.Thread(target=ensure_webview2, name="webview2-bootstrap",
                              daemon=True)
    thread.start()
