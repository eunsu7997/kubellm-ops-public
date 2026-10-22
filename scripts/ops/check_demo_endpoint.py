"""Bounded real chat/SSE check using only the Python standard library."""
import argparse
import json
import os
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

MODEL = 'Qwen/Qwen2.5-0.5B-Instruct'
ALLOWED = {'http://127.0.0.1:18003', 'https://your-device.your-tailnet.ts.net'}


def check_curl(base):
    if base not in ALLOWED:
        raise ValueError('unapproved endpoint')

    def fetch(path, stream=None):
        cmd = ['curl.exe' if os.name == 'nt' else 'curl', '--fail', '--silent', '--show-error',
               '--noproxy', '*', '--max-time', '40', base + path]
        payload = None
        if stream is not None:
            payload = json.dumps({'model': MODEL, 'messages': [{'role': 'user', 'content': 'Count to three'}],
                                  'max_tokens': 16, 'stream': stream}).encode()
            cmd += ['-H', 'Content-Type: application/json', '--data-binary', '@-']
        result = subprocess.run(cmd, input=payload, capture_output=True, timeout=45, check=True)
        return result.stdout.decode('utf-8')

    ready = json.loads(fetch('/readyz')).get('status') == 'ready'
    chat = any(c.get('message', {}).get('content') for c in json.loads(fetch('/v1/chat/completions', False)).get('choices', []))
    lines = fetch('/v1/chat/completions', True).splitlines()
    content = any(content_event(line) for line in lines)
    done = 'data: [DONE]' in lines
    return {'checked_at': datetime.now(UTC).isoformat(), 'base': base, 'transport': 'curl TLS verification enabled',
            'ready': ready, 'chat_content': bool(chat), 'sse_content': content, 'sse_done': done,
            'ok': ready and bool(chat) and content and done, 'location': 'caller host; academy not implied'}


def content_event(line):
    if not line.startswith('data:') or line[5:].strip() == '[DONE]':
        return False
    data = json.loads(line[5:].strip())
    return any(bool(c.get('delta', {}).get('content')) for c in data.get('choices', []))


def check(base):
    if base not in ALLOWED:
        raise ValueError('Use the verified localhost or private Tailscale URL only')
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    with client.open(base + '/readyz', timeout=15) as response:
        ready = response.status == 200
    payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'Count to three'}], 'max_tokens': 16}

    def request(stream):
        return urllib.request.Request(base + '/v1/chat/completions',
                                      json.dumps({**payload, 'stream': stream}).encode(),
                                      headers={'Content-Type': 'application/json'})

    with client.open(request(False), timeout=40) as response:
        data = json.load(response)
        chat = response.status == 200 and any(c.get('message', {}).get('content') for c in data.get('choices', []))
    content, done = False, False
    with client.open(request(True), timeout=40) as response:
        for raw in response:
            if time.monotonic() - started > 90:
                raise TimeoutError('bounded demo check exceeded 90 seconds')
            line = raw.decode('utf-8').strip()
            content = content or content_event(line)
            if line == 'data: [DONE]':
                done = True
                break
    return {'checked_at': datetime.now(UTC).isoformat(), 'base': base, 'ready': ready, 'chat_content': bool(chat),
            'sse_content': content, 'sse_done': done, 'ok': ready and bool(chat) and content and done,
            'location': 'home localhost' if base.startswith('http:') else 'caller host via private tailnet'}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:18003', choices=sorted(ALLOWED))
    ap.add_argument('--output', type=Path)
    ap.add_argument('--transport', choices=['urllib', 'curl'], default='curl' if os.name == 'nt' else 'urllib')
    args = ap.parse_args()
    try:
        result = check_curl(args.base) if args.transport == 'curl' else check(args.base)
    except Exception as exc:
        result = {'ok': False, 'base': args.base, 'error': type(exc).__name__}
    text = json.dumps(result, indent=2)
    if args.output:
        with args.output.open('x', encoding='utf-8') as file:
            file.write(text)
    print(text)
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
