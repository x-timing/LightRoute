# LightRoute Tools

`tools/` is the source of truth for deterministic callable tools used by the
LightRoute orchestration pipeline.

The main orchestration path calls these tools through `ToolRegistry` directly.
POI recall and route planning should not be implemented as `.claude` skills.

## Registered Tools

Tools are registered through `tools.registry.ToolRegistry`.

```python
from tools.registry import ToolRegistry

registry = ToolRegistry()
print(registry.list_tools())
```

Default tools:

- `poi_search`: retrieve and enrich POI candidates for route planning.
- `route_planning`: generate ranked route options from POI candidates and route
  preferences.

Tool names are canonicalized, so both hyphen and underscore forms work:

- `poi-search` and `poi_search`
- `route-planning` and `route_planning`

## Common Call Contract

All default tools accept:

- `context`: orchestration context for the current user request.
- `previous_results`: previous agent/tool outputs in execution order.

Example:

```python
result = await registry.run_tool(
    "route-planning",
    context={
        "original_query": "北京短途游，从国贸出发，6小时，景点和餐饮兼顾",
        "duration": "6小时",
        "route_preference": {
            "route_type": "balanced",
            "route_type_label": "景点和餐饮兼顾",
            "weights": {
                "sightseeing": 0.38,
                "food": 0.32,
                "experience": 0.10,
                "travel_efficiency": 0.10,
                "queue": 0.05,
                "cost": 0.05,
            },
        },
    },
    previous_results=[],
)
```

## `poi_search`

Implementation:

- `tools/poi_search_tool.py`
- Main entry: `run_poi_search(context=None, previous_results=None, ...)`

Responsibilities:

- Resolve city, anchor area, route preference, and start location from context
  and previous results.
- Build recall specs and phrase banks for route preferences.
- Query AMap through `services.amap_client.AmapClient`.
- Enrich and normalize POIs with UGC signals through
  `services.ugc_service.UGCService`.
- Return categorized candidates for route planning.

Typical output fields:

```json
{
  "poi_search_complete": true,
  "city": "北京",
  "anchor_hint": "国贸附近",
  "start_location": {
    "name": "国贸",
    "location": {"lng": 116.461841, "lat": 39.909104}
  },
  "pois": [],
  "poi_counts": {
    "dining": 5,
    "culture_entertainment": 5,
    "other": 0
  },
  "route_preference": {},
  "weights": {},
  "warnings": []
}
```

## `route_planning`

Implementation:

- `tools/route_planning_tool.py`
- Main entry: `run_route_planning(context=None, previous_results=None)`

Responsibilities:

- Read POI candidates from `poi_search` output.
- Resolve route preference and time budget.
- Resolve start location and include the start-to-first-POI leg.
- Infer composition policy:
  - `food`: dining-focused route.
  - `sightseeing`: culture/photo-focused route.
  - `balanced`: dining and culture/entertainment coverage.
  - `food_only`: strict dining-only route; may return no route with a warning
    if dining candidates are insufficient.
- Score, rank, and format route options.
- Return route metrics for CLI and itinerary rendering.

Typical output fields:

```json
{
  "route_planning_complete": true,
  "route_options": [
    {
      "option_id": "route_1",
      "title": "均衡短途路线",
      "profile": "balanced",
      "start_location": {},
      "poi_sequence": [],
      "pois": [],
      "legs": [],
      "schedule": [],
      "estimated_duration_min": 315.7,
      "total_distance_m": 8049,
      "score": 9.8,
      "metrics": {},
      "constraints": {},
      "warnings": []
    }
  ],
  "composition_policy": {},
  "route_preference": {},
  "weights": {},
  "warnings": [],
  "diagnostics": {}
}
```

## Ownership Rules

- Add POI recall behavior in `tools/poi_search_tool.py`.
- Add route scoring or composition behavior in `tools/route_planning_tool.py`.
- Main orchestration must call `poi_search` and `route_planning` through
  `ToolRegistry`, not through lazy-loaded `.claude` skills.
- Do not add new `.claude/skills/poi-search` or
  `.claude/skills/route-planning` agent logic. Add behavior in the tool modules
  and cover it with no-pytest tool checks.
- Add no-pytest checks for tool behavior in `tests/test_tool_registry.py` or a
  focused tool test module.

## Validation

Run the no-pytest tool checks:

```bash
python tests/test_tool_registry.py
```

Run the broader Beijing short-trip checks:

```bash
python tests/run_beijing_short_trip_checks.py
```
