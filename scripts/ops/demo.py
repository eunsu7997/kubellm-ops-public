"""Local demo launcher; replay never masquerades as live inference."""
import argparse
import html
import json
import subprocess
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECORDED = ROOT / 'evidence/public-integration-20261023/latest'


def render(summary, mode):
    esc = lambda value: html.escape(str(value))  # noqa: E731
    rows = ''.join(f'<tr><td>{esc(s["stage"])}</td><td>{esc(s["status"])}</td></tr>'
                   for s in summary.get('stages', []))
    comparisons = ''
    for key in ('baseline_slo', 'candidate_slo'):
        slo = summary.get(key) or {}
        checks = ''.join(f'<li>{esc(c["name"])}: {esc(c["actual"])} / {esc(c["target"])} '
                         f'({esc(c["status"])})</li>' for c in slo.get('checks', []))
        comparisons += f'<h2>{esc(key)}: {esc(slo.get("verdict", "NOT RUN"))}</h2><ul>{checks}</ul>'
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>Five project demo</title>
<style>body{{font:18px system-ui;max-width:1050px;margin:40px auto;padding:20px;background:#101827;color:#edf3ff}}
strong{{color:#ffda85}}td{{padding:10px;border-bottom:1px solid #536078}}h1,h2{{color:#93d9ff}}</style>
<h1>5개 프로젝트 통합 실행</h1><p><strong>{esc(mode)}</strong></p>
<p>Run ID: {esc(summary.get('run_id'))}<br>Status: {esc(summary.get('status'))}</p>
<p>Guard → 실제 batch 점유 조회 → Gateway/GPU SSE → SLO 판정 → 제한된 429 재시도 → Copilot 검증</p>
<table>{rows}</table>{comparisons}
<p>Copilot 초안 채택: {esc(summary.get('llm_draft_eligible'))}. false이면 근거 리포트를 사용합니다.</p>
<p>단일 GPU의 제한된 실험입니다. production 변경·Kubernetes 배포·scale-out은 실행하지 않습니다.
2.5초 목표는 실험용 전체 SSE E2E이며 운영 보장이나 1초 TTFT 목표가 아닙니다.</p></html>'''


def main():
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser()
    ap.add_argument('--replay', action='store_true')
    ap.add_argument('--no-open', action='store_true')
    args = ap.parse_args()
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    output = ROOT / 'evidence' / 'demo-runs' / stamp
    output.mkdir(parents=True, exist_ok=False)
    code = 0
    source = RECORDED
    mode = 'RECORDED — 과거 실행 기록 열람. 현재 GPU 실행이 아닙니다.'
    if not args.replay:
        print('LIVE: checking existing serving; no deployment or configuration changes.', flush=True)
        import oplib as ops
        checks = {'ready': ops.http('GET', '/readyz'), 'chat': ops.chat(), 'sse': ops.sse()}
        (output / 'preflight.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
        if checks['ready']['status'] != 200 or not checks['chat']['ok'] or not checks['sse']['ok']:
            print('Preflight failed. Evidence preserved. See docs/RUN-LOCAL.md; replay is explicit only.')
            return 2
        source = output / 'pipeline'
        code = subprocess.call([sys.executable, str(ROOT / 'scripts/ops/five_module_pipeline.py'),
                                '--execute', '--rounds', '5', '--output-dir', str(source)], cwd=ROOT)
        mode = 'LIVE — 이번 실행에서 생성한 결과. 성공 여부는 아래 상태를 확인하세요.'
    summary_path = source / 'SUMMARY.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {
        'status': 'FAILED BEFORE SUMMARY', 'stages': []}
    (output / 'SUMMARY.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    page = output / 'DEMO.html'
    page.write_text(render(summary, mode), encoding='utf-8')
    print(f'{mode}\n{page}\nRaw evidence: {source}', flush=True)
    if not args.no_open:
        webbrowser.open(page.as_uri())
    return code


if __name__ == '__main__':
    raise SystemExit(main())
