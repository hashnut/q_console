# usage-view tray. Read-only, display-only: reads the pre-rendered JSON cache
# (hover_line / summary_lines / detail_text) written by the orchestrator.
#
# The parent (app.py in frozen mode, or a source-mode launcher) starts this with
# -STA already set and passes the worker command + app home EXPLICITLY, so this
# script never guesses `python` off PATH (Architect F6). On a clean PC with no
# python/bun installed, a source-mode `Get-Command python` would throw and kill
# the tray at startup; the frozen exe passes its own path instead.
#
# Start (source):  powershell -STA -NoProfile -ExecutionPolicy Bypass -File tray.ps1 `
#                    -WorkerExecutable python -WorkerArguments 'core/__main__.py' -AppHome '<dir>'
# Start (frozen):  app.exe launches this with -WorkerExecutable <app.exe>
#                    -WorkerArguments '--refresh-worker' -AppHome '%LOCALAPPDATA%\usage-view'
param(
    [Parameter(Mandatory = $true)] [string] $WorkerExecutable,
    [Parameter(Mandatory = $true)] [string] $WorkerArguments,
    [Parameter(Mandatory = $true)] [string] $AppHome,
    [string] $ShotPath = '',
    [switch] $OpenDetail
)
$created = $false
$script:TrayMutex = [System.Threading.Mutex]::new(
    $true, 'Local\gjc-cost-audit-usage-view-tray', [ref]$created
)
if (-not $created) { exit 0 }
$ErrorActionPreference = 'Stop'

# No STA self-relaunch guard: the parent already starts us -STA. Re-launching here
# would spawn a grandchild and let the parent mistake the tray for dead (F3).

$CachePath = Join-Path $AppHome 'usage-cache.json'
$PollMs = 30 * 60 * 1000

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# The detail window is a WebView2 surface (D-52). This file keeps the tray icon,
# the menu and the worker plumbing; webview2-host.ps1 owns presentation only.
. (Join-Path $PSScriptRoot 'webview2-host.ps1')

# Background worker jobs. The tray runs on one WinForms UI thread, so the worker
# exe is NEVER invoked with `&` from a click/tick handler: a probe cycle can take
# 80 s (probe 20 s + measure 60 s) and that blocked the whole app - the freeze the
# user hit when picking a theme. Jobs are spawned detached and polled here.
$script:Jobs = New-Object System.Collections.ArrayList
$script:ThemePending = $null
$script:ThemeJobActive = $false

$script:JobTimer = New-Object System.Windows.Forms.Timer
$script:JobTimer.Interval = 200
$script:JobTimer.add_Tick({
    try {
        foreach ($job in @($script:Jobs)) {
            if (-not $job.proc.HasExited) {
                if (([DateTime]::Now - $job.started).TotalSeconds -gt 180) {
                    try { $job.proc.Kill() } catch {}
                }
                continue
            }
            [void]$script:Jobs.Remove($job)
            if ($job.stdout) {
                Remove-Item -LiteralPath $job.stdout -Force -ErrorAction SilentlyContinue
                Remove-Item -LiteralPath ($job.stdout + '.err') -Force -ErrorAction SilentlyContinue
            }
            if ($job.onDone) { & $job.onDone }
        }
        if ($script:Jobs.Count -eq 0) { $script:JobTimer.Stop() }
    } catch {
        # a failed callback must never kill the tray ($ErrorActionPreference=Stop)
        $script:Jobs.Clear()
        $script:JobTimer.Stop()
    }
})

