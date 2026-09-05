#!/usr/bin/env python3
"""token-usage — attribute Claude Code token usage to the work that consumed it.

Parses Claude Code session transcripts (~/.claude/projects/<project>/<session>.jsonl),
deduplicates streamed usage entries by requestId (taking per-field maxima, since
streamed duplicates may carry partial usage snapshots), segments the session at
slash-command invocations — or Skill tool_use blocks in Cowork (the Claude
desktop app) — where each owns all turns until the next, rolls subagent
transcripts up into the segment that spawned them, and prices the result against
a bundled pricing table. Transcript discovery falls back to the Cowork sandbox
mount when no Claude Code project directory matches the cwd, and then to the
newest transcript under any project on the machine — but only when no project
directory was named: an explicit one never falls through to another project.

Subcommands:
    report [TRANSCRIPT]   Markdown breakdown table (default: latest session in cwd project)
    json   [TRANSCRIPT]   Same data as JSON
    hook                  Read Claude Code hook JSON on stdin, update the session ledger
    history               Cross-session rollup by project/day/command/model
    insights              Rule-based findings for one session or a window
    top_consumers         Costliest sessions or commands in a window
(run with --help for each subcommand's flags)

Stdlib only. Python 3.9+.
"""

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

COMMAND_RE = re.compile(r"<command-name>([^<]+)</command-name>")
OTHER_LABEL = "(no command)"
LEDGER_DIR = Path(os.environ.get("TOKEN_USAGE_LEDGER_DIR", Path.home() / ".cache" / "token-usage"))

# Per-MTok USD rates. Cache read = 0.1x input unless the entry carries an explicit
# "cache_read" rate (Fable/Mythos 5.1 bill hits at $0.25/MTok, i.e. 0.025x);
# cache write = 1.25x (5m TTL) / 2x (1h TTL).
# Keys are matched by longest prefix against the model ID, so dated IDs resolve too.
DEFAULT_PRICING = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-mythos-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-mythos-5": {"input": 10.0, "output": 50.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
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
    "claude-3-5-haiku": {"input": 0.8, "output": 4.0},
}
CACHE_READ_MULT = 0.1
CACHE_5M_MULT = 1.25
CACHE_1H_MULT = 2.0


