"""Mock Backend — 실제 GPU/vLLM 없이 Gateway ↔ 백엔드 흐름 전체를 검증하기 위한 가짜 LLM 서버.

OpenAI 호환 API 형태(`/v1/chat/completions`)로 응답하되, 실제 모델 추론은 하지 않고
입력을 그대로 echo하는 더미 응답을 만든다. `x-mock-mode` 헤더로 정상/느림/오류 상태를
재현할 수 있다 — 이건 vLLM이 다운되거나 느려졌을 때 Gateway가 어떻게 반응하는지를
GPU 없이도 실제로 테스트하기 위한 장치다.
"""
import asyncio
import os
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI(title="KubeLLM-Ops Mock Backend")

# "slow" 모드일 때 몇 초를 기다릴지 (Gateway의 REQUEST_TIMEOUT보다 길게 잡으면 timeout을 재현할 수 있다)
SLOW_DELAY_SECONDS = float(os.environ.get("MOCK_SLOW_DELAY_SECONDS", "5"))

# 요청마다 x-mock-mode 헤더를 보낼 수 없는 상황(예: Gateway를 거쳐서 헤더가 전달되지 않는
# end-to-end 장애 재현 스크립트)을 위해, 프로세스 전체의 기본 모드를 환경변수로도 강제할 수 있다.
# 헤더가 오면 헤더가 우선한다.
MOCK_FORCE_MODE = os.environ.get("MOCK_FORCE_MODE")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(payload: dict, x_mock_mode: str | None = Header(default=None)):
    """
    x-mock-mode 헤더(또는 MOCK_FORCE_MODE 환경변수)로 동작을 고른다:
      - 없음 또는 "normal": 정상 응답을 즉시 반환
      - "slow": SLOW_DELAY_SECONDS초 대기 후 응답 (timeout 재현용)
      - "error500": 500 오류를 그대로 반환

    Gateway는 클라이언트가 보낸 x-mock-mode 헤더를 백엔드로 그대로 전달하지 않으므로
    (원래 목적이 아닌 헤더를 얇은 프록시가 임의로 전달하지 않기 위함), Gateway를 통한
    end-to-end 장애 재현에는 MOCK_FORCE_MODE 환경변수로 이 프로세스 자체의 기본 동작을
    강제한다. 헤더는 mock-backend를 직접 호출하는 단위 테스트에서 사용한다.

    "connection failure"(연결 자체가 안 되는 상황)는 이 엔드포인트가 흉내낼 수 없다 —
    서버가 응답할 수 있다는 것 자체가 "연결은 됐다"는 뜻이기 때문이다. 그 경우는
    scripts/failure-tests에서 이 프로세스를 종료하거나, Gateway가 존재하지 않는
    포트를 가리키게 해서 재현한다.
    """
    mode = (x_mock_mode or MOCK_FORCE_MODE or "normal").lower()

    if mode == "error500":
        raise HTTPException(status_code=500, detail="mock backend forced error (x-mock-mode: error500)")

    if mode == "slow":
        await asyncio.sleep(SLOW_DELAY_SECONDS)

    model = payload.get("model", "mock-model")
    user_message = ""
    for m in payload.get("messages", []):
        if m.get("role") == "user":
            user_message = m.get("content", "")
            break

    prompt_tokens = max(1, len(user_message.split()))
    completion_text = f"[mock response] echo: {user_message}"
    completion_tokens = max(1, len(completion_text.split()))

    return JSONResponse(
        {
            "id": f"mock-{uuid.uuid4().hex[:8]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": completion_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )
