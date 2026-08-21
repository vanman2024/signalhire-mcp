# ADR-0001 — MCP is a surface, not the plumbing

- **Status:** accepted (2026-08-17) — ownership resolved to option A
- **Date:** 2026-08-17
- **Context repos:** `signalhire-mcp`, `staffhive` (issues #188, #196, #187)
- **Supersedes in part:** the single-process collapse in `531e06c`

## Context

Three facts collided.

1. **MCP servers exist for models.** Their value is discovery, descriptions, schemas and
   taught failure modes — this server's `INSTRUCTIONS` teach the credit model and the
   two-pool 402 trap. That value is real for an agent and exactly zero for application code
   that already knows what it wants.
2. **FastMCP Cloud (Prefect Horizon) is the standing host** for all self-built MCP servers.
   It is MCP-shaped: it serves `https://<name>.fastmcp.app/mcp`, redeploys on every push to
   `main`, autoscales, and documents no persistent volume and no guarantee that it routes
   any path other than `/mcp`.
3. **The SignalHire reveal is asynchronous and billed at submission.** A credit is spent when
   the request is made; the answer arrives later by webhook; SignalHire retries three times
   and then destroys the result permanently.

`531e06c` collapsed the receiver into the MCP process to fix a real defect — the old split
ran the receiver in a separate process holding results in memory that no tool could read.
That was correct for a single droplet. It is wrong for FastMCP Cloud, and the two-service
split it replaced was in fact the documented deployment intent
(`docs/deployment/.fastmcp-deployments.json`: *"Server requires external callback server for
webhook handling"*).

## Decision

**One core, several front doors. MCP is one of them, not the trunk.**

| Caller | Interface | Why |
| :--- | :--- | :--- |
| A model deciding what to do | **MCP tool** | discovery, descriptions, taught failure modes |
| Application code that already knows what it wants | **library call / thin REST** | no discovery needed; MCP adds a protocol and a round-trip for nothing |
| A vendor POSTing to us | **plain HTTPS** | SignalHire does not speak MCP and never will |

Three consequences follow.

**The receive path is shared infrastructure, below both front doors.** SignalHire does not
know whether an agent or an application asked. The test that settles it: if the receiver
lives inside the MCP server, any application wanting enrichment *without* an agent has to run
an MCP server just to catch webhooks. That is the wrong place.

**Persist before acknowledge, always — and acknowledge only what is durably stored.** A 2xx
to a vendor is a promise that the payload is safe. Returning 200 and continuing work in the
background converts a retryable vendor failure into permanent data loss. This is the live
defect in `staffhive/src/app/api/webhooks/signalhire/callback/route.ts:445`, which schedules
the canonical candidate update inside Next.js `after()` and returns 200 first — what #188
names **"transport success without business success."**

**Keep MCP servers pure, so FastMCP Cloud fits.** Once state lives in a network datastore the
MCP server is stateless: an outbound connection, which Horizon handles fine. Both platform
risks dissolve rather than being worked around — no persistent volume is needed because state
is not on the container, and no non-`/mcp` ingress is needed because the webhook lands
elsewhere. The standing FastMCP Cloud policy works *because* not everything runs through MCP.

If a server ever genuinely needs to serve `/mcp` **and** a real REST API from one deployment,
FastMCP mounts inside a FastAPI app — but that is not a FastMCP Cloud deployment, and the
exception belongs in the policy rather than being broken quietly.

## Ownership — resolved

**StaffHive owns the durable callback inbox** (option A, chosen 2026-08-17), following
`staffhive#188` as written: *"StaffHive/Supabase is canonical persistence."*

SignalHire posts directly to StaffHive. StaffHive inserts the raw payload into
`webhook_events` **before** returning 2xx, then correlates and persists to `candidates` in a
retryable worker, then emits `candidate.contacts.enriched`, then optionally runs a targeted
CATS sync. No intermediate service, no extra hop.

Two rejected alternatives, recorded so they are not silently revisited:

- **B — `signalhire-mcp` owns it.** Rejected: contradicts #188's canonical decision and makes
  the StaffHive candidate row derivative of an integration service.
- **C — split by concern**, with this repo owning vendor-facing durability and delivering via
  `WebhookAdapter`. Genuinely defensible and it preserves working code, but it reintroduces the
  extra hop and a second store. Revisit only if a second consumer appears that is not StaffHive
  — which is the still-open "StaffHive component or standalone product?" question in
  `specs/product-brief.md`.

### What this costs, stated plainly

Part of `531e06c` was built for a deployment model now retired. `InboxStore`, `delivery/` and
the `routes.py` callback handler become redundant *in this repo*. That is roughly the durable
half of the refactor. The design is not wasted — `staffhive#188` specifies the same state
machine, and this implementation is the reference for it.

### The consequence that follows, and is not yet decided

`tools.py` currently reads results back through the store: `get_reveal_results`,
`check_reveal_status`, `list_requests`, `list_failed_events`, `requeue_event`. With the store
gone from this repo, **those tools have nothing to read.** A stateless `signalhire-mcp` can
search, submit a reveal, and check credits — all synchronous — but cannot answer "what came
back," because the answer now lives in StaffHive.

Three ways out, to be decided before W001-02 finishes:

1. Retire those tools here; agents read results through StaffHive's own MCP surface.
   Keeps this server consumer-neutral. Most consistent with the decision.
2. Keep them, reading from StaffHive over the network. Couples this server to StaffHive and
   contradicts `config.py`'s stated neutrality.
3. Keep them as thin pass-throughs over a StaffHive-owned API, neutral by configuration.

Option 1 is the default unless someone argues otherwise.

## Consequences

- `routes.py`'s callback handler leaves the MCP server; `/health` stays.
- `tools.py` is untouched — it already only talks to the store.
- `InboxStore` grows a network backend behind its existing interface, or is retired (see §Open).
- `deploy-to-droplet.sh` and `install.sh` are superseded.
- The reason for the split is recorded here so nobody "simplifies" it back into one process in
  six months without reading why.

## References

- `staffhive#188` — durable callback persistence + targeted ATS sync (the prior art)
- `staffhive#196` — standardize integration runtime around FastMCP + durable adapters
- `staffhive#187` — workflow registry + CATS MCP adapter boundary
- symptoms: `staffhive#76`, `#96`, `#97`, `#149`
- <https://gofastmcp.com/servers/storage-backends> — file storage "not suitable for distributed deployments"
- <https://gofastmcp.com/deployment/http> — custom routes are never covered by auth middleware
