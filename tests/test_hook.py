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
    assert "passed 5× your $10.00 budget" in msg["systemMessage"]  # $50 = 5x $10
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
    # Session grows past the limit -> the nudge then fires (at 5x here).
    make_transcript(tmp_path, out_tokens=1_000_000)
    r2 = run_hook(payload, tmp_path, env)
    assert "passed 5× your $10.00 budget" in json.loads(r2.stdout)["systemMessage"]


def test_budget_renudges_at_each_multiple(tmp_path):
    # $15 on a $10 budget -> first nudge (1x). Growing past $20 -> 2x nudge.
    t = make_transcript(tmp_path, out_tokens=300_000)      # fable: ≈ $15
    payload = {"session_id": "bud-5", "transcript_path": str(t)}
    env = {"TOKEN_USAGE_BUDGET_USD": "10"}
    r1 = run_hook(payload, tmp_path, env)
    assert "passed your $10.00 budget" in json.loads(r1.stdout)["systemMessage"]
    r2 = run_hook(payload, tmp_path, env)                  # unchanged cost: silent
    assert r2.stdout.strip() == ""
    make_transcript(tmp_path, out_tokens=500_000)          # grows to ≈ $25
    r3 = run_hook(payload, tmp_path, env)
    msg = json.loads(r3.stdout)["systemMessage"]
    assert "2×" in msg and "$10.00" in msg
    r4 = run_hook(payload, tmp_path, env)                  # 2x already notified: silent
    assert r4.stdout.strip() == ""
    ledger = json.loads((tmp_path / "ledger" / "bud-5.json").read_text())
    assert ledger["budget_notified_multiple"] == 2
    assert ledger["budget_notified"] is True               # legacy field still maintained


def test_budget_jump_message_names_actual_multiple(tmp_path):
    # A session that first reports in at 2x must say 2x, not just "your budget".
    t = make_transcript(tmp_path, out_tokens=500_000)      # fable: ≈ $25
    payload = {"session_id": "bud-7", "transcript_path": str(t)}
    r = run_hook(payload, tmp_path, {"TOKEN_USAGE_BUDGET_USD": "10"})
    msg = json.loads(r.stdout)["systemMessage"]
    assert "2× your $10.00 budget" in msg
    ledger = json.loads((tmp_path / "ledger" / "bud-7.json").read_text())
    assert ledger["budget_notified_multiple"] == 2


def test_hook_leaves_no_stray_tmp_and_valid_latest(tmp_path):
    t = make_transcript(tmp_path)
    r = run_hook({"session_id": "tmp-1", "transcript_path": str(t)}, tmp_path)
    assert r.returncode == 0
    ledger_dir = tmp_path / "ledger"
    assert list(ledger_dir.glob("*.tmp")) == []
    latest = ledger_dir / "latest.json"
    assert latest.is_symlink() and latest.resolve() == (ledger_dir / "tmp-1.json").resolve()


def test_budget_multiple_survives_legacy_bool_ledger(tmp_path):
    # A 0.2.x ledger has only budget_notified: true — treat as 1x already sent.
    t = make_transcript(tmp_path, out_tokens=300_000)      # ≈ $15 -> still 1x
    payload = {"session_id": "bud-6", "transcript_path": str(t)}
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True)
    (ledger_dir / "bud-6.json").write_text('{"budget_notified": true}')
    r = run_hook(payload, tmp_path, {"TOKEN_USAGE_BUDGET_USD": "10"})
    assert r.returncode == 0
    assert r.stdout.strip() == ""                          # 1x was already notified


def make_session_with_agent(tmp_path, main_out=100, agent_out=40):
    proj = tmp_path / "proj"
    main = write_jsonl(proj / "sess-1.jsonl", [
        user("2026-06-12T10:00:00Z", command="/big"),
        assistant("2026-06-12T10:00:01Z", usage(out=main_out), request_id="r1"),
    ])
    agent = write_jsonl(proj / "sess-1" / "subagents" / "agent-001.jsonl", [
        assistant("2026-06-12T10:00:30Z", usage(out=agent_out), request_id="a1"),
    ])
    return main, agent


def test_subagent_stop_reaggregates_full_session(tmp_path):
    # SubagentStop delivers the subagent's own sidechain transcript; the hook
    # must resolve the owning session and ledger the WHOLE session, not the
    # sidechain alone.
    main, agent = make_session_with_agent(tmp_path)
    r = run_hook({"session_id": "sess-1", "transcript_path": str(agent),
                  "hook_event_name": "SubagentStop"}, tmp_path)
    assert r.returncode == 0
    ledger = json.loads((tmp_path / "ledger" / "sess-1.json").read_text())
    assert ledger["total"]["usage"]["output"] == 140       # main 100 + agent 40


def test_subagent_stop_without_main_transcript_bails(tmp_path):
    # If the owning session transcript can't be found, never write a ledger
    # from sidechain-only data.
    agent = write_jsonl(tmp_path / "proj" / "sess-2" / "subagents" / "agent-001.jsonl", [
        assistant("2026-06-12T10:00:30Z", usage(out=40), request_id="a1"),
    ])
    r = run_hook({"session_id": "sess-2", "transcript_path": str(agent),
                  "hook_event_name": "SubagentStop"}, tmp_path)
    assert r.returncode == 0
    assert not (tmp_path / "ledger" / "sess-2.json").exists()


def test_subagent_stop_never_fires_budget_nudge(tmp_path):
    # Parallel SubagentStop hooks race the ledger read-modify-write, so budget
    # nudges only fire from the (serial) Stop hook. SubagentStop still updates
    # the ledger but must not consume the pending multiple.
    main, agent = make_session_with_agent(tmp_path, main_out=1_000_000)  # ≈ $50+
    env = {"TOKEN_USAGE_BUDGET_USD": "10"}
    r1 = run_hook({"session_id": "sess-1", "transcript_path": str(agent),
                   "hook_event_name": "SubagentStop"}, tmp_path, env)
    assert r1.returncode == 0
    assert r1.stdout.strip() == ""                         # no nudge mid-turn
    ledger = json.loads((tmp_path / "ledger" / "sess-1.json").read_text())
    assert ledger["budget_notified_multiple"] == 0         # left for Stop to claim
    r2 = run_hook({"session_id": "sess-1", "transcript_path": str(main),
                   "hook_event_name": "Stop"}, tmp_path, env)
    assert "your $10.00 budget" in json.loads(r2.stdout)["systemMessage"]


def test_hooks_json_registers_stop_and_subagent_stop():
    # SubagentStop keeps the ledger (and statusline) fresh during long
    # agent-heavy turns instead of only at turn end.
    hooks = json.loads((SCRIPT.parent.parent / "hooks" / "hooks.json").read_text())
    events = hooks["hooks"]
    assert set(events) >= {"Stop", "SubagentStop"}
    commands = {e: events[e][0]["hooks"][0]["command"] for e in ("Stop", "SubagentStop")}
    assert commands["Stop"] == commands["SubagentStop"]


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
