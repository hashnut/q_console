param(
    [Parameter(Mandatory=$true)][string]$AppHome,
    [Parameter(Mandatory=$true)][string]$OutputPath,
    [string]$OrgId = '',
    [switch]$Connect
)
$ErrorActionPreference = 'Stop'
try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Web.Extensions
    $sdk = Join-Path $PSScriptRoot 'webview2'
    $core = Join-Path $sdk 'Microsoft.Web.WebView2.Core.dll'
    $forms = Join-Path $sdk 'Microsoft.Web.WebView2.WinForms.dll'
    Add-Type -Path $core
    Add-Type -Path $forms
    $env:PATH = $sdk + ';' + $env:PATH
    Add-Type -Path (Join-Path $PSScriptRoot 'console-credit-host.cs') -ReferencedAssemblies @(
        'System.Windows.Forms','System.Drawing','System.Web.Extensions','System.Core',$core,$forms
    )
    [QConsole.ConsoleCreditHost]::Run($AppHome, $OutputPath, $OrgId, [bool]$Connect)
} catch {
    # Never serialize exceptions, request headers, cookies, or page contents.
    [IO.File]::WriteAllText($OutputPath, '{"status":"unavailable","error":"host_error"}')
    exit 1
}
