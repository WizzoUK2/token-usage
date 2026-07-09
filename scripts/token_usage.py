#!/usr/bin/env python3
"""token-usage — attribute Claude Code token usage to the work that consumed it.

Parses Claude Code session transcripts (~/.claude/projects/<project>/<session>.jsonl),
deduplicates streamed usage entries by requestId (taking per-field maxima, since
streamed duplicates may carry partial usage snapshots), segments the session at
slash-command invocations — or Skill tool_use blocks in Cowork (the Claude
desktop app) — where each owns all turns until the next, rolls subagent
transcripts up into the segment that spawned them, and prices the result against
a bundled pricing table. Transcript discovery falls back to the Cowork sandbox
mount when no Claude Code project directory matches the cwd.

Subcommands:
    report [TRANSCRIPT]   Markdown breakdown table (default: latest session in cwd project)
    json   [TRANSCRIPT]   Same data as JSON
    hook                  Read Claude Code hook JSON on stdin, update the session ledger

Stdlib only. Python 3.9+.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

COMMAND_RE = re.compile(r"<command-name>([^<]+)</command-name>")
OTHER_LABEL = "(no command)"
LEDGER_DIR = Path(os.environ.get("TOKEN_USAGE_LEDGER_DIR", Path.home() / ".cache" / "token-usage"))

# Per-MTok USD rates. Cache read = 0.1x input; cache write = 1.25x (5m TTL) / 2x (1h TTL).
# Keys are matched by longest prefix against the model ID, so dated IDs resolve too.
DEFAULT_PRICING = {
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-mythos-5": {"input": 10.0, "output": 50.0},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-1": {"input": 15.0, "output": 75.0},
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
CACHE_READ_MULT = 0.1
CACHE_5M_MULT = 1.25
CACHE_1H_MULT = 2.0


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


# Bedrock-style IDs prepend an optional region and "anthropic." (us.anthropic.claude-...).
PROVIDER_PREFIX_RE = re.compile(r"^(?:[a-z]{2,3}\.)?anthropic\.")


def rates_for(model, pricing):
    if not model:
        return None
    candidates = [model]
    if "/" in model:  # OpenRouter/LiteLLM-style "anthropic/claude-..."
        candidates.append(model.rsplit("/", 1)[1])
    candidates += [s for c in list(candidates)
                   if (s := PROVIDER_PREFIX_RE.sub("", c)) != c]
    best = None
    for cand in candidates:
        for key in pricing:
            # A key only matches at a segment boundary, so "claude-opus-4-10"
            # falls through to "claude-opus-4" instead of hitting "claude-opus-4-1".
            if not cand.startswith(key):
                continue
            if len(cand) > len(key) and cand[len(key)].isalnum():
                continue
            if best is None or len(key) > len(best):
                best = key
    return pricing.get(best) if best else None


def empty_usage():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "requests": 0}


def normalize_usage(usage):
    """Flatten an API usage dict to the bucket fields (without the request count)."""
    cc = usage.get("cache_creation") or {}
    five_m = cc.get("ephemeral_5m_input_tokens")
    one_h = cc.get("ephemeral_1h_input_tokens")
    if five_m is None and one_h is None:
        # Older transcripts: only the flat total exists; assume 5m TTL.
        five_m = usage.get("cache_creation_input_tokens") or 0
        one_h = 0
    return {
        "input": usage.get("input_tokens") or 0,
        "output": usage.get("output_tokens") or 0,
        "cache_read": usage.get("cache_read_input_tokens") or 0,
        "cache_5m": five_m or 0,
        "cache_1h": one_h or 0,
    }


def add_flat(bucket, flat):
    for k, v in flat.items():
        bucket[k] += v
    bucket["requests"] += 1


def max_flat(dest, flat):
    # Streamed duplicates of one request may carry partial snapshots; keep the maxima.
    for k, v in flat.items():
        dest[k] = max(dest[k], v)


def cost_usd(by_model, pricing):
    """Estimate USD cost across per-model usage buckets; None if no model is priceable."""
    total, priced = 0.0, False
    for model, bucket in by_model.items():
        rates = rates_for(model, pricing)
        if not rates:
            continue
        priced = True
        inp, out = rates["input"] / 1e6, rates["output"] / 1e6
        total += (
            bucket["input"] * inp
            + bucket["output"] * out
            + bucket["cache_read"] * inp * CACHE_READ_MULT
            + bucket["cache_5m"] * inp * CACHE_5M_MULT
            + bucket["cache_1h"] * inp * CACHE_1H_MULT
        )
    return total if priced else None


def cache_savings_usd(by_model, pricing):
    """USD saved by cache reads being billed at 0.1x instead of the full input rate."""
    total, priced = 0.0, False
    for model, bucket in by_model.items():
        rates = rates_for(model, pricing)
        if not rates:
            continue
        priced = True
        total += bucket["cache_read"] * rates["input"] / 1e6 * (1 - CACHE_READ_MULT)
    return total if priced else None


def unpriced_models(by_model, pricing):
    """Model IDs with recorded usage but no resolvable rates (costs understated)."""
    return sorted(m for m in by_model if rates_for(m, pricing) is None)


def unpriced_footnote(models):
    if not models:
        return None
    return (f"{len(models)} model(s) unpriced ({', '.join(models)}): "
            f"add rates to {user_pricing_path()}")


def merge_by_model(dest, src):
    for model, bucket in src.items():
        d = dest.setdefault(model, empty_usage())
        for k in d:
            d[k] += bucket[k]


def sum_buckets(by_model):
    total = empty_usage()
    for bucket in by_model.values():
        for k in total:
            total[k] += bucket[k]
    return total


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_user_prompt(entry):
    """True if this entry starts a new human turn (not a tool result, meta, or sidechain)."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        if not any(isinstance(b, dict) and b.get("type") == "text" for b in content):
            return False
    elif not isinstance(content, str):
        return False
    return True