def user_pricing_path():
    """User pricing overlay location ($XDG_CONFIG_HOME/token-usage/pricing.json)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "token-usage" / "pricing.json"


def _is_rate(x):
    """A usable $/MTok rate: a real, finite, non-negative number.

    NaN/Infinity (json.loads accepts the bare literals, and 1e400 overflows to
    inf) would poison every cost downstream — a `"cost_usd": NaN` is not
    RFC-8259 JSON, and int(cost // limit) raises — and a negative rate silently
    pays the user back. 0 is allowed: free tiers exist."""
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    try:
        return math.isfinite(x) and x >= 0
    except OverflowError:   # an int too large to be a float rate
        return False


def _valid_rates(v):
    """{"input": $/MTok, "output": $/MTok} plus an optional "cache_read" $/MTok."""
    return (isinstance(v, dict)
            and _is_rate(v.get("input")) and _is_rate(v.get("output"))
            and ("cache_read" not in v or _is_rate(v["cache_read"])))


def warn(message, warnings=None):
    """Report a non-fatal problem on stderr and, when a list is given, collect
    it. MCP callers never see stderr, so anything that silently changes the
    numbers has to be able to travel in the result too."""
    print(f"token-usage: {message}", file=sys.stderr)
    if warnings is not None:
        warnings.append(message)


def load_pricing(warnings=None):
    """Three-layer per-model-key merge: defaults <- bundled <- user overlay.

    A malformed layer (or a single invalid entry) is warned about once on
    stderr and skipped — never fatal, because the Stop hook calls this. Pass a
    list as `warnings` to collect the same messages (the MCP server does)."""
    pricing = dict(DEFAULT_PRICING)
    bundled = Path(__file__).resolve().parent.parent / "data" / "pricing.json"
    for layer in (bundled, user_pricing_path()):
        if not layer.exists():
            continue
        try:
            data = json.loads(layer.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError):
            warn(f"ignoring malformed pricing file {layer}", warnings)
            continue
        if not isinstance(data, dict):
            warn(f"ignoring malformed pricing file {layer}", warnings)
            continue
        for key, rates in data.items():
            if _valid_rates(rates):
                pricing[key] = rates
            else:
                warn(f"ignoring invalid rates for {key} in {layer}", warnings)
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
            + bucket["cache_read"] * cache_read_rate(rates) / 1e6
            + bucket["cache_5m"] * inp * CACHE_5M_MULT
            + bucket["cache_1h"] * inp * CACHE_1H_MULT
        )
    return total if priced else None


def cache_read_rate(rates):
    """$/MTok for cache hits: the entry's own rate if given, else 0.1x input."""
    return rates.get("cache_read", rates["input"] * CACHE_READ_MULT)


def cache_savings_usd(by_model, pricing):
    """USD saved by cache reads being billed at the cache-hit rate instead of full input."""
    total, priced = 0.0, False
    for model, bucket in by_model.items():
        rates = rates_for(model, pricing)
        if not rates:
            continue
        priced = True
        total += bucket["cache_read"] * (rates["input"] - cache_read_rate(rates)) / 1e6
    return total if priced else None


def unpriced_models(by_model, pricing):
    """Model IDs with recorded usage but no resolvable rates (costs understated)."""
    return sorted(m for m, b in by_model.items()
                  if rates_for(m, pricing) is None
                  and any(b[k] for k in ("input", "output", "cache_read", "cache_5m", "cache_1h")))


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
    # errors="replace": a transcript with a few undecodable bytes must still
    # parse (the affected line simply fails json.loads and is skipped) rather
    # than turning every reader — report, the hook, the corpus scan — into a
    # UnicodeDecodeError traceback.
    with open(path, encoding="utf-8", errors="replace") as fh:
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
                    meta = json.loads(meta_path.read_text(encoding="utf-8",
                                                          errors="replace"))
                except (ValueError, OSError):   # ValueError: malformed JSON
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
    transcript_path = data.get("transcript_path")
    if transcript_path:
        # Resolution can fall back to a transcript in a project other than
        # the caller's (see find_latest_transcript) — always name which one
        # was actually measured, rather than leaving that silent.
        tp = Path(transcript_path)
        lines.append(f"Session: `{tp.parent.name}/{tp.name}`")
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


INDEX_VERSION = 4


def projects_dir():
    return Path(os.environ.get("TOKEN_USAGE_PROJECTS_DIR",
                               Path.home() / ".claude" / "projects"))


def check_projects_root(warnings=None):
    """The projects root as a string when it is NOT a readable directory
    (missing, a regular file, a stale mount), else None.

    Path.glob() on a non-existent path or a file yields nothing rather than
    raising, so a mistyped TOKEN_USAGE_PROJECTS_DIR, an MCP server started
    with a different HOME or an unmounted sandbox made the whole corpus scan
    report a clean, successful, empty result. "There is nothing here to read"
    is not the same answer as "you spent nothing"."""
    root = projects_dir()
    if root.is_dir():
        return None
    warn(f"no Claude Code projects directory at {root}", warnings)
    return str(root)


def projects_dir_footnote(path):
    """Markdown footnote for a corpus scan that had no projects root to read."""
    if not path:
        return None
    return f"No Claude Code projects directory at {path} — nothing was scanned."


def index_dir():
    return Path(os.environ.get("TOKEN_USAGE_LEDGER_DIR",
                               Path.home() / ".cache" / "token-usage")) / "index"


def pricing_fingerprint(pricing):
    """Stable hash of a pricing table; cached summaries bake costs in, so a rate
    change (bundled update or user overlay edit) must invalidate them."""
    import hashlib
    return hashlib.sha1(json.dumps(pricing, sort_keys=True).encode()).hexdigest()


def summarize_transcript(path, pricing, st=None):
    # Stat BEFORE parsing: if the transcript is appended mid-parse, the recorded
    # mtime is then stale and the next run re-parses — never a silently stale cache.
    st = st or path.stat()
    segs = parse_session(path)
    data = aggregate(segs, pricing)
    day_models = sum_by_day(path)
    subagents_dir = path.parent / path.stem / "subagents"
    if subagents_dir.is_dir():
        for agent_file in sorted(subagents_dir.glob("agent-*.jsonl")):
            for day, models in sum_by_day(agent_file).items():
                merge_by_model(day_models.setdefault(day, {}), models)
    return {
        "version": INDEX_VERSION, "path": str(path),
        "mtime_ns": st.st_mtime_ns, "size": st.st_size,
        "pricing": pricing_fingerprint(pricing),
        "project": path.parent.name,
        "first_ts": next((s["start_ts"] for s in segs if s["start_ts"]), None),
        # "unpriced": this label used at least one model with no rates, so
        # its cost_usd covers only part of its usage (v4 of the index; a
        # per-session cost is not enough to spot mixing inside one label).
        "by_label": {label: {"usage": agg["usage"], "cost_usd": agg["cost_usd"],
                             "invocations": agg["invocations"],
                             "unpriced": bool(unpriced_models(agg["by_model"], pricing))}
                     for label, agg in data["by_label"].items()},
        "by_model": data["total"]["by_model"],
        "total": {"usage": data["total"]["usage"],
                  "cost_usd": data["total"]["cost_usd"]},
        "by_day": {day: {"usage": sum_buckets(models),
                         "cost_usd": cost_usd(models, pricing)}
                   for day, models in day_models.items()},
    }


_CACHE_WRITE_WARNED = False


def cached_summary(path, pricing, warnings=None):
    import hashlib
    global _CACHE_WRITE_WARNED
    cache_file = index_dir() / (hashlib.sha1(str(path).encode()).hexdigest() + ".json")
    st = path.stat()
    if cache_file.exists():
        try:
            c = json.loads(cache_file.read_text(encoding="utf-8", errors="replace"))
            # Valid JSON that isn't an object would blow up on .get() — a
            # corrupt entry must re-parse, never crash the whole scan.
            if (isinstance(c, dict) and c.get("version") == INDEX_VERSION
                    and c.get("mtime_ns") == st.st_mtime_ns and c.get("size") == st.st_size
                    and c.get("pricing") == pricing_fingerprint(pricing)):
                return c, True
        except (ValueError, OSError):
            pass
    s = summarize_transcript(path, pricing, st)
    # An unwritable cache directory costs speed, not correctness: warn once
    # per process and hand back the freshly parsed summary anyway.
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(s), encoding="utf-8")
        tmp.replace(cache_file)
    except OSError as e:
        # Once per process on stderr, but every caller's `warnings` list gets
        # it: an MCP caller never sees stderr, and a long-lived server would
        # otherwise re-parse the whole corpus on every query in silence.
        message = f"cannot write summary cache {index_dir()}: {e}"
        if not _CACHE_WRITE_WARNED:
            _CACHE_WRITE_WARNED = True
            warn(message, warnings)
        elif warnings is not None and message not in warnings:
            warnings.append(message)
    return s, False


