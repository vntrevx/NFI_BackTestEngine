param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,

    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
uv run --extra benchmark nfi-bte benchmark $Manifest --output $Output
exit $LASTEXITCODE
