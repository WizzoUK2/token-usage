import json
import os

from conftest import assistant, usage, user, write_jsonl


def seed_projects(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    write_jsonl(proj / "-Users-x-repo-one" / "s1.jsonl", [
        user("2026-06-10T10:00:00Z", command="/review"),
        assistant("2026-06-10T10:00:01Z", usage(out=100), request_id="r1"),
    ])
    write_jsonl(proj / "-Users-x-repo-two" / "s2.jsonl", [
        user("2026-06-12T10:00:00Z", command="/commit"),
        assistant("2026-06-12T10:00:01Z", usage(out=50), request_id="r2"),
    ])
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    return proj


def test_history_by_project_and_command(tu, tmp_path, monkeypatch):
    seed_projects(tmp_path, monkeypatch)
    rows = tu.run_history(by="project")
    by_key = {r["key"]: r for r in rows["rows"]}
    assert by_key["-Users-x-repo-one"]["usage"]["output"] == 100
    assert by_key["-Users-x-repo-two"]["usage"]["output"] == 50
    cmd_rows = tu.run_history(by="command")
    assert {r["key"] for r in cmd_rows["rows"]} == {"/review", "/commit"}


def test_history_since_filters(tu, tmp_path, monkeypatch):
    seed_projects(tmp_path, monkeypatch)
    rows = tu.run_history(by="project", since="2026-06-11")
    assert [r["key"] for r in rows["rows"]] == ["-Users-x-repo-two"]


def test_history_cache_hit_and_invalidation(tu, tmp_path, monkeypatch):
    proj = seed_projects(tmp_path, monkeypatch)
    tu.run_history(by="project")
    cache_files = list((tmp_path / "cache" / "index").glob("*.json"))
    assert len(cache_files) == 2
    # Unchanged file -> cache reused (mtime of the cache entry stays put).
    before = {f: f.stat().st_mtime_ns for f in cache_files}
    tu.run_history(by="project")
    assert {f: f.stat().st_mtime_ns for f in cache_files} == before
    # Changed transcript -> its summary recomputes.
    target = proj / "-Users-x-repo-one" / "s1.jsonl"
    write_jsonl(target, [
        user("2026-06-10T10:00:00Z", command="/review"),
        assistant("2026-06-10T10:00:01Z", usage(out=999), request_id="r9"),
    ])
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 10))
    rows = tu.run_history(by="project")
    by_key = {r["key"]: r for r in rows["rows"]}
    assert by_key["-Users-x-repo-one"]["usage"]["output"] == 999


def test_history_by_day_uses_local_time(tu, tmp_path, monkeypatch):
    from datetime import datetime
    proj = tmp_path / "projects"
    write_jsonl(proj / "-Users-x-repo-one" / "s1.jsonl", [
        user("2026-06-10T23:30:00Z", command="/late"),
        assistant("2026-06-10T23:30:01Z", usage(out=10), request_id="r1"),
    ])
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    rows = tu.run_history(by="day")
    expected = datetime.fromisoformat("2026-06-10T23:30:00+00:00").astimezone().date().isoformat()
    assert [r["key"] for r in rows["rows"]] == [expected]


def test_history_since_relative_days(tu, tmp_path, monkeypatch):
    seed_projects(tmp_path, monkeypatch)
    assert tu.run_history(by="project", since="36500d")["rows"]      # ~100y: includes all
    assert tu.run_history(by="project", since="0d")["rows"] == []    # cutoff=now: excludes all


def seed_mixed_model_projects(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    write_jsonl(proj / "-Users-x-repo-one" / "s1.jsonl", [
        user("2026-06-10T10:00:00Z", command="/review"),
        assistant("2026-06-10T10:00:01Z", usage(out=100),
                  model="claude-fable-5", request_id="r1"),
    ])
    write_jsonl(proj / "-Users-x-repo-two" / "s2.jsonl", [
        user("2026-06-12T10:00:00Z", command="/commit"),
        assistant("2026-06-12T10:00:01Z", usage(out=50),
                  model="claude-haiku-4-5", request_id="r2"),
        assistant("2026-06-12T10:00:02Z", usage(out=25),
                  model="claude-haiku-4-5", request_id="r3"),
    ])
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(proj))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    return proj


