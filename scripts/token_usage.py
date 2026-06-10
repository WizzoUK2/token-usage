#!/usr/bin/env python3
"""token-usage — attribute Claude Code token usage to the work that consumed it.

Parses Claude Code session transcripts (~/.claude/projects/<project>/<session>.jsonl),
deduplicates streamed usage entries by requestId, segments the session by slash-command
invocations, rolls subagent transcripts up into the segment that spawned them, and
prices the result against a bundled pricing table.

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


def rates_for(model, pricing):
    if not model:
        return None
    best = None
    for key in pricing:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return pricing.get(best) if best else None


def empty_usage():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_5m": 0, "cache_1h": 0, "requests": 0}


def add_usage(bucket, usage):
    bucket["input"] += usage.get("input_tokens") or 0
    bucket["output"] += usage.get("output_tokens") or 0
    bucket["cache_read"] += usage.get("cache_read_input_tokens") or 0
    cc = usage.get("cache_creation") or {}
    five_m = cc.get("ephemeral_5m_input_tokens")
    one_h = cc.get("ephemeral_1h_input_tokens")
    if five_m is None and one_h is None:
        # Older transcripts: only the flat total exists; assume 5m TTL.
        five_m = usage.get("cache_creation_input_tokens") or 0
        one_h = 0
    bucket["cache_5m"] += five_m or 0
    bucket["cache_1h"] += one_h or 0
    bucket["requests"] += 1


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
    by_model, seen, first_ts = {}, set(), None
    for entry in iter_jsonl(path):
        if first_ts is None and entry.get("timestamp"):
            first_ts = entry["timestamp"]
        if entry.get("type") != "assistant":
            continue
        req = entry.get("requestId")
        if req and req in seen:
            continue
        if req:
            seen.add(req)
        msg = entry.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        bucket = by_model.setdefault(msg.get("model") or "unknown", empty_usage())
        add_usage(bucket, usage)
    return by_model, first_ts


def parse_session(transcript_path):
    transcript_path = Path(transcript_path)
    segments = []  # chronological: {label, start_ts, usage, models, prompt}

    def new_segment(label, ts, prompt=""):
        segments.append({
            "label": label, "start_ts": ts, "by_model": {},
            "prompt": prompt.strip()[:120], "subagents": [],
        })

    seen_requests = set()
    for entry in iter_jsonl(transcript_path):
        if is_user_prompt(entry):
            text = text_of((entry.get("message") or {}).get("content"))
            m = COMMAND_RE.search(text)
            label = m.group(1).strip() if m else OTHER_LABEL
            new_segment(label, entry.get("timestamp"), "" if m else text)
            continue
        if entry.get("type") != "assistant":
            continue
        req = entry.get("requestId")
        if req and req in seen_requests:
            continue
        if req:
            seen_requests.add(req)
        msg = entry.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        if not segments:
            new_segment(OTHER_LABEL, entry.get("timestamp"))
        seg = segments[-1]
        bucket = seg["by_model"].setdefault(msg.get("model") or "unknown", empty_usage())
        add_usage(bucket, usage)

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
                "type": meta.get("agentType", "agent"),
                "description": meta.get("description", ""),
                "output_tokens": sum_buckets(a_by_model)["output"],
            })
    return segments


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
    for agg in by_label.values():
        agg["usage"] = sum_buckets(agg["by_model"])
        agg["cost_usd"] = cost_usd(agg["by_model"], pricing)
    return {
        "by_label": by_label,
        "total": {
            "usage": sum_buckets(total_by_model),
            "cost_usd": cost_usd(total_by_model, pricing),
            "models": sorted(total_by_model),
        },
        "segments": [
            {**{k: s[k] for k in ("label", "start_ts", "prompt", "subagents")},
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


def render_report(data):
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
    t = data["total"]
    u = t["usage"]
    lines.append(
        f"| **Total** | | **{fmt_tokens(u['output'])}** | **{fmt_tokens(u['input'])}** "
        f"| **{fmt_tokens(u['cache_read'])}** | **{fmt_tokens(u['cache_5m'] + u['cache_1h'])}** "
        f"| **{fmt_cost(t['cost_usd'])}** |"
    )
    models = ", ".join(sorted(t["models"]))
    lines.append("")
    lines.append(f"Models: {models}. Cost is an API-price estimate (cache-aware); "
                 "subscription plans are not billed per token.")
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
        tmp = ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(ledger)
        (LEDGER_DIR / "latest.json").unlink(missing_ok=True)
        try:
            (LEDGER_DIR / "latest.json").symlink_to(ledger)
        except OSError:
            pass
    except Exception:
        return 0  # a broken ledger update must never break the session
    return 0


def main():
    ap = argparse.ArgumentParser(prog="token-usage")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("report", "json"):
        p = sub.add_parser(name)
        p.add_argument("transcript", nargs="?", default=None)
    sub.add_parser("hook")
    args = ap.parse_args()

    if args.cmd == "hook":
        sys.exit(run_hook())
    transcript = resolve_transcript(getattr(args, "transcript", None))
    data = aggregate(parse_session(transcript), load_pricing())
    data["transcript_path"] = str(transcript)
    if args.cmd == "json":
        print(json.dumps(data, indent=1))
    else:
        print(render_report(data))


if __name__ == "__main__":
    main()
