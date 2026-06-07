"""Tool registry for deterministic LightRoute tools."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional


ToolCallable = Callable[..., Any]


@dataclass(frozen=True)
class ToolSpec:
    """Public metadata for a callable deterministic tool."""

    name: str
    description: str
    callable: ToolCallable
    required_inputs: tuple[str, ...] = ("context", "previous_results")


class ToolRegistry:
    """Register and execute deterministic tools by canonical name."""

    def __init__(
        self,
        tools: Optional[Mapping[str, ToolCallable]] = None,
        tool_kwargs: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> None:
        default_specs: Dict[str, ToolSpec] = {
            "poi_search": ToolSpec(
                name="poi_search",
                description="Retrieve and enrich POI candidates for route planning.",
                callable=_run_poi_search,
            ),
            "route_planning": ToolSpec(
                name="route_planning",
                description="Generate ranked route options from POI candidates and route preferences.",
                callable=_run_route_planning,
            ),
        }
        if tools:
            for name, tool in tools.items():
                canonical = self.canonical_name(name)
                default_specs[canonical] = ToolSpec(
                    name=canonical,
                    description=f"Custom registered tool: {canonical}",
                    callable=tool,
                )

        self._tool_specs = default_specs
        self._tool_kwargs = {
            self.canonical_name(name): dict(kwargs)
            for name, kwargs in (tool_kwargs or {}).items()
        }

    def list_tools(self) -> Dict[str, Dict[str, Any]]:
        """Return serializable metadata for registered tools."""
        return {
            name: {
                "name": spec.name,
                "description": spec.description,
                "required_inputs": list(spec.required_inputs),
            }
            for name, spec in sorted(self._tool_specs.items())
        }

    def get_tool_spec(self, name: str) -> ToolSpec:
        """Return the registered tool spec by canonical name."""
        canonical = self.canonical_name(name)
        if canonical not in self._tool_specs:
            raise KeyError(f"Tool not registered: {name}")
        return self._tool_specs[canonical]

    def has_tool(self, name: str) -> bool:
        return self.canonical_name(name) in self._tool_specs

    async def run_tool(
        self,
        name: str,
        context: Dict[str, Any],
        previous_results: list,
    ) -> Dict[str, Any]:
        canonical = self.canonical_name(name)
        if canonical not in self._tool_specs:
            raise KeyError(f"Tool not registered: {name}")

        tool = self._tool_specs[canonical].callable
        kwargs = self._tool_kwargs.get(canonical, {})
        result = tool(context=context, previous_results=previous_results, **kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    @staticmethod
    def canonical_name(name: str) -> str:
        mapping = {
            "poi-search": "poi_search",
            "route-planning": "route_planning",
        }
        normalized = str(name or "").strip()
        return mapping.get(normalized, normalized.replace("-", "_"))


def _run_poi_search(**kwargs):
    from tools.poi_search_tool import run_poi_search

    return run_poi_search(**kwargs)


def _run_route_planning(**kwargs):
    from tools.route_planning_tool import run_route_planning

    return run_route_planning(**kwargs)
