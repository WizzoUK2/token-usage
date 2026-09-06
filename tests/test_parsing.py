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


def test_bundled_pricing_covers_current_models(tu):
    pricing = tu.load_pricing()
    # Sonnet 5's $2/$10 launch price became the standard price (Sept 2026).
    assert tu.rates_for("claude-sonnet-5", pricing) == {"input": 2.0, "output": 10.0}
    assert tu.rates_for("claude-sonnet-5-20260601", pricing) == {"input": 2.0, "output": 10.0}
    assert tu.rates_for("claude-mythos-5", pricing) == {"input": 10.0, "output": 50.0}
    assert tu.rates_for("claude-opus-5", pricing) == {"input": 5.0, "output": 25.0}
    assert tu.rates_for("claude-opus-5-20260724", pricing) == {"input": 5.0, "output": 25.0}
    assert tu.rates_for("claude-3-5-haiku-20241022", pricing) == {"input": 0.8, "output": 4.0}


def test_fable_5_1_has_its_own_cache_read_rate(tu):
    # Fable 5.1 / Mythos 5.1 bill cache hits at $0.25/MTok (0.025x), not the
    # usual 0.1x — so they must not fall through to the "claude-fable-5" key.
    pricing = tu.load_pricing()
    for model in ("claude-fable-5-1", "claude-fable-5-1-20260901", "claude-mythos-5-1"):
        assert tu.rates_for(model, pricing) == {"input": 10.0, "output": 50.0,
                                                "cache_read": 0.25}, model
    assert "cache_read" not in tu.rates_for("claude-fable-5", pricing)


def test_rates_for_prefix_stops_at_segment_boundary(tu):
    # "claude-opus-4-10" must not match the "claude-opus-4-1" key — it should
    # fall through to the family base key "claude-opus-4".
    pricing = {"claude-opus-4": {"input": 1.0, "output": 2.0},
               "claude-opus-4-1": {"input": 15.0, "output": 75.0}}
    assert tu.rates_for("claude-opus-4-10", pricing) == pricing["claude-opus-4"]
    assert tu.rates_for("claude-opus-4-1-20250805", pricing) == pricing["claude-opus-4-1"]
    assert tu.rates_for("claude-opus-4", pricing) == pricing["claude-opus-4"]


def test_cost_and_cache_savings_math(tu):
    pricing = {"m": {"input": 10.0, "output": 50.0}}
    by_model = {"m": {"input": 1_000_000, "output": 1_000_000,
                      "cache_read": 1_000_000, "cache_5m": 1_000_000,
                      "cache_1h": 1_000_000, "requests": 1}}
    # 10 + 50 + 10*0.1 + 10*1.25 + 10*2.0 = 93.5
    assert tu.cost_usd(by_model, pricing) == 93.5
    # savings: 1MTok read at 0.9 * input rate = 9.0
    assert tu.cache_savings_usd(by_model, pricing) == 9.0


def test_cost_uses_per_model_cache_read_rate_when_given(tu):
    pricing = {"m": {"input": 10.0, "output": 50.0, "cache_read": 0.25}}
    by_model = {"m": {"input": 1_000_000, "output": 1_000_000,
                      "cache_read": 1_000_000, "cache_5m": 1_000_000,
                      "cache_1h": 1_000_000, "requests": 1}}
    # 10 + 50 + 0.25 + 10*1.25 + 10*2.0 = 92.75
    assert tu.cost_usd(by_model, pricing) == 92.75
    # savings: 1MTok read at (10 - 0.25) = 9.75
    assert tu.cache_savings_usd(by_model, pricing) == 9.75


def test_project_slug_replaces_all_non_alphanumerics(tu):
    # Claude Code slugs a project path by replacing every non-alphanumeric
    # character with a dash — including spaces, not just / . _
    assert tu.project_slug("/Users/c/My Projects/app_v2.0") == "-Users-c-My-Projects-app-v2-0"
    assert tu.project_slug("/a/b-c") == "-a-b-c"


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


def test_undecodable_transcript_bytes_are_replaced_not_fatal(tu, tmp_path):
    # A transcript with a few undecodable bytes must still parse: open() ran
    # with strict UTF-8, so one stray byte turned every reader (report, json,
    # insights, the hook, the corpus scan) into a UnicodeDecodeError traceback.
    p = tmp_path / "t.jsonl"
    p.write_bytes(
        json.dumps(user("2026-06-12T10:00:00Z", command="/build")).encode() + b"\n"
        + json.dumps(assistant("2026-06-12T10:00:01Z", usage(out=100), request_id="r1")).encode() + b"\n"
        + b'{"type": "user", "message": {"role": "user", "content": "caf\xe9"}}\n'
        + b"\xff\xfe\x00\n"
        + json.dumps(assistant("2026-06-12T10:00:02Z", usage(out=50), request_id="r2")).encode()
        + b"\n")
    by_model, _ts = tu.sum_transcript(p)
    assert by_model["claude-fable-5"]["output"] == 150
    segs = tu.parse_session(p)
    assert sum(s["by_model"]["claude-fable-5"]["output"] for s in segs) == 150


def test_cli_report_survives_an_undecodable_transcript(tmp_path):
    import os
    import subprocess
    import sys

    from conftest import SCRIPT
    p = tmp_path / "t.jsonl"
    p.write_bytes(b"\xff\xfe\x00\n" + json.dumps(
        assistant("2026-06-12T10:00:01Z", usage(out=100), request_id="r1")).encode() + b"\n")
    r = subprocess.run([sys.executable, str(SCRIPT), "report", str(p)],
                       capture_output=True, text=True, check=False,
                       env={**os.environ, "TOKEN_USAGE_LEDGER_DIR": str(tmp_path / "c")})
    assert r.returncode == 0, r.stderr
    assert "Total" in r.stdout


def test_undecodable_lines_are_counted_and_warned_once(tu, tmp_path, capsys):
    # A few bad bytes must not cost the whole transcript — but the lines that
    # are lost have to be disclosed, or a real 500-token turn just evaporates
    # from the numbers. One warning per file, however many passes read it
    # (summarize_transcript alone parses each transcript twice).
    p = tmp_path / "t.jsonl"
    lines = [json.dumps(assistant("2026-06-12T10:00:01Z", usage(out=100),
                                  request_id="r1")).encode(),
             json.dumps(assistant("2026-06-12T10:00:02Z", usage(out=500),
                                  request_id="r2")).encode() + b"\xff",
             json.dumps(assistant("2026-06-12T10:00:03Z", usage(out=50),
                                  request_id="r3")).encode()]
    p.write_bytes(b"\n".join(lines) + b"\n")
    by_model, _ts = tu.sum_transcript(p)
    assert by_model["claude-fable-5"]["output"] == 150        # r1 and r3 still count
    tu.sum_transcript(p)                                      # a second pass over the file
    assert capsys.readouterr().err == \
        f"token-usage: {p}: 1 line(s) had undecodable bytes\n"


def test_a_transcript_that_decodes_to_nothing_is_unreadable(tu, tmp_path):
    import pytest
    # Nothing parsed at all is not a zero-usage session: summarize_transcript
    # raises so iter_summaries can route it to skipped_transcripts, the way an
    # unreadable file already is.
    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe\x00\x01\x02\n")
    with pytest.raises(tu.UnreadableTranscript):
        tu.summarize_transcript(binary, tu.load_pricing())
    assert isinstance(tu.UnreadableTranscript("x"), ValueError)   # iter_summaries catches it
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert tu.summarize_transcript(empty, tu.load_pricing())["total"]["usage"]["output"] == 0
