# 실행과 발표 안내

## 실제 집 GPU 채팅

기존 Gateway가 127.0.0.1:18003에서 실행 중일 때:

```powershell
powershell -ExecutionPolicy Bypass -File .\START-CHAT.ps1
```

브라우저에서 `http://127.0.0.1:18004/`를 연다. 실제 Qwen2.5-0.5B-Instruct 모델이며 GPT가 아니다. 각 질문은 독립적으로 전달하고 500자·128 출력 토큰·동시 생성1개로 제한한다. 대화는 브라우저 화면에만 표시하며 파일로 저장하지 않는다. 작은 모델의 자기소개·비교 답변은 오류일 수 있다.

## 통합 실험

```powershell
powershell -ExecutionPolicy Bypass -File .\START-DEMO.ps1
```

ready·실제 채팅·SSE 점검 후 실행하고 새 evidence 폴더에 기록한다. 별도 gpu-deploy-guard/incident-copilot checkout과 gpu-queue-lab source, 검토된 SLO 함수 및 기존 Docker/WSL 조회 환경이 필요하다. 5개 서비스의 자동 설치 프로그램이 아니다. [실행 코드의 CLI](../scripts/ops/five_module_pipeline.py)에서 저장소 경로를 조정한다.

```powershell
.\.venv\Scripts\python.exe scripts/ops/restore_stack.py --execute
```

복구는 기존 안전 검사에 통과한 경우에만 기존 6개 secure Docker 컨테이너를 시작한다. 다른 프로젝트를 종료하거나 모델·설정을 재설치하지 않는다.

## 오프라인 기록

```powershell
powershell -ExecutionPolicy Bypass -File .\START-DEMO.ps1 -Replay
.\.venv\Scripts\python.exe scripts/ops/build_instructor_packet.py --run evidence/public-integration-20261023/latest
```

Replay는 과거 집 GPU 실행 기록임을 명시한다. ZIP은 압축을 풀고 OPEN-ME.html 하나를 브라우저에서 열면 된다. 실제 실시간 결과로 소개하지 않는다.

## 개인 원격 연결

집 PC와 Docker·Tailscale을 유지하고 학원 PC를 같은 개인 tailnet에 연결한다. 실제 주소와 계정은 저장소에 게시하지 않는다. 개인 Gateway 주소의 `/readyz`, `/v1/chat/completions`를 사용한다. Gateway `/`와 `/v1/models`의 404는 상태 점검 기준이 아니다.

Windows에서 실제 개인 주소로 바꾸어 실행한다.

```powershell
curl.exe --fail --max-time 15 https://YOUR-DEVICE.YOUR-TAILNET.ts.net/readyz
```

원격 연결은 Gateway API만 제공하며 localhost18004의 채팅 UI나 원격 명령 실행기를 공개하지 않는다. Funnel·공유기 포트 개방은 사용하지 않는다. 집에서 Windows curl의 TLS 검증을 유지해 HTTPS 채팅·SSE를 확인했지만 학원 PC 연결은 아직 미검증이다. 집 PC의 Python urllib 인증서 검증은 실패했고 우회하지 않았다.
