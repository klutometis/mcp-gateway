"""Plan 057 end to end: this gateway's signed-identity proxy in front of a
real per-person wss-shim sidecar, with two fake laptops.

Proves the two repos agree on the wire (header, claims, key, audience):
a caller reaches their own laptop by any of their addresses, someone with
no laptop gets a named error, and a wrong key or another sidecar's
assertion reaches nobody.

wss-shim's per-person code is on its `per-person-slots` branch, so point
--with at a checkout of that branch:

    uv run --with ~/.harness/worktrees/wss-shim-per-person \
        python tests/manual/wss_shim_signed_identity_e2e.py

Exit status 0 means every case passed.
"""
import asyncio, json, secrets, socket, sys, tempfile, time, pathlib
import jwt, uvicorn, websockets
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jwt.algorithms import OKPAlgorithm
from fastmcp import Client
from wss_shim.identity import Identity, IdentityConfig
from wss_shim.sidecar import build_app
from mcp_gateway.identity import make_identity_forwarding_proxy

KEY = secrets.token_urlsafe(32)
tinker = Ed25519PrivateKey.generate()
def device_token(sub, emails):
    now = int(time.time())
    return jwt.encode({"sub": sub, "emails": emails, "dev": "chrome", "jti": secrets.token_hex(4),
                       "iat": now, "exp": now + 3600}, tinker, algorithm="EdDSA", headers={"kid": "k1"})

async def laptop(port, owner, token):
    ws = await websockets.connect(f"ws://127.0.0.1:{port}/bridge", subprotocols=["mcp"],
                                  additional_headers={"Authorization": f"Bearer {token}"})
    async def serve():
        try:
          async for raw in ws:
            m = json.loads(raw)
            if "id" not in m: continue
            r = ({"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": owner, "version": "0"}}
                 if m["method"] == "initialize" else
                 {"content": [{"type": "text", "text": f"{owner}'s Chrome"}]})
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r}))
        except websockets.ConnectionClosed:
            pass
    return ws, asyncio.create_task(serve())

async def main():
    d = pathlib.Path(tempfile.mkdtemp())
    jwk = {**OKPAlgorithm.to_jwk(tinker.public_key(), as_dict=True), "kid": "k1", "alg": "EdDSA"}
    (d / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    cfg = IdentityConfig.from_config({"mode": "required", "device": "chrome", "label": "Chrome",
                                      "jwks_file": str(d / "jwks.json")}, env={"WSS_SHIM_ASSERTION_KEY": KEY})
    _, app = build_app("chrome", "e2e", [{"name": "list_pages", "description": "tabs",
                       "inputSchema": {"type": "object", "properties": {}}}], None, Identity(cfg, audience="chrome"))
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started: await asyncio.sleep(0.01)
    for owner, emails in [("peter", ["peter@danenberg.name", "peter@danenberg.ai"]), ("logan", ["logan@example.com"])]:
        await laptop(port, owner, device_token(owner, emails))
    while len(app.state.registry.people) < 2: await asyncio.sleep(0.01)

    caller = {"email": None}
    def gateway(key=KEY, audience="chrome"):
        return make_identity_forwarding_proxy(f"http://127.0.0.1:{port}/mcp", lambda: caller["email"],
                                              carrier="signed", signing_key=key.encode(), audience=audience)
    async def call(proxy, email):
        caller["email"] = email
        async with Client(proxy) as c:
            r = await c.call_tool("list_pages", {}, raise_on_error=False)
        return ("ERROR " if r.is_error else "") + "".join(getattr(b, "text", "") for b in r.content)

    gw = gateway()
    results = [
        ("peter via .ai", await call(gw, "peter@danenberg.ai"), "peter's Chrome"),
        ("peter via .name", await call(gw, "Peter@Danenberg.name"), "peter's Chrome"),
        ("logan", await call(gw, "logan@example.com"), "logan's Chrome"),
        ("no laptop", await call(gw, "brian@example.com"), "ERROR Chrome is not connected for brian@example.com."),
        ("gateway with the wrong key", await call(gateway(key="w" * 43), "logan@example.com"), "ERROR caller identity rejected"),
        ("assertion for another sidecar", await call(gateway(audience="tmux"), "logan@example.com"), "ERROR caller identity rejected"),
    ]
    ok = True
    for name, got, want in results:
        good = (got == want) if not want.startswith("ERROR") else (got.startswith("ERROR") and want[6:] in got)
        ok &= good
        print(f"{'PASS' if good else 'FAIL'}  {name:30} -> {got[:90]}")
    server.should_exit = True
    await asyncio.wait_for(task, 5)
    sys.exit(0 if ok else 1)

asyncio.run(main())
