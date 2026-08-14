"""SignalHire MCP server.

A FastMCP adapter for the SignalHire Person/Search API with a durable callback
inbox and pluggable delivery adapters.

The design problem this package exists to solve: SignalHire's reveal API is
fully asynchronous. You POST identifiers, get a `requestId`, and results arrive
later as a webhook. Credits are spent at submit time, so a callback that is
received and dropped is money burned for nothing. SignalHire retries a failed
callback three times and then discards it permanently.

Therefore the callback path persists before it acknowledges, and delivery to
downstream systems is a separate, retryable step.
"""

__version__ = "2.0.0"
