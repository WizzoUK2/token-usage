"""Insights engine: baseline, rules, rendering."""
from conftest import assistant, usage, user, write_jsonl


def _agg(tu, entries, tmp_path, name="x"):
    t = write_jsonl(tmp_path / f"{name}.jsonl", entries)
    return tu.aggregate(tu.parse_session(t), tu.load_pricing())


BASE = {"sessions": 10, "median_session_cost": 1.0,
        "commands": {"/go": {"sessions": 10, "median_cost": 0.5,
                             "median_cache_ratio": 0.9}}}
THIN = {"sessions": 2, "median_session_cost": 1.0, "commands": {}}


def _session(tmp_path, name, out_tokens, ts="2026-07-01T10:00:00Z", project="p"):
    return write_jsonl(tmp_path / "projects" / project / f"{name}.jsonl", [
        user(ts, command="/go"),
        assistant(ts, usage(inp=100, out=out_tokens, cache_read=1000), request_id="r1"),
    ])


def test_median(tu):
    assert tu._median([]) is None
    assert tu._median([3.0]) == 3.0
    assert tu._median([1.0, 3.0]) == 2.0
    assert tu._median([1.0, 2.0, 9.0]) == 2.0


def test_cache_ratio(tu):
    u = tu.empty_usage()
    assert tu.cache_ratio(u) is None
    u.update(cache_read=900, input=50, cache_5m=50)
    assert tu.cache_ratio(u) == 0.9


def test_finding_shape(tu):
    f = tu.finding("cost-outlier", "warn", "msg", ratio=2.5)
    assert f == {"rule": "cost-outlier", "severity": "warn",
                 "message": "msg", "data": {"ratio": 2.5}}


def test_compute_baseline(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, out in enumerate([1000, 2000, 3000, 4000, 5000]):
        _session(tmp_path, f"s{i}", out, ts=ts)
    current = _session(tmp_path, "current", 99000, ts=ts)
    b = tu.compute_baseline(tu.load_pricing(), project="p", exclude=str(current))
    assert b["sessions"] == 5                     # excludes the current transcript
    assert b["median_session_cost"] is not None
    assert b["commands"]["/go"]["sessions"] == 5
    assert 0 < b["commands"]["/go"]["median_cache_ratio"] < 1


def test_compute_baseline_scopes_to_project(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _session(tmp_path, "mine", 1000, ts=ts, project="p")
    _session(tmp_path, "other", 1000, ts=ts, project="q")
    b = tu.compute_baseline(tu.load_pricing(), project="p")
    assert b["sessions"] == 1


def test_cost_outlier_warn_info_and_quiet(tu, tmp_path):
    big = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                    assistant("2026-07-01T10:00:05Z", usage(out=70000), request_id="r1")],
               tmp_path, "big")        # 70k out on fable-5 = $3.50 = 3.5x median
    rules = {f["rule"]: f for f in tu.session_insights(big, BASE)}
    assert rules["cost-outlier"]["severity"] == "warn"
    assert "3.5×" in rules["cost-outlier"]["message"]
    mid = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                    assistant("2026-07-01T10:00:05Z", usage(out=44000), request_id="r1")],
               tmp_path, "mid")        # $2.20 = 2.2x -> info
    assert {f["rule"]: f for f in tu.session_insights(mid, BASE)}["cost-outlier"]["severity"] == "info"
    small = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                      assistant("2026-07-01T10:00:05Z", usage(out=2000), request_id="r1")],
                 tmp_path, "small")    # $0.10 -> quiet
    assert "cost-outlier" not in {f["rule"] for f in tu.session_insights(small, BASE)}


def test_cost_outlier_skipped_on_thin_baseline(tu, tmp_path):
    big = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                    assistant("2026-07-01T10:00:05Z", usage(out=70000), request_id="r1")],
               tmp_path, "big2")
    assert "cost-outlier" not in {f["rule"] for f in tu.session_insights(big, THIN)}


def test_cache_regression_fires_and_respects_norm(tu, tmp_path):
    # all-input, no cache reads: ratio 0.0 vs norm 0.9 -> warn
    cold = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                     assistant("2026-07-01T10:00:05Z", usage(inp=5000, out=10), request_id="r1")],
                tmp_path, "cold")
    rules = {f["rule"] for f in tu.session_insights(cold, BASE)}
    assert "cache-regression" in rules
    warm = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                     assistant("2026-07-01T10:00:05Z",
                               usage(inp=100, out=10, cache_read=9000), request_id="r1")],
                tmp_path, "warm")      # ratio ~0.99 -> quiet
    assert "cache-regression" not in {f["rule"] for f in tu.session_insights(warm, BASE)}