def _since_days(arg):
    """Day count of a relative 'Nd' --since value, or None if not that form."""
    m = re.fullmatch(r"(\d+)d", arg or "")
    return int(m.group(1)) if m else None


def since_cutoff(arg, flag="--since"):
    """'7d' -> ISO instant 7 days ago; ISO dates pass through; junk errors out.

    `flag` names the offending input in the error: callers that aren't the CLI
    (the MCP server) must not tell their users about a "--since" flag they
    never typed."""
    if not arg:
        return None
    days = _since_days(arg)
    if days is not None:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc)
                - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", arg) and _is_calendar_date(arg):
        return arg
    # Anything else would string-compare against ISO timestamps and silently
    # filter out every session — reject loudly instead.
    sys.exit(f"token-usage: invalid {flag} value {arg!r} — use Nd (e.g. 7d) or YYYY-MM-DD")


def _is_calendar_date(arg):
    """True when `arg` is a real instant, not just the right shape.

    The shape check alone let 31 September through, and window_halves' call to
    datetime.fromisoformat then turned an insights window into a ValueError
    traceback while `history` took the same value and silently matched
    nothing."""
    from datetime import datetime
    try:
        datetime.strptime(arg[:10], "%Y-%m-%d")  # noqa: DTZ007 — a date, not an instant
        if len(arg) > 10:
            datetime.fromisoformat(arg.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _local_day(ts):
    """ISO day of a session's first timestamp in LOCAL time ('unknown' if absent/unparseable)."""
    if not ts:
        return "unknown"
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return ts[:10]


def iter_summaries(pricing, cutoff=None, project=None, exclude=None, progress=False,
                   skipped=None, warnings=None):
    """Yield cached_summary() results for every transcript under projects_dir(),
    applying the filters shared by every caller that scans the whole corpus:
    an optional `cutoff` (skip sessions starting before it), an optional
    `exclude` path (skip that one transcript — the session being analysed),
    and an optional `project` substring filter (`project in s["project"]`).

    compute_baseline needs *exact* project equality instead, so it deliberately
    does not pass `project` here and filters the yielded summaries itself —
    don't fold that exact-match rule into this generator.

    `progress` prints run_history's stderr counter for freshly-parsed (not
    cache-hit) transcripts; other callers leave it off.

    A transcript that cannot be read or parsed is skipped with a stderr
    warning and its path appended to `skipped` when a list is given — an
    empty table is otherwise indistinguishable from "you spent nothing".
    `warnings` travels to cached_summary so an unwritable cache dir reaches
    MCP callers too; callers disclose a missing projects root themselves (see
    check_projects_root), since it belongs in the result, not just the log.
    """
    parsed = 0
    for f in sorted(projects_dir().glob("*/*.jsonl")):
        try:
            s, hit = cached_summary(f, pricing, warnings)
        except (OSError, ValueError, AttributeError) as e:
            # OSError: unreadable/a directory. ValueError: UnicodeDecodeError
            # and malformed JSON. AttributeError: a cache entry of the wrong shape.
            warn(f"skipping unreadable transcript {f}: {e}")
            if skipped is not None:
                skipped.append(str(f))
            continue
        if progress and not hit:
            parsed += 1
            if parsed % 25 == 0:
                print(f"token-usage: parsed {parsed} transcripts…", file=sys.stderr)
        if exclude and s["path"] == exclude:
            continue
        if cutoff and (s["first_ts"] or "") < cutoff:
            continue
        if project and project not in s["project"]:
            continue
        yield s


def run_history(by="project", since=None, project=None, warnings=None):
    pricing = load_pricing(warnings)
    cutoff = since_cutoff(since)
    missing_root = check_projects_root(warnings)
    rows = {}
    unpriced, skipped = set(), []

    def add_row(key, usage_dict, cost, calls):
        r = rows.setdefault(key, {"key": key, "usage": empty_usage(),
                                  "cost_usd": None, "calls": 0})
        for k in r["usage"]:
            r["usage"][k] += usage_dict.get(k, 0)
        if cost is not None:
            r["cost_usd"] = (r["cost_usd"] or 0.0) + cost
        r["calls"] += calls

    for s in iter_summaries(pricing, cutoff=cutoff, project=project, progress=True,
                            skipped=skipped, warnings=warnings):
        unpriced.update(unpriced_models(s.get("by_model", {}), pricing))
        if by == "project":
            add_row(s["project"], s["total"]["usage"], s["total"]["cost_usd"], 1)
        elif by == "day":
            # Sessions split across the local-time days they actually touched;
            # the calls column counts sessions touching that day.
            for day, b in s.get("by_day", {}).items():
                add_row(day, b["usage"], b["cost_usd"], 1)
        elif by == "model":
            for model, bucket in s.get("by_model", {}).items():
                add_row(model, bucket, cost_usd({model: bucket}, pricing),
                        bucket.get("requests", 0))
        else:  # command
            for label, agg in s["by_label"].items():
                add_row(label, agg["usage"], agg["cost_usd"], agg["invocations"])

    ordered = sorted(rows.values(), key=lambda r: (-(r["cost_usd"] or 0), r["key"]))
    return {"by": by, "since": since, "project": project, "rows": ordered,
            "unpriced_models": sorted(unpriced), "skipped_transcripts": skipped,
            "projects_dir_missing": missing_root}


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


def skipped_footnote(paths):
    """Footnote naming transcripts the corpus scan could not read; None when none were."""
    if not paths:
        return None
    return f"{len(paths)} transcript(s) skipped (unreadable): {', '.join(paths)}"


def _usage_cells(u, cost, partial=False):
    """The output/input/cache-read/cache-write/cost cells shared by every
    history and top-consumers row (leading pipe included).

    `partial` marks a cost that is only a priced subtotal (some of the row's
    usage ran on an unpriced model) with a trailing `*`, so the reader can see
    which figure the footnote is about — an understated number is otherwise
    typographically identical to an honest one, and the rows are ranked on it.
    A row with no cost at all already shows "—" and is never marked."""
    return (f"| {fmt_tokens(u['output'])} | {fmt_tokens(u['input'])} "
            f"| {fmt_tokens(u['cache_read'])} | {fmt_tokens(u['cache_5m'] + u['cache_1h'])} "
            f"| {fmt_cost(cost)}{'*' if partial and cost is not None else ''} |")


def partial_footnote(rows, kind):
    """Footnote for rows whose cost cell is a priced subtotal, or None.

    Counts only the MARKED cells: a row with no cost at all already shows "—",
    so counting it overstates how many of the numbers on screen mislead."""
    n = sum(1 for r in rows if r.get("partial") and r.get("cost_usd") is not None)
    if not n:
        return None
    return (f"{n} {kind}(s) partially priced (marked *): "
            "some of their usage ran on unpriced models.")


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
        lines.append(f"| {r['key']} | {r['calls']} " + _usage_cells(u, r["cost_usd"]))
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
    for note in (unpriced_footnote(data.get("unpriced_models") or []),
                 skipped_footnote(data.get("skipped_transcripts") or []),
                 projects_dir_footnote(data.get("projects_dir_missing"))):
        if note:
            lines += ["", note]
    return "\n".join(lines)


def run_top_consumers(by="session", since="30d", project=None, limit=10, warnings=None):
    """Costliest sessions (by="session") or command labels aggregated across
    sessions (by="command") in a window. Unpriced rows sort last."""
    pricing = load_pricing(warnings)
    cutoff = since_cutoff(since)
    missing_root = check_projects_root(warnings)
    unpriced, skipped = set(), []
    sessions, commands = [], {}
    for s in iter_summaries(pricing, cutoff=cutoff, project=project, skipped=skipped,
                            warnings=warnings):
        unpriced.update(unpriced_models(s.get("by_model", {}), pricing))
        if by == "session":
            # "partial": some of this session's usage ran on an unpriced
            # model, so cost_usd is a priced subtotal — the session ranks on
            # it, and a bare number would hide that.
            sessions.append({"session_id": Path(s["path"]).stem, "path": s["path"],
                             "project": s["project"], "first_ts": s["first_ts"],
                             "usage": s["total"]["usage"],
                             "cost_usd": s["total"]["cost_usd"],
                             "partial": bool(unpriced_models(s.get("by_model", {}), pricing))})
            continue
        for label, agg in s["by_label"].items():
            c = commands.setdefault(label, {"label": label, "sessions": 0, "invocations": 0,
                                            "usage": empty_usage(), "cost_usd": None,
                                            "partial": False})
            c["sessions"] += 1
            c["invocations"] += agg["invocations"]
            for k in c["usage"]:
                c["usage"][k] += agg["usage"].get(k, 0)
            if agg["cost_usd"] is None or agg.get("unpriced"):
                # The label's usage is complete but its cost is a priced
                # subtotal — say so rather than quietly understating it.
                # Either the whole segment was unpriced (cost_usd None) or it
                # mixed priced and unpriced models inside one session.
                c["partial"] = True
            if agg["cost_usd"] is not None:
                c["cost_usd"] = (c["cost_usd"] or 0.0) + agg["cost_usd"]
    rows = sessions if by == "session" else list(commands.values())
    key = "session_id" if by == "session" else "label"
    rows.sort(key=lambda r: (r["cost_usd"] is None, -(r["cost_usd"] or 0.0), r[key]))
    data = {"by": by, "since": since, "project": project, "limit": limit,
            "rows": rows[:limit], "unpriced_models": sorted(unpriced),
            "skipped_transcripts": skipped, "projects_dir_missing": missing_root}
    if by == "session":
        # Counted over the whole window: unpriced sessions rank last, so the
        # limit is exactly what hides them.
        data["unpriced_rows"] = sum(1 for r in rows if r["cost_usd"] is None)
    return data


def render_top_consumers(data):
    if not data["rows"]:
        # Still footnoted: "no rows" is exactly when the reader needs to know
        # whether there was anything to read in the first place.
        empty = ("No commands in window." if data["by"] == "command"
                 else "No sessions in window.")
        return "\n\n".join([empty] + [n for n in (
            skipped_footnote(data.get("skipped_transcripts") or []),
            projects_dir_footnote(data.get("projects_dir_missing"))) if n])
    if data["by"] == "session":
        lines = ["| Session | Project | Started | Output | Input | Cache read | Cache write | Est. cost |",
                 "|---|---|---|---:|---:|---:|---:|---:|"]
        for r in data["rows"]:
            u = r["usage"]
            lines.append(f"| {r['session_id']} | {r['project']} | {(r['first_ts'] or '')[:10]} "
                         + _usage_cells(u, r["cost_usd"], r.get("partial")))
    else:
        lines = ["| Command | Sessions | Calls | Output | Input | Cache read | Cache write | Est. cost |",
                 "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for r in data["rows"]:
            u = r["usage"]
            name = r["label"] if r["label"] == OTHER_LABEL else f"`{r['label']}`"
            lines.append(f"| {name} | {r['sessions']} | {r['invocations']} "
                         + _usage_cells(u, r["cost_usd"], r.get("partial")))
    notes = [unpriced_footnote(data.get("unpriced_models") or []),
             skipped_footnote(data.get("skipped_transcripts") or []),
             projects_dir_footnote(data.get("projects_dir_missing"))]
    note = partial_footnote(data["rows"], data["by"])
    if note:
        notes.append(note)
    if data["by"] == "session":
        shown = sum(1 for r in data["rows"] if r["cost_usd"] is None)
        cut = (data.get("unpriced_rows") or 0) - shown
        if cut > 0:
            notes.append(f"{cut} unpriced session(s) rank last and were cut by --limit.")
    for note in notes:
        if note:
            lines += ["", note]
    return "\n".join(lines)


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


def compute_baseline(pricing, project, days=30, exclude=None, warnings=None):
    """Per-project norms from the history index (median session cost; per-command
    median cost and cache-read ratio) over the trailing `days`, excluding the
    transcript at `exclude` (the session being analysed).

    `skipped_transcripts` lists the paths the scan could not read, and
    `projects_dir_missing` names a projects root that is not a directory at
    all: every baseline rule goes quiet below INSIGHT_MIN_BASELINE_SESSIONS,
    so a thinned corpus and a genuinely unremarkable session look identical
    without them."""
    cutoff = since_cutoff(f"{days}d")
    missing_root = check_projects_root(warnings)
    session_costs, commands, skipped = [], {}, []
    for s in iter_summaries(pricing, cutoff=cutoff, exclude=exclude, skipped=skipped,
                            warnings=warnings):
        if s["project"] != project:
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
        "skipped_transcripts": skipped,
        "projects_dir_missing": missing_root,
        "median_session_cost": _median(session_costs),
        "commands": {label: {"sessions": len(c["costs"]),
                             "median_cost": _median(c["costs"]),
                             "median_cache_ratio": _median(c["ratios"])}
                     for label, c in commands.items()},
    }


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

    order = {"warn": 0, "info": 1}
    out.sort(key=lambda f: (order[f["severity"]], f["rule"]))
    return out


def window_halves(summaries, cutoff, now=None):
    """(first, second) — the window's sessions split at its midpoint.

    Sessions with no usable timestamp can't be placed in either half and sit
    the comparison out. Shared with run_insights so the result can report how
    much of the window rules 7-8 actually had to compare: they are gated on
    the FIRST half having spend, and a window longer than the project's whole
    history puts everything in the second."""
    from datetime import datetime, timezone

    def _dt(iso):
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d

    now_dt = _dt(now) if now else datetime.now(timezone.utc)
    mid = (_dt(cutoff) + (now_dt - _dt(cutoff)) / 2).strftime("%Y-%m-%dT%H:%M:%SZ")
    placed = [s for s in summaries if s["first_ts"]]
    return ([s for s in placed if s["first_ts"] < mid],
            [s for s in placed if s["first_ts"] >= mid])


def half_cost(summaries):
    return sum(s["total"]["cost_usd"] or 0.0 for s in summaries)


def window_insights(summaries, cutoff, pricing, now=None, halves=None):
    """Findings for a --since window: trend between the window's halves,
    the top mover behind an increase, and window-wide unpriced models.

    `halves` lets the caller split the window ONCE and share the result: two
    calls to window_halves() take their own datetime.now(), so the midpoints
    could differ by a truncated second and the disclosure could then describe
    a different split from the one the rules ran on."""
    out = []
    first, second = halves if halves is not None else window_halves(summaries, cutoff, now=now)
    c1, c2 = half_cost(first), half_cost(second)

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


def run_insights(transcript=None, since=None, project=None, budget=None, warnings=None):
    pricing = load_pricing(warnings)
    if since:
        cutoff = since_cutoff(since)
        missing_root = check_projects_root(warnings)
        skipped = []
        summaries = list(iter_summaries(pricing, cutoff=cutoff, project=project,
                                        skipped=skipped, warnings=warnings))
        # The trend rules compare the window's halves, so "how many sessions
        # matched" is not the whole story of what could be compared: report
        # the first half too, and let the renderer disclose a dead one.
        halves = window_halves(summaries, cutoff)
        first = halves[0]
        c1 = half_cost(first)
        return {"mode": "window",
                "findings": window_insights(summaries, cutoff, pricing, halves=halves),
                # "first_half_spend" is the rules' own predicate, UNROUNDED:
                # first_half_cost is rounded for the payload, and a first half
                # under $5e-7 rounds to 0.0 while rules 7-8 still run on it.
                "baseline": {"sessions": len(summaries), "since": since,
                             "first_half_sessions": len(first),
                             "first_half_cost": round(c1, 6),
                             "first_half_spend": c1 > 0},
                "skipped_transcripts": skipped,
                "projects_dir_missing": missing_root}
    t = resolve_transcript(transcript)
    data = aggregate(parse_session(t), pricing)
    baseline = compute_baseline(pricing, project=t.parent.name, exclude=str(t),
                                warnings=warnings)
    # One canonical top-level key in both modes: render_insights footnotes it,
    # and a session-mode reader needs it most -- the baseline scan is the only
    # thing that makes session mode say anything at all.
    skipped = baseline.pop("skipped_transcripts")
    missing_root = baseline.pop("projects_dir_missing")
    return {"mode": "session",
            "findings": session_insights(data, baseline, budget=budget),
            "baseline": baseline,
            "skipped_transcripts": skipped,
            "projects_dir_missing": missing_root,
            "transcript_path": str(t)}


def insights_caveat(result):
    """The trailing "(baseline: ...)" qualifier naming rules that could not
    run, or "" when everything the mode offers did run.

    It rides on BOTH branches of the render, findings or none. Several rules
    need no baseline at all — ad-hoc dominance and unpriced models fire
    happily on a project with one prior session — so a non-empty findings list
    is not evidence that the comparison rules ran. Qualify only the empty
    branch and the qualifier's absence means nothing, while the skill teaches
    the reader to take it as "everything was checked".

    Two silences to break:
      * session mode — a baseline below INSIGHT_MIN_BASELINE_SESSIONS switches
        rules 1-2 off for every young project;
      * window mode — rules 7-8 are gated on the window's first half holding
        spend, so a window longer than the project's history (or one whose
        sessions all land after its midpoint) switches them off too. This
        keys off `first_half_spend`, the rules' OWN unrounded predicate:
        gating on the rounded `first_half_cost` made the qualifier deny
        trend findings printed two lines above it.

    A count the caller did not supply is left alone: state only what we know."""
    b = result.get("baseline") or {}
    mode = result.get("mode")
    if mode == "session":
        n = b.get("sessions")
        if n is not None and n < INSIGHT_MIN_BASELINE_SESSIONS:
            return (f"(baseline: {n} prior session(s); the comparison rules "
                    f"need {INSIGHT_MIN_BASELINE_SESSIONS})")
    elif mode == "window":
        if not b.get("sessions"):
            return ""
        spent = b.get("first_half_spend")
        if spent is None:                       # a result from before the key
            c1 = b.get("first_half_cost")
            spent = c1 is None or c1 > 0
        if not spent:
            if b.get("first_half_sessions") == 0:
                return ("(baseline: no sessions in the window's first half; "
                        "the trend rules need both halves)")
            return ("(baseline: no spend in the window's first half; "
                    "the trend rules need both halves)")
    return ""


def insights_all_clear(result, session):
    """The empty-findings sentence, qualified by what the scan managed to see.

    An empty `findings` list is also what "the rules could not run" looks
    like, and the commonest cause is silent: a window scan that matched no
    sessions at all (missing/mistyped TOKEN_USAGE_PROJECTS_DIR, a fresh
    machine, a `project` filter matching no slug). That one replaces the
    sentence outright; the narrower "some rules were off" cases are appended
    by insights_caveat() in either branch."""
    n = (result.get("baseline") or {}).get("sessions")
    if result.get("mode") == "window" and n == 0:
        return "No sessions in window — nothing was scanned."
    return ("No notable findings. " + session).strip()


def render_insights(result):
    """Findings as `- [severity] message` lines, or "No notable findings.".

    Session mode names the session in either branch — "nothing to report" is
    exactly when the reader has no other clue which session was measured, and
    discovery may well have guessed it. Both branches also carry the caveat —
    see insights_caveat()."""
    transcript_path = result.get("transcript_path")
    session = ""
    if transcript_path:
        tp = Path(transcript_path)
        session = f"(session: {tp.parent.name}/{tp.name})"
    caveat = insights_caveat(result)
    if result["findings"]:
        lines = [f"- [{f['severity']}] {f['message']}" for f in result["findings"]]
        tail = " ".join(x for x in (session, caveat) if x)
        if tail:
            lines.append(tail)
    else:
        lines = [" ".join(x for x in (insights_all_clear(result, session), caveat) if x)]
    for note in (skipped_footnote(result.get("skipped_transcripts") or []),
                 projects_dir_footnote(result.get("projects_dir_missing"))):
        if note:
            lines += ["", note]
    return "\n".join(lines)


def project_slug(path_str):
    # Claude Code slugs project paths by replacing every non-alphanumeric
    # character with a dash (not just / . _).
    return re.sub(r"[^A-Za-z0-9]", "-", path_str)


def _newest(paths):
    paths = list(paths)
    return max(paths, key=lambda p: p.stat().st_mtime) if paths else None


def _cowork_roots():
    """Cowork sandbox mount roots that might hold the live session's
    transcript, read-only, under <mount>/.claude/projects/<slug>/<id>.jsonl.
    A separate function so tests can neutralise it without depending on
    where this machine's real HOME or /sessions happen to point."""
    roots = [Path.home() / "mnt" / ".claude" / "projects"]
    sessions = Path("/sessions")
    if sessions.is_dir():
        roots.extend(sorted(sessions.glob("*/mnt/.claude/projects")))
    return roots


def _find_latest_with_source(project_dir=None):
    """(transcript, rung) for find_latest_transcript, (None, None) on a miss.

    Rungs: "project_dir" (the caller named a project), "cwd", "cowork",
    "any_project" — the last two only reachable without a project dir."""
    root = projects_dir()
    explicit = project_dir is not None
    target = Path(project_dir).expanduser().resolve() if explicit else Path.cwd()
    # 1) Claude Code: <projects>/<slug>/*.jsonl
    slug_dir = root / project_slug(str(target))
    if slug_dir.is_dir():
        f = _newest(slug_dir.glob("*.jsonl"))
        if f:
            return f, ("project_dir" if explicit else "cwd")
    if explicit:
        return None, None
    # 2) Cowork (Claude desktop app).
    for r in _cowork_roots():
        if r.is_dir():
            f = _newest(r.glob("*/*.jsonl"))
            if f:
                return f, "cowork"
    # 3) No project context at all: newest transcript on the machine.
    f = _newest(root.glob("*/*.jsonl")) if root.is_dir() else None
    return (f, "any_project") if f else (None, None)


def find_latest_transcript(project_dir=None):
    """Newest transcript for a project, or None.

    project_dir=None means "no project context to anchor on" (e.g. Claude
    desktop, or the MCP server with no caller-supplied hint): try cwd's own
    project directory, then the Cowork sandbox mounts (which hold exactly the
    live session), then the newest transcript under any project at all.

    An *explicit* project_dir only ever looks at that project's own
    directory — if it has no transcripts, that's None, not a guess at some
    other project's session."""
    return _find_latest_with_source(project_dir)[0]


def locate_transcript_with_source(arg=None, session_id=None, project_dir=None):
    """(transcript, rung) — the transcript to analyse and which rung produced it.

    Order: "explicit" (arg, must exist) > "session_id" (globbed across every
    project; several matches pick the newest by mtime, and the id is sanitised
    to [A-Za-z0-9_-] so it can never escape the projects dir) >
    "env" (TOKEN_USAGE_TRANSCRIPT, must exist) > discovery
    ("project_dir" / "cwd" / "cowork" / "any_project", see
    find_latest_transcript).

    On a miss the rung names the selector that stopped the search — an
    explicit path, a session id and TOKEN_USAGE_TRANSCRIPT all fail closed
    rather than falling through, so callers can say *which* one was wrong —
    and is None when discovery simply found nothing."""
    if arg:
        p = Path(arg)
        return (p if p.is_file() else None), "explicit"
    if session_id:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id))
        return (_newest(projects_dir().glob(f"*/{safe}.jsonl")) if safe else None), "session_id"
    env = os.environ.get("TOKEN_USAGE_TRANSCRIPT")
    if env:
        p = Path(env)
        return (p if p.is_file() else None), "env"
    return _find_latest_with_source(project_dir)


