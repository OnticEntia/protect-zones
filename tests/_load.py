"""Loads the integration's modules by file path, without Home Assistant.

zones.py needs nothing. stream.py imports aiohttp, homeassistant and (lazily)
uiprotect: load_stream() puts small stand-ins for those in sys.modules first.
"""

from __future__ import annotations

import enum
import importlib.util
from pathlib import Path
import sys
import types

COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "protect_zones"
sys.dont_write_bytecode = True


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"protect_zones_{name}", COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_zones():
    return _load("zones")


class WSMsgType(enum.Enum):
    TEXT = 1
    BINARY = 2
    CLOSED = 257
    ERROR = 258


class ConfigEntryState(enum.Enum):
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"


class FakeWSPacket:
    """Stand-in for uiprotect's WSPacket: the "bytes" are (action, data), or anything else to fail."""

    def __init__(self, raw) -> None:
        action, data = raw  # raises on a packet that "can't be decoded"
        self.action_frame = types.SimpleNamespace(data=action)
        self.data_frame = types.SimpleNamespace(data=data)


def load_stream():
    """(stream module, issues) — issues lists the repair issues now raised, as (domain, id)."""
    issues: list[tuple[str, str]] = []

    def module(name: str, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    def create_issue(hass, domain, issue_id, **kwargs):
        assert kwargs["translation_key"] == issue_id
        if (domain, issue_id) not in issues:
            issues.append((domain, issue_id))

    def delete_issue(hass, domain, issue_id):
        if (domain, issue_id) in issues:
            issues.remove((domain, issue_id))

    module("aiohttp", WSMsgType=WSMsgType)
    ir = module(
        "homeassistant.helpers.issue_registry",
        IssueSeverity=enum.Enum("IssueSeverity", "ERROR WARNING"),
        async_create_issue=create_issue,
        async_delete_issue=delete_issue,
    )
    module("homeassistant")
    module("homeassistant.config_entries", ConfigEntryState=ConfigEntryState)
    module("homeassistant.core", HomeAssistant=object, callback=lambda func: func)
    module("homeassistant.helpers", issue_registry=ir)
    module("uiprotect")
    module("uiprotect.data")
    module("uiprotect.data.websocket", WSPacket=FakeWSPacket)
    return _load("stream"), issues
