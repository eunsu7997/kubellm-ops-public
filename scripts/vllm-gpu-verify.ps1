<#
KubeLLM-Ops: run vLLM's OpenAI-compatible server on the real GPU via Docker, and
verify it end to end (model load, chat completion, /metrics).

This must be run on the user's PC (Docker Desktop with GPU access) - Claude Code's
sandbox has no docker/GPU access, so it cannot run this instead. Run from the repo
root (<PROJECT_ROOT>).

Before running: close GPU-heavy apps (browser, games) to free up VRAM. The GPU
check showed an 8GB RTX 4060 with about 4.6GB already used by other programs at
that time.

Model: Qwen/Qwen2.5-0.5B-Instruct
- Reason: a small (not gated) instruct model that still holds a real conversation.
  Weights are roughly 1GB in bf16, which leaves headroom on an 8GB card even while
  sharing VRAM with other running programs.
- --gpu-memory-utilization 0.4 is a conservative cap (about 3.2GB of the 8GB) to
  avoid an out-of-memory error.

The vllm/vllm-openai image is several GB, and the model is downloaded from
Hugging Face Hub on first run, so the first run can take minutes depending on
network speed - that is expected, not a failure.

Usage:
    .\scripts\vllm-gpu-verify.ps1

Note: this file intentionally contains no non-ASCII characters (not even in
comments). A previous script in this repo broke the PowerShell parser because a
Korean comment, read without a byte-order mark, got misdecoded under the system
codepage in a way that swallowed a line break and merged two lines together.
Keeping this file pure ASCII avoids that failure mode entirely.
#>

$ErrorActionPreference = "Continue"
$evidenceDir = Join-Path $PSScriptRoot ("..\evidence\gpu-vllm-local\run-" + (Get-Date -Format "yyyyMMddTHHmmssfff") + "-" + [guid]::NewGuid().ToString("N"))
$evidenceDir = (Resolve-Path (New-Item -ItemType Directory -Force -Path $evidenceDir)).Path

$containerName = "kubellm-vllm"
$hostPort = 8002
$model = "Qwen/Qwen2.5-0.5B-Instruct"

function Save-Text {
    param([string]$Path, [string]$Title, [string]$Body)
    $lines = @()
    $lines += "=== $Title ==="
    $lines += "Timestamp: $([DateTimeOffset]::Now.ToString("o"))"
    $lines += ""
    $lines += $Body
    ($lines -join "`r`n") | Out-File -FilePath (Join-Path $evidenceDir $Path) -Encoding utf8
    Write-Host "[$Path] saved"
}

# Refuse replacement and fail closed if inventory is unavailable.
$existingNames = docker ps -a --format '{{.Names}}' 2>&1
if ($LASTEXITCODE -ne 0) { throw "Docker inventory unavailable; no launch attempted." }
if ($existingNames -contains $containerName) { throw "Container exists; no launch attempted." }

Write-Host "`n=== Step 1: Start vLLM container (model: $model) ===`n"
$runOutput = docker run -d --name $containerName --gpus all -p "127.0.0.1:${hostPort}:8000" --ipc=host `
    vllm/vllm-openai@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90 --model $model --gpu-memory-utilization 0.4 --max-model-len 2048 2>&1 | Out-String
$runExitCode = $LASTEXITCODE
Save-Text "03_vllm-container-start.txt" "docker run vllm/vllm-openai (detached)" $runOutput

if ($runExitCode -ne 0) {
    Write-Host "[ABORT] docker run failed - see 03_vllm-container-start.txt" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== Step 2: Wait for model to load (polling /v1/models, up to 20 min) ===`n"
$deadline = (Get-Date).AddMinutes(20)
$modelsReady = $false
$models = $null
do {
    Start-Sleep -Seconds 10
    try {
        $models = Invoke-RestMethod -Uri "http://localhost:${hostPort}/v1/models" -Method GET -TimeoutSec 5
        if ($models.data -and $models.data.Count -gt 0) {
            $modelsReady = $true
        }
    } catch {
        # Not ready yet - keep polling.
    }
    $isRunning = docker ps --filter "name=$containerName" --filter "status=running" -q
    if (-not $isRunning) {
        Write-Host "[ABORT] container exited early" -ForegroundColor Red
        break
    }
} while ((-not $modelsReady) -and ((Get-Date) -lt $deadline))

$logsTail = docker logs $containerName --tail 80 2>&1 | Out-String
if ($modelsReady) {
    $modelsResponseText = ($models | ConvertTo-Json -Depth 5)
} else {
    $modelsResponseText = "NOT READY within timeout (or container exited) - see container logs below"
}
$body04 = @()
$body04 += "GET /v1/models response:"
$body04 += $modelsResponseText
$body04 += ""
$body04 += "--- docker logs $containerName --tail 80 ---"
$body04 += $logsTail
Save-Text "04_vllm-model-loaded.txt" "vLLM model load status (GET /v1/models + container logs)" ($body04 -join "`r`n")

if (-not $modelsReady) {
    Write-Host "[ABORT] model did not become ready - see 04_vllm-model-loaded.txt" -ForegroundColor Red
    exit 1
}

Write-Host "`n=== Step 3: Real chat completion request ===`n"
$chatBody = @{
    model      = $model
    messages   = @(@{ role = "user"; content = "In one short sentence, what is Kubernetes?" })
    max_tokens = 64
} | ConvertTo-Json -Depth 5

try {
    $chatResp = Invoke-RestMethod -Uri "http://localhost:${hostPort}/v1/chat/completions" -Method POST -ContentType "application/json" -Body $chatBody -TimeoutSec 60
    $chatText = ($chatResp | ConvertTo-Json -Depth 5)
} catch {
    $chatText = "Request failed: $($_.Exception.Message)"
}
Save-Text "05_vllm-chat-response.txt" "POST /v1/chat/completions (real GPU inference)" $chatText

Write-Host "`n=== Step 4: /metrics ===`n"
try {
    $metricsText = (Invoke-WebRequest -Uri "http://localhost:${hostPort}/metrics" -UseBasicParsing -TimeoutSec 10).Content
} catch {
    $metricsText = "Request failed: $($_.Exception.Message)"
}
Save-Text "06_vllm-metrics.txt" "GET /metrics (raw, full)" $metricsText

Write-Host "`n=== Done: check evidence/gpu-vllm-local/ ===`n"
Get-ChildItem $evidenceDir | Select-Object Name, Length
Write-Host "`nContainer '$containerName' is still running (so you can inspect it)."
Write-Host "This script does not stop or replace services."
