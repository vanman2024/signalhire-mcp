# External Callback Setup — removed

**This document described an architecture that did not work. It has been
removed rather than updated, because following it reintroduces the bug.**

## What it used to say

Run the MCP server in one place, run a separate callback server somewhere else
(a DigitalOcean droplet, `start-callback.py`), and point `EXTERNAL_CALLBACK_URL`
at the second one.

## Why that could never work

The MCP server and the callback receiver were **different processes with no
shared state**. SignalHire delivered results into the receiver's memory, and
the process holding the tools had no way to read them. In practice it was worse
than that: no handler was ever registered against the receiver, so every
payload was logged and dropped.

`start-callback.py` was also never in this repository — it existed only on the
droplet — so the deployed behaviour could not be read, reviewed, or reproduced
from source.

## What replaces it

One process serves everything on one port:

```
/mcp/                            the MCP endpoint
/signalhire/callback/{tenant}    SignalHire's webhook
/health                          liveness + inbox depth
```

The callback is a FastMCP custom route in the same ASGI app as `/mcp/`, sharing
the same durable store. See [the README](../../README.md).

## Migrating

| Old | New |
|---|---|
| `EXTERNAL_CALLBACK_URL=https://other-host/signalhire/callback` | `SIGNALHIRE_PUBLIC_BASE_URL=https://this-host` |
| (none) | `SIGNALHIRE_CALLBACK_SECRET` — required on HTTP |
| (none) | `SIGNALHIRE_AUTH_MODE` — required on HTTP |
| (none) | `SIGNALHIRE_DATA_DIR` — where callbacks are persisted |
| `requirements.callback.txt` | removed; there is one dependency set |
| `start-callback.py` | removed; `python -m signalhire_mcp.app` |

The callback path now carries the tenant and the shared secret:

```
https://<host>/signalhire/callback/<tenant>?secret=<secret>
```

`Settings.callback_url()` builds it, and the reveal tools pass it to SignalHire
automatically. You do not construct it by hand.

## Decommissioning the old droplet service

Once the new service is verified:

```bash
doctl compute ssh signalhire-callback --ssh-command '
  sudo systemctl disable --now signalhire-callback
  sudo rm -f /etc/systemd/system/signalhire-callback.service
  sudo systemctl daemon-reload
'
```

Leave the old `/opt/signalhire-callback` directory until you have confirmed the
new endpoint is receiving callbacks — it is the only copy of `start-callback.py`.
