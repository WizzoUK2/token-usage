# token-usage 0.5.0 "Insights" Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship v0.5.0: an `insights` subcommand (9 rule-based findings), a user pricing overlay with unpriced-model visibility, and honest per-day splitting for `history --by day`.

**Architecture:** Everything lives in the existing single script `scripts/token_usage.py` (house pattern — do not create new modules). Insights are pure functions over the aggregates the script already produces, with a baseline computed from the existing history index. The index schema gains per-day buckets (`INDEX_VERSION` 2→3, one-off re-parse on upgrade).

**Tech Stack:** Python 3.9+ stdlib only. pytest for tests (existing suite: 51 tests in `tests/`).

**Spec:** `docs/superpowers/specs/2026-07-09-token-usage-0.5.0-insights-design.md` — read it first.

## Global Constraints

- Python 3.9+ **stdlib only, zero dependencies** (no new imports beyond stdlib).
- No LLM calls, no network, no telemetry.
- The hook path (`run_hook` → `aggregate` → `load_pricing`) must never raise: a malformed user pricing file gets ONE stderr warning and is skipped.
- Zero findings is a valid `insights` result: print exactly `No notable findings.`
- Baseline-dependent rules skip silently when the project has < 5 prior sessions in the trailing 30 days.
- Every rule's message states an action, not just an observation.
- Test conventions: `tests/conftest.py` exposes a `tu` fixture (the script loaded as a module) and helpers `usage()`, `user()`, `assistant()`, `write_jsonl()`. Import helpers with `from conftest import usage, user, assistant, write_jsonl`. Redirect scanning via env vars `TOKEN_USAGE_PROJECTS_DIR` and `TOKEN_USAGE_LEDGER_DIR` (both read at call time) using `monkeypatch.setenv`.
- Run the full suite with `python3 -m pytest tests/ -q` from the repo root before every commit; all tests must pass.
- Commit after every task (message style: `feat:`/`fix:`/`docs:` prefix, imperative).

---

### Task 1: User pricing overlay (three-layer merge)

**Files:**
- Modify: `scripts/token_usage.py` (replace `load_pricing`, lines 54–61; add `user_pricing_path` above it)
- Test: `tests/test_pricing.py` (create)

**Interfaces:**
- Produces: `user_pricing_path() -> Path` — `$XDG_CONFIG_HOME/token-usage/pricing.json`, default `~/.config/token-usage/pricing.json`. Env read at call time.
- Produces: `load_pricing() -> dict` — per-model-key merge: `DEFAULT_PRICING` ← bundled `data/pricing.json` ← user overlay. Malformed layer or invalid rate entry: one stderr warning, layer/entry skipped, never raises.

- [ ] **Step 0: Isolate the user overlay in ALL tests**

The overlay makes `load_pricing()` read a real file in the developer's home
directory — every existing test that prices usage would silently depend on it.
Add an autouse fixture to `tests/conftest.py` (after the `tu` fixture) so the
whole suite is hermetic; individual tests re-point `XDG_CONFIG_HOME` at their
own tmp dir via `monkeypatch.setenv`, which overrides this:

```python
@pytest.fixture(autouse=True)
def _isolated_pricing_overlay(monkeypatch, tmp_path_factory):
    # Keep the suite hermetic: never read the developer's real user overlay.
    monkeypatch.setenv("XDG_CONFIG_HOME",
                       str(tmp_path_factory.mktemp("xdg-isolated")))
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_pricing.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_pricing.py -v`
Expected: FAIL — `AttributeError: module 'token_usage' has no attribute 'user_pricing_path'` (and merge assertions fail).

- [ ] **Step 3: Implement**

Replace the existing `load_pricing` (currently lines 54–61) with:

```python
def user_pricing_path():
    """User pricing overlay location ($XDG_CONFIG_HOME/token-usage/pricing.json)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "token-usage" / "pricing.json"


def _valid_rates(v):
    return (isinstance(v, dict)
            and isinstance(v.get("input"), (int, float))
            and isinstance(v.get("output"), (int, float)))


def load_pricing():
    """Three-layer per-model-key merge: defaults <- bundled <- user overlay.

    A malformed layer (or a single invalid entry) is warned about once on
    stderr and skipped — never fatal, because the Stop hook calls this."""
    pricing = dict(DEFAULT_PRICING)
    bundled = Path(__file__).resolve().parent.parent / "data" / "pricing.json"
    for layer in (bundled, user_pricing_path()):
        if not layer.exists():
            continue
        try:
            data = json.loads(layer.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"token-usage: ignoring malformed pricing file {layer}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"token-usage: ignoring malformed pricing file {layer}", file=sys.stderr)
            continue
        for key, rates in data.items():
            if _valid_rates(rates):
                pricing[key] = rates
            else:
                print(f"token-usage: ignoring invalid rates for {key} in {layer}",
                      file=sys.stderr)
    return pricing
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (56 tests). The bundled table equals `DEFAULT_PRICING` today, so the replace→merge change is behaviour-neutral for existing tests.

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_pricing.py
git commit -m "feat: user pricing overlay at ~/.config/token-usage/pricing.json (three-layer merge)"
```

---

### Task 2: Unpriced-model detection + footnotes

**Files:**
- Modify: `scripts/token_usage.py` — add `unpriced_models()` helper near `cost_usd()`; extend `aggregate()` total dict; extend `render_report()`; extend `run_history()` + `render_history()`
- Test: `tests/test_pricing.py` (extend)

**Interfaces:**
- Consumes: `user_pricing_path()` from Task 1 (used in the footnote text).
- Produces: `unpriced_models(by_model, pricing) -> list[str]` — sorted model IDs with usage but no resolvable rates.
- Produces: `aggregate()` total gains `"unpriced_models": [...]` (always present, possibly empty). `run_history()` result dict gains top-level `"unpriced_models": [...]`.
- Produces: `render_report()`/`render_history()` append the footnote line
  `N model(s) unpriced (a, b): add rates to <user_pricing_path()>` only when non-empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pricing.py`:

```python
from conftest import usage, user, assistant, write_jsonl


def test_unpriced_models_helper(tu):
    by_model = {"claude-fable-5": tu.empty_usage(), "claude-mystery-9": tu.empty_usage()}
    assert tu.unpriced_models(by_model, tu.DEFAULT_PRICING) == ["claude-mystery-9"]
    assert tu.unpriced_models({"claude-fable-5": tu.empty_usage()}, tu.DEFAULT_PRICING) == []


