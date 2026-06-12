# token-usage 0.2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship token-usage 0.2.0: pytest suite + CI, sticky multi-turn attribution, agent-type breakdowns, budget nudges, `--diff` compare mode, and a `history` subcommand with an incremental cache.

**Architecture:** Everything stays in the single stdlib-only script `scripts/token_usage.py` (the repo's established pattern). Tests live in `tests/` and import the script via `importlib` (it is not a package); hook behaviour is tested black-box via `subprocess` so the module-level `LEDGER_DIR` constant needs no reloading tricks. Spec: `docs/superpowers/specs/2026-06-12-token-usage-0.2.0-design.md`.

**Tech Stack:** Python 3.9+ stdlib (runtime), pytest (dev-only), GitHub Actions.

**Conventions for every task:** run tests with `python3 -m pytest tests/ -v` from the repo root. Commit messages follow the existing imperative style. Never add a runtime dependency.

---

## File map

| File | Responsibility |
|---|---|
| `scripts/token_usage.py` | All runtime logic (modified in Tasks 2–6) |
| `tests/conftest.py` | Module loader + transcript fixture builders (Task 1) |
| `tests/test_parsing.py` | Dedup, pricing, cost math (Task 1), sticky segmentation (Task 2) |
| `tests/test_agents.py` | Subagent rollup + agent types (Tasks 1, 3) |
| `tests/test_hook.py` | Ledger writing + budget nudges, via subprocess (Tasks 1, 4) |
| `tests/test_diff.py` | Compare mode (Task 5) |
| `tests/test_history.py` | History rollups + cache invalidation (Task 6) |
| `.github/workflows/test.yml` | CI (Task 1) |
| `README.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, `skills/report/SKILL.md` | Docs + release (Task 7) |

---

### Task 1: Test scaffolding, baseline tests, CI

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_parsing.py`
- Create: `tests/test_agents.py`
- Create: `tests/test_hook.py`
- Create: `.github/workflows/test.yml`

- [ ] **Step 1: Write `tests/conftest.py`**

```python
"""Shared fixtures: load the script as a module + synthetic transcript builders."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "token_usage.py"

_spec = importlib.util.spec_from_file_location("token_usage", SCRIPT)
_tu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tu)


@pytest.fixture
def tu():
    return _tu


def usage(inp=0, out=0, cache_read=0, cache_5m=0, cache_1h=0):
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cache_read}
    if cache_5m or cache_1h:
        u["cache_creation"] = {
            "ephemeral_5m_input_tokens": cache_5m,
            "ephemeral_1h_input_tokens": cache_1h,
        }
    return u


def user(ts, text="hello", command=None):
    if command:
        text = f"<command-name>{command}</command-name> <command-message>{command}</command-message>"
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def assistant(ts, u, model="claude-fable-5", request_id=None):
    e = {"type": "assistant", "timestamp": ts,
         "message": {"role": "assistant", "model": model, "usage": u}}
    if request_id:
        e["requestId"] = request_id
    return e


def write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return path
```

- [ ] **Step 2: Write baseline tests in `tests/test_parsing.py`**

```python
import json

from conftest import assistant, usage, user, write_jsonl


def test_dedup_keeps_per_field_maxima(tu, tmp_path):
    # Two streamed snapshots of one request: output grows 10 -> 50.
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z"),
        assistant("2026-06-12T10:00:01Z", usage(inp=100, out=10), request_id="req_1"),
        assistant("2026-06-12T10:00:02Z", usage(inp=100, out=50), request_id="req_1"),
    ])
    by_model, _ = tu.sum_transcript(t)
    b = by_model["claude-fable-5"]
    assert b["output"] == 50          # maxima, not 60 (sum) or 10 (first-seen)
    assert b["input"] == 100
    assert b["requests"] == 1


def test_entries_without_request_id_each_count(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        assistant("2026-06-12T10:00:01Z", usage(out=10)),
        assistant("2026-06-12T10:00:02Z", usage(out=20)),
    ])
    by_model, _ = tu.sum_transcript(t)
    assert by_model["claude-fable-5"]["output"] == 30
    assert by_model["claude-fable-5"]["requests"] == 2


def test_rates_for_provider_prefixed_ids(tu):
    pricing = {"claude-opus-4-8": {"input": 5.0, "output": 25.0}}
    for model in (
        "claude-opus-4-8-20250601",
        "us.anthropic.claude-opus-4-8-20250601-v1:0",
        "anthropic.claude-opus-4-8-v1:0",
        "anthropic/claude-opus-4-8",
    ):
        assert tu.rates_for(model, pricing) == pricing["claude-opus-4-8"], model
    assert tu.rates_for("gpt-4o", pricing) is None


def test_cost_and_cache_savings_math(tu):
    pricing = {"m": {"input": 10.0, "output": 50.0}}
    by_model = {"m": {"input": 1_000_000, "output": 1_000_000,
                      "cache_read": 1_000_000, "cache_5m": 1_000_000,
                      "cache_1h": 1_000_000, "requests": 1}}
    # 10 + 50 + 10*0.1 + 10*1.25 + 10*2.0 = 93.5
    assert tu.cost_usd(by_model, pricing) == 93.5
    # savings: 1MTok read at 0.9 * input rate = 9.0
    assert tu.cache_savings_usd(by_model, pricing) == 9.0


def test_totals_reconcile_with_segments(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z", command="/commit"),
        assistant("2026-06-12T10:00:01Z", usage(out=10), request_id="r1"),
        user("2026-06-12T10:01:00Z", command="/review"),
        assistant("2026-06-12T10:01:01Z", usage(out=20), request_id="r2"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    seg_sum = sum(s["usage"]["output"] for s in data["segments"])
    assert seg_sum == data["total"]["usage"]["output"] == 30
```

- [ ] **Step 3: Write baseline subagent test in `tests/test_agents.py`**

```python
from conftest import assistant, usage, user, write_jsonl


def make_session_with_agents(tu, tmp_path):
    t = write_jsonl(tmp_path / "sess.jsonl", [
        user("2026-06-12T10:00:00Z", command="/code-review"),
        assistant("2026-06-12T10:00:01Z", usage(out=100), request_id="r1"),
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
```

- [ ] **Step 4: Write baseline hook test in `tests/test_hook.py`**

```python
import json
import os
import subprocess
import sys

from conftest import SCRIPT, assistant, usage, user, write_jsonl


def run_hook(payload, tmp_path, extra_env=None):
    env = {**os.environ, "TOKEN_USAGE_LEDGER_DIR": str(tmp_path / "ledger")}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "hook"],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def make_transcript(tmp_path, out_tokens=1000):
    return write_jsonl(tmp_path / "t.jsonl", [
        user("2026-06-12T10:00:00Z", command="/big"),
        assistant("2026-06-12T10:00:01Z", usage(out=out_tokens), request_id="r1"),
    ])


def test_hook_writes_ledger_and_exits_zero(tmp_path):
    t = make_transcript(tmp_path)
    r = run_hook({"session_id": "abc-123", "transcript_path": str(t)}, tmp_path)
    assert r.returncode == 0
    ledger = json.loads((tmp_path / "ledger" / "abc-123.json").read_text())
    assert ledger["total"]["usage"]["output"] == 1000


def test_hook_never_fails_on_garbage(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "hook"], input="not json{",
                       capture_output=True, text=True,
                       env={**os.environ, "TOKEN_USAGE_LEDGER_DIR": str(tmp_path)})
    assert r.returncode == 0
```

- [ ] **Step 5: Run the suite — all baseline tests must pass against v0.1.1**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS (these assert current behaviour; failures mean a fixture bug — fix the fixture, not the script).

- [ ] **Step 6: Create `.github/workflows/test.yml`**

```yaml
name: test
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.9", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install pytest
      - run: python3 -m pytest tests/ -v
```

- [ ] **Step 7: Commit**

```bash
git add tests/ .github/workflows/test.yml
git commit -m "Add pytest suite asserting 0.1.1 behaviour + GitHub Actions CI"
```

---

### Task 2: Sticky multi-turn attribution

**Files:**
- Modify: `scripts/token_usage.py` (`parse_session`, lines ~230-235)
- Test: `tests/test_parsing.py`

- [ ] **Step 1: Write the failing tests (append to `tests/test_parsing.py`)**

```python
def test_command_owns_followup_turns(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z", command="/code-review"),
        assistant("2026-06-12T10:00:01Z", usage(out=100), request_id="r1"),
        user("2026-06-12T10:05:00Z", text="yes, fix that"),          # follow-up
        assistant("2026-06-12T10:05:01Z", usage(out=50), request_id="r2"),
        user("2026-06-12T10:10:00Z", command="/commit"),             # next command
        assistant("2026-06-12T10:10:01Z", usage(out=10), request_id="r3"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["by_label"]["/code-review"]["usage"]["output"] == 150
    assert data["by_label"]["/commit"]["usage"]["output"] == 10
    assert tu.OTHER_LABEL not in data["by_label"]


def test_no_command_only_before_first_command(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-06-12T10:00:00Z", text="hi"),
        assistant("2026-06-12T10:00:01Z", usage(out=5), request_id="r1"),
        user("2026-06-12T10:01:00Z", text="more"),                   # still pre-command
        assistant("2026-06-12T10:01:01Z", usage(out=5), request_id="r2"),
        user("2026-06-12T10:02:00Z", command="/commit"),
        assistant("2026-06-12T10:02:01Z", usage(out=10), request_id="r3"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["by_label"][tu.OTHER_LABEL]["usage"]["output"] == 10
    assert data["by_label"][tu.OTHER_LABEL]["invocations"] == 1      # ONE sticky segment
    assert data["by_label"]["/commit"]["usage"]["output"] == 10
```

- [ ] **Step 2: Run to verify both fail**

Run: `python3 -m pytest tests/test_parsing.py -v -k "followup or before_first"`
Expected: FAIL — `/code-review` shows 100 not 150; `(no command)` has `invocations == 2`.

- [ ] **Step 3: Implement — replace the user-prompt branch in `parse_session`**

Replace:

```python
        if is_user_prompt(entry):
            text = text_of((entry.get("message") or {}).get("content"))
            m = COMMAND_RE.search(text)
            label = m.group(1).strip() if m else OTHER_LABEL
            new_segment(label, entry.get("timestamp"), "" if m else text)
            continue
```

with:

```python
        if is_user_prompt(entry):
            text = text_of((entry.get("message") or {}).get("content"))
            m = COMMAND_RE.search(text)
            if m:
                # A command always starts a new segment...
                new_segment(m.group(1).strip(), entry.get("timestamp"))
            elif not segments:
                # ...but a plain prompt only starts the one pre-command segment;
                # otherwise the active segment keeps ownership (sticky attribution).
                new_segment(OTHER_LABEL, entry.get("timestamp"), text)
            continue
```

Also update the module docstring's segmentation sentence to: `segments the session at slash-command invocations (a command owns all turns until the next command)`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS (Task 1 baseline tests were written sticky-compatible: each prompt there either carries a command or precedes the first command).

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_parsing.py
git commit -m "Sticky attribution: a command owns all turns until the next command"
```

---

### Task 3: Agent-type granularity

**Files:**
- Modify: `scripts/token_usage.py` (subagent rollup in `parse_session`; `aggregate`; `render_report`; `main`)
- Test: `tests/test_agents.py`

- [ ] **Step 1: Write the failing tests (append to `tests/test_agents.py`)**

```python
def test_agents_grouped_by_type_in_json(tu, tmp_path):
    t = make_session_with_agents(tu, tmp_path)
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    agg = data["by_label"]["/code-review"]
    assert agg["agents"] == [
        {"type": "Explore", "count": 2,
         "usage": agg["agents"][0]["usage"], "cost_usd": agg["agents"][0]["cost_usd"]},
    ]
    assert agg["agents"][0]["usage"]["output"] == 100  # 40 + 60


def test_render_report_agents_flag(tu, tmp_path):
    t = make_session_with_agents(tu, tmp_path)
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    plain = tu.render_report(data)
    detailed = tu.render_report(data, show_agents=True)
    assert "↳ Explore ×2" not in plain
    assert "↳ Explore ×2" in detailed
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_agents.py -v`
Expected: FAIL — `KeyError: 'agents'` and `TypeError` on `show_agents`.

- [ ] **Step 3: Implement**

(a) In `parse_session`'s rollup, keep each agent's buckets — change the `seg["subagents"].append({...})` call to:

```python
            seg["subagents"].append({
                "type": meta.get("agentType", "agent"),
                "description": meta.get("description", ""),
                "output_tokens": sum_buckets(a_by_model)["output"],
                "by_model": a_by_model,
            })
```

(b) Add a helper above `aggregate`:

```python
def agents_by_type(subagents, pricing):
    """Group subagent entries by agent type with summed usage and cost."""
    groups = {}
    for a in subagents:
        g = groups.setdefault(a["type"], {"count": 0, "by_model": {}})
        g["count"] += 1
        merge_by_model(g["by_model"], a.get("by_model") or {})
    out = [{"type": t, "count": g["count"],
            "usage": sum_buckets(g["by_model"]),
            "cost_usd": cost_usd(g["by_model"], pricing)}
           for t, g in groups.items()]
    return sorted(out, key=lambda g: -(g["cost_usd"] or g["usage"]["output"] / 1e6))
```

(c) In `aggregate`: collect subagents per label and attach groups. In the segment loop add `agg.setdefault("_subagents", []).extend(seg["subagents"])`; in the finalising loop add:

```python
    for agg in by_label.values():
        agg["usage"] = sum_buckets(agg["by_model"])
        agg["cost_usd"] = cost_usd(agg["by_model"], pricing)
        agg["agents"] = agents_by_type(agg.pop("_subagents", []), pricing)
```

(d) Keep emitted JSON lean — in the `"segments"` list comprehension, strip raw buckets from subagent entries and add per-segment groups:

```python
        "segments": [
            {**{k: s[k] for k in ("label", "start_ts", "prompt")},
             "subagents": [{k: v for k, v in a.items() if k != "by_model"}
                           for a in s["subagents"]],
             "agents": agents_by_type(s["subagents"], pricing),
             "usage": sum_buckets(s["by_model"]),
             "cost_usd": cost_usd(s["by_model"], pricing)}
            for s in segments
        ],
```

(e) `render_report(data)` becomes `render_report(data, show_agents=False)`; after each label row append:

```python
        if show_agents and agg.get("agents"):
            for g in agg["agents"]:
                gu = g["usage"]
                lines.append(
                    f"| ↳ {g['type']} ×{g['count']} | | {fmt_tokens(gu['output'])} | {fmt_tokens(gu['input'])} "
                    f"| {fmt_tokens(gu['cache_read'])} | {fmt_tokens(gu['cache_5m'] + gu['cache_1h'])} "
                    f"| {fmt_cost(g['cost_usd'])} |"
                )
```

(f) In `main()`: give the `report` parser `p.add_argument("--agents", action="store_true")` (add it only when `name == "report"`), and pass through: `print(render_report(data, show_agents=getattr(args, "agents", False)))`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_agents.py
git commit -m "Agent-type granularity: per-type rollups in json, report --agents breakdown rows"
```

---

### Task 4: Budget nudges

**Files:**
- Modify: `scripts/token_usage.py` (`run_hook`)
- Test: `tests/test_hook.py`

- [ ] **Step 1: Write the failing tests (append to `tests/test_hook.py`)**

```python
def test_budget_nudge_fires_once(tmp_path):
    t = make_transcript(tmp_path, out_tokens=1_000_000)  # 1MTok fable output ≈ $50
    payload = {"session_id": "bud-1", "transcript_path": str(t)}
    env = {"TOKEN_USAGE_BUDGET_USD": "10"}
    r1 = run_hook(payload, tmp_path, env)
    assert r1.returncode == 0
    msg = json.loads(r1.stdout)
    assert "passed your $10.00 budget" in msg["systemMessage"]
    assert "/big" in msg["systemMessage"]
    ledger = json.loads((tmp_path / "ledger" / "bud-1.json").read_text())
    assert ledger["budget_notified"] is True
    r2 = run_hook(payload, tmp_path, env)                 # second run: silent
    assert r2.returncode == 0
    assert r2.stdout.strip() == ""


def test_budget_unset_or_invalid_is_inert(tmp_path):
    t = make_transcript(tmp_path, out_tokens=1_000_000)
    for env in ({}, {"TOKEN_USAGE_BUDGET_USD": "not-a-number"}):
        r = run_hook({"session_id": "bud-2", "transcript_path": str(t)}, tmp_path, env)
        assert r.returncode == 0
        assert r.stdout.strip() == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_hook.py -v -k budget`
Expected: FAIL — no stdout, no `budget_notified` key.

- [ ] **Step 3: Implement — inside `run_hook`'s `try:` block, replace the ledger-writing section**

Replace from `LEDGER_DIR.mkdir(...)` through the symlink block with:

```python
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        ledger = LEDGER_DIR / f"{session_id}.json"

        prior_notified = False
        if ledger.exists():
            try:
                prior_notified = bool(json.loads(ledger.read_text()).get("budget_notified"))
            except (json.JSONDecodeError, OSError):
                pass
        limit = None
        try:
            limit = float(os.environ["TOKEN_USAGE_BUDGET_USD"])
        except (KeyError, ValueError):
            pass
        cost = data["total"]["cost_usd"]
        fire = (limit is not None and not prior_notified
                and cost is not None and cost >= limit)
        data["budget_notified"] = prior_notified or fire

        tmp = ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(ledger)
        (LEDGER_DIR / "latest.json").unlink(missing_ok=True)
        try:
            (LEDGER_DIR / "latest.json").symlink_to(ledger)
        except OSError:
            pass
        if fire:
            top = "—"
            if data["by_label"]:
                top = max(data["by_label"].items(),
                          key=lambda kv: kv[1]["cost_usd"] or 0)[0]
            print(json.dumps({"systemMessage":
                f"token-usage: session estimate ${cost:.2f} has passed your "
                f"${limit:.2f} budget — top consumer: {top}"}))
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_hook.py
git commit -m "Budget nudges: one-shot systemMessage when TOKEN_USAGE_BUDGET_USD is crossed"
```

---

### Task 5: Compare mode (`--diff`)

**Files:**
- Modify: `scripts/token_usage.py` (new `diff_data`, `render_diff`; `main`)
- Create: `tests/test_diff.py`

- [ ] **Step 1: Write the failing tests in `tests/test_diff.py`**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_diff.py -v`
Expected: FAIL with `AttributeError: ... 'diff_data'`.

- [ ] **Step 3: Implement — add after `render_report`**

```python
def diff_data(old_path, new_path, pricing):
    """Compare two transcripts label-by-label. Rows ordered by |Δ cost| desc."""
    a = aggregate(parse_session(old_path), pricing)
    b = aggregate(parse_session(new_path), pricing)
    rows = []
    for label in set(a["by_label"]) | set(b["by_label"]):
        ra, rb = a["by_label"].get(label), b["by_label"].get(label)
        ca = ra["cost_usd"] if ra else None
        cb = rb["cost_usd"] if rb else None
        oa = ra["usage"]["output"] if ra else 0
        ob = rb["usage"]["output"] if rb else 0
        rows.append({"label": label, "a_cost": ca, "b_cost": cb,
                     "a_output": oa, "b_output": ob,
                     "delta_cost": (cb or 0.0) - (ca or 0.0),
                     "delta_output": ob - oa})
    rows.sort(key=lambda r: -abs(r["delta_cost"]))
    return {"a_total": a["total"], "b_total": b["total"], "rows": rows}


def render_diff(d):
    lines = ["| Activity | A cost | B cost | Δ cost | Δ output |",
             "|---|---:|---:|---:|---:|"]
    for r in d["rows"]:
        name = r["label"] if r["label"] == OTHER_LABEL else f"`{r['label']}`"
        sign = "+" if r["delta_output"] >= 0 else "-"
        lines.append(f"| {name} | {fmt_cost(r['a_cost'])} | {fmt_cost(r['b_cost'])} "
                     f"| {fmt_cost(r['delta_cost'])} | {sign}{fmt_tokens(abs(r['delta_output']))} |")
    ta, tb = d["a_total"], d["b_total"]
    dt = (tb["cost_usd"] or 0.0) - (ta["cost_usd"] or 0.0)
    do = tb["usage"]["output"] - ta["usage"]["output"]
    sign = "+" if do >= 0 else "-"
    lines.append(f"| **Total** | **{fmt_cost(ta['cost_usd'])}** | **{fmt_cost(tb['cost_usd'])}** "
                 f"| **{fmt_cost(dt)}** | **{sign}{fmt_tokens(abs(do))}** |")
    return "\n".join(lines)
```

(d) In `main()` wire it up — extend the report/json parser loop:

```python
    for name in ("report", "json"):
        p = sub.add_parser(name)
        p.add_argument("transcript", nargs="?", default=None)
        p.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"), default=None)
        if name == "report":
            p.add_argument("--agents", action="store_true")
```

and at dispatch time, before `resolve_transcript`:

```python
    if getattr(args, "diff", None):
        if getattr(args, "agents", False):
            sys.exit("token-usage: --diff and --agents cannot be combined")
        d = diff_data(Path(args.diff[0]), Path(args.diff[1]), load_pricing())
        print(json.dumps(d, indent=1) if args.cmd == "json" else render_diff(d))
        return
```

- [ ] **Step 4: Run the full suite, plus a manual smoke**

Run: `python3 -m pytest tests/ -v` → all PASS.
Run: `python3 scripts/token_usage.py report --diff <any transcript> <any transcript>` → renders a Δ table.

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_diff.py
git commit -m "Compare mode: report/json --diff OLD NEW with per-label cost and output deltas"
```

---

### Task 6: Cross-session history

**Files:**
- Modify: `scripts/token_usage.py` (new `projects_dir`, `index_dir`, `summarize_transcript`, `cached_summary`, `since_cutoff`, `run_history`; `main`)
- Create: `tests/test_history.py`

- [ ] **Step 1: Write the failing tests in `tests/test_history.py`**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_history.py -v`
Expected: FAIL with `AttributeError: ... 'run_history'`.

- [ ] **Step 3: Implement — add after `render_diff`**

```python
INDEX_VERSION = 1


def projects_dir():
    return Path(os.environ.get("TOKEN_USAGE_PROJECTS_DIR",
                               Path.home() / ".claude" / "projects"))


def index_dir():
    return Path(os.environ.get("TOKEN_USAGE_LEDGER_DIR",
                               Path.home() / ".cache" / "token-usage")) / "index"


def summarize_transcript(path, pricing):
    segs = parse_session(path)
    data = aggregate(segs, pricing)
    st = path.stat()
    return {
        "version": INDEX_VERSION, "path": str(path),
        "mtime": st.st_mtime, "size": st.st_size,
        "project": path.parent.name,
        "first_ts": next((s["start_ts"] for s in segs if s["start_ts"]), None),
        "by_label": {label: {"usage": agg["usage"], "cost_usd": agg["cost_usd"],
                             "invocations": agg["invocations"]}
                     for label, agg in data["by_label"].items()},
        "total": {"usage": data["total"]["usage"],
                  "cost_usd": data["total"]["cost_usd"]},
    }


def cached_summary(path, pricing):
    import hashlib
    cache_file = index_dir() / (hashlib.sha1(str(path).encode()).hexdigest() + ".json")
    st = path.stat()
    if cache_file.exists():
        try:
            c = json.loads(cache_file.read_text())
            if (c.get("version") == INDEX_VERSION
                    and c.get("mtime") == st.st_mtime and c.get("size") == st.st_size):
                return c, True
        except (json.JSONDecodeError, OSError):
            pass
    s = summarize_transcript(path, pricing)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(cache_file)
    return s, False


def since_cutoff(arg):
    """'7d' -> ISO instant 7 days ago; ISO strings pass through. None -> None."""
    if not arg:
        return None
    m = re.fullmatch(r"(\d+)d", arg)
    if m:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc)
                - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    return arg


