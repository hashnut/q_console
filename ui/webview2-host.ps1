# WebView2 display host for the q_console detail window (D-52).
#
# Why this file exists: D-44 replaced the six HTML layouts with one common
# WinForms card board, which lost the original 1080x640 designs (D-47/D-48).
# D-52 settled the replacement path with measurements rather than taste:
#
#   - Embedding WebView2 inside a PowerShell STA WinForms form works on this
#     runtime (151.0.4129.86) and renders both designs byte-identically at
#     1080x640 with no scrollbars.
#   - Rebuilding the designs in pure GDI+ does not: there is no letter-spacing
#     API (the two designs use it 22 times), no mix-blend-mode, no ::before/
#     ::after, no grid auto-placement, and the surfacer display face ships as an
#     embedded base64 @font-face that GDI+ cannot use.
#   - Cost of embedding: 0.81 MB of redistributable DLLs. Evergreen mode, so the
#     browser itself is NOT shipped.
#
# The one real drawback is cold start (4.6 s on a fresh user-data folder), so
# this host PREWARMS: the form is created off-screen during tray startup and the
# user only ever pays the reposition (69 ms measured). Never create the control
# lazily on click; that reintroduces the 4.6 s stall this design removed.
#
# This file owns ONLY presentation. It never refreshes data, never spawns the
# worker, and never writes config: a theme switch changes an in-memory model and
# a file name, exactly as D-49 requires.

$script:WV2Ready = $false        # CoreWebView2 finished initialising
$script:WV2Form = $null
$script:WV2Control = $null
$script:WV2Failure = $null       # non-null => we must show the fallback UI
$script:WV2LastHtml = $null
# Explicit open/closed state. NEVER infer it from Location/WindowState: a
# MINIMIZED WinForms form reports Location -32000,-32000 and Size 237x39, which
# is pixel-identical to the off-screen prewarm parking spot. Inferring from
# geometry made a minimized window read as "not open", so the refresh cycle
# skipped re-rendering it and the user saw frozen numbers after restoring it.
$script:WV2Open = $false
# Set when a refresh landed while the surface was not composited (minimized or
# parked off-screen). WebView2 does not produce frames for such a window - a
# Reload() there resolves the DOM but paints nothing, and the restored window came
# back BLANK (measured: CapturePreviewAsync times out while minimized). So we defer
# the render and replay it the moment the window is actually shown.
$script:WV2Dirty = $false
# Overlay mode: borderless + topmost + translucent, parked in a screen corner.
# Persisted by the worker (config.json overlay_mode); the tray applies it here.
$script:WV2Overlay = $false
# Remembered normal-window geometry so leaving overlay mode puts the window back
# where the user had it instead of wherever the overlay was parked.
$script:WV2SavedBounds = $null
# The document's intrinsic size (data-w/data-h). Auto-resize happens only when
# the DOCUMENT size changes (theme switch), never on a refresh of the same
# layout - a Sizable window belongs to the user once they have dragged it.
$script:WV2DocSize = $null

# ── runtime detection ───────────────────────────────────────────────────────
# Microsoft's documented check: the pv value under either EdgeUpdate client key.
# Absent / empty / 0.0.0.0 all mean "not installed". Windows 11 ships it in-box,
# but a copied folder on another PC (AC-12) may not have it, so we degrade to an
# explanatory screen instead of crashing.
function Get-WebView2RuntimeVersion {
    $guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    $keys = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$guid",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$guid",
        "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$guid"
    )
    foreach ($k in $keys) {
        try {
            $pv = (Get-ItemProperty -Path $k -ErrorAction Stop).pv
            if ($pv -and $pv -ne '0.0.0.0') { return [string]$pv }
        } catch { }
    }
    return $null
}

function Get-WebView2SdkDir {
    return (Join-Path $PSScriptRoot 'webview2')
}