def test_report_footnote_for_unpriced_model(tu, tmp_path):
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
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=20), request_id="r1"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    assert data["total"]["unpriced_models"] == []
    assert "unpriced" not in tu.render_report(data)


def test_history_collects_unpriced(tu, monkeypatch, tmp_path):
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_pricing.py -v`
Expected: the four new tests FAIL (`no attribute 'unpriced_models'`, `KeyError: 'unpriced_models'`).

- [ ] **Step 3: Implement**

Add after `cache_savings_usd()`:

```python
def unpriced_models(by_model, pricing):
    """Model IDs with recorded usage but no resolvable rates (costs understated)."""
    return sorted(m for m in by_model if rates_for(m, pricing) is None)


def unpriced_footnote(models):
    if not models:
        return None
    return (f"{len(models)} model(s) unpriced ({', '.join(models)}): "
            f"add rates to {user_pricing_path()}")
```

In `aggregate()`, extend the `"total"` dict (after `"by_model": total_by_model,`):

```python
            "unpriced_models": unpriced_models(total_by_model, pricing),
```

In `render_report()`, immediately before the final `Models: …` line:

```python
    note = unpriced_footnote(t.get("unpriced_models") or [])
    if note:
        lines.append(note)
```

In `run_history()`: accumulate `unpriced = set()` in the file loop —

```python
        unpriced.update(unpriced_models(s.get("by_model", {}), pricing))
