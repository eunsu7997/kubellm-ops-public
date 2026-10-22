"""Export a small offline presentation packet. No raw logs, credentials or uploads."""
import argparse
import hashlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from demo import ROOT, render


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', type=Path, required=True)
    args = ap.parse_args()
    run = args.run.resolve()
    if not run.is_relative_to(ROOT / 'evidence'):
        ap.error('run must be inside local evidence')
    original = json.loads((run / 'SUMMARY.json').read_text(encoding='utf-8'))
    if original['status'] != 'COMPLETED_WITH_LIMITATIONS':
        ap.error('packet requires an explicitly completed evidence run')
    # Export an allowlist of scalars and SLO values, not stage raw commands or host paths.
    summary = {k: original[k] for k in ('run_id', 'status', 'baseline_slo', 'candidate_slo', 'llm_draft_eligible')}
    summary['stages'] = [{'stage': s['stage'], 'status': s['status']} for s in original['stages']]
    folder = ROOT / 'dist' / ('instructor-' + datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(parents=True, exist_ok=False)
    files = {
        'OPEN-ME.html': render(summary, 'RECORDED — 2026-10-22 집 GPU 실행 기록. 현재 실시간 실행이 아닙니다.').encode(),
        'SUMMARY.json': json.dumps(summary, ensure_ascii=False, indent=2).encode(),
        'PRESENTATION.md': (ROOT / 'docs/INSTRUCTOR-TALK.md').read_bytes(),
        'CONNECTION.md': (ROOT / 'docs/DEMO-TOMORROW.md').read_bytes(),
        'check_demo_endpoint.py': (ROOT / 'scripts/ops/check_demo_endpoint.py').read_bytes(),
    }
    files['MANIFEST.json'] = json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
                                        indent=2).encode()
    archive = folder / 'TAKE-TO-ACADEMY.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, data in files.items():
            (folder / name).write_bytes(data)
            bundle.writestr(name, data)
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read('MANIFEST.json'))
        assert set(bundle.namelist()) == set(files)
        for name, digest in manifest.items():
            assert hashlib.sha256(bundle.read(name)).hexdigest() == digest
    print(json.dumps({'packet': str(archive), 'files_verified': len(files),
                      'zip_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
