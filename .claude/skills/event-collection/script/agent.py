"""
事项收集智能体
职责：收集用户的出发地/事项地点/事项时间/返程地

核心功能：
- 提取出发地、目的地、时间、返程地等基础信息
- 识别缺失信息并提示
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List
import json
import logging
import re
import sys
import os

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

logger = logging.getLogger(__name__)


class EventCollectionAgent(AgentBase):
    """事项收集智能体"""

    def __init__(self, name: str = "EventCollectionAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content={}, role="assistant")

        # 解析输入内容
        content = x.content if not isinstance(x, list) else x[-1].content

        # 如果content是JSON字符串，解析它
        if isinstance(content, str):
            try:
                data = json.loads(content)
                context = data.get("context", {})
                query_parts = [
                    context.get("rewritten_query", ""),
                    context.get("original_query", ""),
                    context.get("query", ""),
                ]
                user_query = " ".join(str(part) for part in query_parts if part) or str(data)
                user_preferences = context.get("user_preferences", {})
            except json.JSONDecodeError:
                user_query = content
                user_preferences = {}
        else:
            user_query = str(content)
            user_preferences = {}

        # 构建用户背景信息
        background_info = ""
        if user_preferences:
            bg_parts = ["【用户背景信息】（可用于推断缺失信息）"]
            if user_preferences.get("home_location"):
                bg_parts.append(f"• 家庭住址: {user_preferences['home_location']}")
            if user_preferences.get("hotel_brands"):
                bg_parts.append(f"• 酒店偏好: {', '.join(user_preferences['hotel_brands'])}")
            if user_preferences.get("airlines"):
                bg_parts.append(f"• 航空偏好: {', '.join(user_preferences['airlines'])}")

            if len(bg_parts) > 1:
                background_info = "\n".join(bg_parts) + "\n\n"

        # 获取当前时间
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        prompt = f"""你是事项收集专家，负责提取旅行的基础信息。

【当前时间】
{current_date} {weekday}

{background_info}【用户输入】
{user_query}

【提取要求】
请尽可能提取以下信息：
1. origin - 出发地
2. destination - 目的地
3. start_date - 出发日期（YYYY-MM-DD格式）
4. end_date - 返程日期
5. duration_days - 行程天数
6. return_location - 返程地
7. trip_purpose - 行程目的

【日期处理规则】（重要）
- 当前时间是{current_date}
- 用户说"2月27日"或"2.27"等相对时间，请根据当前时间推断完整日期（年月日）
- 用户说"明天"、"后天"、"下周"等相对时间，请根据当前时间计算具体日期
- 所有日期必须输出完整的YYYY-MM-DD格式

【特殊处理】
- 对于"北京一日游"这类：destination和origin都设为北京
- 对于"一日游"：duration_days设为1
- 如果用户没说出发地，但有家庭住址信息，可推断出发地为家庭住址

【输出格式】(严格JSON)
{{
    "origin": "北京",
    "destination": "北京",
    "start_date": "2026-02-27",
    "end_date": "2026-02-27",
    "duration_days": 1,
    "return_location": "北京",
    "trip_purpose": "旅游",
    "missing_info": [],
    "extracted_count": 7,
    "summary": "北京一日游，2月27日"
}}

缺失的信息在missing_info中列出，对应字段设为null。
"""

        try:
            # 调用模型
            response = await self.model([
                {"role": "user", "content": prompt}
            ])

            # 获取响应文本 - 处理异步生成器
            text = ""
            if hasattr(response, '__aiter__'):
                # 异步生成器，需要迭代获取内容
                async for chunk in response:
                    if isinstance(chunk, str):
                        text = chunk
                    elif hasattr(chunk, 'content'):
                        if isinstance(chunk.content, str):
                            text = chunk.content
                        elif isinstance(chunk.content, list):
                            for item in chunk.content:
                                if isinstance(item, dict) and item.get('type') == 'text':
                                    text = item.get('text', '')
            elif hasattr(response, 'text'):
                text = response.text
            elif hasattr(response, 'content'):
                text = response.content
            elif isinstance(response, dict) and 'content' in response:
                text = response['content']
            else:
                text = str(response) if response else ""

            # 清理文本，移除markdown代码块标记
            text = text.strip()
            if text.startswith('```json'):
                text = text[7:]
            if text.startswith('```'):
                text = text[3:]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

            # 提取JSON
            start_idx = text.find('{')
            end_idx = text.rfind('}')

            if start_idx != -1 and end_idx != -1:
                json_str = text[start_idx:end_idx+1]
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError as e:
                    # 记录详细错误信息用于调试
                    logger.error(f"JSON parse failed. Text sample: {json_str[:100]}")
                    raise ValueError(f"Failed to parse JSON. Error: {e}")
            else:
                raise ValueError("No JSON found in response")
        except Exception as e:
            logger.debug(f"Event collection switched to local fallback: {e}")
            result = self._fallback_extract(user_query, user_preferences)

        # 返回JSON字符串格式
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    @classmethod
    def _fallback_extract(cls, user_query: str, user_preferences: dict = None) -> dict:
        """Rule-based fallback used when the LLM is unavailable."""
        from datetime import datetime

        user_preferences = user_preferences or {}
        text = str(user_query or "")
        today = datetime.now().strftime("%Y-%m-%d")

        destination = cls._extract_city(text)
        origin = None
        if destination and ("一日游" in text or "游玩" in text or "旅行" in text or "旅游" in text):
            origin = destination
        if not origin:
            origin = user_preferences.get("home_location")

        duration_days = 1 if any(term in text for term in ("一日游", "一天", "1天")) else None
        trip_purpose = "旅游" if any(term in text for term in ("游", "玩", "景点", "路线", "美食")) else None

        missing_info = []
        if not origin:
            missing_info.append("出发地")
        if not destination:
            missing_info.append("目的地")

        result = {
            "origin": origin,
            "destination": destination,
            "start_date": today,
            "end_date": today if duration_days == 1 else None,
            "duration_days": duration_days or 1,
            "return_location": origin,
            "trip_purpose": trip_purpose or "旅游",
            "missing_info": missing_info,
            "extracted_count": 7 - len(missing_info),
            "summary": cls._summary(destination, duration_days or 1, today),
            "fallback_used": True,
        }
        return result

    @staticmethod
    def _extract_city(text: str) -> Optional[str]:
        known_cities = (
            "杭州", "北京", "上海", "深圳", "广州", "成都", "南京", "苏州",
            "西安", "重庆", "武汉", "长沙", "厦门", "青岛", "天津",
        )
        for city in known_cities:
            if city in text:
                return city

        match = re.search(r"([\u4e00-\u9fa5]{2,8})(?:一日游|两日游|三日游|旅游|旅行|游玩)", text)
        return match.group(1) if match else None

    @staticmethod
    def _summary(destination: Optional[str], duration_days: int, start_date: str) -> str:
        city_text = destination or "目的地"
        if duration_days == 1:
            return f"{city_text}一日游，{start_date}"
        return f"{city_text}{duration_days}天行程，{start_date}出发"
