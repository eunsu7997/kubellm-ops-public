#!/usr/bin/env bash
# Scenario 3: GPU Resource 부족 -> Pod Pending 관찰
#
# 상태: NOT VERIFIED (GPU/Kubernetes 클러스터가 없는 환경에서 작성만 했다).
# 실제 GPU 1개짜리 노드가 있는 클러스터에서 이 스크립트를 실행하면
# USER VERIFICATION REQUIRED 항목을 직접 채울 수 있다.
#
# 방법: vLLM Deployment를 그대로 두고, 클러스터가 가진 것보다 많은 GPU를
# 요구하는 "가짜 경쟁자" Deployment를 하나 더 만들어서 스케줄러가 Pod를
# Pending 상태로 남기는 것을 관찰한다. (기존 vllm/deployment.yaml을 건드리지 않기
# 위해 별도 임시 Deployment를 이 스크립트 안에서 생성하고 끝나면 삭제한다.)

set -euo pipefail

NAMESPACE="kubellm-ops"
RESULTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/results"
OUT_FILE="${RESULTS_DIR}/scenario3_gpu_resource_pending_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "${RESULTS_DIR}"
TMP_MANIFEST=$(mktemp --suffix=.yaml)

cat > "${TMP_MANIFEST}" <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gpu-hog-temp
  namespace: ${NAMESPACE}
  labels:
    app: gpu-hog-temp
spec:
  replicas: 1
  selector:
    matchLabels:
      app: gpu-hog-temp
  template:
    metadata:
      labels:
        app: gpu-hog-temp
    spec:
      containers:
        - name: gpu-hog-temp
          image: busybox:1.36
          command: ["sleep", "3600"]
          resources:
            limits:
              nvidia.com/gpu: 99
            requests:
              nvidia.com/gpu: 99
EOF

echo "=== Scenario 3: GPU Resource 부족 (임시 Deployment로 재현) ===" | tee "${OUT_FILE}"
echo "--- 임시 manifest 적용 (nvidia.com/gpu: 99 요청) ---" | tee -a "${OUT_FILE}"
kubectl apply -f "${TMP_MANIFEST}" | tee -a "${OUT_FILE}"

echo "--- 10초 대기 후 Pod 상태 확인 ---" | tee -a "${OUT_FILE}"
sleep 10
kubectl get pods -n "${NAMESPACE}" -l app=gpu-hog-temp -o wide | tee -a "${OUT_FILE}"

POD_NAME=$(kubectl get pods -n "${NAMESPACE}" -l app=gpu-hog-temp -o jsonpath='{.items[0].metadata.name}')
echo "--- kubectl describe pod (Scheduler 메시지 확인용) ---" | tee -a "${OUT_FILE}"
kubectl describe pod -n "${NAMESPACE}" "${POD_NAME}" | tee -a "${OUT_FILE}"

echo "--- 관련 Kubernetes Event (FailedScheduling 기대) ---" | tee -a "${OUT_FILE}"
kubectl get events -n "${NAMESPACE}" --field-selector involvedObject.name="${POD_NAME}" | tee -a "${OUT_FILE}"

echo "--- 정리: 임시 Deployment 삭제 ---" | tee -a "${OUT_FILE}"
kubectl delete -f "${TMP_MANIFEST}" | tee -a "${OUT_FILE}"
rm -f "${TMP_MANIFEST}"

echo "결과 저장: ${OUT_FILE}"
echo "USER VERIFICATION REQUIRED: Pod가 Pending 상태였는지, describe 출력에 FailedScheduling"
echo "(insufficient nvidia.com/gpu) 같은 메시지가 있었는지 docs/incidents/ 문서에 옮겨 적어라."
