import json

from conftest import assistant, usage, user, write_jsonl


def two_transcripts(tmp_path):
    a = write_jsonl(tmp_path / "a.jsonl", [
        user("2026-06-12T10:00:00Z", command="/review"),
        assistant("2026-06-12T10:00:01Z", usage(out=100_000), request_id="r1"),
        user("2026-06-12T11:00:00Z", command="/old-only"),
        assistant("2026-06-12T11:00:01Z", usage(out=1_000), request_id="r2"),
    ])
    b = write_jsonl(tmp_path / "b.jsonl", [
        user("2026-06-12T12:00:00Z", command="/review"),
        assistant("2026-06-12T12:00:01Z", usage(out=40_000), request_id="r3"),
    ])
    return a, b


def test_diff_joins_labels_and_orders_by_abs_delta(tu, tmp_path):
    a, b = two_transcripts(tmp_path)
    d = tu.diff_data(a, b, tu.load_pricing())
    by_label = {r["label"]: r for r in d["rows"]}
    assert by_label["/review"]["delta_output"] == -60_000
    assert by_label["/old-only"]["b_cost"] is None        # missing on the new side
    assert d["rows"][0]["label"] == "/review"             # biggest |Δ cost| first


def test_render_diff_table(tu, tmp_path):
    a, b = two_transcripts(tmp_path)
    out = tu.render_diff(tu.diff_data(a, b, tu.load_pricing()))
    assert "| Activity | A cost | B cost | Δ cost | Δ output |" in out
    assert "—" in out                                     # missing side renders as —
