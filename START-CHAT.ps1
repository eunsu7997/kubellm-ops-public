$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$runtime = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$listener = Get-NetTCPConnection -LocalPort 18004 -State Listen -ErrorAction SilentlyContinue
if ($listener) { throw 'Port 18004 is already in use. Verify the existing local chat server before opening http://127.0.0.1:18004/.' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','scripts.ops.chat_demo:app','--host','127.0.0.1','--port','18004','--workers','1') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput "$runtime\chat-$stamp.out.log" -RedirectStandardError "$runtime\chat-$stamp.err.log" -PassThru | Select-Object Id
Write-Host 'Chat URL: http://127.0.0.1:18004/'
