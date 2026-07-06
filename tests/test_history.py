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


def test_history_recovers_from_corrupt_cache_entry(tu, tmp_path, monkeypatch):
    seed_projects(tmp_path, monkeypatch)
    tu.run_history(by="project")
    victim = next((tmp_path / "cache" / "index").glob("*.json"))
    victim.write_text("{corrupt")
    rows = tu.run_history(by="project")
    assert {r["key"] for r in rows["rows"]} == {"-Users-x-repo-one", "-Users-x-repo-two"}
    assert json.loads(victim.read_text())["version"] == tu.INDEX_VERSION  # healed
