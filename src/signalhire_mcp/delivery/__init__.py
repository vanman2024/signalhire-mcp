"""Pluggable delivery of enrichment results to downstream systems."""

from signalhire_mcp.delivery.base import Adapter, DeliveryResult
from signalhire_mcp.delivery.mcp_adapter import McpAdapter
from signalhire_mcp.delivery.normalize import (
    Contact,
    Education,
    Employment,
    RevealedProfile,
    normalize_payload,
)
from signalhire_mcp.delivery.registry import (
    TenantConfigError,
    TenantRegistry,
    UnknownTenantError,
)
from signalhire_mcp.delivery.webhook import WebhookAdapter
from signalhire_mcp.delivery.worker import DeliveryWorker

__all__ = [
    "Adapter",
    "Contact",
    "DeliveryResult",
    "DeliveryWorker",
    "Education",
    "Employment",
    "McpAdapter",
    "RevealedProfile",
    "TenantConfigError",
    "TenantRegistry",
    "UnknownTenantError",
    "WebhookAdapter",
    "normalize_payload",
]