def iter_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def sum_transcript(path):
    """Sum usage in one transcript file, deduped by requestId. Returns (by_model, first_ts)."""
    by_model, pending, first_ts = {}, {}, None  # pending: requestId -> (model, flat maxima)
    for entry in iter_jsonl(path):
        if first_ts is None and entry.get("timestamp"):
            first_ts = entry["timestamp"]
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        flat = normalize_usage(usage)
        req = entry.get("requestId")
        if not req:
            add_flat(by_model.setdefault(msg.get("model") or "unknown", empty_usage()), flat)
        elif req in pending:
            max_flat(pending[req][1], flat)
        else:
            pending[req] = (msg.get("model") or "unknown", flat)
    for model, flat in pending.values():
        add_flat(by_model.setdefault(model, empty_usage()), flat)
    return by_model, first_ts


def parse_session(transcript_path):
    transcript_path = Path(transcript_path)
    segments = []  # chronological: {label, start_ts, usage, models, prompt}

    def new_segment(label, ts, prompt=""):
        segments.append({
            "label": label, "start_ts": ts, "by_model": {},
            "prompt": prompt.strip()[:120], "subagents": [],
        })

    pending = {}  # requestId -> (segment, model, flat maxima); segment = first occurrence's
    seen_skill_uses = set()  # Skill tool_use ids already segmented (Cowork)
    for entry in iter_jsonl(transcript_path):
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
            elif segments[-1]["label"] == OTHER_LABEL and not segments[-1]["prompt"]:
                segments[-1]["prompt"] = text.strip()[:120]
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        # Cowork (Claude desktop app): skills are invoked mid-turn via the Skill
        # tool rather than a <command-name> user prompt. Give each skill its own
        # segment (sticky until the next command/skill), deduped by tool-use id
        # so streamed duplicates don't reopen it. Runs before the requestId dedup
        # below because a streamed duplicate may carry the same tool_use block.
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if (isinstance(block, dict) and block.get("type") == "tool_use"
                        and block.get("name") == "Skill"):
                    skill = (block.get("input") or {}).get("skill")
                    use_id = block.get("id") or f"{entry.get('requestId')}:{skill}"
                    if skill and use_id not in seen_skill_uses:
                        seen_skill_uses.add(use_id)
                        new_segment("/" + str(skill), entry.get("timestamp"))
        usage = msg.get("usage")
        if not usage:
            continue
        flat = normalize_usage(usage)
        req = entry.get("requestId")
        if req and req in pending:
            max_flat(pending[req][2], flat)
            continue
        if not segments:
            new_segment(OTHER_LABEL, entry.get("timestamp"))
        seg = segments[-1]
        model = msg.get("model") or "unknown"
        if req:
            pending[req] = (seg, model, flat)
        else:
            add_flat(seg["by_model"].setdefault(model, empty_usage()), flat)
    for seg, model, flat in pending.values():
        add_flat(seg["by_model"].setdefault(model, empty_usage()), flat)

    # Roll up subagent transcripts into the segment active when each agent started.
    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    if subagents_dir.is_dir():
        starts = [(s["start_ts"], i) for i, s in enumerate(segments) if s["start_ts"]]
        starts.sort()
        for agent_file in sorted(subagents_dir.glob("agent-*.jsonl")):
            a_by_model, a_ts = sum_transcript(agent_file)
            if not a_by_model:
                continue
            meta = {}
            meta_path = agent_file.parent / (agent_file.stem + ".meta.json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except (json.JSONDecodeError, OSError):
                    pass
            idx = None
            if a_ts:
                for ts, i in starts:
                    if ts <= a_ts:
                        idx = i
            if idx is None:
                idx = len(segments) - 1 if segments else None
            if idx is None:
                new_segment(OTHER_LABEL, a_ts)
                idx = 0
            seg = segments[idx]
            merge_by_model(seg["by_model"], a_by_model)
            seg["subagents"].append({
                "type": (meta.get("agentType") or "agent"),
                "description": meta.get("description", ""),
                "output_tokens": sum_buckets(a_by_model)["output"],
                "by_model": a_by_model,
            })
    return segments


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


def models_breakdown(by_model, pricing):
    """Per-model rows (subsets of the containing total) with summed usage and cost."""
    out = [{"model": m, "usage": dict(bucket),
            "cost_usd": cost_usd({m: bucket}, pricing)}
           for m, bucket in by_model.items()]
    return sorted(out, key=lambda r: -(r["cost_usd"] or r["usage"]["output"] / 1e6))


def aggregate(segments, pricing):
    by_label, total_by_model = {}, {}
    for seg in segments:
        agg = by_label.setdefault(seg["label"], {
            "by_model": {}, "invocations": 0, "subagents": 0,
        })
        agg["invocations"] += 1
        agg["subagents"] += len(seg["subagents"])
        merge_by_model(agg["by_model"], seg["by_model"])
        merge_by_model(total_by_model, seg["by_model"])
        agg.setdefault("_subagents", []).extend(seg["subagents"])
    for agg in by_label.values():
        agg["usage"] = sum_buckets(agg["by_model"])
        agg["cost_usd"] = cost_usd(agg["by_model"], pricing)
        agg["agents"] = agents_by_type(agg.pop("_subagents", []), pricing)
        agg["models"] = models_breakdown(agg["by_model"], pricing)
    return {
        "by_label": by_label,
        "total": {
            "usage": sum_buckets(total_by_model),
            "cost_usd": cost_usd(total_by_model, pricing),
            "cache_savings_usd": cache_savings_usd(total_by_model, pricing),
            "models": sorted(total_by_model),
            "by_model": total_by_model,
            "unpriced_models": unpriced_models(total_by_model, pricing),
        },
        "segments": [
            {**{k: s[k] for k in ("label", "start_ts", "prompt")},
             "subagents": [{k: v for k, v in a.items() if k != "by_model"}
                           for a in s["subagents"]],
             "agents": agents_by_type(s["subagents"], pricing),
             "usage": sum_buckets(s["by_model"]),
             "cost_usd": cost_usd(s["by_model"], pricing)}
            for s in segments
        ],
    }


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_cost(c):
    return f"${c:.2f}" if c is not None else "—"


def fmt_cost_delta(c):
    if c is None:
        return "—"
    return f"{'-' if c < 0 else '+'}${abs(c):.2f}"


def sub_row(label, u, cost):
    """One ↳ breakdown row (a subset of its parent row) for the report table."""
    return (f"| ↳ {label} | | {fmt_tokens(u['output'])} | {fmt_tokens(u['input'])} "
            f"| {fmt_tokens(u['cache_read'])} | {fmt_tokens(u['cache_5m'] + u['cache_1h'])} "
            f"| {fmt_cost(cost)} |")


def render_report(data, show_agents=False, show_models=False):
    lines = [
        "| Activity | Calls | Output | Input | Cache read | Cache write | Est. cost |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    rows = sorted(data["by_label"].items(),
                  key=lambda kv: -(kv[1]["cost_usd"] or kv[1]["usage"]["output"] / 1e6))
    for label, agg in rows:
        u = agg["usage"]
        if u["requests"] == 0:
            continue
        name = label if label == OTHER_LABEL else f"`{label}`"
        if agg["subagents"]:
            name += f" (+{agg['subagents']} agents)"
        lines.append(
            f"| {name} | {agg['invocations']} | {fmt_tokens(u['output'])} | {fmt_tokens(u['input'])} "
            f"| {fmt_tokens(u['cache_read'])} | {fmt_tokens(u['cache_5m'] + u['cache_1h'])} "
            f"| {fmt_cost(agg['cost_usd'])} |"
        )
        if show_models and agg.get("models"):
            lines += [sub_row(m["model"], m["usage"], m["cost_usd"])
                      for m in agg["models"]]
        if show_agents and agg.get("agents"):
            lines += [sub_row(f"{g['type']} ×{g['count']}", g["usage"], g["cost_usd"])
                      for g in agg["agents"]]
    t = data["total"]
    u = t["usage"]
    lines.append(
        f"| **Total** | | **{fmt_tokens(u['output'])}** | **{fmt_tokens(u['input'])}** "
        f"| **{fmt_tokens(u['cache_read'])}** | **{fmt_tokens(u['cache_5m'] + u['cache_1h'])}** "
        f"| **{fmt_cost(t['cost_usd'])}** |"
    )
    models = ", ".join(sorted(t["models"]))
    lines.append("")
    savings = t.get("cache_savings_usd")
    if savings is not None and savings >= 0.01:
        lines.append(f"Prompt caching saved ~{fmt_cost(savings)} vs. full input rates.")
    note = unpriced_footnote(t.get("unpriced_models") or [])
    if note:
        lines.append(note)
    lines.append(f"Models: {models}. Cost is an API-price estimate (cache-aware); "
                 "subscription plans are not billed per token.")
    return "\n".join(lines)


def diff_data(old_path, new_path, pricing):
    """Compare two transcripts label-by-label. Rows ordered by |Δ cost| desc."""
    a = aggregate(parse_session(old_path), pricing)
    b = aggregate(parse_session(new_path), pricing)
    rows = []
    for label in sorted(set(a["by_label"]) | set(b["by_label"])):
        ra, rb = a["by_label"].get(label), b["by_label"].get(label)
        ca = ra["cost_usd"] if ra else None
        cb = rb["cost_usd"] if rb else None
        oa = ra["usage"]["output"] if ra else 0
        ob = rb["usage"]["output"] if rb else 0
        # Absent on a side genuinely means $0 spent there; present-but-unpriceable
        # means unknown — a delta against unknown is itself unknown.
        unpriceable = (ra is not None and ca is None) or (rb is not None and cb is None)
        delta_cost = None if unpriceable else (cb if rb else 0.0) - (ca if ra else 0.0)
        rows.append({"label": label, "a_cost": ca, "b_cost": cb,
                     "a_output": oa, "b_output": ob,
                     "delta_cost": delta_cost,
                     "delta_output": ob - oa})
    rows.sort(key=lambda r: (-abs(r["delta_cost"] or 0.0), r["label"]))
    return {"a_total": a["total"], "b_total": b["total"], "rows": rows}


def render_diff(d):
    lines = ["| Activity | A cost | B cost | Δ cost | Δ output |",
             "|---|---:|---:|---:|---:|"]
    for r in d["rows"]:
        name = r["label"] if r["label"] == OTHER_LABEL else f"`{r['label']}`"
        sign = "+" if r["delta_output"] >= 0 else "-"
        lines.append(f"| {name} | {fmt_cost(r['a_cost'])} | {fmt_cost(r['b_cost'])} "
                     f"| {fmt_cost_delta(r['delta_cost'])} | {sign}{fmt_tokens(abs(r['delta_output']))} |")
    ta, tb = d["a_total"], d["b_total"]
    dt = None
    if ta["cost_usd"] is not None and tb["cost_usd"] is not None:
        dt = tb["cost_usd"] - ta["cost_usd"]
    do = tb["usage"]["output"] - ta["usage"]["output"]
    sign = "+" if do >= 0 else "-"
    lines.append(f"| **Total** | **{fmt_cost(ta['cost_usd'])}** | **{fmt_cost(tb['cost_usd'])}** "
                 f"| **{fmt_cost_delta(dt)}** | **{sign}{fmt_tokens(abs(do))}** |")
    return "\n".join(lines)


INDEX_VERSION = 2


def projects_dir():
    return Path(os.environ.get("TOKEN_USAGE_PROJECTS_DIR",
                               Path.home() / ".claude" / "projects"))


def index_dir():
    return Path(os.environ.get("TOKEN_USAGE_LEDGER_DIR",
                               Path.home() / ".cache" / "token-usage")) / "index"


def summarize_transcript(path, pricing, st=None):
    # Stat BEFORE parsing: if the transcript is appended mid-parse, the recorded
    # mtime is then stale and the next run re-parses — never a silently stale cache.
    st = st or path.stat()
    segs = parse_session(path)
    data = aggregate(segs, pricing)
    return {
        "version": INDEX_VERSION, "path": str(path),
        "mtime_ns": st.st_mtime_ns, "size": st.st_size,
        "project": path.parent.name,
        "first_ts": next((s["start_ts"] for s in segs if s["start_ts"]), None),
        "by_label": {label: {"usage": agg["usage"], "cost_usd": agg["cost_usd"],
                             "invocations": agg["invocations"]}
                     for label, agg in data["by_label"].items()},
        "by_model": data["total"]["by_model"],
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
                    and c.get("mtime_ns") == st.st_mtime_ns and c.get("size") == st.st_size):
                return c, True
        except (json.JSONDecodeError, OSError):
            pass
    s = summarize_transcript(path, pricing, st)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(cache_file)
    return s, False


def _since_days(arg):
    """Day count of a relative 'Nd' --since value, or None if not that form."""
    m = re.fullmatch(r"(\d+)d", arg or "")
    return int(m.group(1)) if m else None


def since_cutoff(arg):
    """'7d' -> ISO instant 7 days ago; ISO dates pass through; junk errors out."""
    if not arg:
        return None
    days = _since_days(arg)
    if days is not None:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc)
                - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", arg):
        return arg
    # Anything else would string-compare against ISO timestamps and silently
    # filter out every session — reject loudly instead.
    sys.exit(f"token-usage: invalid --since value {arg!r} — use Nd (e.g. 7d) or YYYY-MM-DD")