def test_history_by_model(tu, tmp_path, monkeypatch):
    seed_mixed_model_projects(tmp_path, monkeypatch)
    rows = tu.run_history(by="model")
    by_key = {r["key"]: r for r in rows["rows"]}
    assert set(by_key) == {"claude-fable-5", "claude-haiku-4-5"}
    assert by_key["claude-fable-5"]["usage"]["output"] == 100
    assert by_key["claude-haiku-4-5"]["usage"]["output"] == 75
    assert by_key["claude-haiku-4-5"]["calls"] == 2          # API requests
    assert by_key["claude-fable-5"]["cost_usd"] > by_key["claude-haiku-4-5"]["cost_usd"]
    # The count column means API requests here, not sessions — label it so.
    assert "| Model | Requests |" in tu.render_history(rows)
    assert tu.render_history_csv(rows).splitlines()[0].startswith("model,requests,")


def test_since_rejects_malformed_values(tu, tmp_path, monkeypatch):
    import pytest
    seed_projects(tmp_path, monkeypatch)
    for bad in ("week", "7", "last-tuesday"):
        with pytest.raises(SystemExit):
            tu.run_history(by="project", since=bad)
    # Valid forms still pass.
    assert tu.run_history(by="project", since="7d") is not None
    assert tu.run_history(by="project", since="2026-06-01") is not None


def test_history_project_filter_composes_with_by(tu, tmp_path, monkeypatch):
    seed_mixed_model_projects(tmp_path, monkeypatch)
    rows = tu.run_history(by="command", project="repo-one")
    assert [r["key"] for r in rows["rows"]] == ["/review"]
    rows = tu.run_history(by="model", project="repo-two")
    assert [r["key"] for r in rows["rows"]] == ["claude-haiku-4-5"]


def test_history_csv_output(tu, tmp_path, monkeypatch):
    import csv
    import io
    seed_mixed_model_projects(tmp_path, monkeypatch)
    text = tu.render_history_csv(tu.run_history(by="project"))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert {r["project"] for r in rows} == {"-Users-x-repo-one", "-Users-x-repo-two"}
    one = next(r for r in rows if r["project"] == "-Users-x-repo-one")
    assert one["output"] == "100"                            # raw ints, not humanized
    assert float(one["cost_usd"]) > 0


def test_history_burn_rate_footer_for_relative_since(tu, tmp_path, monkeypatch):
    seed_projects(tmp_path, monkeypatch)
    # Relative window -> footer with per-day average and weekly projection.
    out = tu.render_history(tu.run_history(by="project", since="36500d"))
    assert "Burn rate" in out and "/day" in out and "/week" in out
    # No window (or absolute date) -> no projection; a partial window would lie.
    assert "Burn rate" not in tu.render_history(tu.run_history(by="project"))
    assert "Burn rate" not in tu.render_history(
        tu.run_history(by="project", since="2026-01-01"))


def test_history_burn_rate_math(tu):
    # 7-day window, $14 total -> $2.00/day, $14.00/week.
    line = tu.burn_rate_line(14.0, "7d")
    assert "$2.00/day" in line and "$14.00/week" in line
    assert tu.burn_rate_line(14.0, "2026-01-01") is None
    assert tu.burn_rate_line(None, "7d") is None
    assert tu.burn_rate_line(14.0, "0d") is None


def test_history_recovers_from_corrupt_cache_entry(tu, tmp_path, monkeypatch):
    seed_projects(tmp_path, monkeypatch)
    tu.run_history(by="project")
    victim = next((tmp_path / "cache" / "index").glob("*.json"))
    victim.write_text("{corrupt")
    rows = tu.run_history(by="project")
    assert {r["key"] for r in rows["rows"]} == {"-Users-x-repo-one", "-Users-x-repo-two"}
    assert json.loads(victim.read_text())["version"] == tu.INDEX_VERSION  # healed


