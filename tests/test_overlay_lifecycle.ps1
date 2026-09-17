param([Parameter(Mandatory = $true)][string]$AppHome)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$WorkerExecutable = 'unused'
$WorkerArguments = 'unused'
$root = Split-Path $PSScriptRoot -Parent
. (Join-Path $root 'ui/webview2-host.ps1')

Add-Type -TypeDefinition @'
public class OverlayTestNative {
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    public static extern System.IntPtr GetForegroundWindow();
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    public static extern System.IntPtr GetWindow(System.IntPtr hwnd, uint command);
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    public static extern bool IsWindowVisible(System.IntPtr hwnd);

    public static bool IsAbove(System.IntPtr upper, System.IntPtr lower) {
        // GW_HWNDPREV walks towards the top of the actual native Z order.
        for (var h = GetWindow(lower, 3); h != System.IntPtr.Zero; h = GetWindow(h, 3)) {
            if (h == upper) return true;
        }
        return false;
    }
}
'@
function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Pump-Events([int]$Milliseconds = 100) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.ElapsedMilliseconds -lt $Milliseconds) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 10
    }
}
function Assert-Focus([string]$Stage) {
    $actual = [OverlayTestNative]::GetForegroundWindow()
    # The user may switch apps while QA runs; only OUR activation is a failure.
    Assert-True (-not $script:testActivated -and $actual -ne $f.Handle) "QA took foreground focus: $Stage"
}

