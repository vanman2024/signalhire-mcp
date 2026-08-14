# SignalHire MCP Server

A FastMCP 4 server for SignalHire contact enrichment, with a **durable callback
inbox** and **pluggable delivery adapters**.

One process serves all three surfaces on one port:

| Path | Purpose |
|---|---|
| `/mcp/` | The MCP endpoint — tools, resources, prompts, skills |
| `/signalhire/callback/{tenant}` | SignalHire's webhook |
| `/health` | Liveness plus inbox depth |

---

## Why this was rebuilt

SignalHire's reveal API is asynchronous and **bills at submission**. You POST
identifiers, get a `requestId`, and results arrive minutes later as a webhook.
The credit is gone the moment you submit — so a callback that is received and
dropped is money burned for nothing. SignalHire retries a failed callback three
times and then **discards it permanently**.

The previous design could not hold up its end of that bargain:

- **The webhook receiver was a different process.** A FastAPI app on a daemon
  thread, or on a separate droplet entirely. Results landed in a process the
  MCP server could not read.
- **No handler was ever registered** against that receiver, so every payload
  was logged and dropped.
- **No correlation record was written** at submission, so `get_request_status`
  could only ever answer "unknown" — and it looked up a request id in a cache
  keyed by candidate uid.
- **The serverless path acknowledged before persisting.** It returned `200` and
  then did the real work — Supabase writes, Chromium PDF generation, ATS sync —
  inside Vercel's `after()`, which the platform kills at the function timeout.
  The `200` was a promise that was routinely false.
- **Every failure was `console.error` and `continue`.** No retry, no queue, no
  record.
- **No auth.** The deployment listened on `0.0.0.0:8000` with nothing in front.
  On this server that is not merely a read risk: every reveal tool spends a
  credit, so an open endpoint is a way to bill the account.

This version fixes the structure, not just the symptoms.

---

## How it works

```
SignalHire ──POST──▶ /signalhire/callback/{tenant}
                            │
                            │  (1) verify shared secret
                            │  (2) persist raw payload to disk   ← fsync
                            │  (3) return 200                     ← ~6ms
                            ▼
                     durable inbox  (events/pending/)
                            │
                            ▼
                     delivery worker ── retry w/ backoff ──┐
                            │                              │
              ┌─────────────┼──────────────┐               │
              ▼             ▼              ▼               │
       WebhookAdapter  McpAdapter    McpAdapter            │
        (StaffHive)     (CATS MCP)   (other ATS)           │
              │             │              │               │
              └─────────────┴──────────────┘               │
                            │                              │
                  all succeeded → done/      any failed ───┘
                                             exhausted → failed/ (parked, kept)
```

**Persist before acknowledging.** The callback handler does no normalisation,
no adapter work, and no network I/O. It verifies the secret, writes the payload
with `fsync`, and returns. Measured at ~6ms against a 10-second budget.

**Delivery is separate and retryable.** Adapters are tracked independently: if
StaffHive accepts an event and the ATS is down, only the ATS is retried.
Re-delivering to an adapter that already succeeded would duplicate writes.

**Nothing is deleted.** An event that exhausts its retries is *parked*, not
dropped. The payload stays readable, and `retry_delivery()` requeues it.

---

## Adapters — CATS is not special

An adapter writes an enrichment result somewhere. There are two kinds, and
neither contains vendor-specific code:

- **`webhook`** — POST the normalised result to a URL.
- **`mcp`** — call a tool on another MCP server.

CATS is reached through the `mcp` adapter, pointed at the existing
[CATS MCP server](../cats-mcp-server). There is no CATS HTTP client in this
repository and there should never be one — duplicating a maintained 200-tool
server here would mean two implementations drifting apart. Adding Bullhorn or
Greenhouse later is a config entry, not an integration.

```json
{
  "acme": {
    "signalhire_api_key_env": "ACME_SIGNALHIRE_KEY",
    "adapters": [
      {"type": "webhook", "name": "staffhive",
       "url": "https://staffhive.example.com/api/webhooks/signalhire/relay",
       "secret_env": "STAFFHIVE_RELAY_SECRET"},
      {"type": "mcp", "name": "cats",
       "server": "http://127.0.0.1:3000/mcp/",
       "tool": "upsert_candidate_from_enrichment",
       "auth_token_env": "CATS_MCP_TOKEN"}
    ]
  }
}
```

Secrets are referenced by environment variable name, never inlined, so this
value is safe in a config file.

### Mounting vs. calling

Two different things, often confused:

- `SIGNALHIRE_MOUNTS` exposes another server's tools *to agents* through this
  endpoint (`ats_*`). That is composition, for discovery.
- The `mcp` **adapter** calls a remote server from the delivery worker.

They are configured separately on purpose — you may well want to write to CATS
without exposing its 200 tools to whatever model is connected here.

---

## Multi-tenancy

One process can serve several customers. Two models, both supported by the same
code path:

- **BYOK** — each customer brings their own SignalHire account
  (`signalhire_api_key_env` per tenant). Clean isolation and billing.
- **Agency** — you own one key. Note that SignalHire has **no sub-account
  model**: seats share one credit pool *and share all revealed contacts*. So
  this server is the only place spend can be attributed per customer, and data
  isolation must be enforced by the consuming application.

**The tenant identifier comes from verified token claims, never from a tool
argument.** A `tenant_id` parameter would be model-controlled — any caller could
spend another customer's credits by asking. A test enforces that no tool exposes
one. Multi-tenant routing therefore requires `SIGNALHIRE_AUTH_MODE=jwt`.

---

## Quick start

```bash
# Install (fastmcp 4 is a prerelease; exact pins keep that scoped to fastmcp)
uv pip install --prerelease=allow -e ".[dev]"

cp .env.example .env    # then fill it in
```

