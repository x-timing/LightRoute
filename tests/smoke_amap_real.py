#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Optional real AMap smoke test.

Run only when you want to verify the remote server can access AMap:
  export AMAP_KEY="your-web-service-key"
  python tests/smoke_amap_real.py
"""
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from services.amap_client import AmapAPIError, AmapClient
from services.ugc_service import UGCService


def main():
    if not os.getenv("AMAP_KEY"):
        print("SKIPPED: AMAP_KEY is not set.")
        return 0

    client = AmapClient(timeout=8)
    ugc = UGCService()

    try:
        dining = client.search_text("西湖 美食", city="杭州", types=["050000"], offset=5)
        culture = client.search_text("西湖 景点 文化", city="杭州", types=["110000", "140000"], offset=5)
    except AmapAPIError as exc:
        print(f"AMAP SMOKE FAILED: {exc}")
        print("If you see USERKEY_PLAT_NOMATCH (10009), use an AMap Web Service key.")
        print("If you see USER_KEY_RECYCLED (10013), create a new key because the current key was deleted or recycled.")
        return 1

    pois = ugc.enrich_pois([*dining, *culture], visit_hour=12)
    print(f"AMAP SMOKE OK: fetched {len(pois)} POIs")
    for poi in pois[:8]:
        queue = poi.get("ugc", {}).get("queue_level", "unknown")
        print(f"- {poi.get('name')} | {poi.get('category')} | {poi.get('location')} | queue={queue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
