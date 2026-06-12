import json

from conftest import assistant, usage, user, write_jsonl


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
