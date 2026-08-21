# SignalHire MCP Server — product brief

**Profile:** `integration-project` (Integration / MCP Server)
**Delivery:** web: n · pwa: n · native mobile: n — headless service, no human surface
**Status:** draft — **every inference below is marked and needs confirmation**
**Amended:** 2026-08-17 — deployment target decided (FastMCP Cloud / Prefect Horizon)

> Reconstructed from the code, the README and the repository during
> `project-baseline`. A brownfield project has its product decisions embedded in
> code and in someone's head; this writes them down so future features are not
> re-argued from scratch. Sections the code genuinely could not answer are marked
> **OPEN** rather than invented — a guessed ICP is worse than an absent one,
> because it gets planned against.

## Problem

*(inferred from code + README — high confidence)*

A recruiter needs contact details for candidates found in a search. SignalHire has
the data, but its reveal API is hostile to naive integration in one specific way:
**it bills a credit at submission and returns the answer later, by webhook.** If
that webhook is lost, the money is gone and the data is not retrievable — SignalHire
retries three times, then discards permanently.

Every failure mode in the previous implementation was a variant of that:
receiver in a separate process the tools could not read, no handler registered,
acknowledgement before persistence, `console.error` and `continue`. The problem
this product solves is not "call the SignalHire API" — it is **not losing paid-for
data between an asynchronous vendor and a destination system.**

## Who it is for

- **User** — *(inferred)* an AI agent or MCP client acting for a recruiter, and the
  recruiter behind it.
- **Buyer** — **OPEN.** The SignalHire account holder pays SignalHire directly; whether
  this server is ever sold, or is purely internal infrastructure, is not recorded anywhere.
- **ICP** — **OPEN.** The code is deliberately consumer-neutral (`config.py`: *"Nothing
  here knows about StaffHive, recruiting workflows, or any particular consumer"*), yet
  StaffHive is named as a concrete delivery target in the README diagram.
  Whether the ICP is *"our own StaffHive deployment"*, *"any agency running an ATS"*,
  or *"any MCP user with a SignalHire account"* is undecided, and it materially changes
  S17 (public developer platform) and the whole multi-tenant question.

## What it does

Puts SignalHire behind an MCP tool surface (search, reveal, batch reveal, credit checks,
status, results) and guarantees the asynchronous half: a callback endpoint that persists
before acknowledging, a durable on-disk inbox, and a background worker that delivers each
stored result to every configured destination with retry and backoff, parking rather than
deleting anything it cannot deliver.

The three things it must do well:

1. **Never lose a billed callback.** Persist-before-ack; 500 on persist failure to earn
   back vendor retries; crash recovery at startup.
2. **Never spend a credit twice.** The server's own `INSTRUCTIONS` teach the credit model
   and the two-pool 402 trap to whatever model connects, because the most expensive
   mistakes here are made by the caller, not the server.
3. **Deliver anywhere without touching the receive path.** A pluggable `Adapter` protocol
   with an idempotent contract; adding an ATS is a new adapter, not a change to intake.

## Differentiation

*(inferred)* Against the realistic alternative — calling SignalHire directly from
application code — the difference is that the asynchronous, billed path is handled once,
correctly, instead of at every call site. Against a generic webhook/queue product, the
difference is that the vendor's specific constraints (10-second budget, three retries then
permanent discard, two independent credit pools) are encoded rather than discovered in
production.

## Domains

Single product, five bounded contexts, zero ownership conflicts:
`enrichment` · `callback-inbox` · `delivery` · `tenancy` · `platform`.
Detail and canonical/consumer authority are recorded in the graph (`domain_ownership`)
and summarised in [`baseline.md`](baseline.md).

## Commercial model

- **Model:** **OPEN.** No billing code, no entitlement checks, no pricing surface exists.
- **Free tier / paid tiers / upgrade trigger:** **OPEN** — not applicable unless this is
  ever sold.
- **Unit economics:** *(known)* the real cost driver is the **SignalHire credit**, spent
  per identifier at submission. Prior research recorded that SignalHire uses a shared team
  credit pool with no per-seat fee. Two independent balances exist ("with contacts" /
  "without contacts") and the second is zero on most accounts — spending against it returns
  402 even with thousands in the main pool.

