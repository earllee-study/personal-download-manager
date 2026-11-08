# Sign and verify Windows binaries with an Authenticode certificate.
#
# CI calls this only when WINDOWS_CERT_PFX_BASE64 is configured; without a
# certificate the release workflow warns and publishes unsigned artifacts.
#
# Example:
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\sign_windows.ps1 `
#     -PfxPath C:\temp\personal_download_manager.pfx -Password $env:WINDOWS_CERT_PASSWORD `
#     -Files dist\pdm-setup-0.2.0.exe,dist\pdm-gui\pdm-gui.exe
#
# The password is supplied as an argument (never written to a file or echoed).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PfxPath,

    [Parameter(Mandatory = $true)]
    [string]$Password,

    [Parameter(Mandatory = $true)]
    [string[]]$Files,

    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PfxPath -PathType Leaf)) {
    throw "PFX file not found: $PfxPath"
}

$patterns = @(
    "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe",
    "$env:ProgramFiles\Windows Kits\10\bin\*\x64\signtool.exe"
)
$signtool = Get-ChildItem -Path $patterns -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $signtool) {
    throw "signtool.exe not found (install the Windows SDK signing tools)"
}

$signed = 0
foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw "file to sign not found: $file"
    }
    Write-Host "Signing $file"
    & $signtool sign /f $PfxPath /p $Password /fd SHA256 /tr $TimestampUrl /td SHA256 $file
    if ($LASTEXITCODE -ne 0) {
        throw "signtool sign failed for $file (exit $LASTEXITCODE)"
    }
    & $signtool verify /pa /v $file | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "signtool verify failed for $file (exit $LASTEXITCODE)"
    }
    $signed += 1
}

Write-Host "Signed and verified $signed file(s)."
