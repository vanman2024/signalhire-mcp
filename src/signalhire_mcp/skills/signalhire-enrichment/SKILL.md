---
name: signalhire-enrichment
description: How to enrich contacts through the SignalHire MCP server without wasting credits or losing results. Use when revealing emails/phones for candidates, searching the SignalHire database, or diagnosing a reveal that never produced data.
---

# SignalHire Enrichment

This skill describes the **SignalHire MCP server**, not the raw REST API. The
server wraps SignalHire and adds durable handling of its asynchronous results.

## The one thing that governs everything

**Credits are spent when you submit, not when results arrive.**

SignalHire's reveal API is asynchronous: you submit identifiers, get a
`request_id` back immediately, and the actual data is POSTed to the server
minutes later. The credit is already gone by the time you see the `request_id`.

The consequence that matters: **never re-submit an identifier because the
answer has not appeared yet.** That spends a second credit for data you have
already paid for and which is already on its way. If results seem missing, use
the diagnosis steps below — do not resubmit.

## Workflow

### Enriching people you already have

```
check_credits()                              # confirm the balance covers the batch
batch_reveal_contacts(identifiers=[...])     # up to 100; returns request_id
get_request_status(request_id)               # poll — has the callback landed?
get_enrichment_result(request_id)            # the revealed profiles
```

`identifiers` accepts LinkedIn URLs, email addresses, phone numbers, or
32-character SignalHire UIDs, mixed freely.

### Finding people first

```
search_prospects(title=..., location=[...], exclude_revealed=true)
  -> review the results, decide who is worth revealing
batch_reveal_contacts(identifiers=[uid, uid, ...])
```

`search_prospects` is **free**. It returns profile summaries and UIDs but no
contact details. This is the step where spend is controlled: search widely,
reveal narrowly.

`exclude_revealed=true` skips people already revealed on this account, which is
the cheapest possible optimisation — it stops you paying twice for the same
person.

Boolean operators work in `title`, `company`, and `keywords`:

```
title="(Welder OR Fabricator) AND NOT Apprentice"
keywords="GMAW AND \"pressure vessel\""
```

If a search returns a `scroll_id`, call `scroll_search_results()` **immediately**
— the cursor expires 15 seconds after it is issued. If it expires, restart the
search rather than trying to resume.

## The two credit pools

SignalHire keeps two independent balances, and confusing them produces the most
common error against this API.

| Pool | Used by | Typical balance |
|---|---|---|
| `with_contacts` | normal reveals (the default) | your actual credits |
| `without_contacts` | `without_contacts=true` | **zero on most accounts** |

A `402 Insufficient credits` almost never means the account is empty. It nearly
always means a request went to the `without_contacts` pool, which was never
purchased, while the main balance sits untouched.

**Leave `without_contacts` at its default of false.** Only set it true after
`check_credits(without_contacts=true)` returns a non-zero number.

Read `signalhire://credits` to see both balances at once.

## Understanding request status

`get_request_status(request_id)` returns one of:

| status | meaning | what to do |
|---|---|---|
| `awaiting_callback` | Submitted; SignalHire has not POSTed yet | Wait. Normal for a few minutes on a large batch. |
| `callback_received` | Data arrived and is stored | Read it with `get_enrichment_result()` |
| `unknown` | No record of this request | It predates this deployment, or the submission never completed |

When `callback_received`, each event also carries a `delivery_state` describing
whether the data reached downstream systems:

- `delivered` — every configured destination accepted it.
- `partial` / `received` — some destination is still failing; retries continue.
- `failed` — retries exhausted. **The data is still stored and readable.**
  Nothing is lost; only the onward delivery stopped.

Delivery state is separate from whether *you* can read the data. Even a `failed`
event returns full profiles from `get_enrichment_result()`.

## When results never arrive

Work down this list before considering a resubmit:

1. `get_request_status(request_id)` — distinguish "not yet" from "broken".
2. `list_failed_deliveries()` — parked callbacks, with the failing destination
   named and the last error attached.
3. `retry_delivery(event_id)` — requeue, once the underlying problem is fixed.
4. Read `signalhire://inbox/stats` — queue depth and whether the delivery worker
   is running.

If SignalHire never called back at all, the cause is on the network path, not in
your request. Usual suspects: a callback URL that is not publicly reachable, a
TLS certificate the vendor rejects, or a mismatched callback secret returning
401. SignalHire retries three times and then discards permanently, so the
server's log is the place to look for rejected callbacks.

## Limits worth remembering

| Limit | Value |
|---|---|
| Identifiers per reveal | 100 |
| Person API throughput | 600 elements/minute |
| Concurrent searches | 3 |
| Scroll cursor lifetime | 15 seconds |
| Callback acknowledgement | 10 seconds, 3 retries, then discarded |

`batch_reveal_contacts` rejects batches over 100 rather than splitting them
silently — a partial submission that looks complete is how people lose track of
what they paid for. Split explicitly.

## What this server does not do

It does not decide recruiting policy. It reveals contacts and hands the result
to whatever destinations are configured for the tenant. Matching candidates to
jobs, enrolling anyone in outreach, and deciding who to contact belong to the
system that owns that workflow.
