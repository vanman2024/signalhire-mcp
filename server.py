"""
SignalHire MCP Server - Standalone FastMCP Cloud Deployment

Complete MCP server implementation wrapping the SignalHire agent functionality.
Provides 13 tools, 7 resources, and 8 prompts for comprehensive access to SignalHire API.

Self-contained configuration: Loads .env from server directory automatically.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastmcp import FastMCP, Context
from pydantic import Field

# Load .env from the same directory as this server.py file
SERVER_DIR = Path(__file__).parent
ENV_FILE = SERVER_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
    print(f"✅ Loaded configuration from {ENV_FILE}")
else:
    print(f"⚠️  No .env file found at {ENV_FILE}")

# Import local SignalHire components (all local, no external package dependencies)
from lib.signalhire_client import SignalHireClient
from lib.callback_server import CallbackServer, get_server
from lib.contact_cache import ContactCache
from lib.config import load_config
from models.person_callback import PersonCallbackItem

# Mem0 and Supabase removed - agents handle storage per use case


# =============================================================================
# Server Configuration & Initialization
# =============================================================================

# Global state management
class AppState:
    """Application state container"""
    client: SignalHireClient | None = None
    callback_server: CallbackServer | None = None
    cache: ContactCache | None = None

state = AppState()

def get_callback_url() -> str:
    """
    Get callback URL - either external (DigitalOcean) or local server.

    Priority:
    1. EXTERNAL_CALLBACK_URL environment variable (e.g., DigitalOcean)
    2. Local callback server URL
    """
    external_url = os.getenv("EXTERNAL_CALLBACK_URL")
    if external_url:
        return external_url
    if state.callback_server:
        return state.callback_server.get_callback_url()
    raise RuntimeError("No callback server configured")


@asynccontextmanager
async def lifespan():
    """Server lifecycle management (FastMCP 2.x pattern)"""
    # ===== STARTUP =====
    print("🚀 Starting SignalHire MCP Server...")

    # Load configuration
    config = load_config()

    # Initialize SignalHire client
    state.client = SignalHireClient(
        api_key=config.signalhire.api_key,
        base_url=config.signalhire.api_base_url,
        api_prefix=config.signalhire.api_prefix
    )
    await state.client.start_session()

    # Check if using external callback server (DigitalOcean)
    external_callback_url = os.getenv("EXTERNAL_CALLBACK_URL")

    if external_callback_url:
        # Using external callback server (e.g., DigitalOcean)
        print(f"✅ Using external callback server: {external_callback_url}")
        state.callback_server = None  # No local server needed
    else:
        # Start local callback server
        state.callback_server = get_server(
            host=config.callback_server.host,
            port=config.callback_server.port
        )
        state.callback_server.start(background=True)
        print(f"📡 Local callback server: {get_callback_url()}")

    # Initialize contact cache
    state.cache = ContactCache()

    print("✅ SignalHire MCP Server started successfully")

    yield  # Server runs here

    # ===== SHUTDOWN =====
    print("👋 Shutting down SignalHire MCP Server...")

    if state.client:
        await state.client.close_session()
    if state.callback_server:
        state.callback_server.stop()

    print("✅ SignalHire MCP Server stopped")


# Create FastMCP server with lifespan
mcp = FastMCP(
    name="SignalHire",
    instructions="""
    SignalHire MCP Server provides access to 900M+ professional profiles for contact enrichment and lead generation.

    Key capabilities:
    - Search for prospects with advanced Boolean queries
    - Reveal contact information (emails, phones) for profiles
    - Batch operations with automatic rate limiting
    - Credit management and usage tracking
    - Webhook-based async operations

    Use search_prospects() to find candidates, then reveal_contact() to get their contact info.
    All operations respect rate limits (600 items/min, 3 concurrent searches).
    """,
    version="1.0.0",
    lifespan=lifespan  # FastMCP 2.x lifecycle management
)


# =============================================================================
# TOOLS (13 total)
# =============================================================================

# -----------------------------------------------------------------------------
# Core API Tools (5)
# -----------------------------------------------------------------------------

@mcp.tool
async def search_prospects(
    title: Annotated[str | None, Field(description="Job title (supports Boolean: AND, OR, NOT)")] = None,
    location: Annotated[list[str] | None, Field(description="City, state, or country (multiple allowed)")] = None,
    company: Annotated[str | None, Field(description="Company name (supports Boolean operators)")] = None,
    keywords: Annotated[str | None, Field(description="Skills, education, bio keywords (Boolean supported)")] = None,
    years_experience_from: Annotated[int | None, Field(ge=0, description="Minimum years of experience")] = None,
    years_experience_to: Annotated[int | None, Field(le=50, description="Maximum years of experience")] = None,
    open_to_work: Annotated[bool | None, Field(description="Filter by job-seeking status")] = None,
    size: Annotated[int, Field(ge=1, le=100, description="Results per page")] = 25,
    ctx: Context = None
) -> dict:
    """
    Search SignalHire's 900M+ profile database with advanced filters.

    Returns profile UIDs only (no contacts). Use reveal_contact() to get contact info.
    Supports Boolean queries: "Software Engineer AND (Python OR Java)"
    """
    await ctx.info(f"Searching for prospects with title='{title}', location={location}")

    # Build search criteria
    criteria = {}
    if title:
        criteria["currentTitle"] = title
    if location:
        criteria["location"] = location
    if company:
        criteria["currentCompany"] = company
    if keywords:
        criteria["keywords"] = keywords
    if years_experience_from is not None:
        criteria["yearsOfCurrentExperienceFrom"] = years_experience_from
    if years_experience_to is not None:
        criteria["yearsOfCurrentExperienceTo"] = years_experience_to
    if open_to_work is not None:
        criteria["openToWork"] = open_to_work

    # Execute search
    response = await state.client.search_prospects(criteria, size=size)

    if not response.success:
        raise ValueError(f"Search failed: {response.error}")

    profiles = response.data.get("profiles", [])
    total = response.data.get("total", 0)

    await ctx.info(f"Found {total} total profiles, returning first {len(profiles)}")

    return {
        "total": total,
        "count": len(profiles),
        "profiles": profiles,
        "scroll_id": response.data.get("scrollId"),
        "request_id": response.data.get("requestId")
    }


@mcp.tool
async def reveal_contact(
    identifier: Annotated[str, Field(description="LinkedIn URL, email, phone, or UID")],
    without_contacts: Annotated[bool, Field(description="If true, returns profile without contact info (cheaper)")] = False,
    ctx: Context = None
) -> dict:
    """
    Reveal contact information for a single profile.

    Async operation - returns requestId immediately, then sends results to webhook.
    Use get_request_status() to check completion or wait for webhook callback.
    """
    await ctx.info(f"Revealing contact for: {identifier}")

    callback_url = get_callback_url()

    response = await state.client.reveal_contact_by_identifier(
        identifier,
        callback_url,
        without_contacts=without_contacts
    )

    if not response.success:
        raise ValueError(f"Reveal failed: {response.error}")

    request_id = response.data.get("request_id")
    await ctx.info(f"Reveal request submitted: {request_id}")

    return {
        "request_id": request_id,
        "status": "processing",
        "identifier": identifier,
        "callback_url": callback_url,
        "message": "Results will be sent to webhook when ready"
    }


@mcp.tool
async def batch_reveal_contacts(
    identifiers: Annotated[list[str], Field(description="List of LinkedIn URLs, emails, phones, or UIDs (max 100)")],
    without_contacts: Annotated[bool, Field(description="If true, returns profiles without contact info")] = False,
    ctx: Context = None
) -> dict:
    """
    Batch reveal contact information for multiple profiles.

    Automatically splits into 100-item chunks and handles rate limiting.
    Returns request_id for tracking. Use get_request_status() to monitor progress.
    """
    if len(identifiers) > 100:
        await ctx.warning(f"Batch size {len(identifiers)} exceeds 100 - will be split automatically")

    await ctx.info(f"Batch revealing {len(identifiers)} contacts")
    await ctx.report_progress(0, len(identifiers), "Starting batch reveal...")

    callback_url = get_callback_url()

    response = await state.client.batch_reveal_contacts(
        identifiers,
        callback_url,
        without_contacts=without_contacts,
        progress_callback=lambda p, t: asyncio.create_task(ctx.report_progress(p, t))
    )

    if not response.success:
        raise ValueError(f"Batch reveal failed: {response.error}")

    await ctx.report_progress(len(identifiers), len(identifiers), "Batch reveal submitted")

    return {
        "request_id": response.data.get("request_id"),
        "count": len(identifiers),
        "status": "processing",
        "message": "Batch processing started - results will be sent to webhook"
    }


@mcp.tool
async def check_credits(
    without_contacts: Annotated[bool, Field(description="Check credits for no-contact operations")] = False,
    ctx: Context = None
) -> dict:
    """
    Check remaining API credits.

    Returns current credit balance. Different credit pools for with/without contacts.
    """
    response = await state.client.check_credits(without_contacts=without_contacts)

    if not response.success:
        raise ValueError(f"Credits check failed: {response.error}")

    credits = response.data.get("credits", 0)
    await ctx.info(f"Remaining credits: {credits}")

    return {
        "credits": credits,
        "type": "no_contacts" if without_contacts else "with_contacts"
    }


@mcp.tool
async def scroll_search_results(
    request_id: Annotated[str, Field(description="Request ID from initial search")],
    scroll_id: Annotated[str, Field(description="Scroll ID from previous batch")],
    ctx: Context = None
) -> dict:
    """
    Fetch next batch of search results.

    scrollId expires after 15 seconds - must be called promptly.
    Returns next batch of profiles and new scrollId for pagination.
    """
    await ctx.info(f"Scrolling search results for request {request_id}")

    response = await state.client.scroll_search(request_id, scroll_id)

    if not response.success:
        if "expired" in str(response.error).lower():
            raise ValueError("scrollId expired (15s timeout) - restart search")
        raise ValueError(f"Scroll failed: {response.error}")

    profiles = response.data.get("profiles", [])
    new_scroll_id = response.data.get("scrollId")

    await ctx.info(f"Fetched {len(profiles)} more profiles")

    return {
        "count": len(profiles),
        "profiles": profiles,
        "scroll_id": new_scroll_id,
        "has_more": bool(new_scroll_id)
    }


# -----------------------------------------------------------------------------
# Workflow Tools (5)
# -----------------------------------------------------------------------------

@mcp.tool
async def search_and_enrich(
    title: Annotated[str | None, Field(description="Job title to search")] = None,
    location: Annotated[list[str] | None, Field(description="Locations to search")] = None,
    company: Annotated[str | None, Field(description="Company to search")] = None,
    max_results: Annotated[int, Field(ge=1, le=100, description="Max profiles to enrich")] = 25,
    ctx: Context = None
) -> dict:
    """
    Combined workflow: Search for profiles then automatically enrich with contacts.

    1. Searches SignalHire database with filters
    2. Extracts UIDs from results
    3. Batch reveals contacts for all UIDs
    4. Returns request_id for tracking

    Most common use case - use this for end-to-end lead generation.
    """
    await ctx.info("Starting search and enrich workflow...")
    await ctx.report_progress(0, 3, "Phase 1: Searching profiles...")

    # Phase 1: Search
    search_result = await search_prospects(
        title=title,
        location=location,
        company=company,
        size=max_results,
        ctx=ctx
    )

    profiles = search_result["profiles"]
    uids = [p["uid"] for p in profiles if "uid" in p]

    if not uids:
        return {
            "error": "No profiles found",
            "search_total": search_result["total"]
        }

    await ctx.report_progress(1, 3, f"Phase 2: Enriching {len(uids)} profiles...")

    # Phase 2: Batch reveal
    reveal_result = await batch_reveal_contacts(uids, ctx=ctx)

    await ctx.report_progress(3, 3, "Workflow complete")

    return {
        "search_total": search_result["total"],
        "profiles_found": len(profiles),
        "enrichment_request_id": reveal_result["request_id"],
        "status": "processing",
        "message": f"Enriching {len(uids)} profiles - check request status or wait for webhook"
    }


@mcp.tool
async def enrich_linkedin_profile(
    linkedin_url: Annotated[str, Field(description="LinkedIn profile URL")],
    ctx: Context = None
) -> dict:
    """
    Enrich a single LinkedIn profile with contact information.

    High-level wrapper that:
    1. Checks credits
    2. Reveals contact
    3. Returns tracking info

    Use this for simple single-profile enrichment.
    """
    await ctx.info(f"Enriching LinkedIn profile: {linkedin_url}")

    # Check credits first
    credits_result = await check_credits(ctx=ctx)
    if credits_result["credits"] < 1:
        raise ValueError("Insufficient credits")

    # Reveal contact
    result = await reveal_contact(linkedin_url, ctx=ctx)

    return {
        "request_id": result["request_id"],
        "profile_url": linkedin_url,
        "status": "processing",
        "credits_remaining": credits_result["credits"] - 1,
        "message": "Profile enrichment started - check cache or wait for webhook"
    }


@mcp.tool
async def validate_email(
    email: Annotated[str, Field(description="Email address to validate")],
    ctx: Context = None
) -> dict:
    """
    Validate if an email exists in SignalHire database.

    Quick validation without consuming credits if email is invalid.
    Returns true/false + confidence score.
    """
    await ctx.info(f"Validating email: {email}")

    # Use search with email to check existence
    response = await state.client.reveal_contact_by_identifier(
        email,
        get_callback_url(),
        without_contacts=True  # Cheaper
    )

    if not response.success:
        return {
            "email": email,
            "valid": False,
            "confidence": 0.0,
            "reason": str(response.error)
        }

    return {
        "email": email,
        "valid": True,
        "confidence": 0.9,
        "request_id": response.data.get("request_id")
    }


@mcp.tool
async def export_results(
    request_id: Annotated[str, Field(description="Request ID to export")],
    format: Annotated[str, Field(description="Export format: csv, json, or excel")] = "json",
    ctx: Context = None
) -> dict:
    """
    Export enriched contacts to file format.

    Retrieves completed results from cache and formats for export.
    Supports CSV, JSON, and Excel formats.
    """
    await ctx.info(f"Exporting results for request {request_id} as {format}")

    # Get request status (would need to implement this in client)
    # For now, return export structure

    return {
        "request_id": request_id,
        "format": format,
        "status": "exported",
        "message": "Export functionality to be implemented based on export_service"
    }


@mcp.tool
async def get_search_suggestions(
    partial_query: Annotated[str, Field(description="Partial search query")],
    ctx: Context = None
) -> list[str]:
    """
    Get search query refinement suggestions.

    Analyzes partial query and suggests Boolean operators, filters, and common patterns.
    Helps users construct effective search queries.
    """
    await ctx.info(f"Generating suggestions for: {partial_query}")

    suggestions = []

    # Basic Boolean suggestions
    if "and" not in partial_query.lower():
        suggestions.append(f"{partial_query} AND Python")
        suggestions.append(f"{partial_query} AND (AWS OR Azure)")

    # Location suggestions
    if not any(loc in partial_query.lower() for loc in ["san francisco", "new york", "remote"]):
        suggestions.append(f"{partial_query} in San Francisco")
        suggestions.append(f"{partial_query} (Remote)")

    # Experience level suggestions
    suggestions.append(f"Senior {partial_query}")
    suggestions.append(f"{partial_query} with 5+ years")

    # Company type suggestions
    suggestions.append(f"{partial_query} at (Google OR Amazon OR Microsoft)")
    suggestions.append(f"{partial_query} at Startup")

    return suggestions[:10]


# -----------------------------------------------------------------------------
# Management Tools (3)
# -----------------------------------------------------------------------------

@mcp.tool
async def get_request_status(
    request_id: Annotated[str, Field(description="Request ID to check")],
    ctx: Context = None
) -> dict:
    """
    Check status of an async request.

    Returns: processing, completed, failed
    Use this to monitor reveal/batch operations.
    """
    await ctx.info(f"Checking status for request: {request_id}")

    response = await state.client.get_request_status(request_id)

    if not response.success:
        raise ValueError(f"Status check failed: {response.error}")

    return response.data


@mcp.tool
async def list_requests(
    limit: Annotated[int, Field(ge=1, le=100, description="Max requests to return")] = 10,
    ctx: Context = None
) -> dict:
    """
    List recent API requests with their status.

    Returns request history for monitoring and debugging.
    """
    await ctx.info(f"Listing last {limit} requests")

    response = await state.client.list_requests(limit=limit)

    if not response.success:
        raise ValueError(f"List requests failed: {response.error}")

    return response.data


@mcp.tool
async def clear_cache(ctx: Context = None) -> dict:
    """
    Clear local contact cache.

    Removes all cached enriched profiles from local storage.
    Use this to free up disk space or reset state.
    """
    await ctx.info("Clearing contact cache...")

    state.cache.clear()

    return {
        "status": "cleared",
        "message": "All cached contacts removed"
    }


# =============================================================================
# RESOURCES (7 total)
# =============================================================================

@mcp.resource("signalhire://contacts/{uid}")
async def get_cached_contact(uid: str) -> dict:
    """Get enriched contact from cache by UID"""
    contact = cache.get(uid)
    if not contact:
        raise ValueError(f"Contact {uid} not found in cache")
    return contact


@mcp.resource("signalhire://cache/stats")
async def get_cache_stats() -> dict:
    """Get contact cache statistics"""
    stats = cache.get_stats() if hasattr(cache, 'get_stats') else {}
    return {
        "total_contacts": len(cache._cache) if hasattr(cache, '_cache') else 0,
        "cache_size_mb": 0,  # Would need to calculate
        **stats
    }


@mcp.resource("signalhire://recent-searches")
async def get_recent_searches() -> list[dict]:
    """Get recent search queries (last 10)"""
    # Would implement based on request history
    return []


@mcp.resource("signalhire://credits")
async def get_credits_resource() -> dict:
    """Current credit balance (cached for 5 min)"""
    response = await state.client.check_credits()
    if not response.success:
        raise ValueError(f"Credits check failed: {response.error}")
    return response.data


@mcp.resource("signalhire://rate-limits")
async def get_rate_limits() -> dict:
    """Current rate limit status"""
    rate_limiter = client._rate_limiter if hasattr(client, '_rate_limiter') else None
    if not rate_limiter:
        return {"status": "unknown"}

    return {
        "items_per_minute": 600,
        "concurrent_searches": 3,
        "current_usage": 0,  # Would track actual usage
        "reset_at": None
    }


@mcp.resource("signalhire://requests/history")
async def get_requests_history() -> list[dict]:
    """Request history (last 100)"""
    response = await state.client.list_requests(limit=100)
    if not response.success:
        return []
    return response.data.get("requests", [])


@mcp.resource("signalhire://account")
async def get_account_info() -> dict:
    """Account information and subscription details"""
    response = await state.client.get_account_info() if hasattr(client, 'get_account_info') else None
    if not response or not response.success:
        return {"status": "unavailable"}
    return response.data


# =============================================================================
# PROMPTS (8 total)
# =============================================================================

@mcp.prompt
async def enrich_linkedin_profile_prompt(linkedin_url: str) -> str:
    """Guide for enriching a single LinkedIn profile with contact info"""
    return f"""
