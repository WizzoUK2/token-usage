"""Transcript resolution order used by the CLI and the MCP server."""
import os

from conftest import assistant, usage, user, write_jsonl


def seed(tmp_path, monkeypatch):
    proj = tmp_path / "projects"
    a = write_jsonl(proj / "-Users-x-alpha" / "aaa-111.jsonl", [
        user("2026-06-10T10:00:00Z"),
        assistant("2026-06-10T10:00:01Z", usage(out=10), request_id="r1"),
    ])
    b = write_jsonl(proj / "-Users-x-beta" / "bbb-222.jsonl", [
        user("2026-06-12T10:00:00Z"),
        assistant("2026-06-12T10:00:01Z", usage(out=20), request_id="r2"),
    ])
    # Make b the newest file on disk regardless of write order.
    os.utime(a, (1_700_000_000, 1_700_000_000))
    os.utime(b, (1_700_000_100, 1_700_000_100))
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(proj))
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT", raising=False)
    return proj, a, b


def test_explicit_path_wins_and_must_exist(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    assert tu.locate_transcript(str(a)) == a
    assert tu.locate_transcript(str(tmp_path / "missing.jsonl")) is None


def test_session_id_is_searched_across_projects(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    assert tu.locate_transcript(session_id="aaa-111") == a
    assert tu.locate_transcript(session_id="nope-999") is None
    # Path characters are stripped so an id can never escape the projects dir.
    assert tu.locate_transcript(session_id="../../etc/passwd") is None


def test_project_dir_picks_that_projects_newest(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    # slug("/Users/x/alpha") == "-Users-x-alpha"
    assert tu.locate_transcript(project_dir="/Users/x/alpha") == a
    assert tu.find_latest_transcript(project_dir="/Users/x/beta") == b


def test_unknown_project_falls_back_to_newest_anywhere(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)  # cwd slug matches no project either
    assert tu.locate_transcript(project_dir="/Users/x/nothing-here") == b
    assert tu.locate_transcript() == b


def test_env_transcript_overrides_discovery(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch)
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(a))
    assert tu.locate_transcript(project_dir="/Users/x/beta") == a


def test_resolve_transcript_exits_when_nothing_found(tu, tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        tu.resolve_transcript(None)
    assert "no transcript found" in str(e.value)
