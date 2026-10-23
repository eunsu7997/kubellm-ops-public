<#
KubeLLM-Ops: Docker Desktop Kubernetes에서 Gateway + Mock Backend를 실제로 배포하고
증거를 evidence/kubernetes-local/ 에 저장하는 스크립트.

이 스크립트는 사용자 PC(Docker Desktop + Kubernetes 활성화된 환경)에서 직접 실행해야
한다 - Claude Code의 샌드박스에서는 docker/kubectl에 접근할 수 없어 대신 실행할 수
없다. repo 루트(<PROJECT_ROOT>)에서 실행할 것.

사용법:
    .\scripts\k8s-local-verify.ps1

전제:
- Docker Desktop 설정 > Kubernetes > "Enable Kubernetes" 가 체크되어 있고
  "Kubernetes is running" 상태여야 한다 (Settings > Kubernetes, 초록 불).
  아직이라면: Docker Desktop 열기 -> 오른쪽 위 톱니바퀴(Settings) -> 왼쪽 메뉴
  "Kubernetes" -> "Enable Kubernetes" 체크 -> 우측 하단 "Apply & Restart" 클릭 ->
  1~2분 정도 기다린 뒤 이 스크립트를 실행한다.
- vLLM/GPU/DCGM은 이번 단계 범위가 아니므로 k8s/vllm, k8s/monitoring은 적용하지 않는다.

참고: 실행 중 표시되는 로그 메시지는 인코딩 문제(한글 깨짐이 PowerShell 파서 오류로
이어지는 것)를 피하기 위해 의도적으로 영어(ASCII)로만 작성했다. 주석은 한글로 남겼다.
#>

$ErrorActionPreference = "Continue"
$ns = "kubellm-ops"
$evidenceDir = Join-Path $PSScriptRoot "..\evidence\kubernetes-local"
$evidenceDir = (Resolve-Path (New-Item -ItemType Directory -Force -Path $evidenceDir)).Path

function Write-Section {
    param([string]$Path, [string]$Title, [scriptblock]$Body)
    $lines = @()
    $lines += "=== $Title ==="
    $lines += "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $lines += ""
    $result = & $Body 2>&1 | Out-String
    $lines += $result
    ($lines -join "`r`n") | Out-File -FilePath (Join-Path $evidenceDir $Path) -Encoding utf8
    Write-Host "[$Path] saved"
}

Write-Host "`n=== Step 1: Environment check ===`n"
Write-Section "01_cluster-info.txt" "docker / kubectl / cluster environment check" {
    Write-Output "--- docker version ---"
    docker version
    Write-Output "`n--- kubectl version --client ---"
    kubectl version --client
    Write-Output "`n--- kubectl config current-context ---"
    kubectl config current-context
    Write-Output "`n--- kubectl cluster-info ---"
    kubectl cluster-info
}

kubectl cluster-info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ABORT] kubectl cluster-info failed - Kubernetes cluster is not running." -ForegroundColor Red
    Write-Host "Open Docker Desktop -> Settings -> Kubernetes -> check 'Enable Kubernetes' -> Apply & Restart, then re-run this script." -ForegroundColor Yellow
    exit 1
}

Write-Section "02_nodes.txt" "kubectl get nodes -o wide" {
    kubectl get nodes -o wide
}

Write-Host "`n=== Step 2: Build local images ===`n"
docker build -t kubellm-ops-gateway:local .\gateway
if ($LASTEXITCODE -ne 0) { Write-Host "[ABORT] gateway image build failed" -ForegroundColor Red; exit 1 }
docker build -t kubellm-ops-mock-backend:local .\mock-backend
if ($LASTEXITCODE -ne 0) { Write-Host "[ABORT] mock-backend image build failed" -ForegroundColor Red; exit 1 }

Write-Host "`n=== Step 3: Deploy to Kubernetes (vLLM/GPU/DCGM excluded) ===`n"
# k8s/kustomization.yaml = base + gateway + mock + NetworkPolicy
kubectl apply -k k8s/

