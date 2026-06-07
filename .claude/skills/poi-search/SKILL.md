---
name: poi-search
description: Use this skill during route planning after event collection. It retrieves real POI candidates from AMap and enriches them with local UGC signals such as queue risk, tags, and tips.
---

# POI Search

Retrieve structured POI candidates for route planning.

## When to Use

- Use after `event_collection` for route planning requests.
- Requires a destination city or area.
- Retrieves at least two categories:
  - dining
  - culture_entertainment

## Agent

- `PoiSearchAgent`
- The Agent calls:
  - `services.amap_client.AmapClient`
  - `services.ugc_service.UGCService`

## Output

Strict JSON:

```json
{
  "poi_search_complete": true,
  "city": "杭州",
  "pois": [],
  "poi_counts": {
    "dining": 5,
    "culture_entertainment": 5,
    "other": 0
  },
  "warnings": []
}
```

## Notes

- Do not hardcode AMap keys. Use `AMAP_KEY` from the environment.
- If AMap returns an error, return a JSON error with a clear message.

