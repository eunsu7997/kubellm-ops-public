#!/usr/bin/env bash
# Scenario 1: Gateway Pod 강제 삭제 -> 복구 관찰
#
# 상태: NOT VERIFIED (이 프로젝트를 만든 PC에 Kubernetes 클러스터가 없어 직접 실행하지
# 못했다). 실제 클러스터(kind/minikube/EKS 등 아무 것이나)에 이 프로젝트의
# k8s/ manifest를 배포한 뒤 이 스크립트를 실행하면 USER VERIFICATION REQUIRED 항목을
# 직접 채울 수 있다.
#
# 확인하는 것:
#   1. 기존 Gateway Pod 중 하나를 강제로 삭제한다 (kubectl delete pod).
#   2. 삭제 시각부터 새 Pod가 Running + Ready가 될 때까지 걸린 시간(복구시간)을 잰다.
#   3. 그동안 Service(gateway)가 트래픽을 계속 처리했는지(다른 replica로 라우팅됐는지)
#      /healthz 를 반복 호출해서 확인한다.
#
# 전제: k8s/base, k8s/gateway 매니페스트가 이미 apply되어 있고, replicas: 2로
# Gateway가 떠 있어야 한다(복제본이 1개면 삭제 순간 전체 다운타임이 생긴다 —
# 그 자체도 "왜 replicas: 2로 설정했는가"를 보여주는 관찰 포인트다).

set -euo pipefail

NAMESPACE="kubellm-ops"
RESULTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/results"
OUT_FILE="${RESULTS_DIR}/scenario1_gateway_pod_delete_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "${RESULTS_DIR}"

echo "=== Scenario 1: Gateway Pod 강제 삭제 ===" | tee "${OUT_FILE}"

echo "--- 삭제 전 Pod 상태 ---" | tee -a "${OUT_FILE}"
kubectl get pods -n "${NAMESPACE}" -l app=gateway -o wide | tee -a "${OUT_FILE}"

TARGET_POD=$(kubectl get pods -n "${NAMESPACE}" -l app=gateway -o jsonpath='{.items[0].metadata.name}')
echo "삭제 대상 Pod: ${TARGET_POD}" | tee -a "${OUT_FILE}"

DELETE_TIME=$(date +%s)
kubectl delete pod -n "${NAMESPACE}" "${TARGET_POD}"

echo "--- 새 Pod가 Ready 될 때까지 대기 (kubectl wait) ---" | tee -a "${OUT_FILE}"
# 삭제된 이름과 다른, 새로 생성된 Pod가 Ready 상태가 될 때까지 기다린다.
kubectl wait --for=condition=Ready pod -n "${NAMESPACE}" -l app=gateway --timeout=120s | tee -a "${OUT_FILE}"
READY_TIME=$(date +%s)

RECOVERY_SECONDS=$((READY_TIME - DELETE_TIME))
echo "복구 시간(삭제 -> 새 Pod Ready): ${RECOVERY_SECONDS}초" | tee -a "${OUT_FILE}"

echo "--- 삭제 후 Pod 상태 ---" | tee -a "${OUT_FILE}"
kubectl get pods -n "${NAMESPACE}" -l app=gateway -o wide | tee -a "${OUT_FILE}"

echo "--- 관련 Kubernetes Event ---" | tee -a "${OUT_FILE}"
kubectl get events -n "${NAMESPACE}" --sort-by='.lastTimestamp' | tail -n 20 | tee -a "${OUT_FILE}"

echo "결과 저장: ${OUT_FILE}"
echo "USER VERIFICATION REQUIRED: 위 로그의 복구 시간과 Event를 docs/incidents/ 문서에 옮겨 적어라."
