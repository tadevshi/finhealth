"""Response schema for the ``/health`` endpoint.

The schema carries three pieces of information:

* ``status`` — service-level liveness marker. Always ``"ok"`` when the
  service is up.
* ``database`` — backend connection status. ``"ok"`` if the most
  recent health probe round-tripped against the database, otherwise
  ``"error"``.
* ``version`` — the running application's :data:`app.__version__`,
  useful for client-side compatibility checks and log correlation.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app import __version__


class HealthResponse(BaseModel):
    """Shape of the JSON returned by ``GET /health``."""

    status: Literal["ok"] = Field(
        description="Service status. Always 'ok' when the endpoint responds.",
    )
    database: Literal["ok", "error"] = Field(
        default="ok",
        description="Database connection status from the health probe.",
    )
    version: str = Field(
        default=__version__,
        description="Running application version (mirrors app.__version__).",
    )
