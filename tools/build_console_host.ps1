$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$sdk = Join-Path $root 'ui\webview2'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
& $compiler /nologo /target:winexe /platform:x64 /optimize+ `
    "/out:$root\ui\console-credit-host.exe" `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll `
    /reference:System.Web.Extensions.dll /reference:System.Core.dll `
    "/reference:$sdk\Microsoft.Web.WebView2.Core.dll" `
    "/reference:$sdk\Microsoft.Web.WebView2.WinForms.dll" `
    "$root\ui\console-credit-host.cs"
if ($LASTEXITCODE -ne 0) { throw 'Console host build failed' }