def test_sum_by_day_splits_midnight_and_dedups(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=100), request_id="r1"),
        # streamed duplicate of r1 — must not double count
        assistant("2026-07-01T10:00:06Z", usage(inp=10, out=100), request_id="r1"),
        # 12:00Z next day: stays a different LOCAL day in any timezone CI runs in
        assistant("2026-07-02T12:00:00Z", usage(inp=10, out=300), request_id="r2"),
    ])
    by_day = tu.sum_by_day(t)
    days = sorted(by_day)
    assert len(days) == 2
    assert tu.sum_buckets(by_day[days[0]])["output"] == 100
    assert tu.sum_buckets(by_day[days[1]])["output"] == 300


def test_sum_by_day_timestampless_requests_fall_to_first_day(tu, tmp_path):
    entries = [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
        assistant(None, usage(out=50), request_id="r2"),
    ]
    entries[2].pop("timestamp")
    t = write_jsonl(tmp_path / "s.jsonl", entries)
    by_day = tu.sum_by_day(t)
    assert len(by_day) == 1
    assert tu.sum_buckets(next(iter(by_day.values())))["output"] == 150


def test_summarize_transcript_has_by_day_and_version_3(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=100), request_id="r1"),
        assistant("2026-07-02T01:00:00Z", usage(inp=10, out=300), request_id="r2"),
    ])
    s = tu.summarize_transcript(t, tu.load_pricing())
    assert s["version"] == 3
    assert len(s["by_day"]) == 2
    assert sum(d["usage"]["output"] for d in s["by_day"].values()) == 400
    assert all(d["cost_usd"] is not None for d in s["by_day"].values())


def test_summarize_by_day_includes_subagents(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
    ])
    write_jsonl(tmp_path / "projects" / "p" / "s" / "subagents" / "agent-1.jsonl", [
        assistant("2026-07-01T10:01:00Z", usage(out=40), request_id="a1"),
    ])
    s = tu.summarize_transcript(t, tu.load_pricing())
    assert sum(d["usage"]["output"] for d in s["by_day"].values()) == 140


def test_v2_index_entry_reparses_once(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
    ])
    s1, hit1 = tu.cached_summary(t, tu.load_pricing())
    assert not hit1 and s1["version"] == 3
    # forge a stale v2 entry: same mtime/size but version 2 and no by_day
    import hashlib, json as j
    cache_file = tu.index_dir() / (hashlib.sha1(str(t).encode()).hexdigest() + ".json")
    stale = j.loads(cache_file.read_text())
    stale["version"] = 2
    stale.pop("by_day")
    cache_file.write_text(j.dumps(stale))
    s2, hit2 = tu.cached_summary(t, tu.load_pricing())
    assert not hit2 and s2["version"] == 3 and "by_day" in s2   # re-parsed
    s3, hit3 = tu.cached_summary(t, tu.load_pricing())
    assert hit3                                                  # now cached


def test_history_by_day_splits_sessions_across_days(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
        assistant("2026-07-02T12:00:00Z", usage(out=300), request_id="r2"),
    ])
    data = tu.run_history(by="day")
    rows = {r["key"]: r for r in data["rows"]}
    assert len(rows) == 2
    day1, day2 = sorted(rows)
    assert rows[day1]["usage"]["output"] == 100
    assert rows[day2]["usage"]["output"] == 300
    assert rows[day1]["calls"] == 1 and rows[day2]["calls"] == 1  # touches both days


