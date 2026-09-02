#requires -Version 5.1

[CmdletBinding()]
param(
    [int]$DurationMinutes = 0,
    [string]$Port = "",
    [string]$ReferencePort = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

function Test-PythonVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [string[]]$Arguments = @()
    )

    try {
        & $Command @Arguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-PythonCommand {
    $Candidates = @(
        @{ Command = "py"; Args = @("-3") },
        @{ Command = "python"; Args = @() },
        @{ Command = "python3"; Args = @() }
    )

    foreach ($Candidate in $Candidates) {
        $Command = $Candidate.Command
        $CommandArgs = @($Candidate.Args)

        if (Test-PythonVersion -Command $Command -Arguments $CommandArgs) {
            return $Candidate
        }
    }

    throw "Python 3.10 or newer was not found. Install Python 3.10+ and run scripts\start-test.cmd again."
}

function Install-TestRequirementsIfNeeded {
    param(
        [Parameter(Mandatory = $true)]
        [string]$VenvPython,

        [Parameter(Mandatory = $true)]
        [string]$RequirementsPath
    )

    $HashPath = Join-Path $Root ".venv\.requirements.sha256"
    $CurrentHash = (Get-FileHash -LiteralPath $RequirementsPath -Algorithm SHA256).Hash
    $InstalledHash = ""

    if (Test-Path -LiteralPath $HashPath) {
        $InstalledHash = (Get-Content -LiteralPath $HashPath -Raw -Encoding ASCII).Trim()
    }

    if ($InstalledHash -eq $CurrentHash) {
        return
    }

    Write-Host "Installing/updating required local test tools ..."
    & $VenvPython -m pip install --disable-pip-version-check --quiet -r $RequirementsPath

    if ($LASTEXITCODE -ne 0) {
        throw "The required test tools could not be installed."
    }

    Set-Content -LiteralPath $HashPath -Value $CurrentHash -Encoding ASCII
}

Write-Host ""
Write-Host "=========================================="
Write-Host " ESP32 Hardware / Quality Test"
Write-Host "=========================================="
Write-Host ""

$Python = Get-PythonCommand
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$RequirementsPath = Join-Path $Root "requirements.txt"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Setting up the local test tools on first launch ..."

    $PythonCommand = $Python.Command
    $PythonArgs = @($Python.Args)

    & $PythonCommand @PythonArgs -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "The local Python environment could not be created."
    }

    & $VenvPython -m pip install --disable-pip-version-check --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "pip could not be updated in the local Python environment."
    }
}

if (-not (Test-PythonVersion -Command $VenvPython)) {
    throw "The existing .venv uses a Python version older than 3.10. Recreate the local .venv with Python 3.10 or newer."
}

Install-TestRequirementsIfNeeded -VenvPython $VenvPython -RequirementsPath $RequirementsPath

$RunnerArgs = @((Join-Path $Root "tools\run_test_artifact.py"))

if ($DurationMinutes -gt 0) {
    $RunnerArgs += @("--duration-minutes", [string]$DurationMinutes)
}

if ($Port) {
    $RunnerArgs += @("--port", $Port)
}

if ($ReferencePort) {
    $RunnerArgs += @("--reference-port", $ReferencePort)
}

& $VenvPython @RunnerArgs
$ExitCode = $LASTEXITCODE

exit $ExitCode