def run_history(by="project", since=None, as_json=False):
    pricing = load_pricing()
    cutoff = since_cutoff(since)
    rows = {}

    def add_row(key, usage_dict, cost, calls):
        r = rows.setdefault(key, {"key": key, "usage": empty_usage(),
                                  "cost_usd": None, "calls": 0})
        for k in r["usage"]:
            r["usage"][k] += usage_dict.get(k, 0)
        if cost is not None:
            r["cost_usd"] = (r["cost_usd"] or 0.0) + cost
        r["calls"] += calls

    files = sorted(projects_dir().glob("*/*.jsonl"))
    for n, f in enumerate(files, 1):
        if n % 25 == 0:
            print(f"token-usage: scanned {n}/{len(files)} transcripts…", file=sys.stderr)
        try:
            s, _ = cached_summary(f, pricing)
        except OSError:
            continue
        if cutoff and (s["first_ts"] or "") < cutoff:
            continue
        if by == "project":
            add_row(s["project"], s["total"]["usage"], s["total"]["cost_usd"], 1)
        elif by == "day":
            add_row((s["first_ts"] or "unknown")[:10],
                    s["total"]["usage"], s["total"]["cost_usd"], 1)
        else:  # command
            for label, agg in s["by_label"].items():
                add_row(label, agg["usage"], agg["cost_usd"], agg["invocations"])

    ordered = sorted(rows.values(), key=lambda r: -(r["cost_usd"] or 0))
    return {"by": by, "since": since, "rows": ordered}