# Spawn one worker invocation without blocking the UI thread. $OnDone runs on the
# UI thread once the process exits, so it may touch WinForms objects safely.
#
# stdout MUST be redirected. A --noconsole frozen exe launched with no stdout
# handle blocks forever on its result line (measured: 167 ms with redirection vs
# still running at 25 s without), which is what wedged every theme click.
function Start-Worker {
    param(
        [string[]]$Arguments,
        [scriptblock]$OnDone,
        [string]$Kind = 'generic'
    )
    $argList = @()
    foreach ($a in ($WorkerArguments -split ' ')) { if ($a) { $argList += $a } }
    foreach ($a in $Arguments) { $argList += $a }
    $stdout = Join-Path $AppHome ('worker-{0}.out' -f [Guid]::NewGuid().ToString('N'))
    try {
        $proc = Start-Process -FilePath $WorkerExecutable -ArgumentList $argList `
            -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $stdout -RedirectStandardError ($stdout + '.err')
    } catch {
        return $false
    }
    [void]$script:Jobs.Add(@{
        proc = $proc; onDone = $OnDone; started = [DateTime]::Now
        stdout = $stdout; kind = $Kind
    })
    $script:JobTimer.Start()
    return $true
}

function Test-DetailOpen {
    return (Test-DetailHostVisible)
}

function Read-Cache {
    if (-not (Test-Path $CachePath)) { return $null }
    try {
        return Get-Content -Raw -Encoding UTF8 $CachePath | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Format-Age {
    param([double]$GeneratedAtMs)
    if ($GeneratedAtMs -le 0) { return '' }
    $nowMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $sec = [math]::Max(0, ($nowMs - $GeneratedAtMs) / 1000.0)
    if ($sec -lt 60) { return ('{0}s' -f [int]$sec) }
    if ($sec -lt 3600) { return ('{0}m' -f [int]($sec / 60)) }
    if ($sec -lt 86400) { return ('{0:0.0}h' -f ($sec / 3600)) -replace '\.0h', 'h' }
    return ('{0:0.0}d' -f ($sec / 86400)) -replace '\.0d', 'd'
}

# Ask the orchestrator for fresh data in the background. Returns immediately; the
# tray repaints (and re-fills an open detail window) when the worker exits.
function Request-Refresh {
    if (@($script:Jobs | Where-Object { $_.kind -eq 'refresh' }).Count -gt 0) { return }
    [void](Start-Worker -Arguments @('--refresh') -Kind 'refresh' -OnDone {
        Update-Display
        # Refill an already-open window IN PLACE. Never Show-Detail here: that
        # raises and activates, so the 30-minute poll popped the window over
        # whatever the user was doing (D-64). A background refresh may update
        # what a window shows; it may never decide to show one.
        if (Test-DetailOpen) { [void](Update-DetailContent) }
    })
}

function ConvertFrom-Hex {
    param([string]$Hex)
    $Hex = $Hex.TrimStart('#')
    return [System.Drawing.Color]::FromArgb(
        [Convert]::ToInt32($Hex.Substring(0, 2), 16),
        [Convert]::ToInt32($Hex.Substring(2, 2), 16),
        [Convert]::ToInt32($Hex.Substring(4, 2), 16))
}

# The detail window's colours live entirely in the renderer (D-52). The only
# colour the tray still chooses is its own icon, which has a separate palette
# picked for taskbar legibility (D-53) rather than for a black canvas.

# Theme = one of the surviving designs. Legacy ids from an older config.json map
# onto the nearest survivor, matching core/config.py THEME_ALIASES so the tray
# and the worker never disagree about what is selected.
#   phosphor / surfacer - full 1080x640 boards, opened and closed (D-49)
#   mini                - narrow always-on-screen strip (D-65)
$script:THEME_ALIASES = @{
    'board-ember' = 'surfacer'; 'board-slate' = 'surfacer'; 'board-abyss' = 'phosphor'
    'billboard' = 'surfacer'; 'ledger' = 'surfacer'; 'shoreline' = 'surfacer'
    'orbit' = 'surfacer'; 'console' = 'phosphor'; 'mono' = 'surfacer'
    'midnight' = 'phosphor'; 'surfacer' = 'surfacer'; 'phosphor' = 'phosphor'
    'mini' = 'mini'
}

function Get-NativeTheme {
    $path = Join-Path $AppHome 'config.json'
    try {
        $value = [string]((Get-Content -Raw -Encoding UTF8 $path | ConvertFrom-Json).theme)
    } catch {
        $value = 'phosphor'
    }
    $mapped = $script:THEME_ALIASES[$value]
    if ($mapped) { return $mapped }
    return 'phosphor'
}

# Persisted always-on-top flag; same file as the theme, missing/corrupt -> off.
function Get-AlwaysOnTop {
    $path = Join-Path $AppHome 'config.json'
    try {
        return [bool]((Get-Content -Raw -Encoding UTF8 $path | ConvertFrom-Json).always_on_top)
    } catch {
        return $false
    }
}

function Get-OverlayFlag {
    $path = Join-Path $AppHome 'config.json'
    try {
        return [bool]((Get-Content -Raw -Encoding UTF8 $path | ConvertFrom-Json).overlay_mode)
    } catch {
        return $false
    }
}

# Persist the overlay flag through the worker and keep the menu check honest.
# Also called from the host's grip double-click (exit gesture), so it must be
# safe to call with the menu not yet built.
function Save-OverlayFlag {
    param([bool]$Value)
    $flag = 'off'; if ($Value) { $flag = 'on' }
    [void](Start-Worker -Arguments @('--set-overlay', $flag) -Kind 'config')
    if ($script:OverlayItem) { $script:OverlayItem.Checked = $Value }
}


# ── tray icon: a percent badge sized for the shell, not for the canvas ───────
# Measured on this PC: 144 DPI, SM_CXSMICON = 24, dark taskbar. The old painter
# drew a 32px donut with dashboard tones and let the shell downscale it, which
# cost legibility three separate ways (D-53):
#   1. 32 -> 24 downscale smeared the digits; now we paint at the asked size.
#   2. Dashboard tones are picked against a black canvas. `relaxed #4A4A4A` on a
#      dark taskbar is 1.84 contrast and `warning #FFFFFF` on a light taskbar is
#      1.11 — both effectively invisible. The icon now owns its own tone table,
#      chosen per taskbar polarity and verified >= 4.5 against that background.
#   3. The digits used the SAME colour as the ring, so `100` in blocked red sat
#      on red and could not be read. Ink is now derived from the fill it sits on.
# A square badge is used because rectangles land on the pixel grid at 16-24px,
# where a circle spends its corners on nothing and shrinks the glyph box.
$script:IconHandles = New-Object System.Collections.ArrayList

# Icon-only tone table. Deliberately NOT $script:TONE: that one is authored for
# the black dashboard canvas and is unreadable on a taskbar.
$script:ICON_TONE_DARK = @{
    'relaxed' = '#D6D6D6'; 'caution' = '#F2C14E'; 'warning' = '#FF9142'
    'blocked' = '#FF5A50'; 'unknown' = '#9AA0A6'
}
$script:ICON_TONE_LIGHT = @{
    'relaxed' = '#3D3D3D'; 'caution' = '#8A6A00'; 'warning' = '#B3400A'
    'blocked' = '#C4160D'; 'unknown' = '#5A5A5A'
}

# The shell repaints the tray on theme switch but never tells us, so read the
# same key Explorer reads. Cheap enough to call per repaint (every 30 min).
function Test-LightTaskbar {
    try {
        $v = Get-ItemPropertyValue -ErrorAction Stop `
            -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' `
            -Name 'SystemUsesLightTheme'
        return ([int]$v -ne 0)
    } catch { return $false }   # Win11 default is dark; assume it when unreadable
}

# The size the shell actually asks for. 100% DPI = 16, 150% = 24, 200% = 32.
#
# This process is DPI-UNAWARE (the tray never calls SetProcessDPIAware, because
# doing so would rescale every WinForms surface it already owns). Windows
# therefore virtualises every metric to 96 DPI, and both SM_CXSMICON and
# SystemInformation.SmallIconSize answer 16 on a 144 DPI machine that actually
# renders the tray at 24. Painting 16 and letting the shell upscale is exactly
# the blur D-53 set out to remove.
#
# Measured on this PC: SM_CXSMICON = 16 (virtualised) vs GetDpiForWindow on the
# taskbar = 144 -> GetSystemMetricsForDpi(SM_CXSMICON, 144) = 24 (true). So ask
# the taskbar's own DPI, which is not virtualised, and fall back down the chain
# only when an API is missing.
Add-Type -Namespace UV -Name Dpi -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern System.IntPtr FindWindow(string lpClassName, string lpWindowName);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern uint GetDpiForWindow(System.IntPtr hwnd);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern int GetSystemMetricsForDpi(int nIndex, uint dpi);
'@ -ErrorAction SilentlyContinue

function Get-TrayIconSize {
    $SM_CXSMICON = 49
    try {
        $tray = [UV.Dpi]::FindWindow('Shell_TrayWnd', $null)
        if ($tray -ne [System.IntPtr]::Zero) {
            $dpi = [UV.Dpi]::GetDpiForWindow($tray)
            if ($dpi -ge 96) {
                $s = [UV.Dpi]::GetSystemMetricsForDpi($SM_CXSMICON, $dpi)
                if ($s -ge 8 -and $s -le 64) { return [int]$s }
            }
        }
    } catch { }
    # Fallback: the DPI the shell applied at logon, scaled off the 96 DPI baseline.
    try {
        $applied = Get-ItemPropertyValue -ErrorAction Stop `
            -Path 'HKCU:\Control Panel\Desktop\WindowMetrics' -Name 'AppliedDPI'
        $s = [int][Math]::Round(16.0 * [int]$applied / 96.0)
        if ($s -ge 8 -and $s -le 64) { return $s }
    } catch { }
    return 16
}

# WCAG relative luminance -> pick the ink that is readable ON this fill.
function Get-InkFor {
    param($Fill)
    $ch = @($Fill.R, $Fill.G, $Fill.B) | ForEach-Object {
        $s = $_ / 255.0
        if ($s -le 0.03928) { $s / 12.92 } else { [Math]::Pow((($s + 0.055) / 1.055), 2.4) }
    }
    $lum = 0.2126 * $ch[0] + 0.7152 * $ch[1] + 0.0722 * $ch[2]
    if ($lum -gt 0.35) { return [System.Drawing.Color]::FromArgb(12, 12, 12) }
    return [System.Drawing.Color]::FromArgb(255, 255, 255)
}

function Resolve-IconTone {
    param([string]$Tone)
    $table = if (Test-LightTaskbar) { $script:ICON_TONE_LIGHT } else { $script:ICON_TONE_DARK }
    $hex = $table[$Tone]
    if (-not $hex) { $hex = $table['unknown'] }
    return ConvertFrom-Hex $hex
}

# Map a dashboard tone name onto the five icon tones. Anything that means "we do
# not have a number" collapses to unknown so the icon never fakes a reading.
function Get-IconToneName {
    param([string]$Tone)
    switch ($Tone) {
        'relaxed'        { 'relaxed' }
        'caution'        { 'caution' }
        'warning'        { 'warning' }
        'blocked'        { 'blocked' }
        'config-error'   { 'blocked' }
        'reset-imminent' { 'blocked' }
        default          { 'unknown' }
    }
}

function New-GaugeIcon {
    param([double]$Percent = 0, [string]$Tone = 'relaxed', [switch]$Unknown)
    $size = Get-TrayIconSize
    $bmp = New-Object System.Drawing.Bitmap $size, $size
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'
    $g.PixelOffsetMode = 'HighQuality'
    # ClearType fringes colour onto a transparent bitmap; grid-fit AA does not.
    $g.TextRenderingHint = 'AntiAliasGridFit'
    $g.Clear([System.Drawing.Color]::Transparent)

    $fill = Resolve-IconTone $(if ($Unknown) { 'unknown' } else { $Tone })
    $ink = Get-InkFor $fill
    $light = Test-LightTaskbar

    $m = [int][Math]::Round($size * 0.06)
    $rect = New-Object System.Drawing.Rectangle $m, $m, ($size - 2 * $m - 1), ($size - 2 * $m - 1)

    $bg = New-Object System.Drawing.SolidBrush $fill
    $g.FillRectangle($bg, $rect); $bg.Dispose()
    # Separator against a taskbar of the same polarity as the badge.
    $edgeCol = if ($light) { [System.Drawing.Color]::FromArgb(210, 255, 255, 255) }
               else        { [System.Drawing.Color]::FromArgb(210, 0, 0, 0) }
    $edge = New-Object System.Drawing.Pen $edgeCol, 1
    $g.DrawRectangle($edge, $rect); $edge.Dispose()

    if (-not $Unknown) {
        # Burn bar along the bottom edge: a second, non-colour channel for the
        # same number, so the icon still reads for a colour-blind user.
        $barH = [int][Math]::Max(2, [Math]::Round($size * 0.17))
        $barY = $rect.Bottom - $barH + 1
        $trk = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(60, $ink.R, $ink.G, $ink.B))
        $g.FillRectangle($trk, $rect.X, $barY, $rect.Width, $barH); $trk.Dispose()
        $pct = [Math]::Max(0, [Math]::Min(100, $Percent))
        $w = [int][Math]::Round($rect.Width * $pct / 100.0)
        if ($pct -gt 0 -and $w -lt 2) { $w = 2 }   # keep a real reading visible
        if ($w -gt 0) {
            $fb = New-Object System.Drawing.SolidBrush $ink
            $g.FillRectangle($fb, $rect.X, $barY, $w, $barH); $fb.Dispose()
        }
    }

    $txt = if ($Unknown) { '?' } else { [string][int][Math]::Round($Percent) }
    $fs = if ($txt.Length -ge 3) { $size * 0.40 } else { $size * 0.50 }
    $f = New-Object System.Drawing.Font('Segoe UI', $fs, [System.Drawing.FontStyle]::Bold, 'Pixel')
    $b = New-Object System.Drawing.SolidBrush $ink
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment = 'Center'; $fmt.LineAlignment = 'Center'
    # Lift the glyph box off the burn bar so descender space is not wasted.
    $box = New-Object System.Drawing.RectangleF $rect.X, ($rect.Y - $size * 0.10), $rect.Width, $rect.Height
    $g.DrawString($txt, $f, $b, $box, $fmt)
    $f.Dispose(); $b.Dispose()
    $g.Dispose()

    $hicon = $bmp.GetHicon()
    $bmp.Dispose()
    $ico = [System.Drawing.Icon]::FromHandle($hicon)
    [void]$script:IconHandles.Add($hicon)
    return $ico
}

# Win32 DestroyIcon so repainting the gauge every 30 min does not leak handles.
Add-Type -Namespace UV -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", CharSet=System.Runtime.InteropServices.CharSet.Auto)]
public static extern bool DestroyIcon(System.IntPtr handle);
'@


function Set-GaugeIcon {
    param([double]$Percent = 0, [string]$Tone = 'relaxed', [switch]$Unknown)
    $old = $icon.Icon
    $icon.Icon = if ($Unknown) { New-GaugeIcon -Unknown } else { New-GaugeIcon -Percent $Percent -Tone $Tone }
    if ($old) { $old.Dispose() }
    while ($script:IconHandles.Count -gt 2) {
        [void][UV.Native]::DestroyIcon($script:IconHandles[0])
        $script:IconHandles.RemoveAt(0)
    }
}

$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = New-GaugeIcon -Unknown
$icon.Visible = $true
$icon.Text = 'q_console'

# Colour dashboard popup rendered from the structured gui_model in the cache.
# ADHD-friendly: a coloured status banner, per-provider cards with traffic-light
# usage bars + reset countdowns, a colour 7x24 activity heatmap, and green
# recommendation badges. Falls back to the ASCII detail_text if gui_model absent.
# ── dark theme palette (GitHub-dark derived) ─────────────────────────────────

# Render the current cache into the chosen design.
#
# The renderer runs in the WORKER (python), not here: PowerShell has no business
# generating this HTML, and the worker already owns the cache. The tray asks for
# a render, gets a file path back, and points the browser at it. A theme switch
# therefore never re-probes and never spawns a measure cycle (D-49).
#
# Update-DetailContent REFILLS the window and nothing else. It never shows,
# raises, activates or un-minimizes anything, so a background refresh can keep an
# open window current without stealing focus (D-64: the 30-minute poll used to
# yank the window over whatever the user was doing).
# Show-Detail = refill + bring on screen. ONLY user gestures may call it.
function Update-DetailContent {
    if (-not (Test-DetailHostReady)) { return $false }
    $htmlPath = Get-ActiveDetailPath   # board, or overlay.html in overlay mode
    if (-not (Test-Path $htmlPath)) { return $false }
    # The worker already wrote this file; re-reading it just to hand the same
    # bytes back for a rewrite was a pointless round trip that also raced the
    # worker's own atomic replace. Point the engine straight at the file.
    $script:WV2Form.Text = "q_console / " + (Get-NativeTheme)
    if (-not (Test-DetailSurfaceLive)) {
        $script:WV2Dirty = $true      # hidden: defer, replay on show (D-64)
        return $true
    }
    Invoke-DetailNavigate $htmlPath
    return $true
}

function Show-Detail {
    param($Cache)

    if (-not (Initialize-DetailHost)) {
        Show-DetailFallback $script:WV2Failure
        return
    }
    if (-not (Wait-DetailHostReady)) {
        $why = $script:WV2Failure
        if ([string]::IsNullOrWhiteSpace($why)) { $why = '화면 준비 중' }
        Show-DetailFallback $why
        return
    }

    [void](Update-DetailContent)
    Show-DetailHostWindow
}

# Honest empty/degraded state. Never a blank window: say what is missing and how
# it resolves. Used when WebView2 is absent (another PC, AC-12) or the very first
# render has not landed yet.
function Show-DetailFallback {
    param([string]$Message)
    if (-not $script:FallbackForm -or $script:FallbackForm.IsDisposed) {
        $f = New-Object System.Windows.Forms.Form
        $f.Text = 'q_console'
        foreach ($ic in @('q_console.ico', 'usage-view.ico')) {
            $appIcon = Join-Path $PSScriptRoot $ic
            if (Test-Path $appIcon) {
                try { $f.Icon = New-Object System.Drawing.Icon $appIcon; break } catch { }
            }
        }
        $f.ClientSize = New-Object System.Drawing.Size 720, 340
        $f.StartPosition = 'CenterScreen'
        $f.BackColor = [System.Drawing.Color]::FromArgb(5, 8, 7)
        $box = New-Object System.Windows.Forms.TextBox
        $box.Multiline = $true; $box.ReadOnly = $true; $box.Dock = 'Fill'
        $box.ScrollBars = 'Vertical'; $box.BorderStyle = 'None'
        $box.Font = New-Object System.Drawing.Font('Consolas', 11)
        $box.BackColor = [System.Drawing.Color]::FromArgb(5, 8, 7)
        $box.ForeColor = [System.Drawing.Color]::FromArgb(126, 231, 168)
        $f.Controls.Add($box)
        $f.Tag = $box
        # Chrome is applied after Show() below, not here: DWM ignores the border
        # colour on a window that has never been shown.
        $f.add_HandleCreated({ param($src, $e) Set-TaskbarIdentity $src })
        $f.add_Activated({ param($src, $e) Set-WindowChrome $src })
        $script:FallbackForm = $f
    }
    $text = if ([string]::IsNullOrWhiteSpace($Message)) {
        "사용량 데이터를 아직 못 읽었음.`n`n" +
        "지금 백그라운드로 조회 중이며 끝나면 이 창이 스스로 채워짐.`n" +
        "계속 비어 있으면 트레이 우클릭 → Refresh now 를 눌러라."
    } else { $Message }
    $script:FallbackForm.Tag.Text = ($text -replace "`n", "`r`n")
    $script:FallbackForm.Show()
    Set-WindowChrome $script:FallbackForm
    $script:FallbackForm.Activate()
    $script:FallbackForm.BringToFront()
}


function Update-Display {
    param([switch]$ShowDetail, [switch]$Refresh)
    if ($Refresh) { Request-Refresh }
    $cache = Read-Cache
    if ($null -eq $cache) {
        $icon.Text = 'q_console  -  STALE (no cache)'
        if ($ShowDetail) {
            Show-Detail $null
        }
        return
    }

    $age = Format-Age ([double]$cache.generated_at_ms)

    # Tooltip: NotifyIcon.Text is HARD-capped at 63 chars by the shell (setting a
    # longer string throws). So we pack the most information into two short lines:
    #   line 1: verdict            e.g. "여유"
    #   line 2: the 3 providers compacted to fit, e.g. "C 11% · X 22% · G 8.9%"
    # then append the age only if the whole thing still fits. Everything is clamped
    # to 63 chars so the assignment can never throw and kill the tray.
    $hover = [string]$cache.hover_line
    if ([string]::IsNullOrWhiteSpace($hover)) { $hover = 'q_console' }
    # compact the 3 summary lines: take the leading token as a 1-letter tag + %/$.
    $compact = ''
    if ($cache.PSObject.Properties['summary_lines'] -and $cache.summary_lines) {
        $parts = @()
        foreach ($sl in @($cache.summary_lines)) {
            $t = ([string]$sl).Trim()
            if (-not $t) { continue }
            # "Claude 11% 7d (F 6%)" -> tag "C", first %/$ token
            $tag = $t.Substring(0,1)
            if ($t -like 'Codex*') { $tag = 'X' }
            elseif ($t -like 'Grok*') { $tag = 'G' }
            elseif ($t -like 'Claude*') { $tag = 'C' }
            $val = ''
            if ($t -match '(\d+(?:\.\d+)?%)') { $val = $Matches[1] }
            elseif ($t -match '(\$\d+)') { $val = $Matches[1] }
            elseif ($t -match 'unavailable') { $val = 'N/A' }
            elseif ($t -match 'ERROR') { $val = 'ERR' }
            else { $val = '--' }
            $parts += ('{0} {1}' -f $tag, $val)
        }
        $compact = ($parts -join '  ')
    }
    $tip = $hover
    if ($compact) {
        $cand = "$hover`n$compact"
        if ($cand.Length -le 63) { $tip = $cand }
    }
    if ($age) {
        $cand2 = "$tip  ($age)"
        if ($cand2.Length -le 63) { $tip = $cand2 }
    }
    if ($tip.Length -gt 63) { $tip = $tip.Substring(0, 63) }
    $icon.Text = $tip

    # tray gauge: worst (highest) used% across the displayed limits, toned by that
    # limit's own band. No numeric usage anywhere -> `?` badge, never a fake 0%.
    # The icon is handed the tone NAME; it owns its own taskbar-legible palette
    # (D-53). Passing a dashboard hex here is what made the badge unreadable.
    $worst = -1.0
    $worstTone = 'relaxed'
    if ($cache.PSObject.Properties['gui_model'] -and $cache.gui_model) {
        foreach ($prov in $cache.gui_model.providers) {
            foreach ($acct in $prov.accounts) {
                foreach ($lim in $acct.limits) {
                    if ($null -ne $lim.used) {
                        $u = [double]$lim.used
                        if ($u -gt $worst) { $worst = $u; $worstTone = Get-IconToneName ([string]$lim.bar_tone) }
                    }
                }
            }
        }
    }
    if ($worst -lt 0) { Set-GaugeIcon -Unknown } else { Set-GaugeIcon -Percent $worst -Tone $worstTone }

    # Balloon shows the 3 representative provider lines + age.
    $summary = @($cache.summary_lines)
    $balloon = ($summary -join "`n")
    if ($age) { $balloon = "$balloon`nage $age" }
    # No balloon/toast: the user asked for a silent tray. The hover tooltip and the
    # click-through dashboard carry everything; popups were noise.

    if ($ShowDetail) {
        Show-Detail $cache
    }
}

# Tick the menu entry matching the persisted theme.
function Update-ThemeChecks {
    $current = Get-NativeTheme
    foreach ($item in $script:ThemeItems) {
        $item.Checked = ([string]$item.Tag -eq $current)
    }
}

# Persist a theme, then re-skin the window in place. Theme clicks are coalesced:
# while one atomic config write is running, only the newest requested name remains.
# This prevents six rapid clicks from racing six workers and letting an older worker
# overwrite the user's final selection.
function Start-NextTheme {
    if ([string]::IsNullOrWhiteSpace([string]$script:ThemePending)) {
        $script:ThemeJobActive = $false
        return
    }
    $next = [string]$script:ThemePending
    $script:ThemePending = $null
    $script:ThemeJobActive = $true
    $started = Start-Worker -Arguments @('--set-theme', $next) -Kind 'theme' -OnDone {
        $script:ThemeJobActive = $false
        if (-not [string]::IsNullOrWhiteSpace([string]$script:ThemePending)) {
            Start-NextTheme
            return
        }
        # Re-skin what is already on screen. A theme pick is a user gesture, but
        # it is not a request to UN-MINIMIZE: if the user parked the window, the
        # new theme is rendered in place and they see it when they open it next.
        # Show-Detail here would restore and raise a window they deliberately hid.
        if (Test-DetailOpen) {
            [void](Update-DetailContent)
        } else {
            Update-Display -ShowDetail
        }
        Update-ThemeChecks
    }
    if (-not $started) {
        $script:ThemeJobActive = $false
        Start-NextTheme
    }
}

function Set-Theme {
    param([string]$Name)
    $script:ThemePending = $Name
    if (-not $script:ThemeJobActive) { Start-NextTheme }
}

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$refreshItem = $menu.Items.Add('Refresh now')
$refreshItem.add_Click({ Update-Display -Refresh -ShowDetail })

# Theme submenu. Two designs remain (D-49); both are fully rendered, so there is
# no longer a "colour only" section to disclose.
$script:ThemeItems = New-Object System.Collections.ArrayList
$themeRoot = New-Object System.Windows.Forms.ToolStripMenuItem 'Theme'

function Add-ThemeItem {
    param($Root, [string]$Id, [string]$Label)
    $mi = New-Object System.Windows.Forms.ToolStripMenuItem $Label
    $mi.Tag = $Id
    $mi.add_Click({ param($src, $e) Set-Theme ([string]$src.Tag) })
    [void]$Root.DropDownItems.Add($mi)
    [void]$script:ThemeItems.Add($mi)
}

Add-ThemeItem $themeRoot 'phosphor' 'HUD (터미널)'
Add-ThemeItem $themeRoot 'surfacer' 'Surfacer (빅넘버)'
Add-ThemeItem $themeRoot 'mini' 'Mini (구석에 상주)'
[void]$menu.Items.Add($themeRoot)
Update-ThemeChecks

# Always on Top: applies to the form instantly (UI thread), persists via the
# worker in the background. UI is source of truth for the visual state; on
# worker failure the config just stays stale and the next launch reverts,
# which is visible and recoverable rather than silently divergent.
# Overlay mode: tiny one-line status strip instead of the full board. The
# toggle must work even when the WebView2 host is not warm yet, so the click
# initialises it on demand (same path Show-Detail takes).
$script:OverlayItem = New-Object System.Windows.Forms.ToolStripMenuItem 'Overlay (미니 상태바)'
$script:OverlayItem.CheckOnClick = $true
$script:OverlayItem.Checked = (Get-OverlayFlag)
$script:OverlayItem.add_Click({
    param($src, $e)
    $on = [bool]$src.Checked
    if ($on -and -not (Test-DetailHostReady)) {
        if (-not (Initialize-DetailHost) -or -not (Wait-DetailHostReady)) {
            $src.Checked = $false
            Show-DetailFallback $script:WV2Failure
            return
        }
    }
    if (Test-DetailHostReady) { Set-OverlayMode $on }
    Save-OverlayFlag $on
})
[void]$menu.Items.Add($script:OverlayItem)

$script:AotItem = New-Object System.Windows.Forms.ToolStripMenuItem 'Always on Top'
$script:AotItem.CheckOnClick = $true
$script:AotItem.Checked = (Get-AlwaysOnTop)
Set-DetailTopMost $script:AotItem.Checked
$script:AotItem.add_Click({
    param($src, $e)
    $on = [bool]$src.Checked
    Set-DetailTopMost $on
    $flag = 'off'; if ($on) { $flag = 'on' }
    [void](Start-Worker -Arguments @('--set-always-on-top', $flag) -Kind 'config')
})
[void]$menu.Items.Add($script:AotItem)

$exitItem = $menu.Items.Add('Exit')
$exitItem.add_Click({ $icon.Visible = $false; [System.Windows.Forms.Application]::Exit() })
$icon.ContextMenuStrip = $menu

# Left-click shows the window. It is modeless now, so a second click just brings
# the existing window forward instead of stacking or blocking.
$script:LastDetailClosed = [DateTime]::MinValue

$icon.add_MouseUp({
    param($src, $e)
    if ($e.Button -ne [System.Windows.Forms.MouseButtons]::Left) { return }
    if (([DateTime]::Now - $script:LastDetailClosed).TotalMilliseconds -lt 600) { return }
    Update-Display -Refresh -ShowDetail
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $PollMs
$timer.add_Tick({ Update-Display -Refresh })
$timer.Start()

# Visual-verification hook: refresh synchronously, render once, screenshot, exit.
# Only this path may block, because it runs before the message loop starts.
if ($ShotPath) {
    $argList = @()
    foreach ($a in ($WorkerArguments -split ' ')) { if ($a) { $argList += $a } }
    $argList += '--refresh'
    try { & $WorkerExecutable @argList 2>&1 | Out-Null } catch {}

    # Reproduce the real startup route: a persisted overlay navigates directly
    # from the prewarmed off-screen host, never through detail.html first.
    if (Get-OverlayFlag) {
        if ((Initialize-DetailHost) -and (Wait-DetailHostReady)) {
            Set-OverlayMode $true
        }
    } else {
        Show-Detail (Read-Cache)
    }
    # CapturePreviewAsync grabs the browser surface directly, so the screenshot no
    # longer depends on the window being unobscured. CopyFromScreen produced a
    # black image when the handle was not composited (shots/037).
    if (Test-DetailHostReady) {
        $t0 = [DateTime]::Now
        while ((([DateTime]::Now - $t0).TotalMilliseconds) -lt 900) {
            [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 20
        }
        $ms = New-Object System.IO.MemoryStream
        $task = $script:WV2Control.CoreWebView2.CapturePreviewAsync(
            [Microsoft.Web.WebView2.Core.CoreWebView2CapturePreviewImageFormat]::Png, $ms)
        $dl = [DateTime]::Now.AddSeconds(20)
        while (-not $task.IsCompleted -and [DateTime]::Now -lt $dl) {
            [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 10
        }
        if ($task.IsCompleted) {
            [System.IO.File]::WriteAllBytes($ShotPath, $ms.ToArray())
        }
        $ms.Dispose()
    }
    exit 0
}

# Initial paint: request a refresh without blocking. A manual launch also opens
# the native window immediately; the background job fills it when it completes.
Update-Display -Refresh
if ($OpenDetail) {
    Update-Display -ShowDetail
} else {
    # Prewarm the WebView2 host off-screen NOW so the first left-click pays only
    # the ~69 ms reposition, not the 4.6 s cold start + up-to-15 s ready wait on
    # the UI thread. Initialize-DetailHost builds the form far off-screen and kicks
    # CoreWebView2 init asynchronously, returning immediately; it is a no-op if the
    # runtime is absent (that path degrades to the fallback screen on click). This
    # is the prewarm webview2-host.ps1's header promises but nothing was invoking.
    [void](Initialize-DetailHost)
    # Prewarming leaves the surface BLANK until something renders into it. Fill it
    # once, off-screen, as soon as the engine is ready: the window then always has
    # a real page behind it, so it can never be shown empty. Done on a one-shot
    # timer because CoreWebView2 init is async - blocking here would reintroduce
    # the UI-thread stall the prewarm exists to remove.
    $script:PrimeTimer = New-Object System.Windows.Forms.Timer
    $script:PrimeTimer.Interval = 500
    $script:PrimeTries = 0
    $script:PrimeTimer.add_Tick({
        $script:PrimeTries++
        if (Test-DetailHostReady) {
            # Overlay survives restarts. Do NOT prime detail.html first here:
            # moving the dirty off-screen form on-screen flushes overlay.html,
            # and Set-OverlayMode would immediately Reload that same URI while
            # its first navigation is still in flight, leaving a blank surface.
            # The mode switch already performs the one navigation we need.
            if ((Get-OverlayFlag) -and -not (Test-OverlayMode)) {
                try {
                    Set-OverlayMode $true
                } catch {
                    # Never let the HUD nicety take the whole tray down; log and
                    # fall back to normal-window mode.
                    try {
                        ("overlay apply failed: " + $_.Exception.ToString()) |
                            Out-File -Append -Encoding utf8 (Join-Path $AppHome 'tray-error.log')
                    } catch { }
                }
            } else {
                [void](Update-DetailContent)
            }
            $script:PrimeTimer.Stop()
        } elseif ($script:PrimeTries -ge 60) {   # 30 s, then give up quietly
            $script:PrimeTimer.Stop()
        }
    })
    $script:PrimeTimer.Start()
}
[System.Windows.Forms.Application]::Run()
$timer.Stop()
$icon.Dispose()