> Monetization (S19) is not in this profile's capability set. If the answer to the ICP
> question is "sold to others", that changes and this section must be filled in properly.

## AI requirements

No models, retrieval or agents inside the server. It is **consumed by** AI agents over MCP,
and it invests in that relationship deliberately: it serves its own agent skills as MCP
resources, so a connecting client learns the credit model and failure diagnosis without
anyone installing a skill locally. Not autonomous; takes no action it was not asked for.

## Integrations

| System | Role |
| :--- | :--- |
| SignalHire REST API | **source of truth** for profile and contact data |
| SignalHire webhook | the asynchronous return path — the reason this product exists |
| StaffHive (via `WebhookAdapter`) | delivery destination — *adapter, not source of truth* |
| CATS / other ATS (via `McpAdapter`) | delivery destination — *adapter, not source of truth* |
| Mounted MCP servers (`SIGNALHIRE_MOUNTS`) | optional composition for **discovery only** — deliberately separate from delivery |

The `mount()` / delivery separation is an explicit decision worth preserving: you may want to
write to an ATS without also exposing its several hundred tools to whatever model is connected.

## Constraints

- **Vendor, non-negotiable:** answer callbacks within 10s; three retries then permanent
  discard; 100 identifiers per reveal; 600 elements/minute; 3 concurrent searches; search
  scroll cursors expire 15s after issue.
- **Technical, committed:** FastMCP `4.0.0b2` (a **prerelease**), `httpx2` not `httpx`, exact
  pins throughout to keep the prerelease surface to fastmcp alone. Python ≥3.10.
- **Operational:** the durable inbox requires a writable volume that survives restarts, with a
  single writer. On the chosen platform (FastMCP Cloud) this is not merely unenforced — it is
  **undocumented and probably unavailable**, and FastMCP's own storage guidance steers cloud and
  multi-instance deployments to Redis or DynamoDB. See W001-02.
- **Compliance / data residency:** **OPEN.** The system stores revealed personal contact data
  (emails, phone numbers) on disk with no stated retention or deletion policy. For a product
  handling PII this needs an answer.

## Environment and delivery

Profile requires `development · staging · production`. Today only development and production
exist, and promotion is local → production by hand.

**Provider is decided (2026-08-17): FastMCP Cloud (Prefect Horizon)** — the standing host for
every self-built MCP server, not a per-project choice. The DigitalOcean droplet path
(`deploy-to-droplet.sh`, `install.sh`) is superseded and should be retired rather than kept as a
divergent alternative. Staging becomes a Horizon branch preview (W001-03).

This is the right trade for a team that should not be hand-operating hosts, but it is not free:
Horizon is managed and autoscaled and redeploys on every push to `main`, while the durable inbox
is a local-filesystem store that is correct only for a single writer on a persistent volume.
Until W001-02 resolves that, the product's central promise — *never lose a billed callback* — is
not actually guaranteed on the chosen platform. That is the one thing in this brief that should
not be allowed to sit.

## Go to market

**OPEN** — entirely. No marketing surface, no positioning, no launch plan. Consistent with
"internal infrastructure", which is the most likely reading, but unconfirmed.

## Open questions

1. **Is this a StaffHive component or a standalone product?** Drives ICP, S17 target,
   commercial model and go-to-market. The single highest-value unanswered question.
2. **Is multi-tenancy a real requirement?** The BYOK/agency seam is fully built and tested
   (`Runtime.credential_for`, `TenantRegistry`, tenant strictly from verified claims) but only
   reachable under `AUTH_MODE=jwt`. Prior investigation found the intended consumer has no
   tenant concept at all. Speculative capability, or near-term need?
3. **What is the PII retention policy** for revealed contacts held in the inbox?
4. ~~**Which deployment target is canonical** — FastMCP Cloud or droplet?~~
   **Answered: FastMCP Cloud (Prefect Horizon)**, for all self-built MCP servers. The follow-on
   question it raises is now tracked as W001-02, not here.
5. **Does `fastmcp==4.0.0b2` stay pinned to a prerelease** for production, and what is the
   upgrade trigger? This gets sharper under FastMCP Cloud: the platform and the library ship from
   the same team, so a Horizon-side change can move under a pinned prerelease.
