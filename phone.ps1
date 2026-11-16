$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw 'Create the Python environment with setup.ps1 first.'
}
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($MyInvocation.ExpectingInput) {
        $input | & $pythonExe -X utf8 -m mhxy_control.phone @args
    } else {
        & $pythonExe -X utf8 -m mhxy_control.phone @args
    }
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
