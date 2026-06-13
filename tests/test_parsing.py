import json

from conftest import assistant, usage, user, write_jsonl


def skill_use(ts, skill, u, request_id=None, use_id=None):
    """Assistant turn that invokes a Cowork Skill via a tool_use block."""
    return {
        "type": "assistant", "timestamp": ts, "requestId": request_id,
        "message": {
            "role": "assistant", "model": "claude-fable-5", "usage": u,
            "content": [{
                "type": "tool_use", "id": use_id or f"toolu_{skill}",
                "name": "Skill", "input": {"skill": skill},
            }],
        },
    }


def test_dedup_keeps_per_field_maxima(tu, tmp_path):
    # Two streamed snapshots of one request: output grows 10 -> 50.
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z"),
        assistant("2026-06-12T10:00:01Z", usage(inp=100, out=10), request_id="req_1"),
        assistant("2026-06-12T10:00:02Z", usage(inp=100, out=50), request_id="req_1"),
    ])
    by_model, _ = tu.sum_transcript(t)
    b = by_model["claude-fable-5"]
    assert b["output"] == 50          # maxima, not 60 (sum) or 10 (first-seen)
    assert b["input"] == 100
    assert b["requests"] == 1


def test_entries_without_request_id_each_count(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        assistant("2026-06-12T10:00:01Z", usage(out=10)),
        assistant("2026-06-12T10:00:02Z", usage(out=20)),
    ])
    by_model, _ = tu.sum_transcript(t)
    assert by_model["claude-fable-5"]["output"] == 30
    assert by_model["claude-fable-5"]["requests"] == 2


def test_rates_for_picks_longest_prefix(tu):
    pricing = {"claude-opus-4": {"input": 15.0, "output": 75.0},
               "claude-opus-4-5": {"input": 5.0, "output": 25.0}}
    assert tu.rates_for("claude-opus-4-5-20250929", pricing) == pricing["claude-opus-4-5"]


def test_rates_for_provider_prefixed_ids(tu):
    pricing = {"claude-opus-4-8": {"input": 5.0, "output": 25.0}}
    for model in (
        "claude-opus-4-8-20250601",
        "us.anthropic.claude-opus-4-8-20250601-v1:0",
        "anthropic.claude-opus-4-8-v1:0",
        "anthropic/claude-opus-4-8",
    ):
        assert tu.rates_for(model, pricing) == pricing["claude-opus-4-8"], model
    assert tu.rates_for("gpt-4o", pricing) is None


def test_cost_and_cache_savings_math(tu):
    pricing = {"m": {"input": 10.0, "output": 50.0}}
    by_model = {"m": {"input": 1_000_000, "output": 1_000_000,
                      "cache_read": 1_000_000, "cache_5m": 1_000_000,
                      "cache_1h": 1_000_000, "requests": 1}}
    # 10 + 50 + 10*0.1 + 10*1.25 + 10*2.0 = 93.5
    assert tu.cost_usd(by_model, pricing) == 93.5
    # savings: 1MTok read at 0.9 * input rate = 9.0
    assert tu.cache_savings_usd(by_model, pricing) == 9.0


def test_totals_reconcile_with_segments(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z", command="/commit"),
        assistant("2026-06-12T10:00:01Z", usage(out=10), request_id="r1"),
        user("2026-06-12T10:01:00Z", command="/review"),
        assistant("2026-06-12T10:01:01Z", usage(out=20), request_id="r2"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    seg_sum = sum(s["usage"]["output"] for s in data["segments"])
    assert seg_sum == data["total"]["usage"]["output"] == 30


def test_command_owns_followup_turns(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z", command="/code-review"),
        assistant("2026-06-12T10:00:01Z", usage(out=100), request_id="r1"),
        user("2026-06-12T10:05:00Z", text="yes, fix that"),          # follow-up
        assistant("2026-06-12T10:05:01Z", usage(out=50), request_id="r2"),
        user("2026-06-12T10:10:00Z", command="/commit"),             # next command
        assistant("2026-06-12T10:10:01Z", usage(out=10), request_id="r3"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["by_label"]["/code-review"]["usage"]["output"] == 150
    assert data["by_label"]["/commit"]["usage"]["output"] == 10
    assert tu.OTHER_LABEL not in data["by_label"]


def test_skill_tool_use_starts_sticky_segment(tu, tmp_path):
    # Cowork: a Skill tool_use opens its own segment that owns the invoking turn
    # and every follow-up until the next command/skill.
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z", text="make a report"),
        assistant("2026-06-12T10:00:01Z", usage(out=10), request_id="r1"),   # pre-skill
        skill_use("2026-06-12T10:00:02Z", "report", usage(out=40), request_id="r2"),
        assistant("2026-06-12T10:00:03Z", usage(out=20), request_id="r3"),   # owned by /report
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["by_label"]["/report"]["usage"]["output"] == 60  # 40 invoking + 20 follow-up
    assert data["by_label"]["/report"]["invocations"] == 1
    assert data["by_label"][tu.OTHER_LABEL]["usage"]["output"] == 10


def test_skill_streamed_duplicate_does_not_reopen_segment(tu, tmp_path):
    # Same tool-use id streamed twice (one requestId): one segment, maxima usage.
    t = write_jsonl(tmp_path / "s.jsonl", [
        skill_use("2026-06-12T10:00:01Z", "report", usage(out=10),
                  request_id="r1", use_id="toolu_1"),
        skill_use("2026-06-12T10:00:02Z", "report", usage(out=50),
                  request_id="r1", use_id="toolu_1"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["by_label"]["/report"]["usage"]["output"] == 50  # maxima, not 60
    assert data["by_label"]["/report"]["invocations"] == 1


def test_no_command_only_before_first_command(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z", text="hi"),
        assistant("2026-06-12T10:00:01Z", usage(out=5), request_id="r1"),
        user("2026-06-12T10:01:00Z", text="more"),                   # still pre-command
        assistant("2026-06-12T10:01:01Z", usage(out=5), request_id="r2"),
        user("2026-06-12T10:02:00Z", command="/commit"),
        assistant("2026-06-12T10:02:01Z", usage(out=10), request_id="r3"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["by_label"][tu.OTHER_LABEL]["usage"]["output"] == 10
    assert data["by_label"][tu.OTHER_LABEL]["invocations"] == 1      # ONE sticky segment
    assert data["by_label"]["/commit"]["usage"]["output"] == 10