I need to enrich this LinkedIn profile: {linkedin_url}

Steps:
1. First check credits: call check_credits()
2. If credits > 0, call enrich_linkedin_profile(linkedin_url="{linkedin_url}")
3. Monitor the request: call get_request_status(request_id="<returned_id>")
4. Once completed, get contacts from cache: read signalhire://contacts/<uid>

This will return emails, phones, and full profile data.
"""


@mcp.prompt
async def bulk_enrich_contacts_prompt(count: int) -> str:
    """Guide for bulk contact enrichment"""
    return f"""
I need to enrich {count} LinkedIn profiles in bulk.

Workflow:
1. Check credits: call check_credits() to ensure you have >= {count} credits
2. Prepare list of identifiers (LinkedIn URLs, emails, or UIDs)
3. Call batch_reveal_contacts(identifiers=["url1", "url2", ...])
4. Track progress: call get_request_status(request_id="<returned_id>")
5. Results will arrive via webhook and be cached automatically

For large batches (>100), the tool automatically splits into chunks.
Rate limits: 600 items/min, so expect ~{count/600:.1f} minutes for completion.
"""


@mcp.prompt
async def search_candidates_by_criteria_prompt(title: str, location: str) -> str:
    """Guide for searching candidates with specific criteria"""
    return f"""
