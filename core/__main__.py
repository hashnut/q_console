"""q_console - local usage tracker for Claude Code + Codex.

This is the ORCHESTRATOR half of the original app: it produces the two files the
tray reads (usage-cache.json, detail.html) and it starts the tray. The tray
itself, the WebView2 detail host and the icon painter are the shipped
PowerShell scripts under usage-view/_internal/tray - untouched, so the original
gjc-backed exe still works exactly as before if you ever want it.

Worker CLI (this is the contract tray.ps1 expects):
    --refresh                 rebuild the cache + detail.html, print the report
    --set-theme <id>          persist theme, re-render (surfacer|phosphor|mini)
    --set-always-on-top on|off
    --refresh-worker          accepted and ignored (the frozen build passed it)

Human CLI:
    (no args) / --tray        start the tray
    --open                    start the tray with the window already open
    --print                   text report to stdout, no files touched
    --install-autostart / --uninstall-autostart
    --install-startmenu / --uninstall-startmenu
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

if __package__ in (None, ""):  # allow `python core/__main__.py`
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "core"

from core import config, render, snapshot  # noqa: E402

FROZEN = bool(getattr(sys, "frozen", False))
BUNDLE_ROOT = (getattr(sys, "_MEIPASS", None) or
               os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(sys.executable) if FROZEN else BUNDLE_ROOT
# ui/ is our fork of the shipped tray (resizable window, overlay mode, new
# icon); the original under usage-view/ stays untouched as the gjc fallback.
SCRIPT = os.path.abspath(__file__)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "q_console"


# ── plumbing ────────────────────────────────────────────────────────────────

def short_path(path: str) -> str:
    """8.3 form when a path has spaces.

    tray.ps1 splits -WorkerArguments on spaces, so the worker command it builds
    must not contain any. Every path here is space-free today; this keeps that
    true if the folder is ever moved under e.g. 'Program Files'.
    """
    if " " not in path:
        return path
    try:
        import ctypes
        from ctypes import wintypes
        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        if get_short(path, buf, 1024):
            return buf.value
    except Exception:
        pass
    return path


def pythonw() -> str:
    """Windowless interpreter, so a background refresh flashes no console."""
    exe = sys.executable or "python.exe"
    candidate = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else exe


def prepare_tray_assets() -> str:
    """Return a persistent tray.ps1 path for source and one-file EXE runs.

    PyInstaller removes its temporary extraction directory when q_console.exe
    exits, while the PowerShell tray keeps running. Copy the small UI runtime
    into AppHome first so scripts and WebView2 assemblies remain available.
    """
    source = os.path.join(BUNDLE_ROOT, "ui")
    if not FROZEN:
        return os.path.join(source, "tray.ps1")
    target = os.path.join(config.ensure_home(), "runtime", "ui")
    os.makedirs(target, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    return os.path.join(target, "tray.ps1")


def worker_executable() -> str:
    return sys.executable if FROZEN else pythonw()


def worker_arguments() -> str:
    return "--refresh-worker" if FROZEN else short_path(SCRIPT)


def detached_child_environment() -> dict:
    """Environment for non-PyInstaller children that outlive this process.

    A one-file executable marks its parent/child bootloader relationship with
    private ``_PYI_*`` variables. The persistent PowerShell tray must not keep
    those markers: it later launches a brand-new q_console.exe worker, whose
    bootloader would otherwise mistake PowerShell for its own one-file parent
    and show a security-validation error dialog.
    """
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("_PYI_"):
            env.pop(name, None)
    return env


def emit(text) -> None:
    """Write CLI output only when this process has an attached/redirected stream.

    A windowed PyInstaller process has no stdout when started by double-click.
    Treating that as a normal GUI launch keeps a second launch able to replace
    the existing tray instead of failing on an informational print.
    """
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return
    try:
        print(text, file=stream)
    except (OSError, AttributeError, ValueError):
        pass


def write_outputs(snap: dict, cfg: dict) -> None:
    config.write_atomic(config.CACHE_PATH,
                        json.dumps(snap, ensure_ascii=False, indent=1))
    config.write_atomic(config.DETAIL_PATH, render.render(snap, cfg["theme"]))
    config.write_atomic(config.OVERLAY_PATH, render.render_overlay(snap))


def refresh(cfg=None) -> dict:
    cfg = cfg or config.load()
    if not os.path.isfile(config.CONFIG_PATH):
        # The tray reads the theme straight out of this file. Seed it once -
        # never on every refresh, or a refresh could stomp a concurrent
        # theme switch with the value it loaded before the click.
        config.save(cfg)
    snap = snapshot.build(cfg)
    write_outputs(snap, cfg)
    return snap


def load_cached_snapshot():
    try:
        with open(config.CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ── tray ────────────────────────────────────────────────────────────────────

def stop_existing_tray() -> int:
    """Kill any running tray (ours or the original gjc one) so a fresh launch
    always wins.

    Both trays guard with the same named mutex, so without this a second launch
    just exits silently and looks like "nothing happened" - which is exactly
    what the user reported. The WebView2 browser children on OUR user-data
    folder are killed too: they hold a lock on that folder, and a new
    CoreWebView2 environment cannot start while a stale one lingers.
    """
    script = (
        "$n = 0;"
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" |"
        " Where-Object { $_.CommandLine -match 'tray\\.ps1' } |"
        " ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force; $n++ } catch {} };"
        "Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" |"
        " Where-Object { $_.CommandLine -match 'q_console.webview2' } |"
        " ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} };"
        "$n"
    )
    creation = 0x08000000 if os.name == "nt" else 0
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20, creationflags=creation)
        killed = int((out.stdout or "0").strip().splitlines()[-1] or 0)
    except (subprocess.SubprocessError, ValueError, OSError, IndexError):
        return 0
    if killed:
        import time
        time.sleep(1.0)   # let the mutex and the user-data-folder lock go
    return killed


def start_tray(open_detail: bool = False) -> int:
    killed = stop_existing_tray()
    if killed:
        emit("기존 트레이 %d개 종료, 새로 시작" % killed)
    tray_ps1 = prepare_tray_assets()
    if not os.path.isfile(tray_ps1):
        emit("tray.ps1 없음: %s" % tray_ps1)
        return 2
    config.ensure_home()
    cfg = config.load()
    config.save(cfg)          # make sure the tray can read a theme immediately
    try:
        refresh(cfg)          # first paint has real data, not an empty window
    except Exception as exc:  # a bad refresh must not block the tray
        emit("초기 refresh 실패: %r" % (exc,))

    args = [
        "powershell.exe", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden", "-File", tray_ps1,
        "-WorkerExecutable", worker_executable(),
        "-WorkerArguments", worker_arguments(),
        "-AppHome", short_path(config.APP_HOME),
    ]
    if open_detail:
        args.append("-OpenDetail")
    creation = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    subprocess.Popen(args, creationflags=creation, close_fds=True,
                     env=detached_child_environment())
    return 0


# ── install helpers ─────────────────────────────────────────────────────────

def _run_value() -> str:
    if FROZEN:
        return '"%s" --tray' % sys.executable
    return '"%s" "%s" --tray' % (pythonw(), SCRIPT)


def install_autostart(remove: bool = False) -> int:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
        if remove:
            try:
                winreg.DeleteValue(key, RUN_NAME)
                emit("자동 시작 해제됨")
            except FileNotFoundError:
                emit("자동 시작 항목이 없음")
            return 0
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, _run_value())
    emit("로그인 시 자동 시작 등록됨:\n  %s" % _run_value())
    return 0


def _startmenu_path() -> str:
    base = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs")
    return os.path.join(base, "q_console.lnk")


def install_startmenu(remove: bool = False) -> int:
    link = _startmenu_path()
    if remove:
        try:
            os.remove(link)
            emit("시작 메뉴에서 제거됨")
        except OSError:
            emit("시작 메뉴 항목이 없음")
        return 0
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if FROZEN:
        target = sys.executable
        arguments = "--tray"
        working = os.path.dirname(sys.executable)
        icon = sys.executable
    else:
        target = pythonw()
        arguments = '"%s" --tray' % SCRIPT
        working = ROOT
        icon = os.path.join(ROOT, "ui", "q_console.ico")
    ps = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
        "$s.TargetPath='%s';$s.Arguments='%s';"
        "$s.WorkingDirectory='%s';$s.IconLocation='%s';$s.Description="
        "'q_console - Claude Code / Fable / Codex 계정 사용률';$s.Save()"
        % (link, target, arguments, working, icon)
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-Command", ps], check=False)
    emit("시작 메뉴 등록됨: %s" % link)
    return 0


# ── entry ───────────────────────────────────────────────────────────────────

def main(argv) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    refresh_worker = "--refresh-worker" in argv
    args = [a for a in argv if a != "--refresh-worker"]

    if "--set-theme" in args:
        index = args.index("--set-theme")
        name = args[index + 1] if index + 1 < len(args) else "surfacer"
        cfg = config.update(theme=config.THEME_ALIASES.get(name, "surfacer"))
        snap = load_cached_snapshot()
        if snap:                       # a theme switch must never re-scan
            config.write_atomic(config.DETAIL_PATH, render.render(snap, cfg["theme"]))
        else:
            refresh(cfg)
        emit("theme=%s" % cfg["theme"])
        return 0

    if "--set-always-on-top" in args:
        index = args.index("--set-always-on-top")
        flag = (args[index + 1] if index + 1 < len(args) else "off").lower()
        config.update(always_on_top=flag in ("on", "1", "true", "yes"))
        return 0

    if "--set-overlay" in args:
        index = args.index("--set-overlay")
        flag = (args[index + 1] if index + 1 < len(args) else "off").lower()
        config.update(overlay_mode=flag in ("on", "1", "true", "yes"))
        return 0

    if "--print" in args:
        emit(snapshot.build(config.load())["detail_text"])
        return 0

    if "--refresh" in args:
        emit(refresh()["detail_text"])
        return 0

    if "--install-autostart" in args:
        return install_autostart()
    if "--uninstall-autostart" in args:
        return install_autostart(remove=True)
    if "--install-startmenu" in args:
        return install_startmenu()
    if "--uninstall-startmenu" in args:
        return install_startmenu(remove=True)

    if "--help" in args or "-h" in args:
        emit(__doc__)
        return 0

    if refresh_worker and not args:
        refresh()
        return 0

    return start_tray(open_detail="--open" in args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
