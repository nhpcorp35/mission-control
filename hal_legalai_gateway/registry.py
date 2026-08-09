"""Machine-readable registry loader and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hal_legalai_gateway.forwarding import ToolBinding

REGISTRY_FILENAME = "registry.json"
REQUIRED_NAMESPACES = frozenset({"case", "storage", "mission"})
REQUIRED_SERVICES = frozenset(
    {"bridge", "storage", "mission_control", "artifacts"}
)
REQUIRED_GATEWAY_TOOLS = frozenset(
    {
        "case.get_artifact",
        "storage.archive_feedback",
        "storage.archive_review_packet",
        "storage.verify_archive",
        "mission.submit",
        "mission.status",
    }
)


@dataclass(frozen=True)
class DownstreamService:
    """One independently deployable downstream dependency."""

    key: str
    service_id: str
    display_name: str
    base_url_env: str
    health_path: str
    default_base_url: str | None
    notes: str = ""


@dataclass(frozen=True)
class NamespaceMapping:
    """Logical namespace → downstream service + intended tools."""

    name: str
    description: str
    downstream_service: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class ToolRoute:
    """Optional per-tool override to a specific downstream."""

    tool: str
    namespace: str
    downstream_service: str
    notes: str = ""


@dataclass(frozen=True)
class GatewayRegistry:
    """Validated gateway registry (Phase 2 namespaced tool surface)."""

    version: int
    description: str
    services: dict[str, DownstreamService]
    namespaces: dict[str, NamespaceMapping]
    tool_routes: tuple[ToolRoute, ...]
    tool_bindings: tuple[ToolBinding, ...]

    def service_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self.services))

    def namespace_for_tool(self, tool: str) -> str | None:
        for binding in self.tool_bindings:
            if binding.gateway_tool == tool:
                return binding.namespace
        for route in self.tool_routes:
            if route.tool == tool:
                return route.namespace
        for name, mapping in self.namespaces.items():
            if tool in mapping.tools:
                return name
        return None

    def downstream_for_tool(self, tool: str) -> str | None:
        for binding in self.tool_bindings:
            if binding.gateway_tool == tool:
                return binding.downstream_service
        for route in self.tool_routes:
            if route.tool == tool:
                return route.downstream_service
        for mapping in self.namespaces.values():
            if tool in mapping.tools:
                return mapping.downstream_service
        return None

    def downstream_tool_for_gateway_tool(self, tool: str) -> str | None:
        for binding in self.tool_bindings:
            if binding.gateway_tool == tool:
                return binding.downstream_tool
        return None

    def as_public_dict(self) -> dict[str, Any]:
        """Serialize without resolving secrets or live URLs."""
        return {
            "version": self.version,
            "description": self.description,
            "services": {
                key: {
                    "service_id": svc.service_id,
                    "display_name": svc.display_name,
                    "base_url_env": svc.base_url_env,
                    "health_path": svc.health_path,
                    "default_base_url": svc.default_base_url,
                    "notes": svc.notes,
                }
                for key, svc in sorted(self.services.items())
            },
            "namespaces": {
                name: {
                    "description": ns.description,
                    "downstream_service": ns.downstream_service,
                    "tools": list(ns.tools),
                }
                for name, ns in sorted(self.namespaces.items())
            },
            "tool_routes": [
                {
                    "tool": route.tool,
                    "namespace": route.namespace,
                    "downstream_service": route.downstream_service,
                    "notes": route.notes,
                }
                for route in self.tool_routes
            ],
            "tool_bindings": [
                {
                    "tool": binding.gateway_tool,
                    "namespace": binding.namespace,
                    "downstream_service": binding.downstream_service,
                    "downstream_tool": binding.downstream_tool,
                    "notes": binding.notes,
                }
                for binding in self.tool_bindings
            ],
        }


def default_registry_path() -> Path:
    return Path(__file__).resolve().parent / REGISTRY_FILENAME


def load_registry_document(path: Path | None = None) -> dict[str, Any]:
    registry_path = path or default_registry_path()
    try:
        raw = registry_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"gateway registry missing or unreadable: {registry_path}"
        ) from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gateway registry is not valid JSON: {registry_path}"
        ) from exc
    if not isinstance(document, dict):
        raise RuntimeError("gateway registry root must be a JSON object")
    return document


def _require_str(obj: dict[str, Any], key: str, *, context: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def parse_registry(document: dict[str, Any]) -> GatewayRegistry:
    """Validate and normalize a registry document."""
    version = document.get("version")
    if not isinstance(version, int) or version < 1:
        raise RuntimeError("registry.version must be a positive integer")

    description = document.get("description", "")
    if description is not None and not isinstance(description, str):
        raise RuntimeError("registry.description must be a string")

    services_raw = document.get("services")
    if not isinstance(services_raw, dict) or not services_raw:
        raise RuntimeError("registry.services must be a non-empty object")

    missing_services = REQUIRED_SERVICES - set(services_raw)
    if missing_services:
        raise RuntimeError(
            "registry.services missing required keys: "
            + ", ".join(sorted(missing_services))
        )

    services: dict[str, DownstreamService] = {}
    for key, raw in services_raw.items():
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("registry.services keys must be non-empty strings")
        if not isinstance(raw, dict):
            raise RuntimeError(f"registry.services.{key} must be an object")
        default_url = raw.get("default_base_url")
        if default_url is not None and not isinstance(default_url, str):
            raise RuntimeError(
                f"registry.services.{key}.default_base_url must be a string or null"
            )
        health_path = _require_str(raw, "health_path", context=f"services.{key}")
        if not health_path.startswith("/"):
            raise RuntimeError(
                f"registry.services.{key}.health_path must start with '/'"
            )
        services[key] = DownstreamService(
            key=key,
            service_id=_require_str(raw, "service_id", context=f"services.{key}"),
            display_name=_require_str(
                raw, "display_name", context=f"services.{key}"
            ),
            base_url_env=_require_str(
                raw, "base_url_env", context=f"services.{key}"
            ),
            health_path=health_path,
            default_base_url=(default_url.strip().rstrip("/") if default_url else None),
            notes=str(raw.get("notes") or ""),
        )

    namespaces_raw = document.get("namespaces")
    if not isinstance(namespaces_raw, dict) or not namespaces_raw:
        raise RuntimeError("registry.namespaces must be a non-empty object")

    missing_namespaces = REQUIRED_NAMESPACES - set(namespaces_raw)
    if missing_namespaces:
        raise RuntimeError(
            "registry.namespaces missing required keys: "
            + ", ".join(sorted(missing_namespaces))
        )

    namespaces: dict[str, NamespaceMapping] = {}
    for name, raw in namespaces_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(
                "registry.namespaces keys must be non-empty strings"
            )
        if not isinstance(raw, dict):
            raise RuntimeError(f"registry.namespaces.{name} must be an object")
        downstream = _require_str(
            raw, "downstream_service", context=f"namespaces.{name}"
        )
        if downstream not in services:
            raise RuntimeError(
                f"namespaces.{name}.downstream_service "
                f"'{downstream}' is not defined in registry.services"
            )
        tools_raw = raw.get("tools")
        if not isinstance(tools_raw, list) or not tools_raw:
            raise RuntimeError(
                f"namespaces.{name}.tools must be a non-empty array"
            )
        tools: list[str] = []
        for tool in tools_raw:
            if not isinstance(tool, str) or not tool.strip():
                raise RuntimeError(
                    f"namespaces.{name}.tools entries must be non-empty strings"
                )
            tools.append(tool.strip())
        namespaces[name] = NamespaceMapping(
            name=name,
            description=str(raw.get("description") or ""),
            downstream_service=downstream,
            tools=tuple(tools),
        )

    routes_raw = document.get("tool_routes") or []
    if not isinstance(routes_raw, list):
        raise RuntimeError("registry.tool_routes must be an array when present")

    tool_routes: list[ToolRoute] = []
    for index, raw in enumerate(routes_raw):
        context = f"tool_routes[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"{context} must be an object")
        namespace = _require_str(raw, "namespace", context=context)
        if namespace not in namespaces:
            raise RuntimeError(
                f"{context}.namespace '{namespace}' is not defined"
            )
        downstream = _require_str(raw, "downstream_service", context=context)
        if downstream not in services:
            raise RuntimeError(
                f"{context}.downstream_service '{downstream}' is not defined"
            )
        tool_routes.append(
            ToolRoute(
                tool=_require_str(raw, "tool", context=context),
                namespace=namespace,
                downstream_service=downstream,
                notes=str(raw.get("notes") or ""),
            )
        )

    bindings_raw = document.get("tool_bindings") or []
    if not isinstance(bindings_raw, list):
        raise RuntimeError("registry.tool_bindings must be an array when present")

    tool_bindings: list[ToolBinding] = []
    for index, raw in enumerate(bindings_raw):
        context = f"tool_bindings[{index}]"
        if not isinstance(raw, dict):
            raise RuntimeError(f"{context} must be an object")
        namespace = _require_str(raw, "namespace", context=context)
        if namespace not in namespaces:
            raise RuntimeError(
                f"{context}.namespace '{namespace}' is not defined"
            )
        downstream = _require_str(raw, "downstream_service", context=context)
        if downstream not in services:
            raise RuntimeError(
                f"{context}.downstream_service '{downstream}' is not defined"
            )
        tool_bindings.append(
            ToolBinding(
                gateway_tool=_require_str(raw, "tool", context=context),
                namespace=namespace,
                downstream_service=downstream,
                downstream_tool=_require_str(
                    raw, "downstream_tool", context=context
                ),
                notes=str(raw.get("notes") or ""),
                description=str(raw.get("description") or ""),
            )
        )

    if tool_bindings:
        present = {binding.gateway_tool for binding in tool_bindings}
        missing_required = REQUIRED_GATEWAY_TOOLS - present
        if missing_required:
            raise RuntimeError(
                "registry.tool_bindings missing required gateway tools: "
                + ", ".join(sorted(missing_required))
            )

    return GatewayRegistry(
        version=version,
        description=str(description or ""),
        services=services,
        namespaces=namespaces,
        tool_routes=tuple(tool_routes),
        tool_bindings=tuple(tool_bindings),
    )


def load_registry(path: Path | None = None) -> GatewayRegistry:
    return parse_registry(load_registry_document(path))