# ── window chrome ───────────────────────────────────────────────────────────
# The title bar is drawn by DWM, not by WinForms, so BackColor never reached it
# and the window wore the system accent (bright blue) around a black dashboard.
# Windows 11 exposes the caption/border/text colours as DWM attributes:
#   34 DWMWA_BORDER_COLOR   35 DWMWA_CAPTION_COLOR   36 DWMWA_TEXT_COLOR
#   20 DWMWA_USE_IMMERSIVE_DARK_MODE (older, wider support)
# Measured on build 26100: all four return S_OK and the caption renders black.
# Every call is best-effort - on an OS without these attributes the call fails
# harmlessly and the window keeps the system chrome rather than breaking.
Add-Type -Namespace UV -Name Chrome -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("dwmapi.dll")]
public static extern int DwmSetWindowAttribute(System.IntPtr hwnd, int attr, ref int val, int size);
'@ -ErrorAction SilentlyContinue

# WebMessageReceived can arrive outside the WinForms UI runspace. Queue the
# pointer events in a tiny CLR bridge; a UI-thread timer drains them and is the
# only code that ever touches Form.Location.
Add-Type -Namespace UV -Name DragBridge -MemberDefinition @'
private static readonly System.Collections.Concurrent.ConcurrentQueue<string> events =
    new System.Collections.Concurrent.ConcurrentQueue<string>();
public static void Push(string value) { events.Enqueue(value); }
public static string Pop() { string value; return events.TryDequeue(out value) ? value : null; }
'@ -ErrorAction SilentlyContinue

# Alt-Tab lists every unowned top-level window regardless of ShowInTaskbar.
# The only thing that removes a window from that list is WS_EX_TOOLWINDOW
# (and NOT WS_EX_APPWINDOW), so the overlay strip sets it on its own handle.
Add-Type -Namespace UV -Name WinStyle -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
public static extern int GetWindowLong(System.IntPtr hwnd, int index);
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
public static extern int SetWindowLong(System.IntPtr hwnd, int index, int value);
'@ -ErrorAction SilentlyContinue

function Set-AltTabHidden {
    # $Hidden=$true: tool window (no Alt-Tab, no taskbar). $false: normal app
    # window. Re-applied on HandleCreated because WinForms recreates the HWND
    # (and so drops raw ex-styles) whenever ShowInTaskbar flips.
    param($Form, [bool]$Hidden)
    if (-not $Form -or $Form.IsDisposed) { return }
    try {
        $GWL_EXSTYLE = -20; $WS_EX_TOOLWINDOW = 0x80; $WS_EX_APPWINDOW = 0x40000
        $h = $Form.Handle
        $ex = [UV.WinStyle]::GetWindowLong($h, $GWL_EXSTYLE)
        if ($Hidden) { $ex = ($ex -bor $WS_EX_TOOLWINDOW) -band (-bnot $WS_EX_APPWINDOW) }
        else         { $ex = ($ex -band (-bnot $WS_EX_TOOLWINDOW)) -bor $WS_EX_APPWINDOW }
        [void][UV.WinStyle]::SetWindowLong($h, $GWL_EXSTYLE, $ex)
    } catch { }
}

function Set-WindowChrome {
    param($Form)
    if (-not $Form) { return }
    try {
        $handle = $Form.Handle          # forces creation; harmless if already made
        # COLORREF is 0x00BBGGRR, NOT RGB - swapping the ends silently gives blue.
        $caption = 0x000A0A0A           # near-black, matches the canvas
        $border  = 0x00202020           # a hair lighter so the edge stays findable
        $text    = 0x00C8C8C8
        $dark    = 1
        [void][UV.Chrome]::DwmSetWindowAttribute($handle, 20, [ref]$dark, 4)
        [void][UV.Chrome]::DwmSetWindowAttribute($handle, 35, [ref]$caption, 4)
        [void][UV.Chrome]::DwmSetWindowAttribute($handle, 34, [ref]$border, 4)
        [void][UV.Chrome]::DwmSetWindowAttribute($handle, 36, [ref]$text, 4)
    } catch { }
}

# ── assembly load ───────────────────────────────────────────────────────────
# WebView2Loader.dll is resolved through the process search path, not the CLR
# probing path, so the native directory must be on PATH before the managed
# assemblies load. Doing this twice is harmless; the guard keeps it cheap.
$script:WV2Loaded = $false
function Initialize-WebView2Assemblies {
    if ($script:WV2Loaded) { return $true }
    $sdk = Get-WebView2SdkDir
    $core = Join-Path $sdk 'Microsoft.Web.WebView2.Core.dll'
    $wf = Join-Path $sdk 'Microsoft.Web.WebView2.WinForms.dll'
    if (-not (Test-Path $core) -or -not (Test-Path $wf)) {
        $script:WV2Failure = "WebView2 라이브러리를 못 찾음:`n$sdk"
        return $false
    }
    try {
        if ($env:PATH -notlike "*$sdk*") { $env:PATH = "$sdk;$env:PATH" }
        Add-Type -Path $core
        Add-Type -Path $wf
        $script:WV2Loaded = $true
        return $true
    } catch {
        $script:WV2Failure = "WebView2 라이브러리 로드 실패:`n" + $_.Exception.Message
        return $false
    }
}