def render_history(data):
    head = {"project": "Project", "day": "Day", "command": "Command"}[data["by"]]
    lines = [f"| {head} | Calls | Output | Input | Cache read | Cache write | Est. cost |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    total = empty_usage()
    total_cost, calls = None, 0
    for r in data["rows"]:
        u = r["usage"]
        lines.append(f"| {r['key']} | {r['calls']} | {fmt_tokens(u['output'])} | {fmt_tokens(u['input'])} "
                     f"| {fmt_tokens(u['cache_read'])} | {fmt_tokens(u['cache_5m'] + u['cache_1h'])} "
                     f"| {fmt_cost(r['cost_usd'])} |")
        for k in total:
            total[k] += u[k]
        if r["cost_usd"] is not None:
            total_cost = (total_cost or 0.0) + r["cost_usd"]
        calls += r["calls"]
    lines.append(f"| **Total** | **{calls}** | **{fmt_tokens(total['output'])}** | **{fmt_tokens(total['input'])}** "
                 f"| **{fmt_tokens(total['cache_read'])}** | **{fmt_tokens(total['cache_5m'] + total['cache_1h'])}** "
                 f"| **{fmt_cost(total_cost)}** |")
    return "\n".join(lines)
```

Note `cached_summary` returns a `(summary, was_cache_hit)` tuple — `run_history` ignores the flag; it exists so tests and future progress reporting can distinguish hits.

(b) In `main()` add the subcommand and dispatch (before the hook dispatch):

```python
    h = sub.add_parser("history")
    h.add_argument("--by", choices=("project", "day", "command"), default="project")
    h.add_argument("--since", default=None)
    h.add_argument("--json", action="store_true", dest="as_json")
```

```python
    if args.cmd == "history":
        data = run_history(by=args.by, since=args.since, as_json=args.as_json)
        print(json.dumps(data, indent=1) if args.as_json else render_history(data))
        return
```

- [ ] **Step 4: Run the full suite + a real-machine smoke**

Run: `python3 -m pytest tests/ -v` → all PASS.
Run: `python3 scripts/token_usage.py history --by command --since 7d` → table renders; second run visibly faster (cache).

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_history.py
git commit -m "history subcommand: cross-session rollups with incremental per-transcript cache"
```

---

### Task 7: Docs, version, release

**Files:**
- Modify: `README.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`, `skills/report/SKILL.md`

- [ ] **Step 1: Update `README.md`**

- Features list: change the attribution bullet to "every slash command owns all turns until the next command"; add bullets for `history`, `--agents`, `--diff`, and `TOKEN_USAGE_BUDGET_USD`.
- Usage section: add the three new invocations with one-line descriptions:
  `report --agents`, `report --diff OLD NEW`, `history [--by project|day|command] [--since 7d] [--json]`, and a "Budget nudges" paragraph (env var, one nudge per session, API-price estimate disclaimer).
- Limitations: delete the two attribution bullets and the "current-session scope only" bullet; keep the no-`requestId` dedup caveat; add "history `--since` compares ISO timestamps lexicographically — mixed-timezone transcripts may be off by hours at the boundary".

- [ ] **Step 2: Update `skills/report/SKILL.md`**

Read the file first; extend its description/instructions so the skill also triggers on "token history", "what did I spend this week", and documents `--agents`, `--diff`, and `history` invocations with the same one-liners as the README.

- [ ] **Step 3: CHANGELOG — add the 0.2.0 entry**

```markdown
## [0.2.0] — <release date>

### Changed

- **Sticky attribution (behaviour change):** a slash command now owns every
  turn until the next command, so per-command numbers grow vs 0.1.x for the
  same transcript. `(no command)` covers only turns before the first command.

### Added

- `history` subcommand — cross-session rollups by project, day, or command,
  with an incremental per-transcript cache (`--since 7d`, `--json`).
- `report --agents` — per-agent-type breakdown rows; `json` output gains
  `agents` arrays per label and segment.
- `report|json --diff OLD NEW` — per-label cost/output deltas between two
  transcripts.
- Budget nudges: set `TOKEN_USAGE_BUDGET_USD` and the Stop hook emits a
  one-time warning when the session's estimated cost crosses it.
- Test suite (pytest) and GitHub Actions CI (Python 3.9 + 3.12).
```

Update the link refs at the bottom (`[Unreleased]` compares from `v0.2.0`, add the `v0.1.1...v0.2.0` compare line).

- [ ] **Step 4: Bump `.claude-plugin/plugin.json` version to `0.2.0`**

- [ ] **Step 5: Full verification**

Run: `python3 -m pytest tests/ -v` → all PASS.
Run: `python3 scripts/token_usage.py report` on a real session → table renders, sticky labels visible.
Run: `echo '{"session_id":"x","transcript_path":"/nonexistent"}' | python3 scripts/token_usage.py hook; echo $?` → `0`.

- [ ] **Step 6: Commit, tag, push**

```bash
git add README.md CHANGELOG.md .claude-plugin/plugin.json skills/report/SKILL.md
git commit -m "Release 0.2.0: sticky attribution, history, budget nudges, agent types, diff, tests+CI"
git tag v0.2.0
git push origin main v0.2.0
```

---

## Self-review notes

- Spec coverage: §1→Task 2, §2→Task 6, §3→Task 4, §4→Task 3, §5→Task 5, §6→Task 1; docs/changelog/release →Task 7. The spec's "active session always re-parses" cache special-case is satisfied implicitly: an active transcript's `(mtime, size)` changes every turn, which already invalidates the cache entry — no extra code needed.
- The spec's open question (agent-type metadata field) is resolved: the rollup already reads `meta.get("agentType", "agent")` from `agent-*.meta.json` (v0.1.1, `parse_session`).
- Task 1 baseline tests are written sticky-compatible on purpose (every fixture prompt either carries a command or precedes the first command), so Task 2 changes no Task 1 test.
