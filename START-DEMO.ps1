param([switch]$Replay, [switch]$NoOpen)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (!(Test-Path $python)) { throw 'Missing .venv Python. See docs/DEMO-TOMORROW.md.' }
$demoArgs = @('scripts/ops/demo.py')
if ($Replay) { $demoArgs += '--replay' }
if ($NoOpen) { $demoArgs += '--no-open' }
& $python @demoArgs
exit $LASTEXITCODE
