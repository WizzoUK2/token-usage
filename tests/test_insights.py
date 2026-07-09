"""Insights engine: baseline, rules, rendering."""
from conftest import usage, user, assistant, write_jsonl


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
