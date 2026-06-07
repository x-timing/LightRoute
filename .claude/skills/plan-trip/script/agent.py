"""
行程规划智能体
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Dict, Any
import json
import logging
import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))
# project_root = Path(__file__).parent.parent # Removed old logic
# sys.path.insert(0, str(project_root))

from utils.json_parser import robust_json_parse, extract_json_from_async_response

logger = logging.getLogger(__name__)


class ItineraryPlanningAgent(AgentBase):
    """
    行程规划智能体（主协调）
    职责：协调事项收集、路线规划、酒店规划等多个子任务

    整合三层编排智能体的结果，生成完整行程计划
    """

    def __init__(self, name: str = "ItineraryPlanningAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        from utils.skill_loader import SkillLoader
        self.skill_loader = SkillLoader()

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content={}, role="assistant")

        # 解析输入内容
        content = x.content if not isinstance(x, list) else x[-1].content

        # 初始化变量
        user_query = ""
        context_info = {}
        previous_results = []
        user_preferences = {}

        # 如果content是JSON字符串，解析它（来自OrchestrationAgent）
        if isinstance(content, str):
            try:
                data = json.loads(content)
                context_info = data.get("context", {})
                user_query = context_info.get("rewritten_query", "")
                previous_results = data.get("previous_results", [])
                user_preferences = context_info.get("user_preferences", {})
            except json.JSONDecodeError:
                user_query = content
        elif isinstance(content, dict):
            context_info = content
            user_query = content.get("rewritten_query", str(content))
            user_preferences = content.get("user_preferences", {})

        # 整合所有可用信息
        all_info = {
            "user_query": user_query,
            "context": context_info,
        }

        # 从previous_results中提取其他agent的数据
        for prev in previous_results:
            agent_name = self._canonical_agent_name(prev.get("agent_name", ""))
            result_data = prev.get("result", {}).get("data", {})
            if result_data and agent_name:
                all_info[agent_name] = result_data

        route_planning_data = all_info.get("route_planning", {})
        route_options = route_planning_data.get("route_options", []) if isinstance(route_planning_data, dict) else []
        if route_options:
            result = self._build_itinerary_from_route_options(
                route_options=route_options,
                route_planning_data=route_planning_data,
                event_data=all_info.get("event_collection", {}),
            )
            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")
        if isinstance(route_planning_data, dict) and route_planning_data:
            warnings = route_planning_data.get("warnings", []) or ["no_route_options_available"]
            result = {
                "itinerary": {
                    "title": "路线规划未生成可执行方案",
                    "duration": "未知",
                    "daily_plans": [
                        {
                            "day": 1,
                            "date": all_info.get("event_collection", {}).get("start_date", ""),
                            "city": all_info.get("event_collection", {}).get("destination", "目的地"),
                            "theme": "暂无可执行路线",
                            "activities": [],
                            "meals": {},
                        }
                    ],
                    "route_options": [],
                    "notes": ["路线规划没有返回可用方案：" + "、".join(str(item) for item in warnings)],
                },
                "planning_complete": False,
                "route_planning_used": True,
                "warnings": warnings,
            }
            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

        # 构建用户偏好信息
        preferences_info = ""
        if user_preferences:
            pref_parts = ["【用户偏好】（规划时优先考虑）"]
            if user_preferences.get("home_location"):
                pref_parts.append(f"• 家庭住址: {user_preferences['home_location']}")
            if user_preferences.get("hotel_brands"):
                pref_parts.append(f"• 酒店偏好: {', '.join(user_preferences['hotel_brands'])}")
            if user_preferences.get("airlines"):
                pref_parts.append(f"• 航空偏好: {', '.join(user_preferences['airlines'])}")
            if user_preferences.get("seat_preference"):
                pref_parts.append(f"• 座位偏好: {user_preferences['seat_preference']}")

            if len(pref_parts) > 1:
                preferences_info = "\n".join(pref_parts) + "\n\n"

        # 获取当前时间
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        current_month = datetime.now().month
        current_season = "冬季" if current_month in [12, 1, 2] else \
                        "春季" if current_month in [3, 4, 5] else \
                        "夏季" if current_month in [6, 7, 8] else "秋季"
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        # 尝试从 SKILL.md 动态读取详细指令 (Progressive Disclosure)
        skill_instruction = self.skill_loader.get_skill_content("plan-trip")
        if not skill_instruction:
            # Fallback: 如果读取失败，使用默认的简单指令
            skill_instruction = "请根据用户需求和偏好生成行程规划。"

        prompt = f"""你是一个高级行程规划专家。

【当前时间】
{current_date} {weekday}，当前季节是{current_season}

【用户需求】
{user_query}

{preferences_info}【所有收集的信息】
{json.dumps(all_info, ensure_ascii=False, indent=2)}

【任务说明与指南】
{skill_instruction}

