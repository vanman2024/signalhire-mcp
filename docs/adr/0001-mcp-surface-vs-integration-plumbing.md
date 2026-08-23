# ADR-0001 — MCP is a surface, not the plumbing

- **Status:** **partially superseded (2026-08-22).** The decision *rule* stands. The
  **ownership choice (option A) does not** — see §Ownership.
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

## Ownership — REOPENED 2026-08-22

Option A (StaffHive owns the durable callback inbox; this repo becomes a pure
agent-facing surface) was chosen on 2026-08-17 and is **withdrawn before any code
was written against it.**

It rested on an assumption this ADR named and did not verify: that signalhire-mcp
is a StaffHive component. §Open originally said option C should be revisited "only
if a second consumer appears that is not StaffHive — which is the still-open
'StaffHive component or standalone product?' question." That question has now been
answered, the other way:

> SignalHire is **one integration among many**. The system connects to it by API
> key, structured like any other integration, under an organizational hierarchy
> that owns the list. It is deliberately **not** wired to StaffHive.

Under that answer option A is wrong in a specific way: it makes this repo's value
conditional on one consumer, and it retires the very capabilities (durable
vendor-facing receipt, pluggable delivery, per-tenant credentials) that let it be
a general integration rather than a point-to-point pipe.

**Option C is the shape that matches** — this repo owns vendor-facing durability
(the 10-second budget, retries, dedup, the raw payload) and stays
consumer-neutral; whatever consumes it owns its own canonical persistence. The
`TenantRegistry` / `credential_for` / `Adapter` seam already implements this at
the single-integration level.

Nothing is retired. `InboxStore`, `delivery/` and the callback route stay.

### Still true, and unaffected

The decision *rule* is untouched, and staffhive#188's fix stands on its own:

- MCP is a surface for models; app-facing calls and inbound vendor webhooks are
  ordinary plumbing below both front doors.
- **Persist before acknowledge.** A 2xx to a vendor promises the payload is safe.
  This is why staffhive#203 is right regardless of who owns what — StaffHive's own
  receiver was acknowledging before persisting, which destroyed billed reveals.
  That fix is about StaffHive's handler, not about this repo's ownership.
- Keep MCP servers pure enough that FastMCP Cloud fits. Still open, and now
  harder: if this repo keeps the receive path, the two FastMCP Cloud blockers
  (no documented persistent volume, no documented non-`/mcp` ingress) come back
  and must be solved rather than sidestepped.

### What is now blocking

The prerequisite is the **organizational hierarchy** that owns a list of
integrations — the level above `TenantRegistry`. Until its shape is known, where
the inbox lives and how a credential is resolved are premature questions.

## Consequences

- `deploy-to-droplet.sh` and `install.sh` are superseded by the FastMCP Cloud decision.
- The receive path **stays** pending the organizational-hierarchy design.
- The FastMCP Cloud storage and ingress questions are unresolved again, and are now
  the real blockers rather than an ownership question.
- The reason both options were considered is recorded here so nobody re-derives it.

## References

- `staffhive#188` — durable callback persistence + targeted ATS sync (the prior art)
- `staffhive#196` — standardize integration runtime around FastMCP + durable adapters
- `staffhive#187` — workflow registry + CATS MCP adapter boundary
- symptoms: `staffhive#76`, `#96`, `#97`, `#149`
- <https://gofastmcp.com/servers/storage-backends> — file storage "not suitable for distributed deployments"
- <https://gofastmcp.com/deployment/http> — custom routes are never covered by auth middleware
