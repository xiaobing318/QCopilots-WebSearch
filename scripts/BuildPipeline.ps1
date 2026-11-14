$ErrorActionPreference = 'Stop'
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Install Python 3.12 or newer with the Python launcher.'
}
& py -3 (Join-Path $PSScriptRoot 'pipeline.py') @args
exit $LASTEXITCODE
