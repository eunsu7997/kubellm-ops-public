# Kubernetes 매니페스트

| 경로 | 용도 | 적용 |
| --- | --- | --- |
| `k8s/` (kustomization) | GPU 없이 Gateway + mock-backend + NetworkPolicy | `kubectl apply -k k8s/` |
| `k8s/overlays/gpu/` | Gateway → vLLM(Qwen2.5-0.5B) + DCGM exporter | `kubectl apply -k k8s/overlays/gpu` |
| `k8s/validation/` | 2026-10-21 WSL k3s에서 실제로 적용한 실험용 매니페스트(별도 namespace) | 기록 보존용 |

- 기본 구성은 Docker Desktop Kubernetes에서 검증했고(`scripts/k8s-local-verify.ps1`), GPU 경로는 `k8s/validation/`의 매니페스트로 검증했다. 결과는 [Kubernetes 실측](../docs/KUBERNETES-LIVE-VALIDATION.md)에 있다.
- `overlays/gpu`, NetworkPolicy, securityContext, PDB는 실측 이후 추가했고 아직 클러스터에 적용하지 않았다. CI는 두 구성이 렌더링되는지까지만 확인한다.
- vLLM은 GPU가 한 장이라 `replicas: 1`, `strategy: Recreate`. liveness는 readiness보다 느슨하게 둬서 과부하 중 재시작을 피한다.
- NetworkPolicy는 기본 거부 후 외부 → Gateway, Gateway → 백엔드, Prometheus → 지표만 연다. CNI가 지원해야 적용된다(k3s 기본은 지원, Docker Desktop은 무시).