# ── prewarm ─────────────────────────────────────────────────────────────────
# Called once at tray startup. Builds the form far off-screen with ShowInTaskbar
# disabled so the user never sees it, then initialises CoreWebView2 in the
# background while they are doing something else.
function Initialize-DetailHost {
    param([int]$Width = 1080, [int]$Height = 640)

    if ($script:WV2Form) { return $true }

    $rt = Get-WebView2RuntimeVersion
    if (-not $rt) {
        $script:WV2Failure = "이 PC에 WebView2 런타임이 없음.`n`n" +
            "usage-view 화면은 WebView2로 그린다. Microsoft Edge WebView2 Runtime을 설치하면 바로 동작함.`n" +
            "설치 링크: https://developer.microsoft.com/microsoft-edge/webview2/`n`n" +
            "설치 전에도 트레이 아이콘과 hover 요약은 정상 동작한다."
        return $false
    }
    if (-not (Initialize-WebView2Assemblies)) { return $false }

    try {
        $form = New-Object System.Windows.Forms.Form
        $form.Text = 'q_console'
        # Without this the window wears powershell.exe's icon in the title bar,
        # the taskbar and Alt-Tab, because that is the process hosting us.
        # q_console.ico is the pixel-art badge (tools/make_icon.py); the old
        # usage-view.ico stays as the fallback for a partially-copied folder.
        foreach ($ic in @('q_console.ico', 'usage-view.ico')) {
            $appIcon = Join-Path $PSScriptRoot $ic
            if (Test-Path $appIcon) {
                try { $form.Icon = New-Object System.Drawing.Icon $appIcon; break } catch { }
            }
        }
        # Sizable + MaximizeBox: gives resize grips, the maximize button, AND
        # Win+Arrow / drag-to-edge snap (the shell only offers snap to windows
        # with WS_THICKFRAME|WS_MAXIMIZEBOX, which is exactly this pair). The
        # document scales itself to whatever viewport it gets (render.py fit).
        $form.FormBorderStyle = 'Sizable'
        $form.MaximizeBox = $true
        $form.MinimumSize = New-Object System.Drawing.Size 360, 180
        $form.ClientSize = New-Object System.Drawing.Size $Width, $Height
        $form.BackColor = [System.Drawing.Color]::Black
        $form.StartPosition = 'Manual'
        $form.Location = New-Object System.Drawing.Point -32000, -32000
        $form.ShowInTaskbar = $false
        # Chrome recolouring happens AFTER Show() (see below) - applied at
        # construction time the border keeps the system accent, measured.
        # Closing must HIDE, not dispose: disposing would throw away the warmed
        # browser process and make the next open pay init again. Clearing WV2Open
        # here is what makes the next refresh stop re-rendering an unseen window.
        $form.add_HandleCreated({
            param($src, $e)
            if ($script:WV2Overlay) { Set-AltTabHidden $src $true }
        })
        $form.add_FormClosing({
            param($src, $e)
            if ($e.CloseReason -eq [System.Windows.Forms.CloseReason]::UserClosing) {
                $e.Cancel = $true
                $script:WV2Open = $false
                $src.Hide()
                $src.ShowInTaskbar = $false
            }
        })

        # A deferred render must be flushed the moment the surface becomes
        # paintable, no matter WHICH way it got there. Three distinct events can
        # make a hidden window visible and none of them go through
        # Show-DetailHostWindow:
        #   Resize          - restored from the taskbar / Alt-Tab
        #   Move            - dragged back from an off-screen position
        #   VisibleChanged  - Show() after a hide
        # Watching only one of them left blank-window paths open (measured: a
        # window moved on-screen without a resize kept the unpainted surface).
        $flush = {
            param($src, $e)
            if ($script:WV2Dirty -and (Test-DetailSurfaceLive)) {
                Invoke-DetailNavigate (Get-ActiveDetailPath)
            }
        }
        $form.add_Resize($flush)
        $form.add_Move($flush)
        $form.add_VisibleChanged($flush)

        $wv = New-Object Microsoft.Web.WebView2.WinForms.WebView2
        $wv.Dock = 'Fill'
        $wv.DefaultBackgroundColor = [System.Drawing.Color]::Black
        $form.Controls.Add($wv)

        $script:WV2DragFrom = $null
        $script:WV2DragTimer = New-Object System.Windows.Forms.Timer
        $script:WV2DragTimer.Interval = 16
        $script:WV2DragTimer.add_Tick({
            try {
                while ($true) {
                    $message = [UV.DragBridge]::Pop()
                    if ($null -eq $message) { break }
                    if ($message -eq 'q_console:drag-start') {
                        $script:WV2DragFrom = @(
                            [System.Windows.Forms.Cursor]::Position,
                            $script:WV2Form.Location)
                        continue
                    }
                    if ($message -eq 'q_console:drag-end') {
                        $script:WV2DragFrom = $null
                        continue
                    }
                    if ($message -ne 'q_console:drag-move' -or -not $script:WV2DragFrom) { continue }
                    $anchor = $script:WV2DragFrom
                    $cur = [System.Windows.Forms.Cursor]::Position
                    $nx = $anchor[1].X + $cur.X - $anchor[0].X
                    $ny = $anchor[1].Y + $cur.Y - $anchor[0].Y
                    # The taskbar is itself a shell-owned topmost window and can
                    # legitimately cover us if the user drags into its rectangle.
                    # Clamp to the current monitor's WorkingArea instead: this lets
                    # the strip sit right above taskbars on any screen or edge while
                    # never getting trapped behind one.
                    $work = [System.Windows.Forms.Screen]::FromPoint($cur).WorkingArea
                    $pad = 4
                    $maxX = [Math]::Max($work.Left + $pad,
                                        $work.Right - $script:WV2Form.Width - $pad)
                    $maxY = [Math]::Max($work.Top + $pad,
                                        $work.Bottom - $script:WV2Form.Height - $pad)
                    $nx = [Math]::Min([Math]::Max($nx, $work.Left + $pad), $maxX)
                    $ny = [Math]::Min([Math]::Max($ny, $work.Top + $pad), $maxY)
                    $script:WV2Form.Location = New-Object System.Drawing.Point $nx, $ny
                }
            } catch {
                $script:WV2DragFrom = $null
                try {
                    ("overlay drag failed: " + $_.Exception.ToString()) |
                        Out-File -Append -Encoding utf8 (Join-Path $AppHome 'tray-error.log')
                } catch { }
            }
        })
        $script:WV2DragTimer.Start()

        $udf = Join-Path $AppHome 'webview2'
        New-Item -ItemType Directory -Force -Path $udf | Out-Null
        $envTask = [Microsoft.Web.WebView2.Core.CoreWebView2Environment]::CreateAsync($null, $udf, $null)
        if (-not $envTask.Wait(60000)) { throw 'WebView2 environment timeout' }

        $wv.add_CoreWebView2InitializationCompleted({
            param($src, $e)
            if ($e.IsSuccess) {
                $script:WV2Ready = $true
                $s = $src.CoreWebView2.Settings
                # This view shows local generated HTML only; browser affordances
                # would just be ways to break the layout.
                $s.AreDefaultContextMenusEnabled = $false
                $s.AreDevToolsEnabled = $false
                $s.IsStatusBarEnabled = $false
                $s.IsZoomControlEnabled = $false
                $s.AreBrowserAcceleratorKeysEnabled = $false
                $src.CoreWebView2.add_WebMessageReceived({
                    param($sender, $eventArgs)
                    if (-not $script:WV2Overlay -or -not $script:WV2Form) { return }
                    try { $message = $eventArgs.TryGetWebMessageAsString() } catch { return }
                    if ($message -like 'q_console:drag-*') {
                        [UV.DragBridge]::Push($message)
                    }
                })
            } else {
                $script:WV2Failure = 'CoreWebView2 초기화 실패'
            }
        })
        $wv.EnsureCoreWebView2Async($envTask.Result) | Out-Null
        $form.Show()          # off-screen: required for the control to initialise

        # DWM only honours the caption/border colours once the window exists and
        # has been shown. Applied before Show() the caption went black but the
        # BORDER kept the system accent, so a focused window drew a bright blue
        # ring around a black dashboard (measured: edge pixel (0,99,177) while
        # focused). After Show() the same calls land: (9,14,18).
        Set-WindowChrome $form
        # Windows repaints the frame from the accent colour on every activation,
        # so re-apply then too or the blue comes back the first time the user
        # clicks the window.
        $form.add_Activated({ param($src, $e) Set-WindowChrome $src })

        $script:WV2Form = $form
        $script:WV2Control = $wv
        return $true
    } catch {
        $script:WV2Failure = "WebView2 host 생성 실패:`n" + $_.Exception.Message
        return $false
    }
}