Write-Host "`n=== Step 4: Wait for pods to be Ready ===`n"
kubectl wait --for=condition=Ready pod -l app=gateway -n $ns --timeout=120s
kubectl wait --for=condition=Ready pod -l app=mock-backend -n $ns --timeout=120s

Write-Section "03_deployments-services.txt" "Deployments / Services / ConfigMaps" {
    kubectl get deployments,services,configmaps -n $ns -o wide
}

Write-Section "04_pods-ready.txt" "Pods (with Ready status)" {
    kubectl get pods -n $ns -o wide
}

Write-Section "05_gateway-describe.txt" "kubectl describe pod (gateway) - probe configuration" {
    kubectl describe pod -n $ns -l app=gateway
}

Write-Section "05b_security-pdb-netpol.txt" "securityContext / PDB / NetworkPolicy as applied" {
    Write-Output "--- gateway pod securityContext (pod, container) ---"
    kubectl get pods -n $ns -l app=gateway -o jsonpath="{range .items[*]}{.metadata.name}{' pod='}{.spec.securityContext}{' container='}{.spec.containers[0].securityContext}{'\n'}{end}"
    Write-Output "`n--- uid inside the gateway container ---"
    $gw = (kubectl get pods -n $ns -l app=gateway -o jsonpath="{.items[0].metadata.name}")
    kubectl exec -n $ns $gw -- id
    Write-Output "`n--- write to root filesystem must fail (read-only) ---"
    kubectl exec -n $ns $gw -- sh -c "touch /app/probe 2>&1 || true"
    Write-Output "`n--- PodDisruptionBudget ---"
    kubectl get pdb -n $ns
    Write-Output "`n--- NetworkPolicy (Docker Desktop CNI may not enforce these) ---"
    kubectl get networkpolicy -n $ns
}

Write-Host "`n=== Step 5: In-cluster E2E check (port-forward) ===`n"
# Docker Compose gateway may already be using local port 8000, so use a different
# local port (18080) for the Kubernetes check to avoid ambiguity about which stack answered.
$pf = Start-Job -ScriptBlock { kubectl port-forward svc/gateway 18080:80 -n $using:ns }
Start-Sleep -Seconds 3
$body = @{ model = "mock-model"; messages = @(@{ role = "user"; content = "hello from kubernetes" }) } | ConvertTo-Json -Depth 5

$e2eLines = @()
$e2eLines += "=== Kubernetes in-cluster E2E: POST http://localhost:18080/v1/chat/completions ==="
$e2eLines += "(port-forward -> svc/gateway:80 -> Pod:8000 -> mock-backend Service)"
$e2eLines += "Timestamp: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
try {
    $resp = Invoke-RestMethod -Uri "http://localhost:18080/v1/chat/completions" -Method POST -ContentType "application/json" -Body $body
    $e2eLines += ($resp | ConvertTo-Json -Depth 5)
} catch {
    $e2eLines += "Request failed: $($_.Exception.Message)"
}
($e2eLines -join "`r`n") | Out-File -FilePath (Join-Path $evidenceDir "06_k8s-e2e-response.txt") -Encoding utf8
Write-Host "[06_k8s-e2e-response.txt] saved"
Stop-Job $pf -ErrorAction SilentlyContinue | Out-Null
Remove-Job $pf -ErrorAction SilentlyContinue | Out-Null

Write-Host "`n=== Step 6: Gateway Pod forced-delete failure experiment ===`n"
Write-Section "07_pod-delete-before.txt" "Gateway pod status before delete" {
    kubectl get pods -n $ns -l app=gateway -o wide
}
$beforeJson = kubectl get pods -n $ns -l app=gateway -o json | ConvertFrom-Json
$targetPod = $beforeJson.items[0].metadata.name
Write-Host "Target pod to delete: $targetPod"

# replicas: 2이므로 삭제 직후에도 삭제 대상이 아닌 기존 Pod가 이미 Ready 상태다.
# 그 기존 Pod를 "새로 복구된 Pod"로 착각하지 않도록, 삭제 전 존재하던 모든 Pod 이름을
# 기록해두고 그 목록에 없는(=진짜 새로 생성된) Pod가 Ready가 될 때까지 기다린다.
$beforeNames = @($beforeJson.items | ForEach-Object { $_.metadata.name })