```

(place it right after the `project` filter, so only rows that survive filtering contribute) — and include `"unpriced_models": sorted(unpriced)` in the returned dict. In `render_history()`, after the burn-rate footer block:

```python
    note = unpriced_footnote(data.get("unpriced_models") or [])
    if note:
        lines += ["", note]
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (60 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_pricing.py
git commit -m "feat: surface unpriced models in report/history/json with overlay remedy"
```

---

### Task 3: Per-day buckets in the transcript summary (index v3)

**Files:**
- Modify: `scripts/token_usage.py` — add `sum_by_day()` near `sum_transcript()`; extend `summarize_transcript()`; bump `INDEX_VERSION` to 3
- Test: `tests/test_history.py` (extend)

**Interfaces:**
- Produces: `sum_by_day(path) -> dict` — `{local_day: by_model}` for ONE jsonl file, deduped by requestId (first-seen timestamp wins per request). Requests with no timestamp fall into the file's first known day, or `"unknown"` if the file has no timestamps at all.
- Produces: `summarize_transcript()` output gains `"by_day": {day: {"usage": <flat+requests>, "cost_usd": float|None}}` covering the main transcript **plus its subagent transcripts**. `INDEX_VERSION == 3`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_history.py`:

```python
def test_sum_by_day_splits_midnight_and_dedups(tu, tmp_path):
    t = write_jsonl(tmp_path / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=100), request_id="r1"),
        # streamed duplicate of r1 — must not double count
        assistant("2026-07-01T10:00:06Z", usage(inp=10, out=100), request_id="r1"),
        # 12:00Z next day: stays a different LOCAL day in any timezone CI runs in
        assistant("2026-07-02T12:00:00Z", usage(inp=10, out=300), request_id="r2"),
    ])
    by_day = tu.sum_by_day(t)
    days = sorted(by_day)
    assert len(days) == 2
    assert tu.sum_buckets(by_day[days[0]])["output"] == 100
    assert tu.sum_buckets(by_day[days[1]])["output"] == 300


def test_sum_by_day_timestampless_requests_fall_to_first_day(tu, tmp_path):
    entries = [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
        assistant(None, usage(out=50), request_id="r2"),
    ]
    entries[2].pop("timestamp")
    t = write_jsonl(tmp_path / "s.jsonl", entries)
    by_day = tu.sum_by_day(t)
    assert len(by_day) == 1
    assert tu.sum_buckets(next(iter(by_day.values())))["output"] == 150


def test_summarize_transcript_has_by_day_and_version_3(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(inp=10, out=100), request_id="r1"),
        assistant("2026-07-02T01:00:00Z", usage(inp=10, out=300), request_id="r2"),
    ])
    s = tu.summarize_transcript(t, tu.load_pricing())
    assert s["version"] == 3
    assert len(s["by_day"]) == 2
    assert sum(d["usage"]["output"] for d in s["by_day"].values()) == 400
    assert all(d["cost_usd"] is not None for d in s["by_day"].values())


def test_summarize_by_day_includes_subagents(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
    ])
    write_jsonl(tmp_path / "projects" / "p" / "s" / "subagents" / "agent-1.jsonl", [
        assistant("2026-07-01T10:01:00Z", usage(out=40), request_id="a1"),
    ])
    s = tu.summarize_transcript(t, tu.load_pricing())
    assert sum(d["usage"]["output"] for d in s["by_day"].values()) == 140


def test_v2_index_entry_reparses_once(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
    ])
    s1, hit1 = tu.cached_summary(t, tu.load_pricing())
    assert not hit1 and s1["version"] == 3
    # forge a stale v2 entry: same mtime/size but version 2 and no by_day
    import hashlib, json as j
    cache_file = tu.index_dir() / (hashlib.sha1(str(t).encode()).hexdigest() + ".json")
    stale = j.loads(cache_file.read_text())
    stale["version"] = 2
    stale.pop("by_day")
    cache_file.write_text(j.dumps(stale))
    s2, hit2 = tu.cached_summary(t, tu.load_pricing())
    assert not hit2 and s2["version"] == 3 and "by_day" in s2   # re-parsed
    s3, hit3 = tu.cached_summary(t, tu.load_pricing())
    assert hit3                                                  # now cached
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_history.py -v`
Expected: new tests FAIL (`no attribute 'sum_by_day'`, `KeyError: 'by_day'`, `assert s["version"] == 3` fails).

- [ ] **Step 3: Implement**

Add after `sum_transcript()`:

```python
def sum_by_day(path):
    """Per-local-day usage for one transcript file, deduped by requestId.

    Returns {local_day: by_model}. A request keeps its first-seen timestamp's
    day; requests with no timestamp fall into the file's first known day
    ('unknown' if the file has no timestamps at all)."""
    first_ts, by_day, pending = None, {}, {}  # pending: req -> (day|None, model, flat)
    for entry in iter_jsonl(path):
        ts = entry.get("timestamp")
        if first_ts is None and ts:
            first_ts = ts
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        flat = normalize_usage(usage)
        req = entry.get("requestId")
        day = _local_day(ts) if ts else None   # None = resolve to first day at the end
        model = msg.get("model") or "unknown"
        if req and req in pending:
            max_flat(pending[req][2], flat)
        elif req:
            pending[req] = (day, model, flat)
        else:
            add_flat(by_day.setdefault(day, {}).setdefault(model, empty_usage()), flat)
    fallback = _local_day(first_ts)
    out = {}
    for day, models in by_day.items():
        merge_by_model(out.setdefault(day or fallback, {}), models)
    for day, model, flat in pending.values():
        add_flat(out.setdefault(day or fallback, {}).setdefault(model, empty_usage()), flat)
    return out
```

Note `_local_day` is defined lower in the file than `sum_by_day`; that's fine — it resolves at call time. In `summarize_transcript()`, before the `return`, build the merged day map:

```python
    day_models = sum_by_day(path)
    subagents_dir = path.parent / path.stem / "subagents"
    if subagents_dir.is_dir():
        for agent_file in sorted(subagents_dir.glob("agent-*.jsonl")):
            for day, models in sum_by_day(agent_file).items():
                merge_by_model(day_models.setdefault(day, {}), models)
```

and add to the returned dict (after `"by_model": …`):

```python
        "by_day": {day: {"usage": sum_buckets(models),
                         "cost_usd": cost_usd(models, pricing)}
                   for day, models in day_models.items()},
```

Change line 498: `INDEX_VERSION = 3`.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (65 tests). If an existing history test asserts `version == 2` or forges v2 cache entries, update it to 3 — check with `grep -n "INDEX_VERSION\|version.*2" tests/test_history.py` first.

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_history.py
git commit -m "feat: per-day usage buckets in transcript summaries (index v3)"
```

---

### Task 4: `history --by day` splits sessions across days

**Files:**
- Modify: `scripts/token_usage.py` — the `elif by == "day":` branch in `run_history()`
- Test: `tests/test_history.py` (extend)

**Interfaces:**
- Consumes: `s["by_day"]` from Task 3.
- Produces: day rows sum per-day buckets; the Calls column counts sessions **touching** each day (a midnight-spanning session counts once in each day it touched).

- [ ] **Step 1: Write the failing test**

```python
def test_history_by_day_splits_sessions_across_days(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
        assistant("2026-07-02T12:00:00Z", usage(out=300), request_id="r2"),
    ])
    data = tu.run_history(by="day")
    rows = {r["key"]: r for r in data["rows"]}
    assert len(rows) == 2
    day1, day2 = sorted(rows)
    assert rows[day1]["usage"]["output"] == 100
    assert rows[day2]["usage"]["output"] == 300
    assert rows[day1]["calls"] == 1 and rows[day2]["calls"] == 1  # touches both days
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_history.py::test_history_by_day_splits_sessions_across_days -v`
Expected: FAIL — today the whole session books to `day1` (`assert rows[day2]…` → KeyError or wrong totals; `len(rows) == 2` fails).

- [ ] **Step 3: Implement**

Replace the day branch in `run_history()`:

```python
        elif by == "day":
            # Sessions split across the local-time days they actually touched;
            # the calls column counts sessions touching that day.
            for day, b in s.get("by_day", {}).items():
                add_row(day, b["usage"], b["cost_usd"], 1)
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (66 tests). Existing `--by day` tests still pass because single-day sessions produce identical rows.

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_history.py
git commit -m "feat: history --by day splits sessions across the days they touched"
```

---

### Task 5: Insights scaffolding — constants, helpers, baseline

**Files:**
- Modify: `scripts/token_usage.py` — new "Insights" section after `render_history()`
- Test: `tests/test_insights.py` (create)

**Interfaces:**
- Produces (all consumed by Tasks 6–8):
  - Constants block: `INSIGHT_MIN_BASELINE_SESSIONS = 5`, `INSIGHT_OUTLIER_WARN = 3.0`, `INSIGHT_OUTLIER_INFO = 2.0`, `INSIGHT_CACHE_DROP_PP = 0.20`, `INSIGHT_ADHOC_SHARE = 0.50`, `INSIGHT_AGENT_SHARE = 0.70`, `INSIGHT_BUDGET_PACE = 0.75`, `INSIGHT_TREND_WARN = 0.50`, `INSIGHT_TREND_INFO = 0.25`, `INSIGHT_MOVER_SHARE = 0.30`
  - `finding(rule, severity, message, **data) -> dict` — `{"rule", "severity", "message", "data"}`
  - `cache_ratio(u) -> float|None` — `cache_read / (cache_read + input + cache_5m + cache_1h)`, None when denominator is 0
  - `_median(xs) -> float|None`
  - `compute_baseline(pricing, project, days=30, exclude=None) -> dict` — `{"sessions": int, "median_session_cost": float|None, "commands": {label: {"sessions": int, "median_cost": float|None, "median_cache_ratio": float|None}}}`. `project` is the exact project directory name; `exclude` is the current transcript's path string (excluded from the baseline).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_insights.py
"""Insights engine: baseline, rules, rendering."""
from conftest import usage, user, assistant, write_jsonl


def _session(tmp_path, name, out_tokens, ts="2026-07-01T10:00:00Z", project="p"):
    return write_jsonl(tmp_path / "projects" / project / f"{name}.jsonl", [
        user(ts, command="/go"),
        assistant(ts, usage(inp=100, out=out_tokens, cache_read=1000), request_id="r1"),
    ])


def test_median(tu):
    assert tu._median([]) is None
    assert tu._median([3.0]) == 3.0
    assert tu._median([1.0, 3.0]) == 2.0
    assert tu._median([1.0, 2.0, 9.0]) == 2.0


def test_cache_ratio(tu):
    u = tu.empty_usage()
    assert tu.cache_ratio(u) is None
    u.update(cache_read=900, input=50, cache_5m=50)
    assert tu.cache_ratio(u) == 0.9


def test_finding_shape(tu):
    f = tu.finding("cost-outlier", "warn", "msg", ratio=2.5)
    assert f == {"rule": "cost-outlier", "severity": "warn",
                 "message": "msg", "data": {"ratio": 2.5}}


def test_compute_baseline(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, out in enumerate([1000, 2000, 3000, 4000, 5000]):
        _session(tmp_path, f"s{i}", out, ts=ts)
    current = _session(tmp_path, "current", 99000, ts=ts)
    b = tu.compute_baseline(tu.load_pricing(), project="p", exclude=str(current))
    assert b["sessions"] == 5                     # excludes the current transcript
    assert b["median_session_cost"] is not None
    assert b["commands"]["/go"]["sessions"] == 5
    assert 0 < b["commands"]["/go"]["median_cache_ratio"] < 1


def test_compute_baseline_scopes_to_project(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _session(tmp_path, "mine", 1000, ts=ts, project="p")
    _session(tmp_path, "other", 1000, ts=ts, project="q")
    b = tu.compute_baseline(tu.load_pricing(), project="p")
    assert b["sessions"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_insights.py -v`