请直接输出 JSON 格式的行程规划。
"""

        try:
            # 调用模型 - 使用消息列表格式
            response = await self.model([
                {"role": "user", "content": prompt}
            ])

            # 获取响应文本
            text = await extract_json_from_async_response(response)

            # 解析结果
            result = None
            
            # 策略1: 尝试标准解析 (依赖 robust_json_parse 的清洗能力)
            try:
                result = robust_json_parse(text, fallback=None)
            except Exception:
                # 策略2: 使用 raw_decode 解析前缀 JSON (最强力，能忽略尾随文本如 Thinking)
                try:
                    # 再次清理 Markdown (以防 extract_json_from_async_response 漏网)
                    clean_text = text
                    if "```" in clean_text:
                        import re
                        clean_text = re.sub(r'```json\s*', '', clean_text, flags=re.IGNORECASE)
                        clean_text = re.sub(r'```', '', clean_text)
                    
                    clean_text = clean_text.strip()
                    start_idx = clean_text.find('{')
                    
                    if start_idx != -1:
                        # 从第一个 { 开始尝试解析
                        clean_text = clean_text[start_idx:]
                        decoder = json.JSONDecoder()
                        obj, _ = decoder.raw_decode(clean_text)
                        result = obj
                    else:
                        raise ValueError("No JSON object start '{' found")
                except Exception as decode_err:
                    # 如果策略2也失败，抛出包含详细信息的异常
                    raise ValueError(f"All JSON parsing attempts failed. Strategy 2 error: {decode_err}")

            if result is None:
                raise ValueError("Parsed result is None")

        except Exception as e:
            logger.error(f"Itinerary planning failed: {e}")
            # Ensure text is defined for logging even if extraction failed
            # 使用 locals().get 安全获取 text，防止 UnboundLocalError
            raw_text = locals().get('text', 'N/A')
            logger.error(f"Raw response text (first 500 chars): {str(raw_text)[:500]}")

            # 构建用户友好的错误消息
            error_detail = str(e)
            if "JSON" in error_detail or "parse" in error_detail.lower():
                user_message = "抱歉，模型返回的数据格式有误，无法解析行程信息。请稍后重试或简化您的需求描述。"
            else:
                user_message = f"行程规划过程中出现问题：{error_detail}"

            result = {
                "itinerary": {
                    "title": "行程规划",
                    "duration": "待完善",
                    "daily_plans": []
                },
                "planning_complete": False,
                "error": user_message,
                "technical_error": str(e)  # 保留技术细节用于调试
            }

        # 返回JSON字符串格式
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    def _build_itinerary_from_route_options(
        self,
        route_options: List[Dict[str, Any]],
        route_planning_data: Dict[str, Any],
        event_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        primary_route = route_options[0]
        primary_metrics = primary_route.get("metrics", {})
        route_names = [poi.get("name", "") for poi in primary_route.get("pois", []) if poi.get("name")]
        city = event_data.get("destination") or event_data.get("city") or "目的地"

        daily_plan = {
            "day": 1,
            "date": event_data.get("start_date", ""),
            "city": city,
            "theme": primary_route.get("title", "推荐路线"),
            "activities": self._activities_from_route(primary_route),
            "meals": self._meals_from_route(primary_route),
        }

        notes = self._notes_from_route_planning(primary_route, route_planning_data)
        compact_options = [dict(route) for route in route_options]

        return {
            "itinerary": {
                "title": f"{city}智能路线规划",
                "duration": self._format_duration(primary_metrics.get("total_minutes")),
                "route": " -> ".join(route_names),
                "daily_plans": [daily_plan],
                "route_options": compact_options,
                "notes": notes,
                "estimated_budget": self._format_budget(primary_metrics.get("estimated_cost")),
                "metrics": primary_metrics,
            },
            "planning_complete": True,
            "route_planning_used": True,
            "warnings": route_planning_data.get("warnings", []),
        }

    def _activities_from_route(self, route: Dict[str, Any]) -> List[Dict[str, Any]]:
        activities = []
        pois_by_id = {poi.get("id"): poi for poi in route.get("pois", [])}
        legs = route.get("legs", [])
        for index, slot in enumerate(route.get("schedule", [])):
            poi = pois_by_id.get(slot.get("poi_id"), {})
            leg = legs[index] if index < len(legs) else {}
            queue_level = slot.get("queue_level", "unknown")
            category = self._category_label(slot.get("category", poi.get("category", "other")))
            description_parts = [
                category,
                f"建议停留 {self._format_duration(slot.get('visit_minutes'))}",
                f"等候情况：{self._queue_wait_text(queue_level)}",
            ]
            tips = poi.get("ugc", {}).get("tips") if isinstance(poi.get("ugc"), dict) else ""
            if tips:
                description_parts.append(tips)

            transport = ""
            if leg:
                travel_minutes = self._safe_float(leg.get("travel_minutes", 0))
                distance_m = self._safe_float(leg.get("distance_m", 0))
                if index == 0 or (travel_minutes <= 0.5 and distance_m <= 1):
                    transport = "行程起点"
                else:
                    transport = f"前往本点 {self._format_duration(travel_minutes)}，距离约 {int(round(distance_m))} 米"

            activities.append(
                {
                    "time": f"{slot.get('arrival_time', '')}-{slot.get('departure_time', '')}",
                    "location": slot.get("poi_name", ""),
                    "description": "；".join(part for part in description_parts if part),
                    "transport": transport,
                }
            )
        return activities

    def _meals_from_route(self, route: Dict[str, Any]) -> Dict[str, str]:
        dining_slots = [
            slot for slot in route.get("schedule", [])
            if slot.get("category") == "dining"
        ]
        if not dining_slots:
            return {}

        meals = {}
        first_dining = dining_slots[0]
        arrival_hour = self._hour_from_time(first_dining.get("arrival_time", "12:00"))
        meal_text = f"{first_dining.get('poi_name', '')}（{self._queue_wait_text(first_dining.get('queue_level'))}）"
        if arrival_hour is not None and arrival_hour >= 16:
            meals["dinner"] = meal_text
        else:
            meals["lunch"] = meal_text
        return meals

    def _notes_from_route_planning(
        self,
        primary_route: Dict[str, Any],
        route_planning_data: Dict[str, Any],
    ) -> List[str]:
        metrics = primary_route.get("metrics", {})
        notes = [
            (
                f"主方案预计总时长 {self._format_duration(metrics.get('total_minutes'))}，"
                f"交通 {self._format_duration(metrics.get('travel_minutes'))}，"
                f"总距离约 {metrics.get('distance_m', 0)} 米。"
            ),
            f"{self._queue_wait_from_risk(metrics.get('avg_queue_risk'))}，预算估算 {self._format_budget(metrics.get('estimated_cost'))}。",
        ]

        warnings = list(primary_route.get("warnings", [])) + list(route_planning_data.get("warnings", []))
        explanations = primary_route.get("explanations", [])
        for explanation in explanations:
            if explanation:
                notes.append(str(explanation))
        if warnings:
            notes.append("约束提醒：" + "、".join(sorted(set(warnings))))
        else:
            notes.append("路线已满足至少3个地点，并覆盖餐饮与文化/娱乐两类。")
        return notes

    def _compact_route_option(self, route: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "profile": route.get("profile", ""),
            "title": route.get("title", "路线方案"),
            "poi_sequence": [poi.get("name", "") for poi in route.get("pois", []) if poi.get("name")],
            "metrics": route.get("metrics", {}),
            "constraints": route.get("constraints", {}),
            "explanations": route.get("explanations", []),
            "warnings": route.get("warnings", []),
        }

    @staticmethod
    def _format_duration(minutes: Any) -> str:
        try:
            value = float(minutes)
        except (TypeError, ValueError):
            return "未知"
        hours = int(value // 60)
        mins = int(round(value % 60))
        if hours and mins:
            return f"约{hours}小时{mins}分钟"
        if hours:
            return f"约{hours}小时"
        return f"约{mins}分钟"

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _format_budget(value: Any) -> str:
        try:
            return f"约{float(value):.0f}元"
        except (TypeError, ValueError):
            return "待估算"

    @staticmethod
    def _hour_from_time(value: str) -> Optional[int]:
        try:
            return int(str(value).split(":", 1)[0])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _category_label(category: str) -> str:
        labels = {
            "dining": "餐饮",
            "culture_entertainment": "文化/娱乐",
            "other": "其他",
        }
        return labels.get(str(category), str(category))

    @staticmethod
    def _queue_label(level: Any) -> str:
        labels = {
            "high": "高",
            "medium": "中",
            "low": "低",
            "unknown": "未知",
        }
        return labels.get(str(level), str(level))

    @staticmethod
    def _queue_wait_text(level: Any) -> str:
        labels = {
            "high": "排队概率较高，建议预留35分钟以上",
            "medium": "可能需要短暂等候，建议预留10-20分钟",
            "low": "大概率不用久等",
            "unknown": "排队情况暂不明确，建议到店前确认",
        }
        return labels.get(str(level), "排队情况暂不明确，建议到店前确认")

    @staticmethod
    def _queue_wait_from_risk(risk: Any) -> str:
        try:
            value = float(risk)
        except (TypeError, ValueError):
            return "整体排队情况暂不明确，建议出发前确认"

        if value < 0.25:
            return "整体看大概率不用久等"
        if value < 0.4:
            return "整体看有小可能需要等候，建议预留5-10分钟"
        if value < 0.6:
            return "整体看可能需要短暂等候，建议预留10-20分钟"
        if value < 0.75:
            return "整体排队概率偏高，建议预留20-35分钟"
        return "整体排队概率较高，建议预留35分钟以上"

    @staticmethod
    def _canonical_agent_name(agent_name: str) -> str:
        mapping = {
            "event-collection": "event_collection",
            "poi-search": "poi_search",
            "route-planning": "route_planning",
            "plan-trip": "itinerary_planning",
        }
        name = str(agent_name or "").strip()
        return mapping.get(name, name)
