"""Loopback-only chat screen for the existing GPU Gateway. No runtime reconfiguration."""
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

app = FastAPI(title='Local GPU chat demo')
MODEL = 'Qwen/Qwen2.5-0.5B-Instruct'
GATEWAY = 'http://127.0.0.1:18003'
PAGE = Path(__file__).with_name('chat_demo.html')
busy = False


@app.get('/')
def page():
    return FileResponse(PAGE)


@app.get('/readyz')
async def ready():
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get(GATEWAY + '/readyz')
            return JSONResponse({'ready': response.status_code == 200, 'model': MODEL}, status_code=response.status_code)
    except httpx.HTTPError:
        return JSONResponse({'ready': False}, status_code=503)


@app.post('/chat')
async def chat(request: Request):
    global busy
    # Same-origin browser requests only; no CORS or remote execution interface.
    if request.headers.get('origin') not in (None, 'http://127.0.0.1:18004'):
        return JSONResponse({'error': 'origin rejected'}, status_code=403)
    raw = await request.body()
    if len(raw) > 4096:
        return JSONResponse({'error': '질문은 500자 이하로 입력해주세요.'}, status_code=413)
    try:
        prompt = json.loads(raw).get('prompt')
    except (ValueError, AttributeError):
        prompt = None
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 500:
        return JSONResponse({'error': '질문은 1~500자로 입력해주세요.'}, status_code=400)
    if busy:
        return JSONResponse({'error': '다른 응답을 생성 중입니다. 잠시 후 다시 시도해주세요.'}, status_code=429)
    busy = True

    async def generate():
        global busy
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45), trust_env=False) as client:
                async with client.stream('POST', GATEWAY + '/v1/chat/completions', json={
                    'model': MODEL, 'messages': [{'role': 'user', 'content': prompt}],
                    'stream': True, 'max_tokens': 128, 'temperature': .3,
                }) as response:
                    if response.status_code != 200:
                        yield 'data: ' + json.dumps({'error': f'Gateway HTTP {response.status_code}'}) + '\n\n'
                        return
                    async for line in response.aiter_lines():
                        yield line + '\n'
        except httpx.HTTPError as exc:
            yield 'data: ' + json.dumps({'error': type(exc).__name__}) + '\n\n'
        finally:
            busy = False

    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-store'})