function Test-DetailHostReady {
    return ($script:WV2Ready -and $script:WV2Control -and $script:WV2Form -and -not $script:WV2Form.IsDisposed)
}

# Pump until CoreWebView2 finishes. Only used on the very first open, and only if
# the user beat the prewarm; steady state returns immediately.
function Wait-DetailHostReady {
    param([int]$TimeoutMs = 15000)
    if (Test-DetailHostReady) { return $true }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-DetailHostReady) -and $sw.ElapsedMilliseconds -lt $TimeoutMs) {
        if ($script:WV2Failure) { return $false }
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 10
    }
    return (Test-DetailHostReady)
}

# ── rendering ───────────────────────────────────────────────────────────────
# The renderer writes a full self-contained document to app home and we navigate
# to it. NavigateToString is capped at ~2 MB and mangles relative URIs, and these
# documents embed a base64 font, so a real file is both safer and faster.

# Which document this window should be showing right now. The worker writes
# BOTH on every refresh: detail.html (full board, themed) and overlay.html
# (one-line status strip). Overlay mode swaps the document, not just the frame.
function Get-ActiveDetailPath {
    if ($script:WV2Overlay) {
        $p = Join-Path $AppHome 'overlay.html'
        if (Test-Path $p) { return $p }
    }
    return (Join-Path $AppHome 'detail.html')
}
function Show-DetailHtml {
    param([string]$Html, [string]$Title = 'q_console')

    if (-not (Test-DetailHostReady)) { return $false }
    $path = Join-Path $AppHome 'detail.html'
    try {
        [System.IO.File]::WriteAllText($path, $Html, (New-Object System.Text.UTF8Encoding $false))
    } catch {
        $script:WV2Failure = "화면 파일 쓰기 실패:`n" + $_.Exception.Message
        return $false
    }
    $script:WV2Form.Text = $Title

    # A window that is not composited (minimized, or still parked off-screen for
    # the prewarm) produces no frames. Navigating it there leaves a blank surface
    # that survives the restore. Mark it dirty instead and let Show-DetailHostWindow
    # replay this once the window is really on screen.
    if (-not (Test-DetailSurfaceLive)) {
        $script:WV2Dirty = $true
        return $true
    }
    Invoke-DetailNavigate $path
    return $true
}

# True only when the surface can actually paint: shown, not minimized, on-screen.
function Test-DetailSurfaceLive {
    if (-not $script:WV2Form -or $script:WV2Form.IsDisposed) { return $false }
    $f = $script:WV2Form
    if (-not $f.Visible) { return $false }
    if ($f.WindowState -eq [System.Windows.Forms.FormWindowState]::Minimized) { return $false }
    if ($f.Location.X -lt -10000) { return $false }
    return $true
}

# Point the engine at the file. Re-navigating the SAME uri is ignored, so an
# unchanged path needs an explicit Reload to pick the new bytes up.
function Invoke-DetailNavigate {
    param([string]$Path)
    $uri = ([Uri]$Path).AbsoluteUri
    if ($script:WV2LastHtml -eq $uri) {
        $script:WV2Control.CoreWebView2.Reload()
    } else {
        $script:WV2Control.CoreWebView2.Navigate($uri)
        $script:WV2LastHtml = $uri
    }
    $script:WV2Dirty = $false
    Resize-DetailToDocument $Path
}

# The mini layout (D-65) is a narrow strip whose height depends on how many
# limits exist, so a fixed 1080x640 form would frame it in dead black. The
# document states its own size, and the window follows.
#
# The size is parsed from the FILE rather than asked of the DOM: ExecuteScript is
# async and would need pumping, and this runs on the UI thread during a render.
# A layout that says nothing keeps the standard board size.
function Resize-DetailToDocument {
    param([string]$Path)
    if (-not $script:WV2Form -or $script:WV2Form.IsDisposed) { return }
    $w = 1080; $h = 640
    try {
        $head = [System.IO.File]::ReadAllText($Path)
        $m = [regex]::Match($head, "data-w='(\d+)'\s+data-h='(\d+)'")
        if ($m.Success) {
            $w = [int]$m.Groups[1].Value
            $h = [int]$m.Groups[2].Value
        }
    } catch { return }
    # Guard rails: never let a malformed document produce an unusable window.
    if ($w -lt 200 -or $w -gt 4000 -or $h -lt 100 -or $h -gt 3000) { return }

    # The window is Sizable now, so the user's own size wins. Auto-resize ONLY
    # when the document's intrinsic size changed (a theme switch, e.g. board ->
    # mini) - a routine refresh of the same layout must not snap the window back.
    $prev = $script:WV2DocSize
    $script:WV2DocSize = @($w, $h)
    if ($prev -and $prev[0] -eq $w -and $prev[1] -eq $h) { return }
    if ($script:WV2Overlay) { Set-OverlayGeometry; return }
    if ($script:WV2Form.WindowState -ne [System.Windows.Forms.FormWindowState]::Normal) { return }
    if ($script:WV2Form.ClientSize.Width -ne $w -or $script:WV2Form.ClientSize.Height -ne $h) {
        $script:WV2Form.ClientSize = New-Object System.Drawing.Size $w, $h
    }
}