Expected: FAIL — `AttributeError: module 'token_usage' has no attribute '_median'` etc.

- [ ] **Step 3: Implement**

Add after `render_history()` in `scripts/token_usage.py`:

```python
# --- Insights ---------------------------------------------------------------
# Thresholds are maintainer-tunable constants, deliberately not user-config
# (YAGNI until someone asks). Every rule must state an action, not just an
# observation; rules needing a baseline skip silently when it is too thin.
INSIGHT_MIN_BASELINE_SESSIONS = 5   # baseline rules need this many prior sessions
INSIGHT_OUTLIER_WARN = 3.0          # session cost >= 3x project median -> warn
INSIGHT_OUTLIER_INFO = 2.0          # session cost >= 2x project median -> info
INSIGHT_CACHE_DROP_PP = 0.20        # cache-read ratio 20pp below command norm -> warn
INSIGHT_ADHOC_SHARE = 0.50          # (no command) >= 50% of session cost -> info
INSIGHT_AGENT_SHARE = 0.70          # subagents >= 70% of a command's cost -> info
INSIGHT_BUDGET_PACE = 0.75          # >= 75% of TOKEN_USAGE_BUDGET_USD -> info
INSIGHT_TREND_WARN = 0.50           # window spend up >= 50% half-over-half -> warn
INSIGHT_TREND_INFO = 0.25           # window spend +/- 25% half-over-half -> info
INSIGHT_MOVER_SHARE = 0.30          # one label explains >= 30% of the increase -> info


def finding(rule, severity, message, **data):
    return {"rule": rule, "severity": severity, "message": message, "data": data}


def cache_ratio(u):
    """Share of prompt tokens served from cache; None when there were none."""
    denom = u["cache_read"] + u["input"] + u["cache_5m"] + u["cache_1h"]
    return u["cache_read"] / denom if denom else None


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    mid = n // 2
    return xs[mid] if n % 2 else (xs[mid - 1] + xs[mid]) / 2


def compute_baseline(pricing, project, days=30, exclude=None):
    """Per-project norms from the history index (median session cost; per-command
    median cost and cache-read ratio) over the trailing `days`, excluding the
    transcript at `exclude` (the session being analysed)."""
    cutoff = since_cutoff(f"{days}d")
    session_costs, commands = [], {}
    for f in sorted(projects_dir().glob("*/*.jsonl")):
        try:
            s, _ = cached_summary(f, pricing)
        except OSError:
            continue
        if exclude and s["path"] == exclude:
            continue
        if (s["first_ts"] or "") < cutoff or s["project"] != project:
            continue
        if s["total"]["cost_usd"] is not None:
            session_costs.append(s["total"]["cost_usd"])
        for label, agg in s["by_label"].items():
            c = commands.setdefault(label, {"costs": [], "ratios": []})
            if agg["cost_usd"] is not None:
                c["costs"].append(agg["cost_usd"])
            r = cache_ratio(agg["usage"])
            if r is not None:
                c["ratios"].append(r)
    return {
        "sessions": len(session_costs),
        "median_session_cost": _median(session_costs),
        "commands": {label: {"sessions": len(c["costs"]),
                             "median_cost": _median(c["costs"]),
                             "median_cache_ratio": _median(c["ratios"])}
                     for label, c in commands.items()},
    }
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (71 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_insights.py
git commit -m "feat: insights scaffolding — thresholds, finding shape, baseline from history index"
```

---

### Task 6: Session rules 1–3 (cost outlier, cache regression, ad-hoc dominance)

**Files:**
- Modify: `scripts/token_usage.py` — add `session_insights()` after `compute_baseline()`
- Test: `tests/test_insights.py` (extend)

**Interfaces:**
- Consumes: Task 5's constants/helpers; `aggregate()` output shape; `OTHER_LABEL`; `fmt_cost`.
- Produces: `session_insights(data, baseline, budget=None) -> list[finding]`, sorted warn-first then by rule name. Tasks 7 extends this same function; Task 8 calls it from the CLI.

- [ ] **Step 1: Write the failing tests**

