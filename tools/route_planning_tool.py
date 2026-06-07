"""
Deterministic route planning tool.

This module is intentionally algorithmic: it does not call an LLM. It can use
an injected map-route client for real road costs, with a deterministic
Haversine fallback, and produces stable ``route_options`` for later itinerary
writing.
"""
from __future__ import annotations

import math
import re
from itertools import combinations, product
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.opening_hours import opening_status

ROUTE_WEIGHT_KEYS = (
    "sightseeing",
    "food",
    "experience",
    "travel_efficiency",
    "queue",
    "cost",
)

DEFAULT_WEIGHTS = {
    "sightseeing": 0.38,
    "food": 0.32,
    "experience": 0.10,
    "travel_efficiency": 0.10,
    "queue": 0.05,
    "cost": 0.05,
}

DEFAULT_VISIT_DURATION_MIN = {
    "dining": 50,
    "culture_entertainment": 55,
    "other": 40,
}


def _legacy_mojibake_variants(*terms: str, rounds: int = 1) -> Tuple[str, ...]:
    """Generate legacy UTF-8-as-GBK mojibake terms without storing them in source."""
    variants: List[str] = []
    for term in terms:
        current = str(term or "")
        for _ in range(max(1, rounds)):
            try:
                current = current.encode("utf-8").decode("gbk", errors="replace")
            except UnicodeError:
                break
            for variant in (current, current.replace("\ufffd", "")):
                if variant and variant not in variants:
                    variants.append(variant)
    return tuple(variants)


PROFILE_TITLES = {
    "balanced": "景点和餐饮兼顾路线",
    "food_focused": "美食优先路线",
    "culture_focused": "景点优先路线",
    "sightseeing_focused": "景点打卡路线",
    "citywalk": "轻松 Citywalk 路线",
    "efficient": "效率优先路线",
    "low_queue": "少排队路线",
}

LOCAL_FOOD_QUERY_TERMS = (
    "\u672c\u5730\u7279\u8272",
    "\u8001\u5317\u4eac",
    "\u5317\u4eac\u7279\u8272",
    "\u4eac\u5473",
    "\u8001\u5b57\u53f7",
    "\u5c0f\u5403",
    "\u7279\u8272\u9910\u996e",
    "\u7279\u8272\u7f8e\u98df",
)
CUISINE_REQUEST_TERMS = {
    "川菜": ("川菜", "四川菜", "四川餐厅", "麻辣", "水煮鱼", "四川火锅", "重庆火锅"),
    "粤菜": ("粤菜", "广东菜", "广府菜", "早茶", "点心"),
    "湘菜": ("湘菜", "湖南菜", "剁椒", "小炒黄牛肉"),
    "火锅": ("火锅", "重庆火锅", "四川火锅"),
}
BEIJING_LOCAL_FOOD_TERMS = (
    "\u5317\u4eac\u83dc",
    "\u4eac\u5473",
    "\u8001\u5317\u4eac",
    "\u8001\u5b57\u53f7",
    "\u70b8\u9171\u9762",
    "\u5364\u716e",
    "\u70e4\u9e2d",
    "\u6dae\u8089",
    "\u94dc\u9505",
    "\u7206\u809a",
    "\u8c46\u6c41",
    "\u7126\u5708",
    "\u9a74\u6253\u6eda",
    "\u8c4c\u8c46\u9ec4",
    "\u5c0f\u5403",
)
GENERIC_WESTERN_DINING_TERMS = (
    "bagel",
    "coffee",
    "brunch",
    "贝果",
    "咖啡",
    "烘焙",
    "面包",
    "西餐",
    "轻食",
    "汉堡",
    "披萨",
    *_legacy_mojibake_variants("贝果", "咖啡", "烘焙", "面包", "西餐", "轻食", "汉堡", "披萨"),
)

SOCIAL_FIT_TERMS = (
    "聊天",
    "安静",
    "约会",
    "闺蜜",
    "朋友",
    "包间",
    "亲子",
    "拍照",
    *_legacy_mojibake_variants("聊天", "安静", "约会", "闺蜜", "朋友", "包间", "亲子", "拍照"),
)

QUEUE_REQUEST_TERMS = (
    "不想排队",
    "不排队",
    "少排队",
    "别排队",
    "低排队",
    "不用排队",
    *_legacy_mojibake_variants("不想排队", "不排队", "少排队", "别排队", "低排队", "不用排队", rounds=2),
)

BEIJING_CITY_TERMS = ("北京", *_legacy_mojibake_variants("北京"))

LOCAL_FOOD_LEGACY_COMPAT_TERMS = _legacy_mojibake_variants(
    "北京特色",
    "特色",
    "小吃",
    "本地",
    "老字号",
)

def _route_option_title(profile: Any, policy_type: Any) -> str:
    """Keep the route theme visible when low queue is the ranking profile."""
    profile_text = str(profile or "")
    policy_text = str(policy_type or "")
    transport_titles = {
        "fastest": "最快多交通路线",
        "shortest": "最短距离多交通路线",
        "fewest_transfers": "少换乘多交通路线",
        "low_walking": "少步行多交通路线",
    }
    if profile_text in transport_titles:
        return transport_titles[profile_text]
    if profile_text != "low_queue":
        return PROFILE_TITLES.get(profile_text, PROFILE_TITLES["balanced"])
    themed_titles = {
        "food_only": "少排队纯美食路线",
        "food_focused": "少排队美食路线",
        "culture_focused": "少排队景点路线",
        "balanced": "少排队均衡路线",
    }
    return themed_titles.get(policy_text, PROFILE_TITLES["low_queue"])


EARTH_RADIUS_M = 6371000.0
WALKING_SPEED_KMPH = 4.8
MULTIMODAL_LOW_FRICTION = "multimodal_low_friction"
MULTIMODAL_ALLOWED_MODES = ("walking", "bicycling", "transit")
WEATHER_SENSITIVE_NON_BICYCLING_CONDITIONS = {"rain", "storm", "snow", "hot", "wind", "windy"}
WEATHER_SENSITIVE_NON_BICYCLING_TERMS = (
    "rain",
    "storm",
    "snow",
    "thunder",
    "shower",
    "wind",
    "windy",
    "hot",
    "\u96e8",
    "\u96f7\u96e8",
    "\u9635\u96e8",
    "\u66b4\u96e8",
    "\u5f3a\u5bf9\u6d41",
    "\u5927\u98ce",
    "\u9ad8\u6e29",
    "\u964d\u96ea",
)
TRANSPORT_OPTIMIZATION_PROFILES = ("balanced", "fastest", "shortest", "fewest_transfers", "low_walking")
MIN_ROUTE_POIS = 3
CITYWALK_MAX_DISTANCE_M = 6000
CITYWALK_MIN_QUALITY_POIS = 3
URBAN_ACTIVITY_REJECT_TERMS = (
    "school",
    "training",
    "education",
    "language",
    "campus",
    "ielts",
    "toefl",
    "sat",
    "alevel",
    "storage",
    "locker",
    "shoe",
    "parking",
    "hotel",
    "accommodation",
    "lodging",
    "convenience",
    "supermarket",
    "grocery",
    "subway",
    "station",
    "police",
    "swimming",
    "clinic",
    "medical",
    "hospital",
    "treatment",
    "\u5b66\u6821",
    "\u6821\u533a",
    "\u57f9\u8bad",
    "\u6559\u80b2",
    "\u8bed\u8a00",
    "\u96c5\u601d",
    "\u6258\u798f",
    "\u5b58\u5305",
    "\u5e03\u978b",
    "\u978b",
    "\u505c\u8f66",
    "\u9152\u5e97",
    "\u5bbe\u9986",
    "\u4f4f\u5bbf",
    "\u4fbf\u5229\u5e97",
    "\u4fbf\u5229\u8702",
    "\u8d85\u5e02",
    "\u5c0f\u5356\u90e8",
    "\u5730\u94c1",
    "\u6d3e\u51fa\u6240",
    "\u8b66",
    "\u6e38\u6cf3",
    "\u7532\u6c9f\u708e",
    "\u7070\u6307\u7532",
    "\u4e13\u79d1",
    "\u8bca\u6240",
    "\u533b\u9662",
    "\u6cbb\u7597",
    "\u8db3\u75c5",
)
ACTIVITY_QUALITY_RULES = (
    {
        "slot_terms": ("wellness", "massage", "spa", "foot_spa", "relax", "\u6309\u6469", "\u8db3\u7597", "\u63a8\u62ff", "\u517b\u751f", "\u653e\u677e", "\u7406\u7597"),
        "positive_terms": ("massage", "spa", "foot_spa", "foot massage", "tuina", "therapy", "\u6309\u6469", "\u8db3\u7597", "\u8db3\u6d74", "\u63a8\u62ff", "\u517b\u751f", "\u7406\u7597", "\u6c34\u7597"),
        "reject_terms": ("school", "training", "education", "language", "campus", "academy", "college", "course", "chair", "showroom", "swimming", "sports school", "child", "kids", "pediatric", "instrument", "device", "massager", "store", "shop", "ielts", "toefl", "sat", "alevel", "\u5b66\u6821", "\u5b66\u9662", "\u6821\u533a", "\u57f9\u8bad", "\u8bfe\u7a0b", "\u6309\u6469\u6905", "\u6309\u6469\u4eea", "\u6309\u6469\u5668", "\u6309\u6469\u67aa", "\u4e13\u5356", "\u5546\u5e97", "\u5bb6\u5c45", "\u6e38\u6cf3", "\u8fd0\u52a8\u5b66\u6821", "\u5c0f\u513f", "\u513f\u7ae5", "\u6bcd\u5b50", "\u4eb2\u5b50", "\u5c11\u513f", "\u513f\u79d1", "\u96c5\u601d", "\u6258\u798f"),
        "hard_reject_terms": ("school", "training", "education", "language", "campus", "academy", "college", "course", "chair", "showroom", "swimming", "sports school", "child", "kids", "pediatric", "instrument", "device", "massager", "store", "shop", "\u5b66\u6821", "\u5b66\u9662", "\u6821\u533a", "\u57f9\u8bad", "\u8bfe\u7a0b", "\u6309\u6469\u6905", "\u6309\u6469\u4eea", "\u6309\u6469\u5668", "\u6309\u6469\u67aa", "\u4e13\u5356", "\u5546\u5e97", "\u5bb6\u5c45", "\u6e38\u6cf3", "\u8fd0\u52a8\u5b66\u6821", "\u5c0f\u513f", "\u513f\u7ae5", "\u6bcd\u5b50", "\u4eb2\u5b50", "\u5c11\u513f", "\u513f\u79d1"),
        "warning": "wellness",
    },
    {
        "slot_terms": ("bar", "drink", "drinks", "wine", "pub", "cocktail", "beer", "\u5c0f\u9152", "\u5c0f\u914c", "\u9152\u9986", "\u9152\u5427", "\u6e05\u5427", "\u7cbe\u917f", "\u9e21\u5c3e\u9152"),
        "positive_terms": ("bar", "bistro", "wine", "pub", "cocktail", "beer", "lounge", "lobby bar", "\u5c0f\u9152", "\u5c0f\u914c", "\u9152\u9986", "\u9152\u5427", "\u6e05\u5427", "\u7cbe\u917f", "\u9e21\u5c3e\u9152", "\u5564\u9152", "\u5927\u5802\u5427", "\u9910\u9152\u9986", "\u5a01\u58eb\u5fcc"),
        "reject_terms": ("coffee", "cafe", "juice", "tea", "noodle", "restaurant", "meal", "\u5496\u5561", "\u679c\u6c41", "\u5976\u8336", "\u8336", "\u725b\u8089\u9762", "\u62c9\u9762", "\u9762\u9986", "\u9910\u9986", "\u9910\u5385", "\u5b58\u5305", "\u5e03\u978b", "\u978b"),
        "hard_reject_terms": ("starbucks", "coffee", "cafe", "juice", "milk tea", "tea space", "tea house", "noodle", "\u661f\u5df4\u514b", "\u5496\u5561", "\u679c\u6c41", "\u5976\u8336", "\u8336\u7a7a\u95f4", "\u8336\u9986", "\u8336\u5ba4", "\u725b\u8089\u9762", "\u62c9\u9762", "\u9762\u9986"),
        "warning": "drinks",
    },
    {
        "slot_terms": ("exhibition", "gallery", "museum", "art", "\u5c55\u89c8", "\u770b\u5c55", "\u7f8e\u672f\u9986", "\u535a\u7269\u9986", "\u753b\u5eca", "\u827a\u672f"),
        "positive_terms": ("exhibition", "gallery", "museum", "art", "show", "\u5c55\u89c8", "\u5c55", "\u7f8e\u672f\u9986", "\u535a\u7269\u9986", "\u753b\u5eca", "\u827a\u672f", "\u827a\u672f\u9986"),
        "reject_terms": ("store", "shop", "anime", "mall", "school", "training", "education", "campus", "concert", "music hall", "theater", "theatre", "performance", "\u5e97", "\u5546\u5e97", "\u52a8\u6f2b", "\u5b66\u6821", "\u57f9\u8bad", "\u6559\u80b2", "\u6821\u533a", "\u97f3\u4e50\u5385", "\u97f3\u4e50\u4f1a", "\u5267\u573a", "\u6f14\u51fa", "\u5267\u9662", "\u5b58\u5305", "\u5e03\u978b", "\u978b"),
        "hard_reject_terms": ("school", "training", "education", "campus", "concert", "music hall", "theater", "theatre", "performance", "game", "video game", "console", "ps5", "switch", "\u5b66\u6821", "\u57f9\u8bad", "\u6559\u80b2", "\u6821\u533a", "\u97f3\u4e50\u5385", "\u97f3\u4e50\u4f1a", "\u5267\u573a", "\u6f14\u51fa", "\u5267\u9662", "\u6e38\u620f", "\u7535\u73a9", "\u4e3b\u673a", "\u7535\u5b50\u6e38\u620f"),
        "warning": "exhibition",
    },
    {
        "slot_terms": ("photo_spot", "photo", "sightseeing", "landmark", "scenic", "checkin", "check-in", "\u62cd\u7167", "\u6253\u5361", "\u51fa\u7247", "\u5730\u6807", "\u666f\u70b9", "\u89c2\u5149"),
        "positive_terms": ("photo", "sightseeing", "landmark", "scenic", "museum", "gallery", "park", "square", "street", "hutong", "tower", "view", "\u62cd\u7167", "\u6253\u5361", "\u51fa\u7247", "\u5730\u6807", "\u666f\u70b9", "\u535a\u7269\u9986", "\u7f8e\u672f\u9986", "\u516c\u56ed", "\u5e7f\u573a", "\u8857\u533a", "\u80e1\u540c", "\u6b65\u884c\u8857", "\u57ce\u697c", "\u591c\u666f"),
        "reject_terms": ("hotel", "inn", "restaurant", "bbq", "barbecue", "cafe", "coffee", "tea", "milk tea", "dessert", "noodle", "meal", "food", "store", "shop", "\u9152\u5e97", "\u5bbe\u9986", "\u4f4f\u5bbf", "\u9910\u5385", "\u9910\u9986", "\u70e4\u8089", "\u70e7\u70e4", "\u5496\u5561", "\u5976\u8336", "\u8336", "\u751c\u54c1", "\u9762\u9986", "\u7f8e\u98df", "\u996d", "\u5e97", "\u5546\u5e97"),
        "hard_reject_terms": ("hotel", "inn", "restaurant", "bbq", "barbecue", "cafe", "coffee", "tea", "milk tea", "dessert", "noodle", "meal", "food", "\u9152\u5e97", "\u5bbe\u9986", "\u4f4f\u5bbf", "\u9910\u5385", "\u9910\u9986", "\u70e4\u8089", "\u70e7\u70e4", "\u5496\u5561", "\u5976\u8336", "\u751c\u54c1", "\u9762\u9986", "\u7f8e\u98df"),
        "warning": "sightseeing",
    },
    {
        "slot_terms": ("nail", "beauty", "manicure", "\u7f8e\u7532", "\u7f8e\u776b", "\u95fa\u871c"),
        "positive_terms": ("nail", "beauty", "manicure", "lash", "\u7f8e\u7532", "\u7f8e\u776b", "\u7f8e\u5bb9"),
        "reject_terms": ("school", "training", "storage", "clinic", "medical", "hospital", "treatment", "\u5b66\u6821", "\u57f9\u8bad", "\u5b58\u5305", "\u7532\u6c9f\u708e", "\u7070\u6307\u7532", "\u4e13\u79d1", "\u8bca\u6240", "\u533b\u9662", "\u6cbb\u7597", "\u8db3\u75c5"),
        "hard_reject_terms": ("clinic", "medical", "hospital", "treatment", "\u7532\u6c9f\u708e", "\u7070\u6307\u7532", "\u4e13\u79d1", "\u8bca\u6240", "\u533b\u9662", "\u6cbb\u7597", "\u8db3\u75c5"),
        "warning": "beauty",
    },
    {
        "slot_terms": ("dining", "dinner", "food", "meal", "restaurant", "late_night_food", "snack", "\u665a\u996d", "\u5403\u996d", "\u7f8e\u98df", "\u591c\u5bb5", "\u5c0f\u5403"),
        "positive_terms": ("dining", "dinner", "food", "meal", "restaurant", "bbq", "snack", "\u9910", "\u996d", "\u7f8e\u98df", "\u591c\u5bb5", "\u70e7\u70e4", "\u5c0f\u5403", "\u83dc", "\u9986"),
        "reject_terms": ("school", "training", "education", "storage", "shoe", "\u5b66\u6821", "\u57f9\u8bad", "\u6559\u80b2", "\u5b58\u5305", "\u5e03\u978b", "\u978b"),
        "warning": "dining",
    },
)
CITYWALK_SEMANTIC_TERMS = (
    "citywalk",
    "city walk",
    "walk",
    "walking",
    "walking_route",
    "walk_route",
    "stroll",
    "easy_walk",
    "light_walk",
    "scenic_walk",
    "photo_walk",
    "hutong",
    "neighborhood",
    "\u6563\u6b65",
    "\u6f2b\u6b65",
    "\u5f92\u6b65",
    "\u8d70\u8d70",
    "\u8857\u533a",
    "\u80e1\u540c",
    "\u8f7b\u677e",
)
CITYWALK_STRONG_TERMS = (
    "citywalk",
    "city walk",
    "walking_route",
    "walk_route",
    "easy_walk",
    "light_walk",
    "scenic_walk",
    "photo_walk",
    "hutong",
    "block",
    "street",
    "neighborhood",
    "\u6f2b\u6b65",
    "\u5f92\u6b65",
    "\u8857\u533a",
    "\u80e1\u540c",
)
NON_CITYWALK_PRIMARY_TERMS = (
    "dining",
    "dinner",
    "food",
    "late_night_food",
    "restaurant",
    "meal",
    "cafe",
    "coffee",
    "bar",
    "drink",
    "drinks",
    "wellness",
    "massage",
    "spa",
    "nail",
    "\u665a\u996d",
    "\u5403\u996d",
    "\u7f8e\u98df",
    "\u591c\u5bb5",
    "\u6309\u6469",
    "\u8db3\u7597",
    "\u7f8e\u7532",
    "\u5c0f\u9152",
)
CITYWALK_SUPPORT_ACTIVITY_TYPES = {
    "cultural_sightseeing",
    "culture_sightseeing",
    "culture",
    "leisure_walk",
    "park_visit",
    "photo_spot",
    "scenic_walk",
    "walking",
    "walk",
    "stroll",
    "strolling",
    "food_tasting",
    "snack_tasting",
    "rest",
    "relaxation",
    "sightseeing",
}
CITYWALK_POI_TERMS = (
    "citywalk",
    "walk",
    "stroll",
    "landmark",
    "park",
    "square",
    "street",
    "hutong",
    "museum",
    "gallery",
    "temple",
    "gate",
    "tower",
    "night view",
    "\u516c\u56ed",
    "\u5e7f\u573a",
    "\u8857\u533a",
    "\u80e1\u540c",
    "\u6b65\u884c\u8857",
    "\u524d\u95e8",
    "\u6b63\u9633\u95e8",
    "\u4e1c\u4ea4\u6c11\u5df7",
    "\u5927\u6805\u680f",
    "\u7eaa\u5ff5\u7891",
    "\u7eaa\u5ff5\u5802",
    "\u56fd\u5bb6\u5927\u5267\u9662",
    "\u666f\u70b9",
    "\u5730\u6807",
    "\u535a\u7269\u9986",
    "\u5c55\u89c8",
    "\u753b\u5eca",
    "\u5bfa",
    "\u95e8",
    "\u697c",
    "\u591c\u666f",
)
CITYWALK_REJECT_TERMS = (
    "hotel",
    "inn",
    "accommodation",
    "parking",
    "subway",
    "metro",
    "station",
    "police",
    "restaurant",
    "dining",
    "\u9152\u5e97",
    "\u5bbe\u9986",
    "\u996d\u5e97",
    "\u505c\u8f66\u573a",
    "\u5730\u94c1",
    "\u5730\u94c1\u7ad9",
    "\u516c\u5b89",
    "\u6d3e\u51fa\u6240",
    "\u5206\u5c40",
    "\u9910\u5385",
    "\u9910\u996e",
)
CITYWALK_TRANSIT_REJECT_TERMS = (
    "subway",
    "metro",
    "station",
    "\u5730\u94c1",
    "\u5730\u94c1\u7ad9",
)
CITYWALK_NON_TRANSIT_REJECT_TERMS = tuple(
    term for term in CITYWALK_REJECT_TERMS if term not in CITYWALK_TRANSIT_REJECT_TERMS
)
CITYWALK_COMMERCIAL_MAIN_REJECT_TERMS = (
    "school",
    "training",
    "swimming",
    "gym",
    "fitness",
    "\u89c2\u5149\u8f66",
    "\u94db\u94db\u8f66",
    "\u5496\u5561",
    "\u4e0b\u5348\u8336",
    "\u9152\u5427",
    "\u5c0f\u9152",
    "\u5267\u573a",
    "\u5c0f\u5267\u573a",
    "\u5f71\u5267\u9662",
    "\u5a31\u4e50\u573a\u6240",
    "\u4f53\u80b2\u4f11\u95f2\u670d\u52a1\u573a\u6240",
    "\u73a9\u5bb6",
    "\u6e38\u6cf3",
    "\u8fd0\u52a8\u5b66\u6821",
    "\u5b66\u6821",
    "\u6821\u533a",
    "\u57f9\u8bad",
    "\u5065\u8eab",
)
CITYWALK_REST_STOP_TERMS = (
    "cafe",
    "coffee",
    "tea",
    "bookstore",
    "dessert",
    "\u5496\u5561",
    "\u8336",
    "\u4e66\u5e97",
    "\u751c\u54c1",
    "\u4f11\u606f",
)

CITYWALK_ACTIVITY_TYPE_TERMS = (
    "citywalk",
    "stroll",
    "scenic_walk",
    "walk",
    "walking",
    "walking_tour",
    "strolling",
    "leisure_walk",
    "sightseeing",
    "cultural_sightseeing",
    "culture_sightseeing",
    "park_visit",
    "relaxation",
    "\u6563\u6b65",
    "\u6f2b\u6b65",
    "\u5f92\u6b65",
)

ACCESS_PUBLIC_SPACE_TERMS = (
    "\u5e7f\u573a",
    "\u80e1\u540c",
    "\u8857\u533a",
    "\u6b65\u884c\u8857",
    "\u524d\u95e8",
    "\u524d\u95e8\u5927\u8857",
    "\u4e1c\u4ea4\u6c11\u5df7",
    "\u5927\u6805\u680f",
    "\u6cb3\u8fb9",
    "\u6cb3\u6ee8",
    "\u6b65\u9053",
    "\u6865",
    "\u5927\u8857",
    "\u5c0f\u5df7",
)

ACCESS_VIEW_ONLY_TERMS = (
    "\u6545\u5bab",
    "\u5929\u5b89\u95e8",
    "\u534e\u8868",
    "\u7aef\u95e8",
    "\u57ce\u697c",
    "\u724c\u697c",
    "\u7eaa\u5ff5\u7891",
    "\u7eaa\u5ff5\u5802",
    "\u6b63\u9633\u95e8",
    "\u7bad\u697c",
    "\u56fd\u5bb6\u5927\u5267\u9662",
    "\u5730\u6807",
)

ACCESS_REQUIRES_OPENING_TERMS = (
    "\u516c\u56ed",
    "\u535a\u7269\u9986",
    "\u5c55\u89c8",
)

ACCESS_STRICT_REQUIRES_OPENING_TERMS = (
    "\u5f71\u9662",
    "\u5f71\u57ce",
    "\u6f14\u51fa",
    "\u5546\u573a",
    "\u5546\u5e97",
    "\u4e66\u5e97",
    "\u56fe\u4e66\u9986",
    "\u9910\u5385",
    "\u9910\u996e",
    "\u5496\u5561",
    "\u9152\u5427",
    "\u5c0f\u9152",
    "spa",
    "\u6309\u6469",
    "\u8db3\u7597",
    "\u7f8e\u7532",
    "ktv",
)


