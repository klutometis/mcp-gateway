"""End-to-end test harness for multi-instance gateway routing.

Spins up 3 wss-shim instances, 2 wss-bridges (laptop & desktop) wrapping
a labeled echo MCP, and a local gateway with a 3-instance config. The
third instance ("vm") has no bridge attached so we can exercise the
offline error path. Then runs an MCP client against gateway-local
asserting all seven routing cases pass.

Run with:
    uv run python tests/manual/multi_instance_e2e.py

On Ctrl-C (or normal exit) all child processes are cleaned up.

Topology:
    gateway-local :3100  /mcp
       └─ MultiInstanceProxy "echo"
            ├─ laptop  -> http://127.0.0.1:8013/mcp -> shim -> bridge -> echo(label=laptop)
            ├─ desktop -> http://127.0.0.1:8014/mcp -> shim -> bridge -> echo(label=desktop)
            └─ vm      -> http://127.0.0.1:8015/mcp -> shim -> (no bridge attached)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths (adjust if your layout differs)
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]  # mcp-gateway/
WSS_BRIDGE_REPO = REPO.parent / "wss-bridge"

GATEWAY_LOCAL = REPO / ".venv" / "bin" / "gateway-local"
WSS_SHIM = shutil.which("wss-shim") or str(
    Path.home() / ".local" / "bin" / "wss-shim"
)
WSS_BRIDGE = WSS_BRIDGE_REPO / ".venv" / "bin" / "wss-bridge"
BRIDGE_PYTHON = WSS_BRIDGE_REPO / ".venv" / "bin" / "python"

WORKDIR = Path("/tmp/multi_instance_e2e")
LOGDIR = WORKDIR / "logs"

# Use high ports to minimize collisions with anything the operator
# already has running locally (including their day-to-day gateway-local
# on the canonical :3100).
GATEWAY_PORT = 13100
SHIM_PORTS = {"laptop": 18013, "desktop": 18014, "vm": 18015}

# Address to bind the shims' /bridge endpoint to. 127.0.0.1 (default)
# only allows local bridges. With --bind-lan we listen on 0.0.0.0 so a
# bridge on another machine on the LAN can dial in.
SHIM_BIND_HOST = "127.0.0.1"

# Bridges to skip spawning locally. Useful when you'll run that bridge
# from another machine. Populated by --no-bridge NAME.
SKIP_LOCAL_BRIDGES: set[str] = set()

# Seconds to wait for remote bridges (the ones we skipped) to dial in
# and complete their MCP handshake. Configurable via --wait N. The
# default (60s) accounts for first-run `uv run` overhead of installing
# fastmcp on the remote box, which can take 20-30s.
REMOTE_BRIDGE_WAIT_SECONDS: float = 60.0

# Shared bearer token for all shim/bridge pairs (toy; real deploy uses
# per-class tokens from env vars).
SHARED_TOKEN = "e2e-token-xyz"


# ---------------------------------------------------------------------------
# Asset writers
# ---------------------------------------------------------------------------


LABELED_ECHO_SRC = '''\
"""Labeled echo MCP: each instance returns its BRIDGE_LABEL in responses.

Used by tests/manual/multi_instance_e2e.py to prove which backing actually
handled a routed call.
"""
import os
from fastmcp import FastMCP

LABEL = os.environ.get("BRIDGE_LABEL", "unlabeled")
mcp = FastMCP(f"labeled-echo-{LABEL}")


@mcp.tool
def ping(msg: str) -> str:
    """Echo back the message, prefixed with this bridge's label."""
    return f"[{LABEL}] pong: {msg}"


if __name__ == "__main__":
    mcp.run()
