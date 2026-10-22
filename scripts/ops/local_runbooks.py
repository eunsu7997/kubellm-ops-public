"""Small lexical local runbook retriever. Retrieval content is evidence, never executable instruction."""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = ("docs/SAFETY-HARNESS-CLOSURE.md", "docs/OPERATIONAL-CLOSURE.md",
             "docs/INTEGRATION-FOLLOWUP-20261022.md")


def terms(text):
    return set(re.findall(r"[a-z0-9_]{3,}", text.lower()))


def retrieve(query, *, root=ROOT, paths=ALLOWLIST, limit=2):
    requested = terms(query)
    if not requested:
        return []
    candidates = []
    root = Path(root).resolve()
    for relative in paths:
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError("runbook must stay in repository")
        raw = path.read_bytes()
        # Fixed-size line chunks preserve an exact source line and file hash.
        lines = raw.decode("utf-8").splitlines()
        for start in range(0, len(lines), 12):
            chunk = "\n".join(lines[start:start + 12])[:700]
            score = len(requested & terms(chunk))
            if score:
                candidates.append({"path": relative, "line": start + 1, "text": chunk,
                                   "file_sha256": hashlib.sha256(raw).hexdigest(), "score": score})
    candidates.sort(key=lambda r: (-r["score"], r["path"], r["line"]))
    return [{**r, "evidence_id": f"RB-{i + 1:02d}"} for i, r in enumerate(candidates[:limit])]
