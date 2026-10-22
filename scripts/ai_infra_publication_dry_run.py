"""Read-only publication inventory. Never stages, copies, archives or publishes."""

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
RISKS = {
    "private-user-path": r"(?i)(?:[a-z]:[\\/]users[\\/]|/home/|/Users/)",
    "machine-ID": r"(?i)(?:GPU-[0-9a-f-]+|\b(?:uuid|machine.?id|hostname|nodeName)\b)",
    "driver/GPU-process": r"(?i)(?:driver version|nvidia-smi|\bPID\b|GPU Memory|process name)",
    "proxy/host-address": r"(?i)(?:proxy|host\.docker\.internal|https?://|\b(?:\d{1,3}\.){3}\d{1,3}\b)",
    "credential-marker": r"(?i)(?:authorization|bearer|password|api[_-]?key|access[_-]?token|secret|AKIA[0-9A-Z]{16})",
}


def git_paths(*args):
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", *args],
        check=True,
        capture_output=True,
    )
    return set(result.stdout.decode("utf-8").split("\0")) - {""}


def main():
    tracked = git_paths()
    untracked = git_paths("--others", "--exclude-standard")
    docs = sorted((ROOT / "docs").glob("AI-INFRA-*.md"))
    candidates = {p.relative_to(ROOT).as_posix() for p in docs}
    missing = set()
    external = 0
    links = 0
    for doc in docs:
        for raw in LINK.findall(doc.read_text(encoding="utf-8")):
            target = urlsplit(raw.strip().strip("<>"))
            if target.scheme or target.netloc or not target.path:
                continue
            links += 1
            path = (doc.parent / unquote(target.path)).resolve()
            if not path.is_relative_to(ROOT):
                # Do not disclose or read private trace paths or their contents.
                external += 1
                continue
            rel = path.relative_to(ROOT).as_posix()
            candidates.add(rel)
            if not path.is_file():
                missing.add(rel)
    print("DRY RUN ONLY: no staging, copying, archiving, redaction, upload or publication")
    print(f"Direct local links: {links}; missing repo files: {len(missing)}; outside-repo links: {external}")
    for rel in sorted(candidates):
        path = ROOT / rel
        state = "TRACKED" if rel in tracked else "UNTRACKED" if rel in untracked else "IGNORED/OTHER"
        existence = "PRESENT" if path.is_file() else "MISSING"
        flags = []
        if path.is_file():
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".pdf", ".zip"}:
                flags.append("manual-visual/binary-review")
            else:
                content = path.read_bytes().decode("utf-8", errors="replace")
                flags = [name for name, pattern in RISKS.items() if re.search(pattern, content)]
        print(
            f"CANDIDATE (manual approval only) {rel}: {existence}, {state}; flags={','.join(flags) or 'none-detected'}"
        )
    print("Flags are heuristics, not findings of secret values or privacy clearance; every file needs manual review.")
    print("Only direct links in AI-INFRA docs checked; supporting docs may have further links.")
    print("Three-Markdown-only ZIP: NOT SELF-CONTAINED; ../evidence/ targets absent by construction.")
    print("Runtime, remote availability and publication: NOT VERIFIED / USER VERIFICATION REQUIRED")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
