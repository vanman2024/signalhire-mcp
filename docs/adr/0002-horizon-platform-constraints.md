# ADR-0002 — What Prefect Horizon actually does, and what it forecloses

- **Status:** accepted (facts, not a choice)
- **Date:** 2026-09-07
- **Resolves:** the three questions ADR-0001 §"Still true, and unaffected" left open
- **Source:** <https://docs.horizon.prefect.io> (official platform documentation)

## Context

ADR-0001 reasoned about FastMCP Cloud (Prefect Horizon) from **absence of
documentation**: it "documents no persistent volume and no guarantee that it
routes any path other than `/mcp`." That is an inference, and it was load-bearing
for both the option A decision and its reversal. Issue #4 inherited the same
inference and stamped it as justification.

The inference has now been replaced with the documentation itself. `docs.horizon.prefect.io`
answers all three questions directly. This ADR records the answers so nobody
re-derives them — which has already cost two research passes.

**This ADR decides nothing.** It records platform facts and names precisely which
previously-open options they eliminate. The hosting choice remains blocked on the
organizational-hierarchy design (ADR-0001 §"What is now blocking").

## The facts

### 1. Only `/mcp` is served. Custom routes do not exist.

> "Custom routes, health checks, and webhooks are not supported — traffic reaches
> only the standard MCP HTTP endpoint through the gateway layer."

`GET /mcp` and `DELETE /mcp` return method-not-allowed **at the gateway, without
invoking server code at all**. Only `POST /mcp` reaches the process.

This is stronger than ADR-0001's guess. It is not "no guarantee that other paths
route" — other paths are documented as unsupported.

### 2. The filesystem is ephemeral, and there is no instance affinity.

> "The Horizon filesystem is ephemeral… any files written before a redeploy will
> not persist after the deployment."

> "There is no affinity guarantee, so subsequent requests may be routed to
> different instances."

Also documented: a **170-second request timeout** and **1024 MB memory**.

The affinity clause is the subtler half and the more damaging one. Even within a
single deployment's lifetime, a payload written by instance A is invisible to a
tool call served by instance B. A durable disk would not fix this; only moving
state off the container does.

### 3. Caller identity **is** forwarded — as HTTP headers, not a JWT.

The gateway "removes any client-supplied `horizon-*` identity headers and adds
trusted actor context":

| Header | Contents |
| :--- | :--- |
| `horizon-actor` | Horizon ID of the user or service account |
| `horizon-actor-type` | `user` or `service_account` |
| `horizon-actor-email` | user's email where available |
| `horizon-user-role` | organization role |
| `horizon-server-roles` | resolved server roles |

> "These are standard HTTP headers, not JWT tokens."

## Consequences

### The callback route cannot be served from a Horizon-hosted MCP server

`routes.py`'s `POST /signalhire/callback/{tenant_id}` is unreachable on Horizon.
So is `GET /health`. Neither fails loudly — the gateway rejects them before the
process sees anything, so there is no log line on our side to notice.

This forces the receive path out of a Horizon deployment **for a platform reason,
independent of ownership.** Issue #4's objective ("extract the receive path") is
therefore correct, and its stated destination (StaffHive) remains withdrawn per
ADR-0001 §Ownership. The two are separable and both hold.

### `InboxStore` cannot run on Horizon

Its own docstring already scoped it: *"correct for one writer process. Running two
replicas against the same directory would let both claim the same event."* Fact 2
says Horizon provides neither a durable directory nor a single writer. `InboxStore`
is sound for a droplet or a container with a volume, and unusable here.

`fastmcp.json` currently ships `SIGNALHIRE_DATA_DIR`, `SIGNALHIRE_PUBLIC_BASE_URL`
and `SIGNALHIRE_CALLBACK_SECRET` in its Horizon `env` block. All three assume
capabilities Horizon does not provide. That file is misleading as committed.

### BYOK is reachable, but requires a deliberate security exception

`auth/claims.py` states it "deliberately never consults tool arguments, HTTP
headers, query parameters, or the request body. Those are all attacker- or
model-controlled." On Horizon that premise is false for exactly one header family:
the gateway strips client-supplied `horizon-*` headers before adding its own, and
is the only route to the process.

So per-user credential resolution is achievable — `horizon-actor` is a trustworthy
tenant discriminator — but **not** via `AUTH_MODE=jwt`, which expects a verified
token that Horizon does not send. It requires a narrow, documented exception to the
never-trust-headers rule, valid only under `AuthMode.PLATFORM` behind Horizon.

That exception is security-relevant and is deliberately **not** made here. It gets
its own change and its own review, and it depends on the hierarchy design naming
what `horizon-actor` should resolve *to*.

### What this does not decide

Where the receive path goes. Two shapes survive:

- an always-on receiver off Horizon (function, worker, container) writing to a
  network datastore, with Horizon serving the stateless MCP surface; or
- host the whole server off Horizon, keeping Horizon for genuinely stateless MCP
  servers.

Choosing between them needs the organizational hierarchy first. This ADR only
establishes that "keep everything in one Horizon deployment" is no longer among
the options.

## References

- <https://docs.horizon.prefect.io> — gateway routing, identity forwarding, compute model
- ADR-0001 §"Still true, and unaffected" — the three questions this answers
- `src/signalhire_mcp/inbox/store.py` — the single-writer docstring
- `src/signalhire_mcp/auth/claims.py` — the never-trust-headers rule
- `fastmcp.json` — the env block that assumes a durable disk and a reachable callback
- <https://gofastmcp.com/servers/storage-backends> — file storage "not suitable for distributed deployments"