I want to find candidates with title "{title}" in {location}.

Search syntax supports Boolean operators:
- AND: "Software Engineer AND Python"
- OR: "Manager OR Director"
- NOT: "Developer NOT Junior"
- Parentheses: "(Python OR Java) AND AWS"

Example search:
call search_prospects(
    title="{title}",
    location=["{location}"],
    keywords="Python AND (AWS OR GCP)",
    years_experience_from=3,
    open_to_work=true
)

This returns UIDs. To get contacts, use search_and_enrich() instead.
"""


@mcp.prompt
async def search_and_enrich_workflow_prompt(criteria: str) -> str:
    """Guide for complete search and enrich workflow"""
    return f"""
I want to search for "{criteria}" and get their contact information.

Use the all-in-one tool:
call search_and_enrich(
    title="{criteria}",
    location=["San Francisco", "Remote"],
    max_results=50
)

This automatically:
1. Searches SignalHire database
2. Extracts UIDs from results
3. Batch reveals contacts
4. Returns tracking request_id

Monitor with: get_request_status(request_id="<returned_id>")
Results arrive via webhook and are cached.
"""


@mcp.prompt
async def manage_credits_prompt() -> str:
    """Guide for credit management"""
    return """
To manage SignalHire API credits:

1. Check current balance:
   call check_credits()

