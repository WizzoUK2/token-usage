#!/usr/bin/env python3
"""token-usage — attribute Claude Code token usage to the work that consumed it.

Parses Claude Code session transcripts (~/.claude/projects/<project>/<session>.jsonl),
deduplicates streamed usage entries by requestId (taking per-field maxima, since
streamed duplicates may carry partial usage snapshots), segments the session at
slash-command invocations (a command owns all turns until the next command),
rolls subagent transcripts up into the segment that spawned them, and prices
the result against a bundled pricing table.

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


def load_pricing():
    bundled = Path(__file__).resolve().parent.parent / "data" / "pricing.json"
    if bundled.exists():
        try:
            return json.loads(bundled.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_PRICING


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
            if cand.startswith(key) and (best is None or len(key) > len(best)):
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
    return {
        "by_label": by_label,
        "total": {
            "usage": sum_buckets(total_by_model),
            "cost_usd": cost_usd(total_by_model, pricing),
            "cache_savings_usd": cache_savings_usd(total_by_model, pricing),
            "models": sorted(total_by_model),
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


def render_report(data, show_agents=False):
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
        if show_agents and agg.get("agents"):
            for g in agg["agents"]:
                gu = g["usage"]
                lines.append(
                    f"| ↳ {g['type']} ×{g['count']} | | {fmt_tokens(gu['output'])} | {fmt_tokens(gu['input'])} "
                    f"| {fmt_tokens(gu['cache_read'])} | {fmt_tokens(gu['cache_5m'] + gu['cache_1h'])} "
                    f"| {fmt_cost(g['cost_usd'])} |"
                )
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
    lines.append(f"Models: {models}. Cost is an API-price estimate (cache-aware); "
                 "subscription plans are not billed per token.")
    return "\n".join(lines)


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


def find_latest_transcript():
    # Claude Code slugs project paths by replacing /, ., and _ with dashes.
    slug = re.sub(r"[/._]", "-", str(Path.cwd()))
    project_dir = Path.home() / ".claude" / "projects" / slug
    if not project_dir.is_dir():
        return None
    files = sorted(project_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


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
    try:
        data = aggregate(parse_session(transcript), load_pricing())
        data["session_id"] = session_id
        data["transcript_path"] = str(transcript)
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        ledger = LEDGER_DIR / f"{session_id}.json"

        prior_notified = False
        if ledger.exists():
            try:
                prior = json.loads(ledger.read_text())
                prior_notified = isinstance(prior, dict) and bool(prior.get("budget_notified"))
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
        p.add_argument("--diff", nargs=2, metavar=("OLD", "NEW"), default=None)
    sub.add_parser("hook")
    args = ap.parse_args()

    if args.cmd == "hook":
        sys.exit(run_hook())
    if getattr(args, "diff", None):
        if getattr(args, "agents", False):
            sys.exit("token-usage: --diff and --agents cannot be combined")
        d = diff_data(Path(args.diff[0]), Path(args.diff[1]), load_pricing())
        print(json.dumps(d, indent=1) if args.cmd == "json" else render_diff(d))
        return
    transcript = resolve_transcript(getattr(args, "transcript", None))
    data = aggregate(parse_session(transcript), load_pricing())
    data["transcript_path"] = str(transcript)
    if args.cmd == "json":
        print(json.dumps(data, indent=1))
    else:
        print(render_report(data, show_agents=getattr(args, "agents", False)))


if __name__ == "__main__":
    main()