def test_index_recomputes_when_pricing_changes(tu, monkeypatch, tmp_path):
    # A pricing update (bundled or user overlay) must not leave stale cached
    # costs in the history index, which only re-validates by (mtime, size).
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    t = write_jsonl(tmp_path / "projects" / "proj-a" / "s1.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(out=1_000_000),
                  model="claude-sonnet-5", request_id="r1"),
    ])
    old = {"claude-sonnet-5": {"input": 3.0, "output": 15.0}}
    new = {"claude-sonnet-5": {"input": 2.0, "output": 10.0}}
    s1, hit1 = tu.cached_summary(t, old)
    s2, hit2 = tu.cached_summary(t, old)
    s3, hit3 = tu.cached_summary(t, new)
    assert (hit1, hit2, hit3) == (False, True, False)
    assert s1["total"]["cost_usd"] == 15.0
    assert s3["total"]["cost_usd"] == 10.0


def test_unreadable_transcripts_are_skipped_and_disclosed(tu, tmp_path, monkeypatch):
    # A directory named like a transcript (IsADirectoryError) used to be
    # swallowed silently: every row vanished and the caller was told "no
    # usage" with a clean exit. Skip it, say so on stderr, and name it.
    proj = seed_projects(tmp_path, monkeypatch)
    (proj / "-Users-x-repo-one" / "junk.jsonl").mkdir()
    data = tu.run_history(by="project")
    assert {r["key"] for r in data["rows"]} == {"-Users-x-repo-one", "-Users-x-repo-two"}
    assert data["skipped_transcripts"] == [str(proj / "-Users-x-repo-one" / "junk.jsonl")]
    out = tu.render_history(data)
    assert "1 transcript(s) skipped (unreadable)" in out and "junk.jsonl" in out
    top = tu.run_top_consumers(by="session", since="2026-01-01")
    assert [r["session_id"] for r in top["rows"]] == ["s1", "s2"]
    assert len(top["skipped_transcripts"]) == 1
    assert "1 transcript(s) skipped (unreadable)" in tu.render_top_consumers(top)


def test_iter_summaries_skips_a_non_dict_cache_entry(tu, tmp_path, monkeypatch, capsys):
    # A cache file holding valid JSON that isn't an object used to crash the
    # scan with AttributeError inside cached_summary's freshness check.
    seed_projects(tmp_path, monkeypatch)
    tu.run_history(by="project")
    for f in (tmp_path / "cache" / "index").glob("*.json"):
        f.write_text("[]")
    skipped = []
    rows = list(tu.iter_summaries(tu.load_pricing(), skipped=skipped))
    assert len(rows) == 2 and skipped == []      # re-parsed, not skipped
    assert capsys.readouterr().err == ""


def test_unwritable_cache_dir_still_returns_rows(tu, tmp_path, monkeypatch, capsys):
    import os as _os
    if _os.geteuid() == 0:
        import pytest as _pytest
        _pytest.skip("root ignores directory permissions")
    seed_projects(tmp_path, monkeypatch)
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    cache.chmod(0o500)
    monkeypatch.setattr(tu, "_CACHE_WRITE_WARNED", False)
    try:
        data = tu.run_history(by="project")
    finally:
        cache.chmod(0o700)
    by_key = {r["key"]: r for r in data["rows"]}
    assert by_key["-Users-x-repo-one"]["usage"]["output"] == 100
    assert by_key["-Users-x-repo-two"]["usage"]["output"] == 50
    assert data["skipped_transcripts"] == []
    err = capsys.readouterr().err
    assert err.count("cannot write summary cache") == 1      # one warning per process


def test_window_insights_and_baseline_disclose_skipped_transcripts(tu, tmp_path, monkeypatch):
    proj = seed_projects(tmp_path, monkeypatch)
    (proj / "-Users-x-repo-two" / "junk.jsonl").mkdir()
    window = tu.run_insights(since="2026-01-01")
    assert len(window["skipped_transcripts"]) == 1
    assert "1 transcript(s) skipped (unreadable)" in tu.render_insights(window)
    baseline = tu.compute_baseline(tu.load_pricing(), project="-Users-x-repo-one")
    assert baseline["skipped_transcripts"] == 1              # a count is enough here
