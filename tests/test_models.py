from conftest import assistant, usage, user, write_jsonl


def make_mixed_model_session(tu, tmp_path):
    return write_jsonl(tmp_path / "sess.jsonl", [
        user("2026-06-12T10:00:00Z", command="/code-review"),
        assistant("2026-06-12T10:00:01Z", usage(out=100),
                  model="claude-fable-5", request_id="r1"),
        assistant("2026-06-12T10:00:02Z", usage(out=50),
                  model="claude-haiku-4-5", request_id="r2"),
    ])


def test_models_grouped_per_label_in_json(tu, tmp_path):
    t = make_mixed_model_session(tu, tmp_path)
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    models = data["by_label"]["/code-review"]["models"]
    # sorted by cost desc: fable (100 out @ $50/M) > haiku (50 out @ $5/M)
    assert [m["model"] for m in models] == ["claude-fable-5", "claude-haiku-4-5"]
    assert models[0]["usage"]["output"] == 100
    assert models[1]["usage"]["output"] == 50
    assert models[0]["cost_usd"] > models[1]["cost_usd"] > 0


def test_model_rows_are_subsets_of_label_total(tu, tmp_path):
    t = make_mixed_model_session(tu, tmp_path)
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    agg = data["by_label"]["/code-review"]
    assert sum(m["usage"]["output"] for m in agg["models"]) == agg["usage"]["output"]


def test_render_report_models_flag(tu, tmp_path):
    t = make_mixed_model_session(tu, tmp_path)
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    plain = tu.render_report(data)
    detailed = tu.render_report(data, show_models=True)
    assert "↳ claude-haiku-4-5" not in plain
    assert "↳ claude-fable-5" in detailed
    assert "↳ claude-haiku-4-5" in detailed
