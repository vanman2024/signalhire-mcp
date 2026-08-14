#!/bin/bash
# SignalHire MCP Server — local development install.
#
# For deployment use deploy-to-droplet.sh, which installs a systemd service.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo "SignalHire MCP Server — install"
echo "=================================="

# --- 1. Python --------------------------------------------------------------

echo ""
echo "1/4  Checking Python"

if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${RED}python3 not found${NC}" >&2
    exit 1
fi

PYTHON_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    echo -e "${GREEN}OK${NC} Python $PYTHON_VERSION"
else
    echo -e "${RED}Python $PYTHON_VERSION is too old; 3.10+ required${NC}" >&2
    exit 1
fi

# --- 2. Dependencies --------------------------------------------------------

echo ""
echo "2/4  Installing dependencies"

# --prerelease=allow / --pre is required: fastmcp 4 is a prerelease. The exact
# pins in pyproject.toml are what keep that permission from also pulling
# prerelease pydantic and httpx.
if command -v uv >/dev/null 2>&1; then
    uv pip install --prerelease=allow -e ".[dev]"
else
    echo -e "${YELLOW}uv not found; falling back to pip${NC}"
    python3 -m pip install --pre -e ".[dev]"
fi

echo -e "${GREEN}OK${NC} dependencies installed"

# --- 3. Environment ---------------------------------------------------------

echo ""
echo "3/4  Checking environment"

ENV_FILE="$SCRIPT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE"
    echo -e "${YELLOW}Created .env from .env.example — edit it before running.${NC}"
else
    echo -e "${GREEN}OK${NC} .env exists"
fi

if grep -q "^SIGNALHIRE_API_KEY=your_signalhire_api_key_here" "$ENV_FILE" 2>/dev/null; then
    echo -e "${YELLOW}SIGNALHIRE_API_KEY is still the placeholder${NC}"
fi

if ! grep -qE "^SIGNALHIRE_CALLBACK_SECRET=.+" "$ENV_FILE" 2>/dev/null \
   || grep -q "^SIGNALHIRE_CALLBACK_SECRET=generate_a_long_random_value" "$ENV_FILE" 2>/dev/null; then
    echo -e "${YELLOW}SIGNALHIRE_CALLBACK_SECRET is unset.${NC}"
    echo "  The server refuses to serve HTTP without it. Generate one with:"
    echo "    openssl rand -hex 32"
fi

# --- 4. Verify --------------------------------------------------------------

echo ""
echo "4/4  Verifying the server builds"

# Import the entrypoint the way fastmcp.json does. This is a real check now:
# create_server() registers every component at import time, so a server that
# imports is a server with tools.
SIGNALHIRE_TRANSPORT=stdio python3 -c "
from signalhire_mcp.server import create_server
from signalhire_mcp.config import Settings
import tempfile, asyncio

settings = Settings(transport='stdio', data_dir=tempfile.mkdtemp(), api_key='verify')
mcp = create_server(settings)

async def check():
    from fastmcp import Client
    async with Client(mcp) as c:
        tools = await c.list_tools()
        prompts = await c.list_prompts()
        resources = await c.list_resources()
        print(f'  {len(tools)} tools, {len(resources)} resources, {len(prompts)} prompts')

asyncio.run(check())
" || { echo -e "${RED}Server failed to build${NC}" >&2; exit 1; }

echo -e "${GREEN}OK${NC} server builds"

cat <<'SUMMARY'

==================================
Installation complete.

Next:

  1. Edit .env — at minimum SIGNALHIRE_API_KEY.

  2. Reveals need a PUBLIC HTTPS callback URL. SignalHire bills at submission
     and delivers results by webhook, so without one every reveal is paid for
     and then discarded. For local work:

         ngrok http 8000
         # then set SIGNALHIRE_PUBLIC_BASE_URL to the https:// URL

  3. Run it:

         fastmcp run fastmcp.json
         # or
         python -m signalhire_mcp.app

  4. See the whole surface:

         fastmcp inspect fastmcp.json --format fastmcp

  5. Run the tests:

         pytest tests/ -q

  6. Install into Claude Code:

         fastmcp install claude-code fastmcp.json

Deployment is deploy-to-droplet.sh. It installs ONE service that serves /mcp/
and the callback endpoint on the same port.
==================================
SUMMARY
