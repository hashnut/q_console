param([Parameter(Mandatory = $true)][string]$AppHome)
$ErrorActionPreference = 'Stop'
$AppHome = (Resolve-Path -LiteralPath $AppHome).Path
$WorkerExecutable = 'unused'
$WorkerArguments = 'unused'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'ui/webview2-host.ps1')
Add-Type -TypeDefinition @'
public static class QAFocus {
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    public static extern System.IntPtr GetForegroundWindow();
}
'@
function Wait-Task($Task) {
    $deadline = [DateTime]::Now.AddSeconds(20)
    while (-not $Task.IsCompleted -and [DateTime]::Now -lt $deadline) {
        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 10
    }
    if (-not $Task.IsCompleted) { throw 'WebView2 task timed out' }
    return $Task.GetAwaiter().GetResult()
}
try {
    if (-not (Initialize-DetailHost) -or -not (Wait-DetailHostReady)) {
        throw "WebView2 init failed: $script:WV2Failure"
    }
    $script:qaActivated = $false
    $script:WV2Form.add_Activated({
        param($sender, $args)
        if ([QAFocus]::GetForegroundWindow() -eq $sender.Handle) { $script:qaActivated = $true }
    })
    $noActivate = {
        param($sender, $args)
        $style = [UV.WinStyle]::GetWindowLong($sender.Handle, -20)
        [void][UV.WinStyle]::SetWindowLong($sender.Handle, -20, ($style -bor 0x08000000))
    }
    $script:WV2Form.add_HandleCreated($noActivate)
    & $noActivate $script:WV2Form $null
    $script:WV2Form.Enabled = $false
    $script:WV2Overlay = $true
    $script:WV2Form.FormBorderStyle = 'None'
    $script:WV2Form.MinimumSize = New-Object Drawing.Size 1, 1
    foreach ($file in (Get-ChildItem -LiteralPath $AppHome -Filter '*.html')) {
        $html = [IO.File]::ReadAllText($file.FullName)
        $size = [regex]::Match($html, "data-w='(\d+)'\s+data-h='(\d+)'")
        $script:WV2Form.ClientSize = New-Object Drawing.Size ([int]$size.Groups[1].Value), ([int]$size.Groups[2].Value)
        # Keep the real renderer off-screen; no tray mutex or user's profile.
        $script:qaNavigated = $false
        $handler = { param($sender, $eventArgs) $script:qaNavigated = $eventArgs.IsSuccess }
        $script:WV2Control.CoreWebView2.add_NavigationCompleted($handler)
        $script:WV2Control.CoreWebView2.Navigate(([Uri]$file.FullName).AbsoluteUri)
        $deadline = [DateTime]::Now.AddSeconds(20)
        while (-not $script:qaNavigated -and [DateTime]::Now -lt $deadline) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 10
        }
        $script:WV2Control.CoreWebView2.remove_NavigationCompleted($handler)
        if (-not $script:qaNavigated) { throw "Navigation failed: $($file.Name)" }
        $watch = [Diagnostics.Stopwatch]::StartNew()
        while ($watch.ElapsedMilliseconds -lt 250) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 10
        }
        $json = Wait-Task ($script:WV2Control.CoreWebView2.ExecuteScriptAsync(@'
JSON.stringify((()=>{const s=document.querySelector('#stage'),b=s.getBoundingClientRect();
return {text:s.innerText,width:b.width,scroll:s.scrollWidth,client:s.clientWidth,
clipped:[...s.querySelectorAll('span')].filter(e=>{const r=e.getBoundingClientRect();return r.right>b.right-4||r.left<b.left||r.bottom>b.bottom||r.top<b.top;}).map(e=>e.textContent)}})())
'@))
        $metrics = ($json | ConvertFrom-Json) | ConvertFrom-Json
        Write-Output "$($file.Name): $($metrics | ConvertTo-Json -Compress)"
        $stream = New-Object IO.MemoryStream
        try {
            Wait-Task ($script:WV2Control.CoreWebView2.CapturePreviewAsync(
                [Microsoft.Web.WebView2.Core.CoreWebView2CapturePreviewImageFormat]::Png, $stream)) | Out-Null
            [IO.File]::WriteAllBytes((Join-Path $AppHome ($file.BaseName + '.png')), $stream.ToArray())
        } finally { $stream.Dispose() }
        if ($metrics.clipped.Count -gt 0 -or $metrics.scroll -gt $metrics.client) {
            throw "Clipped overlay: $($file.Name)"
        }
        if ($script:qaActivated) { throw 'QA activated a window' }
    }
    Write-Output 'PASS: WebView2 overlay text, bounds, capture, no activation'
} finally {
    if ($script:WV2Form) { $script:WV2Form.Dispose() }
}
