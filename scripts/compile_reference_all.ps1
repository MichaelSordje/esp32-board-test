#requires -Version 5.1

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
& (Join-Path $ScriptDir "compile_all.ps1") -Command "compile-reference-all"
exit $LASTEXITCODE
