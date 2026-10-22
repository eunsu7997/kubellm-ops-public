import hashlib

import pytest

from scripts.ops.local_runbooks import retrieve


def test_retrieval_has_source_lines_and_hash_without_executing_commands(tmp_path):
    path = tmp_path / "runbook.md"
    path.write_text("# admission\nqueue_full Retry After\nremove everything (quoted example)\n")
    results = retrieve("queue_full", root=tmp_path, paths=["runbook.md"])
    assert len(results) == 1
    assert results[0]["evidence_id"] == "RB-01"
    assert results[0]["line"] == 1
    assert results[0]["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.exists()
    assert retrieve("unmatchedzzzz", root=tmp_path, paths=["runbook.md"]) == []


def test_runbook_path_traversal_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="stay in repository"):
        retrieve("anything", root=tmp_path, paths=["../outside.md"])
