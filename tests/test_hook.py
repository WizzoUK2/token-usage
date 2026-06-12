import json
import os
import subprocess
import sys

from conftest import SCRIPT, assistant, usage, user, write_jsonl


def run_hook(payload, tmp_path, extra_env=None):
    env = {**os.environ, "TOKEN_USAGE_LEDGER_DIR": str(tmp_path / "ledger")}
    env.pop("TOKEN_USAGE_BUDGET_USD", None)
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


def test_budget_nudge_fires_once(tmp_path):
    t = make_transcript(tmp_path, out_tokens=1_000_000)  # 1MTok fable output ≈ $50
    payload = {"session_id": "bud-1", "transcript_path": str(t)}
    env = {"TOKEN_USAGE_BUDGET_USD": "10"}
    r1 = run_hook(payload, tmp_path, env)
    assert r1.returncode == 0
    msg = json.loads(r1.stdout)
    assert "passed your $10.00 budget" in msg["systemMessage"]
    assert "/big" in msg["systemMessage"]
    ledger = json.loads((tmp_path / "ledger" / "bud-1.json").read_text())
    assert ledger["budget_notified"] is True
    r2 = run_hook(payload, tmp_path, env)                 # second run: silent
    assert r2.returncode == 0
    assert r2.stdout.strip() == ""


def test_budget_unset_or_invalid_is_inert(tmp_path):
    t = make_transcript(tmp_path, out_tokens=1_000_000)
    for env in ({}, {"TOKEN_USAGE_BUDGET_USD": "not-a-number"}):
        r = run_hook({"session_id": "bud-2", "transcript_path": str(t)}, tmp_path, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""


def test_budget_under_limit_is_silent_and_unarmed(tmp_path):
    t = make_transcript(tmp_path, out_tokens=1000)        # ≈ $0.05, well under $10
    payload = {"session_id": "bud-3", "transcript_path": str(t)}
    env = {"TOKEN_USAGE_BUDGET_USD": "10"}
    r = run_hook(payload, tmp_path, env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    ledger = json.loads((tmp_path / "ledger" / "bud-3.json").read_text())
    assert ledger["budget_notified"] is False              # not prematurely armed
    # Session grows past the limit -> the nudge then fires.
    make_transcript(tmp_path, out_tokens=1_000_000)
    r2 = run_hook(payload, tmp_path, env)
    assert "passed your $10.00 budget" in json.loads(r2.stdout)["systemMessage"]


def test_hook_recovers_from_non_dict_ledger(tmp_path):
    t = make_transcript(tmp_path)
    payload = {"session_id": "bud-4", "transcript_path": str(t)}
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "bud-4.json").write_text("[1, 2, 3]")    # valid JSON, wrong shape
    r = run_hook(payload, tmp_path)
    assert r.returncode == 0
    ledger = json.loads((ledger_dir / "bud-4.json").read_text())
    assert ledger["total"]["usage"]["output"] == 1000      # ledger self-healed
