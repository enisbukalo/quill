"""FastAPI dependency aliases.

`Annotated` aliases rather than a `request.app.state` lookup in every handler: the dependency is
declared in the signature, so it type-checks, appears in the OpenAPI schema, and a test can
override it through the normal FastAPI mechanism.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from quill_api.services import Services
from quill_api.settings import Settings


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def get_settings(services: ServicesDep) -> Settings:
    return services.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
