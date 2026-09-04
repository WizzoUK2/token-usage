"""Transcript resolution order used by the CLI and the MCP server."""
import os

import pytest

from conftest import assistant, usage, user, write_jsonl


@pytest.fixture(autouse=True)
def _no_cowork_mounts(monkeypatch, tu):
    # Every test in this module ends up calling find_latest_transcript()
    # (directly, via locate_transcript(), or via resolve_transcript()), and
    # that function's Cowork fallback reads whatever this machine's real
    # HOME/mnt/.claude/projects or /sessions/*/mnt/.claude/projects happen to
    # contain. Neutralise that lookup for the whole module, unconditionally,
    # rather than per-test via seed() — a test that never calls seed() (e.g.
    # one exercising resolve_transcript() directly) must not see real Cowork
    # sandbox mounts either.
    monkeypatch.setattr(tu, "_cowork_roots", lambda: [])


def seed(tmp_path, monkeypatch, tu):
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
    # Cowork mount neutralisation is handled module-wide by the autouse
    # _no_cowork_mounts fixture above, so results here depend only on the
    # seeded `proj` tree.
    return proj, a, b


def test_explicit_path_wins_and_must_exist(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    assert tu.locate_transcript(str(a)) == a
    assert tu.locate_transcript(str(tmp_path / "missing.jsonl")) is None


def test_session_id_is_searched_across_projects(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    assert tu.locate_transcript(session_id="aaa-111") == a
    assert tu.locate_transcript(session_id="nope-999") is None
    # Path characters are stripped so an id can never escape the projects dir.
    assert tu.locate_transcript(session_id="../../etc/passwd") is None


def test_project_dir_picks_that_projects_newest(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    # slug("/Users/x/alpha") == "-Users-x-alpha"
    assert tu.locate_transcript(project_dir="/Users/x/alpha") == a
    assert tu.find_latest_transcript(project_dir="/Users/x/beta") == b


def test_explicit_project_dir_with_no_sessions_is_none(tu, tmp_path, monkeypatch):
    # An explicit project_dir that matches no project must never fall back to
    # guessing some *other* project's transcript — that would silently
    # attribute the report to the wrong session.
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    assert tu.locate_transcript(project_dir="/Users/x/nothing-here") is None
    assert tu.find_latest_transcript(project_dir="/Users/x/nothing-here") is None


def test_explicit_project_dir_is_normalised(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    # Trailing separator, "~"-relative and non-canonical forms must all slug
    # to the same project as the canonical absolute path.
    monkeypatch.setenv("HOME", "/Users/x")
    assert tu.locate_transcript(project_dir="/Users/x/alpha/") == a
    assert tu.locate_transcript(project_dir="~/alpha") == a


def test_unrecognised_cwd_falls_back_to_newest_anywhere(tu, tmp_path, monkeypatch):
    # No project_dir at all (the default) means "no project context to
    # anchor on" — e.g. Claude desktop, or a bare CLI invocation — so this
    # is the one case allowed to fall back to the newest transcript anywhere.
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    monkeypatch.chdir(tmp_path)  # cwd slug matches no project either
    assert tu.locate_transcript() == b


def test_env_transcript_overrides_discovery(tu, tmp_path, monkeypatch):
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(a))
    assert tu.locate_transcript(project_dir="/Users/x/beta") == a


def test_env_transcript_missing_file_returns_none(tu, tmp_path, monkeypatch):
    # TOKEN_USAGE_TRANSCRIPT must be validated like every other explicit
    # source — a stale/typo'd path fails closed instead of handing back a
    # Path that blows up downstream with an unhandled FileNotFoundError.
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(tmp_path / "gone.jsonl"))
    assert tu.locate_transcript() is None


def test_resolve_transcript_exits_when_nothing_found(tu, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        tu.resolve_transcript(None)
    assert "no transcript found" in str(e.value)
