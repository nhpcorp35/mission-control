"""HAL LegalAI Gateway — thin interface consolidation (Phase 1).

Downstream Bridge, Storage, Mission Control, and artifact retrieval remain
separately deployed, testable, and replaceable. This package consolidates the
operator-facing interface (registry + health), not business logic.
"""

__version__ = "0.1.0"
