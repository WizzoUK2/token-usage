"""User pricing overlay: three-layer per-key merge, malformed files non-fatal."""
import json


def test_user_pricing_path_respects_xdg(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert tu.user_pricing_path() == tmp_path / "cfg" / "token-usage" / "pricing.json"


def test_overlay_merges_per_key(tu, monkeypatch, tmp_path):
    p = tmp_path / "cfg" / "token-usage" / "pricing.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "claude-fable-5": {"input": 99.0, "output": 500.0},   # override bundled
        "claude-newmodel-7": {"input": 4.0, "output": 20.0},  # brand new
    }))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    pricing = tu.load_pricing()
    assert pricing["claude-fable-5"] == {"input": 99.0, "output": 500.0}
    assert pricing["claude-newmodel-7"] == {"input": 4.0, "output": 20.0}
    # a bundled key NOT in the overlay survives the merge
    assert pricing["claude-haiku-4-5"] == {"input": 1.0, "output": 5.0}


def test_malformed_overlay_is_skipped_with_warning(tu, monkeypatch, tmp_path, capsys):
    p = tmp_path / "cfg" / "token-usage" / "pricing.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    pricing = tu.load_pricing()
    assert pricing["claude-fable-5"] == {"input": 10.0, "output": 50.0}  # bundled intact
    assert "pricing" in capsys.readouterr().err


def test_invalid_rate_entry_is_skipped(tu, monkeypatch, tmp_path, capsys):
    p = tmp_path / "cfg" / "token-usage" / "pricing.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"claude-fable-5": {"input": "cheap"}}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    pricing = tu.load_pricing()
    assert pricing["claude-fable-5"] == {"input": 10.0, "output": 50.0}
    assert "claude-fable-5" in capsys.readouterr().err


def test_boolean_rate_entry_is_rejected(tu, monkeypatch, tmp_path, capsys):
    p = tmp_path / "cfg" / "token-usage" / "pricing.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"claude-fable-5": {"input": True, "output": 5.0}}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    pricing = tu.load_pricing()
    assert pricing["claude-fable-5"] == {"input": 10.0, "output": 50.0}  # bundled intact
    assert "claude-fable-5" in capsys.readouterr().err


def test_non_utf8_overlay_is_skipped_with_warning(tu, monkeypatch, tmp_path, capsys):
    p = tmp_path / "cfg" / "token-usage" / "pricing.json"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xfe\x00garbage")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    pricing = tu.load_pricing()
    assert pricing["claude-fable-5"] == {"input": 10.0, "output": 50.0}  # bundled intact
    assert "pricing" in capsys.readouterr().err


def test_no_overlay_matches_bundled(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nowhere"))
    assert tu.load_pricing()["claude-sonnet-5"] == {"input": 2.0, "output": 10.0}


def test_overlay_accepts_optional_cache_read_rate(tu, monkeypatch, tmp_path):
    p = tmp_path / "cfg" / "token-usage" / "pricing.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "claude-newmodel-7": {"input": 4.0, "output": 20.0, "cache_read": 0.1},
    }))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    pricing = tu.load_pricing()
    assert pricing["claude-newmodel-7"] == {"input": 4.0, "output": 20.0, "cache_read": 0.1}


def test_overlay_rejects_non_numeric_cache_read(tu, monkeypatch, tmp_path, capsys):
    p = tmp_path / "cfg" / "token-usage" / "pricing.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"claude-fable-5-1": {"input": 10.0, "output": 50.0,
                                                  "cache_read": "cheap"}}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    pricing = tu.load_pricing()
    assert pricing["claude-fable-5-1"] == {"input": 10.0, "output": 50.0, "cache_read": 0.25}
    assert "claude-fable-5-1" in capsys.readouterr().err


def test_unpriced_models_helper(tu):
    by_model = {"claude-fable-5": tu.empty_usage(),
                "claude-mystery-9": dict(tu.empty_usage(), output=100)}
    assert tu.unpriced_models(by_model, tu.DEFAULT_PRICING) == ["claude-mystery-9"]
    assert tu.unpriced_models({"claude-fable-5": tu.empty_usage()}, tu.DEFAULT_PRICING) == []


def test_unpriced_models_skips_zero_usage_pseudo_model(tu):
    by_model = {"claude-fable-5": dict(tu.empty_usage(), output=100),
                "<synthetic>": tu.empty_usage()}
    assert tu.unpriced_models(by_model, tu.DEFAULT_PRICING) == []


def test_report_footnote_for_unpriced_model(tu, tmp_path):
    from conftest import usage, user, assistant, write_jsonl
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=20),
                  model="claude-mystery-9", request_id="r1"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["total"]["unpriced_models"] == ["claude-mystery-9"]
    out = tu.render_report(data)
    assert "unpriced" in out and "claude-mystery-9" in out and "pricing.json" in out


def test_no_footnote_when_all_priced(tu, tmp_path):
    from conftest import usage, user, assistant, write_jsonl
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=20), request_id="r1"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["total"]["unpriced_models"] == []
    assert "unpriced" not in tu.render_report(data)


def test_history_collects_unpriced(tu, monkeypatch, tmp_path):
    from conftest import usage, user, assistant, write_jsonl
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    write_jsonl(tmp_path / "projects" / "proj-a" / "s1.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=20),
                  model="claude-mystery-9", request_id="r1"),
    ])
    data = tu.run_history(by="project")
    assert data["unpriced_models"] == ["claude-mystery-9"]
    assert "unpriced" in tu.render_history(data)
