"""
Local mock UGC insight service.

The route planner should not depend on raw review text. This service converts
mock review records into structured signals such as queue risk, tags and tips.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - compatibility with older package name
    try:
        from duckduckgo_search import DDGS
    except ImportError:  # pragma: no cover - optional runtime dependency
        DDGS = None


class UGCService:
    """Loads and matches local POI UGC records."""

    DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "ugc" / "mock_poi_reviews.json"
    DEFAULT_WEB_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "ugc" / "web_ugc_cache.json"

    def __init__(
        self,
        data_path: Optional[str] = None,
        enable_web_fallback: Optional[bool] = None,
        web_cache_path: Optional[str] = None,
        web_timeout_sec: Optional[float] = None,
        web_max_results: Optional[int] = None,
    ) -> None:
        self.data_path = Path(data_path) if data_path else self.DEFAULT_DATA_PATH
        self.records = self._load_records()
        self._by_id = {
            str(record.get("poi_id")): record
            for record in self.records
            if record.get("poi_id")
        }
        if enable_web_fallback is None:
            enable_web_fallback = self._env_enabled("TRAVELER_ENABLE_WEB_UGC")
        self.enable_web_fallback = bool(enable_web_fallback)
        self.web_cache_path = Path(web_cache_path) if web_cache_path else self.DEFAULT_WEB_CACHE_PATH
        self.web_timeout_sec = self._env_float("TRAVELER_WEB_UGC_TIMEOUT", web_timeout_sec or 1.5)
        self.web_max_results = self._env_int("TRAVELER_WEB_UGC_MAX_RESULTS", web_max_results or 5)
        self._web_cache = self._load_web_cache()

    def find_by_poi(
        self,
        poi_id: Optional[str] = None,
        poi_name: Optional[str] = None,
        city: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find a UGC record by exact POI id, then by normalized name."""
        if poi_id and poi_id in self._by_id:
            return self._by_id[poi_id]

        normalized_name = self._normalize_name(poi_name)
        normalized_city = self._normalize_name(city)

        if not normalized_name:
            return None

        for record in self.records:
            record_city = self._normalize_name(record.get("city"))
            if normalized_city and record_city and not self._name_matches(normalized_city, record_city):
                continue

            record_name = self._normalize_name(record.get("poi_name"))
            aliases = [self._normalize_name(alias) for alias in record.get("aliases", [])]
            candidates = [record_name, *aliases]

            if any(self._name_matches(normalized_name, candidate) for candidate in candidates):
                return record

        return None

    def enrich_poi(self, poi: Dict[str, Any], visit_hour: Optional[int] = None) -> Dict[str, Any]:
        """Return a copy of a POI enriched with structured UGC signals."""
        enriched = dict(poi)
        record = self.find_by_poi(
            poi_id=str(poi.get("id", "")) or None,
            poi_name=poi.get("name"),
            city=poi.get("cityname") or poi.get("city"),
        )

        if not record:
            web_ugc = self.find_by_web(poi, visit_hour=visit_hour)
            if web_ugc:
                enriched["ugc"] = web_ugc
                if not enriched.get("estimated_cost") and web_ugc.get("estimated_cost") is not None:
                    enriched["estimated_cost"] = web_ugc["estimated_cost"]
                return enriched

            queue_risk = self._heuristic_queue_risk(poi, visit_hour)
            enriched["ugc"] = {
                "matched": False,
                "queue_risk": queue_risk,
                "queue_level": self.queue_level(queue_risk),
                "tags": self._heuristic_tags(poi),
                "tips": self._heuristic_tip(poi, queue_risk),
                "source": "heuristic",
                "confidence": "heuristic" if queue_risk is not None else "unknown",
            }
            return enriched

        queue_risk = self._queue_risk(record, visit_hour)
        enriched["ugc"] = {
            "matched": True,
            "rating": record.get("avg_rating"),
            "sentiment_score": record.get("sentiment_score"),
            "queue_risk": queue_risk,
            "queue_level": self.queue_level(queue_risk),
            "tags": record.get("tags", []),
            "review_keywords": record.get("review_keywords", []),
            "price_level": record.get("price_level"),
            "suitable_for": record.get("suitable_for", []),
            "tips": record.get("tips", ""),
            "source": "mock_ugc",
        }
        return enriched

    def enrich_pois(self, pois: List[Dict[str, Any]], visit_hour: Optional[int] = None) -> List[Dict[str, Any]]:
        """Enrich a list of POIs with UGC signals."""
        return [self.enrich_poi(poi, visit_hour=visit_hour) for poi in pois]

    def find_by_web(self, poi: Dict[str, Any], visit_hour: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Search public snippets for lightweight UGC signals before heuristics."""
        if not self.enable_web_fallback:
            return None

        cache_key = self._web_cache_key(poi)
        cached = self._web_cache.get(cache_key)
        if isinstance(cached, dict):
            result = dict(cached)
            result["cache_hit"] = True
            return result

        query = self._build_web_query(poi)
        search_results = self._search_web(query)
        if not search_results:
            return None

        web_ugc = self._extract_web_ugc(poi, search_results, visit_hour=visit_hour)
        if not web_ugc:
            return None

        self._web_cache[cache_key] = web_ugc
        self._save_web_cache()
        return dict(web_ugc)

    @staticmethod
    def queue_level(queue_risk: Optional[float]) -> str:
        if queue_risk is None:
            return "unknown"
        if queue_risk >= 0.7:
            return "high"
        if queue_risk >= 0.35:
            return "medium"
        return "low"

    def _load_records(self) -> List[Dict[str, Any]]:
        if not self.data_path.exists():
            return []
        with self.data_path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, list):
            raise ValueError(f"UGC data must be a list: {self.data_path}")
        return [item for item in data if isinstance(item, dict)]

    @staticmethod
    def _queue_risk(record: Dict[str, Any], visit_hour: Optional[int]) -> Optional[float]:
        if visit_hour is not None:
            by_hour = record.get("queue_risk_by_hour", {})
            value = by_hour.get(str(int(visit_hour))) if isinstance(by_hour, dict) else None
            if value is not None:
                return float(value)

        value = record.get("default_queue_risk")
        return float(value) if value is not None else None

    def _load_web_cache(self) -> Dict[str, Dict[str, Any]]:
        if not self.web_cache_path.exists():
            return {}
        try:
            with self.web_cache_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, dict)}

    def _save_web_cache(self) -> None:
        if not self.enable_web_fallback:
            return
        try:
            self.web_cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self.web_cache_path.with_suffix(self.web_cache_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(self._web_cache, file, ensure_ascii=False, indent=2)
            tmp_path.replace(self.web_cache_path)
        except OSError:
            return

    def _search_web(self, query: str) -> List[Dict[str, Any]]:
        if DDGS is None:
            return []

        for backend in ("bing", "duckduckgo", "auto"):
            try:
                try:
                    ddgs = DDGS(timeout=self.web_timeout_sec)
                except TypeError:
                    ddgs = DDGS()
                with ddgs:
                    results = list(
                        ddgs.text(
                            query,
                            max_results=self.web_max_results,
                            safesearch="on",
                            region="cn-zh",
                            backend=backend,
                        )
                    )
                if results:
                    return [item for item in results if isinstance(item, dict)]
            except Exception:
                continue
        return []

    def _extract_web_ugc(
        self,
        poi: Dict[str, Any],
        search_results: List[Dict[str, Any]],
        visit_hour: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        snippets: List[str] = []
        sources: List[Dict[str, str]] = []
        for item in search_results:
            title = str(item.get("title", "") or "").strip()
            snippet = str(
                item.get("body")
                or item.get("snippet")
                or item.get("content")
                or item.get("description")
                or ""
            ).strip()
            url = str(item.get("href") or item.get("url") or item.get("link") or "").strip()
            if not title and not snippet:
                continue
            snippets.append(f"{title} {snippet}".strip())
            if title or url:
                sources.append({"title": title[:80], "url": url})

        text = " ".join(snippets).casefold()
        if not text:
            return None

        queue_risk, queue_source = self._queue_risk_from_web_text(text)
        tags = self._tags_from_web_text(poi, text)
        estimated_cost = self._estimated_cost_from_web_text(text)
        rating = self._rating_from_web_text(text)

        if queue_risk is None and not tags and estimated_cost is None and rating is None:
            return None

        if queue_risk is None:
            queue_risk = self._heuristic_queue_risk(poi, visit_hour)
            queue_source = "heuristic"

        signal_count = sum(
            1
            for value in (queue_source == "web", bool(tags), estimated_cost is not None, rating is not None)
            if value
        )
        ugc: Dict[str, Any] = {
            "matched": True,
            "rating": rating,
            "queue_risk": queue_risk,
            "queue_level": self.queue_level(queue_risk),
            "tags": tags,
            "price_level": self._price_level_from_cost(estimated_cost),
            "estimated_cost": estimated_cost,
            "tips": self._web_tip(poi, queue_risk, queue_source),
            "source": "web_search",
            "confidence": "medium" if signal_count >= 2 else "low",
            "queue_estimation_source": queue_source,
            "evidence": [self._clean_snippet(snippet) for snippet in snippets[:3]],
            "sources": sources[:3],
        }
        return ugc

    @classmethod
    def _queue_risk_from_web_text(cls, text: str) -> tuple[Optional[float], str]:
        low_tokens = (
            "不用排队",
            "无需排队",
            "不排队",
            "不用等",
            "无需等位",
            "人少",
            "清静",
            "小众",
            "错峰",
        )
        high_tokens = (
            "排队很久",
            "排长队",
            "等位很久",
            "爆满",
            "拥挤",
            "人很多",
            "预约难",
            "一小时",
            "1小时",
            "半小时以上",
        )
        medium_tokens = ("排队", "等位", "人多", "热门", "需要预约")

        if any(token in text for token in low_tokens):
            return 0.18, "web"
        if any(token in text for token in high_tokens):
            return 0.78, "web"
        if any(token in text for token in medium_tokens):
            return 0.48, "web"
        return None, "unknown"

    @classmethod
    def _tags_from_web_text(cls, poi: Dict[str, Any], text: str) -> List[str]:
        tags: List[str] = []
        category = str(poi.get("category", ""))
        if category:
            tags.append(category)

        tag_rules = [
            ("老字号", ("老字号", "百年老店")),
            ("小吃", ("小吃", "豆汁", "卤煮", "炸酱面", "糖火烧", "驴打滚")),
            ("本地菜", ("本地人", "本地菜", "北京菜", "杭帮菜", "地道")),
            ("特色餐", ("特色菜", "招牌", "必吃", "推荐菜")),
            ("少排队", ("少排队", "不用排队", "无需排队", "人少", "小众")),
            ("平价", ("平价", "便宜", "性价比", "实惠")),
            ("地标", ("地标", "故宫", "天安门", "西湖", "景山")),
            ("博物馆", ("博物馆", "美术馆", "展览", "展馆")),
            ("文化体验", ("文化体验", "非遗", "胡同", "历史", "文化")),
            ("拍照", ("拍照", "出片", "打卡", "网红")),
            ("免费", ("免费", "免票")),
        ]

        for tag, tokens in tag_rules:
            if any(token in text for token in tokens):
                tags.append(tag)

        return cls._dedupe_list(tags)

    @staticmethod
    def _estimated_cost_from_web_text(text: str) -> Optional[float]:
        patterns = (
            r"(?:人均|均价|￥|¥)\s*[:：]?\s*(\d{1,4})",
            r"(\d{1,4})\s*元\s*/?\s*人",
            r"门票\s*[:：]?\s*(\d{1,4})\s*元",
        )
        values: List[int] = []
        for pattern in patterns:
            for match in re.findall(pattern, text):
                try:
                    value = int(match)
                except (TypeError, ValueError):
                    continue
                if 0 <= value <= 3000:
                    values.append(value)
        if not values:
            return None
        return float(values[0])

    @staticmethod
    def _rating_from_web_text(text: str) -> Optional[float]:
        for match in re.findall(r"([3-5](?:\.\d)?)\s*分", text):
            try:
                value = float(match)
            except ValueError:
                continue
            if 0 <= value <= 5:
                return value
        return None

    @staticmethod
    def _price_level_from_cost(cost: Optional[float]) -> Optional[str]:
        if cost is None:
            return None
        if cost <= 50:
            return "low"
        if cost <= 150:
            return "medium"
        return "high"

    @classmethod
    def _web_tip(cls, poi: Dict[str, Any], queue_risk: Optional[float], queue_source: str) -> str:
        if queue_risk is None:
            return "网络搜索未提到明确排队信息，可作为普通备选。"
        if queue_source == "web":
            if queue_risk >= 0.7:
                return "网络搜索提到人流或等位压力较高，建议提前预约或错峰。"
            if queue_risk >= 0.35:
                return "网络搜索提到可能需要等候，建议预留时间或提前取号。"
            return "网络搜索提到排队压力较低，适合少排队方案。"
        return cls._heuristic_tip(poi, queue_risk)

    @classmethod
    def _web_cache_key(cls, poi: Dict[str, Any]) -> str:
        city = cls._normalize_name(poi.get("cityname") or poi.get("city") or poi.get("pname"))
        name = cls._normalize_name(poi.get("name"))
        return f"{city}|{name}"

    @staticmethod
    def _build_web_query(poi: Dict[str, Any]) -> str:
        city = str(poi.get("cityname") or poi.get("city") or "").strip()
        name = str(poi.get("name") or "").strip()
        category = str(poi.get("category") or "")
        if category == "dining":
            suffix = "排队 点评 人均 推荐菜"
        elif category == "culture_entertainment":
            suffix = "排队 人多 预约 门票 游玩"
        else:
            suffix = "排队 点评 人均 推荐"
        return " ".join(part for part in (city, name, suffix) if part)

    @staticmethod
    def _clean_snippet(value: str, max_len: int = 140) -> str:
        cleaned = " ".join(str(value).split())
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[: max_len - 1] + "..."

    @staticmethod
    def _dedupe_list(values: List[Any]) -> List[Any]:
        result: List[Any] = []
        seen = set()
        for value in values:
            key = str(value)
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _env_enabled(name: str) -> bool:
        return os.getenv(name, "").strip().casefold() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        value = os.getenv(name)
        if value is None:
            return float(default)
        try:
            return float(value)
        except ValueError:
            return float(default)

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        value = os.getenv(name)
        if value is None:
            return int(default)
        try:
            return int(value)
        except ValueError:
            return int(default)

    @classmethod
    def _heuristic_queue_risk(cls, poi: Dict[str, Any], visit_hour: Optional[int]) -> Optional[float]:
        category = str(poi.get("category", ""))
        if category not in {"dining", "culture_entertainment"}:
            return None

        text = cls._normalize_name(
            " ".join(
                str(poi.get(key, ""))
                for key in ("name", "type", "address", "business_area", "adname")
            )
        )

        if category == "dining":
            risk = 0.42
            if any(token in text for token in ("来福士", "湖滨", "银泰", "西湖", "杭州酒家", "知味观", "热门")):
                risk += 0.18
            if any(token in text for token in ("创意", "私房", "本地", "社区", "茶餐厅", "小馆")):
                risk -= 0.12
            if visit_hour in {11, 12, 13, 18, 19}:
                risk += 0.12
        else:
            risk = 0.35
            if any(token in text for token in ("西湖", "雷峰塔", "宋城", "断桥", "景区", "风景名胜")):
                risk += 0.18
            if any(token in text for token in ("博物馆", "公园", "茶", "文化", "艺术", "展览")):
                risk -= 0.1
            if visit_hour in {10, 11, 14, 15}:
                risk += 0.06

        return max(0.08, min(0.88, round(risk, 2)))

    @classmethod
    def _heuristic_tags(cls, poi: Dict[str, Any]) -> List[str]:
        category = str(poi.get("category", ""))
        tags = [category] if category else []
        text = cls._normalize_name(str(poi.get("name", "")) + str(poi.get("type", "")))
        if any(token in text for token in ("杭帮", "浙菜", "杭州酒家", "中国菜")):
            tags.extend(["local_food", "杭帮菜"])
        if any(token in text for token in ("博物馆", "文化", "艺术", "茶")):
            tags.append("culture")
        return tags

    @classmethod
    def _heuristic_tip(cls, poi: Dict[str, Any], queue_risk: Optional[float]) -> str:
        if queue_risk is None:
            return ""
        category = str(poi.get("category", ""))
        if queue_risk >= 0.7:
            return "启发式判断排队风险较高，建议提前预约或避开高峰。"
        if queue_risk >= 0.35:
            if category == "dining":
                return "启发式判断排队风险中等，建议错峰到店或提前取号。"
            return "启发式判断人流中等，建议避开核心高峰时段。"
        return "启发式判断排队风险较低，适合作为少排队备选。"

    @staticmethod
    def _normalize_name(value: Optional[Any]) -> str:
        if value is None:
            return ""
        return "".join(str(value).casefold().split())

    @staticmethod
    def _name_matches(left: str, right: str) -> bool:
        if not left or not right:
            return False
        return left == right or left in right or right in left
