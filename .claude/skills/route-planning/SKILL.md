---
name: route-planning
description: Use this skill after POI search for route planning. It generates ranked route options by optimizing POI quality, travel time, queue risk, budget, and user preferences.
---

# Route Planning

Generate deterministic route options from structured POI candidates.

## When to Use

- Use after `poi-search`.
- Requires POI candidates with `category` and `location`.
- Produces route options for final itinerary writing.

## Agent

- `RoutePlanningAgent`
- The Agent calls:
  - `planning.route_optimizer.RouteOptimizer`
  - `planning.scoring`

## Output

Strict JSON:

```json
{
  "route_planning_complete": true,
  "route_options": [],
  "diagnostics": {},
  "warnings": []
}
```

## Constraints

- At least 3 POIs.
- Must include dining and culture_entertainment.
- Consider time budget, distance, queue risk, money budget, and preferences.