# Exercise real HWND state without showing pixels or using input. No WebView2
# profile, credentials, live tray mutex, or user's windows are touched.
$f = New-Object UV.DetailForm
$script:testActivated = $false
$f.add_Activated({ $script:testActivated = $true })
$script:WV2Form = $f
$script:WV2Overlay = $true
$script:WV2Open = $true
$f.FormBorderStyle = 'None'
$f.ShowInTaskbar = $false
$f.StartPosition = 'Manual'
$f.Opacity = 0
$f.ClientSize = New-Object System.Drawing.Size 440, 32
$work = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
$f.Location = New-Object System.Drawing.Point ($work.Left + 30), ($work.Top + 30)
try {
    Set-AltTabHidden $f $true
    $f.Show()
    Pump-Events
    Assert-Focus 'initial show'
    $bounds = $f.Bounds
    # An overlay minimized by Windows must be restored by the user-open path.
    $f.WindowState = 'Minimized'
    Pump-Events
    Assert-Focus 'minimize'
    Assert-True ($f.WindowState -eq 'Minimized') 'Minimize precondition failed'
    Show-DetailHostWindow
    Pump-Events
    Assert-Focus 'user restore'
    Assert-True ($f.WindowState -eq 'Normal') 'Overlay stayed minimized after reopening'
    Assert-True ($f.Bounds -eq $bounds) 'Restoring lost the dragged position/size'

    # Run the actual recovery timer from tray.ps1, excluding tray startup.
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        (Join-Path $root 'ui/tray.ps1'), [ref]$tokens, [ref]$errors)
    Assert-True ($errors.Count -eq 0) 'Tray script has parse errors'
    foreach ($statement in $ast.EndBlock.Statements) {
        if ($statement.Extent.Text -match '^\$overlayTimer') {
            . ([scriptblock]::Create($statement.Extent.Text))
            if ($statement.Extent.Text -eq '$overlayTimer.Start()') { break }
        }
    }
    $f.WindowState = 'Minimized'
    Assert-Focus 'before timer'
    Pump-Events 2300
    Assert-Focus 'timer restore'
    Assert-True ($f.WindowState -eq 'Normal') 'Periodic recovery did not restore overlay'

    # A second topmost HWND can cover an overlay whose TOPMOST bit is still set.
    # Testing just that bit misses the regression: assert the real Z order.
    $cover = New-Object UV.DetailForm
    $cover.FormBorderStyle = 'None'
    $cover.ShowInTaskbar = $false
    $cover.StartPosition = 'Manual'
    $cover.Bounds = $f.Bounds
    $cover.Opacity = 0
    Set-AltTabHidden $cover $true
    $cover.Show()
    [void][UV.WinStyle]::SetWindowPos($cover.Handle, [IntPtr](-1), 0, 0, 0, 0, 0x13)
    Assert-True ([OverlayTestNative]::IsAbove($cover.Handle, $f.Handle)) 'Covering-window precondition failed'
    Assert-True (([UV.WinStyle]::GetWindowLong($f.Handle, -20) -band 8) -ne 0) 'Overlay lost TOPMOST before test'
    Pump-Events 2300
    Assert-Focus 'timer Z-order repair'
    Assert-True ([OverlayTestNative]::IsAbove($f.Handle, $cover.Handle)) 'Topmost overlay stayed behind another topmost window'
    Assert-True ($f.Bounds -eq $bounds) 'Z-order recovery moved or resized the strip'
    # Repeated foreground changes must not consume a one-shot repair.
    1..3 | ForEach-Object {
        [void][UV.WinStyle]::SetWindowPos($cover.Handle, [IntPtr](-1), 0, 0, 0, 0, 0x13)
        Repair-OverlayVisibility
        Assert-True ([OverlayTestNative]::IsAbove($f.Handle, $cover.Handle)) 'Repeated Z-order recovery failed'
    }
    $cover.Dispose()
    $overlayTimer.Stop()

    [void][UV.WinStyle]::SetWindowPos($f.Handle, [IntPtr](-2), 0, 0, 0, 0, 0x13)
    Assert-True (([UV.WinStyle]::GetWindowLong($f.Handle, -20) -band 8) -eq 0) 'Topmost-demotion precondition failed'
    Repair-OverlayVisibility
    Assert-True (([UV.WinStyle]::GetWindowLong($f.Handle, -20) -band 8) -ne 0) 'Native topmost demotion was not repaired'
    # Reproduce stale style/group state from WinForms HWND/style recreation.
    $styles = [UV.WinStyle]::GetWindowLong($f.Handle, -20)
    [void][UV.WinStyle]::SetWindowLong($f.Handle, -20, ($styles -band (-bnot 8)))
    Repair-OverlayVisibility
    Assert-True (([UV.WinStyle]::GetWindowLong($f.Handle, -20) -band 8) -ne 0) 'Stale native topmost membership was not repaired'
    [void][UV.WinStyle]::ShowWindow($f.Handle, 0)
    Assert-True (-not [OverlayTestNative]::IsWindowVisible($f.Handle)) 'Native-hide precondition failed'
    Repair-OverlayVisibility
    Assert-True ([OverlayTestNative]::IsWindowVisible($f.Handle)) 'Native-hidden overlay was not restored'
    $f.Hide()
    Repair-OverlayVisibility
    Assert-True $f.Visible 'Externally hidden overlay was not restored'
    $f.Location = New-Object System.Drawing.Point 20000, 20000
    Assert-True (-not (Test-DetailSurfaceLive)) 'Off-screen surface was considered live'
    Repair-OverlayVisibility
    $screen = [System.Windows.Forms.Screen]::FromRectangle($f.Bounds)
    Assert-True ($screen.WorkingArea.Contains($f.Bounds)) 'Disconnected-monitor position was not repaired'
    $bounds = $f.Bounds
    1..5 | ForEach-Object { Repair-OverlayVisibility }
    Assert-True ($f.Bounds -eq $bounds) 'Recovery moved a valid overlay'

    $overlayPath = Join-Path $AppHome 'overlay.html'
    [IO.File]::WriteAllText($overlayPath, "<body data-w='240' data-h='32'>Codex</body>")
    $position = $f.Location
    Resize-DetailToDocument $overlayPath
    Assert-True ($f.ClientSize.Width -eq 240) 'Overlay did not shrink with the selected items'
    Assert-True ($f.Location -eq $position) 'Changing overlay items moved the strip'

    Set-DetailTopMost $false
    Assert-True (([UV.WinStyle]::GetWindowLong($f.Handle, -20) -band 8) -ne 0) 'Dashboard setting unpinned overlay'
    Assert-True (-not $script:WV2AlwaysOnTop) 'Dashboard preference was not remembered'

    # A deferred refresh is replayed exactly once after restoring the surface.
    $script:navigations = 0
    function Test-DetailHostReady { return $true }
    function Invoke-DetailNavigate {
        param([string]$Path)
        $script:navigations++
        $script:WV2Dirty = $false
    }
    $script:WV2Dirty = $true
    $f.WindowState = 'Minimized'
    Repair-OverlayVisibility
    Repair-OverlayVisibility
    Assert-True ($script:navigations -eq 1) 'Deferred refresh was lost or replayed repeatedly'

    Close-DetailHost
    Repair-OverlayVisibility
    Assert-True (-not $f.Visible) 'Recovery reopened an explicitly closed overlay'
    $script:WV2Overlay = $false
    $script:WV2Open = $true
    $f.WindowState = 'Minimized'
    Repair-OverlayVisibility
    Assert-True ($f.WindowState -eq 'Minimized') 'Recovery restored a minimized dashboard'
    Assert-Focus 'final overlay recovery'
    $f.Dispose()
    $f = New-Object UV.DetailForm
    $script:WV2Form = $f
    Set-DetailTopMost $false
    Assert-True (-not $f.TopMost) 'Dashboard topmost preference was ignored'
    Assert-Focus 'final'
    Write-Output 'PASS: overlay restore, timer Z order, repeated covering, native hide/demotion, stale styles, monitor recovery, position, deferred refresh, explicit close, dashboard, focus'
} finally {
    if ($cover) { $cover.Dispose() }
    if ($overlayTimer) { $overlayTimer.Stop(); $overlayTimer.Dispose() }
    $f.Dispose()
}