def test_adhoc_dominance(tu, tmp_path):
    heavy = _agg(tu, [user("2026-07-01T10:00:00Z", "just chatting"),
                      assistant("2026-07-01T10:00:05Z", usage(out=60000), request_id="r1"),
                      user("2026-07-01T11:00:00Z", command="/go"),
                      assistant("2026-07-01T11:00:05Z", usage(out=1000), request_id="r2")],
                 tmp_path, "adhoc")
    f = {f["rule"]: f for f in tu.session_insights(heavy, THIN)}["adhoc-dominance"]
    assert f["severity"] == "info" and "ad-hoc" in f["message"]


def test_findings_sorted_warn_first(tu, tmp_path):
    big = _agg(tu, [user("2026-07-01T10:00:00Z", "chat"),
                    assistant("2026-07-01T10:00:05Z", usage(inp=5000, out=70000), request_id="r1")],
               tmp_path, "sortcheck")
    found = tu.session_insights(big, BASE)
    sevs = [f["severity"] for f in found]
    assert sevs == sorted(sevs, key=lambda s: {"warn": 0, "info": 1}[s])


def test_unpriced_models_rule(tu, tmp_path):
    a = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                  assistant("2026-07-01T10:00:05Z", usage(out=100),
                            model="claude-mystery-9", request_id="r1")],
             tmp_path, "unpriced")
    f = {f["rule"]: f for f in tu.session_insights(a, THIN)}["unpriced-models"]
    assert f["severity"] == "warn"
    assert "claude-mystery-9" in f["message"] and "pricing.json" in f["message"]