'''


SHIM_TOOLS_CONFIG = {
    "name": "echo",
    "version": "0.1.0",
    "bearer_token_env": "SHIM_E2E_TOKEN",
    "tools": [
        {
            "name": "ping",
            "description": "Echo back with the bridge's label.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "msg": {"type": "string", "description": "Message to echo."}
                },
                "required": ["msg"],
            },
        }
    ],
}


def gateway_servers_config() -> dict:
    """servers.json for gateway-local with a 3-instance 'echo' server."""
    return {
        "echo": {
            "transport": "streamable-http",
            "param_name": "host",
            "instances": {
                host: {"url": f"http://127.0.0.1:{port}/mcp"}
                for host, port in SHIM_PORTS.items()
            },
            "tools": {
                "ping": {
                    "name": "echo_ping",
                    "title": "Echo Ping",
                    "description": "Ping the echo MCP on a specified host.",
                }
            },
        }
    }


def write_assets() -> dict[str, Path]:
    """Write all config + script files to WORKDIR. Returns paths."""
    WORKDIR.mkdir(parents=True, exist_ok=True)
    LOGDIR.mkdir(exist_ok=True)

    echo_mcp = WORKDIR / "labeled_echo_mcp.py"
    echo_mcp.write_text(LABELED_ECHO_SRC)

    shim_cfg = WORKDIR / "shim-tools.json"
    shim_cfg.write_text(json.dumps(SHIM_TOOLS_CONFIG, indent=2))

    servers_json = WORKDIR / "servers.json"
    servers_json.write_text(json.dumps(gateway_servers_config(), indent=2))

    return {
        "echo_mcp": echo_mcp,
        "shim_cfg": shim_cfg,
        "servers_json": servers_json,
    }


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------


class ProcessManager:
    """Spawn + kill background processes, redirecting output to log files."""

    def __init__(self) -> None:
        self.procs: list[tuple[str, subprocess.Popen]] = []

    def spawn(self, label: str, cmd: list[str], env: dict[str, str] | None = None) -> None:
        logf = open(LOGDIR / f"{label}.log", "w")
        full_env = {**os.environ, **(env or {})}
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=full_env,
            start_new_session=True,
        )
        self.procs.append((label, proc))
        print(f"  spawned {label} (pid={proc.pid}) → {LOGDIR}/{label}.log")

    def shutdown(self) -> None:
        for label, proc in reversed(self.procs):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for label, proc in reversed(self.procs):
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()

    def assert_all_alive(self) -> None:
        dead = [(l, p.returncode) for l, p in self.procs if p.poll() is not None]
        if dead:
            raise RuntimeError(
                f"Processes exited unexpectedly: {dead}. See {LOGDIR}/ for logs."
            )


@contextmanager
def managed_processes():
    pm = ProcessManager()
    try:
        yield pm
    finally:
        print("\nTearing down processes…")
        pm.shutdown()


# ---------------------------------------------------------------------------
# Wait-for-ready helpers
# ---------------------------------------------------------------------------


async def wait_for_gateway_tools(client_url: str, expected_tools: set[str],
                                  timeout: float = 15.0) -> None:
    """Poll gateway tools/list until expected tools appear or timeout."""
    from fastmcp import Client

    deadline = time.monotonic() + timeout
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            async with Client(client_url) as c:
                tools = await c.list_tools()
                got = {t.name for t in tools}
                if expected_tools.issubset(got):
                    return
                last_err = AssertionError(
                    f"expected {expected_tools}, got {got}"
                )
        except Exception as e:
            last_err = e
        await asyncio.sleep(0.3)
    raise RuntimeError(f"gateway not ready after {timeout}s: {last_err}")


def wait_for_port(port: int, timeout: float = 10.0) -> None:
    """Poll a TCP port until something accepts connections."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"nothing listening on :{port} after {timeout}s")


def assert_ports_free(ports: list[int]) -> None:
    """Refuse to start if any of our ports are already bound.

    Otherwise we'd silently connect to whatever's on the colliding port
    and report nonsense (ask me how I know).
    """
    import socket
    busy: list[int] = []
    for p in ports:
        try:
            with socket.create_connection(("127.0.0.1", p), timeout=0.2):
                busy.append(p)
        except OSError:
            pass  # nothing there, good
    if busy:
        raise RuntimeError(
            f"Ports already in use: {busy}. Stop whatever's bound or "
            "adjust GATEWAY_PORT/SHIM_PORTS at the top of this file."
        )


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


async def run_tests(gateway_url: str) -> int:
    """Run the seven test cases. Return number of failures."""
    from fastmcp import Client

    failures = 0
    pass_count = 0

    def report(case: str, ok: bool, detail: str = "") -> None:
        nonlocal failures, pass_count
        if ok:
            pass_count += 1
            print(f"  ✓ {case}")
            if detail:
                print(f"      {detail}")
        else:
            failures += 1
            print(f"  ✗ {case}")
            if detail:
                print(f"      {detail}")

    async with Client(gateway_url) as c:
        tools = await c.list_tools()
        tool_names = {t.name for t in tools}

        # ────────────── case 1: enum + synthetic tool present ──────────────
        ping = next((t for t in tools if t.name == "echo_ping"), None)
        if ping is None:
            report("tools/list includes echo_ping", False,
                   f"got: {sorted(tool_names)}")
        else:
            schema = ping.inputSchema
            props = schema.get("properties", {})
            enum_ok = (
                "host" in props
                and props["host"].get("enum") == ["laptop", "desktop", "vm"]
            )
            report(
                "echo_ping schema has host enum=[laptop,desktop,vm]",
                enum_ok,
                f"properties.host = {props.get('host')}",
            )
            required_ok = "host" in (schema.get("required") or [])
            report(
                "echo_ping schema requires host (3 instances)",
                required_ok,
                f"required = {schema.get('required')}",
            )

        # No synthetic host_list — with enum-in-schema the configured
        # set is already on every tools/list. Removing it keeps the
        # tool surface lean. If we ever switch to bare schema (no
        # enum), restore the discovery tool then.
        report(
            "tools/list does NOT include a synthetic host_list",
            "host_list" not in tool_names,
            f"got tools: {sorted(tool_names)}",
        )

        # ────────────── case 3: success on laptop ──────────────────────────
        try:
            result = await c.call_tool(
                "echo_ping", {"host": "laptop", "msg": "hello"}
            )
            text = _extract_text(result)
            ok = "[laptop]" in text and "hello" in text
            report("echo_ping(host=laptop) routes to laptop bridge", ok,
                   f"got: {text!r}")
        except Exception as e:
            report("echo_ping(host=laptop) routes to laptop bridge", False,
                   f"raised: {type(e).__name__}: {e}")

        # ────────────── case 4: success on desktop ─────────────────────────
        try:
            result = await c.call_tool(
                "echo_ping", {"host": "desktop", "msg": "hello"}
            )
            text = _extract_text(result)
            ok = "[desktop]" in text and "hello" in text
            report("echo_ping(host=desktop) routes to desktop bridge", ok,
                   f"got: {text!r}")
        except Exception as e:
            report("echo_ping(host=desktop) routes to desktop bridge", False,
                   f"raised: {type(e).__name__}: {e}")

        # ────────────── case 5: offline error on vm ────────────────────────
        try:
            result = await c.call_tool(
                "echo_ping", {"host": "vm", "msg": "hello"}
            )
            text = _extract_text(result)
            is_error = getattr(result, "is_error", False)
            mentions_others = "laptop" in text and "desktop" in text
            report(
                "echo_ping(host=vm) returns enriched offline error",
                is_error and mentions_others,
                f"is_error={is_error}, text={text!r}",
            )
        except Exception as e:
            # Errors might surface as exceptions in some clients; treat as
            # success if message includes the alternatives.
            msg = str(e)
            ok = "laptop" in msg and "desktop" in msg
            report(
                "echo_ping(host=vm) returns enriched offline error",
                ok,
                f"raised: {type(e).__name__}: {e}",
            )

        # ────────────── case 6: missing param when multiple ────────────────
        try:
            result = await c.call_tool("echo_ping", {"msg": "hello"})
            text = _extract_text(result)
            is_error = getattr(result, "is_error", False)
            mentions_all = (
                "laptop" in text and "desktop" in text and "vm" in text
            )
            report(
                "echo_ping(no host) errors listing all configured hosts",
                is_error and mentions_all,
                f"is_error={is_error}, text={text!r}",
            )
        except Exception as e:
            msg = str(e)
            ok = "laptop" in msg and "desktop" in msg and "vm" in msg
            report(
                "echo_ping(no host) errors listing all configured hosts",
                ok,
                f"raised: {type(e).__name__}: {e}",
            )

        # ────────────── case 7: unknown host value ─────────────────────────
        try:
            result = await c.call_tool(
                "echo_ping", {"host": "bogus", "msg": "hello"}
            )
            text = _extract_text(result)
            is_error = getattr(result, "is_error", False)
            mentions_all = (
                "laptop" in text and "desktop" in text and "vm" in text
            )
            report(
                "echo_ping(host=bogus) errors listing valid hosts",
                is_error and mentions_all and "bogus" in text,
                f"is_error={is_error}, text={text!r}",
            )
        except Exception as e:
            msg = str(e)
            ok = (
                "laptop" in msg and "desktop" in msg and "vm" in msg
                and "bogus" in msg
            )
            report(
                "echo_ping(host=bogus) errors listing valid hosts",
                ok,
                f"raised: {type(e).__name__}: {e}",
            )

    print(f"\n{'='*60}")
    print(f"  PASS: {pass_count}  FAIL: {failures}")
    print(f"{'='*60}")
    return failures


def _extract_text(result: Any) -> str:
    if hasattr(result, "content"):
        return "".join(
            getattr(item, "text", "") for item in result.content
        )
    return str(result)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> None:
    """Minimal flag parsing; argparse would be overkill for three flags."""
    global SHIM_BIND_HOST, REMOTE_BRIDGE_WAIT_SECONDS
    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag == "--bind-lan":
            SHIM_BIND_HOST = "0.0.0.0"
            i += 1
        elif flag == "--no-bridge":
            if i + 1 >= len(argv):
                raise SystemExit("--no-bridge requires a name (laptop|desktop)")
            SKIP_LOCAL_BRIDGES.add(argv[i + 1])
            i += 2
        elif flag == "--wait":
            if i + 1 >= len(argv):
                raise SystemExit("--wait requires a number of seconds")
            REMOTE_BRIDGE_WAIT_SECONDS = float(argv[i + 1])
            i += 2
        elif flag in ("-h", "--help"):
            print(__doc__)
            print(
                "\nFlags:\n"
                "  --bind-lan          bind shims on 0.0.0.0 (LAN-reachable)\n"
                "  --no-bridge NAME    don't spawn the local bridge for NAME\n"
                "  --wait SECONDS      how long to wait for remote bridges\n"
                f"                      to dial in (default {REMOTE_BRIDGE_WAIT_SECONDS:.0f}s)\n"
            )
            raise SystemExit(0)
        else:
            raise SystemExit(f"unknown flag {flag!r} (try --help)")


async def main() -> int:
    parse_args(sys.argv[1:])

    # Sanity-check binaries exist (skip bridge-related ones if we're
    # not spawning any local bridges).
    needed = [
        ("gateway-local", GATEWAY_LOCAL),
        ("wss-shim", WSS_SHIM),
    ]
    local_bridges = [
        h for h in ("laptop", "desktop") if h not in SKIP_LOCAL_BRIDGES
    ]
    if local_bridges:
        needed += [
            ("wss-bridge", WSS_BRIDGE),
            ("bridge-python", BRIDGE_PYTHON),
        ]
    for label, path in needed:
        if not Path(path).exists():
            print(f"ERROR: missing {label} at {path}", file=sys.stderr)
            return 2

    print("Pre-flight: checking ports are free…")
    assert_ports_free([GATEWAY_PORT, *SHIM_PORTS.values()])

    print(f"Writing assets to {WORKDIR}/ …")
    assets = write_assets()

    with managed_processes() as pm:
        # 1. shims
        print(f"\nStarting 3 shims (bind={SHIM_BIND_HOST})…")
        for host, port in SHIM_PORTS.items():
            pm.spawn(
                f"shim-{host}",
                [
                    str(WSS_SHIM),
                    "--config", str(assets["shim_cfg"]),
                    "--listen", f"{SHIM_BIND_HOST}:{port}",
                ],
                env={"SHIM_E2E_TOKEN": SHARED_TOKEN},
            )
        for port in SHIM_PORTS.values():
            wait_for_port(port)

        # 2. bridges (laptop + desktop by default; vm intentionally
        # has no bridge; any host in SKIP_LOCAL_BRIDGES is also skipped
        # so the operator can dial in from another machine).
        local_bridges = [
            h for h in ("laptop", "desktop") if h not in SKIP_LOCAL_BRIDGES
        ]
        skipped = [
            h for h in ("laptop", "desktop") if h in SKIP_LOCAL_BRIDGES
        ]
        print(
            f"\nStarting {len(local_bridges)} local bridge(s): "
            f"{', '.join(local_bridges) or '<none>'}"
        )
        if skipped:
            print(f"\nSkipping local bridge for: {', '.join(skipped)}")
            print("Dial in manually from another machine, e.g.:")
            for h in skipped:
                print(
                    f"  BRIDGE_LABEL={h} wss-bridge --wss-url "
                    f"ws://localhost:{SHIM_PORTS[h]}/bridge "
                    f"--token {SHARED_TOKEN} --cmd python labeled_echo_mcp.py"
                )
        for host in local_bridges:
            port = SHIM_PORTS[host]
            pm.spawn(
                f"bridge-{host}",
                [
                    str(WSS_BRIDGE),
                    "--wss-url", f"ws://127.0.0.1:{port}/bridge",
                    "--token", SHARED_TOKEN,
                    "--cmd", str(BRIDGE_PYTHON), str(assets["echo_mcp"]),
                ],
                env={"BRIDGE_LABEL": host},
            )

        # Give bridges a moment to dial in + MCP handshake. If we're
        # waiting for a remote bridge, give the operator more time — the
        # first `uv run` on the remote can take 20-30s to install
        # fastmcp before the subprocess responds to the shim's handshake.
        if skipped:
            print(
                f"\nGiving you {REMOTE_BRIDGE_WAIT_SECONDS:.0f}s to start the "
                f"remote bridge(s). If you're not ready, those instances\n"
                f"will hit the 'helper offline' path (which is also a "
                f"valid test). Pass --wait N to change."
            )
            await asyncio.sleep(REMOTE_BRIDGE_WAIT_SECONDS)
        else:
            await asyncio.sleep(1.5)
        pm.assert_all_alive()

        # 3. gateway
        print("\nStarting gateway-local…")
        pm.spawn(
            "gateway-local",
            [str(GATEWAY_LOCAL)],
            env={
                "MCP_SERVERS_FILE": str(assets["servers_json"]),
                "MCP_PORT": str(GATEWAY_PORT),
            },
        )
        wait_for_port(GATEWAY_PORT)

        gateway_url = f"http://127.0.0.1:{GATEWAY_PORT}/mcp/"
        print(f"\nWaiting for gateway to advertise echo_ping at {gateway_url}…")
        await wait_for_gateway_tools(
            gateway_url, expected_tools={"echo_ping"}
        )

        print("\nRunning test cases…\n")
        failures = await run_tests(gateway_url)
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
