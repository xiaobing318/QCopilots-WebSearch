$ErrorActionPreference = 'Stop'
if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    throw 'Install Python 3.12 or newer with the Python launcher.'
}
# An unquoted list such as `--Stages configure,build,test` arrives as an array
# when this script is invoked from a PowerShell prompt. Rejoin it so Python
# receives one comma-separated argument, as it does from -File or cmd.exe.
$forward = @(foreach ($argument in $args) {
    if ($argument -is [array]) { $argument -join ',' } else { $argument }
})
# Under 2>&1, Windows PowerShell turns native stderr lines into errors. Rely on
# the exit code instead, so diagnostics on stderr cannot abort the wrapper.
$ErrorActionPreference = 'Continue'
& py -3 (Join-Path $PSScriptRoot 'pipeline.py') @forward
exit $LASTEXITCODE