def _local_day(ts):
    """ISO day of a session's first timestamp in LOCAL time ('unknown' if absent/unparseable)."""
    if not ts:
        return "unknown"
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def run_history(by="project", since=None, project=None):
    pricing = load_pricing()
    cutoff = since_cutoff(since)
    rows = {}
    unpriced = set()

    def add_row(key, usage_dict, cost, calls):
        r = rows.setdefault(key, {"key": key, "usage": empty_usage(),
                                  "cost_usd": None, "calls": 0})
        for k in r["usage"]:
            r["usage"][k] += usage_dict.get(k, 0)
        if cost is not None:
            r["cost_usd"] = (r["cost_usd"] or 0.0) + cost
        r["calls"] += calls

    parsed = 0
    files = sorted(projects_dir().glob("*/*.jsonl"))
    for f in files:
        try:
            s, hit = cached_summary(f, pricing)
        except OSError:
            continue
        if not hit:
            parsed += 1
            if parsed % 25 == 0:
                print(f"token-usage: parsed {parsed} transcripts…", file=sys.stderr)
        if cutoff and (s["first_ts"] or "") < cutoff:
            continue
        if project and project not in s["project"]:
            continue
        unpriced.update(unpriced_models(s.get("by_model", {}), pricing))
        if by == "project":
            add_row(s["project"], s["total"]["usage"], s["total"]["cost_usd"], 1)
        elif by == "day":
            add_row(_local_day(s["first_ts"]), s["total"]["usage"], s["total"]["cost_usd"], 1)
        elif by == "model":
            for model, bucket in s.get("by_model", {}).items():
                add_row(model, bucket, cost_usd({model: bucket}, pricing),
                        bucket.get("requests", 0))
        else:  # command
            for label, agg in s["by_label"].items():
                add_row(label, agg["usage"], agg["cost_usd"], agg["invocations"])

    ordered = sorted(rows.values(), key=lambda r: (-(r["cost_usd"] or 0), r["key"]))
    return {"by": by, "since": since, "project": project, "rows": ordered, "unpriced_models": sorted(unpriced)}


