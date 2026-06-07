"""Deterministic tools used by LightRoute agents."""

__all__ = ["ToolRegistry", "ToolSpec", "run_poi_search", "run_route_planning"]


def __getattr__(name):
    if name == "ToolRegistry":
        from tools.registry import ToolRegistry

        return ToolRegistry
    if name == "ToolSpec":
        from tools.registry import ToolSpec

        return ToolSpec
    if name == "run_poi_search":
        from tools.poi_search_tool import run_poi_search

        return run_poi_search
    if name == "run_route_planning":
        from tools.route_planning_tool import run_route_planning

        return run_route_planning
    raise AttributeError(name)
