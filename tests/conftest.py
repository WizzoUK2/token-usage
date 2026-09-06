"""Shared fixtures: load the script as a module + synthetic transcript builders."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "token_usage.py"

# Loaded once at collection. Module-level constants (e.g. LEDGER_DIR) bind at import — in-process tests must monkeypatch module attributes, not env vars.
_spec = importlib.util.spec_from_file_location("token_usage", SCRIPT)
# Public so module-level test helpers (not just the `tu` fixture) can
# monkeypatch the very same module object the MCP server imports.
TOKEN_USAGE = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TOKEN_USAGE)


@pytest.fixture
def tu():
    return TOKEN_USAGE


@pytest.fixture(autouse=True)
def _isolated_pricing_overlay(monkeypatch, tmp_path_factory):
    # Keep the suite hermetic: never read the developer's real user overlay.
    monkeypatch.setenv("XDG_CONFIG_HOME",
                       str(tmp_path_factory.mktemp("xdg-isolated")))


@pytest.fixture(autouse=True)
def _no_cowork_mounts(monkeypatch):
    # Transcript discovery falls through to the Cowork sandbox mounts, which on
    # a real Cowork host hold a live transcript this suite must never see.
    # Neutralised once, for every test file, rather than per module — a test
    # that reaches discovery indirectly must be hermetic too. A test that wants
    # a mount sets its own roots afterwards.
    monkeypatch.setattr(TOKEN_USAGE, "_cowork_roots", list)


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


SERVER = Path(__file__).resolve().parent.parent / "scripts" / "mcp_server.py"


@pytest.fixture
def mcp():
    """The MCP server module, bound to the SAME token_usage instance as `tu`
    so monkeypatching either side is visible to both."""
    import sys
    sys.modules.setdefault("token_usage", TOKEN_USAGE)
    spec = importlib.util.spec_from_file_location("mcp_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