def locate_transcript(arg=None, session_id=None, project_dir=None):
    """Transcript to analyse, or None. locate_transcript_with_source documents
    the (authoritative) resolution order."""
    return locate_transcript_with_source(arg, session_id=session_id,
                                         project_dir=project_dir)[0]


def budget_from_env(warnings=None):
    """Session budget from TOKEN_USAGE_BUDGET_USD, or None when unset/unparseable.
    Shared by the CLI, the Stop hook and the MCP server so all three read the
    variable the same way. Unset is silent; a value that isn't a number — or
    isn't greater than zero — is a typo worth reporting (see warn())."""
    raw = os.environ.get("TOKEN_USAGE_BUDGET_USD")
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        warn(f"ignoring TOKEN_USAGE_BUDGET_USD={raw!r} — not a number", warnings)
        return None
    if not math.isfinite(value) or value <= 0:
        # 0, a negative value and NaN all parsed fine and then silently
        # switched the hook's nudge and insights rule 6 off, leaving the user
        # believing budget monitoring was armed.
        warn(f"ignoring TOKEN_USAGE_BUDGET_USD={raw!r} — must be > 0", warnings)
        return None
    return value


def resolve_transcript(arg):
    t, via = locate_transcript_with_source(arg)
    if t:
        return t
    if via == "explicit":
        # A path *was* passed, so "pass a path" is the wrong advice: the file
        # is simply not there (typo, stale path, wrong machine).
        sys.exit(f"token-usage: transcript not found: {arg}")
    if via == "env":
        sys.exit(f"token-usage: TOKEN_USAGE_TRANSCRIPT is set to "
                 f"{os.environ.get('TOKEN_USAGE_TRANSCRIPT')} but that file does not exist")
    sys.exit("token-usage: no transcript found — pass a path to a session .jsonl file")


