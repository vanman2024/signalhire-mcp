"""
Test fixtures for SignalHire MCP Server
Provides mcp_client fixture using FastMCP in-memory testing pattern
"""
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set up test environment variables before any imports
os.environ["SIGNALHIRE_API_KEY"] = "test_api_key_202.R6cmAKCaf7FHPPstzfP2Vnh5XOBo"
os.environ["EXTERNAL_CALLBACK_URL"] = "https://test-callback.example.com/webhook"

from fastmcp import FastMCP


@pytest.fixture
async def mcp_client():
    """
    Create in-memory MCP client for testing.

    Uses FastMCP's client context manager to create a test client
    without starting an actual HTTP server.
    """
    # Import server after env vars are set
    import server

    # Mock external dependencies to prevent actual API calls
    with patch.object(server.state, 'client') as mock_client:
        # Configure mock client
        mock_client.search_prospects = AsyncMock(return_value=Mock(
            success=True,
            data={"profiles": [], "total": 0, "scrollId": "test_scroll", "requestId": "test_req"}
        ))
        mock_client.reveal_contact_by_identifier = AsyncMock(return_value=Mock(
            success=True,
            data={"request_id": "test_reveal_req"}
        ))
        mock_client.batch_reveal_contacts = AsyncMock(return_value=Mock(
            success=True,
            data={"request_id": "test_batch_req"}
        ))
        mock_client.check_credits = AsyncMock(return_value=Mock(
            success=True,
            data={"credits": 1000}
        ))
        mock_client.scroll_search = AsyncMock(return_value=Mock(
            success=True,
            data={"profiles": [], "scrollId": "next_scroll"}
        ))
        mock_client.get_request_status = AsyncMock(return_value=Mock(
            success=True,
            data={"status": "completed", "request_id": "test_req"}
        ))
        mock_client.list_requests = AsyncMock(return_value=Mock(
            success=True,
            data={"requests": []}
        ))
        mock_client.start_session = AsyncMock()
        mock_client.close_session = AsyncMock()

        # Mock cache
        with patch.object(server.state, 'cache') as mock_cache:
            mock_cache.get = Mock(return_value=None)
            mock_cache.clear = Mock()
            mock_cache._cache = {}

            # Create test client using FastMCP's client context
            async with server.mcp.client() as client:
                yield client


@pytest.fixture
def mock_signalhire_api():
    """
    Mock SignalHire API responses for testing actual API integration.
    Use this fixture when you need to simulate different API responses.
    """
    return {
        "search_success": {
            "profiles": [
                {
                    "uid": "test_uid_1",
                    "name": "John Doe",
                    "title": "Software Engineer",
                    "company": "Tech Corp"
                }
            ],
            "total": 1,
            "scrollId": "scroll_123",
            "requestId": "req_123"
        },
        "reveal_success": {
            "request_id": "reveal_123",
            "status": "processing"
        },
        "credits_response": {
            "credits": 500
        },
        "error_response": {
            "error": "API Error",
            "message": "Invalid request"
        }
    }


@pytest.fixture
def sample_profiles():
    """Sample profile data for testing"""
    return [
        {
            "uid": "uid_001",
            "name": "Alice Smith",
            "title": "Senior Software Engineer",
            "company": "Tech Inc",
            "location": "San Francisco, CA",
            "linkedin_url": "https://linkedin.com/in/alice-smith"
        },
        {
            "uid": "uid_002",
            "name": "Bob Johnson",
            "title": "Engineering Manager",
            "company": "StartupCo",
            "location": "New York, NY",
            "linkedin_url": "https://linkedin.com/in/bob-johnson"
        },
        {
            "uid": "uid_003",
            "name": "Carol White",
            "title": "DevOps Engineer",
            "company": "CloudTech",
            "location": "Remote",
            "linkedin_url": "https://linkedin.com/in/carol-white"
        }
    ]


@pytest.fixture
def sample_identifiers():
    """Sample identifiers for batch operations"""
    return [
        "https://linkedin.com/in/test-user-1",
        "https://linkedin.com/in/test-user-2",
        "test@example.com",
        "uid_12345"
    ]
