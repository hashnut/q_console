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
    $overlayTimer.Stop()

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
    Write-Output 'PASS: overlay restore, timer, monitor recovery, position, topmost, deferred refresh, explicit close, dashboard, focus'
} finally {
    if ($overlayTimer) { $overlayTimer.Stop(); $overlayTimer.Dispose() }
    $f.Dispose()
}
