"""Server construction, auth posture, skills, and normalisation."""

from __future__ import annotations

import pytest

from signalhire_mcp.auth.verifier import (
    InsecureDeploymentError,
    auth_is_enforced,
    build_auth_provider,
)
from signalhire_mcp.config import AuthMode, Settings, Transport
from signalhire_mcp.credentials.base import SignalHireCredential
from signalhire_mcp.delivery.normalize import normalize_payload
from tests.conftest import SAMPLE_CALLBACK


# --- auth posture ---------------------------------------------------------


def test_http_without_an_auth_mode_refuses_to_start():
    """The old deployment sat on 0.0.0.0:8000 with no auth at all.

    Here that is fatal rather than a warning, because every reveal tool spends
    money — an open endpoint is a way to bill the account, not just read it.
    """
    settings = Settings(transport=Transport.HTTP, auth_mode=None, auth_jwks_uri="")

    with pytest.raises(InsecureDeploymentError) as exc:
        build_auth_provider(settings)

    assert "SIGNALHIRE_AUTH_MODE" in str(exc.value)


def test_stdio_does_not_need_an_auth_mode():
    """stdio is already private to the process that launched it."""
    settings = Settings(transport=Transport.STDIO, auth_mode=None)
    assert build_auth_provider(settings) is None


def test_platform_mode_delegates_without_verifying():
    settings = Settings(transport=Transport.HTTP, auth_mode=AuthMode.PLATFORM)
    assert build_auth_provider(settings) is None
    assert auth_is_enforced(settings) is False, "no claims means no per-tenant routing"


def test_jwt_mode_requires_a_jwks_uri():
    settings = Settings(transport=Transport.HTTP, auth_mode=AuthMode.JWT, auth_jwks_uri="")
    with pytest.raises(InsecureDeploymentError):
        build_auth_provider(settings)


def test_jwks_uri_alone_implies_jwt_mode():
    settings = Settings(
        transport=Transport.HTTP,
        auth_mode=None,
        auth_jwks_uri="https://issuer.test/.well-known/jwks.json",
    )
    assert auth_is_enforced(settings) is True


# --- credential leak guards -----------------------------------------------


def test_credential_never_stringifies_its_secret():
    credential = SignalHireCredential(api_key="super-secret", tenant_id="acme")

    assert "super-secret" not in repr(credential)
    assert "super-secret" not in str(credential)
    assert "super-secret" not in f"{credential}"
    assert "super-secret" not in str(credential.redacted())
    # And the real key still works where it is meant to.
    assert credential.auth_header() == {"apikey": "super-secret"}


# --- configuration --------------------------------------------------------


def test_unresolved_placeholder_is_treated_as_unset():
    """FastMCP leaves `${VAR}` intact when the variable is unset.

    There is no `${VAR:-default}` syntax — confirmed by running the tooling, not
    assumed — so every optional variable declared in fastmcp.json arrives as its
    own name when unset. Reading that as the empty string is what lets the real
    check ("SIGNALHIRE_API_KEY is not set") produce the error, instead of a
    pydantic message about a value the operator never wrote.
    """
    settings = Settings(
        api_key="${SIGNALHIRE_API_KEY}",
        callback_secret="${SIGNALHIRE_CALLBACK_SECRET}",
        public_base_url="${SIGNALHIRE_PUBLIC_BASE_URL}",
    )
    assert settings.api_key == ""
    assert settings.callback_secret == ""
    assert settings.public_base_url == ""


def test_unresolved_auth_mode_is_unset_not_invalid():
    settings = Settings(transport=Transport.STDIO, auth_mode="${SIGNALHIRE_AUTH_MODE}")
    assert settings.auth_mode is None


def test_tenant_config_placeholder_is_treated_as_unconfigured(monkeypatch):
    """The same placeholder problem, on the delivery config path."""
    from signalhire_mcp.delivery.registry import TenantRegistry

    monkeypatch.setenv("SIGNALHIRE_TENANTS", "${SIGNALHIRE_TENANTS}")
    registry = TenantRegistry.from_env()
    assert registry.tenant_ids() == ["default"]


# --- callback endpoint protection -----------------------------------------


