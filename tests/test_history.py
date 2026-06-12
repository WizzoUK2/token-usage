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
    rows = tu.run_history(by="project", as_json=True)
    by_key = {r["key"]: r for r in rows["rows"]}
    assert by_key["-Users-x-repo-one"]["usage"]["output"] == 100
    assert by_key["-Users-x-repo-two"]["usage"]["output"] == 50
    cmd_rows = tu.run_history(by="command", as_json=True)
    assert {r["key"] for r in cmd_rows["rows"]} == {"/review", "/commit"}


def test_history_since_filters(tu, tmp_path, monkeypatch):
    seed_projects(tmp_path, monkeypatch)
    rows = tu.run_history(by="project", since="2026-06-11", as_json=True)
    assert [r["key"] for r in rows["rows"]] == ["-Users-x-repo-two"]


def test_history_cache_hit_and_invalidation(tu, tmp_path, monkeypatch):
    proj = seed_projects(tmp_path, monkeypatch)
    tu.run_history(by="project", as_json=True)
    cache_files = list((tmp_path / "cache" / "index").glob("*.json"))
    assert len(cache_files) == 2
    # Unchanged file -> cache reused (mtime of the cache entry stays put).
    before = {f: f.stat().st_mtime_ns for f in cache_files}
    tu.run_history(by="project", as_json=True)
    assert {f: f.stat().st_mtime_ns for f in cache_files} == before
    # Changed transcript -> its summary recomputes.
    target = proj / "-Users-x-repo-one" / "s1.jsonl"
    write_jsonl(target, [
        user("2026-06-10T10:00:00Z", command="/review"),
        assistant("2026-06-10T10:00:01Z", usage(out=999), request_id="r9"),
    ])
    os.utime(target, (target.stat().st_atime, target.stat().st_mtime + 10))
    rows = tu.run_history(by="project", as_json=True)
    by_key = {r["key"]: r for r in rows["rows"]}
    assert by_key["-Users-x-repo-one"]["usage"]["output"] == 999
