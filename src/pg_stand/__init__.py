"""Declarative PostgreSQL stands managed through the Docker SDK."""

from pg_stand.config import MANAGED_POSTGRES_PARAMETERS

__version__ = "0.2.3"

__all__ = ["MANAGED_POSTGRES_PARAMETERS", "__version__"]
