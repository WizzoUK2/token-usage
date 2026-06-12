"""Shared fixtures: load the script as a module + synthetic transcript builders."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "token_usage.py"

# Loaded once at collection. Module-level constants (e.g. LEDGER_DIR) bind at import — in-process tests must monkeypatch module attributes, not env vars.
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
