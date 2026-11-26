"""A stdio MCP backend that exits on anything before ``initialize``.

That is how rmcp, the Rust MCP SDK, behaves (tmux-mcp-rs 0.2.1 through
0.6.0): sent ``server/discover``, it logs "expect initialized request" and
exits instead of answering, so the client's "auto" mode never sees the error
it would fall back on. Handshake era only; one tool, ``echo``.

Run as a script: newline-delimited JSON-RPC on stdin/stdout.
"""

from __future__ import annotations

import json
import sys


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    initialized = False
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if not initialized and method != "initialize":
            sys.stderr.write(f"Failed to start server: expect initialized request, but received: {method}\n")
            sys.exit(1)
        if method == "initialize":
            initialized = True
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "protocolVersion": msg["params"]["protocolVersion"],
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "discover-intolerant", "version": "0"},
                    },
                }
            )
        elif method == "tools/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "Echo text back.",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            }
                        ]
                    },
                }
            )
        elif method == "tools/call":
            text = msg["params"]["arguments"]["text"]
            _send({"jsonrpc": "2.0", "id": msg["id"], "result": {"content": [{"type": "text", "text": text}]}})
        elif "id" in msg:
            _send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"no {method}"}})


if __name__ == "__main__":
    main()