# ── overlay mode ────────────────────────────────────────────────────────────
# A tiny always-on-top status strip: per-provider icon + used% + reset, one
# line, borderless, translucent. It shows its OWN document (overlay.html,
# written by the worker on every refresh alongside detail.html) - not a scaled
# copy of the board. The document posts a drag message on left-button down, so
# every visible black pixel is movable without a separate WinForms grip row.

function Get-OverlayDocSize {
    # data-w/data-h of overlay.html, same contract as the board document.
    $w = 520; $h = 46
    try {
        $head = [System.IO.File]::ReadAllText((Join-Path $AppHome 'overlay.html'))
        $m = [regex]::Match($head, "data-w='(\d+)'\s+data-h='(\d+)'")
        if ($m.Success) { $w = [int]$m.Groups[1].Value; $h = [int]$m.Groups[2].Value }
    } catch { }
    return @($w, $h)
}

function Set-OverlayGeometry {
    # Pin to the bottom-right of the working area at the document's native size.
    # If the user drags it elsewhere we keep that position on
    # later refreshes - only a size change re-pins.
    $f = $script:WV2Form
    if (-not $f -or $f.IsDisposed) { return }
    $doc = Get-OverlayDocSize
    $newSize = New-Object System.Drawing.Size $doc[0], $doc[1]
    $repin = ($f.ClientSize -ne $newSize) -or ($f.Location.X -lt -10000)
    $f.ClientSize = $newSize
    if ($repin) {
        $screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
        $x = $screen.Right - $f.Width - 16
        $y = $screen.Bottom - $f.Height - 16
        $f.Location = New-Object System.Drawing.Point $x, $y
    }
}