def test_http_without_a_callback_secret_refuses_to_start():
    """The callback route cannot sit behind MCP auth, so the secret is the only control.

    Without it anyone reaching the URL can POST forged enrichment results that
    get stored and written into every configured destination.
    """
    from signalhire_mcp.auth.verifier import require_callback_protection

    settings = Settings(
        transport=Transport.HTTP, auth_mode=AuthMode.PLATFORM, callback_secret=""
    )

    with pytest.raises(InsecureDeploymentError) as exc:
        require_callback_protection(settings)

    assert "SIGNALHIRE_CALLBACK_SECRET" in str(exc.value)
    assert "openssl rand" in str(exc.value)


def test_dev_mode_tolerates_a_missing_callback_secret():
    """auth_mode=none already declares this a development deployment."""
    from signalhire_mcp.auth.verifier import require_callback_protection

    settings = Settings(
        transport=Transport.HTTP, auth_mode=AuthMode.NONE, callback_secret=""
    )
    require_callback_protection(settings)  # warns, does not raise


def test_stdio_does_not_need_a_callback_secret():
    from signalhire_mcp.auth.verifier import require_callback_protection

    settings = Settings(transport=Transport.STDIO, callback_secret="")
    require_callback_protection(settings)


def test_callback_url_without_a_public_base_url_is_a_clear_error():
    settings = Settings(public_base_url="")
    with pytest.raises(RuntimeError) as exc:
        settings.callback_url("default")
    assert "billed and then discarded" in str(exc.value)


def test_callback_url_carries_tenant_and_secret():
    settings = Settings(public_base_url="https://sh.test/", callback_secret="abc")
    url = settings.callback_url("acme")
    assert url == "https://sh.test/signalhire/callback/acme?secret=abc"


# --- skills ---------------------------------------------------------------


async def test_bundled_skill_is_served_as_a_resource(client):
    """The instructions travel with the server they describe."""
    resources = await client.list_resources()
    uris = {str(r.uri) for r in resources}
    skill_uris = {u for u in uris if u.startswith("skill://")}
    assert skill_uris, f"no skill resources found; got {sorted(uris)}"
    assert any("signalhire-enrichment" in u for u in skill_uris)


async def test_skill_content_is_readable(client):
    result = await client.read_resource("skill://signalhire-enrichment/SKILL.md")
    text = result[0].text
    assert "Credits are spent when you submit" in text
    assert "without_contacts" in text


# --- normalisation --------------------------------------------------------


def test_normalize_picks_preferred_contacts():
    profiles = normalize_payload(SAMPLE_CALLBACK)
    jane = profiles[0]

    assert jane.succeeded is True
    assert jane.primary_email == "jane@gmail.com", "personal preferred over work"
    assert jane.primary_phone == "+1 403-555-0100"
    assert sorted(jane.emails) == ["jane.work@acme.com", "jane@gmail.com"]
    assert jane.linkedin_url == "https://www.linkedin.com/in/jane-welder"
    assert jane.location == "Calgary, Alberta, Canada"
    assert jane.current_company == "Acme Fabrication"
    assert len(jane.experience) == 2
    assert jane.education[0].university == "SAIT"


def test_normalize_keeps_unsuccessful_items():
    """A downstream system needs to know a reveal resolved to nothing.

    Dropping them means it retries a person SignalHire does not have.
    """
    profiles = normalize_payload(SAMPLE_CALLBACK)
    statuses = {p.status for p in profiles}
    assert statuses == {"success", "failed", "credits_are_over"}


@pytest.mark.parametrize("payload", [None, {}, "text", 7, [None, 3, "x"], [{}]])
def test_normalize_never_raises(payload):
    """It runs inside the worker; an exception here would park a good event."""
    assert isinstance(normalize_payload(payload), list)


def test_normalize_falls_back_to_the_submitted_linkedin_url():
    payload = [
        {
            "item": "https://www.linkedin.com/in/no-social",
            "status": "success",
            "candidate": {"uid": "x" * 32, "fullName": "No Social"},
        }
    ]
    profile = normalize_payload(payload)[0]
    assert profile.linkedin_url == "https://www.linkedin.com/in/no-social"