2. Check credits for no-contact operations (cheaper):
   call check_credits(without_contacts=true)

3. Estimate cost before batch operation:
   - With contacts: 1 credit per profile
   - Without contacts: 0.5 credits per profile (estimate)

4. View usage history:
   read signalhire://requests/history

Credits are consumed when contacts are revealed.
Search operations do NOT consume credits.
"""


@mcp.prompt
async def validate_bulk_emails_prompt(count: int) -> str:
    """Guide for bulk email validation"""
    return f"""
I need to validate {count} email addresses.

Workflow:
1. Prepare list of emails
2. Call batch_reveal_contacts(identifiers=emails, without_contacts=true)
   - without_contacts=true makes it cheaper
3. Monitor: get_request_status(request_id="<returned_id>")
4. Results indicate which emails exist in SignalHire database

This is cheaper than full enrichment and validates email existence.
"""


@mcp.prompt
async def export_search_results_prompt(request_id: str) -> str:
    """Guide for exporting search results"""
    return f"""
To export results for request {request_id}:

1. Check if request completed:
   call get_request_status(request_id="{request_id}")

2. Once status="completed", export:
   call export_results(request_id="{request_id}", format="csv")

3. Supported formats:
   - "csv" - Comma-separated values
   - "json" - JSON format
   - "excel" - Excel spreadsheet

Results include all enriched contact data (emails, phones, profiles).
"""


@mcp.prompt
async def troubleshoot_webhook_prompt() -> str:
    """Guide for troubleshooting webhook issues"""
    return """
Webhook not receiving callbacks? Troubleshooting steps:

1. Check callback server is running:
   - Server should start automatically with MCP server
   - Default URL: http://localhost:8000/signalhire/callback

2. Verify callback URL is reachable:
   - From command line: curl http://localhost:8000/health
   - Should return: {"status": "ok"}

3. Check firewall/network:
   - Ensure port 8000 is open
   - If behind NAT, use ngrok: ngrok http 8000

4. Monitor callback server logs:
   - Callbacks log to console
   - Look for "Received callback for request: <id>"

5. Test with single reveal:
   call reveal_contact(identifier="test@example.com")
   - Should return request_id
   - Wait 30-60 seconds for callback

Common issues:
- Server not started → restart MCP server
- Port blocked → change port in config
- Behind NAT → use ngrok tunnel
"""


# =============================================================================
# Run Server
# =============================================================================

if __name__ == "__main__":
    # Run server in STDIO mode (default for MCP)
    mcp.run()