function Set-OverlayMode {
    param([bool]$On)
    $f = $script:WV2Form
    if (-not $f -or $f.IsDisposed) { return }
    if ($On -eq $script:WV2Overlay) { return }
    $script:WV2Overlay = $On
    if ($On) {
        if ($f.WindowState -ne [System.Windows.Forms.FormWindowState]::Normal) {
            $f.WindowState = [System.Windows.Forms.FormWindowState]::Normal
        }
        $script:WV2SavedBounds = $f.Bounds
        $f.FormBorderStyle = 'None'
        # The board window's minimum would clamp the compact strip height.
        $f.MinimumSize = New-Object System.Drawing.Size 0, 0
        $f.TopMost = $true
        $f.Opacity = 0.90
        $f.ShowInTaskbar = $false
        Set-OverlayGeometry
        Set-AltTabHidden $f $true
        if (-not $f.Visible) { $f.Show() }
        $script:WV2Open = $true
        Invoke-DetailNavigate (Get-ActiveDetailPath)   # swap to overlay.html
    } else {
        $f.FormBorderStyle = 'Sizable'
        $f.MinimumSize = New-Object System.Drawing.Size 360, 180
        $f.Opacity = 1.0
        $f.TopMost = [bool]$script:WV2AlwaysOnTop
        $f.ShowInTaskbar = $true
        Set-AltTabHidden $f $false
        if ($script:WV2SavedBounds) {
            $f.Bounds = $script:WV2SavedBounds
            $script:WV2SavedBounds = $null
        }
        Set-WindowChrome $f
        Invoke-DetailNavigate (Get-ActiveDetailPath)   # back to the board
    }
}

function Test-OverlayMode { return [bool]$script:WV2Overlay }

# Bring the prewarmed window on-screen. This is the whole user-visible cost of
# opening the detail view: measured 69 ms cold, 17 ms after a hide.
#
# Restoring a MINIMIZED window must come BEFORE reading Location: while minimized
# WinForms reports -32000,-32000, so the centring branch below would "recentre" a
# window that already had a good position and yank it away from where the user
# left it.
function Show-DetailHostWindow {
    if (-not $script:WV2Form -or $script:WV2Form.IsDisposed) { return }
    $f = $script:WV2Form
    if ($script:WV2Overlay) {
        # Overlay is its own placement policy: corner-pinned, topmost, no
        # taskbar. A "show" gesture just makes sure it is on screen.
        if ($f.Location.X -lt -10000) { Set-OverlayGeometry }
        if (-not $f.Visible) { $f.Show() }
        Set-AltTabHidden $f $true
        $f.TopMost = $true
        $script:WV2Open = $true
        if ($script:WV2Dirty) {
            Invoke-DetailNavigate (Get-ActiveDetailPath)
        }
        return
    }
    if ($f.WindowState -eq [System.Windows.Forms.FormWindowState]::Minimized) {
        $f.WindowState = [System.Windows.Forms.FormWindowState]::Normal
    }
    if ($f.Location.X -lt -10000) {
        $screen = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
        $x = [int](($screen.Width - $f.Width) / 2) + $screen.X
        $y = [int](($screen.Height - $f.Height) / 2) + $screen.Y
        $f.Location = New-Object System.Drawing.Point $x, $y
    }
    $f.ShowInTaskbar = $true
    if (-not $f.Visible) { $f.Show() }
    # The true->false pulse is only a "raise above siblings" trick; when the
    # user has pinned the window (tray menu), TopMost must stay on afterwards.
    $f.TopMost = $true
    $f.TopMost = [bool]$script:WV2AlwaysOnTop
    $f.Activate()
    $f.BringToFront()
    $script:WV2Open = $true

    # The window is composited from here on, so replay any render that was
    # deferred while it was hidden. Without this the user restores a window whose
    # DOM is current but whose surface was never painted - i.e. a blank screen.
    if ($script:WV2Dirty) {
        Invoke-DetailNavigate (Get-ActiveDetailPath)
    }
}

# Pin/unpin the detail window over other windows. Takes effect immediately when
# the form exists; the flag also survives form creation because Show-DetailHostWindow
# re-applies it after every raise pulse.
function Set-DetailTopMost {
    param([bool]$Value)
    $script:WV2AlwaysOnTop = $Value
    if ($script:WV2Form -and -not $script:WV2Form.IsDisposed) {
        $script:WV2Form.TopMost = $Value
    }
}

# "Is the detail window one the user can see?" A minimized window still counts:
# they will restore it from the taskbar and must not find stale numbers there.
# Only an explicit close (FormClosing) or Close-DetailHost clears this.
function Test-DetailHostVisible {
    return ($script:WV2Open -and $script:WV2Form -and -not $script:WV2Form.IsDisposed -and $script:WV2Form.Visible)
}

function Close-DetailHost {
    $script:WV2Open = $false
    if ($script:WV2Form -and -not $script:WV2Form.IsDisposed) {
        $script:WV2Form.Hide()
        $script:WV2Form.ShowInTaskbar = $false
    }
}
