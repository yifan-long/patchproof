"""FastAPI HTTP layer: app assembly, shared helpers and route routers."""

from ..config import settings
from .app import app, lifespan

__all__ = ["app", "lifespan", "settings"]
