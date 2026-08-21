# signalhire-mcp — baseline

**Profile:** `integration-project` (Integration / MCP Server) — confirmed by detection signals: `fastmcp.json` manifest, `delivery/` provider adapters, `routes.py` webhook handler
**Baselined at commit:** `531e06c`
**Status:** draft — awaiting sign-off on target state and §Open questions
**Amended:** 2026-08-17 — deployment target decided (FastMCP Cloud / Prefect Horizon), and
architecture decided by [ADR-0001](../docs/adr/0001-mcp-surface-vs-integration-plumbing.md):
the receive path leaves this repo. **S09, S15 and S16 are re-targeted to `not_applicable`** —
retired by an ownership decision, not by a defect. Sections below describing the in-process
callback still describe the code at `531e06c`; they are accurate history, not the target.

## What this system is

A single-process FastMCP 4 server that puts SignalHire's contact-enrichment API behind an
MCP tool surface, and — the part that justifies its existence — survives SignalHire's
asynchronous, *billed* result delivery.

The domain constraint drives the whole design: SignalHire charges a credit the moment an
identifier is submitted and returns the answer later by POSTing to a callback URL. A lost
callback is not a retryable error, it is paid-for data destroyed. The previous architecture
ran that webhook receiver as a **separate process**, so results landed in memory the MCP
tools could not read, and no handler was ever registered against it — every payload was
logged and dropped. Commit `531e06c` collapses that into one ASGI app serving `/mcp/`,
`/signalhire/callback/{tenant}` and `/health` on one port, with a durable inbox between
receipt and delivery.

The code is well above average. Failure modes are reasoned about explicitly and the
reasoning is written down at the point of decision — why the callback returns 500 on a
persist failure (to earn back three more vendor attempts), why there is no response cache
(a cached `check_credits` tells an agent it has money it already spent), why an unset
`SIGNALHIRE_AUTH_MODE` is refused rather than defaulted (guessing wrong either exposes
credit-spending tools or breaks a correctly-fronted deploy). That quality is confined to
the application. Everything *around* it — CI, releases, environments — is absent or broken.

The honest one-line summary: **a mature service inside an immature delivery shell.**

## Runtimes

One runtime. A single Python 3.10+ process, `fastmcp.json` → `src/signalhire_mcp/app.py:mcp`,
serving HTTP on `:8000` (or stdio for local clients). It carries an in-process background
delivery worker and a file-backed inbox at `SIGNALHIRE_DATA_DIR`.

The promotion path is **local → production**. There is no staging. Deployment is
`deploy-to-droplet.sh` (305 lines of imperative shell) or `install.sh`, run by hand; there is
no container image and no IaC. Nothing declares the persistent volume that the durable inbox —
the entire point of the refactor — depends on.

## Deployment target — decided, and it costs something

**FastMCP Cloud (Prefect Horizon)** is the host, for this and every self-built MCP server
(decided 2026-08-17). That closes the open question, retires the droplet, and removes the worst
property of the current setup: promotion by hand from a laptop. Horizon builds from the GitHub
repo, reads `requirements.txt`/`pyproject.toml` — both present — and `fastmcp.json` already
declares the entrypoint `src/signalhire_mcp/app.py:mcp` and HTTP transport. The repo is close to
deployable as-is.

The decision also creates the project's sharpest architectural conflict, and it is better to
record it now than to discover it in production.

`inbox/store.py` says what it needs in its own docstring:

> *"this store is correct for one writer process. Running two replicas against the same
> directory would let both claim the same event. If this ever needs to scale horizontally,
> `InboxStore` is the one class to reimplement — nothing above it knows how storage works."*