def _hook_payload():
    """The hook payload as a dict, or None when stdin does not hold one.

    Read as BYTES and decoded here rather than by sys.stdin's locale-dependent
    wrapper. Hook payloads are JSON, i.e. UTF-8 by definition, so a
    POSIX-locale launchd/cron/container run must still understand an accented
    transcript path; and an undecodable byte raises UnicodeDecodeError — a
    ValueError, but NOT a json.JSONDecodeError — which used to escape the
    guard here exactly the way AttributeError/TypeError did.

    Well-formed JSON that isn't a hook payload gets no further either: `null`,
    a list or a bare string all parse, and .get() on them raised outside the
    guarded body below."""
    try:
        raw = sys.stdin.buffer.read()
    except (AttributeError, ValueError, OSError):
        # No binary stdin (a StringIO stand-in) or an unreadable one.
        try:
            raw = sys.stdin.read().encode("utf-8", "replace")
        except (ValueError, OSError):
            return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:  # malformed JSON — never block Claude Code on it
        return None
    return payload if isinstance(payload, dict) else None


def _hook_warn(message):
    """warn() that cannot itself raise — stderr may be an ASCII stream, and a
    diagnostic must never become the traceback it was reporting."""
    try:
        warn(message)
    except Exception:  # noqa: BLE001, S110 — nothing left to report it with
        pass


