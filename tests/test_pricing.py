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


def test_no_overlay_matches_bundled(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nowhere"))
    assert tu.load_pricing()["claude-sonnet-5"] == {"input": 3.0, "output": 15.0}