class RouteCostMatrixError(RuntimeError):
    """Structured route matrix failure without exposing credentials."""

    def __init__(self, message: str, diagnostics: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


def extract_route_planning_input(
    context: Optional[Mapping[str, Any]],
    previous_results: Optional[Sequence[Mapping[str, Any]]],
) -> Dict[str, Any]:
    """Resolve all structured inputs needed by route planning."""
    context = dict(context or {})
    previous_results = list(previous_results or [])

    event_data = _find_previous_data(previous_results, "event_collection")
    poi_data = _find_previous_data(previous_results, "poi_search")

    pois = poi_data.get("pois") if isinstance(poi_data, Mapping) else []
    route_preference = _resolve_route_preference(context, previous_results, poi_data)
    weights = _normalize_weights(route_preference.get("weights"))
    urban_intent_profile = _resolve_urban_intent_profile(context, previous_results, poi_data)

    key_entities = context.get("key_entities") if isinstance(context.get("key_entities"), Mapping) else {}
    query_text = _query_text(context)
    duration_source = _first_non_empty(
        context.get("duration"),
        key_entities.get("duration") if isinstance(key_entities, Mapping) else None,
        query_text,
        event_data.get("duration"),
        event_data.get("duration_text"),
        event_data.get("duration_min"),
        event_data.get("duration_minutes"),
        f"{event_data.get('duration_days')} days" if event_data.get("duration_days") else None,
    )
    duration_min = parse_time_budget_min(duration_source)
    urban_duration_min = _urban_profile_duration_min(urban_intent_profile)
    if urban_duration_min:
        duration_min = urban_duration_min

    city = _first_non_empty(
        poi_data.get("city") if isinstance(poi_data, Mapping) else None,
        event_data.get("destination"),
        event_data.get("city"),
        key_entities.get("destination") if isinstance(key_entities, Mapping) else None,
        key_entities.get("city") if isinstance(key_entities, Mapping) else None,
    )
    anchor_hint = _first_non_empty(
        poi_data.get("anchor_hint") if isinstance(poi_data, Mapping) else None,
        event_data.get("area_hint"),
        event_data.get("search_area"),
        event_data.get("anchor_poi"),
        key_entities.get("area_hint") if isinstance(key_entities, Mapping) else None,
        key_entities.get("search_area") if isinstance(key_entities, Mapping) else None,
        key_entities.get("anchor_poi") if isinstance(key_entities, Mapping) else None,
    )
    start_location = _resolve_start_location(context, event_data, poi_data)

    return {
        "city": str(city or ""),
        "anchor_hint": str(anchor_hint or ""),
        "start_location": start_location,
        "duration_min": duration_min,
        "pois": list(pois or []),
        "route_preference": route_preference,
        "weights": weights,
        "event_data": dict(event_data or {}),
        "poi_search_result": dict(poi_data or {}),
        "urban_intent_profile": urban_intent_profile,
        "weather_context": urban_intent_profile.get("weather_context") if isinstance(urban_intent_profile, Mapping) else {},
        "query_text": query_text,
        "context": context,
    }


def _urban_profile_duration_min(urban_intent_profile: Any) -> Optional[int]:
    if not isinstance(urban_intent_profile, Mapping):
        return None
    time_context = urban_intent_profile.get("time_context")
    if not isinstance(time_context, Mapping):
        return None
    for key in ("duration_min", "duration_minutes", "total_duration_min"):
        value = time_context.get(key)
        try:
            if value not in (None, "", []):
                minutes = int(round(float(value)))
                return minutes if minutes > 0 else None
        except (TypeError, ValueError):
            continue
    return None


def _resolve_urban_intent_profile(
    context: Mapping[str, Any],
    previous_results: Sequence[Mapping[str, Any]],
    poi_data: Mapping[str, Any],
) -> Dict[str, Any]:
    candidates: List[Any] = [
        poi_data.get("urban_intent_profile") if isinstance(poi_data, Mapping) else None,
        context.get("urban_intent_profile"),
    ]
    data = context.get("data") if isinstance(context.get("data"), Mapping) else {}
    if isinstance(data, Mapping):
        candidates.append(data.get("urban_intent_profile"))
    for item in previous_results or []:
        result = item.get("result", {}) if isinstance(item, Mapping) else {}
        data = result.get("data", {}) if isinstance(result, Mapping) else {}
        if isinstance(data, Mapping):
            candidates.append(data.get("urban_intent_profile"))
        if isinstance(result, Mapping):
            candidates.append(result.get("urban_intent_profile"))
    for candidate in candidates:
        if isinstance(candidate, Mapping) and candidate:
            return dict(candidate)
    return {}


def parse_time_budget_min(duration: Any) -> int:
    """Parse a duration into minutes with conservative travel defaults."""
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        value = float(duration)
        if value <= 14:
            return int(round(value * 60))
        return int(round(value))

    text = str(duration or "").strip()
    if not text:
        return 180

    text_lower = text.casefold()
    number = r"([0-9]+(?:\.[0-9]+)?)"

    if "\u534a\u5929" in text_lower:
        return 240
    if any(token in text_lower for token in ("\u4e00\u65e5", "\u4e00\u5929", "1\u65e5", "1\u5929", "\u6574\u5929")):
        return 480
    normal_minute_match = re.search(number + r"\s*(?:\u5206\u949f|\u5206|min|mins|minutes)", text_lower)
    if normal_minute_match:
        return max(30, int(round(float(normal_minute_match.group(1)))))
    normal_hour_match = re.search(number + r"\s*(?:\u5c0f\u65f6|h|hour|hours)", text_lower)
    if normal_hour_match:
        return max(30, int(round(float(normal_hour_match.group(1)) * 60)))
    normal_day_match = re.search(number + r"\s*(?:\u5929|\u65e5|day|days)", text_lower)
    if normal_day_match:
        return max(240, int(round(float(normal_day_match.group(1)) * 480)))
    normal_chinese_numbers = {
        "\u4e00": 1,
        "\u4e8c": 2,
        "\u4e24": 2,
        "\u4e09": 3,
        "\u56db": 4,
        "\u4e94": 5,
        "\u516d": 6,
        "\u4e03": 7,
        "\u516b": 8,
        "\u4e5d": 9,
        "\u5341": 10,
    }
    for cn, value in normal_chinese_numbers.items():
        if f"{cn}\u5c0f\u65f6" in text_lower:
            return value * 60
        if f"{cn}\u5929" in text_lower or f"{cn}\u65e5" in text_lower:
            return value * 480

    return 180


def normalize_pois(pois: Sequence[Mapping[str, Any]], weather_context: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    """Normalize POIs into a stable planner-friendly shape."""
    normalized: List[Dict[str, Any]] = []
    seen = set()
    for index, poi in enumerate(pois or []):
        if not isinstance(poi, Mapping):
            continue
        location = _parse_location(poi)
        if location is None:
            continue

        item = dict(poi)
        name = str(item.get("name") or "").strip()
        poi_id = str(item.get("id") or name or f"poi-{index + 1}").strip()
        key = poi_id or f"{name}:{location['lng']:.6f},{location['lat']:.6f}"
        if key in seen:
            continue
        seen.add(key)

        category = str(item.get("category") or "other").strip() or "other"
        if category not in {"dining", "culture_entertainment", "other"}:
            category = "other"

        item["id"] = poi_id
        item["name"] = name or poi_id
        item["category"] = category
        item["location"] = {"lng": round(location["lng"], 6), "lat": round(location["lat"], 6)}
        item["activity_types"] = _unique_list(_as_list(item.get("activity_types")) or ([item.get("activity_type")] if item.get("activity_type") else []))
        item["matched_activity_slots"] = _unique_list(_as_list(item.get("matched_activity_slots")))
        item["candidate_activity_slots"] = _unique_list(_as_list(item.get("candidate_activity_slots")) or ([item.get("activity_slot_id")] if item.get("activity_slot_id") else []))
        item["micro_category"] = item.get("micro_category") or item.get("activity_type") or item.get("type")
        item["opening_status"] = item.get("opening_status") or opening_status(item.get("opening_hours")).replace("verified_", "")
        item["indoor_outdoor"] = item.get("indoor_outdoor") or "unknown"
        item["weather_tags"] = _unique_list(_as_list(item.get("weather_tags")))
        try:
            item["weather_fit_score"] = float(item.get("weather_fit_score"))
        except (TypeError, ValueError):
            item["weather_fit_score"] = _default_weather_fit_score(item, weather_context)
        item["rating"] = _bounded_float(_first_non_empty(_nested_get(item, "ugc", "rating"), item.get("rating")), 0, 5, 3.8)
        item["cost"] = max(0.0, _to_float(_first_non_empty(item.get("cost"), item.get("estimated_cost")), 80.0))
        item["queue_risk"] = _bounded_float(
            _first_non_empty(_nested_get(item, "ugc", "queue_risk"), item.get("queue_risk")),
            0,
            1,
            0.45,
        )
        if item.get("activity_duration_min") not in (None, ""):
            try:
                item["visit_duration_min"] = max(20, int(float(item.get("activity_duration_min"))))
            except (TypeError, ValueError):
                item["visit_duration_min"] = _visit_duration(item)
        else:
            item["visit_duration_min"] = _visit_duration(item)
        item["_input_index"] = index
        normalized.append(item)

    return sorted(normalized, key=lambda item: (_category_rank(item), item["name"], item["id"]))


def _compact_place_name(value: Any) -> str:
    return re.sub(r"[\s\(\)\uff08\uff09\-_\u00b7\.,，。:：]+", "", str(value or "").casefold())


def _is_start_location_duplicate_poi(poi: Mapping[str, Any], start_location: Any) -> bool:
    start = _normalize_start_location(start_location)
    if not start:
        return False
    start_name = _compact_place_name(start.get("name") or start.get("address"))
    poi_name = _compact_place_name(poi.get("name"))
    if not start_name or not poi_name:
        return False
    distance_m = _haversine_meters(start, poi)
    if poi_name == start_name and distance_m <= 250.0:
        return True
    if distance_m <= 80.0 and (poi_name in start_name or start_name in poi_name):
        return True
    return False


def _filter_start_location_duplicate_pois(
    pois: Sequence[Dict[str, Any]],
    start_location: Any,
) -> Tuple[List[Dict[str, Any]], int]:
    if not pois or not _normalize_start_location(start_location):
        return list(pois), 0
    filtered = [poi for poi in pois if not _is_start_location_duplicate_poi(poi, start_location)]
    return filtered, len(pois) - len(filtered)


def infer_composition_policy(context: Mapping[str, Any], weights: Mapping[str, Any]) -> Dict[str, Any]:
    """Infer route composition rules from preference weights and time budget."""
    duration_min = int(context.get("duration_min") or parse_time_budget_min(context.get("duration")))
    food = float(weights.get("food", 0.0) or 0.0)
    sightseeing = float(weights.get("sightseeing", 0.0) or 0.0)
    efficiency = float(weights.get("travel_efficiency", 0.0) or 0.0)
    queue = float(weights.get("queue", 0.0) or 0.0)
    query_text = str(context.get("query_text") or _query_text(context))
    route_preference = context.get("route_preference") if isinstance(context.get("route_preference"), Mapping) else {}
    route_type = str(route_preference.get("route_type") or "").casefold()
    avoid_queue = _queue_requested(query_text)
    citywalk_requested = _citywalk_requested(query_text, route_preference)

    max_pois = 3 if duration_min <= 210 or efficiency >= 0.18 else 4
    if duration_min >= 420 and efficiency < 0.18:
        max_pois = 5

    route_size_min = 3
    route_size_max = max(route_size_min, max_pois)
    policy_type = "balanced"
    min_dining = 1
    max_dining = 1
    min_culture = 1
    required_categories = ["dining", "culture_entertainment"]
    allowed_category_compositions: List[Dict[str, int]] = []
    profiles = ["balanced", "efficient", "low_queue"]

    food_only_query = any(
        token in query_text
        for token in ("\u53ea\u60f3\u5403", "\u53ea\u5403", "\u4e0d\u901b\u666f\u70b9", "\u4e0d\u8981\u666f\u70b9", "\u7eaf\u7f8e\u98df", "\u63a2\u5e97")
    )
    if citywalk_requested:
        policy_type = "citywalk"
        route_size_min = MIN_ROUTE_POIS
        route_size_max = 3
        min_dining = 0
        max_dining = 1
        min_culture = 2
        required_categories = ["culture_entertainment"]
        allowed_category_compositions = [
            {"dining": 0, "culture_entertainment": 2, "other": 0},
            {"dining": 0, "culture_entertainment": 3, "other": 0},
            {"dining": 1, "culture_entertainment": 2, "other": 0},
            {"dining": 0, "culture_entertainment": 2, "other": 1},
        ]
        profiles = ["citywalk", "efficient", "low_queue"]
    elif route_type in {"food_only", "food-only"} or food_only_query:
        policy_type = "food_only"
        route_size_max = 3
        min_dining = 3
        max_dining = 3
        min_culture = 0
        required_categories = ["dining"]
        allowed_category_compositions = [{"dining": 3, "culture_entertainment": 0, "other": 0}]
        profiles = ["food_only", "food_focused", "efficient", "low_queue"]
    elif food >= max(0.45, sightseeing + 0.12):
        policy_type = "food_focused"
        route_size_max = 3
        min_dining = 2
        max_dining = 3
        min_culture = 0
        required_categories = ["dining"]
        allowed_category_compositions = [
            {"dining": 2, "culture_entertainment": 1, "other": 0},
            {"dining": 3, "culture_entertainment": 0, "other": 0},
            {"dining": 2, "culture_entertainment": 0, "other": 1},
        ]
        profiles = ["food_focused", "efficient", "low_queue"]
    elif sightseeing >= max(0.45, food + 0.12):
        policy_type = "culture_focused"
        route_size_max = 3
        min_dining = 0
        max_dining = 1
        min_culture = 2
        required_categories = ["culture_entertainment"]
        allowed_category_compositions = [
            {"dining": 1, "culture_entertainment": 2, "other": 0},
            {"dining": 0, "culture_entertainment": 3, "other": 0},
            {"dining": 0, "culture_entertainment": 2, "other": 1},
        ]
        profiles = ["culture_focused", "efficient", "low_queue"]
    elif efficiency >= 0.18:
        policy_type = "efficient"
        route_size_max = 3
        profiles = ["efficient", "balanced", "low_queue"]

    if queue >= 0.12 or avoid_queue:
        profiles = ["low_queue", *[profile for profile in profiles if profile != "low_queue"]]

    return {
        "policy_type": policy_type,
        "duration_budget_min": duration_min,
        "min_pois": route_size_min,
        "route_size_min": route_size_min,
        "route_size_max": route_size_max,
        "min_dining": min_dining,
        "max_dining": max_dining,
        "min_culture_entertainment": min_culture,
        "required_categories": required_categories,
        "allowed_category_compositions": allowed_category_compositions,
        "profiles": profiles,
        "weather_context": context.get("weather_context", {}),
        "decision": {
            "food_weight": round(food, 4),
            "sightseeing_weight": round(sightseeing, 4),
            "travel_efficiency_weight": round(efficiency, 4),
            "queue_weight": round(queue, 4),
            "avoid_queue_requested": avoid_queue,
            "citywalk_requested": citywalk_requested,
        },
    }


def compute_poi_reward(
    poi: Mapping[str, Any],
    weights: Mapping[str, Any],
    anchor_hint: str,
    query_text: Any = "",
    city: Any = "",
) -> float:
    """Compute a deterministic multi-objective reward for a single POI."""
    category = str(poi.get("category") or "other")
    rating_norm = _bounded_float(poi.get("rating"), 0, 5, 3.8) / 5.0
    queue = _bounded_float(poi.get("queue_risk"), 0, 1, 0.45)
    cost = max(0.0, _to_float(poi.get("cost"), 80.0))

    sightseeing_match = 1.0 if category == "culture_entertainment" else _term_match(poi, ("\u666f\u70b9", "\u6587\u5316", "\u6253\u5361", "\u535a\u7269\u9986"))
    food_match = 1.0 if category == "dining" else _term_match(poi, ("\u7f8e\u98df", "\u9910\u996e", "\u5c0f\u5403", "\u7279\u8272\u83dc"))
    experience_match = _term_match(poi, ("\u4f53\u9a8c", "\u5c55\u89c8", "\u535a\u7269\u9986", "\u7279\u8272", "\u8001\u5b57\u53f7", "\u975e\u9057"))
    efficiency_match = _term_match(poi, ("\u9644\u8fd1", "citywalk", "\u987a\u8def", "\u5546\u5708", "\u5730\u94c1"))
    anchor_bonus = 0.08 if anchor_hint and anchor_hint.replace("\u9644\u8fd1", "") in _poi_text(poi) else 0.0

    quality = 1.8 * rating_norm
    preference = (
        float(weights.get("sightseeing", 0.0) or 0.0) * sightseeing_match
        + float(weights.get("food", 0.0) or 0.0) * food_match
        + float(weights.get("experience", 0.0) or 0.0) * experience_match
        + float(weights.get("travel_efficiency", 0.0) or 0.0) * efficiency_match
    )
    queue_penalty = (0.25 + 1.6 * float(weights.get("queue", 0.0) or 0.0)) * queue
    cost_penalty = (0.12 + 1.2 * float(weights.get("cost", 0.0) or 0.0)) * min(1.2, cost / 180.0)
    recall_bonus = min(0.18, 0.04 * len(_as_list(poi.get("recall_sources"))))
    local_food_bonus = 0.0
    generic_food_penalty = 0.0
    cuisine_terms = _requested_cuisine_terms(query_text)
    cuisine_bonus = 0.0
    cuisine_mismatch_penalty = 0.0
    if category == "dining" and cuisine_terms:
        food_weight = float(weights.get("food", 0.0) or 0.0)
        cuisine_match = _text_term_match(_poi_text(poi), cuisine_terms)
        cuisine_bonus = (1.2 + 1.4 * food_weight) * cuisine_match
        if cuisine_match <= 0:
            cuisine_mismatch_penalty = (0.4 + 0.8 * food_weight) * _text_term_match(
                _poi_core_text(poi),
                BEIJING_LOCAL_FOOD_TERMS,
            )
    if category == "dining" and not cuisine_terms and _local_food_requested(query_text, city, weights):
        core_text = _poi_core_text(poi)
        local_match = _text_term_match(core_text, BEIJING_LOCAL_FOOD_TERMS)
        generic_match = _text_term_match(core_text, GENERIC_WESTERN_DINING_TERMS)
        food_weight = float(weights.get("food", 0.0) or 0.0)
        local_food_bonus = (1.0 + 1.4 * food_weight) * local_match
        generic_food_penalty = (1.2 + 1.6 * food_weight) * generic_match

    return round(
        max(
            0.01,
            quality
            + 2.2 * preference
            + anchor_bonus
            + recall_bonus
            + cuisine_bonus
            + local_food_bonus
            - cuisine_mismatch_penalty
            - generic_food_penalty
            - queue_penalty
            - cost_penalty,
        ),
        4,
    )


def urban_poi_reward_adjustment(poi: Mapping[str, Any], urban_intent_profile: Any) -> float:
    if not isinstance(urban_intent_profile, Mapping) or not urban_intent_profile:
        return 0.0
    adjustment = 0.0
    status = opening_status(poi.get("opening_hours"))
    if status == "verified_open":
        adjustment += 0.35
    elif status == "unknown":
        adjustment -= 0.25
    weather = urban_intent_profile.get("weather_context") if isinstance(urban_intent_profile.get("weather_context"), Mapping) else {}
    text = _poi_text(poi)
    ascii_indoor_terms = ("indoor", "sheltered", "covered", "mall")
    ascii_outdoor_terms = ("outdoor", "terrace", "open_air")
    indoor_terms = ("\u5546\u573a", "\u5496\u5561", "\u9910\u5385", "SPA", "spa", "\u6309\u6469", "\u8db3\u7597", "\u7f8e\u7532", "\u5c55\u89c8", "\u535a\u7269\u9986", "\u9152\u5427", "\u5c0f\u9152", "\u5ba4\u5185")
    outdoor_terms = ("\u516c\u56ed", "\u5e7f\u573a", "\u8857\u533a", "citywalk", "\u9732\u53f0", "\u6237\u5916", "\u80e1\u540c", "\u6b65\u884c\u8857")
    if _weather_indoor_preferred(weather):
        if any(term in text for term in indoor_terms) or any(term in text for term in ascii_indoor_terms):
            adjustment += 0.45
        if any(term in text for term in outdoor_terms) or any(term in text for term in ascii_outdoor_terms):
            adjustment -= 0.55
    elif _weather_good_for_outdoor(weather):
        if any(term in text for term in outdoor_terms) or any(term in text for term in ascii_outdoor_terms):
            adjustment += 0.25
    activity_type = str(poi.get("activity_type") or "")
    if activity_type in {"wellness", "beauty", "late_night_food", "drinks"}:
        adjustment += 0.2
    return adjustment


def _weather_indoor_preferred(weather: Mapping[str, Any]) -> bool:
    if not isinstance(weather, Mapping):
        return False
    condition = str(weather.get("condition") or "").casefold()
    precipitation = str(weather.get("precipitation_risk") or "").casefold()
    wind = str(weather.get("wind_risk") or "").casefold()
    try:
        temperature = float(weather.get("temperature_c"))
    except (TypeError, ValueError):
        temperature = 22.0
    return bool(
        weather.get("indoor_preferred") is True
        or condition in {"rain", "storm", "snow", "hot", "wind"}
        or _weather_condition_has_sensitive_term(condition)
        or precipitation in {"medium", "high"}
        or wind in {"medium", "high"}
        or temperature >= 32
        or temperature <= -5
    )


def _weather_condition_has_sensitive_term(condition: Any) -> bool:
    text = str(condition or "").casefold()
    if not text:
        return False
    no_rain_markers = ("no rain", "without rain", "\u65e0\u96e8", "\u4e0d\u4e0b\u96e8")
    if any(marker in text for marker in no_rain_markers):
        non_rain_terms = tuple(
            term
            for term in WEATHER_SENSITIVE_NON_BICYCLING_TERMS
            if term not in {"rain", "shower", "\u96e8", "\u9635\u96e8", "\u66b4\u96e8", "\u96f7\u96e8"}
        )
        return any(term in text for term in non_rain_terms)
    return any(term in text for term in WEATHER_SENSITIVE_NON_BICYCLING_TERMS)


def _weather_discourages_bicycling(weather: Mapping[str, Any]) -> bool:
    if not isinstance(weather, Mapping):
        return False
    condition = str(weather.get("condition") or "").casefold()
    precipitation = str(weather.get("precipitation_risk") or "").casefold()
    wind = str(weather.get("wind_risk") or "").casefold()
    return bool(
        weather.get("indoor_preferred") is True
        or weather.get("prefer_indoor") is True
        or condition in WEATHER_SENSITIVE_NON_BICYCLING_CONDITIONS
        or _weather_condition_has_sensitive_term(condition)
        or precipitation in {"medium", "high"}
        or wind in {"medium", "high"}
    )


def _weather_good_for_outdoor(weather: Mapping[str, Any]) -> bool:
    if not isinstance(weather, Mapping):
        return False
    return str(weather.get("outdoor_suitability") or "").casefold() in {"good", "high"} and not _weather_indoor_preferred(weather)


def _default_weather_fit_score(poi: Mapping[str, Any], weather_context: Optional[Mapping[str, Any]]) -> float:
    indoor_outdoor = str(poi.get("indoor_outdoor") or "unknown")
    weather_context = weather_context if isinstance(weather_context, Mapping) else {}
    if _weather_indoor_preferred(weather_context):
        if indoor_outdoor == "indoor":
            return 0.9
        if indoor_outdoor == "mixed":
            return 0.75
        if indoor_outdoor == "outdoor":
            return 0.25
        return 0.5
    if _weather_good_for_outdoor(weather_context):
        if indoor_outdoor == "outdoor":
            return 0.85
        if indoor_outdoor == "mixed":
            return 0.75
        if indoor_outdoor == "indoor":
            return 0.65
        return 0.55
    if indoor_outdoor == "indoor":
        return 0.75
    if indoor_outdoor == "mixed":
        return 0.65
    if indoor_outdoor == "outdoor":
        return 0.55
    return 0.5


def build_distance_matrix(pois: Sequence[Mapping[str, Any]], mode: str = "haversine") -> List[List[float]]:
    """Build an NxN distance matrix in meters."""
    if mode != "haversine":
        raise ValueError("route_planning first version supports only haversine distance")

    matrix: List[List[float]] = []
    for left in pois:
        row = []
        for right in pois:
            row.append(round(_haversine_meters(left, right), 3))
        matrix.append(row)
    return matrix


def _build_route_costs(
    pois: Sequence[Mapping[str, Any]],
    start_location: Optional[Mapping[str, Any]],
    route_mode: str,
    route_client: Any = None,
    auto_use_amap_route_matrix: bool = False,
    context: Optional[Mapping[str, Any]] = None,
    strict_no_fallback: bool = False,
) -> Dict[str, Any]:
    """Build planner matrices while keeping direct tool calls offline by default."""
    context = context if isinstance(context, Mapping) else {}
    local_costs = _build_haversine_route_costs(pois, start_location, route_mode)
    explicit_setting = context.get("use_amap_route_matrix")
    enabled = route_client is not None or explicit_setting is True or (
        explicit_setting is not False and auto_use_amap_route_matrix
    )
    if not enabled:
        if strict_no_fallback:
            raise RuntimeError("route_cost_matrix_disabled_in_strict_mode")
        return local_costs

    active_client = route_client
    if active_client is None:
        try:
            from services.amap_client import AmapRouteClient

            active_client = AmapRouteClient()
        except Exception:
            if strict_no_fallback:
                raise RuntimeError("amap_route_client_unavailable")
            local_costs["warnings"].append("amap_route_client_unavailable_using_haversine")
            return local_costs
        if not getattr(active_client, "api_key", ""):
            if strict_no_fallback:
                raise RuntimeError("amap_route_key_missing")
            local_costs["warnings"].append("amap_route_key_missing_using_haversine")
            return local_costs

    count_start = {
        "amap_distance_calls": int(getattr(active_client, "_distance_call_count", 0) or 0),
        "amap_direction_calls": int(getattr(active_client, "_direction_call_count", 0) or 0),
    }
    try:
        allowed_modes = _allowed_modes_for_route_context(route_mode, context)
        matrix_result = active_client.build_route_cost_matrix(
            pois,
            route_mode=route_mode,
            include_start_location=start_location,
            max_candidates=max(28, len(pois)),
            strict_no_fallback=strict_no_fallback,
            allowed_modes=allowed_modes,
        )
        projected = _project_route_costs(matrix_result, pois, start_location, route_mode)
        projected["route_client"] = active_client
        projected["_route_client_count_start"] = count_start
        return projected
    except Exception as exc:
        if strict_no_fallback:
            diagnostics = {
                "route_mode": route_mode,
                "matrix_source": "amap_route_matrix",
                "candidate_count": len(pois),
                "has_start_location": bool(start_location),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "amap_distance_calls": int(getattr(active_client, "_distance_call_count", 0) or 0) - count_start["amap_distance_calls"],
                "amap_direction_calls": int(getattr(active_client, "_direction_call_count", 0) or 0) - count_start["amap_direction_calls"],
                "route_error_counts": dict(getattr(active_client, "_route_error_counts", {}) or {}),
            }
            raise RouteCostMatrixError("amap_route_matrix_failed", diagnostics) from exc
        local_costs["warnings"].append("amap_route_matrix_failed_using_haversine")
        return local_costs


def _allowed_modes_for_route_context(route_mode: str, context: Optional[Mapping[str, Any]]) -> Optional[List[str]]:
    if route_mode != MULTIMODAL_LOW_FRICTION:
        return None
    allowed_modes = list(MULTIMODAL_ALLOWED_MODES)
    context = context if isinstance(context, Mapping) else {}
    transport = context.get("transport_mode")
    if isinstance(transport, Mapping) and str(transport.get("source") or "") == "user_explicit":
        explicit_allowed = [
            str(item)
            for item in (transport.get("allowed_modes") or [])
            if str(item) in MULTIMODAL_ALLOWED_MODES
        ]
        return explicit_allowed or allowed_modes
    urban_profile = context.get("urban_intent_profile") if isinstance(context.get("urban_intent_profile"), Mapping) else {}
    weather = urban_profile.get("weather_context") if isinstance(urban_profile.get("weather_context"), Mapping) else {}
    indoor_preferred = weather.get("indoor_preferred") is True or weather.get("prefer_indoor") is True
    if indoor_preferred or _weather_discourages_bicycling(weather):
        return [mode for mode in allowed_modes if mode != "bicycling"]
    return allowed_modes


def _build_haversine_route_costs(
    pois: Sequence[Mapping[str, Any]],
    start_location: Optional[Mapping[str, Any]],
    route_mode: str,
) -> Dict[str, Any]:
    distance_matrix = build_distance_matrix(pois)
    duration_matrix = [
        [_fallback_duration_sec(distance, route_mode) for distance in row]
        for row in distance_matrix
    ]
    fallback_mode = "walking" if route_mode in {"multimodal", MULTIMODAL_LOW_FRICTION} else route_mode
    source_matrix = [
        ["self" if left == right else "haversine_fallback" for right in range(len(pois))]
        for left in range(len(pois))
    ]
    mode_matrix = [
        [fallback_mode for _ in range(len(pois))]
        for _ in range(len(pois))
    ]
    candidate_modes_matrix = [
        [
            {} if left == right else {
                fallback_mode: {
                    "mode": fallback_mode,
                    "distance_m": distance_matrix[left][right],
                    "duration_sec": duration_matrix[left][right],
                    "source": "haversine_fallback",
                    "steps": [],
                    "polyline": "",
                }
            }
            for right in range(len(pois))
        ]
        for left in range(len(pois))
    ]
    normalized_start = _normalize_start_location(start_location)
    start_costs: Dict[int, Dict[str, Any]] = {}
    if normalized_start:
        for index, poi in enumerate(pois):
            distance = _haversine_meters(normalized_start, poi)
            start_costs[index] = {
                "distance_m": distance,
                "duration_sec": _fallback_duration_sec(distance, route_mode),
                "source": "haversine_fallback",
                "mode": fallback_mode,
                "candidate_modes": {
                    fallback_mode: {
                        "mode": fallback_mode,
                        "distance_m": distance,
                        "duration_sec": _fallback_duration_sec(distance, route_mode),
                        "source": "haversine_fallback",
                        "steps": [],
                        "polyline": "",
                    }
                },
                "steps": [],
                "polyline": "",
            }
    source_count = len(pois) * max(0, len(pois) - 1) + len(start_costs)
    return {
        "distance_matrix": distance_matrix,
        "duration_matrix": duration_matrix,
        "source_matrix": source_matrix,
        "mode_matrix": mode_matrix,
        "candidate_modes_matrix": candidate_modes_matrix,
        "start_costs": start_costs,
        "route_client": None,
        "warnings": [],
        "diagnostics": {
            "route_mode": route_mode,
            "matrix_source": "haversine_fallback",
            "source_counts": {"haversine_fallback": source_count},
            "amap_distance_calls": 0,
            "amap_direction_calls": 0,
            "haversine_fallback_count": source_count,
            "failed_pair_count": 0,
        },
    }


def _project_route_costs(
    matrix_result: Mapping[str, Any],
    pois: Sequence[Mapping[str, Any]],
    start_location: Optional[Mapping[str, Any]],
    route_mode: str,
) -> Dict[str, Any]:
    """Project the service matrix into POI matrices plus virtual-start costs."""
    full_distance = list(matrix_result.get("distance_matrix") or [])
    full_duration = list(matrix_result.get("duration_matrix") or [])
    full_source = list(matrix_result.get("source_matrix") or [])
    full_mode = list(matrix_result.get("mode_matrix") or [])
    full_candidates = list(matrix_result.get("candidate_modes_matrix") or [])
    offset = 1 if _normalize_start_location(start_location) else 0
    expected_size = len(pois) + offset
    if (
        len(full_distance) != expected_size
        or len(full_duration) != expected_size
        or len(full_source) != expected_size
    ):
        raise ValueError("route matrix shape does not match POI candidates")
    distance_matrix = [list(row[offset:]) for row in full_distance[offset:]]
    duration_matrix = [list(row[offset:]) for row in full_duration[offset:]]
    source_matrix = [list(row[offset:]) for row in full_source[offset:]]
    fallback_mode = "walking" if route_mode in {"multimodal", MULTIMODAL_LOW_FRICTION} else route_mode
    if len(full_mode) == expected_size:
        mode_matrix = [list(row[offset:]) for row in full_mode[offset:]]
    else:
        mode_matrix = [[fallback_mode for _ in pois] for _ in pois]
    if len(full_candidates) == expected_size:
        candidate_modes_matrix = [list(row[offset:]) for row in full_candidates[offset:]]
    else:
        candidate_modes_matrix = [[{} for _ in pois] for _ in pois]
    details = matrix_result.get("leg_details") if isinstance(matrix_result.get("leg_details"), Mapping) else {}
    start_costs: Dict[int, Dict[str, Any]] = {}
    if offset:
        for poi_index in range(len(pois)):
            matrix_index = poi_index + offset
            detail = details.get(f"0:{matrix_index}") if isinstance(details, Mapping) else {}
            start_costs[poi_index] = {
                "distance_m": full_distance[0][matrix_index],
                "duration_sec": full_duration[0][matrix_index],
                "source": full_source[0][matrix_index],
                "mode": (
                    full_mode[0][matrix_index]
                    if len(full_mode) == expected_size
                    else fallback_mode
                ),
                "candidate_modes": (
                    dict(full_candidates[0][matrix_index])
                    if len(full_candidates) == expected_size and isinstance(full_candidates[0][matrix_index], Mapping)
                    else {}
                ),
                "steps": list(detail.get("steps") or []) if isinstance(detail, Mapping) else [],
                "polyline": str(detail.get("polyline") or "") if isinstance(detail, Mapping) else "",
            }
    diagnostics = dict(matrix_result.get("diagnostics") or {})
    diagnostics.setdefault("route_mode", route_mode)
    diagnostics.setdefault("matrix_source", "amap_route_matrix")
    return {
        "distance_matrix": distance_matrix,
        "duration_matrix": duration_matrix,
        "source_matrix": source_matrix,
        "mode_matrix": mode_matrix,
        "candidate_modes_matrix": candidate_modes_matrix,
        "start_costs": start_costs,
        "warnings": list(matrix_result.get("warnings") or []),
        "diagnostics": diagnostics,
    }


def solve_routes_orienteering_heuristic(
    pois: Sequence[Mapping[str, Any]],
    distance_matrix: Sequence[Sequence[float]],
    weights: Mapping[str, Any],
    composition_policy: Mapping[str, Any],
    duration_budget_min: int,
    start_location: Optional[Mapping[str, Any]] = None,
    duration_matrix: Optional[Sequence[Sequence[float]]] = None,
    source_matrix: Optional[Sequence[Sequence[str]]] = None,
    mode_matrix: Optional[Sequence[Sequence[str]]] = None,
    candidate_modes_matrix: Optional[Sequence[Sequence[Mapping[str, Any]]]] = None,
    start_costs: Optional[Mapping[int, Mapping[str, Any]]] = None,
    route_mode: str = "walking",
    max_options: int = 3,
    prefer_local_food: bool = False,
) -> Dict[str, Any]:
    """Enumerate feasible compositions and rank deterministic route options."""
    indexed_pois = [dict(poi, _matrix_index=index) for index, poi in enumerate(pois)]
    route_size_min = max(MIN_ROUTE_POIS, int(composition_policy.get("route_size_min", MIN_ROUTE_POIS)))
    policy_type = str(composition_policy.get("policy_type") or "balanced")
    warnings: List[str] = []
    dining = [poi for poi in indexed_pois if poi.get("category") == "dining"]
    culture = [poi for poi in indexed_pois if poi.get("category") == "culture_entertainment"]
    other = [poi for poi in indexed_pois if poi.get("category") == "other"]

    def _diagnostics(evaluated_count: int = 0, feasible_count: int = 0) -> Dict[str, Any]:
        return {
            "evaluated_route_count": evaluated_count,
            "candidate_count": len(indexed_pois),
            "dining_candidate_count": len(dining),
            "culture_candidate_count": len(culture),
            "other_candidate_count": len(other),
            "feasible_route_count": feasible_count,
            "profiles": list(composition_policy.get("profiles") or ["balanced"]),
            "policy_type": policy_type,
        }

    if len(indexed_pois) < route_size_min:
        return {
            "routes": [],
            "warnings": ["insufficient_pois"],
            "diagnostics": _diagnostics(),
        }

    evaluated: List[Dict[str, Any]] = []
    profiles = list(composition_policy.get("profiles") or ["balanced"])
    min_size = route_size_min
    max_size = min(int(composition_policy.get("route_size_max", 3)), len(indexed_pois))
    min_dining = int(composition_policy.get("min_dining", 1))
    max_dining = int(composition_policy.get("max_dining", 1))
    min_culture = int(composition_policy.get("min_culture_entertainment", 1))
    unique_dining_brand_count = len(
        {
            _poi_brand_key(poi)
            for poi in dining
            if _poi_brand_key(poi)
        }
    )
    allowed_compositions = composition_policy.get("allowed_category_compositions")
    if not isinstance(allowed_compositions, list):
        allowed_compositions = []

    if policy_type == "food_focused" and len(dining) < 2:
        warnings.append("insufficient_dining_for_food_focused")
        min_dining = min(1, len(dining))
        max_dining = max(min_dining, len(dining))
        allowed_compositions = []
    elif policy_type == "food_only" and len(dining) < 3:
        warnings.append("insufficient_dining_for_food_only")
        return {
            "routes": [],
            "warnings": _unique_list(warnings),
            "diagnostics": _diagnostics(),
        }

    if len(dining) < min_dining:
        warnings.append("missing_required_dining_pois")
        return {
            "routes": [],
            "warnings": _unique_list(warnings),
            "diagnostics": _diagnostics(),
        }
    if len(culture) < min_culture:
        warnings.append("missing_required_culture_entertainment_pois")
        return {
            "routes": [],
            "warnings": _unique_list(warnings),
            "diagnostics": _diagnostics(),
        }

    for profile in profiles:
        for size in range(min_size, max_size + 1):
            for dining_count in range(min_dining, min(max_dining, len(dining), size - min_culture) + 1):
                remaining_after_dining = size - dining_count
                max_culture = min(len(culture), remaining_after_dining)
                for culture_count in range(min_culture, max_culture + 1):
                    other_count = remaining_after_dining - culture_count
                    if other_count < 0 or other_count > len(other):
                        continue
                    if allowed_compositions and not _match_allowed_composition(
                        allowed_compositions,
                        dining_count=dining_count,
                        culture_count=culture_count,
                        other_count=other_count,
                    ):
                        continue
                    for dining_group in combinations(dining, dining_count):
                        for culture_group in combinations(culture, culture_count):
                            other_groups = combinations(other, other_count) if other_count else [()]
                            for other_group in other_groups:
                                group = [*dining_group, *culture_group, *other_group]
                                if (
                                    dining_count > 1
                                    and unique_dining_brand_count >= dining_count
                                    and _has_duplicate_dining_brand(group)
                                ):
                                    continue
                                ordered = _order_group_nearest_neighbor(
                                    group,
                                    distance_matrix,
                                    profile,
                                    start_location=start_location,
                                    start_costs=start_costs,
                                    duration_matrix=duration_matrix,
                                    prefer_local_food=prefer_local_food,
                                )
                                route = _evaluate_route(
                                    ordered,
                                    distance_matrix,
                                    weights,
                                    composition_policy,
                                    duration_budget_min,
                                    profile,
                                    start_location=start_location,
                                    duration_matrix=duration_matrix,
                                    source_matrix=source_matrix,
                                    mode_matrix=mode_matrix,
                                    candidate_modes_matrix=candidate_modes_matrix,
                                    start_costs=start_costs,
                                    route_mode=route_mode,
                                )
                                evaluated.append(route)

    feasible = [route for route in evaluated if route["estimated_duration_min"] <= duration_budget_min]
    soft_feasible = [
        route
        for route in evaluated
        if route["estimated_duration_min"] <= float(duration_budget_min) * 1.15
    ]
    ranked = sorted(feasible or soft_feasible, key=_route_sort_key)
    routes: List[Dict[str, Any]] = []
    used_signatures = set()
    for route in ranked:
        signature = tuple(sorted(poi["id"] for poi in route["pois"]))
        if route_mode == MULTIMODAL_LOW_FRICTION:
            signature = (str(route.get("optimization_profile") or route.get("profile") or ""), *signature)
        if signature in used_signatures:
            continue
        used_signatures.add(signature)
        routes.append(route)
        if len(routes) >= max_options:
            break

    if not routes:
        if evaluated:
            warnings.append("no_route_within_time_budget")
        else:
            warnings.append("no_route_combinations_generated")
    elif not feasible:
        warnings.append("no_route_within_time_budget")

    return {
        "routes": routes,
        "warnings": _unique_list(warnings),
        "diagnostics": _diagnostics(evaluated_count=len(evaluated), feasible_count=len(feasible)),
    }


def has_urban_activity_sequence(urban_intent_profile: Any) -> bool:
    if not isinstance(urban_intent_profile, Mapping):
        return False
    activities = urban_intent_profile.get("activity_sequence")
    return isinstance(activities, list) and bool(activities)


def _poi_matches_activity_slot(poi: Mapping[str, Any], slot_id: str, activity_type: str) -> bool:
    matched_slots = {str(item) for item in _as_list(poi.get("matched_activity_slots")) if str(item)}
    candidate_slots = {str(item) for item in _as_list(poi.get("candidate_activity_slots")) if str(item)}
    if slot_id and (slot_id in matched_slots or slot_id in candidate_slots):
        return True
    activity_types = {str(item) for item in _as_list(poi.get("activity_types")) if str(item)}
    if activity_type and activity_type in activity_types:
        return True
    return bool(activity_type and str(poi.get("activity_type") or "") == activity_type)


def _urban_activity_filler_candidates(
    indexed_pois: Sequence[Mapping[str, Any]],
    activity_order: int,
    reject_bad_filler: bool = True,
) -> List[Dict[str, Any]]:
    candidates = []
    for poi in indexed_pois:
        if opening_status(poi.get("opening_hours")) == "verified_closed":
            continue
        if reject_bad_filler and _is_bad_urban_activity_filler(poi):
            continue
        candidate = dict(poi)
        candidate.setdefault("activity_type", str(candidate.get("activity_type") or "extra_poi"))
        candidate.setdefault("activity_label", str(candidate.get("activity_label") or "route extension"))
        candidate.setdefault("activity_order", activity_order)
        candidate.setdefault(
            "visit_duration_min",
            int(candidate.get("visit_duration_min") or DEFAULT_VISIT_DURATION_MIN.get(candidate.get("category"), 40)),
        )
        candidates.append(candidate)
    candidates.sort(
        key=lambda poi: (
            -float(poi.get("_reward", 0.0) or 0.0),
            float(poi.get("queue_risk", 0.5) or 0.5),
            str(poi.get("name", "")),
        )
    )
    return candidates


def _activity_quality_rule(activity: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    text = _activity_semantic_text(activity)
    for rule in ACTIVITY_QUALITY_RULES:
        if any(term in text for term in rule["slot_terms"]):
            return rule
    return None


def _activity_slot_quality_reasons(
    poi: Mapping[str, Any],
    activity: Mapping[str, Any],
    urban_intent_profile: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    rule = _activity_quality_rule(activity)
    if not rule:
        return []
    primary_text = _poi_primary_text(poi).casefold()
    core_text = _poi_core_text(poi).casefold()
    positive_terms = tuple(rule["positive_terms"])
    reject_terms = tuple(rule["reject_terms"])
    hard_reject_terms = tuple(rule.get("hard_reject_terms") or ())
    reasons: List[str] = []
    matched_hard_reject_terms = [term for term in hard_reject_terms if term in core_text or term in primary_text][:3]
    matched_reject_terms = [term for term in reject_terms if term in core_text or term in primary_text][:3]
    matched_positive_terms = [term for term in positive_terms if term in core_text or term in primary_text]
    if rule.get("warning") == "dining" and str(poi.get("category") or "").casefold() == "dining":
        matched_positive_terms.append("category:dining")
    if matched_hard_reject_terms:
        reasons.append("hard_reject_terms:" + ",".join(matched_hard_reject_terms))
    if matched_reject_terms and not matched_positive_terms:
        reasons.append("reject_terms:" + ",".join(matched_reject_terms))
    if not matched_positive_terms:
        reasons.append("missing_positive_activity_signal")
    if _is_bad_urban_activity_filler(poi) and not matched_positive_terms:
        reasons.append("low_quality_urban_node")
    reasons.extend(_activity_context_reject_reasons(poi, activity, rule, urban_intent_profile))
    return _unique_list(reasons)


def _activity_context_reject_reasons(
    poi: Mapping[str, Any],
    activity: Mapping[str, Any],
    rule: Mapping[str, Any],
    urban_intent_profile: Optional[Mapping[str, Any]],
) -> List[str]:
    if str(rule.get("warning") or "") != "exhibition":
        return []
    if not isinstance(urban_intent_profile, Mapping):
        return []
    profile_text = " ".join(
        str(value)
        for value in (
            _profile_semantic_text(urban_intent_profile),
            urban_intent_profile.get("social_context"),
            urban_intent_profile.get("companions"),
        )
        if value
    ).casefold()
    romantic_scene = any(
        term in profile_text
        for term in ("partner", "romantic", "date", "\u5973\u670b\u53cb", "\u7537\u670b\u53cb", "\u4f34\u4fa3", "\u60c5\u4fa3", "\u7ea6\u4f1a", "\u6d6a\u6f2b")
    )
    if not romantic_scene:
        return []
    primary_text = _poi_primary_text(poi).casefold()
    weak_romantic_exhibition_terms = (
        "youth",
        "science",
        "technology",
        "public service",
        "\u9752\u5c11\u5e74",
        "\u79d1\u6280\u9986",
        "\u79d1\u5b66\u6280\u672f\u9986",
        "\u79d1\u666e",
        "\u6c11\u751f\u5c55\u793a",
        "\u6570\u5b57\u6c11\u751f",
    )
    if any(term in primary_text for term in weak_romantic_exhibition_terms):
        return ["romantic_exhibition_context_mismatch"]
    return []


def activity_slot_fulfillment(
    poi: Mapping[str, Any],
    activity: Mapping[str, Any],
    urban_intent_profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Return explicit evidence that a POI can satisfy a required activity slot."""
    activity_type = str(activity.get("activity_type") or activity.get("type") or "")
    if opening_status(poi.get("opening_hours")) == "verified_closed":
        return {
            "ok": False,
            "score": 0.0,
            "reasons": ["verified_closed"],
            "evidence": [],
            "activity_type": activity_type,
            "hard_rejected": True,
        }
    if _is_citywalk_like_activity(activity):
        ok = _is_citywalk_quality_poi(poi) or _is_citywalk_supplemental_poi(poi)
        reasons = [] if ok else _citywalk_quality_reject_reasons(poi)
        return {
            "ok": ok,
            "score": 0.85 if ok else 0.0,
            "reasons": _unique_list(reasons),
            "evidence": ["citywalk_quality_or_supplemental"] if ok else [],
            "activity_type": activity_type,
            "hard_rejected": bool(reasons),
        }
    rule = _activity_quality_rule(activity)
    if not rule:
        return {
            "ok": True,
            "score": 0.5,
            "reasons": [],
            "evidence": ["no_specific_activity_rule"],
            "activity_type": activity_type,
            "hard_rejected": False,
        }
    primary_text = _poi_primary_text(poi).casefold()
    core_text = _poi_core_text(poi).casefold()
    positive_terms = tuple(rule["positive_terms"])
    reject_terms = tuple(rule["reject_terms"])
    hard_reject_terms = tuple(rule.get("hard_reject_terms") or ())
    matched_positive_terms = [term for term in positive_terms if term in core_text or term in primary_text]
    matched_reject_terms = [term for term in reject_terms if term in core_text or term in primary_text][:3]
    matched_hard_reject_terms = [term for term in hard_reject_terms if term in core_text or term in primary_text][:3]
    reasons = _activity_slot_quality_reasons(poi, activity, urban_intent_profile)
    hard_rejected = bool(matched_hard_reject_terms)
    ok = not reasons
    return {
        "ok": ok,
        "score": round(1.0 if ok else 0.0, 3),
        "reasons": _unique_list(reasons),
        "evidence": _unique_list(
            [
                *(f"positive:{term}" for term in matched_positive_terms[:4]),
                *(f"reject:{term}" for term in matched_reject_terms[:3]),
                *(f"hard_reject:{term}" for term in matched_hard_reject_terms[:3]),
            ]
        ),
        "activity_type": activity_type,
        "hard_rejected": hard_rejected,
    }


def _filter_activity_slot_candidates(
    candidates: Sequence[Mapping[str, Any]],
    activity: Mapping[str, Any],
    urban_intent_profile: Optional[Mapping[str, Any]] = None,
) -> List[Dict[str, Any]]:
    rule = _activity_quality_rule(activity)
    if not rule and not _is_citywalk_like_activity(activity):
        return [dict(poi) for poi in candidates]
    filtered = []
    for poi in candidates:
        fulfillment = activity_slot_fulfillment(poi, activity, urban_intent_profile)
        if fulfillment.get("ok"):
            item = dict(poi)
            item["activity_slot_fulfillment"] = fulfillment
            filtered.append(item)
    return filtered


def _is_connector_filler_candidate(
    poi: Mapping[str, Any],
    activities: Sequence[Mapping[str, Any]],
) -> bool:
    if opening_status(poi.get("opening_hours")) == "verified_closed":
        return False
    if _is_bad_urban_activity_filler(poi):
        return False
    required_types = {
        str(activity.get("activity_type") or activity.get("type") or "").casefold()
        for activity in activities
        if isinstance(activity, Mapping)
    }
    required_slots = {
        str(activity.get("slot_id") or activity.get("id") or f"slot_{int(activity.get('order') or 0)}")
        for activity in activities
        if isinstance(activity, Mapping)
    }
    poi_types = {
        str(item or "").casefold()
        for item in [
            poi.get("activity_type"),
            *(_as_list(poi.get("activity_types"))),
        ]
        if str(item or "")
    }
    matched_slots = {str(item) for item in _as_list(poi.get("matched_activity_slots")) if str(item)}
    candidate_slots = {str(item) for item in _as_list(poi.get("candidate_activity_slots")) if str(item)}
    if (matched_slots | candidate_slots) & required_slots:
        return False
    if poi_types & required_types:
        return False
    if _connector_conflicts_with_scene(poi, activities):
        return False
    if poi_types & {"connector", "short_rest", "rest"}:
        return True
    text = _poi_text(poi).casefold()
    connector_terms = (
        "cafe",
        "coffee",
        "dessert",
        "tea",
        "bookstore",
        "gallery",
        "mall",
        "rest",
        "lounge",
        "lobby",
        "bistro",
        "\u5496\u5561",
        "\u751c\u54c1",
        "\u5976\u8336",
        "\u8336",
        "\u4e66\u5e97",
        "\u7f8e\u672f\u9986",
        "\u753b\u5eca",
        "\u5546\u573a",
        "\u8d2d\u7269\u4e2d\u5fc3",
        "\u4f11\u606f",
        "\u4f11\u95f2",
        "\u5927\u5802\u5427",
        "\u5ba4\u5185",
    )
    category = str(poi.get("category") or "").casefold()
    if category == "culture_entertainment":
        return True
    return any(term in text for term in connector_terms)


def _theme_connector_filler_candidates(
    indexed_pois: Sequence[Mapping[str, Any]],
    activities: Sequence[Mapping[str, Any]],
    activity_order: int,
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for poi in indexed_pois:
        if opening_status(poi.get("opening_hours")) == "verified_closed":
            continue
        if _is_bad_urban_activity_filler(poi):
            continue
        if _connector_conflicts_with_scene(poi, activities):
            continue
        if not _is_theme_connector_candidate(poi, activities):
            continue
        candidate = dict(poi)
        candidate.setdefault("activity_type", "short_theme_stop")
        candidate.setdefault("activity_label", "short thematic stop")
        candidate.setdefault("activity_order", activity_order)
        candidates.append(candidate)
    candidates.sort(
        key=lambda poi: (
            -float(poi.get("_reward", 0.0) or 0.0),
            float(poi.get("queue_risk", 0.5) or 0.5),
            str(poi.get("name", "")),
        )
    )
    return candidates


def _soft_connector_filler_candidates(
    indexed_pois: Sequence[Mapping[str, Any]],
    activities: Sequence[Mapping[str, Any]],
    activity_order: int,
) -> List[Dict[str, Any]]:
    """Relaxed filler pool used only after required activity slots already have candidates."""
    candidates: List[Dict[str, Any]] = []
    for poi in indexed_pois:
        if opening_status(poi.get("opening_hours")) == "verified_closed":
            continue
        if _is_bad_urban_activity_filler(poi):
            continue
        if _connector_conflicts_with_scene(poi, activities):
            continue
        candidate = dict(poi)
        candidate.setdefault("activity_type", "soft_route_extension")
        candidate.setdefault("activity_label", "soft route extension")
        candidate.setdefault("activity_order", activity_order)
        candidates.append(candidate)
    candidates.sort(
        key=lambda poi: (
            -float(poi.get("_reward", 0.0) or 0.0),
            float(poi.get("queue_risk", 0.5) or 0.5),
            str(poi.get("name", "")),
        )
    )
    return candidates


def _is_theme_connector_candidate(
    poi: Mapping[str, Any],
    activities: Sequence[Mapping[str, Any]],
) -> bool:
    category = str(poi.get("category") or "").casefold()
    text = _poi_text(poi).casefold()
    if category == "culture_entertainment" and any(
        term in text
        for term in (
            "gallery",
            "museum",
            "art",
            "exhibition",
            "bookstore",
            "\u7f8e\u672f\u9986",
            "\u535a\u7269\u9986",
            "\u753b\u5eca",
            "\u827a\u672f",
            "\u5c55\u89c8",
            "\u4e66\u5e97",
            "\u6587\u5316",
        )
    ):
        return True
    for activity in activities:
        if not isinstance(activity, Mapping):
            continue
        rule = _activity_quality_rule(activity)
        if not rule:
            continue
        warning = str(rule.get("warning") or "")
        if warning not in {"drinks", "exhibition", "beauty"}:
            continue
        fulfillment = activity_slot_fulfillment(poi, activity)
        if fulfillment.get("ok"):
            return True
    return False


def _connector_conflicts_with_scene(
    poi: Mapping[str, Any],
    activities: Sequence[Mapping[str, Any]],
) -> bool:
    scene_text = " ".join(_activity_semantic_text(activity) for activity in activities if isinstance(activity, Mapping))
    poi_text = _poi_text(poi).casefold()
    primary_text = _poi_primary_text(poi).casefold()
    has_drinks_scene = any(
        term in scene_text
        for term in ("drink", "drinks", "bar", "wine", "beer", "\u5c0f\u9152", "\u5c0f\u914c", "\u9152\u9986", "\u9152\u5427")
    )
    if has_drinks_scene:
        chain_cafe_terms = ("starbucks", "\u661f\u5df4\u514b", "luckin", "\u745e\u5e78", "\u5496\u5561", "coffee")
        alcohol_or_atmosphere_terms = (
            "bar",
            "wine",
            "beer",
            "bistro",
            "lounge",
            "\u9152",
            "\u5427",
            "\u5927\u5802\u5427",
            "\u6c1b\u56f4",
            "\u5b89\u9759",
        )
        if any(term in primary_text for term in chain_cafe_terms) and not any(term in poi_text for term in alcohol_or_atmosphere_terms):
            return True
    return False


def _route_activity_quality_violations(
    route_pois: Sequence[Mapping[str, Any]],
    activities: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    violations: List[Dict[str, Any]] = []
    for index, activity in enumerate(activities):
        if index >= len(route_pois):
            break
        rule = _activity_quality_rule(activity)
        hard_rule = isinstance(rule, Mapping) and str(rule.get("warning") or "") in {"wellness", "drinks", "exhibition", "beauty"}
        if not hard_rule and not _is_citywalk_like_activity(activity):
            continue
        poi = route_pois[index]
        fulfillment = activity_slot_fulfillment(poi, activity)
        reasons = _as_list(fulfillment.get("reasons"))
        if not fulfillment.get("ok"):
            violations.append(
                {
                    "activity_index": index,
                    "activity_type": activity.get("activity_type") or activity.get("type"),
                    "activity_label": activity.get("activity_label") or activity.get("label"),
                    "poi_name": poi.get("name"),
                    "reasons": reasons or ["activity_fulfillment_failed"],
                    "evidence": fulfillment.get("evidence", []),
                }
            )
    return violations


def _is_bad_urban_activity_filler(poi: Mapping[str, Any]) -> bool:
    core_text = _poi_core_text(poi).casefold()
    primary_text = _poi_primary_text(poi).casefold()
    category = str(poi.get("category") or "").casefold()
    activity_text = " ".join(
        str(item or "")
        for item in (
            poi.get("activity_type"),
            poi.get("activity_label"),
            poi.get("micro_category"),
            *(_as_list(poi.get("activity_types"))),
            *(_as_list(poi.get("recall_sources"))),
            *(_as_list(poi.get("recall_keywords"))),
        )
    ).casefold()
    has_citywalk_signal = category == "culture_entertainment" and any(
        term in activity_text for term in CITYWALK_ACTIVITY_TYPE_TERMS
    )
    core_reject_terms = tuple(
        term
        for term in URBAN_ACTIVITY_REJECT_TERMS
        if term not in {"subway", "station", "\u5730\u94c1"}
    )
    transit_primary_reject_terms = ("subway", "station", "\u5730\u94c1")
    if has_citywalk_signal:
        return any(term in primary_text for term in URBAN_ACTIVITY_REJECT_TERMS)
    if any(term in core_text or term in primary_text for term in core_reject_terms):
        return True
    if any(term in primary_text for term in transit_primary_reject_terms):
        return True
    if category in {"transportation", "parking", "government"}:
        return True
    return False


def _is_citywalk_quality_poi(poi: Mapping[str, Any]) -> bool:
    text = _poi_text(poi).casefold()
    primary_text = _poi_primary_text(poi).casefold()
    category = str(poi.get("category") or "").casefold()
    if _citywalk_reject_terms(poi):
        return False
    if any(term in primary_text for term in CITYWALK_COMMERCIAL_MAIN_REJECT_TERMS):
        return False
    accessibility_type = _poi_accessibility_type(poi)
    status = opening_status(poi.get("opening_hours"))
    if accessibility_type == "requires_opening_hours" and status == "unknown":
        return False
    if category == "culture_entertainment":
        return True
    if accessibility_type in {"always_accessible_public_space", "view_only_landmark"}:
        return True
    if any(term in text for term in CITYWALK_REST_STOP_TERMS):
        return True
    if category == "dining":
        return False
    activity_text = " ".join(
        str(item or "")
        for item in (
            poi.get("activity_type"),
            poi.get("activity_label"),
            poi.get("micro_category"),
            *(_as_list(poi.get("activity_types"))),
            *(_as_list(poi.get("recall_sources"))),
            *(_as_list(poi.get("recall_keywords"))),
        )
    ).casefold()
    if any(term in activity_text for term in CITYWALK_ACTIVITY_TYPE_TERMS):
        return True
    return any(term in text for term in CITYWALK_POI_TERMS)


def _citywalk_quality_reject_reasons(poi: Mapping[str, Any]) -> List[str]:
    reasons: List[str] = []
    text = _poi_text(poi).casefold()
    core_text = _poi_core_text(poi).casefold()
    category = str(poi.get("category") or "").casefold()
    matched_reject_terms = _citywalk_reject_terms(poi)[:3]
    if matched_reject_terms:
        reasons.append("reject_terms:" + ",".join(matched_reject_terms))
    accessibility_type = _poi_accessibility_type(poi)
    status = opening_status(poi.get("opening_hours"))
    if accessibility_type == "requires_opening_hours" and status == "unknown":
        reasons.append("requires_opening_hours_unknown")
    if category == "dining":
        reasons.append("dining_not_citywalk_main")
    primary_text = _poi_primary_text(poi).casefold()
    matched_commercial_terms = [term for term in CITYWALK_COMMERCIAL_MAIN_REJECT_TERMS if term in primary_text][:3]
    if matched_commercial_terms:
        reasons.append("commercial_main_terms:" + ",".join(matched_commercial_terms))
    if any(term in core_text for term in ACCESS_STRICT_REQUIRES_OPENING_TERMS):
        reasons.append("strict_opening_sensitive_term")
    activity_text = " ".join(
        str(item or "")
        for item in (
            poi.get("activity_type"),
            poi.get("activity_label"),
            poi.get("micro_category"),
            *(_as_list(poi.get("activity_types"))),
            *(_as_list(poi.get("matched_activity_slots"))),
            *(_as_list(poi.get("candidate_activity_slots"))),
            *(_as_list(poi.get("recall_sources"))),
            *(_as_list(poi.get("recall_keywords"))),
        )
    ).casefold()
    if not any(term in activity_text for term in CITYWALK_ACTIVITY_TYPE_TERMS):
        reasons.append("missing_citywalk_activity_signal")
    if not any(term in text for term in CITYWALK_POI_TERMS) and accessibility_type not in {"always_accessible_public_space", "view_only_landmark"}:
        reasons.append("missing_citywalk_poi_or_accessibility_signal")
    if not reasons and not _is_citywalk_quality_poi(poi):
        reasons.append("not_citywalk_quality")
    return _unique_list(reasons)


def _citywalk_reject_terms(poi: Mapping[str, Any]) -> List[str]:
    core_text = _poi_core_text(poi).casefold()
    primary_text = _poi_primary_text(poi).casefold()
    matched = [term for term in CITYWALK_NON_TRANSIT_REJECT_TERMS if term in core_text]
    matched.extend(term for term in CITYWALK_TRANSIT_REJECT_TERMS if term in primary_text)
    return _unique_list(matched)


def _citywalk_candidate_debug_sample(pois: Sequence[Mapping[str, Any]], limit: int = 15) -> List[Dict[str, Any]]:
    sample: List[Dict[str, Any]] = []
    for poi in list(pois)[:limit]:
        sample.append(
            {
                "name": poi.get("name"),
                "type": poi.get("type"),
                "category": poi.get("category"),
                "accessibility_type": _poi_accessibility_type(poi),
                "opening_status": opening_status(poi.get("opening_hours")),
                "activity_type": poi.get("activity_type"),
                "activity_types": poi.get("activity_types", []),
                "matched_activity_slots": poi.get("matched_activity_slots", []),
                "candidate_activity_slots": poi.get("candidate_activity_slots", []),
                "recall_sources": poi.get("recall_sources", []),
                "recall_keywords": poi.get("recall_keywords", []),
                "is_citywalk_quality": _is_citywalk_quality_poi(poi),
                "is_citywalk_supplemental": _is_citywalk_supplemental_poi(poi),
                "reject_reasons": _citywalk_quality_reject_reasons(poi),
            }
        )
    return sample


def _poi_accessibility_type(poi: Mapping[str, Any]) -> str:
    explicit = str(poi.get("accessibility_type") or "").strip()
    if explicit:
        return explicit
    text = _poi_text(poi).casefold()
    primary_text = _poi_primary_text(poi).casefold()
    if any(term in text for term in ACCESS_STRICT_REQUIRES_OPENING_TERMS):
        return "requires_opening_hours"
    if any(term in text for term in ACCESS_REQUIRES_OPENING_TERMS):
        if any(term in primary_text for term in ACCESS_VIEW_ONLY_TERMS):
            return "view_only_landmark"
        return "requires_opening_hours"
    if any(term in primary_text for term in ACCESS_PUBLIC_SPACE_TERMS):
        return "always_accessible_public_space"
    if any(term in primary_text for term in ACCESS_VIEW_ONLY_TERMS):
        return "view_only_landmark"
    if str(poi.get("category") or "").casefold() == "dining":
        return "requires_opening_hours"
    return "unknown_accessibility"


def _citywalk_quality_candidates(pois: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(poi) for poi in pois if _is_citywalk_quality_poi(poi)]


def _is_citywalk_supplemental_poi(poi: Mapping[str, Any]) -> bool:
    if _is_citywalk_quality_poi(poi):
        return True
    text = _poi_text(poi).casefold()
    core_text = _poi_core_text(poi).casefold()
    primary_text = _poi_primary_text(poi).casefold()
    if _citywalk_reject_terms(poi):
        return False
    if any(term in primary_text for term in CITYWALK_COMMERCIAL_MAIN_REJECT_TERMS):
        return False
    if any(term in core_text for term in ACCESS_STRICT_REQUIRES_OPENING_TERMS):
        return False
    if str(poi.get("category") or "").casefold() == "dining":
        return False
    accessibility_type = _poi_accessibility_type(poi)
    if accessibility_type == "requires_opening_hours" and opening_status(poi.get("opening_hours")) == "unknown":
        return False
    activity_text = " ".join(
        str(item or "")
        for item in (
            poi.get("activity_type"),
            poi.get("activity_label"),
            poi.get("micro_category"),
            *(_as_list(poi.get("activity_types"))),
            *(_as_list(poi.get("matched_activity_slots"))),
            *(_as_list(poi.get("candidate_activity_slots"))),
            *(_as_list(poi.get("recall_sources"))),
            *(_as_list(poi.get("recall_keywords"))),
        )
    ).casefold()
    return any(term in activity_text for term in CITYWALK_ACTIVITY_TYPE_TERMS)


def _citywalk_candidate_key(poi: Mapping[str, Any]) -> str:
    return str(poi.get("id") or poi.get("name") or poi.get("location") or "")


def _clean_time_budget_warnings(
    warnings: Sequence[Any],
    route_options: Sequence[Mapping[str, Any]], 
    duration_budget_min: Any,
) -> List[str]:
    if not _has_route_within_budget(route_options, duration_budget_min):
        return _unique_list(str(item) for item in warnings)
    removable = {"no_urban_activity_route_within_time_budget", "no_route_within_time_budget", "route_exceeds_time_budget"}
    return _unique_list(str(item) for item in warnings if str(item) not in removable)


def _clean_route_time_budget_warnings(
    warnings: Sequence[Any],
    route: Mapping[str, Any],
    duration_budget_min: Any,
) -> List[str]:
    if not _has_route_within_budget([route], duration_budget_min):
        return _unique_list(str(item) for item in warnings)
    return _unique_list(str(item) for item in warnings if str(item) != "route_exceeds_time_budget")


def _has_route_within_budget(route_options: Sequence[Mapping[str, Any]], duration_budget_min: Any) -> bool:
    try:
        budget = float(duration_budget_min)
    except (TypeError, ValueError):
        return False
    if budget <= 0:
        return False
    for route in route_options or []:
        if not isinstance(route, Mapping):
            continue
        metrics = route.get("metrics") if isinstance(route.get("metrics"), Mapping) else {}
        duration = _first_non_empty(
            route.get("estimated_duration_min"),
            route.get("total_minutes"),
            metrics.get("estimated_duration_min"),
            metrics.get("total_minutes"),
        )
        try:
            if float(duration) <= budget + 0.5:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _activity_semantic_text(activity: Mapping[str, Any]) -> str:
    values: List[str] = []
    for key in (
        "activity_type",
        "type",
        "activity_label",
        "label",
        "activity_group",
        "poi_category",
        "micro_category",
    ):
        value = activity.get(key)
        if value not in (None, ""):
            values.append(str(value))
    for key in ("poi_keywords", "keywords", "tags", "soft_preferences"):
        values.extend(str(item) for item in _as_list(activity.get(key)) if str(item))
    return " ".join(values).casefold()


def _profile_semantic_text(urban_intent_profile: Any) -> str:
    if not isinstance(urban_intent_profile, Mapping):
        return ""
    values: List[str] = []
    for key in ("scenario", "scenario_label", "intent_type", "rewritten_query", "query"):
        value = urban_intent_profile.get(key)
        if value not in (None, ""):
            values.append(str(value))
    activities = urban_intent_profile.get("activity_sequence")
    if isinstance(activities, list):
        for activity in activities:
            if isinstance(activity, Mapping):
                values.append(_activity_semantic_text(activity))
    return " ".join(values).casefold()


def _normalize_activity_for_route(activity: Mapping[str, Any], profile_text: str) -> Dict[str, Any]:
    normalized = dict(activity)
    activity_text = _activity_semantic_text(normalized)
    combined = activity_text.casefold()
    activity_type = str(normalized.get("activity_type") or normalized.get("type") or "").casefold()
    drink_terms = (
        "drink",
        "drinks",
        "bar",
        "wine",
        "pub",
        "cocktail",
        "beer",
        "bistro",
        "\u5c0f\u9152",
        "\u5c0f\u914c",
        "\u9152\u9986",
        "\u9152\u5427",
        "\u6e05\u5427",
        "\u7cbe\u917f",
        "\u5fae\u91ba",
        "\u559d\u9152",
    )
    wellness_terms = (
        "massage",
        "spa",
        "foot_spa",
        "tuina",
        "\u6309\u6469",
        "\u8db3\u7597",
        "\u8db3\u6d74",
        "\u63a8\u62ff",
        "\u517b\u751f",
        "\u7406\u7597",
        "\u6c34\u7597",
    )
    exhibition_terms = (
        "exhibition",
        "gallery",
        "museum",
        "art museum",
        "\u5c55\u89c8",
        "\u770b\u5c55",
        "\u5c55\u9986",
        "\u7f8e\u672f\u9986",
        "\u535a\u7269\u9986",
        "\u753b\u5eca",
        "\u827a\u672f\u9986",
    )
    beauty_terms = (
        "nail",
        "manicure",
        "beauty",
        "\u7f8e\u7532",
        "\u505a\u6307\u7532",
        "\u7f8e\u776b",
    )
    late_food_terms = (
        "late_night_food",
        "late night food",
        "night snack",
        "midnight snack",
        "\u591c\u5bb5",
        "\u6df1\u591c\u98df\u5802",
        "\u665a\u4e0a\u5403",
    )
    dining_like = {"social_dining", "dining", "dinner", "food", "meal", "restaurant"}
    relax_like = {"relax", "relaxation", "wellness", "rest"}
    culture_like = {"culture", "cultural_sightseeing", "culture_sightseeing", "sightseeing", "activity", "other"}
    beauty_like = {"beauty", "activity", "other", "shopping"}
    if any(term in combined for term in late_food_terms) and (
        activity_type in dining_like or "food" in activity_type or not activity_type
    ):
        normalized["activity_type"] = "late_night_food"
        normalized["type"] = "late_night_food"
    elif any(term in combined for term in drink_terms) and (
        activity_type in dining_like or "social_dining" in activity_type or not activity_type
    ):
        normalized["activity_type"] = "drinks"
        normalized["type"] = "drinks"
    elif any(term in combined for term in wellness_terms) and (
        activity_type in relax_like or "relax" in activity_type or not activity_type
    ):
        normalized["activity_type"] = "wellness"
        normalized["type"] = "wellness"
    elif any(term in combined for term in exhibition_terms) and (
        activity_type in culture_like or "culture" in activity_type or not activity_type
    ):
        normalized["activity_type"] = "museum_exhibition"
        normalized["type"] = "museum_exhibition"
    elif any(term in combined for term in beauty_terms) and (
        activity_type in beauty_like or not activity_type
    ):
        normalized["activity_type"] = "beauty"
        normalized["type"] = "beauty"
    return normalized


def _is_citywalk_like_activity(activity: Mapping[str, Any]) -> bool:
    text = _activity_semantic_text(activity)
    return any(term in text for term in CITYWALK_SEMANTIC_TERMS)


def _has_strong_citywalk_signal(urban_intent_profile: Any) -> bool:
    if not isinstance(urban_intent_profile, Mapping):
        return False
    values: List[str] = []
    for key in ("scenario", "scenario_label", "intent_type", "rewritten_query", "query"):
        value = urban_intent_profile.get(key)
        if value not in (None, ""):
            values.append(str(value))
    text = " ".join(values).casefold()
    return any(term in text for term in CITYWALK_STRONG_TERMS)


def _has_primary_non_citywalk_activity(activities: Sequence[Mapping[str, Any]], strong_citywalk: bool = False) -> bool:
    for activity in activities:
        activity_type = str(activity.get("activity_type") or activity.get("type") or "").casefold()
        if strong_citywalk and activity_type in CITYWALK_SUPPORT_ACTIVITY_TYPES:
            continue
        text = _activity_semantic_text(activity)
        if any(term in text for term in NON_CITYWALK_PRIMARY_TERMS):
            return True
    return False


def _activity_diagnostics(activities: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    type_counts: Dict[str, int] = {}
    labels: List[str] = []
    for activity in activities:
        activity_type = str(activity.get("activity_type") or activity.get("type") or "unknown")
        type_counts[activity_type] = type_counts.get(activity_type, 0) + 1
        label = str(activity.get("activity_label") or activity.get("label") or activity_type)
        if label:
            labels.append(label)
    return {
        "activity_type_counts": type_counts,
        "activity_labels": _unique_list(labels),
    }


def _urban_visit_duration_cap(activity: Mapping[str, Any], category: Any, duration_budget_min: Any) -> int:
    try:
        budget = int(float(duration_budget_min or 180))
    except (TypeError, ValueError):
        budget = 180
    text = _activity_semantic_text(activity)
    category_text = str(category or "").casefold()
    short_trip = budget <= 210

    if _is_citywalk_like_activity(activity) or any(term in text for term in ("photo", "scenic", "culture", "rest")):
        return 30 if short_trip else 45
    if any(term in text for term in ("late_night_food", "night_food", "late food", "snack", "\u591c\u5bb5", "\u5c0f\u5403")):
        return 40 if short_trip else 60
    if any(term in text for term in ("dinner", "dining", "food", "restaurant", "meal", "\u665a\u996d", "\u5403\u996d", "\u7f8e\u98df")):
        return 55 if short_trip else 90
    if any(term in text for term in ("cafe", "coffee", "dessert", "drink", "bar", "\u5496\u5561", "\u751c\u54c1", "\u5c0f\u9152")):
        return 45 if short_trip else 60
    if any(term in text for term in ("wellness", "massage", "spa", "\u6309\u6469", "\u8db3\u7597")):
        return 50 if short_trip else 90
    if any(term in text for term in ("nail", "\u7f8e\u7532")):
        return 60 if short_trip else 90
    if category_text == "dining":
        return 45 if short_trip else 75
    return 35 if short_trip else 50


def _cap_urban_visit_duration(raw_duration: Any, activity: Mapping[str, Any], category: Any, duration_budget_min: Any) -> int:
    try:
        minutes = int(float(raw_duration))
    except (TypeError, ValueError):
        minutes = DEFAULT_VISIT_DURATION_MIN.get(str(category or "other"), 40)
    cap = _urban_visit_duration_cap(activity, category, duration_budget_min)
    return max(20, min(minutes, cap))


def urban_activity_policy(urban_intent_profile: Any, base_policy: Mapping[str, Any]) -> Dict[str, Any]:
    activities = urban_intent_profile.get("activity_sequence") if isinstance(urban_intent_profile, Mapping) else []
    count = len(activities) if isinstance(activities, list) else 0
    policy = dict(base_policy or {})
    has_walk_signal = any(
        isinstance(activity, Mapping) and _is_citywalk_like_activity(activity)
        for activity in activities
    )
    strong_citywalk = _has_strong_citywalk_signal(urban_intent_profile)
    has_primary_activity = _has_primary_non_citywalk_activity(
        [activity for activity in activities if isinstance(activity, Mapping)],
        strong_citywalk=strong_citywalk,
    )
    citywalk_activity = (strong_citywalk or has_walk_signal) and not has_primary_activity
    single_citywalk = (
        count == 1
        and isinstance(activities[0], Mapping)
        and _is_citywalk_like_activity(activities[0])
    )
    try:
        duration_budget = int(float(policy.get("duration_budget_min") or 180))
    except (TypeError, ValueError):
        duration_budget = 180
    target_route_size = _target_urban_activity_route_size(duration_budget, activities)
    route_size_min = max(MIN_ROUTE_POIS, min(count, target_route_size))
    route_size_max = max(route_size_min, target_route_size)
    if citywalk_activity:
        route_size_min = MIN_ROUTE_POIS
        route_size_max = MIN_ROUTE_POIS
        target_route_size = MIN_ROUTE_POIS
    policy.update(
        {
            "policy_type": "urban_activity",
            "route_size_min": route_size_min,
            "route_size_max": route_size_max,
            "min_pois": route_size_min,
            "target_route_size": target_route_size,
            "weather_context": urban_intent_profile.get("weather_context", {}) if isinstance(urban_intent_profile, Mapping) else {},
            "required_categories": [],
            "allowed_category_compositions": [],
            "profiles": ["urban_activity", "efficient", "low_queue"],
            "single_activity_citywalk": single_citywalk,
            "flexible_citywalk": citywalk_activity,
        }
    )
    return policy


def _target_urban_activity_route_size(duration_budget_min: Any, activities: Sequence[Any]) -> int:
    try:
        budget = int(float(duration_budget_min or 180))
    except (TypeError, ValueError):
        budget = 180
    required_count = sum(1 for item in activities if isinstance(item, Mapping) and item.get("required", True) is not False)
    if budget <= 240:
        target = MIN_ROUTE_POIS
    elif budget < 360:
        target = 4
    else:
        target = 5

    low_intensity = any(
        isinstance(item, Mapping)
        and (
            str(item.get("intensity") or "").casefold() in {"low", "easy", "light"}
            or bool((item.get("soft_preferences") if isinstance(item.get("soft_preferences"), Mapping) else {}).get("low_intensity"))
        )
        for item in activities
    )
    if low_intensity and budget < 360:
        target = min(target, 4)
    return max(MIN_ROUTE_POIS, required_count, target)


def _fallback_urban_activity_route_size(duration_budget_min: Any, activities: Sequence[Any]) -> int:
    target = _target_urban_activity_route_size(duration_budget_min, activities)
    try:
        budget = int(float(duration_budget_min or 180))
    except (TypeError, ValueError):
        budget = 180
    if budget >= 360:
        return max(MIN_ROUTE_POIS, min(target, 4))
    if budget > 240:
        return max(MIN_ROUTE_POIS, min(target, 3))
    return MIN_ROUTE_POIS


def _urban_filler_visit_duration(duration_budget_min: Any, filler_index: int, target_route_size: int) -> int:
    try:
        budget = int(float(duration_budget_min or 180))
    except (TypeError, ValueError):
        budget = 180
    if budget <= 180:
        return 15
    if budget <= 240:
        return 20
    if budget < 360:
        return 40 if filler_index == 0 else 30
    return 45 if target_route_size >= 5 and filler_index < 2 else 30


def solve_urban_activity_routes(
    pois: Sequence[Mapping[str, Any]],
    route_costs: Mapping[str, Any],
    weights: Mapping[str, Any],
    composition_policy: Mapping[str, Any],
    urban_intent_profile: Mapping[str, Any],
    start_location: Optional[Mapping[str, Any]],
    route_mode: str,
    max_options: int = 3,
) -> Dict[str, Any]:
    indexed_pois = [dict(poi, _matrix_index=index) for index, poi in enumerate(pois)]
    activities = [
        dict(item)
        for item in urban_intent_profile.get("activity_sequence", [])
        if isinstance(item, Mapping)
    ]
    activities.sort(key=lambda item: int(item.get("order") or 999))
    profile_text = _profile_semantic_text(urban_intent_profile)
    activities = [_normalize_activity_for_route(activity, profile_text) for activity in activities]
    warnings: List[str] = []
    activity_diag = _activity_diagnostics(activities)
    slot_candidate_counts: Dict[str, int] = {}
    if not activities:
        return {
            "routes": [],
            "warnings": ["missing_activity_sequence"],
            "diagnostics": {"policy_type": "urban_activity", **activity_diag},
        }

    candidate_groups: List[List[Dict[str, Any]]] = []
    if composition_policy.get("flexible_citywalk"):
        raw_citywalk_candidates = _urban_activity_filler_candidates(
            indexed_pois,
            activity_order=1,
            reject_bad_filler=False,
        )
        citywalk_candidates = _citywalk_quality_candidates(raw_citywalk_candidates)
        raw_quality_count = len(citywalk_candidates)
        selected_keys = {_citywalk_candidate_key(poi) for poi in citywalk_candidates}
        supplemental_citywalk_candidates = [
            dict(poi)
            for poi in raw_citywalk_candidates
            if _citywalk_candidate_key(poi) not in selected_keys and _is_citywalk_supplemental_poi(poi)
        ]
        citywalk_quality_relaxed = False
        if len(citywalk_candidates) < MIN_ROUTE_POIS and len(citywalk_candidates) >= MIN_ROUTE_POIS - 1:
            needed = MIN_ROUTE_POIS - len(citywalk_candidates)
            if len(supplemental_citywalk_candidates) >= needed:
                citywalk_candidates = [
                    *citywalk_candidates,
                    *supplemental_citywalk_candidates[:needed],
                ]
                citywalk_quality_relaxed = True
                warnings.append("citywalk_quality_relaxed_with_supplemental_pois")
        slot_candidate_counts["flexible_citywalk_raw"] = len(raw_citywalk_candidates)
        slot_candidate_counts["flexible_citywalk_quality_raw"] = raw_quality_count
        slot_candidate_counts["flexible_citywalk_quality"] = len(citywalk_candidates)
        slot_candidate_counts["flexible_citywalk_supplemental"] = len(supplemental_citywalk_candidates)
        for candidate in citywalk_candidates:
            candidate["activity_type"] = candidate.get("activity_type") or "citywalk"
            candidate["activity_label"] = candidate.get("activity_label") or "citywalk"
            candidate["activity_order"] = 1
            candidate["visit_duration_min"] = _cap_urban_visit_duration(
                candidate.get("visit_duration_min"),
                candidate,
                candidate.get("category"),
                composition_policy.get("duration_budget_min"),
            )
        if len(citywalk_candidates) < MIN_ROUTE_POIS:
            return {
                "routes": [],
                "warnings": ["citywalk_poi_quality_insufficient"],
                "diagnostics": {
                    "policy_type": "urban_activity",
                    "activity_count": len(activities),
                    "candidate_count": len(indexed_pois),
                    "citywalk_candidate_count": len(raw_citywalk_candidates),
                    "citywalk_quality_candidate_count": len(citywalk_candidates),
                    "citywalk_quality_candidate_count_raw": raw_quality_count,
                    "citywalk_supplemental_candidate_count": len(supplemental_citywalk_candidates),
                    "citywalk_quality_relaxed": citywalk_quality_relaxed,
                    "citywalk_min_quality_pois": CITYWALK_MIN_QUALITY_POIS,
                    "citywalk_candidate_debug_sample": _citywalk_candidate_debug_sample(raw_citywalk_candidates),
                    "slot_candidate_counts": slot_candidate_counts,
                    "evaluated_route_count": 0,
                    "feasible_route_count": 0,
                    **activity_diag,
                },
            }
        candidate_groups = [[dict(poi) for poi in citywalk_candidates[:8]] for _ in range(MIN_ROUTE_POIS)]
        activities_for_route = [dict(activities[0], duration_min=45)] if activities else []
        route_group_sets = [candidate_groups]
    else:
        active_activities: List[Dict[str, Any]] = []
        for activity in activities:
            slot_id = str(activity.get("slot_id") or activity.get("id") or f"slot_{int(activity.get('order') or len(candidate_groups) + 1)}")
            activity_type = str(activity.get("activity_type") or activity.get("type") or "")
            required_activity = activity.get("required", True) is not False
            candidates = [
                dict(poi)
                for poi in indexed_pois
                if _poi_matches_activity_slot(poi, slot_id, activity_type)
                and opening_status(poi.get("opening_hours")) != "verified_closed"
            ]
            if not candidates:
                candidates = [
                    dict(poi)
                    for poi in indexed_pois
                    if activity_type in _poi_text(poi).casefold() and opening_status(poi.get("opening_hours")) != "verified_closed"
                ]
            if candidates and _is_citywalk_like_activity(activity):
                quality_candidates = [
                    dict(poi)
                    for poi in candidates
                    if _is_citywalk_quality_poi(poi) or _is_citywalk_supplemental_poi(poi)
                ]
                if quality_candidates:
                    candidates = quality_candidates
                    warnings.append(f"walk_slot_quality_filtered:{activity_type or slot_id}")
                else:
                    candidates = []
                    warnings.append(f"walk_slot_low_quality_candidates_rejected:{activity_type or slot_id}")
            if not candidates and _is_citywalk_like_activity(activity):
                walk_slot_pool = [
                    poi
                    for poi in indexed_pois
                    if opening_status(poi.get("opening_hours")) != "verified_closed"
                ]
                candidates = _citywalk_quality_candidates(walk_slot_pool)
                if candidates:
                    warnings.append(f"walk_slot_citywalk_quality_fill:{activity_type or slot_id}")
                else:
                    candidates = [
                        dict(poi)
                        for poi in walk_slot_pool
                        if _is_citywalk_supplemental_poi(poi)
                    ]
                    if candidates:
                        warnings.append(f"walk_slot_citywalk_supplemental_fill:{activity_type or slot_id}")
            slot_key = f"{int(activity.get('order') or len(candidate_groups) + 1)}:{activity_type or slot_id}"
            slot_candidate_counts[f"{slot_key}:raw"] = len(candidates)
            if candidates and not _is_citywalk_like_activity(activity):
                quality_candidates = _filter_activity_slot_candidates(candidates, activity, urban_intent_profile)
                if len(quality_candidates) < len(candidates):
                    warnings.append(f"activity_slot_quality_filtered:{activity_type or slot_id}")
                    slot_candidate_counts[f"{slot_key}:quality_rejected"] = len(candidates) - len(quality_candidates)
                candidates = quality_candidates
                if not candidates and _activity_quality_rule(activity):
                    warnings.append(f"activity_slot_low_quality_candidates_rejected:{activity_type or slot_id}")
            slot_candidate_counts[slot_key] = len(candidates)
            if not candidates:
                if not required_activity:
                    warnings.append("optional_activity_slot_empty")
                    warnings.append(f"missing_optional_candidates_for_activity:{activity_type}")
                    continue
                warnings.append("required_activity_slot_empty")
                warnings.append(f"missing_candidates_for_activity:{activity_type}")
                return {
                    "routes": [],
                    "warnings": _unique_list(warnings),
                    "diagnostics": {
                        "policy_type": "urban_activity",
                        "activity_count": len(activities),
                        "candidate_count": len(indexed_pois),
                        "slot_candidate_counts": slot_candidate_counts,
                        "citywalk_candidate_debug_sample": _citywalk_candidate_debug_sample(indexed_pois),
                        "evaluated_route_count": 0,
                        "feasible_route_count": 0,
                        **activity_diag,
                    },
                }
            for candidate in candidates:
                candidate["activity_type"] = activity_type
                candidate["activity_label"] = activity.get("activity_label") or activity.get("label") or candidate.get("activity_label") or activity_type
                candidate["matched_activity_slots"] = _unique_list([*_as_list(candidate.get("matched_activity_slots")), slot_id])
                candidate["activity_order"] = int(activity.get("order") or len(candidate_groups) + 1)
                candidate["visit_duration_min"] = _cap_urban_visit_duration(
                    activity.get("duration_min") or activity.get("max_duration_min") or candidate.get("visit_duration_min") or 60,
                    {
                        **dict(activity),
                        "activity_type": activity_type,
                        "activity_label": candidate["activity_label"],
                    },
                    candidate.get("category"),
                    composition_policy.get("duration_budget_min"),
                )
            candidates.sort(key=lambda poi: (-float(poi.get("_reward", 0.0) or 0.0), float(poi.get("queue_risk", 0.5) or 0.5), str(poi.get("name", ""))))
            candidate_groups.append(candidates[:6])
            active_activities.append(activity)

        route_group_sets = [candidate_groups]
        if composition_policy.get("single_activity_citywalk") and candidate_groups:
            candidate_count = len(candidate_groups[0])
            requested_min = max(MIN_ROUTE_POIS, int(composition_policy.get("route_size_min") or MIN_ROUTE_POIS))
            min_size = min(candidate_count, requested_min)
            max_size = min(max(MIN_ROUTE_POIS, int(composition_policy.get("route_size_max") or MIN_ROUTE_POIS)), candidate_count)
            route_group_sets = [[candidate_groups[0] for _ in range(size)] for size in range(min_size, max_size + 1)]
        else:
            requested_min = max(MIN_ROUTE_POIS, int(composition_policy.get("route_size_min") or MIN_ROUTE_POIS))
            requested_target = max(
                requested_min,
                int(composition_policy.get("target_route_size") or composition_policy.get("route_size_max") or requested_min),
            )
            requested_target = min(
                requested_target,
                max(requested_min, int(composition_policy.get("route_size_max") or requested_target)),
            )
            filler_slots = max(0, requested_target - len(candidate_groups))
            if filler_slots:
                filler_candidates = _urban_activity_filler_candidates(indexed_pois, activity_order=len(candidate_groups) + 1)
                filler_candidates = [
                    candidate
                    for candidate in filler_candidates
                    if _is_connector_filler_candidate(candidate, active_activities)
                ]
                slot_candidate_counts["filler"] = len(filler_candidates)
                if len(filler_candidates) < filler_slots:
                    relaxed_fillers = _theme_connector_filler_candidates(
                        indexed_pois,
                        activities,
                        activity_order=len(candidate_groups) + 1,
                    )
                    existing_filler_keys = {_citywalk_candidate_key(candidate) for candidate in filler_candidates}
                    for candidate in relaxed_fillers:
                        if _citywalk_candidate_key(candidate) in existing_filler_keys:
                            continue
                        filler_candidates.append(candidate)
                        existing_filler_keys.add(_citywalk_candidate_key(candidate))
                    slot_candidate_counts["filler_theme_relaxed"] = len(relaxed_fillers)
                    if len(filler_candidates) >= filler_slots:
                        warnings.append("connector_slot_relaxed_with_theme_poi")
                if len(filler_candidates) < filler_slots:
                    soft_fillers = _soft_connector_filler_candidates(
                        indexed_pois,
                        activities,
                        activity_order=len(candidate_groups) + 1,
                    )
                    existing_filler_keys = {_citywalk_candidate_key(candidate) for candidate in filler_candidates}
                    for candidate in soft_fillers:
                        if _citywalk_candidate_key(candidate) in existing_filler_keys:
                            continue
                        filler_candidates.append(candidate)
                        existing_filler_keys.add(_citywalk_candidate_key(candidate))
                    slot_candidate_counts["filler_soft_relaxed"] = len(soft_fillers)
                    if len(filler_candidates) >= filler_slots:
                        warnings.append("connector_slot_relaxed_with_any_route_poi")
                if len(filler_candidates) < filler_slots:
                    fallback_size = _fallback_urban_activity_route_size(
                        composition_policy.get("duration_budget_min"),
                        activities,
                    )
                    fallback_slots = max(0, fallback_size - len(candidate_groups))
                    if len(filler_candidates) >= fallback_slots and fallback_slots > 0:
                        warnings.append("target_route_size_partially_filled")
                        filler_slots = fallback_slots
                    elif len(candidate_groups) >= requested_min:
                        warnings.append("target_route_size_not_filled")
                        warnings.append("optional_connector_slot_empty")
                        filler_slots = 0
                    else:
                        warnings.append("connector_slot_empty")
                        warnings.append("insufficient_poi_candidates_for_min_route_size")
                        return {
                            "routes": [],
                            "warnings": _unique_list(warnings),
                            "diagnostics": {
                                "policy_type": "urban_activity",
                                "activity_count": len(activities),
                                "candidate_count": len(indexed_pois),
                                "required_min_pois": requested_min,
                                "target_route_size": requested_target,
                                "slot_candidate_counts": slot_candidate_counts,
                                "evaluated_route_count": 0,
                                "feasible_route_count": 0,
                                **activity_diag,
                            },
                        }
                filler_groups: List[List[Dict[str, Any]]] = []
                for filler_index in range(filler_slots):
                    filler_duration = _urban_filler_visit_duration(
                        composition_policy.get("duration_budget_min"),
                        filler_index,
                        requested_target,
                    )
                    group: List[Dict[str, Any]] = []
                    for candidate in filler_candidates[:8]:
                        item = dict(candidate)
                        item["activity_order"] = len(candidate_groups) + filler_index + 1
                        item["activity_type"] = item.get("activity_type") or ("leisure_fill" if filler_duration >= 30 else "short_rest")
                        item["activity_label"] = item.get("activity_label") or (
                            "leisure filler stop" if filler_duration >= 30 else "short connector stop"
                        )
                        item["visit_duration_min"] = min(
                            filler_duration,
                            _cap_urban_visit_duration(
                                item.get("visit_duration_min"),
                                {
                                    **dict(item),
                                    "activity_type": item["activity_type"],
                                    "activity_label": item["activity_label"],
                                },
                                item.get("category"),
                                composition_policy.get("duration_budget_min"),
                            ),
                        )
                        group.append(item)
                    filler_groups.append(group)
                route_group_sets = [candidate_groups + filler_groups]
        activities_for_route = active_activities

    evaluated: List[Dict[str, Any]] = []
    duplicate_combo_skip_count = 0
    activity_quality_violation_skip_count = 0
    for route_groups in route_group_sets:
        for combo in product(*route_groups):
            ids = [str(poi.get("id") or poi.get("name")) for poi in combo]
            if len(set(ids)) != len(ids):
                duplicate_combo_skip_count += 1
                continue
            ordered = [dict(poi) for poi in combo]
            for profile in composition_policy.get("profiles") or ["urban_activity"]:
                route = _evaluate_route(
                    ordered,
                    route_costs["distance_matrix"],
                    weights,
                    composition_policy,
                    int(composition_policy.get("duration_budget_min") or 180),
                    str(profile),
                    start_location=start_location,
                    duration_matrix=route_costs.get("duration_matrix"),
                    source_matrix=route_costs.get("source_matrix"),
                    mode_matrix=route_costs.get("mode_matrix"),
                    candidate_modes_matrix=route_costs.get("candidate_modes_matrix"),
                    start_costs=route_costs.get("start_costs"),
                    route_mode=route_mode,
                )
                route["activity_sequence"] = [dict(activity) for activity in activities_for_route]
                if not composition_policy.get("flexible_citywalk") and _route_activity_quality_violations(
                    route.get("pois", []),
                    activities_for_route,
                ):
                    activity_quality_violation_skip_count += 1
                    continue
                evaluated.append(route)

    required_min_pois = max(MIN_ROUTE_POIS, int(composition_policy.get("route_size_min") or MIN_ROUTE_POIS))
    evaluated = [route for route in evaluated if len(route.get("pois", [])) >= required_min_pois]
    distance_limited_count = 0
    if composition_policy.get("flexible_citywalk"):
        before_distance_filter = len(evaluated)
        evaluated = [
            route
            for route in evaluated
            if float(route.get("total_distance_m", 0) or 0) <= CITYWALK_MAX_DISTANCE_M
        ]
        distance_limited_count = before_distance_filter - len(evaluated)
    feasible = [route for route in evaluated if route["estimated_duration_min"] <= int(composition_policy.get("duration_budget_min") or 180)]
    soft_feasible = [route for route in evaluated if route["estimated_duration_min"] <= float(composition_policy.get("duration_budget_min") or 180) * 1.2]
    best_estimated_duration_min = None
    if evaluated:
        best_estimated_duration_min = round(
            min(float(route.get("estimated_duration_min", 0.0) or 0.0) for route in evaluated),
            1,
        )
    ranked = sorted(feasible or soft_feasible or evaluated, key=_route_sort_key)
    routes: List[Dict[str, Any]] = []
    used_signatures = set()
    dedupe_by_poi_sequence = bool(composition_policy.get("flexible_citywalk"))
    for route in ranked:
        signature = tuple(str(poi.get("id") or poi.get("name")) for poi in route.get("pois", []))
        if route_mode == MULTIMODAL_LOW_FRICTION and not dedupe_by_poi_sequence:
            mode_signature = tuple(
                str(leg.get("selected_mode") or leg.get("mode") or "")
                for leg in route.get("legs", [])
                if isinstance(leg, Mapping)
            )
            signature = (*signature, "modes", *mode_signature)
        if signature in used_signatures:
            continue
        used_signatures.add(signature)
        routes.append(route)
        if len(routes) >= max_options:
            break
    if not routes:
        warnings.append("no_urban_activity_routes_generated")
        warnings.append("min_route_poi_count_not_satisfied")
        if composition_policy.get("flexible_citywalk") and distance_limited_count:
            warnings.append("citywalk_route_distance_exceeds_limit")
    elif not feasible:
        warnings.append("no_urban_activity_route_within_time_budget")
    return {
        "routes": routes,
        "warnings": _unique_list(warnings),
        "diagnostics": {
            "policy_type": "urban_activity",
            "duration_budget_min": composition_policy.get("duration_budget_min"),
            "activity_count": len(activities),
            "candidate_count": len(indexed_pois),
            "slot_candidate_counts": slot_candidate_counts,
            "citywalk_quality_relaxed": citywalk_quality_relaxed if composition_policy.get("flexible_citywalk") else False,
            "citywalk_quality_candidate_count_raw": raw_quality_count if composition_policy.get("flexible_citywalk") else None,
            "citywalk_candidate_debug_sample": _citywalk_candidate_debug_sample(raw_citywalk_candidates) if composition_policy.get("flexible_citywalk") else [],
            "duplicate_combo_skip_count": duplicate_combo_skip_count,
            "activity_quality_violation_skip_count": activity_quality_violation_skip_count,
            "citywalk_distance_limited_route_count": distance_limited_count,
            "citywalk_max_distance_m": CITYWALK_MAX_DISTANCE_M if composition_policy.get("flexible_citywalk") else None,
            "evaluated_route_count": len(evaluated),
            "feasible_route_count": len(feasible),
            "best_estimated_duration_min": best_estimated_duration_min,
            **activity_diag,
        },
    }


def format_route_options(
    solver_result: Mapping[str, Any],
    context: Mapping[str, Any],
    weights: Mapping[str, Any],
    composition_policy: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Convert solver routes into stable external route_options."""
    route_options = []
    urban_profile = context.get("urban_intent_profile") if isinstance(context.get("urban_intent_profile"), Mapping) else {}
    duration_budget_min = composition_policy.get("duration_budget_min")
    try:
        required_min_pois = max(MIN_ROUTE_POIS, int(composition_policy.get("route_size_min") or MIN_ROUTE_POIS))
    except (TypeError, ValueError):
        required_min_pois = MIN_ROUTE_POIS
    for index, route in enumerate(solver_result.get("routes", []) or [], start=1):
        route = dict(route)
        if len(route.get("pois", []) or []) < required_min_pois:
            continue
        route["warnings"] = _clean_route_time_budget_warnings(route.get("warnings", []), route, duration_budget_min)
        pois = []
        for position, poi in enumerate(route.get("pois", []), start=1):
            pois.append(
                {
                    "position": position,
                    "id": poi.get("id", ""),
                    "name": poi.get("name", ""),
                    "category": poi.get("category", "other"),
                    "location": poi.get("location"),
                    "rating": poi.get("rating"),
                    "cost": poi.get("cost"),
                    "queue_risk": poi.get("queue_risk"),
                    "visit_duration_min": poi.get("visit_duration_min"),
                    "activity_type": poi.get("activity_type"),
                    "activity_label": poi.get("activity_label"),
                    "activity_order": poi.get("activity_order"),
                    "opening_hours": poi.get("opening_hours"),
                    "opening_status": poi.get("opening_status"),
                    "opening_hours_warning": poi.get("opening_hours_warning"),
                    "accessibility_type": poi.get("accessibility_type") or _poi_accessibility_type(poi),
                    "visit_mode": poi.get("visit_mode"),
                    "accessibility_note": poi.get("accessibility_note"),
                    "activity_types": poi.get("activity_types", []),
                    "matched_activity_slots": poi.get("matched_activity_slots", []),
                    "candidate_activity_slots": poi.get("candidate_activity_slots", []),
                    "micro_category": poi.get("micro_category"),
                    "indoor_outdoor": poi.get("indoor_outdoor"),
                    "weather_tags": poi.get("weather_tags", []),
                    "weather_fit_score": poi.get("weather_fit_score"),
                    "reward": poi.get("_reward"),
                    "tags": poi.get("tags", []),
                    "recall_sources": poi.get("recall_sources", []),
                }
            )

        title = _route_option_title(route.get("profile"), composition_policy.get("policy_type"))
        total_distance_m = route.get("total_distance_m")
        try:
            total_distance_km = round(float(total_distance_m or 0) / 1000.0, 3)
        except (TypeError, ValueError):
            total_distance_km = 0.0
        route_options.append(
            {
                "option_id": f"route_{index}",
                "title": title,
                "profile": route.get("profile"),
                "optimization_profile": route.get("optimization_profile"),
                "transport_mode_summary": route.get("transport_mode_summary", {}),
                "transfer_count": route.get("transfer_count", 0),
                "mode_switch_count": route.get("mode_switch_count", 0),
                "total_walking_distance_m": route.get("total_walking_distance_m", 0),
                "total_bicycling_distance_m": route.get("total_bicycling_distance_m", 0),
                "total_transit_duration_min": route.get("total_transit_duration_min", 0.0),
                "start_location": route.get("start_location"),
                "poi_sequence": [poi.get("name", "") for poi in route.get("pois", [])],
                "pois": pois,
                "activity_sequence": route.get("activity_sequence", urban_profile.get("activity_sequence", [])),
                "legs": route.get("legs", []),
                "schedule": route.get("schedule", []),
                "estimated_duration_min": route.get("estimated_duration_min"),
                "total_distance_m": total_distance_m,
                "distance_m": total_distance_m,
                "score": route.get("score"),
                "legacy_score": route.get("legacy_score"),
                "score_version": route.get("score_version"),
                "score_breakdown": route.get("score_breakdown", {}),
                "matrix_source_summary": route.get("matrix_source_summary", {}),
                "schedule_start_min": route.get("schedule_start_min"),
                "metrics": {
                    "reward_total": route.get("reward_total"),
                    "visit_duration_min": route.get("visit_duration_min"),
                    "travel_duration_min": route.get("travel_duration_min"),
                    "start_travel_duration_min": route.get("start_travel_duration_min"),
                    "start_distance_m": route.get("start_distance_m"),
                    "total_minutes": route.get("estimated_duration_min"),
                    "estimated_duration_min": route.get("estimated_duration_min"),
                    "total_distance_m": total_distance_m,
                    "distance_m": total_distance_m,
                    "total_distance_km": total_distance_km,
                    "avg_queue_risk": route.get("avg_queue_risk"),
                    "estimated_cost": route.get("estimated_cost"),
                    "score": route.get("score"),
                    "legacy_score": route.get("legacy_score"),
                    "score_version": route.get("score_version"),
                    "matrix_source_summary": route.get("matrix_source_summary", {}),
                    "transport_mode_summary": route.get("transport_mode_summary", {}),
                    "transfer_count": route.get("transfer_count", 0),
                    "mode_switch_count": route.get("mode_switch_count", 0),
                    "time_budget_min": composition_policy.get("duration_budget_min"),
                    "time_budget_fit": route.get("estimated_duration_min", 0) <= composition_policy.get("duration_budget_min", 0),
                },
                "constraints": route.get("constraints", {}),
                "score_formula": route.get("score_formula"),
                "warnings": route.get("warnings", []),
            }
        )
    return route_options


def run_route_planning(
    context: Optional[Dict[str, Any]] = None,
    previous_results: Optional[Sequence[Dict[str, Any]]] = None,
    optimizer: Any = None,
    route_client: Any = None,
    auto_use_amap_route_matrix: bool = False,
    strict_no_fallback: bool = False,
) -> Dict[str, Any]:
    """Main entry point for deterministic route planning."""
    planner_input = extract_route_planning_input(context or {}, previous_results or [])
    pois = normalize_pois(planner_input["pois"], planner_input.get("weather_context"))
    pois, start_duplicate_count = _filter_start_location_duplicate_pois(
        pois,
        planner_input.get("start_location"),
    )
    weights = planner_input["weights"]

    if not pois:
        has_poi_search_result = bool(planner_input.get("poi_search_result"))
        warning_code = "missing_poi_search_result" if not has_poi_search_result else "missing_poi_candidates"
        return {
            "route_planning_complete": False,
            "route_options": [],
            "error": "No POI candidates are available for route planning.",
            "error_type": warning_code,
            "warnings": [warning_code],
            "route_preference": planner_input["route_preference"],
            "weights": weights,
            "start_location": planner_input.get("start_location"),
            "composition_policy": infer_composition_policy(planner_input, weights),
            "diagnostics": {
                "input_poi_count": len(planner_input["pois"]),
                "valid_poi_count": 0,
                "start_duplicate_poi_filtered_count": start_duplicate_count,
            },
        }

    anchor_hint = planner_input.get("anchor_hint", "")
    for poi in pois:
        poi["_reward"] = compute_poi_reward(
            poi,
            weights,
            anchor_hint,
            query_text=planner_input.get("query_text", ""),
            city=planner_input.get("city", ""),
        )
        poi["_reward"] = round(
            max(0.01, float(poi.get("_reward", 0.0) or 0.0) + urban_poi_reward_adjustment(poi, planner_input.get("urban_intent_profile", {}))),
            4,
        )

    composition_policy = infer_composition_policy(planner_input, weights)
    composition_policy["schedule_start_min"] = _schedule_start_min_from_urban_profile(
        planner_input.get("urban_intent_profile")
    )
    prefer_local_food = _local_food_requested(
        planner_input.get("query_text", ""),
        planner_input.get("city", ""),
        weights,
    )
    pois = _prefer_local_food_candidates(pois, composition_policy, prefer_local_food)
    pois = _adapt_visit_durations_for_policy(pois, composition_policy)
    pois = _limit_candidates(pois, duration_budget_min=int(planner_input.get("duration_min") or 180))
    route_mode = _resolve_transport_mode(
        planner_input.get("context"),
        planner_input.get("route_preference"),
        planner_input.get("urban_intent_profile"),
        strict_no_fallback=strict_no_fallback,
    )
    route_context = dict(planner_input.get("context") or {})
    route_context.setdefault("urban_intent_profile", planner_input.get("urban_intent_profile", {}))
    try:
        route_costs = _build_route_costs(
            pois,
            start_location=planner_input.get("start_location"),
            route_mode=route_mode,
            route_client=route_client,
            auto_use_amap_route_matrix=auto_use_amap_route_matrix,
            context=route_context,
            strict_no_fallback=strict_no_fallback,
        )
    except RouteCostMatrixError as exc:
        return {
            "route_planning_complete": False,
            "route_options": [],
            "error": str(exc),
            "error_type": "route_cost_matrix_failed",
            "warnings": ["route_cost_matrix_failed"],
            "city": planner_input.get("city", ""),
            "anchor_hint": anchor_hint,
            "start_location": planner_input.get("start_location"),
            "duration_budget_min": composition_policy["duration_budget_min"],
            "route_preference": planner_input["route_preference"],
            "weights": weights,
            "composition_policy": composition_policy,
            "route_mode": route_mode,
            "urban_intent_profile": planner_input.get("urban_intent_profile", {}),
            "weather_context": planner_input.get("weather_context", {}),
            "diagnostics": {
                **dict(exc.diagnostics or {}),
                "input_poi_count": len(planner_input["pois"]),
                "valid_poi_count": len(pois),
                "start_duplicate_poi_filtered_count": start_duplicate_count,
            },
        }
    if has_urban_activity_sequence(planner_input.get("urban_intent_profile")):
        composition_policy = urban_activity_policy(planner_input.get("urban_intent_profile"), composition_policy)
        if start_duplicate_count:
            composition_policy["warnings"] = _unique_list(
                [*(composition_policy.get("warnings") or []), "start_location_duplicate_poi_filtered"]
            )
        if route_mode == MULTIMODAL_LOW_FRICTION:
            composition_policy["profiles"] = list(TRANSPORT_OPTIMIZATION_PROFILES)
        solver_result = solve_urban_activity_routes(
            pois=pois,
            route_costs=route_costs,
            weights=weights,
            composition_policy=composition_policy,
            urban_intent_profile=planner_input.get("urban_intent_profile", {}),
            start_location=planner_input.get("start_location"),
            route_mode=route_mode,
            max_options=3,
        )
    else:
        solver_result = solve_routes_orienteering_heuristic(
            pois=pois,
            distance_matrix=route_costs["distance_matrix"],
            duration_matrix=route_costs["duration_matrix"],
            source_matrix=route_costs["source_matrix"],
            mode_matrix=route_costs["mode_matrix"],
            candidate_modes_matrix=route_costs.get("candidate_modes_matrix"),
            start_costs=route_costs["start_costs"],
            route_mode=route_mode,
            weights=weights,
            composition_policy=composition_policy,
            duration_budget_min=int(composition_policy["duration_budget_min"]),
            start_location=planner_input.get("start_location"),
            max_options=3,
            prefer_local_food=prefer_local_food,
        )
    route_options = format_route_options(solver_result, planner_input, weights, composition_policy)
    _enrich_route_option_legs(
        route_options,
        route_client=route_costs.get("route_client"),
        route_mode=route_mode,
    )

    warnings = _clean_time_budget_warnings(
        _unique_list(
            [
                *(route_costs.get("warnings") or []),
                *(composition_policy.get("warnings") or []),
                *(solver_result.get("warnings") or []),
            ]
        ),
        route_options,
        composition_policy.get("duration_budget_min"),
    )
    if not route_options and solver_result.get("routes"):
        warnings = _unique_list([*warnings, "min_route_poi_count_not_satisfied"])
    complete = bool(route_options)
    matrix_diagnostics = dict(route_costs.get("diagnostics") or {})
    _refresh_route_client_diagnostics(matrix_diagnostics, route_costs)
    result = {
        "route_planning_complete": complete,
        "route_options": route_options,
        "city": planner_input.get("city", ""),
        "anchor_hint": anchor_hint,
        "start_location": planner_input.get("start_location"),
        "duration_budget_min": composition_policy["duration_budget_min"],
        "route_preference": planner_input["route_preference"],
        "weights": weights,
        "composition_policy": composition_policy,
        "profiles": composition_policy.get("profiles", []),
        "route_mode": route_mode,
        "urban_intent_profile": planner_input.get("urban_intent_profile", {}),
        "weather_context": planner_input.get("weather_context", {}),
        "low_queue_requested": _low_queue_requested(weights, planner_input.get("query_text", "")),
        "warnings": warnings,
        "diagnostics": {
            **dict(solver_result.get("diagnostics") or {}),
            **matrix_diagnostics,
            "input_poi_count": len(planner_input["pois"]),
            "valid_poi_count": len(pois),
            "start_duplicate_poi_filtered_count": start_duplicate_count,
        },
    }
    result["low_queue_requested"] = _low_queue_requested(weights, planner_input.get("query_text", ""))
    if not complete and "required_activity_slot_empty" in warnings:
        result["error_type"] = "required_activity_slot_empty"
        result["error"] = "A required activity slot has no verified matching POI candidates."
        return result
    if not complete and "connector_slot_empty" in warnings:
        result["error_type"] = "connector_slot_empty"
        result["error"] = "Required activities have candidates, but no suitable connector POI is available to satisfy the minimum route size."
        return result
    if not complete and "no_route_within_time_budget" in warnings:
        result["error_type"] = "no_route_within_time_budget"
        result["error"] = "Generated candidate routes, but none fit the requested time budget."
        return result
    if not complete:
        result["error_type"] = "no_route_options"
        result["error"] = "No route_options satisfy the route composition constraints."
    return result


def extract_constraints(event_data: Optional[Mapping[str, Any]], context: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Backward-compatible constraint parser used by older direct tests."""
    event_data = event_data or {}
    context = context or {}
    query = _query_text(context)
    duration = _first_non_empty(
        event_data.get("duration"),
        event_data.get("duration_min"),
        context.get("duration"),
        query,
    )
    total_minutes = parse_time_budget_min(duration)
    budget_match = re.search(r"(?:\u9884\u7b97|budget)\s*([0-9]+(?:\.[0-9]+)?)", query, re.I)
    avoid_queue = any(token in query for token in ("\u5c11\u6392\u961f", "\u4e0d\u6392\u961f", "\u522b\u6392\u961f", "\u4f4e\u6392\u961f"))
    avoid_too_tired = any(token in query for token in ("\u4e0d\u60f3\u592a\u7d2f", "\u522b\u592a\u7d2f", "\u8f7b\u677e", "\u77ed\u9014"))
    return {
        "total_minutes": total_minutes,
        "budget": float(budget_match.group(1)) if budget_match else None,
        "avoid_queue": avoid_queue,
        "avoid_too_tired": avoid_too_tired,
        "max_pois": 3 if total_minutes <= 240 or avoid_too_tired else 4,
    }


def _evaluate_route(
    ordered_pois: Sequence[Mapping[str, Any]],
    distance_matrix: Sequence[Sequence[float]],
    weights: Mapping[str, Any],
    policy: Mapping[str, Any],
    duration_budget_min: int,
    profile: str,
    start_location: Optional[Mapping[str, Any]] = None,
    duration_matrix: Optional[Sequence[Sequence[float]]] = None,
    source_matrix: Optional[Sequence[Sequence[str]]] = None,
    mode_matrix: Optional[Sequence[Sequence[str]]] = None,
    candidate_modes_matrix: Optional[Sequence[Sequence[Mapping[str, Any]]]] = None,
    start_costs: Optional[Mapping[int, Mapping[str, Any]]] = None,
    route_mode: str = "walking",
) -> Dict[str, Any]:
    legs = []
    total_distance = 0.0
    start_distance = 0.0
    start_travel_duration = 0.0
    normalized_start = _normalize_start_location(start_location)
    previous_mode = ""
    if normalized_start and ordered_pois:
        first_poi = ordered_pois[0]
        first_index = int(first_poi["_matrix_index"])
        start_cost = dict((start_costs or {}).get(first_index) or {})
        selected_start_cost = _select_transport_candidate(
            start_cost,
            profile=profile,
            previous_mode=previous_mode,
            route_mode=route_mode,
            policy=policy,
        )
        previous_mode = str(selected_start_cost.get("mode") or start_cost.get("mode") or previous_mode)
        start_distance = float(selected_start_cost.get("distance_m") or _haversine_meters(normalized_start, first_poi))
        start_travel_duration = round(
            _duration_minutes(selected_start_cost.get("duration_sec"), start_distance, route_mode),
            1,
        )
        total_distance += start_distance
        legs.append(
            {
                "from": normalized_start.get("name", "start"),
                "to": first_poi.get("name", ""),
                "from_poi_id": "start",
                "from_poi_name": normalized_start.get("name", "start"),
                "to_poi_id": first_poi.get("id", ""),
                "to_poi_name": first_poi.get("name", ""),
                "from_location": _route_location_payload(normalized_start),
                "to_location": _route_location_payload(first_poi),
                "mode": previous_mode or _default_leg_mode(route_mode),
                "selected_mode": previous_mode or _default_leg_mode(route_mode),
                "candidate_modes": dict(selected_start_cost.get("candidate_modes") or start_cost.get("candidate_modes") or {}),
                "selection_reason": selected_start_cost.get("selection_reason") or _selection_reason_for_profile(profile),
                "distance_m": int(round(start_distance)),
                "travel_duration_min": start_travel_duration,
                "duration_min": start_travel_duration,
                "source": selected_start_cost.get("source") or start_cost.get("source") or "haversine_fallback",
                "steps": list(selected_start_cost.get("steps") or []),
                "polyline": str(selected_start_cost.get("polyline") or ""),
                "from_start_location": True,
            }
        )
    for index in range(1, len(ordered_pois)):
        left = ordered_pois[index - 1]
        right = ordered_pois[index]
        left_index = int(left["_matrix_index"])
        right_index = int(right["_matrix_index"])
        base_cost = {
            "distance_m": float(distance_matrix[left_index][right_index]),
            "duration_sec": _matrix_value(duration_matrix, left_index, right_index),
            "source": _matrix_value(source_matrix, left_index, right_index) or "haversine_fallback",
            "mode": _matrix_value(mode_matrix, left_index, right_index) or _default_leg_mode(route_mode),
            "candidate_modes": _matrix_value(candidate_modes_matrix, left_index, right_index) or {},
        }
        selected_cost = _select_transport_candidate(
            base_cost,
            profile=profile,
            previous_mode=previous_mode,
            route_mode=route_mode,
            policy=policy,
        )
        previous_mode = str(selected_cost.get("mode") or base_cost.get("mode") or previous_mode)
        distance = float(selected_cost.get("distance_m") or base_cost["distance_m"])
        travel_duration = round(
            _duration_minutes(selected_cost.get("duration_sec"), distance, route_mode),
            1,
        )
        total_distance += distance
        legs.append(
            {
                "from": left.get("name", ""),
                "to": right.get("name", ""),
                "from_poi_id": left.get("id", ""),
                "from_poi_name": left.get("name", ""),
                "to_poi_id": right.get("id", ""),
                "to_poi_name": right.get("name", ""),
                "from_location": _route_location_payload(left),
                "to_location": _route_location_payload(right),
                "mode": previous_mode or _default_leg_mode(route_mode),
                "selected_mode": previous_mode or _default_leg_mode(route_mode),
                "candidate_modes": dict(selected_cost.get("candidate_modes") or base_cost.get("candidate_modes") or {}),
                "selection_reason": selected_cost.get("selection_reason") or _selection_reason_for_profile(profile),
                "distance_m": int(round(distance)),
                "travel_duration_min": travel_duration,
                "duration_min": travel_duration,
                "source": selected_cost.get("source") or base_cost.get("source") or "haversine_fallback",
                "steps": list(selected_cost.get("steps") or []),
                "polyline": str(selected_cost.get("polyline") or ""),
            }
        )

    reward_total = sum(float(poi.get("_reward", 0.0) or 0.0) for poi in ordered_pois)
    visit_duration = sum(int(poi.get("visit_duration_min", 0) or 0) for poi in ordered_pois)
    travel_duration = sum(float(leg["travel_duration_min"]) for leg in legs)
    estimated_duration = round(visit_duration + travel_duration, 1)
    transport_summary = _summarize_transport_legs(legs, route_mode)
    avg_queue = round(sum(_bounded_float(poi.get("queue_risk"), 0, 1, 0.45) for poi in ordered_pois) / len(ordered_pois), 4)
    estimated_cost = round(sum(max(0.0, _to_float(poi.get("cost"), 80.0)) for poi in ordered_pois), 1)
    categories = [poi.get("category") for poi in ordered_pois]

    policy_type = str(policy.get("policy_type") or "balanced")
    dining_count = categories.count("dining")
    culture_count = categories.count("culture_entertainment")
    required_categories = list(policy.get("required_categories") or [])
    min_dining = int(policy.get("min_dining", 0) or 0)
    min_culture = int(policy.get("min_culture_entertainment", 0) or 0)
    if policy_type == "food_only":
        composition_bonus = 0.75 if dining_count >= max(1, min_dining) and culture_count == 0 else -2.0
    elif policy_type == "food_focused":
        composition_bonus = 0.65 if dining_count >= max(1, min_dining) else -2.0
        if dining_count >= 2:
            composition_bonus += 0.18
    elif policy_type == "culture_focused":
        composition_bonus = 0.75 if culture_count >= max(1, min_culture) else -2.0
        if dining_count == 0 and culture_count >= 3:
            composition_bonus += 0.35
        elif dining_count > 0:
            composition_bonus -= 0.25
    elif policy_type == "citywalk":
        composition_bonus = 0.85 if culture_count >= max(2, min_culture) and dining_count <= 1 else -2.0
        if len(ordered_pois) == 2:
            composition_bonus += 0.25
    elif policy_type == "urban_activity":
        activity_count = len({str(poi.get("activity_type") or poi.get("id") or poi.get("name")) for poi in ordered_pois})
        composition_bonus = 0.95 if activity_count == len(ordered_pois) else -1.0
    else:
        composition_bonus = 0.45 if "dining" in categories and "culture_entertainment" in categories else -2.0
    profile_bonus = _profile_bonus(profile, categories, weights, avg_queue)
    distance_penalty = (total_distance / 1000.0) * (0.18 + 0.9 * float(weights.get("travel_efficiency", 0.0) or 0.0))
    queue_penalty = avg_queue * (0.25 + 1.8 * float(weights.get("queue", 0.0) or 0.0))
    overtime = max(0.0, estimated_duration - duration_budget_min)
    overtime_penalty = overtime / 20.0 * 1.4
    utilization = estimated_duration / duration_budget_min if duration_budget_min else 0.0
    time_fit_bonus = 0.35 if 0.55 <= utilization <= 1.0 else (-0.25 if utilization < 0.45 else 0.0)

    legacy_score = round(
        reward_total
        + composition_bonus
        + profile_bonus
        + time_fit_bonus
        - distance_penalty
        - queue_penalty
        - overtime_penalty,
        4,
    )
    fallback_leg_count = sum(1 for leg in legs if leg.get("source") == "haversine_fallback")
    leg_count = max(1, len(legs))
    fallback_ratio = fallback_leg_count / leg_count
    travel_ratio = travel_duration / estimated_duration if estimated_duration else 1.0
    poi_reward_score = min(35.0, reward_total * 3.0)
    preference_match_score = min(
        15.0,
        15.0
        * (
            float(weights.get("food", 0.0) or 0.0) * dining_count
            + float(weights.get("sightseeing", 0.0) or 0.0) * culture_count
            + float(weights.get("experience", 0.0) or 0.0) * max(0, len(categories) - dining_count - culture_count)
        )
        / max(1, len(categories)),
    )
    composition_score = 15.0 if all(category in categories for category in required_categories) else 0.0
    if policy_type == "food_only" and culture_count:
        composition_score = 0.0
    elif policy_type == "food_focused" and dining_count >= 2:
        composition_score = 15.0
    elif policy_type == "culture_focused" and culture_count >= 2:
        composition_score = 15.0
    elif policy_type == "citywalk" and culture_count >= 2 and dining_count <= 1:
        composition_score = 15.0
    elif policy_type == "urban_activity":
        composition_score = 15.0
    time_fit_score = _time_fit_score(utilization)
    route_efficiency_score = max(0.0, min(10.0, 10.0 * (1.0 - travel_ratio)))
    source_reliability_score = max(0.0, 5.0 * (1.0 - fallback_ratio))
    anchor_bonus = 5.0 if normalized_start else 0.0
    queue_penalty_v2 = min(8.0, avg_queue * 8.0)
    cost_penalty_v2 = min(6.0, estimated_cost / 300.0 * 6.0)
    overtime_penalty_v2 = min(30.0, max(0.0, utilization - 1.0) * 100.0)
    fallback_penalty = min(5.0, fallback_ratio * 5.0)
    activity_match_score = 20.0 if policy_type == "urban_activity" else composition_score / 15.0 * 20.0
    opening_hours_score = _opening_hours_score(ordered_pois)
    weather_fit_score = _route_weather_fit_score(ordered_pois, policy)
    social_fit_score = _social_fit_score(ordered_pois, policy)
    transport_fit_score = max(0.0, 5.0 - min(5.0, float(transport_summary.get("mode_switch_count", 0) or 0)))
    transfer_penalty = min(8.0, float(transport_summary.get("transfer_count", 0) or 0) * 2.0 + float(transport_summary.get("mode_switch_count", 0) or 0))
    weather_penalty = max(0.0, 8.0 - weather_fit_score)
    long_walking = _weather_sensitive_long_walking_metrics(legs, policy)
    long_walking_penalty = float(long_walking.get("penalty", 0.0) or 0.0)
    non_citywalk_walking = _non_citywalk_long_walking_metrics(legs, policy)
    non_citywalk_walking_penalty = float(non_citywalk_walking.get("penalty", 0.0) or 0.0)
    score = round(
        max(
            0.0,
            min(
                100.0,
                activity_match_score
                + min(18.0, poi_reward_score * 18.0 / 35.0)
                + preference_match_score
                + time_fit_score
                + min(15.0, route_efficiency_score * 1.5)
                + opening_hours_score
                + weather_fit_score
                + social_fit_score
                + transport_fit_score
                - queue_penalty_v2
                - cost_penalty_v2
                - transfer_penalty
                - weather_penalty
                - overtime_penalty_v2
                - fallback_penalty
                - long_walking_penalty
                - non_citywalk_walking_penalty,
            ),
        ),
        4,
    )
    matrix_source_summary = _count_leg_sources(legs)

    warnings = []
    if estimated_duration > duration_budget_min:
        warnings.append("route_exceeds_time_budget")
    if "dining" not in categories and "dining" in required_categories:
        warnings.append("route_missing_dining")
    if "culture_entertainment" not in categories and "culture_entertainment" in required_categories:
        warnings.append("route_missing_culture_entertainment")
    if long_walking.get("warning"):
        warnings.append(str(long_walking["warning"]))
    if non_citywalk_walking.get("warning"):
        warnings.append(str(non_citywalk_walking["warning"]))
    if score < 50.0:
        warnings.append("low_route_confidence")

    return {
        "profile": profile,
        "optimization_profile": _optimization_profile(profile, route_mode),
        "transport_mode_summary": transport_summary,
        "transfer_count": transport_summary.get("transfer_count", 0),
        "mode_switch_count": transport_summary.get("mode_switch_count", 0),
        "total_walking_distance_m": transport_summary.get("walking_distance_m", 0),
        "max_walking_leg_m": int(round(float(long_walking.get("max_walking_leg_m", 0.0) or 0.0))),
        "long_weather_sensitive_walking_penalty": round(long_walking_penalty, 4),
        "total_walking_duration_min": round(float(non_citywalk_walking.get("total_walking_duration_min", 0.0) or 0.0), 1),
        "non_citywalk_long_walking_penalty": round(non_citywalk_walking_penalty, 4),
        "total_bicycling_distance_m": transport_summary.get("bicycling_distance_m", 0),
        "total_transit_duration_min": transport_summary.get("transit_duration_min", 0.0),
        "_composition_preference_rank": _composition_preference_rank(policy_type, categories),
        "start_location": normalized_start,
        "pois": [dict(poi) for poi in ordered_pois],
        "legs": legs,
        "schedule": _build_schedule(ordered_pois, legs, _schedule_start_min_from_policy(policy)),
        "reward_total": round(reward_total, 4),
        "duration_budget_min": duration_budget_min,
        "score_breakdown": {
            "activity_match_score": round(activity_match_score, 4),
            "poi_quality_score": round(min(18.0, poi_reward_score * 18.0 / 35.0), 4),
            "poi_reward_score": round(poi_reward_score, 4),
            "preference_match_score": round(preference_match_score, 4),
            "composition_score": round(composition_score, 4),
            "time_fit_score": round(time_fit_score, 4),
            "route_efficiency_score": round(min(15.0, route_efficiency_score * 1.5), 4),
            "source_reliability_score": round(source_reliability_score, 4),
            "opening_hours_score": round(opening_hours_score, 4),
            "weather_fit_score": round(weather_fit_score, 4),
            "social_fit_score": round(social_fit_score, 4),
            "transport_fit_score": round(transport_fit_score, 4),
            "anchor_bonus": round(anchor_bonus, 4),
            "queue_penalty": round(queue_penalty_v2, 4),
            "cost_penalty": round(cost_penalty_v2, 4),
            "transfer_penalty": round(transfer_penalty, 4),
            "weather_penalty": round(weather_penalty, 4),
            "overtime_penalty": round(overtime_penalty_v2, 4),
            "fallback_penalty": round(fallback_penalty, 4),
            "long_walking_penalty": round(long_walking_penalty, 4),
            "non_citywalk_long_walking_penalty": round(non_citywalk_walking_penalty, 4),
            "legacy_components": {
                "reward_total": round(reward_total, 4),
                "composition_bonus": round(composition_bonus, 4),
                "profile_bonus": round(profile_bonus, 4),
                "time_fit_bonus": round(time_fit_bonus, 4),
                "distance_penalty": round(distance_penalty, 4),
                "queue_penalty": round(queue_penalty, 4),
                "overtime_penalty": round(overtime_penalty, 4),
            },
        },
        "visit_duration_min": int(visit_duration),
        "travel_duration_min": round(travel_duration, 1),
        "start_travel_duration_min": start_travel_duration,
        "start_distance_m": int(round(start_distance)),
        "estimated_duration_min": estimated_duration,
        "total_distance_m": int(round(total_distance)),
        "avg_queue_risk": avg_queue,
        "estimated_cost": estimated_cost,
        "score": score,
        "legacy_score": legacy_score,
        "score_version": "v2_100",
        "matrix_source_summary": matrix_source_summary,
        "schedule_start_min": _schedule_start_min_from_policy(policy),
        "constraints": {
            "min_pois": len(ordered_pois) >= int(policy.get("route_size_min", 3)),
            "category_coverage": all(category in categories for category in policy.get("required_categories", [])),
            "time_budget": estimated_duration <= duration_budget_min,
        },
        "score_formula": (
            "clamp(activity_match_score + poi_quality_score + preference_match_score + "
            "time_fit_score + route_efficiency_score + opening_hours_score + weather_fit_score + "
            "social_fit_score + transport_fit_score - queue_penalty - cost_penalty - "
            "transfer_penalty - weather_penalty - overtime_penalty - long_walking_penalty - "
            "non_citywalk_long_walking_penalty, 0, 100)"
        ),
        "warnings": warnings,
    }


def _order_group_nearest_neighbor(
    group: Sequence[Mapping[str, Any]],
    distance_matrix: Sequence[Sequence[float]],
    profile: str,
    start_location: Optional[Mapping[str, Any]] = None,
    start_costs: Optional[Mapping[int, Mapping[str, Any]]] = None,
    duration_matrix: Optional[Sequence[Sequence[float]]] = None,
    prefer_local_food: bool = False,
) -> List[Dict[str, Any]]:
    remaining = [dict(poi) for poi in group]
    normalized_start = _normalize_start_location(start_location)
    if normalized_start:
        non_generic_dining_exists = any(
            poi.get("category") == "dining" and not _is_generic_western_dining(poi)
            for poi in remaining
        )
        current = min(
            remaining,
            key=lambda poi: (
                5000.0 if prefer_local_food and non_generic_dining_exists and _is_generic_western_dining(poi) else 0.0,
                _start_duration_sec(start_costs, poi, normalized_start)
                - _profile_reward_pull_seconds(profile) * float(poi.get("_reward", 0.0) or 0.0),
                _start_distance(start_costs, poi, normalized_start),
                -float(poi.get("_reward", 0.0) or 0.0),
                str(poi.get("name", "")),
            ),
        )
    else:
        current = max(remaining, key=lambda poi: (float(poi.get("_reward", 0.0) or 0.0), -_category_rank(poi), str(poi.get("name", ""))))
    ordered = [current]
    remaining.remove(current)

    while remaining:
        current_index = int(current["_matrix_index"])
        next_poi = min(
            remaining,
            key=lambda poi: (
                _matrix_duration_sec(
                    duration_matrix,
                    current_index,
                    int(poi["_matrix_index"]),
                    float(distance_matrix[current_index][int(poi["_matrix_index"])]),
                )
                - _profile_reward_pull_seconds(profile) * float(poi.get("_reward", 0.0) or 0.0),
                float(distance_matrix[current_index][int(poi["_matrix_index"])]),
                -float(poi.get("_reward", 0.0) or 0.0),
                str(poi.get("name", "")),
            ),
        )
        ordered.append(next_poi)
        remaining.remove(next_poi)
        current = next_poi
    return _two_opt(
        ordered,
        distance_matrix,
        duration_matrix=duration_matrix,
        start_costs=start_costs if normalized_start else None,
    )


def _two_opt(
    order: List[Dict[str, Any]],
    distance_matrix: Sequence[Sequence[float]],
    duration_matrix: Optional[Sequence[Sequence[float]]] = None,
    start_costs: Optional[Mapping[int, Mapping[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    if len(order) < 4:
        return order
    best = list(order)
    best_cost = _route_cost_key(best, distance_matrix, duration_matrix, start_costs=start_costs)
    improved = True
    while improved:
        improved = False
        for i in range(1, len(best) - 2):
            for j in range(i + 1, len(best)):
                if j - i == 1:
                    continue
                candidate = best[:i] + best[i:j][::-1] + best[j:]
                cost = _route_cost_key(candidate, distance_matrix, duration_matrix, start_costs=start_costs)
                if cost < best_cost:
                    best = candidate
                    best_cost = cost
                    improved = True
    return best


def _route_cost_key(
    order: Sequence[Mapping[str, Any]],
    distance_matrix: Sequence[Sequence[float]],
    duration_matrix: Optional[Sequence[Sequence[float]]],
    start_costs: Optional[Mapping[int, Mapping[str, Any]]] = None,
) -> Tuple[float, float]:
    duration = sum(
        _matrix_duration_sec(
            duration_matrix,
            int(order[index - 1]["_matrix_index"]),
            int(order[index]["_matrix_index"]),
            float(distance_matrix[int(order[index - 1]["_matrix_index"])][int(order[index]["_matrix_index"])]),
        )
        for index in range(1, len(order))
    )
    if start_costs and order:
        duration += _start_duration_sec(start_costs, order[0])
    return duration, _route_distance(order, distance_matrix, start_costs=start_costs)


def _route_distance(
    order: Sequence[Mapping[str, Any]],
    distance_matrix: Sequence[Sequence[float]],
    start_costs: Optional[Mapping[int, Mapping[str, Any]]] = None,
) -> float:
    total = sum(
        float(distance_matrix[int(order[index - 1]["_matrix_index"])][int(order[index]["_matrix_index"])])
        for index in range(1, len(order))
    )
    if start_costs and order:
        total += _start_distance(start_costs, order[0])
    return total


def _route_sort_key(route: Mapping[str, Any]) -> Tuple[int, float, float, float, float, float, str]:
    sequence = "|".join(str(poi.get("name", "")) for poi in route.get("pois", []))
    adjusted_travel_min = float(route.get("travel_duration_min", 0.0) or 0.0) - 1.5 * float(
        route.get("reward_total", 0.0) or 0.0
    )
    adjusted_travel_min += 2.0 * float(route.get("long_weather_sensitive_walking_penalty", 0.0) or 0.0)
    adjusted_travel_min += 2.0 * float(route.get("non_citywalk_long_walking_penalty", 0.0) or 0.0)
    return (
        int(route.get("_composition_preference_rank", 0) or 0),
        -float(route.get("score", 0.0) or 0.0),
        _duration_target_gap(route),
        float(route.get("non_citywalk_long_walking_penalty", 0.0) or 0.0),
        adjusted_travel_min,
        float(route.get("total_distance_m", 0) or 0),
        sequence,
    )


def _duration_target_gap(route: Mapping[str, Any]) -> float:
    try:
        duration = float(route.get("estimated_duration_min") or 0.0)
        budget = float(route.get("duration_budget_min") or 0.0)
    except (TypeError, ValueError):
        return 1.0
    if duration <= 0 or budget <= 0:
        return 1.0
    utilization = duration / budget
    if 0.75 <= utilization <= 1.05:
        return 0.0
    if utilization < 0.75:
        return 0.75 - utilization
    return (utilization - 1.05) * 1.5


def _optimization_profile(profile: Any, route_mode: str) -> str:
    profile_text = str(profile or "balanced")
    if route_mode == MULTIMODAL_LOW_FRICTION and profile_text in TRANSPORT_OPTIMIZATION_PROFILES:
        return profile_text
    return "single_mode"


def _summarize_transport_legs(legs: Sequence[Mapping[str, Any]], route_mode: str) -> Dict[str, Any]:
    mode_distance: Dict[str, int] = {}
    mode_duration: Dict[str, float] = {}
    selected_modes: List[str] = []
    for leg in legs or []:
        mode = str(leg.get("selected_mode") or leg.get("mode") or _default_leg_mode(route_mode))
        selected_modes.append(mode)
        distance = int(round(float(leg.get("distance_m", 0) or 0)))
        duration = round(float(leg.get("travel_duration_min", leg.get("duration_min", 0)) or 0), 1)
        mode_distance[mode] = mode_distance.get(mode, 0) + distance
        mode_duration[mode] = round(mode_duration.get(mode, 0.0) + duration, 1)
    mode_switch_count = sum(1 for index in range(1, len(selected_modes)) if selected_modes[index] != selected_modes[index - 1])
    transfer_count = mode_switch_count + sum(1 for mode in selected_modes if mode == "transit")
    summary = {
        "route_mode": route_mode,
        "selected_modes": selected_modes,
        "mode_distance_m": mode_distance,
        "mode_duration_min": mode_duration,
        "walking_distance_m": mode_distance.get("walking", 0),
        "bicycling_distance_m": mode_distance.get("bicycling", 0),
        "transit_duration_min": mode_duration.get("transit", 0.0),
        "transfer_count": transfer_count,
        "mode_switch_count": mode_switch_count,
    }
    return summary


def _weather_sensitive_long_walking_metrics(
    legs: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    weather = policy.get("weather_context") if isinstance(policy, Mapping) else {}
    if not _weather_indoor_preferred(weather if isinstance(weather, Mapping) else {}):
        return {"max_walking_leg_m": 0.0, "total_penalized_walking_m": 0.0, "penalty": 0.0}

    max_walking_leg_m = 0.0
    total_penalized_walking_m = 0.0
    penalty = 0.0
    for leg in legs or []:
        if not isinstance(leg, Mapping):
            continue
        mode = str(leg.get("selected_mode") or leg.get("mode") or "").casefold()
        if mode != "walking":
            continue
        try:
            distance_m = float(leg.get("distance_m") or 0.0)
        except (TypeError, ValueError):
            distance_m = 0.0
        max_walking_leg_m = max(max_walking_leg_m, distance_m)
        leg_penalty = _weather_sensitive_walking_score_penalty_points(distance_m)
        if leg_penalty > 0:
            total_penalized_walking_m += distance_m
            penalty += leg_penalty

    if penalty <= 0.0:
        return {"max_walking_leg_m": max_walking_leg_m, "total_penalized_walking_m": 0.0, "penalty": 0.0}

    return {
        "max_walking_leg_m": max_walking_leg_m,
        "total_penalized_walking_m": total_penalized_walking_m,
        "penalty": round(penalty, 4),
        "warning": "rainy_route_long_walking_leg",
    }


def _non_citywalk_long_walking_metrics(
    legs: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> Dict[str, Any]:
    if _policy_allows_long_walking(policy):
        return {"total_walking_duration_min": 0.0, "max_walking_leg_min": 0.0, "penalty": 0.0}

    total_walking_min = 0.0
    max_walking_leg_min = 0.0
    for leg in legs or []:
        if not isinstance(leg, Mapping):
            continue
        mode = str(leg.get("selected_mode") or leg.get("mode") or "").casefold()
        if mode != "walking":
            continue
        try:
            duration_min = float(leg.get("travel_duration_min", leg.get("duration_min", 0.0)) or 0.0)
        except (TypeError, ValueError):
            duration_min = 0.0
        total_walking_min += duration_min
        max_walking_leg_min = max(max_walking_leg_min, duration_min)

    penalty = _non_citywalk_walking_score_penalty_points(total_walking_min)
    result = {
        "total_walking_duration_min": round(total_walking_min, 1),
        "max_walking_leg_min": round(max_walking_leg_min, 1),
        "penalty": round(penalty, 4),
    }
    if penalty > 0:
        result["warning"] = "non_citywalk_long_walking"
    return result


def _policy_allows_long_walking(policy: Mapping[str, Any]) -> bool:
    if not isinstance(policy, Mapping):
        return False
    policy_type = str(policy.get("policy_type") or "").casefold()
    if policy_type == "citywalk" or bool(policy.get("flexible_citywalk")):
        return True
    decision = policy.get("decision") if isinstance(policy.get("decision"), Mapping) else {}
    if bool(decision.get("citywalk_requested")):
        return True
    return False


def _non_citywalk_walking_score_penalty_points(total_walking_min: Any) -> float:
    try:
        minutes = float(total_walking_min or 0.0)
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes <= 50.0:
        return 0.0
    if minutes <= 60.0:
        return 10.0
    if minutes <= 90.0:
        return 18.0 + math.ceil((minutes - 60.0) / 10.0) * 4.0
    return min(45.0, 30.0 + math.ceil((minutes - 90.0) / 10.0) * 5.0)


def _weather_sensitive_walking_score_penalty_points(distance_m: Any) -> float:
    """Gentle display-score penalty for long walking in bad weather."""
    try:
        distance = float(distance_m or 0.0)
    except (TypeError, ValueError):
        distance = 0.0
    if distance < 800.0:
        return 0.0
    if distance <= 1200.0:
        return 6.0
    extra_km = max(0.0, math.ceil((distance - 2200.0) / 1000.0))
    return min(18.0, 10.0 + extra_km * 4.0)


def _weather_sensitive_walking_choice_penalty_points(distance_m: Any) -> float:
    """Stronger per-leg choice penalty so bad-weather routes prefer transit over long walks."""
    try:
        distance = float(distance_m or 0.0)
    except (TypeError, ValueError):
        distance = 0.0
    if distance < 800.0:
        return 0.0
    if distance <= 1200.0:
        return 20.0
    extra_km = max(0.0, math.ceil((distance - 2200.0) / 1000.0))
    return 30.0 + extra_km * 10.0


def _opening_hours_score(pois: Sequence[Mapping[str, Any]]) -> float:
    if not pois:
        return 0.0
    score = 0.0
    for poi in pois:
        status = opening_status(poi.get("opening_hours"))
        if status == "verified_open":
            score += 10.0
        elif status == "unknown":
            score += 4.0
    return round(score / max(1, len(pois)), 4)


def _route_weather_fit_score(pois: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> float:
    values = []
    for poi in pois or []:
        try:
            values.append(float(poi.get("weather_fit_score")))
        except (TypeError, ValueError):
            values.append(0.5)
    if not values:
        return 4.0
    return round(max(0.0, min(8.0, sum(values) / len(values) * 8.0)), 4)


def _social_fit_score(pois: Sequence[Mapping[str, Any]], policy: Mapping[str, Any]) -> float:
    if not pois:
        return 0.0
    friendly = 0
    for poi in pois:
        text = _poi_text(poi)
        if any(term in text for term in SOCIAL_FIT_TERMS):
            friendly += 1
    return round(min(5.0, 2.5 + 2.5 * friendly / max(1, len(pois))), 4)


def _composition_preference_rank(policy_type: str, categories: Sequence[str]) -> int:
    dining_count = categories.count("dining")
    culture_count = categories.count("culture_entertainment")
    if policy_type == "culture_focused":
        if culture_count >= 3 and dining_count == 0:
            return 0
        if culture_count >= 2 and dining_count == 0:
            return 1
        return 2
    if policy_type == "food_only":
        return 0 if dining_count == len(categories) else 2
    if policy_type == "food_focused":
        return 0 if dining_count >= 2 else 1
    if policy_type == "citywalk":
        if culture_count >= 2 and dining_count == 0:
            return 0
        if culture_count >= 2 and dining_count <= 1:
            return 1
        return 2
    if policy_type == "urban_activity":
        return 0
    if policy_type == "balanced":
        return 0 if dining_count >= 1 and culture_count >= 1 else 1
    return 0


def _build_schedule(
    ordered_pois: Sequence[Mapping[str, Any]],
    legs: Sequence[Mapping[str, Any]],
    start_min: Any = None,
) -> List[Dict[str, Any]]:
    current_min = _safe_schedule_start_min(start_min)
    schedule: List[Dict[str, Any]] = []
    for index, poi in enumerate(ordered_pois):
        leg_index = index if legs and legs[0].get("from_start_location") else index - 1
        if 0 <= leg_index < len(legs):
            current_min += int(round(float(legs[leg_index].get("travel_duration_min", 0) or 0)))
        arrival = current_min
        visit = int(poi.get("visit_duration_min", 0) or DEFAULT_VISIT_DURATION_MIN.get(str(poi.get("category") or "other"), 40))
        departure = arrival + visit
        schedule.append(
            {
                "poi_id": poi.get("id", ""),
                "poi_name": poi.get("name", ""),
                "category": poi.get("category", "other"),
                "arrival_time": _format_clock(arrival),
                "departure_time": _format_clock(departure),
                "visit_minutes": visit,
                "queue_level": _queue_level(poi.get("queue_risk")),
                "accessibility_type": poi.get("accessibility_type") or _poi_accessibility_type(poi),
                "visit_mode": poi.get("visit_mode"),
                "accessibility_note": poi.get("accessibility_note"),
            }
        )
        current_min = departure
    return schedule


def _safe_schedule_start_min(value: Any) -> int:
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        return 9 * 60
    if minutes < 0:
        return 9 * 60
    return minutes


def _schedule_start_min_from_policy(policy: Any) -> int:
    if isinstance(policy, Mapping):
        return _safe_schedule_start_min(policy.get("schedule_start_min"))
    return 9 * 60


def _schedule_start_min_from_urban_profile(urban_profile: Any) -> int:
    if not isinstance(urban_profile, Mapping):
        return 9 * 60
    time_context = urban_profile.get("time_context")
    if not isinstance(time_context, Mapping):
        return 9 * 60
    value = str(time_context.get("inferred_start_time") or "").strip()
    match = re.search(r"(?:^|T|\s)([01]?\d|2[0-3]):([0-5]\d)", value)
    if not match:
        return 9 * 60
    return int(match.group(1)) * 60 + int(match.group(2))


def _format_clock(total_minutes: int) -> str:
    total_minutes = max(0, int(total_minutes))
    hours = (total_minutes // 60) % 24
    minutes = total_minutes % 60
    return f"{hours:02d}:{minutes:02d}"


def _queue_level(queue_risk: Any) -> str:
    value = _bounded_float(queue_risk, 0, 1, 0.45)
    if value < 0.3:
        return "low"
    if value < 0.6:
        return "medium"
    return "high"


def _profile_bonus(profile: str, categories: Sequence[str], weights: Mapping[str, Any], avg_queue: float) -> float:
    if profile in {"food_only", "food_focused"}:
        return 0.24 * categories.count("dining") * (1.0 + float(weights.get("food", 0.0) or 0.0))
    if profile in {"culture_focused", "sightseeing_focused"}:
        return 0.18 * categories.count("culture_entertainment") * (1.0 + float(weights.get("sightseeing", 0.0) or 0.0))
    if profile == "citywalk":
        return 0.24 * categories.count("culture_entertainment") * (
            1.0 + float(weights.get("travel_efficiency", 0.0) or 0.0) + float(weights.get("experience", 0.0) or 0.0)
        )
    if profile == "efficient":
        return 0.18 * (1.0 + float(weights.get("travel_efficiency", 0.0) or 0.0))
    if profile == "low_queue":
        return 0.35 * (1.0 - avg_queue)
    return 0.12


def _profile_reward_pull(profile: str) -> float:
    if profile in {"food_only", "food_focused", "culture_focused", "sightseeing_focused", "citywalk"}:
        return 120.0
    if profile == "efficient":
        return 60.0
    return 90.0


def _profile_reward_pull_seconds(profile: str) -> float:
    return _profile_reward_pull(profile) * 0.75


def _match_allowed_composition(
    allowed_compositions: Sequence[Mapping[str, Any]],
    dining_count: int,
    culture_count: int,
    other_count: int,
) -> bool:
    for composition in allowed_compositions:
        if (
            int(composition.get("dining", -1)) == dining_count
            and int(composition.get("culture_entertainment", -1)) == culture_count
            and int(composition.get("other", -1)) == other_count
        ):
            return True
    return False


def _has_duplicate_dining_brand(pois: Sequence[Mapping[str, Any]]) -> bool:
    seen = set()
    for poi in pois:
        if poi.get("category") != "dining":
            continue
        brand = _poi_brand_key(poi)
        if not brand:
            continue
        if brand in seen:
            return True
        seen.add(brand)
    return False


def _poi_brand_key(poi: Mapping[str, Any]) -> str:
    name = str(poi.get("name") or "").strip()
    if not name:
        return ""
    name = re.sub(r"[\(\[<（【].*?[\)\]>）】]", "", name)
    for separator in ("·", "•", "|", "｜", "/", "\\", "-", "—", "–"):
        if separator in name:
            name = name.split(separator, 1)[0]
            break
    for area in (
        "国贸",
        "王府井",
        "双井",
        "永安里",
        "华贸",
        "前门",
        "大栅栏",
        "和平门",
        "什刹海",
        "三里屯",
        "西单",
        "望京",
        "故宫",
    ):
        name = name.replace(area, "")
    for suffix in ("烤鸭", "北京菜", "涮肉", "羊肉", "爆肚", "炸酱面", "小吃", "清真", "餐厅", "饭店", "酒楼", "店"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return re.sub(r"\s+", "", name).casefold()


def _limit_candidates(
    pois: Sequence[Dict[str, Any]],
    max_total: int = 14,
    per_category: int = 5,
    duration_budget_min: int = 180,
) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    selected_keys: set[str] = set()

    def candidate_key(poi: Mapping[str, Any]) -> str:
        location = poi.get("location")
        location_text = ""
        if isinstance(location, Mapping):
            location_text = f"{location.get('lng', '')},{location.get('lat', '')}"
        return str(poi.get("id") or f"{poi.get('name', '')}:{location_text}")

    def add_candidates(candidates: Sequence[Dict[str, Any]], limit: int) -> None:
        for poi in sorted(candidates, key=lambda item: (-float(item.get("_reward", 0.0)), item["name"], item["id"]))[:limit]:
            key = candidate_key(poi)
            if key in selected_keys:
                continue
            selected.append(poi)
            selected_keys.add(key)

    activity_slots: List[str] = []
    for poi in pois:
        for slot in [*_as_list(poi.get("matched_activity_slots")), *_as_list(poi.get("candidate_activity_slots"))]:
            slot_text = str(slot or "").strip()
            if slot_text and slot_text not in activity_slots:
                activity_slots.append(slot_text)
    for slot in activity_slots:
        slot_pois = [
            poi
            for poi in pois
            if slot in {str(item) for item in [*_as_list(poi.get("matched_activity_slots")), *_as_list(poi.get("candidate_activity_slots"))]}
        ]
        add_candidates(slot_pois, 4)

    activity_types: List[str] = []
    for poi in pois:
        for activity_type in _as_list(poi.get("activity_types")) or [poi.get("activity_type")]:
            type_text = str(activity_type or "").strip()
            if type_text and type_text not in activity_types:
                activity_types.append(type_text)
    for activity_type in activity_types:
        type_pois = [
            poi
            for poi in pois
            if activity_type in {str(item) for item in _as_list(poi.get("activity_types"))}
            or str(poi.get("activity_type") or "") == activity_type
        ]
        add_candidates(type_pois, 3)

    for category in ("dining", "culture_entertainment", "other"):
        category_pois = [poi for poi in pois if poi.get("category") == category]
        add_candidates(category_pois, per_category)
    selected = sorted(selected[:max_total], key=lambda poi: (_category_rank(poi), -float(poi.get("_reward", 0.0)), poi["name"], poi["id"]))
    radius_m = 12000 if duration_budget_min <= 240 else 18000
    return _prune_geo_outliers(selected, max_radius_m=radius_m)


def _prefer_local_food_candidates(
    pois: Sequence[Dict[str, Any]],
    composition_policy: Mapping[str, Any],
    prefer_local_food: bool,
) -> List[Dict[str, Any]]:
    if not prefer_local_food:
        return [dict(poi) for poi in pois]

    dining = [dict(poi) for poi in pois if poi.get("category") == "dining"]
    local_or_neutral_dining = [poi for poi in dining if not _is_generic_western_dining(poi)]
    try:
        min_dining = int(composition_policy.get("min_dining", 1) or 1)
    except (TypeError, ValueError):
        min_dining = 1
    if len(local_or_neutral_dining) < max(1, min_dining):
        return [dict(poi) for poi in pois]

    result: List[Dict[str, Any]] = []
    for poi in pois:
        if poi.get("category") == "dining" and _is_generic_western_dining(poi):
            continue
        result.append(dict(poi))
    return result or [dict(poi) for poi in pois]


def _adapt_visit_durations_for_policy(
    pois: Sequence[Dict[str, Any]],
    composition_policy: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    result = [dict(poi) for poi in pois]
    policy_type = str(composition_policy.get("policy_type") or "")
    try:
        duration_budget = int(float(composition_policy.get("duration_budget_min") or 180))
    except (TypeError, ValueError):
        duration_budget = 180
    if policy_type != "citywalk" or duration_budget > 210:
        return result

    for poi in result:
        category = str(poi.get("category") or "other")
        current = int(poi.get("visit_duration_min") or DEFAULT_VISIT_DURATION_MIN.get(category, 40))
        if category == "culture_entertainment":
            poi["visit_duration_min"] = min(current, 40)
        elif category == "other":
            poi["visit_duration_min"] = min(current, 30)
        elif category == "dining":
            poi["visit_duration_min"] = min(current, 35)
    return result


def _prune_geo_outliers(pois: Sequence[Dict[str, Any]], max_radius_m: float) -> List[Dict[str, Any]]:
    """Remove obvious geo outliers so 'nearby' routes do not jump to remote cities."""
    if len(pois) < 4:
        return list(pois)

    points: List[Tuple[float, float, Dict[str, Any]]] = []
    for poi in pois:
        location = _parse_location(poi)
        if location is None:
            continue
        points.append((float(location["lng"]), float(location["lat"]), poi))
    if len(points) < 4:
        return list(pois)

    best_index = 0
    best_sum = float("inf")
    for idx, (lng_a, lat_a, _) in enumerate(points):
        total = 0.0
        for lng_b, lat_b, _ in points:
            total += _haversine_lng_lat_m(lng_a, lat_a, lng_b, lat_b)
        if total < best_sum:
            best_sum = total
            best_index = idx

    center_lng, center_lat, _ = points[best_index]
    kept: List[Dict[str, Any]] = []
    for lng, lat, poi in points:
        distance = _haversine_lng_lat_m(center_lng, center_lat, lng, lat)
        if distance <= max_radius_m:
            kept.append(poi)

    # Avoid over-pruning sparse recall sets.
    if len(kept) >= 3:
        return sorted(kept, key=lambda poi: (_category_rank(poi), -float(poi.get("_reward", 0.0)), poi["name"], poi["id"]))
    return list(pois)


def _resolve_route_preference(
    context: Mapping[str, Any],
    previous_results: Sequence[Mapping[str, Any]],
    poi_data: Mapping[str, Any],
) -> Dict[str, Any]:
    candidates = [
        context.get("route_preference"),
        poi_data.get("route_preference") if isinstance(poi_data, Mapping) else None,
    ]
    for item in previous_results:
        result = item.get("result", {}) if isinstance(item, Mapping) else {}
        if isinstance(result, Mapping):
            candidates.append(result.get("route_preference"))
            data = result.get("data")
            if isinstance(data, Mapping):
                candidates.append(data.get("route_preference"))
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            resolved = {
                "route_type": str(candidate.get("route_type") or "auto"),
                "route_type_label": str(candidate.get("route_type_label") or candidate.get("route_type") or "系统自动判断"),
                "weights": _normalize_weights(candidate.get("weights")),
            }
            for key in ("semantic_tags", "recall_phrases"):
                values = [str(item).strip() for item in _as_list(candidate.get(key)) if str(item).strip()]
                if values:
                    resolved[key] = _unique_list(values)[:8]
            if candidate.get("travel_style"):
                resolved["travel_style"] = str(candidate.get("travel_style"))
            if candidate.get("adjustment_reasoning"):
                resolved["adjustment_reasoning"] = str(candidate.get("adjustment_reasoning"))
            return resolved
    return {"route_type": "auto", "route_type_label": "系统自动判断", "weights": dict(DEFAULT_WEIGHTS)}


def _normalize_weights(weights: Any) -> Dict[str, float]:
    source = weights if isinstance(weights, Mapping) else {}
    normalized = {}
    for key in ROUTE_WEIGHT_KEYS:
        try:
            normalized[key] = max(0.0, float(source.get(key, DEFAULT_WEIGHTS[key])))
        except (TypeError, ValueError):
            normalized[key] = DEFAULT_WEIGHTS[key]
    total = sum(normalized.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {key: round(value / total, 4) for key, value in normalized.items()}


def _low_queue_requested(weights: Mapping[str, Any], query_text: Any) -> bool:
    return float(weights.get("queue", 0.0) or 0.0) >= 0.12 or _queue_requested(query_text)


def _citywalk_requested(query_text: Any, route_preference: Optional[Mapping[str, Any]] = None) -> bool:
    route_preference = route_preference if isinstance(route_preference, Mapping) else {}
    parts = [
        query_text,
        route_preference.get("route_type"),
        route_preference.get("route_type_label"),
        route_preference.get("travel_style"),
        *(_as_list(route_preference.get("semantic_tags"))),
        *(_as_list(route_preference.get("recall_phrases"))),
    ]
    text = " ".join(str(part or "") for part in parts).casefold()
    return any(
        term in text
        for term in (
            "citywalk",
            "city work",
            "citywork",
            "\u57ce\u5e02\u6f2b\u6b65",
            "\u57ce\u5e02\u6b65\u884c",
            "\u80e1\u540c\u6f2b\u6b65",
            "\u8857\u533a\u6f2b\u6b65",
            "\u8f7b\u677e",
            "\u4f4e\u5f3a\u5ea6",
            "\u6563\u6b65",
        )
    )


def _queue_requested(query_text: Any) -> bool:
    text = str(query_text or "")
    return any(token in text for token in QUEUE_REQUEST_TERMS)


def _find_previous_data(previous_results: Sequence[Mapping[str, Any]], agent_name: str) -> Dict[str, Any]:
    target = agent_name.replace("-", "_")
    for item in previous_results:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("agent_name") or "").replace("-", "_")
        if name != target:
            continue
        result = item.get("result", {})
        if not isinstance(result, Mapping):
            continue
        data = result.get("data")
        if isinstance(data, Mapping):
            return dict(data)
        if "pois" in result or "poi_search_complete" in result:
            return dict(result)
    return {}


def _resolve_start_location(
    context: Mapping[str, Any],
    event_data: Mapping[str, Any],
    poi_data: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    for source in (
        poi_data.get("start_location") if isinstance(poi_data, Mapping) else None,
        event_data.get("start_location") if isinstance(event_data, Mapping) else None,
        context.get("start_location") if isinstance(context, Mapping) else None,
    ):
        normalized = _normalize_start_location(source)
        if normalized:
            return normalized
    return None


def _normalize_start_location(value: Any) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    if isinstance(value, Mapping):
        location = _parse_location(value)
        if location is None and isinstance(value.get("location"), Mapping):
            location = _parse_location(value.get("location"))
        if location is None:
            return None
        return {
            "name": str(value.get("name") or value.get("address") or "start"),
            "address": str(value.get("address") or value.get("name") or ""),
            "city": str(value.get("city") or ""),
            "citycode": str(value.get("citycode") or ""),
            "location": location,
            "source": str(value.get("source") or ""),
        }
    return None


def _route_location_payload(value: Mapping[str, Any]) -> Dict[str, Any]:
    location = _parse_location(value) or {}
    return {
        **location,
        "city": str(value.get("city") or value.get("cityname") or ""),
        "citycode": str(value.get("citycode") or ""),
    }


def _parse_location(poi: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    location = poi.get("location")
    lng = lat = None
    if isinstance(location, Mapping):
        lng = location.get("lng", location.get("longitude"))
        lat = location.get("lat", location.get("latitude"))
    elif isinstance(location, str) and "," in location:
        lng, lat = location.split(",", 1)
    elif isinstance(location, (list, tuple)) and len(location) >= 2:
        lng, lat = location[0], location[1]
    else:
        lng = poi.get("lng", poi.get("longitude"))
        lat = poi.get("lat", poi.get("latitude"))
    try:
        if lng is None or lat is None:
            return None
        return {"lng": float(lng), "lat": float(lat)}
    except (TypeError, ValueError):
        return None


def _haversine_meters(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_location = _parse_location(left)
    right_location = _parse_location(right)
    if left_location is None or right_location is None:
        return 0.0
    lat1 = math.radians(left_location["lat"])
    lat2 = math.radians(right_location["lat"])
    delta_lat = math.radians(right_location["lat"] - left_location["lat"])
    delta_lng = math.radians(right_location["lng"] - left_location["lng"])
    a = math.sin(delta_lat / 2.0) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2.0) ** 2
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _haversine_lng_lat_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    lat1_r = math.radians(lat1)
    lat2_r = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    a = math.sin(delta_lat / 2.0) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(delta_lng / 2.0) ** 2
    return EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _resolve_route_mode(context: Any, duration_budget_min: Any) -> str:
    context = context if isinstance(context, Mapping) else {}
    explicit_mode = _normalize_requested_route_mode(context.get("route_mode"))
    if explicit_mode:
        return explicit_mode
    query = _query_text(context)
    query_modes = (
        ("electrobike", ("\u7535\u52a8\u8f66", "\u7535\u52a8\u81ea\u884c\u8f66", "\u7535\u74f6\u8f66", "electrobike", "ebike")),
        ("driving", ("\u9a7e\u8f66", "\u5f00\u8f66", "\u81ea\u9a7e", "driving")),
        ("transit", ("\u516c\u5171\u4ea4\u901a", "\u516c\u4ea4", "\u5730\u94c1", "transit")),
        ("bicycling", ("\u9a91\u884c", "\u81ea\u884c\u8f66", "\u5355\u8f66", "bicycling", "cycling")),
        ("walking", ("\u6b65\u884c", "\u8d70\u8def", "citywalk", "walking")),
    )
    for mode, terms in query_modes:
        if any(term in query.casefold() for term in terms):
            return mode
    return MULTIMODAL_LOW_FRICTION


def _resolve_transport_mode(
    context: Any,
    route_preference: Any,
    urban_intent_profile: Any,
    strict_no_fallback: bool = False,
) -> str:
    context = context if isinstance(context, Mapping) else {}
    route_preference = route_preference if isinstance(route_preference, Mapping) else {}
    urban_intent_profile = urban_intent_profile if isinstance(urban_intent_profile, Mapping) else {}
    route_constraints = (
        urban_intent_profile.get("route_constraints")
        if isinstance(urban_intent_profile.get("route_constraints"), Mapping)
        else {}
    )
    explicit_route_mode = _normalize_requested_route_mode(context.get("route_mode"))
    if explicit_route_mode:
        return explicit_route_mode
    context_transport = context.get("transport_mode")
    if isinstance(context_transport, Mapping) and str(context_transport.get("source") or "") == "user_explicit":
        explicit_transport_mode = _normalize_requested_route_mode(context_transport)
        if explicit_transport_mode:
            return explicit_transport_mode
    query_mode = _route_mode_from_query(_query_text(context))
    if query_mode:
        return query_mode
    user_preferences = context.get("user_preferences") if isinstance(context.get("user_preferences"), Mapping) else {}
    memory_transport_mode = _route_mode_from_query(
        " ".join(
            str(user_preferences.get(key) or "")
            for key in ("transportation_preference", "transport_preference", "route_transport_preference")
        )
    )
    if memory_transport_mode:
        return memory_transport_mode
    candidates = (
        context_transport,
        route_preference.get("transport_mode"),
        route_preference.get("route_mode"),
        route_constraints.get("transport_mode") if isinstance(route_constraints, Mapping) else None,
        urban_intent_profile.get("transport_mode"),
    )
    for candidate in candidates:
        mode = _normalize_requested_route_mode(candidate)
        if mode:
            return mode
    return MULTIMODAL_LOW_FRICTION


def _route_mode_from_query(query: str) -> str:
    text = str(query or "").casefold()
    query_modes = (
        ("electrobike", ("\u7535\u52a8\u8f66", "\u7535\u52a8\u81ea\u884c\u8f66", "\u7535\u74f6\u8f66", "electrobike", "ebike")),
        ("driving", ("\u9a7e\u8f66", "\u5f00\u8f66", "\u81ea\u9a7e", "driving")),
        ("transit", ("\u516c\u5171\u4ea4\u901a", "\u516c\u4ea4", "\u5730\u94c1", "transit")),
        ("bicycling", ("\u9a91\u884c", "\u81ea\u884c\u8f66", "\u5355\u8f66", "bicycling", "cycling")),
        ("walking", ("\u6b65\u884c", "\u8d70\u8def", "citywalk", "walking")),
    )
    for mode, terms in query_modes:
        if any(term in text for term in terms):
            return mode
    return ""


def _normalize_requested_route_mode(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("mode") or value.get("route_mode") or value.get("transport_mode")
    mode = str(value or "").strip().casefold()
    aliases = {
        "bike": "bicycling",
        "bicycle": "bicycling",
        "cycling": "bicycling",
        "ebike": "electrobike",
        "electric_bike": "electrobike",
        "public_transit": "transit",
        "public_transport": "transit",
        "multimodal": MULTIMODAL_LOW_FRICTION,
        "low_friction": MULTIMODAL_LOW_FRICTION,
    }
    mode = aliases.get(mode, mode)
    return mode if mode in {"walking", "bicycling", "electrobike", "transit", "driving", MULTIMODAL_LOW_FRICTION} else ""


def _fallback_duration_sec(distance_m: Any, route_mode: str) -> float:
    speeds = {
        "walking": WALKING_SPEED_KMPH,
        "bicycling": 15.0,
        "electrobike": 20.0,
        "transit": 18.0,
        "driving": 25.0,
        MULTIMODAL_LOW_FRICTION: WALKING_SPEED_KMPH,
    }
    speed_kmph = speeds.get(route_mode, WALKING_SPEED_KMPH)
    return (float(distance_m or 0.0) / 1000.0) / speed_kmph * 3600.0


def _duration_minutes(duration_sec: Any, distance_m: Any, route_mode: str) -> float:
    try:
        seconds = float(duration_sec)
    except (TypeError, ValueError):
        seconds = _fallback_duration_sec(distance_m, route_mode)
    return max(0.0, seconds / 60.0)


def _default_leg_mode(route_mode: str) -> str:
    return "walking" if route_mode in {"multimodal", MULTIMODAL_LOW_FRICTION} else str(route_mode or "walking")


def _selection_reason_for_profile(profile: str) -> str:
    mapping = {
        "fastest": "fastest_profile",
        "shortest": "shortest_profile",
        "fewest_transfers": "fewest_transfers_profile",
        "low_walking": "low_walking_profile",
        "balanced": "balanced_profile",
    }
    return mapping.get(str(profile or ""), "profile_cost")


def _select_transport_candidate(
    base_cost: Mapping[str, Any],
    profile: str,
    previous_mode: str,
    route_mode: str,
    policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    candidates = base_cost.get("candidate_modes") if isinstance(base_cost.get("candidate_modes"), Mapping) else {}
    normalized: List[Dict[str, Any]] = []
    for mode, payload in candidates.items():
        if not isinstance(payload, Mapping):
            continue
        item = dict(payload)
        item["mode"] = str(item.get("mode") or mode)
        normalized.append(item)
    if not normalized:
        normalized.append(dict(base_cost))
    profile = str(profile or "balanced")

    def duration(item: Mapping[str, Any]) -> float:
        try:
            return float(item.get("duration_sec"))
        except (TypeError, ValueError):
            return _fallback_duration_sec(item.get("distance_m") or base_cost.get("distance_m") or 0, route_mode)

    def distance(item: Mapping[str, Any]) -> float:
        try:
            return float(item.get("distance_m"))
        except (TypeError, ValueError):
            return float(base_cost.get("distance_m") or 0.0)

    def switch_penalty(item: Mapping[str, Any]) -> float:
        mode = str(item.get("mode") or "")
        return 0.0 if not previous_mode or previous_mode == mode else 900.0

    def walking_penalty(item: Mapping[str, Any]) -> float:
        return distance(item) * 2.0 if str(item.get("mode") or "") == "walking" else 0.0

    def weather_mode_penalty(item: Mapping[str, Any]) -> float:
        if route_mode != MULTIMODAL_LOW_FRICTION:
            return 0.0
        weather = policy.get("weather_context") if isinstance(policy, Mapping) else {}
        mode = str(item.get("mode") or "")
        if not _weather_discourages_bicycling(weather):
            return 0.0
        if mode == "bicycling":
            return 7200.0 + distance(item) * 1.5
        if mode == "walking":
            return _weather_sensitive_walking_choice_penalty_points(distance(item)) * 180.0
        return 0.0

    if route_mode != MULTIMODAL_LOW_FRICTION:
        chosen = normalized[0]
    elif profile == "shortest":
        chosen = min(normalized, key=lambda item: (weather_mode_penalty(item), distance(item), duration(item), switch_penalty(item), str(item.get("mode") or "")))
    elif profile == "fewest_transfers":
        chosen = min(normalized, key=lambda item: (weather_mode_penalty(item), switch_penalty(item), duration(item), distance(item), str(item.get("mode") or "")))
    elif profile == "low_walking":
        chosen = min(normalized, key=lambda item: (weather_mode_penalty(item), walking_penalty(item), duration(item), distance(item), str(item.get("mode") or "")))
    elif profile == "balanced":
        chosen = min(normalized, key=lambda item: (weather_mode_penalty(item) + duration(item) + 0.08 * distance(item) + 0.5 * switch_penalty(item), distance(item)))
    else:
        chosen = min(normalized, key=lambda item: (weather_mode_penalty(item), duration(item), distance(item), switch_penalty(item), str(item.get("mode") or "")))
    result = dict(chosen)
    result["candidate_modes"] = {str(item.get("mode") or ""): dict(item) for item in normalized if str(item.get("mode") or "")}
    result["selection_reason"] = _selection_reason_for_profile(profile)
    return result


def _matrix_value(matrix: Any, left_index: int, right_index: int) -> Any:
    if not isinstance(matrix, Sequence):
        return None
    try:
        return matrix[left_index][right_index]
    except (IndexError, KeyError, TypeError):
        return None


def _start_distance(
    start_costs: Optional[Mapping[int, Mapping[str, Any]]],
    poi: Mapping[str, Any],
    start_location: Optional[Mapping[str, Any]] = None,
) -> float:
    cost = (start_costs or {}).get(int(poi.get("_matrix_index", -1))) or {}
    try:
        return float(cost.get("distance_m"))
    except (TypeError, ValueError):
        return _haversine_meters(start_location or {}, poi)


def _start_duration_sec(
    start_costs: Optional[Mapping[int, Mapping[str, Any]]],
    poi: Mapping[str, Any],
    start_location: Optional[Mapping[str, Any]] = None,
) -> float:
    cost = (start_costs or {}).get(int(poi.get("_matrix_index", -1))) or {}
    try:
        return float(cost.get("duration_sec"))
    except (TypeError, ValueError):
        return _fallback_duration_sec(_start_distance(start_costs, poi, start_location), "walking")


def _matrix_duration_sec(
    duration_matrix: Optional[Sequence[Sequence[float]]],
    left_index: int,
    right_index: int,
    distance_m: float,
) -> float:
    try:
        return float(_matrix_value(duration_matrix, left_index, right_index))
    except (TypeError, ValueError):
        return _fallback_duration_sec(distance_m, "walking")


def _time_fit_score(utilization: float) -> float:
    try:
        value = float(utilization)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    if value < 0.5:
        return max(0.0, value / 0.5 * 4.0)
    if value < 0.65:
        return 4.0 + (value - 0.5) / 0.15 * 6.0
    if value < 0.75:
        return 10.0 + (value - 0.65) / 0.10 * 5.0
    if value <= 1.05:
        return 18.0
    if value <= 1.2:
        return max(8.0, 18.0 - (value - 1.05) / 0.15 * 10.0)
    return max(0.0, 8.0 - (value - 1.2) * 30.0)


def _count_leg_sources(legs: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for leg in legs:
        source = str(leg.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
    return counts


def _enrich_route_option_legs(
    route_options: Sequence[Dict[str, Any]],
    route_client: Any,
    route_mode: str,
) -> None:
    """Attach final direction details to the ranked routes when available."""
    for option in route_options:
        warnings = list(option.get("warnings") or [])
        legs = option.get("legs") if isinstance(option.get("legs"), list) else []
        for leg in legs:
            leg.setdefault("from", leg.get("from_poi_name", ""))
            leg.setdefault("to", leg.get("to_poi_name", ""))
            leg.setdefault("mode", route_mode)
            leg.setdefault("selected_mode", leg.get("mode"))
            leg.setdefault("duration_min", leg.get("travel_duration_min", 0.0))
            leg.setdefault("source", "haversine_fallback")
            leg.setdefault("steps", [])
            leg.setdefault("polyline", "")
            if route_client is None:
                continue
            origin = leg.get("from_location")
            destination = leg.get("to_location")
            if not origin or not destination:
                continue
            leg_mode = str(leg.get("mode") or ("walking" if route_mode == "multimodal" else route_mode))
            try:
                detail = route_client.route_pair(origin, destination, route_mode=leg_mode)
            except Exception:
                warnings.append("amap_leg_detail_failed_using_matrix_cost")
                continue
            leg["distance_m"] = int(round(float(detail.get("distance_m") or leg.get("distance_m") or 0)))
            leg["travel_duration_min"] = round(
                _duration_minutes(detail.get("duration_sec"), leg["distance_m"], leg_mode),
                1,
            )
            leg["duration_min"] = leg["travel_duration_min"]
            leg["source"] = detail.get("source") or leg.get("source")
            leg["mode"] = detail.get("mode") or leg_mode
            leg["selected_mode"] = leg["mode"]
            leg["steps"] = list(detail.get("steps") or [])
            leg["polyline"] = str(detail.get("polyline") or "")
        option["warnings"] = _unique_list(warnings)
        option["matrix_source_summary"] = _count_leg_sources(legs)
        metrics = option.get("metrics") if isinstance(option.get("metrics"), dict) else {}
        metrics["matrix_source_summary"] = option["matrix_source_summary"]
        total_distance = int(round(sum(float(leg.get("distance_m", 0) or 0) for leg in legs)))
        travel_duration = round(sum(float(leg.get("travel_duration_min", 0) or 0) for leg in legs), 1)
        visit_duration = int(metrics.get("visit_duration_min", 0) or 0)
        estimated_duration = round(visit_duration + travel_duration, 1)
        option["total_distance_m"] = total_distance
        option["distance_m"] = total_distance
        option["estimated_duration_min"] = estimated_duration
        metrics["total_distance_m"] = total_distance
        metrics["distance_m"] = total_distance
        metrics["total_distance_km"] = round(total_distance / 1000.0, 3)
        metrics["travel_duration_min"] = travel_duration
        metrics["total_minutes"] = estimated_duration
        metrics["estimated_duration_min"] = estimated_duration
        option["schedule"] = _build_schedule(option.get("pois", []), legs, _schedule_start_min_from_policy(option))


def _refresh_route_client_diagnostics(diagnostics: Dict[str, Any], route_costs: Mapping[str, Any]) -> None:
    route_client = route_costs.get("route_client")
    count_start = route_costs.get("_route_client_count_start")
    if route_client is None or not isinstance(count_start, Mapping):
        return
    diagnostics["amap_distance_calls"] = int(getattr(route_client, "_distance_call_count", 0) or 0) - int(
        count_start.get("amap_distance_calls", 0) or 0
    )
    diagnostics["amap_direction_calls"] = int(getattr(route_client, "_direction_call_count", 0) or 0) - int(
        count_start.get("amap_direction_calls", 0) or 0
    )


def _travel_minutes(distance_m: float) -> float:
    return (distance_m / 1000.0) / WALKING_SPEED_KMPH * 60.0


def _visit_duration(poi: Mapping[str, Any]) -> int:
    for key in ("visit_duration_min", "visit_minutes", "suggested_duration_minutes"):
        try:
            value = poi.get(key)
            if value not in (None, "", []):
                return max(15, int(round(float(value))))
        except (TypeError, ValueError):
            continue
    return DEFAULT_VISIT_DURATION_MIN.get(str(poi.get("category") or "other"), DEFAULT_VISIT_DURATION_MIN["other"])


def _term_match(poi: Mapping[str, Any], terms: Sequence[str]) -> float:
    text = _poi_text(poi).casefold()
    if not text:
        return 0.0
    return _text_term_match(text, terms)


def _text_term_match(text: Any, terms: Sequence[str]) -> float:
    value = str(text or "").casefold()
    if not value:
        return 0.0
    hits = sum(1 for term in terms if term.casefold() in value)
    return min(1.0, hits / 2.0)


def _local_food_requested(query_text: Any, city: Any, weights: Optional[Mapping[str, Any]] = None) -> bool:
    text = str(query_text or "")
    if _requested_cuisine_terms(text):
        return False
    city_text = str(city or "")
    combined = f"{text} {city_text}"
    if not any(term in combined for term in BEIJING_CITY_TERMS):
        return False
    if any(term in text for term in (*LOCAL_FOOD_QUERY_TERMS, *LOCAL_FOOD_LEGACY_COMPAT_TERMS)):
        return True
    if isinstance(weights, Mapping):
        try:
            return float(weights.get("food", 0.0) or 0.0) >= 0.5
        except (TypeError, ValueError):
            return False
    return False


def _requested_cuisine_terms(query_text: Any) -> Tuple[str, ...]:
    text = str(query_text or "")
    for terms in CUISINE_REQUEST_TERMS.values():
        if any(term in text for term in terms):
            return tuple(terms)
    return ()


def _poi_core_text(poi: Mapping[str, Any]) -> str:
    parts = []
    for key in ("name", "type", "address", "business_area", "tag"):
        value = poi.get(key)
        if value:
            parts.append(str(value))
    parts.extend(str(value) for value in _as_list(poi.get("tags")) if value)
    ugc = poi.get("ugc") if isinstance(poi.get("ugc"), Mapping) else {}
    for key in ("tags", "review_keywords", "suitable_for"):
        parts.extend(str(value) for value in _as_list(ugc.get(key)) if value)
    return " ".join(parts)


def _poi_primary_text(poi: Mapping[str, Any]) -> str:
    parts = []
    for key in ("name", "type"):
        value = poi.get(key)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _is_generic_western_dining(poi: Mapping[str, Any]) -> bool:
    return str(poi.get("category") or "") == "dining" and _text_term_match(
        _poi_core_text(poi),
        GENERIC_WESTERN_DINING_TERMS,
    ) > 0


def _poi_text(poi: Mapping[str, Any]) -> str:
    parts = []
    for key in ("name", "type", "category", "address", "business_area", "tag", "indoor_outdoor", "micro_category"):
        value = poi.get(key)
        if value:
            parts.append(str(value))
    for key in ("tags", "weather_tags", "recall_keywords", "recall_sources", "recall_reasons"):
        parts.extend(str(value) for value in _as_list(poi.get(key)) if value)
    ugc = poi.get("ugc") if isinstance(poi.get("ugc"), Mapping) else {}
    for key in ("tags", "review_keywords", "suitable_for"):
        parts.extend(str(value) for value in _as_list(ugc.get(key)) if value)
    return " ".join(parts)


def _query_text(context: Mapping[str, Any]) -> str:
    parts = [
        context.get("rewritten_query"),
        context.get("original_query"),
        context.get("query"),
    ]
    key_entities = context.get("key_entities")
    if isinstance(key_entities, Mapping):
        parts.extend(key_entities.values())
    return " ".join(str(part) for part in parts if part)


def _category_rank(poi: Mapping[str, Any]) -> int:
    return {"dining": 0, "culture_entertainment": 1, "other": 2}.get(str(poi.get("category")), 3)


def _nested_get(mapping: Mapping[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _bounded_float(value: Any, minimum: float, maximum: float, default: float) -> float:
    return max(minimum, min(maximum, _to_float(value, default)))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, "", []):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", []):
            return value
    return None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable) and not isinstance(value, Mapping):
        return list(value)
    return [value]


def _unique_list(values: Sequence[Any]) -> List[Any]:
    result = []
    seen = set()
    for value in values:
        if value in (None, "", []):
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
