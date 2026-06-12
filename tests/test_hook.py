import json
import os
import subprocess
import sys

from conftest import SCRIPT, assistant, usage, user, write_jsonl


def run_hook(payload, tmp_path, extra_env=None):
    env = {**os.environ, "TOKEN_USAGE_LEDGER_DIR": str(tmp_path / "ledger")}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "hook"],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def make_transcript(tmp_path, out_tokens=1000):
    return write_jsonl(tmp_path / "t.jsonl", [
        user("2026-06-12T10:00:00Z", command="/big"),
        assistant("2026-06-12T10:00:01Z", usage(out=out_tokens), request_id="r1"),
    ])


def test_hook_writes_ledger_and_exits_zero(tmp_path):
    t = make_transcript(tmp_path)
    r = run_hook({"session_id": "abc-123", "transcript_path": str(t)}, tmp_path)
    assert r.returncode == 0
    ledger = json.loads((tmp_path / "ledger" / "abc-123.json").read_text())
    assert ledger["total"]["usage"]["output"] == 1000


def test_hook_never_fails_on_garbage(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "hook"], input="not json{",
                       capture_output=True, text=True,
                       env={**os.environ, "TOKEN_USAGE_LEDGER_DIR": str(tmp_path)})
    assert r.returncode == 0
