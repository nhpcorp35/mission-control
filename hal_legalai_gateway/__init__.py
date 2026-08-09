"""HAL LegalAI Gateway — thin authenticated interface consolidation (Phase 2).

Downstream Bridge, Storage, Mission Control, and artifact retrieval remain
separately deployed, testable, and replaceable. This package consolidates the
operator-facing MCP interface (namespaced tools + registry + health), not
business logic.
"""

__version__ = "0.2.0"