def burn_rate_line(total_cost, since):
    """Projection footer for a relative --since window; None when not applicable."""
    if total_cost is None:
        return None
    days = _since_days(since)
    if not days:
        return None
    per_day = total_cost / days
    return (f"Burn rate: ~${per_day:.2f}/day over the last {days}d "
            f"(≈ ${per_day * 7:.2f}/week at this pace).")


def render_history_csv(data):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    calls_col = "requests" if data["by"] == "model" else "calls"
    w.writerow([data["by"], calls_col, "output", "input",
                "cache_read", "cache_5m", "cache_1h", "cost_usd"])
    for r in data["rows"]:
        u = r["usage"]
        w.writerow([r["key"], r["calls"], u["output"], u["input"],
                    u["cache_read"], u["cache_5m"], u["cache_1h"],
                    "" if r["cost_usd"] is None else f"{r['cost_usd']:.4f}"])
    return buf.getvalue()


def render_history(data):
    head = {"project": "Project", "day": "Day",
            "command": "Command", "model": "Model"}[data["by"]]
    # Per-model counts are API requests, not sessions/invocations.
    calls_head = "Requests" if data["by"] == "model" else "Calls"
    lines = [f"| {head} | {calls_head} | Output | Input | Cache read | Cache write | Est. cost |",
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
    burn = burn_rate_line(total_cost, data.get("since"))
    if burn:
        lines += ["", burn]
    note = unpriced_footnote(data.get("unpriced_models") or [])
    if note:
        lines += ["", note]
    return "\n".join(lines)


def project_slug(path_str):
    # Claude Code slugs project paths by replacing every non-alphanumeric
    # character with a dash (not just / . _).
    return re.sub(r"[^A-Za-z0-9]", "-", path_str)


def find_latest_transcript():
    # 1) Claude Code: ~/.claude/projects/<cwd-slug>/*.jsonl
    project_dir = Path.home() / ".claude" / "projects" / project_slug(str(Path.cwd()))
    if project_dir.is_dir():
        files = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files[0]
    # 2) Cowork (Claude desktop app): the sandbox mounts the live session's
    #    transcript read-only under <mount>/.claude/projects/<slug>/<session>.jsonl.
    #    Only the current session's project is present, so take the newest .jsonl.
    cowork_roots = [Path.home() / "mnt" / ".claude" / "projects"]
    sessions = Path("/sessions")
    if sessions.is_dir():
        cowork_roots.extend(sorted(sessions.glob("*/mnt/.claude/projects")))
    for root in cowork_roots:
        if not root.is_dir():
            continue
        files = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files[0]
    return None


def resolve_transcript(arg):
    if arg:
        return Path(arg)
    env = os.environ.get("TOKEN_USAGE_TRANSCRIPT")
    if env:
        return Path(env)
    latest = find_latest_transcript()
    if latest:
        return latest
    sys.exit("token-usage: no transcript found — pass a path to a session .jsonl file")


def run_hook():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0  # never block Claude Code on a malformed hook payload
    transcript = payload.get("transcript_path")
    session_id = re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("session_id", "unknown"))) or "unknown"
    if not transcript or not Path(transcript).exists():
        return 0
    transcript = Path(transcript)
    if transcript.parent.name == "subagents":
        # SubagentStop delivers the subagent's own sidechain transcript; find
        # the owning session transcript and re-aggregate the whole session.
        main = next((p / f"{session_id}.jsonl" for p in transcript.parents
                     if (p / f"{session_id}.jsonl").is_file()), None)
        if main is None:
            return 0  # never ledger sidechain-only data under the session id
        transcript = main
    is_subagent_stop = payload.get("hook_event_name") == "SubagentStop"
    try:
        data = aggregate(parse_session(transcript), load_pricing())
        data["session_id"] = session_id
        data["transcript_path"] = str(transcript)
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        ledger = LEDGER_DIR / f"{session_id}.json"

        prior_multiple = 0
        if ledger.exists():
            try:
                prior = json.loads(ledger.read_text())
                if isinstance(prior, dict):
                    pm = prior.get("budget_notified_multiple")
                    if isinstance(pm, (int, float)):
                        prior_multiple = int(pm)
                    elif prior.get("budget_notified"):  # 0.2.x ledgers: bool only
                        prior_multiple = 1
            except (json.JSONDecodeError, OSError):
                pass
        limit = None
        try:
            limit = float(os.environ["TOKEN_USAGE_BUDGET_USD"])
        except (KeyError, ValueError):
            pass
        cost = data["total"]["cost_usd"]
        multiple = int(cost // limit) if (limit and limit > 0 and cost is not None) else 0
        # Parallel SubagentStop hooks race the ledger read-modify-write, so
        # only the (serial) Stop hook fires nudges and advances the counter.
        fire = multiple > prior_multiple and not is_subagent_stop
        notified = multiple if fire else prior_multiple
        data["budget_notified"] = notified >= 1
        data["budget_notified_multiple"] = notified

        # Per-process temp names: concurrent SubagentStop hooks must never
        # interleave writes into a shared temp file.
        tmp = ledger.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(ledger)
        link_tmp = LEDGER_DIR / f".latest.{os.getpid()}.tmp"
        try:
            link_tmp.symlink_to(ledger)
            link_tmp.replace(LEDGER_DIR / "latest.json")
        except OSError:
            link_tmp.unlink(missing_ok=True)
        if fire:
            top = "—"
            if data["by_label"]:
                top = max(data["by_label"].items(),
                          key=lambda kv: kv[1]["cost_usd"] or 0)[0]
            passed = (f"{multiple}× your ${limit:.2f} budget" if multiple >= 2
                      else f"your ${limit:.2f} budget")
            print(json.dumps({"systemMessage":
                f"token-usage: session estimate ${cost:.2f} has passed "
                f"{passed} — top consumer: {top}"}))

    except Exception:
        return 0  # a broken ledger update must never break the session
    return 0


def main():
    ap = argparse.ArgumentParser(prog="token-usage")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("report", "json"):
        p = sub.add_parser(name)
        p.add_argument("transcript", nargs="?", default=None)
        if name == "report":
            p.add_argument("--agents", action="store_true")
            p.add_argument("--models", action="store_true")
        p.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"), default=None)
    sub.add_parser("hook")
    h = sub.add_parser("history")
    h.add_argument("--by", choices=("project", "day", "command", "model"), default="project")
    h.add_argument("--since", default=None)
    h.add_argument("--project", default=None)
    h.add_argument("--json", action="store_true", dest="as_json")
    h.add_argument("--csv", action="store_true", dest="as_csv")
    args = ap.parse_args()

    if args.cmd == "hook":
        sys.exit(run_hook())
    if args.cmd == "history":
        if args.as_json and args.as_csv:
            sys.exit("token-usage: --json and --csv cannot be combined")
        data = run_history(by=args.by, since=args.since, project=args.project)
        if args.as_json:
            print(json.dumps(data, indent=1))
        elif args.as_csv:
            print(render_history_csv(data), end="")
        else:
            print(render_history(data))
        return
    if getattr(args, "diff", None):
        if getattr(args, "transcript", None):
            sys.exit("token-usage: --diff ignores TRANSCRIPT — pass exactly two paths to --diff")
        if getattr(args, "agents", False) or getattr(args, "models", False):
            sys.exit("token-usage: --diff cannot be combined with --agents or --models")
        d = diff_data(Path(args.diff[0]), Path(args.diff[1]), load_pricing())
        print(json.dumps(d, indent=1) if args.cmd == "json" else render_diff(d))
        return
    transcript = resolve_transcript(getattr(args, "transcript", None))
    data = aggregate(parse_session(transcript), load_pricing())
    data["transcript_path"] = str(transcript)
    if args.cmd == "json":
        print(json.dumps(data, indent=1))
    else:
        print(render_report(data, show_agents=getattr(args, "agents", False),
                            show_models=getattr(args, "models", False)))


if __name__ == "__main__":
    main()