Minimum viable `.env` for local development:

```bash
SIGNALHIRE_API_KEY=your_key
SIGNALHIRE_TRANSPORT=stdio          # stdio needs no auth mode
SIGNALHIRE_DATA_DIR=./.signalhire-data
SIGNALHIRE_PUBLIC_BASE_URL=https://your-tunnel.example.com
SIGNALHIRE_CALLBACK_SECRET=$(openssl rand -hex 32)
```

Run it:

```bash
fastmcp run fastmcp.json           # or: python -m signalhire_mcp.app
fastmcp inspect fastmcp.json --format fastmcp   # see the whole surface
```

> Reveals need a **publicly reachable HTTPS** callback URL. For local work use a
> tunnel (`ngrok http 8000`) and set `SIGNALHIRE_PUBLIC_BASE_URL` to it.
> Without one, every reveal is billed by SignalHire and then discarded.

---

## Deployment

```bash
export SIGNALHIRE_API_KEY='...'
export SIGNALHIRE_PUBLIC_BASE_URL='https://signalhire.example.com'
export SIGNALHIRE_CALLBACK_SECRET="$(openssl rand -hex 32)"
export SIGNALHIRE_AUTH_MODE='platform'
bash deploy-to-droplet.sh
```

This deploys **one** systemd service. There is no separate callback service —
that split is what broke the callback.

The server **refuses to start** on HTTP if you have not said who authenticates
callers (`SIGNALHIRE_AUTH_MODE`) or set `SIGNALHIRE_CALLBACK_SECRET`. Both
refusals are deliberate: guessing wrong in either direction is harmful, and the
callback route cannot sit behind MCP auth because SignalHire sends no bearer
token.

Put TLS in front (nginx/caddy). SignalHire requires a valid certificate.

---

## Tools

| Tool | Notes |
|---|---|
| `search_prospects` | Free. Returns UIDs, no contacts. |
| `scroll_search_results` | Cursor expires in 15s; fails fast rather than retrying. |
| `reveal_contact` | 1 credit, charged at submission. |
| `batch_reveal_contacts` | Up to 100. Rejects larger batches rather than splitting silently. |
| `check_credits` | Names which of the two pools it read. |
| `get_request_status` | Reads the durable inbox — correct across restarts. |
| `get_enrichment_result` | The stored profiles. No second call to SignalHire. |
| `list_requests` | Recent submissions. |
| `list_failed_deliveries` | Parked callbacks, with the failing adapter named. |
| `retry_delivery` | Requeue a parked callback. |

`export_results` was removed. It was a stub that returned `"to be implemented"`
while reporting success — a tool that lies is worse than a missing one, because
the agent believes it.

### The two credit pools

The most common confusing error from this API. SignalHire keeps two independent
balances; `without_contacts=true` draws on a pool that is **zero on most
accounts**. A `402` almost never means the account is empty — it means the
request went to the wrong pool. Leave `without_contacts` at its default.

---

## Skills

The server ships a client-facing skill and serves it as an MCP resource, so a
connecting client can learn the workflow without a local install:

```
skill://signalhire-enrichment/SKILL.md
```

Add your own with `SIGNALHIRE_SKILLS_DIR`.

---

## Testing

```bash
pytest tests/ -q      # 86 tests
```

Two patterns, because the FastMCP docs cover one of them:

- **Tools** use the documented in-memory client: `Client(transport=mcp)`,
  assert on `result.data`.
- **Custom HTTP routes** — the callback, which the docs say nothing about — run
  the real ASGI app through `httpx2.ASGITransport` with the Starlette lifespan
  driven in its own task. (Its own task because MCP's streamable-HTTP manager
  opens an anyio task group, and a task group must be exited by the task that
  entered it; pytest-asyncio sets up and tears down async fixtures in
  *different* tasks.)

Nothing in the suite touches the network or can spend credits.

---

## Troubleshooting

**Results never arrive.** Do not resubmit — the credit is already spent.

1. `get_request_status(request_id)` — distinguishes "not yet" from "broken".
2. `list_failed_deliveries()` — parked callbacks with the last error.
3. `retry_delivery(event_id)` once the downstream problem is fixed.
4. `GET /health` — inbox depth and whether the worker is running.

**SignalHire never called back at all.** The cause is on the network path:
a callback URL that is not publicly reachable, a TLS certificate the vendor
rejects, or a mismatched secret returning 401. Check the server log for
rejected callbacks — SignalHire gives up after three attempts.

**`Value is an unresolved placeholder`.** A `${VAR}` in `fastmcp.json` whose
variable is unset. Note there is **no `${VAR:-default}` syntax** — FastMCP looks
up the entire capture as a variable name, so `${VAR:-x}` searches for a variable
literally called `VAR:-x` and the placeholder survives *even when `VAR` is set*.
Use plain `${VAR}`; defaults belong in `config.Settings`.

---

## Layout

```
src/signalhire_mcp/
  app.py            entrypoint (fastmcp.json points here)
  server.py         create_server() — the single factory
  config.py         Settings
  runtime.py        shared objects
  routes.py         the callback + health endpoints
  tools.py          MCP tools
  resources.py      resources and prompts
  observability.py  middleware (and why there is so little)
  auth/             caller authentication, verified claims
  credentials/      SignalHire credential resolution (leak-guarded)
  inbox/            the durable store
  delivery/         adapters, tenant registry, retry worker
  skills/           client-facing skill, served over MCP
```

`create_server()` is the only place a `FastMCP` is constructed and the only
place components are registered — so importing the module always yields a fully
populated server, which is what `fastmcp inspect` and every test rely on.