```python
def _agg(tu, entries, tmp_path, name="x"):
    t = write_jsonl(tmp_path / f"{name}.jsonl", entries)
    return tu.aggregate(tu.parse_session(t), tu.load_pricing())


BASE = {"sessions": 10, "median_session_cost": 1.0,
        "commands": {"/go": {"sessions": 10, "median_cost": 0.5,
                             "median_cache_ratio": 0.9}}}
THIN = {"sessions": 2, "median_session_cost": 1.0, "commands": {}}


def test_cost_outlier_warn_info_and_quiet(tu, tmp_path):
    big = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                    assistant("2026-07-01T10:00:05Z", usage(out=70000), request_id="r1")],
               tmp_path, "big")        # 70k out on fable-5 = $3.50 = 3.5x median
    rules = {f["rule"]: f for f in tu.session_insights(big, BASE)}
    assert rules["cost-outlier"]["severity"] == "warn"
    assert "3.5×" in rules["cost-outlier"]["message"]
    mid = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                    assistant("2026-07-01T10:00:05Z", usage(out=44000), request_id="r1")],
               tmp_path, "mid")        # $2.20 = 2.2x -> info
    assert {f["rule"]: f for f in tu.session_insights(mid, BASE)}["cost-outlier"]["severity"] == "info"
    small = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                      assistant("2026-07-01T10:00:05Z", usage(out=2000), request_id="r1")],
                 tmp_path, "small")    # $0.10 -> quiet
    assert "cost-outlier" not in {f["rule"] for f in tu.session_insights(small, BASE)}


def test_cost_outlier_skipped_on_thin_baseline(tu, tmp_path):
    big = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                    assistant("2026-07-01T10:00:05Z", usage(out=70000), request_id="r1")],
               tmp_path, "big2")
    assert "cost-outlier" not in {f["rule"] for f in tu.session_insights(big, THIN)}


def test_cache_regression_fires_and_respects_norm(tu, tmp_path):
    # all-input, no cache reads: ratio 0.0 vs norm 0.9 -> warn
    cold = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                     assistant("2026-07-01T10:00:05Z", usage(inp=5000, out=10), request_id="r1")],
                tmp_path, "cold")
    rules = {f["rule"] for f in tu.session_insights(cold, BASE)}
    assert "cache-regression" in rules
    warm = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                     assistant("2026-07-01T10:00:05Z",
                               usage(inp=100, out=10, cache_read=9000), request_id="r1")],
                tmp_path, "warm")      # ratio ~0.99 -> quiet
    assert "cache-regression" not in {f["rule"] for f in tu.session_insights(warm, BASE)}


def test_adhoc_dominance(tu, tmp_path):
    heavy = _agg(tu, [user("2026-07-01T10:00:00Z", "just chatting"),
                      assistant("2026-07-01T10:00:05Z", usage(out=60000), request_id="r1"),
                      user("2026-07-01T11:00:00Z", command="/go"),
                      assistant("2026-07-01T11:00:05Z", usage(out=1000), request_id="r2")],
                 tmp_path, "adhoc")
    f = {f["rule"]: f for f in tu.session_insights(heavy, THIN)}["adhoc-dominance"]
    assert f["severity"] == "info" and "ad-hoc" in f["message"]


def test_findings_sorted_warn_first(tu, tmp_path):
    big = _agg(tu, [user("2026-07-01T10:00:00Z", "chat"),
                    assistant("2026-07-01T10:00:05Z", usage(inp=5000, out=70000), request_id="r1")],
               tmp_path, "sortcheck")
    found = tu.session_insights(big, BASE)
    sevs = [f["severity"] for f in found]
    assert sevs == sorted(sevs, key=lambda s: {"warn": 0, "info": 1}[s])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_insights.py -v`
Expected: new tests FAIL — `no attribute 'session_insights'`.

- [ ] **Step 3: Implement**

Add after `compute_baseline()`:

```python
def session_insights(data, baseline, budget=None):
    """Rule-based findings for one session's aggregate vs its project baseline."""
    out = []
    total_cost = data["total"]["cost_usd"]
    solid = baseline.get("sessions", 0) >= INSIGHT_MIN_BASELINE_SESSIONS

    # 1: cost outlier vs project median
    med = baseline.get("median_session_cost")
    if solid and med and total_cost is not None:
        ratio = total_cost / med
        if ratio >= INSIGHT_OUTLIER_INFO:
            sev = "warn" if ratio >= INSIGHT_OUTLIER_WARN else "info"
            out.append(finding("cost-outlier", sev,
                f"This session ({fmt_cost(total_cost)}) is {ratio:.1f}× your 30-day "
                f"median for this project ({fmt_cost(med)}).",
                session_cost=total_cost, median=med, ratio=round(ratio, 2)))

    # 2: cache hygiene regression per command
    for label, agg in data["by_label"].items():
        norm = baseline.get("commands", {}).get(label) or {}
        if norm.get("sessions", 0) < INSIGHT_MIN_BASELINE_SESSIONS:
            continue
        base_r, cur_r = norm.get("median_cache_ratio"), cache_ratio(agg["usage"])
        if base_r is None or cur_r is None:
            continue
        if base_r - cur_r >= INSIGHT_CACHE_DROP_PP:
            out.append(finding("cache-regression", "warn",
                f"Cache-read ratio for {label} dropped {base_r:.0%} → {cur_r:.0%} — "
                "something is invalidating your prompt cache between turns.",
                label=label, baseline_ratio=round(base_r, 3), ratio=round(cur_r, 3)))

    # 3: ad-hoc dominance
    other = data["by_label"].get(OTHER_LABEL)
    if other and total_cost and other["cost_usd"]:
        share = other["cost_usd"] / total_cost
        if share >= INSIGHT_ADHOC_SHARE:
            out.append(finding("adhoc-dominance", "info",
                f"{share:.0%} of spend was ad-hoc work — wrap repeated workflows "
                "in a command to make them trackable.", share=round(share, 3)))

    order = {"warn": 0, "info": 1}
    out.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    return out
```

The trailing sort stays at the end of the function when Task 7 appends rules 4–6 above it.

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (76 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_insights.py
git commit -m "feat: session insight rules — cost outlier, cache regression, ad-hoc dominance"
```

---

### Task 7: Session rules 4–6 (unpriced, agent fan-out, budget pace)

**Files:**
- Modify: `scripts/token_usage.py` — extend `session_insights()` (insert rules 4–6 between rule 3 and the sort)
- Test: `tests/test_insights.py` (extend)

**Interfaces:**
- Consumes: `data["total"]["unpriced_models"]` (Task 2), `agg["agents"]`/`agg["subagents"]` from `aggregate()`, `user_pricing_path()` (Task 1).
- Consumes (test helpers already defined at module scope in `tests/test_insights.py` by Tasks 5–6): `_session`, `_agg`, `BASE`, `THIN`.
- Produces: rules `unpriced-models` (warn), `agent-fanout` (info), `budget-pace` (info, only when `budget` arg is a positive number and spend is in [75%, 100%) of it).

- [ ] **Step 1: Write the failing tests**

```python
def test_unpriced_models_rule(tu, tmp_path):
    a = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                  assistant("2026-07-01T10:00:05Z", usage(out=100),
                            model="claude-mystery-9", request_id="r1")],
             tmp_path, "unpriced")
    f = {f["rule"]: f for f in tu.session_insights(a, THIN)}["unpriced-models"]
    assert f["severity"] == "warn"
    assert "claude-mystery-9" in f["message"] and "pricing.json" in f["message"]


