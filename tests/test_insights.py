"""Insights engine: baseline, rules, rendering."""
from conftest import usage, user, assistant, write_jsonl


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
