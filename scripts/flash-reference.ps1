#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$Port = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Requirements = Join-Path $Root "requirements.txt"

function Find-Python {
    foreach ($Candidate in @("py", "python", "python3")) {
        try {
            if ($Candidate -eq "py") {
                & py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" *> $null
                if ($LASTEXITCODE -eq 0) { return @{ Command = "py"; Args = @("-3") } }
            }
            else {
                & $Candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)" *> $null
                if ($LASTEXITCODE -eq 0) { return @{ Command = $Candidate; Args = @() } }
            }
        }
        catch {}
    }
    throw "Python 3.10 or newer was not found."
}

Set-Location $Root
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Setting up local test tools ..."
    $Python = Find-Python
    & $Python.Command @($Python.Args) -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { throw "The Python environment could not be created." }
}

& $VenvPython -m pip install --disable-pip-version-check --quiet -r $Requirements
if ($LASTEXITCODE -ne 0) { throw "The required test tools could not be installed." }

$FlashArgs = @((Join-Path $Root "tools\flash_reference.py"))
if ($Port) { $FlashArgs += @("--port", $Port) }
& $VenvPython @FlashArgs
exit $LASTEXITCODE