Horizon is managed, advertises scaling, and **redeploys automatically on every push to `main`**.
Its documentation describes no persistent volume and no single-replica guarantee. FastMCP's own
[storage-backends guidance](https://gofastmcp.com/servers/storage-backends) rates file storage
*"not suitable for distributed deployments"* and recommends Redis or DynamoDB for cloud and
multi-instance deployments.

The consequence is worse than double-delivery. Both halves of the product go through this store:
the callback route **writes** to it, and `get_reveal_results` / `check_reveal_status` **read**
back from it. On a per-replica or ephemeral filesystem, a callback persisted by replica A is
invisible to a tool call served by replica B — which is precisely the defect `531e06c` was
written to fix, reintroduced through deployment topology instead of process topology. And
`data_dir` still defaults to `/var/lib/signalhire-mcp`, an absolute host path that on Horizon
would resolve inside a container the next deploy replaces.

### The second, sharper platform question

Horizon serves a deployed server at `https://<name>.fastmcp.app/mcp`. This product needs a
**second public path on that same host** — `POST /signalhire/callback/{tenant_id}` — because
that is the URL SignalHire posts billed results to. FastMCP's HTTP docs confirm custom routes
live in the same ASGI app and are independent of the MCP path, so the *application* serves it.
Whether Horizon's *ingress* routes anything other than `/mcp` is nowhere documented.

If it does not, the callback never arrives and FastMCP Cloud cannot host this product at all.
That is a bigger question than storage and it has the same answer path: ask Prefect. Both belong
in the same message.

A related framework behavior is worth recording because it is easy to miss and the code already
gets it right: **custom routes are never covered by the server's auth middleware**, by design —
*"the primary use case for custom routes is unauthenticated operational endpoints."* So
`AUTH_MODE` does nothing for the callback endpoint. The route authenticates itself
(`hmac.compare_digest` against `SIGNALHIRE_CALLBACK_SECRET`, checked *before* the body is read),
and `auth/verifier.py` refuses to start an HTTP deployment with no secret unless
`auth_mode=none`. That is the correct design for the documented behavior, not an accident.

The good news is that the seam is exactly where it should be. `InboxStore` is one 425-line class
with a narrow async interface, and nothing above it knows how storage works — so this is a
backend swap, not a re-architecture. That is **W001-02**, now the highest-severity open item in
the project.

One caveat on the evidence: Horizon's public docs are silent on volumes and replicas rather than
explicitly denying them. The absence of a documented guarantee is a strong reason not to depend
on one, but W001-02's first acceptance criterion is to get the answer in writing rather than
infer it.

## Domains and ownership

Five bounded contexts, cleanly separated in the package layout. This is a single-product
repository, so `v_ownership_conflicts` returns **0 rows**: no two modules write the same
entity. That is worth recording as a positive result, not an omission.

| Entity / table | Canonical owner | Also written by | Should be |
| :--- | :--- | :--- | :--- |
| `RevealRequest` | enrichment | — | unchanged |
| `IntegrationEvent` | callback-inbox | delivery (consumer, per-adapter status only) | unchanged |
| `NormalizedProfile` | delivery | — | unchanged |
| `SignalHireCredential` | tenancy | — | unchanged |
| `POST /signalhire/callback/{tenant_id}` | callback-inbox | — | unchanged |
| `GET /health` | platform | — | unchanged |

The boundary that matters and is currently held: the callback route **only** persists. It
never normalises, never calls an adapter, never touches the network — because the vendor
allows 10 seconds and then discards the payload permanently.

## Capability state

Profile applies 14 capabilities. Detail lives in the graph (`project_capabilities`).

| Status | Count | Notable |
| :--- | ---: | :--- |
| complete | 5 | S15 Event & Webhook, S16 Integration Framework, S40 Reliability, S06 Domain Model, S08 API Service Layer |
| partial | 6 | S38 Security, S44 Testing, S41 Observability, S42 Infrastructure, S09 Persistence, S17 Developer Platform |
| missing | 2 | **S43 Environments/CI/CD**, **S45 Releases & Versioning** |
| not applicable | 1 | S01 Product Surfaces — headless server, no human shell |

**Reusable today:** S06, S08, S15, S16, S40. A future feature needing durable async intake,
a retrying delivery adapter, or a new outbound destination should **EXTEND** these, not build
its own. Adding a delivery target requires no change to the receive path — that is the single
most valuable property this codebase has.

The capability scanner disagreed with several of these judgements and the scanner is wrong:
it reported S08 and S41 as `NONE` because it looks for HTTP frameworks and metrics libraries
and cannot see an MCP tool surface or a hand-rolled correlation-ID logger. Statuses above are
from reading the code and running the tests.

## Architecture state

Only buckets missing or partial are listed. Names are the canonical catalog names — an earlier
draft of this document paired several bucket IDs with the wrong names; the statuses were right,
the labels were not.

- **B19 Delivery System — `missing`** (required). No working pipeline, no staging, no release.
  See S43.
- **B18 Security, Privacy & Governance — `missing`** (required). The in-application controls are
  strong — auth mode refused rather than defaulted, callback secret enforced at startup, path
  traversal blocked — but nothing is enforced by a gate, no dependency scanning runs, and there is
  no stated retention policy for the PII this system stores.
- **B8 Interface Contracts & Versioning — `partial`** (required). The MCP contract is unusually
  well documented *in place* (server `INSTRUCTIONS` teach the credit model and the two-pool 402
  trap; bundled skills are served as MCP resources so a client learns the workflow without a local
  install) — but it is unversioned and unpublished, so no consumer can pin it or detect a break.
- **B14 Compute Topology — `partial`** (required). One process doing everything. ADR-0001 changes
  this deliberately: the receive path leaves, and the MCP server becomes a stateless surface.
- **B15 Integrations as Extension Modules — `partial`** (required). The `Adapter` protocol is a
  genuinely good extension seam — but under ADR-0001 the delivery adapters retire from this repo,
  so this bucket's target belongs to StaffHive.
- **B16 Observability & Auditability — `partial`** (standard). Structured logs, correlation IDs,
  and a `/health` that reports inbox backlog rather than mere liveness. No metrics, traces or
  alerting.
- **B17 Reliability, Backpressure & Disaster Recovery — `partial`** (standard). Retry with
  backoff and park-never-delete are real; there is no backpressure model and no recovery drill.

Holding at `complete`: **B5** Domain Model & Invariants, **B10** Async Compute & Messaging,
**B11** Consistency, Concurrency & Time, **B12** Failure & Repair Loops (the design centre of
this codebase), **B13** Data Systems Portfolio.

Testing is not a bucket — it is capability **S44**, assessed under §Capability state.

## Delivery foundation

Assessed against S43, strictly, as the skill requires.

| | State |
| :--- | :--- |
| One-command integrated startup | **absent** — not documented |
| CI | **broken** — see below |
| Staging | **absent** |
| Production | manual shell script |
| Rollback | **absent** |
| Release | **absent** |

The CI finding is the sharpest thing in this baseline. `.github/workflows/security-scan.yml`
is the repository's only workflow, and it invokes four scripts — `scan-secrets.sh`,
`scan-dependencies.sh`, `scan-owasp.sh`, `generate-security-report.sh` — **none of which exist
in the repo** (`scripts/` contains only `sanitize-env-for-docs.sh`). It has run exactly twice
in its entire history, in November 2025, and **both runs failed**. Nothing has run since.
Its triggers do not include `staging/**`, so the branch holding all current work would not be
covered even if it worked.

So: nine months of commits, including a full architectural refactor, with zero automated
verification of any kind.

## Target state

Keep the application architecture as-is — it is the asset. Bring the shell up to meet it.

1. **S43 → complete.** Real CI on every push and PR including staging branches; a persistent
   staging environment; promotion local → staging → production.
2. **S44 → complete.** The 86 tests gate every change; coverage measured; ruff enforced.
3. **S45 → complete.** Tag `v2.0.0`, changelog, release workflow.
   Horizon deploys from `main` on push, so versioning stops being bookkeeping and starts being
   the record of what is actually live.
4. **S09 → complete.** Resolve the single-writer question explicitly (see W001-02) rather than
   leaving durability dependent on an undocumented deployment assumption.
5. **S41/S42 → complete.** Wire FastMCP's OpenTelemetry support, export the parked-event count,
   alert on backlog growth; containerise and declare the data volume.
6. **S38 → complete.** Follows from S43 — the controls exist, they just need a gate.
7. **S17 → complete.** Version and publish the MCP contract.

S01 stays `not_applicable`. **M2 "First real path" is vacuous for this profile** — its sole
member is S01 — and should be dropped on sign-off rather than carried as a permanently empty
milestone.

## Gap → migration

Six Work Packages under `R001`. `W001-01` and `W001-02` are **READY**; the rest are **BLOCKED**
on dependencies, recorded honestly rather than optimistically marked ready.

| Work | Depends on | Why |
| :--- | :--- | :--- |
| **W001-01** Make CI real | — | Everything else is unverifiable without it. Delete or repair the broken workflow; run the 86 tests and ruff on push/PR incl. `staging/**`. |
| **W001-02** Make the durable inbox survive FastMCP Cloud | — | **Highest severity.** Horizon guarantees neither a persistent volume nor a single writer; `InboxStore` requires both. Confirm the platform guarantee in writing, then swap the backend behind the existing interface if it does not exist. |
| **W001-03** Stand up staging | W001-01, W001-02 | Horizon branch previews. Needs a green pipeline to deploy from, and needs the storage backend settled first — provisioning staging before W001-02 just builds the wrong thing twice. |
| **W001-04** Release and versioning | W001-01 | A release you cannot verify is not a release. |
| **W001-05** Observability beyond logs | W001-03 | Needs somewhere real to observe. |
| **W001-06** Publish the developer contract | W001-04 | Versioning must exist before a contract can be pinned to it. |

## Findings

11 recorded in `findings`; the ones that matter:

1. **CI is broken and has never passed** (error) — four missing scripts; both historical runs
   failed in Nov 2025.
2. **No test workflow exists** (error) — 86 tests gate nothing.
3. **The entire test suite was unrunnable** (error, **resolved during this baseline**) — `caio`
   (pulled in transitively: fastmcp → aiofile → caio) ships a top-level `tests` package into
   site-packages which shadowed the repo's, so every module died at collection with
   `ImportError: cannot import name 'SAMPLE_CALLBACK'`. Fixed by adding `tests/__init__.py`,
   making the repo's `tests` a regular package that takes precedence over the namespace
   portion. **86 tests now pass.** This is not a project defect — it is a dependency packaging
   wart — but it silently disabled the whole safety net.
4. **The FastMCP Cloud decision conflicts with the file-backed inbox** (error) — Horizon is
   managed, autoscaled and redeploys on every push to `main`, and documents no persistent volume;
   `InboxStore` is correct only for a single writer on one. Because tools read results back
   through the same store, an ephemeral or per-replica filesystem reintroduces the very defect
   `531e06c` fixed. See §Deployment target and W001-02.
5. **No release path** (warning) — 0 tags, 0 releases, no CHANGELOG, while `pyproject` and
   `__init__` both declare `2.0.0`.
6. **Untracked in-flight work** (warning) — `531e06c`, pushed to
   `origin/staging/fastmcp4-durable-callback`, has no issue and no PR. Its open blocker (a new
   SignalHire API key for the full reveal → callback → delivery test) is recorded nowhere.
7. `tools.py::register` is a single 404-line function (info) — the largest unit in the codebase.
8. 5 ruff violations, unenforced (info).
9. CRG graph stale at `4161ed0` with 0 communities (info) — postprocess never ran.
10. Zero issues and only the 9 stock GitHub labels (info) — no dimensional taxonomy.
11. `requirements.txt` mirrors pyproject pins but omits `ruff` (info).

## Open questions

- **Who consumes this server?** The code is deliberately consumer-neutral (`config.py`: "Nothing
  here knows about StaffHive") but StaffHive is the evident driver. Is this a StaffHive component
  or a standalone product? The answer changes S17's target.
- **Single-tenant or multi-tenant in practice?** The BYOK/agency seam is fully built
  (`Runtime.credential_for`, `TenantRegistry`, tenant-from-verified-claims) but only reachable
  under `AUTH_MODE=jwt`. Prior investigation found the intended consumer has no tenant concept
  at all. Is the multi-tenant path a real near-term requirement or speculative capability?
- ~~**Deployment target?**~~ **Answered 2026-08-17: FastMCP Cloud (Prefect Horizon)**, as the
  standing choice for all self-built MCP servers. The droplet path is superseded. This did not
  dissolve W001-02 — it made it concrete and raised its severity. See §Deployment target.
- **Is `M2` dropped?** Recommended, given S01 is `not_applicable`.
