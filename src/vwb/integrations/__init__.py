"""Host-controlled integrations; agents reach remote tools only through Registry."""

from .service import IntegrationService
from .store import IntegrationError

__all__ = ["IntegrationService", "IntegrationError"]
