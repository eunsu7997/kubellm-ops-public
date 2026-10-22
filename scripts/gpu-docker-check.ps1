<#
KubeLLM-Ops: GPU + Docker Desktop(WSL2) 환경 점검 스크립트.

Claude Code 샌드박스에는 GPU/Docker가 없어 대신 실행할 수 없다. 사용자 PC에서
repo 루트(<PROJECT_ROOT>)에서 직접 실행할 것.

사용법:
    .\scripts\gpu-docker-check.ps1

이 스크립트는 다음을 확인한다:
1. 호스트에서 nvidia-smi가 GPU를 보는지 (evidence/gpu-vllm-local/01_nvidia-smi.txt)
2. WSL2 상태 / docker info / 실제 CUDA 컨테이너 안에서 nvidia-smi가 보이는지
   (evidence/gpu-vllm-local/02_docker-gpu-check.txt)

로그 메시지는 인코딩 문제 방지를 위해 ASCII로만 작성했다.
#>

$ErrorActionPreference = "Continue"
$evidenceDir = Join-Path $PSScriptRoot ("..\evidence\gpu-vllm-local\check-" + (Get-Date -Format "yyyyMMddTHHmmssfff") + "-" + [guid]::NewGuid().ToString("N"))
$evidenceDir = (Resolve-Path (New-Item -ItemType Directory -Force -Path $evidenceDir)).Path

function Write-Section {
    param([string]$Path, [string]$Title, [scriptblock]$Body)
    $lines = @()
    $lines += "=== $Title ==="
    $lines += "Timestamp: $([DateTimeOffset]::Now.ToString("o"))"
    $lines += ""
    $result = & $Body 2>&1 | Out-String
    $lines += $result
    ($lines -join "`r`n") | Out-File -FilePath (Join-Path $evidenceDir $Path) -Encoding utf8
    Write-Host "[$Path] saved"
}

Write-Host "`n=== Step 1: Host nvidia-smi ===`n"
Write-Section "01_nvidia-smi.txt" "nvidia-smi (host)" {
    nvidia-smi
}

Write-Host "`n=== Step 2: WSL2 / docker info / GPU-in-container check ===`n"
Write-Section "02_docker-gpu-check.txt" "WSL2 + docker info + GPU visible inside a CUDA container" {
    Write-Output "--- wsl --version ---"
    wsl --version
    Write-Output "`n--- wsl -l -v ---"
    wsl -l -v
    Write-Output "`n--- docker info (relevant lines) ---"
    docker info
    Write-Output "`n--- docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi ---"
    docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
}

Write-Host "`n=== Done: check evidence/gpu-vllm-local/ ===`n"
Get-ChildItem $evidenceDir | Select-Object Name, Length