$deleteTime = Get-Date
kubectl delete pod $targetPod -n $ns

$deadline = (Get-Date).AddSeconds(120)
$newPodName = $null
do {
    Start-Sleep -Seconds 3
    $pods = kubectl get pods -n $ns -l app=gateway -o json | ConvertFrom-Json
    foreach ($p in $pods.items) {
        if ($beforeNames -notcontains $p.metadata.name) {
            $readyCond = $p.status.conditions | Where-Object { $_.type -eq "Ready" }
            if ($readyCond -and $readyCond.status -eq "True") {
                $newPodName = $p.metadata.name
                break
            }
        }
    }
} while (-not $newPodName -and (Get-Date) -lt $deadline)
$readyTime = Get-Date
$recoverySeconds = [math]::Round(($readyTime - $deleteTime).TotalSeconds, 1)

Write-Section "08_pod-delete-after.txt" "Gateway pod status after delete (including new pod)" {
    kubectl get pods -n $ns -l app=gateway -o wide
}

Write-Section "09_events.txt" "kubectl get events (sorted by time)" {
    kubectl get events -n $ns --sort-by=".lastTimestamp"
}

# Re-confirm the API responds normally again after recovery (fresh port-forward).
$pf2 = Start-Job -ScriptBlock { kubectl port-forward svc/gateway 18081:80 -n $using:ns }
Start-Sleep -Seconds 3
$postRecoveryOk = $false
$postRecoveryDetail = ""
try {
    $resp2 = Invoke-RestMethod -Uri "http://localhost:18081/v1/chat/completions" -Method POST -ContentType "application/json" -Body $body
    $postRecoveryOk = $true
    $postRecoveryDetail = ($resp2 | ConvertTo-Json -Depth 5)
} catch {
    $postRecoveryDetail = "Request failed: $($_.Exception.Message)"
}
Stop-Job $pf2 -ErrorAction SilentlyContinue | Out-Null
Remove-Job $pf2 -ErrorAction SilentlyContinue | Out-Null

# 문자열 안에 조건문을 직접 넣지 않고, 표시할 값을 먼저 변수로 전부 계산해둔다
# (here-string 대신 단순 배열 + join을 쓰기 위함 - PowerShell 5.1/7 양쪽에서 안전).
if ($newPodName) {
    $newPodDisplay = $newPodName
    $readyTimeDisplay = $readyTime.ToString("yyyy-MM-dd HH:mm:ss.fff")
    $recoveryDisplay = "$recoverySeconds sec"
} else {
    $newPodDisplay = "NOT DETECTED within 120s timeout"
    $readyTimeDisplay = "N/A"
    $recoveryDisplay = "N/A (timeout)"
}
if ($postRecoveryOk) {
    $postRecoveryStatusDisplay = "SUCCESS (200)"
} else {
    $postRecoveryStatusDisplay = "FAILED"
}
$deleteTimeDisplay = $deleteTime.ToString("yyyy-MM-dd HH:mm:ss.fff")

$summaryLines = @(
    "# Pod Forced-Delete Recovery Result (auto-generated from actual measurements)",
    "",
    "- Deleted pod: $targetPod",
    "- Delete time: $deleteTimeDisplay",
    "- New pod: $newPodDisplay",
    "- New pod Ready time: $readyTimeDisplay",
    "- Recovery time (delete -> new pod Ready): $recoveryDisplay",
    "- Post-recovery API check (POST /v1/chat/completions): $postRecoveryStatusDisplay",
    "",
    "## Post-recovery API response",
    "",
    $postRecoveryDetail,
    "",
    "See 07_pod-delete-before.txt / 08_pod-delete-after.txt for pod status, and 09_events.txt for events."
)
($summaryLines -join "`r`n") | Out-File -FilePath (Join-Path $evidenceDir "10_recovery-summary.md") -Encoding utf8
Write-Host "[10_recovery-summary.md] saved"

Write-Host "`n=== Done: check evidence/kubernetes-local/ ===`n"
Get-ChildItem $evidenceDir | Select-Object Name, Length
