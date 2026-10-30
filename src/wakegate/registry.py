"""Layer types that can be named in a config.

A config never imports code. A type is available only if it is registered here, by a package the
user installed (or by the application embedding wakegate). That keeps the property that reading a
YAML file cannot cause anything to execute.
"""

from __future__ import annotations

from typing import Any

from .layers import Layer

_TYPES: dict[str, type[Layer]] = {}


def register_layer_type(name: str, cls: type[Layer]) -> None:
    """Make `type: <name>` usable in a `layers:` section. Only classifiers can be registered for now."""
    if getattr(cls, "kind", None) != "classifier":
        raise ValueError(f'layer type "{name}": only classifier layers can be registered (got kind "{getattr(cls, "kind", None)}")')
    _TYPES[name] = cls


def unregister_layer_type(name: str) -> None:
    _TYPES.pop(name, None)


def registered_types() -> list[str]:
    return sorted(_TYPES)


def layer_type(name: str) -> type[Layer]:
    if name not in _TYPES:
        known = ", ".join(registered_types()) or "none installed"
        raise ValueError(f'unknown layer type "{name}"; registered types: {known}')
    return _TYPES[name]


def create_layer(type_name: str, name: str, params: dict[str, Any], mode: str) -> Layer:
    layer = layer_type(type_name).create(name, params)
    layer.name = name
    layer.mode = mode  # type: ignore[assignment]
    return layer
