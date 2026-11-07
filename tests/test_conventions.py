"""Code rules Ruff cannot check: frozen models (code.immutability), described operations
(agentic.openapi.described)."""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

import sreditor


def package_models() -> list[type[BaseModel]]:
    """Every Pydantic model defined in the sreditor package, once each."""
    models: list[type[BaseModel]] = []
    for module_info in pkgutil.walk_packages(sreditor.__path__, f"{sreditor.__name__}."):
        module = importlib.import_module(module_info.name)
        for candidate in vars(module).values():
            is_own_model = (
                inspect.isclass(candidate)
                and issubclass(candidate, BaseModel)
                and candidate.__module__ == module.__name__
            )
            if is_own_model and candidate not in models:
                models.append(candidate)
    return models


def test_package_models_are_all_frozen() -> None:
    mutable = [
        f"{model.__module__}.{model.__qualname__}"
        for model in package_models()
        if not model.model_config.get("frozen")
    ]
    assert mutable == []


def test_api_routes_declare_an_explicit_summary(app: FastAPI) -> None:
    # Without one, FastAPI derives the summary from the function name, which is not written
    # for an agent choosing a call.
    operations = [
        route for route in app.routes if isinstance(route, APIRoute) and route.include_in_schema
    ]
    unsummarized = [
        f"{sorted(route.methods)} {route.path}" for route in operations if not route.summary
    ]
    assert operations and unsummarized == []