def _prior_budget_multiple(ledger):
    """How many budget multiples this session has already been nudged about."""
    if not ledger.exists():
        return 0
    try:
        prior = json.loads(ledger.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError):
        return 0
    if not isinstance(prior, dict):
        return 0
    pm = prior.get("budget_notified_multiple")
    if isinstance(pm, (int, float)) and not isinstance(pm, bool):
        return int(pm) if math.isfinite(pm) else 0
    return 1 if prior.get("budget_notified") else 0   # 0.2.x ledgers: bool only


def _write_ledger(ledger, data):
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    # Per-process temp names: concurrent SubagentStop hooks must never
    # interleave writes into a shared temp file.
    tmp = ledger.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(ledger)
    link_tmp = LEDGER_DIR / f".latest.{os.getpid()}.tmp"
    try:
        link_tmp.symlink_to(ledger)
        link_tmp.replace(LEDGER_DIR / "latest.json")
    except OSError:
        link_tmp.unlink(missing_ok=True)


def run_hook():
    """Stop/SubagentStop entry point: exit 0 for ANY stdin bytes, and write
    nothing to stdout but the hook protocol's own JSON. A non-zero exit or a
    traceback here breaks the user's session, so no exception class may
    escape — not even from the diagnostics."""
    try:
        return _run_hook(_hook_payload())
    except Exception as e:  # noqa: BLE001 — a hook must never break the session
        _hook_warn(f"hook: {type(e).__name__}: {e}")
        return 0


