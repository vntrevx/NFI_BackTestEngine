<#
.SYNOPSIS
Install the latest NFI Backtest Engine release on 64-bit Windows.

.DESCRIPTION
The installer selects the Windows wheel from the latest GitHub release, verifies the
SHA-256 digest published by GitHub, and installs the CLI into an isolated uv tool
environment. If uv is missing, its official standalone installer is used first.
#>

[CmdletBinding()]
param(
    [string]$Version = "latest",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Repository = "vntrevx/NFI_BackTestEngine"
$Headers = @{
    Accept = "application/vnd.github+json"
    "User-Agent" = "nfi-backtest-engine-installer"
    "X-GitHub-Api-Version" = "2022-11-28"
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "NFI Backtest Engine currently requires 64-bit Windows."
}

$ReleaseEndpoint = if ($Version -eq "latest") {
    "https://api.github.com/repos/$Repository/releases/latest"
} else {
    $EncodedVersion = [Uri]::EscapeDataString($Version)
    "https://api.github.com/repos/$Repository/releases/tags/$EncodedVersion"
}

$Release = Invoke-RestMethod -Uri $ReleaseEndpoint -Headers $Headers
$Assets = @($Release.assets | Where-Object { $_.name -like "*-win_amd64.whl" })
if ($Assets.Count -ne 1) {
    throw "Expected one Windows x64 wheel in release $($Release.tag_name); found $($Assets.Count)."
}
$Asset = $Assets[0]
if (-not $Asset.digest -or -not $Asset.digest.StartsWith("sha256:")) {
    throw "Release asset $($Asset.name) does not have a published SHA-256 digest."
}

if ($DryRun) {
    Write-Output "release=$($Release.tag_name)"
    Write-Output "asset=$($Asset.name)"
    Write-Output "digest=$($Asset.digest)"
    return
}

$UvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $UvCommand) {
    Write-Host "uv was not found; installing it from the official Astral installer..."
    $UvInstaller = Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1"
    & ([scriptblock]::Create($UvInstaller))
    $UvCandidate = Join-Path $HOME ".local\bin\uv.exe"
    if (-not (Test-Path -LiteralPath $UvCandidate)) {
        throw "uv installation completed but uv.exe was not found at $UvCandidate."
    }
    $UvPath = $UvCandidate
} else {
    $UvPath = $UvCommand.Source
}

$TemporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) (
    "nfi-bte-install-" + [Guid]::NewGuid().ToString("N")
)
New-Item -ItemType Directory -Path $TemporaryDirectory | Out-Null
try {
    $WheelPath = Join-Path $TemporaryDirectory $Asset.name
    Write-Host "Downloading $($Asset.name)..."
    Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $WheelPath -Headers $Headers

    $ExpectedDigest = $Asset.digest.Substring("sha256:".Length).ToLowerInvariant()
    $ActualDigest = (Get-FileHash -LiteralPath $WheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualDigest -ne $ExpectedDigest) {
        throw "Downloaded wheel SHA-256 differs from the GitHub release digest."
    }

    & $UvPath tool install --force --python 3.12 $WheelPath
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install failed with exit code $LASTEXITCODE."
    }
    # Updating the shell path is idempotent. uv prints whether a terminal restart is
    # required, so the installer does not guess which shell the user will open next.
    & $UvPath tool update-shell
    Write-Host "Installed NFI Backtest Engine $($Release.tag_name)."
    Write-Host "Run: nfi-bte --version"
} finally {
    Remove-Item -LiteralPath $TemporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
}
