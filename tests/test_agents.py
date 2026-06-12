from conftest import assistant, usage, user, write_jsonl


def make_session_with_agents(tu, tmp_path):
    t = write_jsonl(tmp_path / "sess.jsonl", [
        user("2026-06-12T10:00:00Z", command="/code-review"),
        assistant("2026-06-12T10:00:01Z", usage(out=100), request_id="r1"),
        user("2026-06-12T10:01:00Z", command="/other"),
        assistant("2026-06-12T10:01:01Z", usage(out=5), request_id="r2"),
    ])
    sub = tmp_path / "sess" / "subagents"
    write_jsonl(sub / "agent-001.jsonl",
                [assistant("2026-06-12T10:00:30Z", usage(out=40), request_id="a1")])
    (sub / "agent-001.meta.json").write_text('{"agentType": "Explore", "description": "scan"}')
    write_jsonl(sub / "agent-002.jsonl",
                [assistant("2026-06-12T10:00:40Z", usage(out=60), request_id="a2")])
    (sub / "agent-002.meta.json").write_text('{"agentType": "Explore", "description": "scan2"}')
    return t


def test_subagents_roll_into_spawning_segment(tu, tmp_path):
    t = make_session_with_agents(tu, tmp_path)
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    agg = data["by_label"]["/code-review"]
    assert agg["usage"]["output"] == 200      # 100 main + 40 + 60 agents
    assert agg["subagents"] == 2
    assert data["by_label"]["/other"]["usage"]["output"] == 5
    assert data["by_label"]["/other"].get("subagents", 0) == 0