def _run_hook(payload):
    if payload is None:
        return 0
    transcript = payload.get("transcript_path")
    session_id = re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("session_id", "unknown"))) or "unknown"
    # A non-string transcript_path is not a path: Path() would raise TypeError.
    if not isinstance(transcript, str) or not transcript or not Path(transcript).exists():
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
    data = aggregate(parse_session(transcript), load_pricing())
    data["session_id"] = session_id
    data["transcript_path"] = str(transcript)
    ledger = LEDGER_DIR / f"{session_id}.json"

    prior_multiple = _prior_budget_multiple(ledger)
    limit = budget_from_env()
    cost = data["total"]["cost_usd"]
    priced = cost is not None and math.isfinite(cost)
    multiple = int(cost // limit) if (limit and priced) else 0
    # Parallel SubagentStop hooks race the ledger read-modify-write, so
    # only the (serial) Stop hook fires nudges and advances the counter.
    fire = multiple > prior_multiple and not is_subagent_stop
    notified = multiple if fire else prior_multiple
    data["budget_notified"] = notified >= 1
    data["budget_notified_multiple"] = notified

    try:
        _write_ledger(ledger, data)
    except Exception as e:  # noqa: BLE001 — a broken ledger must not break the session
        # stdout is the hook protocol, so the only channel left is stderr:
        # silence here means the budget nudge is dead with no way to find out.
        _hook_warn(f"hook: ledger update failed: {e}")
    if fire:
        # The nudge is the plugin's headline feature: it is emitted even when
        # the ledger write failed, as long as it could still be computed.
        top = "—"
        if data["by_label"]:
            top = max(data["by_label"].items(),
                      key=lambda kv: kv[1]["cost_usd"] or 0)[0]
        passed = (f"{multiple}× your ${limit:.2f} budget" if multiple >= 2
                  else f"your ${limit:.2f} budget")
        # ensure_ascii (the default) keeps this printable on an ASCII stdout.
        print(json.dumps({"systemMessage":
            f"token-usage: session estimate ${cost:.2f} has passed "
            f"{passed} — top consumer: {top}"}))
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
    i = sub.add_parser("insights")
    i.add_argument("transcript", nargs="?", default=None)
    i.add_argument("--since", default=None)
    i.add_argument("--project", default=None)
    i.add_argument("--json", action="store_true", dest="as_json")
    t = sub.add_parser("top_consumers")
    t.add_argument("--by", choices=("session", "command"), default="session")
    t.add_argument("--since", default="30d")
    t.add_argument("--project", default=None)
    t.add_argument("--limit", type=int, default=10)
    t.add_argument("--json", action="store_true", dest="as_json")
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
    if args.cmd == "insights":
        if args.transcript and args.since:
            sys.exit("token-usage: pass a transcript OR --since, not both")
        if args.project is not None and not args.since:
            # Session mode has no project filter to apply; accepting one would
            # silently report on whatever session discovery found instead.
            sys.exit("token-usage: --project applies to window mode (use --since)")
        budget = budget_from_env()
        result = run_insights(transcript=args.transcript, since=args.since,
                              project=args.project, budget=budget)
        print(json.dumps(result, indent=1) if args.as_json else render_insights(result))
        return
    if args.cmd == "top_consumers":
        if args.limit < 1:
            sys.exit("token-usage: --limit must be >= 1")
        data = run_top_consumers(by=args.by, since=args.since,
                                 project=args.project, limit=args.limit)
        print(json.dumps(data, indent=1) if args.as_json else render_top_consumers(data))
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