def test_agent_fanout_rule(tu, tmp_path):
    t = write_jsonl(tmp_path / "fan.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(out=1000), request_id="r1"),
    ])
    write_jsonl(tmp_path / "fan" / "subagents" / "agent-1.jsonl", [
        assistant("2026-07-01T10:01:00Z", usage(out=50000), request_id="a1"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    f = {f["rule"]: f for f in tu.session_insights(data, THIN)}["agent-fanout"]
    assert f["severity"] == "info" and "/go" in f["message"] and "subagent" in f["message"]


def test_budget_pace_rule(tu, tmp_path):
    a = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                  assistant("2026-07-01T10:00:05Z", usage(out=160000), request_id="r1")],
             tmp_path, "pace")   # $8.00 on fable-5
    f = {f["rule"]: f for f in tu.session_insights(a, THIN, budget=10.0)}["budget-pace"]
    assert "$8.00" in f["message"] and "$10.00" in f["message"]
    # over budget: the hook already nudged — insights stays quiet
    assert "budget-pace" not in {f["rule"] for f in tu.session_insights(a, THIN, budget=5.0)}
    # no budget set
    assert "budget-pace" not in {f["rule"] for f in tu.session_insights(a, THIN)}


def _summary(ts, cost, label_costs=None, by_model=None):
    return {"first_ts": ts, "total": {"cost_usd": cost, "usage": {}},
            "by_label": {k: {"cost_usd": v, "usage": {}, "invocations": 1}
                         for k, v in (label_costs or {}).items()},
            "by_model": by_model or {}}


CUTOFF, NOW = "2026-07-01T00:00:00Z", "2026-07-11T00:00:00Z"  # midpoint 07-06


def test_spend_trend_warn_and_direction(tu):
    ss = [_summary("2026-07-02T10:00:00Z", 10.0),
          _summary("2026-07-08T10:00:00Z", 20.0)]
    f = {f["rule"]: f for f in tu.window_insights(ss, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert f["spend-trend"]["severity"] == "warn" and "up" in f["spend-trend"]["message"]
    down = [_summary("2026-07-02T10:00:00Z", 20.0),
            _summary("2026-07-08T10:00:00Z", 14.0)]   # -30% -> info
    f = {f["rule"]: f for f in tu.window_insights(down, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert f["spend-trend"]["severity"] == "info" and "down" in f["spend-trend"]["message"]


def test_spend_trend_quiet_when_flat_or_empty_first_half(tu):
    flat = [_summary("2026-07-02T10:00:00Z", 10.0),
            _summary("2026-07-08T10:00:00Z", 11.0)]
    assert "spend-trend" not in {f["rule"] for f in
                                 tu.window_insights(flat, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    empty = [_summary("2026-07-08T10:00:00Z", 11.0)]
    assert tu.window_insights(empty, CUTOFF, tu.DEFAULT_PRICING, now=NOW) == []


def test_top_mover(tu):
    ss = [_summary("2026-07-02T10:00:00Z", 10.0, {"/review": 1.0}),
          _summary("2026-07-08T10:00:00Z", 22.0, {"/review": 11.0})]
    f = {f["rule"]: f for f in tu.window_insights(ss, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert "/review" in f["top-mover"]["message"]


def test_window_unpriced(tu):
    ss = [_summary("2026-07-02T10:00:00Z", 1.0,
                   by_model={"claude-mystery-9": dict(tu.empty_usage(), output=100)})]
    f = {f["rule"]: f for f in tu.window_insights(ss, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert "claude-mystery-9" in f["unpriced-models"]["message"]


def test_window_insights_bare_date_cutoff_default_now(tu):
    # since_cutoff passes bare YYYY-MM-DD through; must not crash with now=None
    ss = [_summary("2026-07-02T10:00:00Z", 10.0)]
    assert isinstance(tu.window_insights(ss, "2026-07-01", tu.DEFAULT_PRICING), list)


def test_window_insights_timestampless_sessions_excluded_from_halves(tu):
    ss = [_summary(None, 100.0),                       # must not inflate either half
          _summary("2026-07-02T10:00:00Z", 10.0),
          _summary("2026-07-08T10:00:00Z", 20.0)]      # +100% -> warn still fires
    f = {f["rule"]: f for f in tu.window_insights(ss, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert f["spend-trend"]["severity"] == "warn"
    assert f["spend-trend"]["data"]["first_half"] == 10.0


import json as jsonlib
import os
import subprocess
import sys


def test_render_insights_empty_and_lines(tu):
    assert tu.render_insights({"findings": []}) == "No notable findings."
    r = {"findings": [tu.finding("x", "warn", "watch out"),
                      tu.finding("y", "info", "fyi")]}
    assert tu.render_insights(r) == "- [warn] watch out\n- [info] fyi"


def test_render_insights_names_the_session_in_both_branches(tu):
    # Which session was measured matters most when discovery guessed it, and
    # "nothing to report" is exactly when the reader has no other clue.
    path = "/home/u/.claude/projects/-Users-x-alpha/aaa-111.jsonl"
    assert tu.render_insights({"findings": [], "transcript_path": path}) == \
        "No notable findings. (session: -Users-x-alpha/aaa-111.jsonl)"
    r = {"findings": [tu.finding("x", "warn", "watch out")], "transcript_path": path}
    assert tu.render_insights(r) == \
        "- [warn] watch out\n(session: -Users-x-alpha/aaa-111.jsonl)"
    # Window mode has no session to name, so the bare sentence stands.
    assert tu.render_insights({"findings": [], "mode": "window"}) == "No notable findings."


def test_run_insights_session_mode(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(5):
        _session(tmp_path, f"s{i}", 2000, ts=ts)          # ~$0.10 median
    current = _session(tmp_path, "current", 70000, ts=ts)  # $3.50
    r = tu.run_insights(transcript=current)
    assert r["mode"] == "session"
    assert "cost-outlier" in {f["rule"] for f in r["findings"]}
    assert r["baseline"]["sessions"] == 5


def test_run_insights_window_mode(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _session(tmp_path, "old", 20000, ts=old)
    _session(tmp_path, "new", 60000, ts=new)
    r = tu.run_insights(since="10d")
    assert r["mode"] == "window"
    assert "spend-trend" in {f["rule"] for f in r["findings"]}


def test_insights_cli_json(tu, monkeypatch, tmp_path):
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
    ])
    env = {"TOKEN_USAGE_PROJECTS_DIR": str(tmp_path / "projects"),
           "TOKEN_USAGE_LEDGER_DIR": str(tmp_path / "cache"),
           "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
           "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, str(tu.__file__ if hasattr(tu, "__file__")
                          else "scripts/token_usage.py"), "insights", str(t), "--json"],
                         capture_output=True, text=True, env=env, check=False)
    assert out.returncode == 0
    data = jsonlib.loads(out.stdout)
    assert data["mode"] == "session" and isinstance(data["findings"], list)


def test_insights_cli_rejects_transcript_plus_since(tu, tmp_path):
    import subprocess
    import sys
    out = subprocess.run([sys.executable, "scripts/token_usage.py", "insights",
                          str(tmp_path / "x.jsonl"), "--since", "7d"],
                         capture_output=True, text=True, check=False)
    assert out.returncode != 0 and "--since" in out.stderr
