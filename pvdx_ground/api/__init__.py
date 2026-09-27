"""HTTP API of the cloud telemetry service (FastAPI). ``create_app`` builds it; ``pvdx-serve`` hosts it."""

from pvdx_ground.api.app import create_app

__all__ = ["create_app"]
