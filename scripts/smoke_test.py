#!/usr/bin/env python3
"""Call tools on a real Deck through the MCP stdio protocol (read-only by default).

    python3 scripts/smoke_test.py                       # all read-only tools
    python3 scripts/smoke_test.py game_info '{"game": "hades"}'
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from steamdeck_mcp.server import TOOLS  # noqa: E402

# Tools that need arguments get a harmless default; tools that are slow or hit the network are skipped.
DEFAULT_ARGS = {"game_info": None, "find_game_logs": None, "read_log": None, "measure_fps": {"seconds": 5},
                "achievements": None, "workshop_items": None, "view_screenshot": None}


def run(calls, timeout=300):
    msgs = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}]
    msgs += [{"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}}
             for i, (n, a) in enumerate(calls, 1)]
    p = subprocess.run([sys.executable, "-I", os.path.join(ROOT, "server.py")], capture_output=True, text=True,
                       input="\n".join(map(json.dumps, msgs)) + "\n", timeout=timeout)
    failures = 0
    for line in p.stdout.splitlines():
        reply = json.loads(line)
        if reply["id"] == 0:
            continue
        name = calls[reply["id"] - 1][0]
        result = reply.get("result", {})
        failed = "error" in reply or result.get("isError")
        failures += bool(failed)
        texts = [c["text"] for c in result.get("content", []) if c["type"] == "text"]
        images = sum(c["type"] == "image" for c in result.get("content", []))
        text = (f"[{images} image] " if images else "") + (texts[0] if texts else json.dumps(reply.get("error")))
        print(f"{'FAIL' if failed else ' ok '}  {name}: {text[:200 if failed else 80].replace(chr(10), ' ')}")
    return failures


if __name__ == "__main__":
    if len(sys.argv) > 1:
        calls = [(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {})]
    else:
        calls = [(t["name"], DEFAULT_ARGS.get(t["name"]) or {}) for t in TOOLS
                 if t["annotations"]["readOnlyHint"] and DEFAULT_ARGS.get(t["name"], {}) is not None]
    sys.exit(1 if run(calls) else 0)
