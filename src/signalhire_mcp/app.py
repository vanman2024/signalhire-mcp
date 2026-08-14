"""Canonical entrypoint.

`fastmcp.json` points here. Components are registered at import time by
`create_server()`, so importing this module always yields a fully populated
server — unlike the previous top-level `server.py`, whose tools were bound to
state that only existed once the lifespan had run.
"""

from __future__ import annotations

from signalhire_mcp.config import Transport, load_settings
from signalhire_mcp.server import create_server

settings = load_settings()
mcp = create_server(settings)


def main() -> None:
    """Run the server on the configured transport.

    Under HTTP this one process serves `/mcp/`, `/signalhire/callback/{tenant}`
    and `/health` on a single port. There is no second process to deploy, which
    is the whole point — the previous split is why callbacks never reached the
    tools.
    """
    if settings.transport is Transport.HTTP:
        mcp.run(
            transport="http",
            host=settings.host,
            port=settings.port,
            path=settings.mcp_path,
            allowed_hosts=settings.allowed_hosts_list(),
            allowed_origins=settings.allowed_origins_list(),
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
