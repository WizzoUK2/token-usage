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
    monkeypatch.setattr(tu, "_cowork_roots", list)


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
    _proj, a, _b = seed(tmp_path, monkeypatch, tu)
    assert tu.locate_transcript(str(a)) == a
    assert tu.locate_transcript(str(tmp_path / "missing.jsonl")) is None


def test_session_id_is_searched_across_projects(tu, tmp_path, monkeypatch):
    _proj, a, _b = seed(tmp_path, monkeypatch, tu)
    assert tu.locate_transcript(session_id="aaa-111") == a
    assert tu.locate_transcript(session_id="nope-999") is None
    # Path characters are stripped so an id can never escape the projects dir.
    assert tu.locate_transcript(session_id="../../etc/passwd") is None


def test_project_dir_picks_that_projects_newest(tu, tmp_path, monkeypatch):
    _proj, a, b = seed(tmp_path, monkeypatch, tu)
    # slug("/Users/x/alpha") == "-Users-x-alpha"
    assert tu.locate_transcript(project_dir="/Users/x/alpha") == a
    assert tu.find_latest_transcript(project_dir="/Users/x/beta") == b


def test_explicit_project_dir_with_no_sessions_is_none(tu, tmp_path, monkeypatch):
    # An explicit project_dir that matches no project must never fall back to
    # guessing some *other* project's transcript — that would silently
    # attribute the report to the wrong session.
    _proj, _a, _b = seed(tmp_path, monkeypatch, tu)
    assert tu.locate_transcript(project_dir="/Users/x/nothing-here") is None
    assert tu.find_latest_transcript(project_dir="/Users/x/nothing-here") is None


def test_explicit_project_dir_is_normalised(tu, tmp_path, monkeypatch):
    _proj, a, _b = seed(tmp_path, monkeypatch, tu)
    # Trailing separator, "~"-relative and non-canonical forms must all slug
    # to the same project as the canonical absolute path.
    monkeypatch.setenv("HOME", "/Users/x")
    assert tu.locate_transcript(project_dir="/Users/x/alpha/") == a
    assert tu.locate_transcript(project_dir="~/alpha") == a


def test_unrecognised_cwd_falls_back_to_newest_anywhere(tu, tmp_path, monkeypatch):
    # No project_dir at all (the default) means "no project context to
    # anchor on" — e.g. Claude desktop, or a bare CLI invocation — so this
    # is the one case allowed to fall back to the newest transcript anywhere.
    _proj, _a, b = seed(tmp_path, monkeypatch, tu)
    monkeypatch.chdir(tmp_path)  # cwd slug matches no project either
    assert tu.locate_transcript() == b


def test_env_transcript_overrides_discovery(tu, tmp_path, monkeypatch):
    _proj, a, _b = seed(tmp_path, monkeypatch, tu)
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(a))
    assert tu.locate_transcript(project_dir="/Users/x/beta") == a


def test_env_transcript_missing_file_returns_none(tu, tmp_path, monkeypatch):
    # TOKEN_USAGE_TRANSCRIPT must be validated like every other explicit
    # source — a stale/typo'd path fails closed instead of handing back a
    # Path that blows up downstream with an unhandled FileNotFoundError.
    _proj, _a, _b = seed(tmp_path, monkeypatch, tu)
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(tmp_path / "gone.jsonl"))
    assert tu.locate_transcript() is None


def test_resolve_transcript_exits_when_nothing_found(tu, tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "empty"))
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        tu.resolve_transcript(None)
    assert "no transcript found" in str(e.value)


def test_resolve_transcript_names_the_path_the_user_passed(tu, tmp_path, monkeypatch):
    # "pass a path to a session .jsonl file" is wrong advice when a path *was*
    # passed and simply doesn't exist -- name the file instead.
    _proj, _a, _b = seed(tmp_path, monkeypatch, tu)
    missing = tmp_path / "gone.jsonl"
    with pytest.raises(SystemExit) as e:
        tu.resolve_transcript(str(missing))
    assert str(e.value) == f"token-usage: transcript not found: {missing}"


def test_locate_with_source_names_the_rung_that_answered(tu, tmp_path, monkeypatch):
    # Callers that guessed the session (cwd / any-project) have to say so, so
    # resolution reports which rung answered alongside the path.
    proj, a, b = seed(tmp_path, monkeypatch, tu)
    assert tu.locate_transcript_with_source(str(a)) == (a, "explicit")
    assert tu.locate_transcript_with_source(session_id="aaa-111") == (a, "session_id")
    assert tu.locate_transcript_with_source(project_dir="/Users/x/alpha") == (a, "project_dir")

    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(a))
    assert tu.locate_transcript_with_source() == (a, "env")
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT")

    # cwd's own project directory.
    cwd_dir = tmp_path / "work"
    cwd_dir.mkdir()
    c = write_jsonl(proj / tu.project_slug(str(cwd_dir)) / "ccc-333.jsonl", [
        user("2026-06-13T10:00:00Z"),
        assistant("2026-06-13T10:00:01Z", usage(out=30), request_id="r3"),
    ])
    os.utime(c, (1_699_000_000, 1_699_000_000))  # keep b the newest overall
    monkeypatch.chdir(cwd_dir)
    assert tu.locate_transcript_with_source() == (c, "cwd")

    # Cowork sandbox mount.
    monkeypatch.chdir(tmp_path)
    mount = tmp_path / "mnt" / ".claude" / "projects"
    d = write_jsonl(mount / "-Users-x-cowork" / "ddd-444.jsonl", [
        user("2026-06-14T10:00:00Z"),
        assistant("2026-06-14T10:00:01Z", usage(out=40), request_id="r4"),
    ])
    monkeypatch.setattr(tu, "_cowork_roots", lambda: [mount])
    assert tu.locate_transcript_with_source() == (d, "cowork")

    # Newest anywhere.
    monkeypatch.setattr(tu, "_cowork_roots", list)
    assert tu.locate_transcript_with_source() == (b, "any_project")


def test_locate_with_source_reports_the_rung_that_failed_closed(tu, tmp_path, monkeypatch):
    _proj, _a, _b = seed(tmp_path, monkeypatch, tu)
    gone = str(tmp_path / "gone.jsonl")
    assert tu.locate_transcript_with_source(gone) == (None, "explicit")
    assert tu.locate_transcript_with_source(session_id="nope-999") == (None, "session_id")
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", gone)
    assert tu.locate_transcript_with_source() == (None, "env")
    monkeypatch.delenv("TOKEN_USAGE_TRANSCRIPT")
    # An explicit project dir with no sessions, and discovery finding nothing
    # at all, are plain misses — no rung claims them.
    assert tu.locate_transcript_with_source(project_dir="/Users/x/nothing") == (None, None)
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "empty"))
    monkeypatch.chdir(tmp_path)
    assert tu.locate_transcript_with_source() == (None, None)


def test_resolve_transcript_diagnoses_a_broken_env_transcript(tu, tmp_path, monkeypatch):
    # "no transcript found" is the wrong diagnosis when TOKEN_USAGE_TRANSCRIPT
    # points at a file that isn't there — name the variable and its value.
    _proj, _a, _b = seed(tmp_path, monkeypatch, tu)
    gone = tmp_path / "gone.jsonl"
    monkeypatch.setenv("TOKEN_USAGE_TRANSCRIPT", str(gone))
    with pytest.raises(SystemExit) as e:
        tu.resolve_transcript(None)
    assert str(e.value) == (f"token-usage: TOKEN_USAGE_TRANSCRIPT is set to {gone} "
                            "but that file does not exist")