def test_agent_fanout_rule(tu, tmp_path):
    t = write_jsonl(tmp_path / "fan.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(out=1000), request_id="r1"),
    ])
    write_jsonl(tmp_path / "fan" / "subagents" / "agent-1.jsonl", [
        assistant("2026-07-01T10:01:00Z", usage(out=50000), request_id="a1"),
    ])
    data = tu.aggregate(tu.parse_session(t), tu.load_pricing())
    f = {f["rule"]: f for f in tu.session_insights(data, THIN)}["agent-fanout"]
    assert f["severity"] == "info" and "/go" in f["message"] and "subagent" in f["message"]


def test_budget_pace_rule(tu, tmp_path):
    a = _agg(tu, [user("2026-07-01T10:00:00Z", command="/go"),
                  assistant("2026-07-01T10:00:05Z", usage(out=160000), request_id="r1")],
             tmp_path, "pace")   # $8.00 on fable-5
    f = {f["rule"]: f for f in tu.session_insights(a, THIN, budget=10.0)}["budget-pace"]
    assert "$8.00" in f["message"] and "$10.00" in f["message"]
    # over budget: the hook already nudged — insights stays quiet
    assert "budget-pace" not in {f["rule"] for f in tu.session_insights(a, THIN, budget=5.0)}
    # no budget set
    assert "budget-pace" not in {f["rule"] for f in tu.session_insights(a, THIN)}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_insights.py -v`
Expected: new tests FAIL with `KeyError: 'unpriced-models'` etc.

- [ ] **Step 3: Implement**

Insert between rule 3 and the sort in `session_insights()`:

```python
    # 4: unpriced models (costs understated until the overlay names them)
    up = data["total"].get("unpriced_models") or []
    if up:
        out.append(finding("unpriced-models", "warn",
            f"{', '.join(up)} unpriced — add rates to {user_pricing_path()} "
            "(costs are currently understated).", models=up))

    # 5: agent fan-out concentration
    for label, agg in data["by_label"].items():
        if not agg["subagents"] or not agg["cost_usd"] or not agg["agents"]:
            continue
        agent_cost = sum(g["cost_usd"] or 0.0 for g in agg["agents"])
        share = agent_cost / agg["cost_usd"]
        if share >= INSIGHT_AGENT_SHARE:
            out.append(finding("agent-fanout", "info",
                f"{share:.0%} of {label}'s cost was its {agg['subagents']} "
                f"subagent(s) (top: {agg['agents'][0]['type']}).",
                label=label, share=round(share, 3),
                agents=agg["subagents"], top=agg["agents"][0]["type"]))

    # 6: budget pace (quiet once over budget — the Stop hook owns that nudge)
    if budget and budget > 0 and total_cost is not None:
        pace = total_cost / budget
        if INSIGHT_BUDGET_PACE <= pace < 1.0:
            out.append(finding("budget-pace", "info",
                f"Session at {fmt_cost(total_cost)} of your ${budget:.2f} budget — "
                f"the Stop hook will nudge at ${budget:.2f}.",
                cost=total_cost, budget=budget, share=round(pace, 3)))
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (79 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_insights.py
git commit -m "feat: session insight rules — unpriced models, agent fan-out, budget pace"
```

---

### Task 8: Window rules 7–9

**Files:**
- Modify: `scripts/token_usage.py` — add `window_insights()` after `session_insights()`
- Test: `tests/test_insights.py` (extend)

**Interfaces:**
- Consumes: index summaries (`first_ts`, `total.cost_usd`, `by_label`, `by_model`), `unpriced_models()` (Task 2).
- Produces: `window_insights(summaries, cutoff, pricing, now=None) -> list[finding]` — `cutoff` is the ISO cutoff string (from `since_cutoff`), `now` an optional ISO string for deterministic tests (defaults to current UTC). Splits the window at its midpoint by `first_ts`. Rules: `spend-trend`, `top-mover`, `unpriced-models`.

- [ ] **Step 1: Write the failing tests**

```python
def _summary(ts, cost, label_costs=None, by_model=None):
    return {"first_ts": ts, "total": {"cost_usd": cost, "usage": {}},
            "by_label": {k: {"cost_usd": v, "usage": {}, "invocations": 1}
                         for k, v in (label_costs or {}).items()},
            "by_model": by_model or {}}


CUTOFF, NOW = "2026-07-01T00:00:00Z", "2026-07-11T00:00:00Z"  # midpoint 07-06


def test_spend_trend_warn_and_direction(tu):
    ss = [_summary("2026-07-02T10:00:00Z", 10.0),
          _summary("2026-07-08T10:00:00Z", 20.0)]
    f = {f["rule"]: f for f in tu.window_insights(ss, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert f["spend-trend"]["severity"] == "warn" and "up" in f["spend-trend"]["message"]
    down = [_summary("2026-07-02T10:00:00Z", 20.0),
            _summary("2026-07-08T10:00:00Z", 14.0)]   # -30% -> info
    f = {f["rule"]: f for f in tu.window_insights(down, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert f["spend-trend"]["severity"] == "info" and "down" in f["spend-trend"]["message"]


def test_spend_trend_quiet_when_flat_or_empty_first_half(tu):
    flat = [_summary("2026-07-02T10:00:00Z", 10.0),
            _summary("2026-07-08T10:00:00Z", 11.0)]
    assert "spend-trend" not in {f["rule"] for f in
                                 tu.window_insights(flat, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    empty = [_summary("2026-07-08T10:00:00Z", 11.0)]
    assert tu.window_insights(empty, CUTOFF, tu.DEFAULT_PRICING, now=NOW) == []


def test_top_mover(tu):
    ss = [_summary("2026-07-02T10:00:00Z", 10.0, {"/review": 1.0}),
          _summary("2026-07-08T10:00:00Z", 22.0, {"/review": 11.0})]
    f = {f["rule"]: f for f in tu.window_insights(ss, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert "/review" in f["top-mover"]["message"]


def test_window_unpriced(tu):
    ss = [_summary("2026-07-02T10:00:00Z", 1.0,
                   by_model={"claude-mystery-9": {}})]
    f = {f["rule"]: f for f in tu.window_insights(ss, CUTOFF, tu.DEFAULT_PRICING, now=NOW)}
    assert "claude-mystery-9" in f["unpriced-models"]["message"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_insights.py -v`
Expected: FAIL — `no attribute 'window_insights'`.

- [ ] **Step 3: Implement**

```python
def window_insights(summaries, cutoff, pricing, now=None):
    """Findings for a --since window: trend between the window's halves,
    the top mover behind an increase, and window-wide unpriced models."""
    from datetime import datetime, timezone
    out = []

    def _dt(iso):
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))

    now_dt = _dt(now) if now else datetime.now(timezone.utc)
    mid = (_dt(cutoff) + (now_dt - _dt(cutoff)) / 2).strftime("%Y-%m-%dT%H:%M:%SZ")
    first = [s for s in summaries if (s["first_ts"] or "") < mid]
    second = [s for s in summaries if (s["first_ts"] or "") >= mid]
    c1 = sum(s["total"]["cost_usd"] or 0.0 for s in first)
    c2 = sum(s["total"]["cost_usd"] or 0.0 for s in second)

    # 7: spend trend, half over half
    if c1 > 0:
        change = (c2 - c1) / c1
        if abs(change) >= INSIGHT_TREND_INFO:
            sev = "warn" if change >= INSIGHT_TREND_WARN else "info"
            direction = "up" if change > 0 else "down"
            out.append(finding("spend-trend", sev,
                f"Spend is {direction} {abs(change):.0%} between the two halves of "
                f"this window (${c1:.2f} → ${c2:.2f}).",
                first_half=round(c1, 2), second_half=round(c2, 2),
                change=round(change, 3)))

        # 8: top mover behind an increase
        if c2 > c1:
            def label_costs(ss):
                d = {}
                for s in ss:
                    for label, agg in s["by_label"].items():
                        if agg["cost_usd"]:
                            d[label] = d.get(label, 0.0) + agg["cost_usd"]
                return d
            l1, l2 = label_costs(first), label_costs(second)
            movers = sorted(((l2.get(k, 0.0) - l1.get(k, 0.0), k)
                             for k in set(l1) | set(l2)), reverse=True)
            if movers and movers[0][0] > 0:
                delta, label = movers[0]
                share = delta / (c2 - c1)
                if share >= INSIGHT_MOVER_SHARE:
                    out.append(finding("top-mover", "info",
                        f"{label} explains {share:.0%} of the increase "
                        f"(+${delta:.2f}) — profile it with report --agents/--models.",
                        label=label, delta=round(delta, 2), share=round(share, 3)))

    # 9: unpriced models anywhere in the window
    up = sorted({m for s in summaries
                 for m in unpriced_models(s.get("by_model", {}), pricing)})
    if up:
        out.append(finding("unpriced-models", "warn",
            f"{', '.join(up)} unpriced — add rates to {user_pricing_path()} "
            "(window totals are understated).", models=up))

    order = {"warn": 0, "info": 1}
    out.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    return out
```

- [ ] **Step 4: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (83 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_insights.py
git commit -m "feat: window insight rules — spend trend, top mover, window unpriced"
```

---

### Task 9: `insights` CLI — run, render, `--json`

**Files:**
- Modify: `scripts/token_usage.py` — add `run_insights()` + `render_insights()` after `window_insights()`; wire subparser + dispatch in `main()`
- Test: `tests/test_insights.py` (extend)

**Interfaces:**
- Consumes: everything from Tasks 5–8; `resolve_transcript`, `since_cutoff`, `projects_dir`, `cached_summary`.
- Consumes (test helper already defined in `tests/test_insights.py` by Task 5): `_session`.
- Produces:
  - `run_insights(transcript=None, since=None, project=None, budget=None) -> dict` — `{"mode": "session"|"window", "findings": [...], "baseline": {...}}`. Session mode: aggregates the transcript, baseline scoped to the transcript's parent-dir project excluding itself, budget from arg. Window mode (`since` set): scans cached summaries like `run_history` (with the same substring `--project` filter) and runs `window_insights`; `"baseline"` is `{"sessions": N, "since": since}`.
  - `render_insights(result) -> str` — `No notable findings.` when empty; else one `- [warn|info] <message>` line per finding.
  - CLI: `insights [transcript] [--since X] [--project SUB] [--json]`; passing both a transcript and `--since` exits with an error.

- [ ] **Step 1: Write the failing tests**

```python
import json as jsonlib
import subprocess
import sys


def test_render_insights_empty_and_lines(tu):
    assert tu.render_insights({"findings": []}) == "No notable findings."
    r = {"findings": [tu.finding("x", "warn", "watch out"),
                      tu.finding("y", "info", "fyi")]}
    assert tu.render_insights(r) == "- [warn] watch out\n- [info] fyi"


def test_run_insights_session_mode(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i in range(5):
        _session(tmp_path, f"s{i}", 2000, ts=ts)          # ~$0.10 median
    current = _session(tmp_path, "current", 70000, ts=ts)  # $3.50
    r = tu.run_insights(transcript=current)
    assert r["mode"] == "session"
    assert "cost-outlier" in {f["rule"] for f in r["findings"]}
    assert r["baseline"]["sessions"] == 5


def test_run_insights_window_mode(tu, monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_USAGE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("TOKEN_USAGE_LEDGER_DIR", str(tmp_path / "cache"))
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _session(tmp_path, "old", 20000, ts=old)
    _session(tmp_path, "new", 60000, ts=new)
    r = tu.run_insights(since="10d")
    assert r["mode"] == "window"
    assert "spend-trend" in {f["rule"] for f in r["findings"]}


def test_insights_cli_json(tu, monkeypatch, tmp_path):
    t = write_jsonl(tmp_path / "projects" / "p" / "s.jsonl", [
        user("2026-07-01T10:00:00Z", command="/go"),
        assistant("2026-07-01T10:00:05Z", usage(out=100), request_id="r1"),
    ])
    env = {"TOKEN_USAGE_PROJECTS_DIR": str(tmp_path / "projects"),
           "TOKEN_USAGE_LEDGER_DIR": str(tmp_path / "cache"),
           "PATH": "/usr/bin:/bin"}
    out = subprocess.run([sys.executable, str(tu.__file__ if hasattr(tu, "__file__")
                          else "scripts/token_usage.py"), "insights", str(t), "--json"],
                         capture_output=True, text=True, env=env)
    assert out.returncode == 0
    data = jsonlib.loads(out.stdout)
    assert data["mode"] == "session" and isinstance(data["findings"], list)


def test_insights_cli_rejects_transcript_plus_since(tu, tmp_path):
    import subprocess, sys
    out = subprocess.run([sys.executable, "scripts/token_usage.py", "insights",
                          str(tmp_path / "x.jsonl"), "--since", "7d"],
                         capture_output=True, text=True)
    assert out.returncode != 0 and "--since" in out.stderr
```

(Note: `tu.__file__` on a spec-loaded module is the script path; the fallback string keeps the test honest if run from the repo root, which pytest does.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_insights.py -v`
Expected: FAIL — `no attribute 'render_insights'` / `'run_insights'`; CLI tests exit 2 (unknown subcommand).

- [ ] **Step 3: Implement**

```python
def run_insights(transcript=None, since=None, project=None, budget=None):
    pricing = load_pricing()
    if since:
        cutoff = since_cutoff(since)
        summaries = []
        for f in sorted(projects_dir().glob("*/*.jsonl")):
            try:
                s, _ = cached_summary(f, pricing)
            except OSError:
                continue
            if (s["first_ts"] or "") < cutoff:
                continue
            if project and project not in s["project"]:
                continue
            summaries.append(s)
        return {"mode": "window",
                "findings": window_insights(summaries, cutoff, pricing),
                "baseline": {"sessions": len(summaries), "since": since}}
    t = resolve_transcript(transcript)
    data = aggregate(parse_session(t), pricing)
    baseline = compute_baseline(pricing, project=t.parent.name, exclude=str(t))
    return {"mode": "session",
            "findings": session_insights(data, baseline, budget=budget),
            "baseline": baseline}


def render_insights(result):
    if not result["findings"]:
        return "No notable findings."
    return "\n".join(f"- [{f['severity']}] {f['message']}" for f in result["findings"])
```

In `main()`, add the subparser after the `history` block:

```python
    i = sub.add_parser("insights")
    i.add_argument("transcript", nargs="?", default=None)
    i.add_argument("--since", default=None)
    i.add_argument("--project", default=None)
    i.add_argument("--json", action="store_true", dest="as_json")
```

and the dispatch after the `history` dispatch:

```python
    if args.cmd == "insights":
        if args.transcript and args.since:
            sys.exit("token-usage: pass a transcript OR --since, not both")
        budget = None
        try:
            budget = float(os.environ["TOKEN_USAGE_BUDGET_USD"])
        except (KeyError, ValueError):
            pass
        result = run_insights(transcript=args.transcript, since=args.since,
                              project=args.project, budget=budget)
        print(json.dumps(result, indent=1) if args.as_json else render_insights(result))
        return
```

- [ ] **Step 4: Run the full suite and a live smoke**

Run: `python3 -m pytest tests/ -q` — expected: all pass (88 tests).
Run: `python3 scripts/token_usage.py insights --since 7d` from the repo root — expected: real findings or `No notable findings.`, exit 0. Also run `python3 scripts/token_usage.py insights` inside a real project dir and sanity-check the messages read well.

- [ ] **Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_insights.py
git commit -m "feat: insights subcommand — session and window modes, --json"
```

---

### Task 10: Docs and version bump

**Files:**
- Modify: `README.md`, `skills/report/SKILL.md`, `CHANGELOG.md`, `.claude-plugin/plugin.json`

**Interfaces:** none (documentation).

- [ ] **Step 1: README**

Add feature bullets (after the Burn rate bullet) and an `## Insights` section with a real example:

```markdown
- **Insights** — `insights` runs rule-based checks over the current session
  (cost outlier vs your 30-day project median, prompt-cache regressions,
  ad-hoc-work dominance, agent fan-out concentration, budget pace, unpriced
  models) or a window (`insights --since 30d`: spend trend, top mover). Pure
  arithmetic — no LLM, no network. "No notable findings." is a valid answer.
- **User pricing overlay** — drop rates into `~/.config/token-usage/pricing.json`
  to price new models the bundled table doesn't know yet; reports name any
  unpriced models they encounter.
```

Update the Limitations section: replace the static-pricing caveat with "brand-new models render `—` and are named in a footnote until you add rates to the user overlay". Note the `--by day` behaviour change.

- [ ] **Step 2: SKILL.md**

Extend the frontmatter `description` with insight triggers ("any tips on my token spend", "analyse my token usage", "why was this session expensive"), bump `version: 0.5.0`, document the `insights` command in "How to run", and add a presentation rule: show findings verbatim, add at most 1–2 sentences of interpretation, never invent findings the tool didn't emit.

- [ ] **Step 3: CHANGELOG**

Add a `## [0.5.0] — <release date>` section (Added: insights subcommand with 9 rules; pricing overlay; unpriced-model footnotes. Changed: `history --by day` splits sessions across days — daily figures shift vs 0.4.0; index schema v3, one-off re-parse). Update the link block (`[Unreleased]` compares from v0.5.0; add the 0.5.0 compare link).

- [ ] **Step 4: plugin.json**

Bump `"version"` to `0.5.0`.

- [ ] **Step 5: Verify and commit**

Run: `python3 -m pytest tests/ -q` (docs shouldn't break anything — belt and braces).

```bash
git add README.md skills/report/SKILL.md CHANGELOG.md .claude-plugin/plugin.json
git commit -m "docs: 0.5.0 — insights, pricing overlay, per-day history"
```

---

### Task 11: Release 0.5.0

**Files:** none new (tag + push).

- [ ] **Step 1: Check for parallel-session drift (the 0.3.0 lesson)**

```bash
git fetch origin
git ls-remote --tags origin        # v0.5.0 must NOT already exist
git log --oneline main..origin/main   # must be empty (or merge first)
```

If `v0.5.0` already exists on the remote: STOP, take the next free version number, and update Task 10's version strings before proceeding.

- [ ] **Step 2: Full suite + live smoke**

```bash
python3 -m pytest tests/ -q                      # all pass
python3 scripts/token_usage.py insights --since 7d
python3 scripts/token_usage.py history --by day --since 7d
python3 scripts/token_usage.py report --models
```

- [ ] **Step 3: Tag and push**

```bash
git tag v0.5.0
git push origin main v0.5.0
gh run watch --exit-status        # CI green on 3.9 + 3.12
```

- [ ] **Step 4: Post-release follow-ups (manual, non-blocking)**

- Update the Notion docs page (established workflow — see the 0.4.0 session).
- The plugin-directory entry pins the v0.4.0 SHA; once the plugin is listed, request a SHA bump to the v0.5.0 commit via the directory's update process. Directory installs stay on 0.4.0 until then.
- Update the memory file `token-usage-release-state.md` with the 0.5.0 outcome.
