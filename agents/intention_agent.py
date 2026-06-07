"""
意图识别智能体 IntentionRecognitionAgent
职责：准确识别用户意图，并进行智能体调度

核心功能：
1. 多意图识别和分类：融合上下文对模糊意图进行消歧
2. 智能体调度决策：基于预定义的触发条件和业务规则，根据识别结果决定调用哪些子智能体
3. Query改写：标准化用户口语化的query输入，补全上下文信息，提取和重组关键信息
4. 显示推理：输出的两段式结构（推理过程 + JSON决策），提升意图识别准确度

架构：
- 使用单一LLM（用户配置的模型）
- 输入：用户query（自然语言）
- 输出：推理过程生成（包含reasoning+原因） + 多意图识别（原因） + 智能Query改写 + 构建结构化决策
"""
from agentscope.agent import AgentBase
from agentscope.message import Msg
from typing import Optional, Union, List, Any, Dict, Tuple, Mapping, Sequence
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from utils.skill_loader import SkillLoader

logger = logging.getLogger(__name__)


ROUTE_WEIGHT_KEYS = (
    "sightseeing",
    "food",
    "experience",
    "travel_efficiency",
    "queue",
    "cost",
)

ROUTE_TYPE_LABELS = {
    "sightseeing": "打卡路线",
    "food": "美食路线",
    "balanced": "景点和餐饮兼顾",
    "citywalk": "轻松Citywalk路线",
    "auto": "系统自动判断",
}

ROUTE_WEIGHT_TEMPLATES: Dict[str, Dict[str, float]] = {
    "sightseeing": {
        "sightseeing": 0.55,
        "food": 0.20,
        "experience": 0.12,
        "travel_efficiency": 0.08,
        "queue": 0.03,
        "cost": 0.02,
    },
    "food": {
        "sightseeing": 0.22,
        "food": 0.55,
        "experience": 0.10,
        "travel_efficiency": 0.05,
        "queue": 0.04,
        "cost": 0.04,
    },
    "balanced": {
        "sightseeing": 0.40,
        "food": 0.40,
        "experience": 0.08,
        "travel_efficiency": 0.06,
        "queue": 0.03,
        "cost": 0.03,
    },
    "citywalk": {
        "sightseeing": 0.40,
        "food": 0.12,
        "experience": 0.18,
        "travel_efficiency": 0.22,
        "queue": 0.05,
        "cost": 0.03,
    },
    "auto": {
        "sightseeing": 0.38,
        "food": 0.32,
        "experience": 0.10,
        "travel_efficiency": 0.10,
        "queue": 0.05,
        "cost": 0.05,
    },
}

PLANNING_TURN_ACTIONS = {
    "new_plan",
    "revise_previous_plan",
    "expand_previous_plan",
    "answer_about_current_plan",
    "clarify_before_planning",
    "save_preference",
    "non_route_query",
}

CUISINE_PREFERENCE_SPECS = (
    {
        "cuisine": "川菜",
        "terms": ("川菜", "四川菜", "四川餐厅", "麻辣", "水煮鱼", "火锅"),
        "keywords": ["川菜 餐厅", "四川菜 餐厅", "麻辣 川菜", "水煮鱼 川菜"],
    },
    {
        "cuisine": "粤菜",
        "terms": ("粤菜", "广东菜", "广府菜", "早茶", "点心"),
        "keywords": ["粤菜 餐厅", "广东菜 餐厅", "广式早茶", "粤式点心"],
    },
    {
        "cuisine": "湘菜",
        "terms": ("湘菜", "湖南菜", "剁椒", "小炒黄牛肉"),
        "keywords": ["湘菜 餐厅", "湖南菜 餐厅", "剁椒 湘菜"],
    },
    {
        "cuisine": "火锅",
        "terms": ("火锅", "重庆火锅", "四川火锅"),
        "keywords": ["火锅 餐厅", "重庆火锅", "四川火锅"],
    },
)


class IntentionAgent(AgentBase):
    """意图识别智能体（IntentionRecognitionAgent）"""

    def __init__(self, name: str = "IntentionRecognitionAgent", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.conversation_history = []
        self.skill_loader = SkillLoader()
        self.last_intent_debug = {}

    def build_local_fallback(self, user_query: str, preset_route_type: str = "auto") -> dict:
        """Build the deterministic fallback used when interactive LLM intent recognition times out."""
        result = {
            "reasoning": "LLM intent recognition timed out; using deterministic local routing fallback.",
            "intents": [
                {
                    "type": "information_query",
                    "confidence": 0.5,
                    "description": "Default query intent",
                    "reason": "The interactive LLM intent call did not finish within its time box.",
                }
            ],
            "key_entities": {},
            "rewritten_query": user_query,
            "agent_schedule": [
                {
                    "agent_name": "information_query",
                    "priority": 1,
                    "reason": "Default query fallback",
                    "expected_output": "Query result",
                }
            ],
        }
        result = self._upgrade_fallback_if_needed(result, user_query)
        result["original_query"] = user_query
        result = self._normalize_agent_schedule(result)
        result = self._normalize_route_preference(result, user_query, preset_route_type)
        return self._normalize_urban_intent_profile(result, user_query)

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        """
        意图识别主流程
        1. 推理过程生成
        2. 多意图识别
        3. 智能Query改写
        4. 构建结构化决策
        """
        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        # 获取用户查询
        if isinstance(x, list):
            user_query = x[-1].content if x else ""
            preset_route_type = self._extract_preset_route_type(x)
            # 提取历史对话，保留角色信息
            self.conversation_history = []
            for msg in x[:-1]:
                if hasattr(msg, 'content') and hasattr(msg, 'role'):
                    # 区分处理不同角色的消息
                    if msg.role == "system":
                        # 长期记忆（system）- 完整保留，不截断
                        self.conversation_history.append(f"[系统记忆]\n{msg.content}")
                    else:
                        # 对话历史（user/assistant）- 适当截断但保留更多信息
                        role_name = "用户" if msg.role == "user" else "助手"
                        content = msg.content[:800] if len(msg.content) > 800 else msg.content
                        if len(msg.content) > 800:
                            content += "..."
                        self.conversation_history.append(f"{role_name}: {content}")
        else:
            user_query = x.content
            preset_route_type = self._extract_preset_route_type([x])

        if self._looks_like_itinerary_query(user_query) and self._should_use_local_route_intent():
            result = self.build_local_fallback(user_query, preset_route_type)
            result["reasoning"] = "Local route intent fast path used for an explicit city route request."
            intents = result.get("intents")
            if isinstance(intents, list) and intents and isinstance(intents[0], dict):
                intents[0]["reason"] = "Explicit route request matched deterministic LightRoute parsing rules."
            self.last_intent_debug = {
                "path": "local_route_intent",
                "query_length": len(str(user_query or "")),
                "preset_route_type": preset_route_type,
                "reason": "LIGHTROUTE_LOCAL_ROUTE_INTENT explicitly enabled",
            }
            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

        # 构建上下文
        # 策略：长期记忆始终保留，短期对话全部保留（已在 cli.py 控制数量）
        context_parts = []
        system_memories = []
        dialogue_history = []

        for item in self.conversation_history:
            if item.startswith("[系统记忆]"):
                system_memories.append(item)  # 保存全部系统上下文：长期记忆、偏好、起点、预设等
            else:
                dialogue_history.append(item)  # 保存对话历史

        # 组装上下文：长期记忆 + 全部对话
        if system_memories:
            context_parts.extend(system_memories)
        if dialogue_history:
            context_parts.extend(dialogue_history) 

        context_str = "\n".join(context_parts) if context_parts else "无历史对话"

        if self._looks_like_itinerary_query(user_query):
            try:
                result = await self._run_fast_route_intent_recognition(
                    user_query=user_query,
                    context_str=context_str,
                    preset_route_type=preset_route_type,
                )
            except Exception as e:
                logger.error(f"Fast route intent recognition failed: {e}")
                raise

            result["original_query"] = user_query
            result = self._normalize_agent_schedule(result)
            result = self._normalize_route_preference(result, user_query, preset_route_type)
            result = self._normalize_urban_intent_profile(result, user_query)
            return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

        # 获取当前时间
        self.last_intent_debug = {
            "path": "full_intent_prompt",
            "query_length": len(str(user_query or "")),
        }
        from datetime import datetime
        current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        weekday = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][datetime.now().weekday()]

        # 动态获取 Skills 描述
        skill_mapping = {
            "memory-query": "memory_query",
            "plan-trip": "itinerary_planning", 
            "preference": "preference",
            "query-info": "information_query",
            "ask-question": "rag_knowledge",
            "event-collection": "event_collection",
            "poi-search": "poi_search",
            "route-planning": "route_planning"
        }
        
        dynamic_skills_prompt = self.skill_loader.get_skill_prompt(skill_mapping)
        preset_route_type = self._canonical_route_type(preset_route_type)
        preset_route_label = ROUTE_TYPE_LABELS[preset_route_type]
        preset_weights = json.dumps(ROUTE_WEIGHT_TEMPLATES[preset_route_type], ensure_ascii=False)
        
        # 构建意图识别Prompt
        prompt = f"""你是一个高级意图识别专家（IntentionRecognitionAgent）。请分析用户查询，识别意图并输出结构化的决策。
        
【当前时间】
{current_time} {weekday}
（重要：当用户说"2月28日"或"明天"等相对时间时，请根据当前时间进行推断完整日期）

【用户Query】
{user_query}

【对话历史上下文】
{context_str}

【可调度的子智能体 (Skills)】
{dynamic_skills_prompt}

【路线偏好预设】
用户当前选择的路线偏好输入状态：{preset_route_type}（{preset_route_label}）
该状态的初始权重模板为：{preset_weights}

输入状态与语义路线说明：
- sightseeing（打卡路线）：强调景点数量、体验项目、文化娱乐覆盖优先；餐饮只保证必要体验，不要喧宾夺主。
- food（美食路线）：强调餐饮质量、特色菜、本地店、餐饮体验优先；景点可以少而精。
- balanced（景点和餐饮兼顾）：景点和餐饮都要照顾，适合作为默认稳妥方案。
- auto（系统自动判断）：用户跳过路线类型选择时使用，以中性权重开始，再根据用户文本动态调整。
- citywalk（轻松Citywalk路线）：不是菜单输入项，但当用户表达 citywalk、城市漫步、胡同漫步、轻松走走、别太累时，auto 可推断为该语义路线；如果用户已显式选了其他菜单项，也要在 semantic_tags/recall_phrases 中保留 citywalk 语义。

路线偏好权重规则：
- 如果是路线/行程规划类请求，必须输出 route_preference 独立字段。
- route_preference.weights 只表达偏好强弱，不要输出 min_dining、min_sightseeing、必须安排几餐、必须安排几个景点等硬约束。
- 权重字段固定为 sightseeing、food、experience、travel_efficiency、queue、cost，权重总和必须为 1.0。
- 结合用户语言动态调整：只有3小时/半天/赶时间 → 提高 travel_efficiency；citywalk/城市漫步/轻松/别太累 → 提高 travel_efficiency 和 experience；不想排队/别等位 → 提高 queue；预算有限/便宜点 → 提高 cost；多打卡/拍照/景点多 → 提高 sightseeing；想吃好/特色小吃/本地菜 → 提高 food。
- 可以在 route_preference 中输出 semantic_tags 和 recall_phrases。recall_phrases 用于 POI 检索关键词扩展，例如 ["天安门周边 citywalk", "胡同漫步", "历史街区", "公园轻松散步", "低强度步行路线"]。

城市微行程规则：
- 城市内短途生活场景必须输出 urban_intent_profile，例如下班后、刚起床、今天下午、约会、同学聚会、闺蜜局、按摩夜宵、美甲小酒、晚饭散步。
- activity_sequence 表达活动槽位和顺序，不要只归类为餐饮/景点。
- companions 必须识别同行人：solo、partner、friends、besties、classmates、colleagues、family、kids、unknown。
- 城市微行程默认需要天气和营业时间校验，route_constraints.require_opening_hours_check 与 weather_adaptive 应为 true。

【重要 - 意图区分原则】
请基于语义理解判断意图，不要机械匹配关键词。同一个词在不同语境下可能对应不同意图：
- "我去过北京吗？" → memory_query（询问自己的历史）
- "北京怎么样？" / "北京有什么好玩的？" → information_query（询问客观信息）
- "我想去北京" → itinerary_planning（规划未来行程）

优先级规则：
- memory_query 优先于 information_query（当问题涉及用户自己的历史时）
- 如果用户明确询问"我的"、"我过去的"，必须识别为 memory_query

【任务要求】
请按以下步骤进行分析：

**第1步：推理过程生成**
- 分析用户query的核心诉求
- 识别query中的关键实体和意图信号
- 判断是否需要结合对话历史进行消歧
- 说明如何融合上下文信息进行推理

**第2步：多意图识别（原因）**
- 识别所有可能的用户意图（可以是多个）
- 为每个意图分配置信度（0-1之间）
- 说明为什么识别出该意图的原因

**第3步：智能Query改写**
- 识别口语化表达，进行标准化
- 补全省略的上下文信息
- 提取和重组关键信息

**第4步：构建结构化决策**
- 基于识别的意图，决定调用哪些子智能体
- 说明调用顺序和优先级
- 输出结构化的调用策略

【输出格式要求】
必须严格按照以下JSON格式输出（**只输出JSON，不要有其他文本**）：

{{
    "reasoning": "这里是详细的推理过程，包含第1步的分析，说明如何理解用户query，如何结合上下文，如何识别意图信号",

    "intents": [
        {{
            "type": "意图类型（如：itinerary_planning, preference_collection, information_query等）",
            "confidence": 0.95,
            "description": "该意图的具体说明",
            "reason": "为什么识别出该意图的原因"
        }}
    ],

    "key_entities": {{
        "origin": "出发地（如果有）",
        "destination": "目的地（如果有）",
        "date": "日期（如果有）",
        "duration": "时长（如果有）",
        "other": "其他关键信息"
    }},

    "rewritten_query": "标准化、补全后的查询内容",

    "route_preference": {{
        "route_type": "{preset_route_type}",
        "route_type_label": "{preset_route_label}",
        "weights": {{
            "sightseeing": 0.0,
            "food": 0.0,
            "experience": 0.0,
            "travel_efficiency": 0.0,
            "queue": 0.0,
            "cost": 0.0
        }},
        "adjustment_reasoning": "说明初始权重和根据用户语言动态调整的依据；不要包含硬性数量约束",
        "semantic_tags": ["citywalk", "easy_walk"],
        "recall_phrases": ["citywalk 半日游", "胡同漫步", "历史街区", "公园轻松散步"]
    }},

    "urban_intent_profile": {{
        "intent_type": "urban_micro_trip",
        "scenario": "根据用户生活场景输出，如 after_work_relax_late_food / girls_afternoon_evening / full_day_photo_food / citywalk_easy",
        "time_context": {{
            "current_datetime": "{datetime.now(timezone(timedelta(hours=8))).isoformat()}",
            "timezone": "Asia/Shanghai",
            "relative_time_phrase": "用户提到的相对时间，如 下班/今天下午/刚起床",
            "inferred_start_time": "按当前真实时间和用户表达推断的ISO时间",
            "inferred_end_time": "按当前真实时间和时长推断的ISO时间",
            "duration_min": 180,
            "day_part": "morning/afternoon/evening/evening_to_late_night/full_day",
            "is_today_plan": true
        }},
        "weather_context": {{
            "source": "pending",
            "city": "目的地城市",
            "warnings": []
        }},
        "companions": [
            {{"type": "unknown", "label": "未说明", "group_size": null}}
        ],
        "social_context": {{
            "relationship_context": "unknown",
            "atmosphere_preference": [],
            "budget_sensitivity": "medium",
            "conversation_friendly": true,
            "photo_friendly": false,
            "privacy_need": "medium"
        }},
        "energy_level": "low/medium/high",
        "mood": ["relaxed"],
        "activity_sequence": [
            {{
                "type": "dining",
                "label": "晚饭",
                "order": 1,
                "duration_min": 70,
                "poi_keywords": ["晚饭", "聚餐", "餐厅"],
                "opening_hours_need": "meal_time_open",
                "weather_fit": "indoor_or_sheltered"
            }}
        ],
        "route_constraints": {{
            "prefer_low_intensity": true,
            "max_transfer_count": 1,
            "prefer_near_start": true,
            "avoid_closed_venues": true,
            "require_opening_hours_check": true,
            "weather_adaptive": true
        }}
    }},

    "agent_schedule": [
        {{
            "agent_name": "子智能体名称",
            "priority": 1,
            "reason": "调用该智能体的原因和依据",
            "expected_output": "期望该智能体提供什么输出"
        }}
    ]
}}

【重要提示 - 优先级设置规则】
优先级数字相同的智能体会**并行执行**，不同优先级按顺序批次执行。

**所有智能体优先级分组：**

**Priority 1（并行执行）- 信息收集类：**
- memory_query: 记忆查询智能体
- event_collection: 事项收集智能体
- preference: 偏好管理智能体
- information_query: 信息查询智能体（联网搜索）
- rag_knowledge: RAG知识库智能体（查询企业知识库）

**Priority 2（依赖 Priority 1）- POI 检索类：**
- poi_search: POI检索智能体（需要事项收集得到目的地城市，调用高德获取餐饮和文化/娱乐候选）

**Priority 3（依赖 Priority 2）- 路线优化类：**
- route_planning: 路线规划智能体（需要 POI 候选和用户偏好，计算多方案路线）

**Priority 4（依赖 Priority 3）- 行程表达类：**
- itinerary_planning: 行程规划智能体（优先使用 route_planning 的结构化路线结果生成可读行程）

**说明：**
- Priority 1 的智能体都是信息获取，互不依赖，可并行执行提升速度
- Priority 2 的 poi_search 需要使用 Priority 1 的 event_collection 结果
- Priority 3 的 route_planning 需要使用 poi_search 的 POI 候选
- Priority 4 的 itinerary_planning 需要使用 route_planning 的路线方案
- 示例：用户说"我要从天津去北京，喜欢住汉庭"
  → Priority 1: preference + event_collection（并行）
  → Priority 2: poi_search（使用目的地检索 POI）
  → Priority 3: route_planning（生成多方案路线）
  → Priority 4: itinerary_planning（生成可读行程）

请开始分析，直接输出JSON：
"""

        # 调用LLM进行意图识别
        try:
            # 构建符合OpenAI格式的messages
            messages = [
                {"role": "system", "content": "你是一个高级意图识别专家。只输出JSON格式的结果，不要输出其他文本。"},
                {"role": "user", "content": prompt}
            ]
            response = await self.model(messages)

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

            # 清理文本
            text = text.strip()
            if text.startswith('```json'):
                text = text[7:]
            if text.startswith('```'):
                text = text[3:]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

            # 解析JSON
            try:
                result = json.loads(text)
            except json.JSONDecodeError as e1:
                # 如果直接解析失败，尝试提取JSON
                start_idx = text.find('{')
                end_idx = text.rfind('}')

                if start_idx != -1 and end_idx != -1:
                    json_str = text[start_idx:end_idx+1]
                    try:
                        result = json.loads(json_str)
                    except json.JSONDecodeError as e2:
                        logger.error(f"JSON parse failed. Text sample: {json_str[:100]}")
                        raise ValueError(f"Failed to parse JSON. Error: {e2}")
                else:
                    raise ValueError(f"No JSON found in response. Parse error: {e1}")

        except Exception as e:
            logger.error(f"意图识别失败：{e}")
            # 返回默认结果
            result = {
                "reasoning": f"意图识别出错，使用默认策略。错误: {str(e)}",
                "intents": [
                    {
                        "type": "information_query",
                        "confidence": 0.5,
                        "description": "默认查询意图",
                        "reason": "无法解析用户意图，使用默认策略"
                    }
                ],
                "key_entities": {},
                "rewritten_query": user_query,
                "agent_schedule": [
                    {
                        "agent_name": "information_query",
                        "priority": 1,
                        "reason": "默认查询",
                        "expected_output": "查询结果"
                    }
                ]
            }

        result = self._upgrade_fallback_if_needed(result, user_query)
        result["original_query"] = user_query
        result = self._normalize_agent_schedule(result)
        result = self._normalize_route_preference(result, user_query, preset_route_type)
        result = self._normalize_urban_intent_profile(result, user_query)

        # 将结果转换为JSON字符串，因为Msg的content必须是字符串
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    async def classify_planning_turn(
        self,
        user_query: str,
        memory_preference_context: Optional[Mapping[str, Any]] = None,
        long_term_summary: str = "",
    ) -> Dict[str, Any]:
        """Decide whether the current user turn starts, revises, expands, or asks about a route."""
        memory_preference_context = memory_preference_context if isinstance(memory_preference_context, Mapping) else {}
        fallback = self._local_planning_turn_decision(user_query, memory_preference_context)
        if self.model is None:
            return fallback
        if fallback.get("action") == "new_plan":
            return fallback

        context_excerpt = json.dumps(memory_preference_context, ensure_ascii=False, default=str)[:2200]
        summary_excerpt = str(long_term_summary or "")[:800]
        prompt = f"""
Return compact JSON only. Classify the user's current turn in LightRoute.
Actions must be one of: new_plan, revise_previous_plan, expand_previous_plan,
answer_about_current_plan, clarify_before_planning, save_preference, non_route_query.

Decision policy:
- Current explicit user text overrides previous route and long-term memory.
- Use previous_route_turn only when the user refers to continuing/changing/adding to the current route.
- If the user says "再补充一些点位" / "再加几个点" and a previous route exists, choose expand_previous_plan.
- expand_previous_plan means regenerate a complete route with extra slots integrated, not just list places, unless the user only asks "还有哪些附近可去".
- If adding/changing may conflict with missing context, choose clarify_before_planning and provide one short question.
- Long-term preferences are soft defaults, never hard constraints.

Long-term summary excerpt:
{summary_excerpt}

Structured memory/recent context:
{context_excerpt}

User query:
{user_query}

JSON shape:
{{
  "action": "new_plan",
  "confidence": 0.0,
  "requires_confirmation": false,
  "confirmation_question": "",
  "reason": "",
  "carry_over": {{}},
  "changes": {{}},
  "rewritten_query_for_planning": "",
  "should_ask_route_preference": true,
  "should_ask_start_location": true
}}
"""
        try:
            response = await self.model(
                [
                    {"role": "system", "content": "Return valid compact JSON only."},
                    {"role": "user", "content": prompt},
                ]
            )
            text = await self._model_response_text(response)
            parsed = self._parse_json_object(text)
            return self._normalize_planning_turn_decision(
                user_query=user_query,
                candidate=parsed,
                fallback=fallback,
                memory_preference_context=memory_preference_context,
            )
        except Exception as exc:
            logger.info(f"Planning turn classifier fell back locally: {exc}")
            return fallback

    @classmethod
    def _normalize_planning_turn_decision(
        cls,
        user_query: str,
        candidate: Mapping[str, Any],
        fallback: Mapping[str, Any],
        memory_preference_context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        action = str(candidate.get("action") or fallback.get("action") or "new_plan").strip()
        if action not in PLANNING_TURN_ACTIONS:
            action = str(fallback.get("action") or "new_plan")
        try:
            confidence = float(candidate.get("confidence", fallback.get("confidence", 0.6)))
        except (TypeError, ValueError):
            confidence = float(fallback.get("confidence", 0.6) or 0.6)
        confirmation_question = str(
            candidate.get("confirmation_question")
            or fallback.get("confirmation_question")
            or ""
        ).strip()
        requires_confirmation = bool(
            candidate.get("requires_confirmation", fallback.get("requires_confirmation", False))
        )
        if action == "clarify_before_planning":
            requires_confirmation = True
            confirmation_question = confirmation_question or "你是想沿用上一条路线调整，还是重新规划一条新路线？"

        carry_over = candidate.get("carry_over") if isinstance(candidate.get("carry_over"), Mapping) else {}
        fallback_carry = fallback.get("carry_over") if isinstance(fallback.get("carry_over"), Mapping) else {}
        merged_carry_over = {**dict(fallback_carry), **dict(carry_over)}

        changes = candidate.get("changes") if isinstance(candidate.get("changes"), Mapping) else {}
        fallback_changes = fallback.get("changes") if isinstance(fallback.get("changes"), Mapping) else {}
        merged_changes = {**dict(fallback_changes), **dict(changes)}

        query_for_planning = str(
            candidate.get("rewritten_query_for_planning")
            or fallback.get("rewritten_query_for_planning")
            or user_query
        ).strip()

        should_ask_route_preference = bool(candidate.get("should_ask_route_preference", fallback.get("should_ask_route_preference", True)))
        should_ask_start_location = bool(candidate.get("should_ask_start_location", fallback.get("should_ask_start_location", True)))
        if action in {"revise_previous_plan", "expand_previous_plan", "answer_about_current_plan", "clarify_before_planning", "save_preference", "non_route_query"}:
            should_ask_route_preference = False
        if action in {"revise_previous_plan", "expand_previous_plan", "answer_about_current_plan", "clarify_before_planning", "save_preference", "non_route_query"}:
            should_ask_start_location = False

        return {
            "schema_version": "planning_turn_decision.v1",
            "action": action,
            "confidence": max(0.0, min(1.0, confidence)),
            "requires_confirmation": requires_confirmation,
            "confirmation_question": confirmation_question,
            "reason": str(candidate.get("reason") or fallback.get("reason") or "").strip(),
            "carry_over": cls._compact_jsonable(merged_carry_over),
            "changes": cls._compact_jsonable(merged_changes),
            "rewritten_query_for_planning": query_for_planning,
            "should_ask_route_preference": should_ask_route_preference,
            "should_ask_start_location": should_ask_start_location,
        }

    @classmethod
    def _local_planning_turn_decision(
        cls,
        user_query: str,
        memory_preference_context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        text = str(user_query or "").strip()
        previous = memory_preference_context.get("previous_route_turn") if isinstance(memory_preference_context, Mapping) else {}
        has_previous_route = cls._has_previous_route_context(previous)
        carry_over = cls._carry_over_from_previous_route(previous)
        lower_text = text.casefold()

        if cls._looks_like_preference_update(text):
            action = "save_preference"
            reason = "用户在表达长期偏好。"
        elif has_previous_route and cls._looks_like_route_answer_question(text):
            action = "answer_about_current_plan"
            reason = "用户在询问上一条路线。"
        elif has_previous_route and cls._looks_like_route_expansion(text):
            action = "expand_previous_plan"
            reason = "用户要求在上一条路线基础上补充点位。"
        elif has_previous_route and cls._cuisine_preference_from_text(text):
            action = "revise_previous_plan"
            reason = "用户要求把上一条路线的餐饮口味改成具体菜系。"
        elif has_previous_route and cls._looks_like_route_revision(text):
            action = "revise_previous_plan"
            reason = "用户要求调整上一条路线。"
        elif not has_previous_route and (cls._looks_like_route_expansion(text) or cls._looks_like_route_revision(text)):
            action = "clarify_before_planning"
            reason = "用户使用了延续上一轮的说法，但当前没有可用上一条路线。"
        elif cls._looks_like_itinerary_query(text):
            action = "new_plan"
            reason = "用户提出新的路线规划需求。"
        elif any(term in lower_text for term in ("route", "plan", "itinerary")):
            action = "new_plan"
            reason = "用户提出英文路线规划需求。"
        else:
            action = "non_route_query"
            reason = "用户输入不像路线规划或路线修订。"

        changes = cls._planning_changes_from_text(text, action)
        rewritten = text
        if action in {"expand_previous_plan", "revise_previous_plan"} and has_previous_route:
            previous_query = str(previous.get("user_query") or "").strip() if isinstance(previous, Mapping) else ""
            prefix = "延续上一条路线"
            if previous_query:
                prefix += f"（上一轮需求：{previous_query[:80]}）"
            rewritten = f"{prefix}，本轮要求：{text}"

        requires_confirmation = action == "clarify_before_planning"
        return {
            "schema_version": "planning_turn_decision.v1",
            "action": action,
            "confidence": 0.82 if action not in {"non_route_query", "clarify_before_planning"} else 0.7,
            "requires_confirmation": requires_confirmation,
            "confirmation_question": "你想沿用上一条路线补充点位，还是重新规划一条新路线？" if requires_confirmation else "",
            "reason": reason,
            "carry_over": carry_over,
            "changes": changes,
            "rewritten_query_for_planning": rewritten,
            "should_ask_route_preference": action == "new_plan",
            "should_ask_start_location": action == "new_plan",
        }

    @staticmethod
    def _has_previous_route_context(previous: Any) -> bool:
        if not isinstance(previous, Mapping):
            return False
        if previous.get("previous_route"):
            return True
        return any(previous.get(key) for key in ("destination", "start_location", "scenario", "activity_sequence"))

    @classmethod
    def _carry_over_from_previous_route(cls, previous: Any) -> Dict[str, Any]:
        if not isinstance(previous, Mapping):
            return {}
        route = previous.get("previous_route") if isinstance(previous.get("previous_route"), Mapping) else {}
        urban_activities = previous.get("activity_sequence") if isinstance(previous.get("activity_sequence"), list) else []
        carry_over = {
            "city": previous.get("destination"),
            "destination": previous.get("destination"),
            "start_location": previous.get("start_location") or route.get("start_location"),
            "duration_min": route.get("duration_min"),
            "transport_mode": previous.get("transport_mode"),
            "activity_sequence": urban_activities,
            "previous_route_sequence": route.get("first_sequence") if isinstance(route, Mapping) else None,
        }
        return {key: value for key, value in carry_over.items() if value not in (None, "", [])}

    @staticmethod
    def _looks_like_route_expansion(text: str) -> bool:
        value = str(text or "")
        expansion_terms = (
            "再补充", "补充一些点", "补充点位", "加几个点", "多加", "再加", "加上",
            "顺路", "附近还有", "还有什么", "再推荐", "多安排", "补一个", "补两个",
            "add", "more places", "more spots",
        )
        return any(term in value.casefold() for term in expansion_terms)

    @staticmethod
    def _looks_like_route_revision(text: str) -> bool:
        value = str(text or "")
        revision_terms = (
            "那", "换成", "改成", "调整", "优化", "重新排", "少排队", "不排队",
            "少走路", "别太累", "更轻松", "缩短", "延长", "删掉", "去掉", "替换",
            "revise", "change", "less queue", "less walking",
        )
        return any(term in value.casefold() for term in revision_terms)

    @staticmethod
    def _looks_like_route_answer_question(text: str) -> bool:
        value = str(text or "")
        question_terms = (
            "哪个", "哪条", "为什么", "区别", "对比", "安全吗", "会不会", "能不能",
            "多少钱", "多远", "多久", "是否", "解释", "推荐理由",
        )
        return any(term in value for term in question_terms) and not IntentionAgent._looks_like_route_expansion(value)

    @staticmethod
    def _looks_like_preference_update(text: str) -> bool:
        value = str(text or "")
        return any(term in value for term in ("我喜欢", "我不喜欢", "以后", "记住", "偏好", "不要给我推荐"))

    @classmethod
    def _planning_changes_from_text(cls, text: str, action: str) -> Dict[str, Any]:
        value = str(text or "")
        changes: Dict[str, Any] = {}
        if action in {"expand_previous_plan", "revise_previous_plan"}:
            slots = cls._activity_slots_from_text(value)
            if slots:
                changes["add_activity_slots"] = slots
        preference_delta = {}
        if any(term in value for term in ("少排队", "不排队", "不用排队", "别排队")):
            preference_delta["queue"] = 0.15
        if any(term in value for term in ("少走路", "别太累", "轻松", "不累")):
            preference_delta["travel_efficiency"] = 0.12
            preference_delta["experience"] = 0.08
        if any(term in value for term in ("美食", "小吃", "吃", "餐厅")):
            preference_delta["food"] = 0.12
        cuisine = cls._cuisine_preference_from_text(value)
        if cuisine and action in {"new_plan", "revise_previous_plan", "expand_previous_plan"}:
            changes["dining_preference"] = cuisine
            preference_delta["food"] = max(float(preference_delta.get("food", 0.0) or 0.0), 0.18)
        if preference_delta:
            changes["preference_delta"] = preference_delta
        return changes

    @staticmethod
    def _cuisine_preference_from_text(text: str) -> Dict[str, Any]:
        value = str(text or "")
        for spec in CUISINE_PREFERENCE_SPECS:
            if any(term in value for term in spec["terms"]):
                return {
                    "cuisine": spec["cuisine"],
                    "keywords": list(spec["keywords"]),
                    "semantic_tags": [f"cuisine:{spec['cuisine']}", spec["cuisine"]],
                    "recall_phrases": list(spec["keywords"]),
                }
        return {}

    @staticmethod
    def _activity_slots_from_text(text: str) -> List[Dict[str, Any]]:
        slot_map = [
            (("咖啡", "咖啡馆", "喝杯"), "cafe", "顺路咖啡", "dining", ["咖啡", "咖啡馆"]),
            (("书店", "看书"), "bookstore_reading", "书店坐坐", "culture_entertainment", ["书店"]),
            (("甜品", "蛋糕", "甜点"), "dessert", "甜品", "dining", ["甜品", "蛋糕"]),
            (("小酒", "酒吧", "清吧"), "drinks", "小酒馆", "dining", ["小酒馆", "清吧"]),
            (("展", "展览", "博物馆", "美术馆"), "museum_exhibition", "看展", "culture_entertainment", ["展览", "美术馆"]),
            (("按摩", "足疗", "放松"), "wellness", "按摩放松", "other", ["按摩", "足疗"]),
            (("美甲", "指甲"), "beauty", "美甲", "other", ["美甲"]),
            (("夜宵", "宵夜"), "late_night_food", "夜宵", "dining", ["夜宵"]),
            (("景点", "打卡", "拍照"), "photo_spot", "拍照打卡点", "culture_entertainment", ["拍照", "打卡"]),
        ]
        slots = []
        for terms, activity_type, label, category, keywords in slot_map:
            if any(term in text for term in terms):
                order = len(slots) + 1
                slots.append(
                    {
                        "slot_id": f"extra_{order}",
                        "activity_type": activity_type,
                        "activity_label": label,
                        "activity_group": IntentionAgent._activity_group(activity_type),
                        "poi_category": category,
                        "order": order,
                        "required": False,
                        "duration_min": 35,
                        "poi_keywords": keywords,
                        "opening_hours_need": "open_now",
                        "weather_fit": "weather_adaptive",
                    }
                )
        if not slots and IntentionAgent._looks_like_route_expansion(text):
            slots.append(
                {
                    "slot_id": "extra_1",
                    "activity_type": "flexible_stop",
                    "activity_label": "顺路停留点",
                    "activity_group": "experience",
                    "poi_category": "culture_entertainment",
                    "order": 1,
                    "required": False,
                    "duration_min": 30,
                    "poi_keywords": ["顺路", "休息", "轻松"],
                    "opening_hours_need": "open_now",
                    "weather_fit": "weather_adaptive",
                }
            )
        return slots[:3]

    @staticmethod
    def _compact_jsonable(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): IntentionAgent._compact_jsonable(item)
                for key, item in value.items()
                if item not in (None, "", [])
            }
        if isinstance(value, list):
            return [IntentionAgent._compact_jsonable(item) for item in value if item not in (None, "", [])]
        return value

    async def _run_fast_route_intent_recognition(
        self,
        user_query: str,
        context_str: str = "",
        preset_route_type: str = "auto",
    ) -> Dict[str, Any]:
        if self.model is None:
            raise RuntimeError("LLM model is required for route intent recognition")

        preset_route_type = self._canonical_route_type(preset_route_type)
        current_dt = datetime.now(timezone(timedelta(hours=8))).isoformat()
        memory_context = self._compact_route_context_excerpt(context_str, max_chars=900)
        prompt = f"""
Return compact JSON only for LightRoute urban micro-trip intent recognition.
No markdown. No chain-of-thought. Current time: {current_dt}
Preset route preference: {preset_route_type}
Long-term memory / recent context excerpt:
{memory_context}

User query:
{user_query}

Rules:
- The context may include memory_preference_context JSON. Use previous_route_turn only when the current query is elliptical, corrective, or asks to continue/change the previous plan.
- The context may include planning_turn_decision JSON. If action is expand_previous_plan or revise_previous_plan, preserve carry_over fields unless the user explicitly overrides them.
- For expand_previous_plan, integrate requested extra activity slots into a complete replanned route intent; do not answer with a bare list of places.
- Long-term user preferences are soft defaults. Current explicit user query always overrides memory.
- Food, drink, activity, companion, weather, and transport preferences should influence semantic_tags, recall_phrases, activities, or transport_mode, not hard route constraints.
- intent_type must be itinerary_planning for a city micro-trip request.
- Use open activity_type values; do not force activities into only dining/sightseeing.
- If citywalk/walk/stroll is explicit, transport_mode must be walking.
- If driving/electrobike/bicycling/transit is explicit, use that mode.
- If transport is not specified, transport_mode must be multimodal_low_friction and allowed_modes must be walking,bicycling,transit.
- Output short semantic slots only; LightRoute code will build the full schema.

JSON shape:
{{
  "intent_type": "itinerary_planning",
  "confidence": 0.0,
  "city": "",
  "duration_min": 0,
  "relative_time_phrase": "",
  "start_location_name": "",
  "scenario": "",
  "transport_mode": {{"mode": "", "allowed_modes": []}},
  "companions": [{{"type": "unknown", "label": "unknown", "group_size": null}}],
  "activities": [
    {{
      "activity_type": "",
      "activity_label": "",
      "activity_group": "",
      "poi_category": "",
      "order": 1,
      "duration_min": 0,
      "poi_keywords": [],
      "opening_hours_need": "open_now",
      "weather_fit": "weather_adaptive"
    }}
  ],
  "semantic_tags": [],
  "recall_phrases": [],
  "rewritten_query": ""
}}
"""
        self.last_intent_debug = {
            "path": "compact_route_intent_prompt",
            "prompt_length": len(prompt),
            "memory_context_length": len(memory_context),
            "query_length": len(str(user_query or "")),
            "preset_route_type": preset_route_type,
        }
        messages = [
            {"role": "system", "content": "Return valid compact JSON only."},
            {"role": "user", "content": prompt},
        ]
        response = await self.model(messages)
        text = await self._model_response_text(response)
        compact = self._parse_json_object(text)
        return self._build_fast_route_intent_result(
            user_query=user_query,
            compact=compact,
            preset_route_type=preset_route_type,
            current_dt=current_dt,
        )

    @staticmethod
    def _compact_route_context_excerpt(context_str: str, max_chars: int = 900) -> str:
        """Keep only routing-relevant memory/context before sending intent prompt."""
        text = str(context_str or "").strip()
        if not text:
            return ""
        keep_terms = (
            "memory_preference_context",
            "planning_turn_decision",
            "previous_route_turn",
            "start_location",
            "preset_route_type",
            "user_preferences",
            "previous_route",
            "route_preference",
            "activity_sequence",
            "transport_mode",
            "destination",
            "origin",
        )
        compact_lines: List[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if any(term in line for term in keep_terms):
                compact_lines.append(line[:360])
            if len("\n".join(compact_lines)) >= max_chars:
                break
        compact = "\n".join(compact_lines).strip()
        if not compact:
            compact = text[-max_chars:]
        if len(compact) > max_chars:
            compact = compact[:max_chars]
        return compact

    @staticmethod
    async def _model_response_text(response: Any) -> str:
        text = ""
        if hasattr(response, "__aiter__"):
            async for chunk in response:
                if isinstance(chunk, str):
                    text = chunk
                elif hasattr(chunk, "content"):
                    content = getattr(chunk, "content")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                parts.append(str(item.get("text") or ""))
                        if parts:
                            text = "".join(parts)
        elif hasattr(response, "text"):
            text = str(response.text or "")
        elif hasattr(response, "content"):
            text = str(response.content or "")
        elif isinstance(response, dict) and "content" in response:
            text = str(response.get("content") or "")
        else:
            text = str(response or "")
        return text.strip()

    @staticmethod
    def _parse_json_object(text: str) -> Dict[str, Any]:
        value = str(text or "").strip()
        if value.startswith("```json"):
            value = value[7:]
        if value.startswith("```"):
            value = value[3:]
        if value.endswith("```"):
            value = value[:-3]
        value = value.strip()
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            start_idx = value.find("{")
            if start_idx == -1:
                raise
            decoder = json.JSONDecoder()
            parsed, _end_idx = decoder.raw_decode(value[start_idx:])
        if not isinstance(parsed, dict):
            raise ValueError("intent response JSON must be an object")
        return parsed

    @classmethod
    def _build_fast_route_intent_result(
        cls,
        user_query: str,
        compact: Dict[str, Any],
        preset_route_type: str,
        current_dt: str,
    ) -> Dict[str, Any]:
        if not isinstance(compact, dict):
            raise ValueError("compact intent response must be an object")
        if "intents" in compact and "urban_intent_profile" in compact:
            return compact

        intent_type = str(compact.get("intent_type") or "").strip()
        if intent_type != "itinerary_planning":
            raise ValueError(f"unsupported_intent_type: {intent_type or 'missing'}")

        transport_mode = cls._compact_transport_mode(compact.get("transport_mode"))
        activities = cls._compact_activity_sequence(compact.get("activities"))
        if not activities:
            raise ValueError("missing_activity_sequence")

        city = str(compact.get("city") or "").strip()
        start_location = str(compact.get("start_location_name") or "").strip()
        duration_min = cls._compact_int(compact.get("duration_min"), 0)
        time_context = {
            "current_datetime": current_dt,
            "timezone": "Asia/Shanghai",
            "relative_time_phrase": str(compact.get("relative_time_phrase") or "").strip(),
            "is_today_plan": True,
        }
        if duration_min > 0:
            time_context["duration_min"] = duration_min

        route_type = cls._canonical_route_type(preset_route_type)
        semantics = {
            "semantic_tags": cls._as_text_list(compact.get("semantic_tags")),
            "recall_phrases": cls._as_text_list(compact.get("recall_phrases")),
        }
        return {
            "reasoning": "compact_llm_intent",
            "intents": [
                {
                    "type": "itinerary_planning",
                    "confidence": float(compact.get("confidence") or 0.85),
                    "description": "urban micro trip",
                    "reason": "LLM compact semantic slots",
                }
            ],
            "key_entities": {
                "destination": city,
                "city": city,
                "duration": duration_min or "",
                "start_location": start_location or None,
                "origin": start_location or None,
                "transport_mode": transport_mode["mode"],
            },
            "rewritten_query": str(compact.get("rewritten_query") or user_query).strip(),
            "route_preference": {
                "route_type": route_type,
                "route_type_label": ROUTE_TYPE_LABELS[route_type],
                "weights": dict(ROUTE_WEIGHT_TEMPLATES[route_type]),
                "adjustment_reasoning": "LLM compact semantic slots.",
                **{key: value for key, value in semantics.items() if value},
            },
            "urban_intent_profile": {
                "intent_type": "urban_micro_trip",
                "scenario": str(compact.get("scenario") or "urban_micro_trip").strip(),
                "time_context": time_context,
                "weather_context": {"source": "pending", "city": city, "warnings": []},
                "companions": cls._compact_companions(compact.get("companions")),
                "transport_mode": transport_mode,
                "activity_sequence": activities,
                "route_constraints": {
                    "prefer_low_intensity": True,
                    "max_transfer_count": 1,
                    "prefer_near_start": True,
                    "avoid_closed_venues": True,
                    "require_opening_hours_check": True,
                    "weather_adaptive": True,
                },
            },
            "agent_schedule": [
                {"agent_name": "event_collection", "priority": 1, "reason": "collect trip fields", "expected_output": "structured trip data"},
                {"agent_name": "poi_search", "priority": 2, "reason": "recall real POIs", "expected_output": "POI candidates"},
                {"agent_name": "route_planning", "priority": 3, "reason": "optimize route options", "expected_output": "route_options"},
                {"agent_name": "itinerary_planning", "priority": 4, "reason": "present route", "expected_output": "readable itinerary"},
            ],
        }

    @staticmethod
    def _compact_int(value: Any, default: int) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @classmethod
    def _compact_transport_mode(cls, value: Any) -> Dict[str, Any]:
        if isinstance(value, str):
            mode = value.strip()
            allowed_modes = [mode] if mode else []
        elif isinstance(value, Mapping):
            mode = str(value.get("mode") or "").strip()
            allowed_modes = cls._as_text_list(value.get("allowed_modes"))
        else:
            mode = ""
            allowed_modes = []
        if not mode:
            raise ValueError("missing_transport_mode")
        if mode == "multimodal_low_friction":
            allowed_modes = allowed_modes or ["walking", "bicycling", "transit"]
        else:
            allowed_modes = allowed_modes or [mode]
        return {
            "mode": mode,
            "allowed_modes": allowed_modes,
            "confidence": 0.85,
            "requires_user_confirmation": False,
        }

    @classmethod
    def _compact_companions(cls, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list) or not value:
            return [{"type": "unknown", "label": "unknown", "group_size": None}]
        companions = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            companion_type = str(item.get("type") or "unknown").strip() or "unknown"
            companions.append(
                {
                    "type": companion_type,
                    "label": str(item.get("label") or companion_type).strip() or companion_type,
                    "group_size": item.get("group_size"),
                }
            )
        return companions or [{"type": "unknown", "label": "unknown", "group_size": None}]

    @classmethod
    def _compact_activity_sequence(cls, value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        activities = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, Mapping):
                continue
            activity_type = str(item.get("activity_type") or item.get("type") or "").strip()
            label = str(item.get("activity_label") or item.get("label") or activity_type).strip()
            if not activity_type or not label:
                continue
            order = cls._compact_int(item.get("order"), index)
            duration_min = cls._compact_int(item.get("duration_min"), 60)
            activities.append(
                {
                    "slot_id": str(item.get("slot_id") or f"slot_{order}"),
                    "type": activity_type,
                    "activity_type": activity_type,
                    "label": label,
                    "activity_label": label,
                    "activity_group": str(item.get("activity_group") or cls._activity_group(activity_type)),
                    "poi_category": str(item.get("poi_category") or cls._poi_category_for_activity(activity_type)),
                    "order": order,
                    "required": bool(item.get("required", True)),
                    "duration_min": max(20, duration_min),
                    "poi_keywords": cls._as_text_list(item.get("poi_keywords")) or [label],
                    "opening_hours_need": str(item.get("opening_hours_need") or "open_now"),
                    "weather_fit": str(item.get("weather_fit") or "weather_adaptive"),
                }
            )
        return sorted(activities, key=lambda item: item.get("order", 999))

    @classmethod
    def default_route_preference(cls, route_type: str = "auto") -> Dict[str, Any]:
        route_type = cls._canonical_route_type(route_type)
        return {
            "route_type": route_type,
            "route_type_label": ROUTE_TYPE_LABELS[route_type],
            "weights": dict(ROUTE_WEIGHT_TEMPLATES[route_type]),
            "adjustment_reasoning": f"使用{ROUTE_TYPE_LABELS[route_type]}的初始权重模板。",
        }

    @classmethod
    def _normalize_route_preference(cls, result: dict, user_query: str, preset_route_type: str = "auto") -> dict:
        if not isinstance(result, dict):
            return result

        schedule = result.get("agent_schedule", [])
        if not cls._has_itinerary_intent(result, schedule if isinstance(schedule, list) else []):
            return result

        preset_route_type = cls._canonical_route_type(preset_route_type)
        existing = result.get("route_preference")
        if preset_route_type != "auto":
            route_preference = cls.default_route_preference(preset_route_type)
            existing_reason = ""
            if isinstance(existing, dict):
                existing_reason = str(existing.get("adjustment_reasoning") or "").strip()
                cls._merge_route_semantics(route_preference, existing)
            if existing_reason:
                route_preference["adjustment_reasoning"] = (
                    f"用户显式选择{ROUTE_TYPE_LABELS[preset_route_type]}，优先使用该路线偏好；{existing_reason}"
                )
        elif isinstance(existing, dict):
            route_type = cls._canonical_route_type(existing.get("route_type") or preset_route_type)
            route_preference = {
                "route_type": route_type,
                "route_type_label": ROUTE_TYPE_LABELS[route_type],
                "weights": cls._normalized_weights(existing.get("weights"), route_type),
                "adjustment_reasoning": str(existing.get("adjustment_reasoning") or "").strip()
                or f"使用{ROUTE_TYPE_LABELS[route_type]}的初始权重模板。",
            }
            cls._merge_route_semantics(route_preference, existing)
        else:
            route_preference = cls.default_route_preference(preset_route_type)

        adjusted_weights, adjustment_reasons = cls._apply_dynamic_weight_adjustments(
            route_preference["weights"],
            user_query,
        )
        route_preference["weights"] = adjusted_weights
        query_semantics = cls._derive_route_semantics(user_query)
        cls._merge_route_semantics(route_preference, query_semantics)
        if preset_route_type == "auto" and cls._canonical_route_type(route_preference.get("route_type")) == "auto":
            inferred_route_type = cls._infer_auto_route_type(adjusted_weights, user_query)
            if inferred_route_type != "auto":
                route_preference["route_type"] = inferred_route_type
                route_preference["route_type_label"] = ROUTE_TYPE_LABELS[inferred_route_type]
                adjustment_reasons.append(f"系统根据需求内容自动判断为{ROUTE_TYPE_LABELS[inferred_route_type]}")
        if adjustment_reasons:
            route_preference["adjustment_reasoning"] = (
                route_preference["adjustment_reasoning"].rstrip("。")
                + "；"
                + "；".join(adjustment_reasons)
                + "。"
            )

        result["route_preference"] = route_preference
        return result

    @classmethod
    def _normalize_urban_intent_profile(cls, result: dict, user_query: str) -> dict:
        if not isinstance(result, dict):
            return result

        schedule = result.get("agent_schedule", [])
        if not cls._has_itinerary_intent(result, schedule if isinstance(schedule, list) else []):
            return result

        existing = result.get("urban_intent_profile")
        profile = cls._derive_urban_intent_profile(user_query, existing if isinstance(existing, dict) else {})
        result["urban_intent_profile"] = profile
        return result

    @classmethod
    def _derive_urban_intent_profile(cls, user_query: str, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        existing = existing or {}
        text = str(user_query or "")
        now = datetime.now(timezone(timedelta(hours=8)))
        city = cls._infer_city(text, existing)
        duration_min = cls._infer_micro_trip_duration(text, existing)
        start_dt, end_dt = cls._infer_micro_trip_window(text, now, duration_min)
        companions = cls._infer_companions(text, existing)
        scenario = cls._infer_micro_trip_scenario(text, companions)
        activities = cls._infer_activity_sequence(text, scenario, duration_min)
        duration_min = max(duration_min, sum(int(item.get("duration_min") or 0) for item in activities) or duration_min)
        if end_dt <= start_dt:
            end_dt = start_dt + timedelta(minutes=duration_min)

        existing_time = existing.get("time_context") if isinstance(existing.get("time_context"), dict) else {}
        existing_weather = existing.get("weather_context") if isinstance(existing.get("weather_context"), dict) else {}

        profile = {
            "schema_version": "1.0",
            "intent_type": "urban_micro_trip",
            "original_query": text,
            "scenario": cls._scenario_payload(existing.get("scenario") or scenario),
            "confidence": float(existing.get("confidence") or 0.75),
            "time_context": {
                "current_datetime": now.isoformat(),
                "current_time": now.isoformat(),
                "timezone": "Asia/Shanghai",
                "requested_start_time": start_dt.isoformat(),
                "requested_end_time": end_dt.isoformat(),
                "inferred_start_time": start_dt.isoformat(),
                "inferred_end_time": end_dt.isoformat(),
                "duration_min": int(round((end_dt - start_dt).total_seconds() / 60)) or duration_min,
                "day_part": cls._day_part(start_dt, end_dt),
                "is_today_plan": start_dt.date() == now.date(),
                "is_time_sensitive": True,
            },
            "weather_context": {
                "source": "pending",
                "city": city,
                "query_time": now.isoformat(),
                "target_window": f"{start_dt.isoformat()}/{end_dt.isoformat()}",
                "condition": "unknown",
                "temperature_c": None,
                "precipitation_risk": "unknown",
                "wind_risk": "unknown",
                "comfort_level": "unknown",
                "outdoor_suitability": "unknown",
                "indoor_preferred": False,
                "warnings": [],
            },
            "companions": companions,
            "social_context": cls._infer_social_context(companions),
            "activity_sequence": activities,
            "route_constraints": {
                "prefer_low_intensity": cls._contains_any(text, ("轻松", "不累", "低强度", "散步", "放松")),
                "max_transfer_count": 1,
                "prefer_near_start": True,
                "avoid_closed_venues": True,
                "require_opening_hours_check": True,
                "weather_adaptive": True,
            },
        }
        profile["time_context"].update({k: v for k, v in existing_time.items() if v not in (None, "", [])})
        profile["weather_context"].update({k: v for k, v in existing_weather.items() if v not in (None, "", [])})
        return profile

    @staticmethod
    def _contains_any(text: str, terms: Sequence[str]) -> bool:
        lowered = str(text or "").casefold()
        return any(str(term).casefold() in lowered for term in terms)

    @classmethod
    def _infer_city(cls, text: str, existing: Mapping[str, Any]) -> str:
        for value in (
            existing.get("city"),
            (existing.get("weather_context") or {}).get("city") if isinstance(existing.get("weather_context"), dict) else None,
        ):
            if value:
                return str(value)
        for city in ("北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "西安", "武汉", "重庆", "天津"):
            if city in text:
                return city
        return "北京"

    @classmethod
    def _infer_micro_trip_duration(cls, text: str, existing: Mapping[str, Any]) -> int:
        time_context = existing.get("time_context") if isinstance(existing.get("time_context"), dict) else {}
        value = time_context.get("duration_min") or existing.get("duration_min")
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            pass
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:小时|h|hour|hours)", text, re.I)
        if match:
            return max(60, int(round(float(match.group(1)) * 60)))
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:分钟|min|mins|minutes)", text, re.I)
        if match:
            return max(45, int(round(float(match.group(1)))))
        if "半天" in text:
            return 240
        return 180

    @classmethod
    def _infer_micro_trip_window(cls, text: str, now: datetime, duration_min: int) -> Tuple[datetime, datetime]:
        range_match = re.search(r"([01]?\d|2[0-3])[:：]?([0-5]\d)?\s*[-~到至]\s*([01]?\d|2[0-3])[:：]?([0-5]\d)?", text)
        if range_match:
            sh = int(range_match.group(1))
            sm = int(range_match.group(2) or 0)
            eh = int(range_match.group(3))
            em = int(range_match.group(4) or 0)
            start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
            if end <= start:
                end += timedelta(days=1)
            return start, end
        if cls._contains_any(text, ("下班", "晚饭", "夜宵", "小酒", "按摩")):
            if now.hour < 18 or (now.hour == 18 and now.minute < 30):
                start = now.replace(hour=18, minute=30, second=0, microsecond=0)
            else:
                start = now + timedelta(minutes=15)
        elif cls._contains_any(text, ("下午", "午后")):
            start = now.replace(hour=14, minute=0, second=0, microsecond=0)
            if start <= now:
                start += timedelta(days=1)
        else:
            start = now + timedelta(minutes=15)
        return start, start + timedelta(minutes=duration_min)

    @classmethod
    def _infer_micro_trip_scenario(cls, text: str, companions: Sequence[Mapping[str, Any]]) -> str:
        if cls._contains_any(text, ("按摩", "足疗", "spa", "SPA")) and cls._contains_any(text, ("夜宵", "宵夜", "深夜", "烧烤", "小龙虾")):
            return "after_work_relax_late_food"
        if cls._contains_any(text, ("美甲", "做指甲")) and cls._contains_any(text, ("小酒", "酒吧", "小酌")):
            return "besties_beauty_drinks"
        if cls._contains_any(text, ("吃饭", "美食", "小吃", "本地小吃", "想吃", "午饭", "晚饭")) and cls._contains_any(text, ("散步", "走走", "逛逛", "逛", "游玩", "轻松游", "轻松玩", "citywalk")):
            return "after_work_social_evening"
        if cls._contains_any(text, ("citywalk", "city work", "citywork", "城市漫步", "城市步行", "散步", "轻松游", "轻松玩", "游玩", "逛逛")):
            return "easy_citywalk"
        if cls._contains_any(text, ("拍照", "打卡")) and cls._contains_any(text, ("吃饭", "美食", "午饭", "晚饭")):
            return "photo_food_day_trip"
        companion_types = {str(item.get("type") or "") for item in companions}
        if "partner" in companion_types:
            return "romantic_date_micro_trip"
        if "classmates" in companion_types:
            return "classmates_budget_gathering"
        if "besties" in companion_types:
            return "besties_social_micro_trip"
        if cls._contains_any(text, ("下班", "晚饭", "散步")):
            return "after_work_social_evening"
        return "general_urban_micro_trip"

    @classmethod
    def _infer_companions(cls, text: str, existing: Mapping[str, Any]) -> List[Dict[str, Any]]:
        current = existing.get("companions")
        if isinstance(current, list) and current:
            return [dict(item) for item in current if isinstance(item, dict)] or [{"type": "unknown", "label": "unknown", "group_size": None}]
        mapping = [
            ("partner", "伴侣", ("伴侣", "对象", "男友", "女友", "情侣", "约会", "爱人")),
            ("besties", "闺蜜", ("闺蜜", "姐妹")),
            ("classmates", "同学", ("同学", "同窗")),
            ("colleagues", "同事", ("同事", "客户", "商务")),
            ("family", "家人", ("家人", "爸妈", "父母", "老人")),
            ("kids", "孩子", ("孩子", "小孩", "亲子", "儿童")),
            ("friends", "朋友", ("朋友", "哥们", "好友")),
            ("solo", "一个人", ("一个人", "独自", "自己去")),
        ]
        for companion_type, label, terms in mapping:
            if cls._contains_any(text, terms):
                size = 1 if companion_type == "solo" else (3 if companion_type in {"friends", "classmates", "besties"} else 2)
                return [{"type": companion_type, "label": label, "group_size": size}]
        return [{"type": "unknown", "label": "unknown", "group_size": None}]

    @staticmethod
    def _infer_social_context(companions: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        companion_type = str((companions[0] or {}).get("type") if companions else "unknown")
        presets = {
            "partner": ("romantic", ["quiet", "romantic", "night_view"], "medium", True, True, "medium_high"),
            "classmates": ("classmates", ["budget", "lively", "easy_chat"], "high", True, True, "low"),
            "besties": ("besties", ["photo_friendly", "relaxed", "drinks"], "medium", True, True, "medium"),
            "colleagues": ("work_social", ["business", "convenient", "private_room"], "medium", True, False, "medium"),
            "family": ("family", ["comfortable", "low_intensity"], "medium", True, False, "medium"),
            "kids": ("family_kids", ["comfortable", "kids_friendly"], "medium", False, False, "medium"),
        }
        relationship, atmosphere, budget, conversation, photo, privacy = presets.get(
            companion_type,
            ("unknown", ["flexible"], "medium", True, False, "medium"),
        )
        return {
            "relationship_context": relationship,
            "atmosphere_preference": atmosphere,
            "budget_sensitivity": budget,
            "conversation_friendly": conversation,
            "photo_friendly": photo,
            "privacy_need": privacy,
        }

    @classmethod
    def _infer_activity_sequence(cls, text: str, scenario: str, duration_min: int) -> List[Dict[str, Any]]:
        specs = [
            ("wellness", "按摩放松", ("按摩", "足疗", "SPA", "spa"), 90, ["按摩", "足疗", "SPA", "放松"], "evening_open", "indoor"),
            ("late_night_food", "夜宵", ("夜宵", "宵夜", "烧烤", "小龙虾", "深夜食堂"), 60, ["夜宵", "烧烤", "小龙虾", "深夜食堂"], "late_night_open", "indoor_or_sheltered"),
            ("beauty", "美甲", ("美甲", "做指甲"), 75, ["美甲", "做指甲", "美睫"], "open_now", "indoor"),
            ("drinks", "小酒", ("小酒", "小酌", "酒吧", "bistro", "精酿"), 75, ["小酒馆", "酒吧", "精酿", "bistro"], "evening_open", "indoor_or_sheltered"),
            ("dining", "吃饭", ("晚饭", "午饭", "吃饭", "想吃", "吃好", "美食", "小吃", "本地小吃", "餐厅", "聚餐", "本地特色"), 60, ["餐厅", "本地特色", "小吃", "晚饭"], "open_now", "indoor_or_sheltered"),
            ("photo_spot", "拍照打卡", ("拍照", "打卡", "出片"), 60, ["拍照", "打卡", "出片", "城市景观"], "open_now", "outdoor_or_indoor"),
            ("citywalk", "轻松散步", ("citywalk", "city work", "citywork", "城市漫步", "散步", "走走", "逛逛", "逛", "游玩", "轻松游", "轻松玩", "轻松逛"), 45, ["citywalk", "散步", "街区", "公园"], "open_now", "outdoor"),
            ("citywalk", "亲子轻松活动", ("带孩子", "亲子", "孩子", "小孩", "轻松玩", "下午玩"), 60, ["亲子", "室内", "公园", "轻松活动"], "open_now", "weather_adaptive"),
            ("cafe", "咖啡休息", ("咖啡", "下午茶"), 45, ["咖啡", "下午茶", "安静"], "open_now", "indoor"),
        ]
        specs.extend(
            [
                ("museum_exhibition", "逛展看馆", ("展览", "逛展", "展馆", "博物馆", "美术馆", "艺术馆"), 75, ["展览", "博物馆", "美术馆", "艺术馆"], "open_now", "indoor"),
                ("shopping_mall", "商场逛逛", ("商场", "购物中心", "室内逛", "逛商场"), 60, ["商场", "购物中心", "室内逛", "休闲购物"], "open_now", "indoor"),
                ("night_view", "看夜景", ("夜景", "看夜景", "夜游"), 45, ["夜景", "城市景观", "观景", "地标"], "evening_open", "outdoor_or_indoor"),
                ("hutong_walk", "胡同漫步", ("胡同", "逛胡同", "胡同漫步", "历史街区"), 60, ["胡同", "历史街区", "citywalk", "散步"], "open_now", "outdoor"),
                ("bookstore_reading", "书店坐坐", ("书店", "阅读", "坐坐", "看书"), 60, ["书店", "阅读", "安静", "适合坐坐"], "open_now", "indoor"),
                ("dessert", "吃甜品", ("甜品", "蛋糕", "下午茶"), 45, ["甜品", "蛋糕", "下午茶"], "open_now", "indoor"),
                ("billiards", "打台球", ("台球", "桌球", "打台球"), 75, ["台球", "桌球", "休闲娱乐"], "open_now", "indoor"),
                ("pet_walk", "遛狗散步", ("遛狗", "宠物", "狗狗"), 45, ["公园", "宠物友好", "散步"], "open_now", "outdoor"),
                ("craft_pottery", "做陶艺", ("陶艺", "手作", "DIY"), 90, ["陶艺", "手作", "DIY", "体验"], "open_now", "indoor"),
                ("board_game_script_kill", "剧本杀桌游", ("剧本杀", "桌游"), 120, ["剧本杀", "桌游", "休闲娱乐"], "open_now", "indoor"),
                ("comedy_show", "看脱口秀", ("脱口秀", "喜剧", "演出"), 90, ["脱口秀", "演出", "剧场", "喜剧"], "evening_open", "indoor"),
                ("fitness_light_food", "健身后轻食", ("健身", "轻食"), 60, ["健身", "轻食", "健康餐"], "open_now", "indoor"),
            ]
        )
        found = []
        for activity_type, label, terms, minutes, keywords, opening_need, weather_fit in specs:
            if cls._contains_any(text, terms):
                found.append((text.find(next((term for term in terms if term in text), terms[0])), activity_type, label, minutes, keywords, opening_need, weather_fit))
        found_types = {item[1] for item in found}
        optional_light_citywalk = (
            "dining" in found_types
            and "citywalk" in found_types
            and cls._contains_any(text, ("轻松游", "轻松玩", "游玩"))
            and not cls._contains_any(text, ("散步", "走走", "citywalk", "city work", "citywork", "城市漫步", "城市步行"))
        )
        if (
            cls._contains_any(text, ("室内", "室内玩", "室内活动", "最好室内"))
            and "shopping_mall" not in found_types
            and not {"museum_exhibition", "bookstore_reading", "cafe", "beauty", "drinks"} & found_types
        ):
            pos = text.find("室内")
            found.append((pos if pos >= 0 else 999, "shopping_mall", "室内休闲", 60, ["商场", "购物中心", "室内亲子", "室内休闲"], "open_now", "indoor"))
            found_types.add("shopping_mall")
        if (
            "museum_exhibition" in found_types
            and "shopping_mall" in found_types
            and "citywalk" not in found_types
            and cls._contains_any(text, ("逛逛", "逛一逛", "走走"))
        ):
            pos = text.find("逛")
            found.append((pos if pos >= 0 else 999, "citywalk", "轻松转场", 30, ["室内逛逛", "步行转场", "轻松活动"], "open_now", "indoor_or_sheltered"))
        if not found:
            if scenario == "easy_citywalk":
                found = [(0, "citywalk", "轻松散步", 60, ["citywalk", "散步", "街区"], "open_now", "outdoor")]
            else:
                found = [(0, "dining", "吃饭", 60, ["餐厅", "本地特色"], "open_now", "indoor_or_sheltered")]
        found.sort(key=lambda item: item[0] if item[0] >= 0 else 999)
        activities = []
        for order, (_, activity_type, label, minutes, keywords, opening_need, weather_fit) in enumerate(found[:4], start=1):
            required = not (activity_type == "citywalk" and optional_light_citywalk)
            activities.append(
                {
                    "slot_id": f"slot_{order}",
                    "type": activity_type,
                    "activity_type": activity_type,
                    "label": label,
                    "activity_label": label,
                    "activity_group": cls._activity_group(activity_type),
                    "order": order,
                    "required": required,
                    "duration_min": minutes,
                    "min_duration_min": max(20, int(minutes * 0.6)),
                    "max_duration_min": minutes,
                    "poi_category": cls._poi_category_for_activity(activity_type),
                    "poi_keywords": keywords,
                    "opening_hours_need": opening_need,
                    "weather_fit": weather_fit,
                    "hard_filters": {"must_be_open": True},
                    "soft_preferences": {
                        "low_queue": True,
                        "conversation_friendly": True,
                        "photo_friendly": activity_type in {"photo_spot", "beauty", "citywalk"},
                        "low_intensity": activity_type in {"citywalk", "cafe", "bookstore_reading", "wellness"},
                    },
                }
            )
        return activities

    @staticmethod
    def _activity_group(activity_type: str) -> str:
        mapping = {
            "dining": "food",
            "late_night_food": "food",
            "dessert": "food",
            "cafe": "food",
            "wellness": "relax_wellness",
            "beauty": "shopping_beauty",
            "drinks": "social_entertainment",
            "billiards": "social_entertainment",
            "board_game_script_kill": "social_entertainment",
            "comedy_show": "social_entertainment",
            "photo_spot": "photo_culture",
            "citywalk": "photo_culture",
            "museum_exhibition": "photo_culture",
            "shopping_mall": "shopping_beauty",
            "night_view": "photo_culture",
            "hutong_walk": "photo_culture",
            "bookstore_reading": "photo_culture",
            "craft_pottery": "custom",
            "pet_walk": "sports_outdoor",
            "fitness_light_food": "sports_outdoor",
        }
        return mapping.get(activity_type, "custom")

    @staticmethod
    def _poi_category_for_activity(activity_type: str) -> str:
        if activity_type in {"dining", "late_night_food", "dessert", "cafe", "drinks", "fitness_light_food"}:
            return "dining"
        if activity_type in {"wellness", "beauty", "craft_pottery"}:
            return "other"
        return "culture_entertainment"

    @staticmethod
    def _day_part(start_dt: datetime, end_dt: datetime) -> str:
        hour = start_dt.hour
        if hour < 11:
            return "morning"
        if hour < 14:
            return "midday"
        if hour < 18:
            return "afternoon"
        if end_dt.hour >= 22 or end_dt.day != start_dt.day:
            return "evening_to_late_night"
        return "evening"

    @staticmethod
    def _extract_preset_route_type(messages: List[Any]) -> str:
        for msg in messages or []:
            content = getattr(msg, "content", "")
            if not isinstance(content, str) or "preset_route_type" not in content:
                continue
            try:
                start_idx = content.find("{")
                end_idx = content.rfind("}")
                if start_idx != -1 and end_idx != -1:
                    data = json.loads(content[start_idx:end_idx + 1])
                    return str(data.get("preset_route_type", "auto"))
            except Exception:
                continue
        return "auto"

    @staticmethod
    def _canonical_route_type(route_type: Any) -> str:
        mapping = {
            "1": "sightseeing",
            "2": "food",
            "3": "balanced",
            "4": "auto",
            "打卡路线": "sightseeing",
            "观光路线": "sightseeing",
            "美食路线": "food",
            "兼顾路线": "balanced",
            "景点和餐饮兼顾": "balanced",
            "citywalk": "citywalk",
            "city work": "citywalk",
            "citywork": "citywalk",
            "城市漫步": "citywalk",
            "轻松路线": "citywalk",
            "胡同漫步": "citywalk",
            "跳过": "auto",
            "自动判断": "auto",
        }
        value = str(route_type or "auto").strip()
        return mapping.get(value, value if value in ROUTE_WEIGHT_TEMPLATES else "auto")

    @staticmethod
    def _as_text_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            result = []
            for item in value:
                text = str(item or "").strip()
                if text:
                    result.append(text)
            return result
        text = str(value or "").strip()
        return [text] if text else []

    @classmethod
    def _merge_route_semantics(cls, route_preference: Dict[str, Any], source: Any) -> None:
        if not isinstance(source, dict):
            return
        for key in ("semantic_tags", "recall_phrases"):
            merged = []
            for item in [
                *cls._as_text_list(route_preference.get(key)),
                *cls._as_text_list(source.get(key)),
            ]:
                if item not in merged:
                    merged.append(item)
            if merged:
                route_preference[key] = merged[:8]
        travel_style = str(source.get("travel_style") or "").strip()
        if travel_style and not route_preference.get("travel_style"):
            route_preference["travel_style"] = travel_style

    @classmethod
    def _derive_route_semantics(cls, user_query: str) -> Dict[str, Any]:
        text = str(user_query or "")
        citywalk_terms = ("citywalk", "city work", "citywork", "城市漫步", "城市步行", "胡同漫步", "街区漫步", "走走逛逛")
        easy_terms = ("轻松", "不累", "别太累", "低强度", "慢走", "散步", "悠闲")
        if not any(term.casefold() in text.casefold() for term in citywalk_terms) and not any(term in text for term in easy_terms):
            return {}
        phrases = ["citywalk 半日游", "胡同漫步", "历史街区", "公园轻松散步", "低强度步行路线"]
        if "天安门" in text:
            phrases = ["天安门周边 citywalk", "东交民巷 历史街区", "前门 大栅栏 citywalk", *phrases]
        return {
            "travel_style": "citywalk_easy",
            "semantic_tags": ["citywalk", "easy_walk"],
            "recall_phrases": phrases[:8],
        }

    @classmethod
    def _normalized_weights(cls, weights: Any, route_type: str) -> Dict[str, float]:
        route_type = cls._canonical_route_type(route_type)
        template = ROUTE_WEIGHT_TEMPLATES[route_type]
        if not isinstance(weights, dict):
            return dict(template)

        normalized = {}
        for key in ROUTE_WEIGHT_KEYS:
            try:
                normalized[key] = max(0.0, float(weights.get(key, template[key])))
            except (TypeError, ValueError):
                normalized[key] = template[key]

        total = sum(normalized.values())
        if total <= 0:
            return dict(template)
        return {key: round(value / total, 4) for key, value in normalized.items()}

    @classmethod
    def _apply_dynamic_weight_adjustments(cls, weights: Dict[str, float], user_query: str) -> Tuple[Dict[str, float], List[str]]:
        text = str(user_query or "")
        adjusted = dict(weights)
        reasons = []

        def add_weight(key: str, delta: float, reason: str) -> None:
            adjusted[key] = adjusted.get(key, 0.0) + delta
            reasons.append(reason)

        if any(term in text for term in ("只有3小时", "三小时", "3小时", "半天", "赶时间", "时间紧", "短途")):
            add_weight("travel_efficiency", 0.08, "用户表达时间较短或赶时间，提高路线效率权重")
        if cls._derive_route_semantics(text):
            add_weight("travel_efficiency", 0.08, "用户表达 citywalk、轻松或低强度漫步，提高路线效率权重")
            add_weight("experience", 0.08, "用户表达 citywalk、轻松或低强度漫步，提高街区体验权重")
        if any(term in text for term in ("不想排队", "少排队", "别等位", "不排队", "排队少", "人少")):
            add_weight("queue", 0.08, "用户表达少排队或少等位偏好，提高排队敏感权重")
        if any(term in text for term in ("预算有限", "便宜点", "省钱", "不想太贵", "性价比", "预算低")):
            add_weight("cost", 0.07, "用户表达预算敏感，提高成本权重")
        if any(term in text for term in ("多打卡", "打卡", "拍照", "景点多", "多逛", "观光")):
            add_weight("sightseeing", 0.08, "用户表达打卡、拍照或景点覆盖偏好，提高景点权重")
        if any(
            term in text
            for term in (
                "想吃好",
                "想吃",
                "吃点",
                "美食",
                "特色小吃",
                "本地菜",
                "本地特色",
                "北京特色",
                "老字号",
                "地道",
                "好吃",
                "烤鸭",
                "炸酱面",
                "卤煮",
                "豆汁",
                "涮肉",
            )
        ):
            add_weight("food", 0.08, "用户表达美食或本地餐饮偏好，提高餐饮权重")

        total = sum(max(0.0, adjusted.get(key, 0.0)) for key in ROUTE_WEIGHT_KEYS)
        if total <= 0:
            return cls._normalized_weights(adjusted, "auto"), reasons
        return {
            key: round(max(0.0, adjusted.get(key, 0.0)) / total, 4)
            for key in ROUTE_WEIGHT_KEYS
        }, reasons

    @staticmethod
    def _infer_auto_route_type(weights: Dict[str, float], user_query: str = "") -> str:
        text = str(user_query or "")
        if IntentionAgent._derive_route_semantics(text):
            return "citywalk"
        has_sightseeing_signal = any(term in text for term in ("多打卡", "打卡", "拍照", "景点多", "多逛", "观光"))
        has_food_signal = any(
            term in text
            for term in (
                "想吃好",
                "想吃",
                "吃点",
                "美食",
                "特色小吃",
                "本地菜",
                "本地特色",
                "北京特色",
                "老字号",
                "地道",
                "好吃",
                "烤鸭",
                "炸酱面",
                "卤煮",
                "豆汁",
                "涮肉",
            )
        )
        if has_sightseeing_signal and has_food_signal:
            return "balanced"
        if has_food_signal:
            return "food"
        if has_sightseeing_signal:
            return "sightseeing"

        try:
            sightseeing = float(weights.get("sightseeing", 0.0) or 0.0)
            food = float(weights.get("food", 0.0) or 0.0)
        except (TypeError, ValueError):
            return "auto"

        if food >= max(0.45, sightseeing + 0.12):
            return "food"
        if sightseeing >= max(0.45, food + 0.12):
            return "sightseeing"
        if food >= 0.30 and sightseeing >= 0.30:
            return "balanced"
        return "auto"

    @classmethod
    def _normalize_urban_intent_profile(cls, result: dict, user_query: str) -> dict:
        if not isinstance(result, dict):
            return result
        schedule = result.get("agent_schedule", [])
        if not cls._has_itinerary_intent(result, schedule if isinstance(schedule, list) else []):
            return result
        existing = result.get("urban_intent_profile") if isinstance(result.get("urban_intent_profile"), dict) else {}
        derived = cls._derive_urban_intent_profile(user_query)
        profile = cls._merge_urban_profile(derived, existing)
        result["urban_intent_profile"] = profile
        return result

    @classmethod
    def _derive_urban_intent_profile(cls, user_query: str) -> Dict[str, Any]:
        text = str(user_query or "")
        companions = cls._derive_companions(text)
        social_context = cls._social_context_for(companions[0]["type"] if companions else "unknown", text)
        activities = cls._derive_activity_sequence(text, social_context)
        scenario = cls._derive_scenario(text, activities, companions)
        time_context = cls._derive_time_context(text, activities)
        city = cls._derive_city(text)
        return {
            "intent_type": "urban_micro_trip",
            "original_query": text,
            "scenario": scenario,
            "time_context": time_context,
            "weather_context": {
                "source": "pending",
                "city": city,
                "query_time": time_context.get("current_datetime", ""),
                "target_window": f"{time_context.get('inferred_start_time', '')}/{time_context.get('inferred_end_time', '')}",
                "warnings": [],
            },
            "companions": companions,
            "social_context": social_context,
            "energy_level": "low" if any(term in text for term in ("下班", "放松", "轻松", "不累", "无事可做")) else "medium",
            "mood": cls._derive_moods(text),
            "activity_sequence": activities,
            "route_constraints": {
                "prefer_low_intensity": any(term in text for term in ("下班", "放松", "轻松", "散步", "不累", "无事可做")),
                "max_transfer_count": 1 if any(term in text for term in ("下班", "轻松", "不累", "约会")) else 2,
                "prefer_near_start": True,
                "avoid_closed_venues": True,
                "require_opening_hours_check": True,
                "weather_adaptive": True,
            },
        }

    @classmethod
    def _merge_urban_profile(cls, derived: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
        profile = dict(derived)
        for key, value in existing.items():
            if value not in (None, "", [], {}):
                profile[key] = value
        profile["time_context"] = {**derived.get("time_context", {}), **(existing.get("time_context") if isinstance(existing.get("time_context"), dict) else {})}
        derived_weather = derived.get("weather_context") if isinstance(derived.get("weather_context"), dict) else {}
        existing_weather = existing.get("weather_context") if isinstance(existing.get("weather_context"), dict) else {}
        profile["weather_context"] = {**derived_weather, **existing_weather}
        if (
            str(derived_weather.get("source") or "") == "user_explicit"
            and str(existing_weather.get("source") or "") in {"", "pending", "unavailable", "unknown"}
        ):
            profile["weather_context"] = {**existing_weather, **derived_weather}
        profile["route_constraints"] = {**derived.get("route_constraints", {}), **(existing.get("route_constraints") if isinstance(existing.get("route_constraints"), dict) else {})}
        if not isinstance(profile.get("companions"), list) or not profile["companions"]:
            profile["companions"] = derived["companions"]
        if not isinstance(profile.get("activity_sequence"), list) or not profile["activity_sequence"]:
            profile["activity_sequence"] = derived["activity_sequence"]
        else:
            existing_types = {
                str(item.get("activity_type") or item.get("type") or "")
                for item in profile.get("activity_sequence", [])
                if isinstance(item, Mapping)
            }
            derived_activities = [
                item
                for item in derived.get("activity_sequence", [])
                if isinstance(item, Mapping)
            ]
            for activity in derived_activities:
                activity_type = str(activity.get("activity_type") or activity.get("type") or "")
                if activity_type not in {"citywalk", "hutong_walk", "shopping_mall", "bookstore_reading"}:
                    continue
                if activity_type in existing_types:
                    continue
                profile["activity_sequence"].append(dict(activity, required=False))
                existing_types.add(activity_type)
                break
        profile["activity_sequence"] = cls._normalize_activity_sequence(profile.get("activity_sequence", []))
        profile["schema_version"] = str(profile.get("schema_version") or "1.0")
        profile["scenario"] = cls._scenario_payload(profile.get("scenario") or "urban_micro_trip")
        profile["confidence"] = float(profile.get("confidence") or 0.75)
        profile["transport_mode"] = cls._infer_transport_mode(
            str(profile.get("original_query") or ""),
            profile,
        )
        if "transport_mode" not in profile or not isinstance(profile.get("transport_mode"), dict):
            profile["transport_mode"] = cls._infer_transport_mode("", {})
        time_context = profile.get("time_context") if isinstance(profile.get("time_context"), dict) else {}
        if time_context:
            time_context.setdefault("current_time", time_context.get("current_datetime"))
            time_context.setdefault("requested_start_time", time_context.get("inferred_start_time"))
            time_context.setdefault("requested_end_time", time_context.get("inferred_end_time"))
            time_context.setdefault("is_time_sensitive", True)
            profile["time_context"] = time_context
        weather_context = profile.get("weather_context") if isinstance(profile.get("weather_context"), dict) else {}
        weather_context.setdefault("prefer_indoor", bool(weather_context.get("indoor_preferred")))
        weather_context.setdefault("prefer_outdoor", False)
        weather_context.setdefault("weather_adaptive_required", True)
        weather_context.setdefault("reason", "城市微行程默认需要天气参与召回和评分。")
        profile["weather_context"] = weather_context
        return profile

    @staticmethod
    def _scenario_payload(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            family = str(value.get("family") or "custom")
            label = str(value.get("label") or family)
            return {
                "family": family,
                "label": label,
                "is_custom": bool(value.get("is_custom", family == "custom")),
                "confidence": float(value.get("confidence") or 0.75),
            }
        label = str(value or "urban_micro_trip")
        known = {
            "after_work_relax_late_food": "relax_wellness",
            "girls_afternoon_evening": "shopping_beauty",
            "full_day_photo_food": "photo_citywalk",
            "citywalk_easy": "photo_citywalk",
            "partner_date": "date",
        }
        family = known.get(label, "custom")
        return {"family": family, "label": label, "is_custom": family == "custom", "confidence": 0.75}

    @staticmethod
    def _infer_transport_mode(text: str, existing: Mapping[str, Any]) -> Dict[str, Any]:
        existing_mode = existing.get("transport_mode") if isinstance(existing, Mapping) else None
        if isinstance(existing_mode, Mapping) and existing_mode.get("mode"):
            return dict(existing_mode)
        lowered = str(text or "").casefold()
        checks = [
            ("electrobike", ("电动车", "电驴", "小电驴", "ebike", "electrobike")),
            ("driving", ("开车", "驾车", "自驾", "打车", "网约车", "driving")),
            ("transit", ("公交", "地铁", "公共交通", "transit")),
            ("bicycling", ("骑车", "骑行", "自行车", "单车", "cycling", "bicycling")),
            ("walking", ("citywalk", "遛狗", "步行", "walking")),
        ]
        for mode, terms in checks:
            if any(term in lowered or term in text for term in terms):
                return {
                    "mode": mode,
                    "confidence": 0.9,
                    "reason": "用户明确表达了交通方式偏好。",
                    "allowed_modes": [mode],
                    "requires_user_confirmation": False,
                }
        return {
            "mode": "multimodal_low_friction",
            "confidence": 0.7,
            "reason": "用户没有指定交通方式，默认使用步行、骑行、公共交通三种低门槛方式组合。",
            "allowed_modes": ["walking", "bicycling", "transit"],
            "requires_user_confirmation": False,
        }

    @staticmethod
    def _derive_companions(text: str) -> List[Dict[str, Any]]:
        mapping = [
            ("partner", "伴侣", ("伴侣", "对象", "男友", "女友", "情侣", "约会", "男朋友", "女朋友")),
            ("besties", "闺蜜", ("闺蜜", "姐妹")),
            ("classmates", "同学", ("同学", "室友")),
            ("colleagues", "同事", ("同事", "客户", "领导")),
            ("kids", "亲子", ("孩子", "小孩", "亲子", "儿童")),
            ("family", "家人", ("家人", "爸妈", "父母", "老人")),
            ("friends", "朋友", ("朋友", "好友")),
        ]
        for companion_type, label, terms in mapping:
            if any(term in text for term in terms):
                return [{"type": companion_type, "label": label, "group_size": 2}]
        if any(term in text for term in ("一个人", "自己", "独自")):
            return [{"type": "solo", "label": "独自", "group_size": 1}]
        return [{"type": "unknown", "label": "未说明", "group_size": None}]

    @staticmethod
    def _social_context_for(companion_type: str, text: str) -> Dict[str, Any]:
        defaults = {
            "unknown": ("unknown", [], "medium", True, False, "medium"),
            "solo": ("solo", ["quiet", "efficient"], "medium", False, False, "medium"),
            "partner": ("romantic", ["quiet", "romantic", "night_view"], "medium", True, True, "medium_high"),
            "friends": ("social", ["lively", "conversation"], "medium", True, True, "medium"),
            "besties": ("social", ["photo_friendly", "stylish", "drinks"], "medium", True, True, "medium"),
            "classmates": ("peer_social", ["budget_friendly", "lively"], "high", True, True, "low"),
            "colleagues": ("work_social", ["safe_choice", "business_friendly"], "medium", True, False, "low"),
            "family": ("family", ["comfortable", "safe"], "medium", True, False, "medium"),
            "kids": ("family_kids", ["child_friendly", "safe", "comfortable"], "medium", True, False, "low"),
        }
        relationship, atmosphere, budget, conversation, photo, privacy = defaults.get(companion_type, defaults["unknown"])
        if any(term in text for term in ("预算有限", "平价", "便宜", "学生")):
            budget = "high"
        return {
            "relationship_context": relationship,
            "atmosphere_preference": list(atmosphere),
            "budget_sensitivity": budget,
            "conversation_friendly": conversation,
            "photo_friendly": photo or any(term in text for term in ("拍照", "打卡")),
            "privacy_need": privacy,
        }

    @classmethod
    def _derive_activity_sequence(cls, text: str, social_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        activities: List[Dict[str, Any]] = []
        def add(activity_type: str, label: str, duration: int, keywords: List[str], opening: str, weather_fit: str) -> None:
            if not any(item["type"] == activity_type for item in activities):
                activities.append(
                    {
                        "slot_id": f"slot_{len(activities) + 1}",
                        "type": activity_type,
                        "activity_type": activity_type,
                        "label": label,
                        "activity_label": label,
                        "activity_group": IntentionAgent._activity_group(activity_type),
                        "order": len(activities) + 1,
                        "required": True,
                        "duration_min": duration,
                        "min_duration_min": max(20, int(duration * 0.6)),
                        "max_duration_min": duration,
                        "poi_category": IntentionAgent._poi_category_for_activity(activity_type),
                        "poi_keywords": keywords,
                        "opening_hours_need": opening,
                        "weather_fit": weather_fit,
                        "hard_filters": {"must_be_open": True},
                        "soft_preferences": {
                            "low_queue": True,
                            "conversation_friendly": True,
                            "photo_friendly": activity_type in {"photo_spot", "beauty", "citywalk"},
                            "low_intensity": activity_type in {"citywalk", "cafe", "bookstore_reading", "wellness"},
                        },
                    }
                )

        if any(term in text for term in ("按摩", "足疗", "SPA", "spa", "放松")):
            add("wellness", "按摩放松", 90, ["按摩", "足疗", "SPA", "放松"], "evening_open", "indoor")
        if any(term in text for term in ("做指甲", "美甲", "美容")):
            add("beauty", "美甲护理", 90, ["美甲", "做指甲", "美容", "日式美甲"], "afternoon_evening_open", "indoor")
        if any(term in text for term in ("拍照", "打卡")):
            add("photo_spot", "拍照打卡", 60, ["拍照", "打卡", "出片", "地标"], "daytime_open", "weather_adaptive")
        if any(term in text for term in ("晚饭", "吃饭", "吃个饭", "聚餐", "午饭", "早饭", "早餐")):
            add("dining", "吃饭", 70, ["餐厅", "聚餐", "晚饭"], "meal_time_open", "indoor_or_sheltered")
        if any(term in text for term in ("夜宵", "宵夜", "深夜食堂")):
            add("late_night_food", "夜宵", 60, ["夜宵", "烧烤", "小龙虾", "深夜食堂"], "late_night_open", "indoor_or_sheltered")
        if any(term in text for term in ("小酒", "喝一杯", "酒吧", "bistro", "微醺")):
            add("drinks", "小酒", 75, ["小酒馆", "bistro", "酒吧", "微醺"], "evening_open", "indoor")
        if any(term in text for term in ("咖啡", "下午茶", "奶茶", "甜品")):
            add("cafe", "咖啡甜品", 45, ["咖啡", "甜品", "下午茶", "奶茶"], "daytime_evening_open", "indoor")
        if any(term in text for term in ("散步", "citywalk", "citywork", "城市漫步", "逛逛", "走走")):
            add("citywalk", "轻松散步", 45, ["citywalk", "散步", "街区", "公园", "步行街"], "any_open", "outdoor")
        if any(term in text for term in ("书店", "阅读", "看书", "坐坐")):
            add("bookstore_reading", "书店坐坐", 60, ["书店", "阅读", "安静", "适合坐坐"], "any_open", "indoor")
        if any(term in text for term in ("甜品", "蛋糕", "下午茶")):
            add("dessert", "吃甜品", 45, ["甜品", "蛋糕", "下午茶"], "daytime_evening_open", "indoor")
        if any(term in text for term in ("台球", "桌球", "打台球")):
            add("billiards", "打台球", 75, ["台球", "桌球", "休闲娱乐"], "any_open", "indoor")
        if any(term in text for term in ("遛狗", "宠物", "狗狗")):
            add("pet_walk", "遛狗散步", 45, ["公园", "宠物友好", "散步"], "any_open", "outdoor")
        if any(term in text for term in ("陶艺", "手作", "DIY")):
            add("craft_pottery", "做陶艺", 90, ["陶艺", "手作", "DIY", "体验"], "any_open", "indoor")
        if any(term in text for term in ("剧本杀", "桌游")):
            add("board_game_script_kill", "剧本杀桌游", 120, ["剧本杀", "桌游", "休闲娱乐"], "any_open", "indoor")
        if any(term in text for term in ("脱口秀", "喜剧", "演出")):
            add("comedy_show", "看脱口秀", 90, ["脱口秀", "演出", "剧场", "喜剧"], "evening_open", "indoor")
        if any(term in text for term in ("健身", "轻食")):
            add("fitness_light_food", "健身后轻食", 60, ["健身", "轻食", "健康餐"], "any_open", "indoor")
        if not activities:
            add("citywalk", "轻松短途", 55, ["citywalk", "景点", "街区"], "any_open", "weather_adaptive")
        if social_context.get("relationship_context") == "romantic" and not any(item["type"] == "drinks" for item in activities):
            add("drinks", "约会小坐", 60, ["小酒馆", "安静", "夜景", "约会"], "evening_open", "indoor")
        return cls._normalize_activity_sequence(activities)

    @staticmethod
    def _normalize_activity_sequence(activities: Any) -> List[Dict[str, Any]]:
        normalized = []
        if not isinstance(activities, list):
            return normalized
        for index, item in enumerate(activities, 1):
            if not isinstance(item, dict):
                continue
            activity = dict(item)
            activity["order"] = int(activity.get("order") or index)
            activity["type"] = str(activity.get("activity_type") or activity.get("type") or "activity")
            activity["activity_type"] = activity["type"]
            activity["label"] = str(activity.get("activity_label") or activity.get("label") or activity["type"])
            activity["activity_label"] = activity["label"]
            activity.setdefault("slot_id", f"slot_{activity['order']}")
            activity.setdefault("required", True)
            activity.setdefault("activity_group", IntentionAgent._activity_group(activity["type"]))
            activity.setdefault("poi_category", IntentionAgent._poi_category_for_activity(activity["type"]))
            try:
                activity["duration_min"] = max(20, int(float(activity.get("duration_min") or 60)))
            except (TypeError, ValueError):
                activity["duration_min"] = 60
            activity.setdefault("min_duration_min", max(20, int(activity["duration_min"] * 0.6)))
            activity.setdefault("max_duration_min", activity["duration_min"])
            activity["poi_keywords"] = [str(value).strip() for value in IntentionAgent._as_text_list(activity.get("poi_keywords")) if str(value).strip()]
            if not activity["poi_keywords"]:
                activity["poi_keywords"] = [activity["label"]]
            activity.setdefault("opening_hours_need", "any_open")
            activity.setdefault("weather_fit", "weather_adaptive")
            activity.setdefault("hard_filters", {"must_be_open": True})
            activity.setdefault("soft_preferences", {"low_queue": True, "conversation_friendly": True})
            normalized.append(activity)
        return sorted(normalized, key=lambda item: item.get("order", 999))

    @staticmethod
    def _derive_scenario(text: str, activities: List[Dict[str, Any]], companions: List[Dict[str, Any]]) -> str:
        activity_types = {item.get("type") for item in activities}
        companion_type = companions[0].get("type") if companions else "unknown"
        if "wellness" in activity_types and "late_night_food" in activity_types:
            return "after_work_relax_late_food"
        if "beauty" in activity_types and "drinks" in activity_types:
            return "girls_afternoon_evening"
        if "photo_spot" in activity_types and "dining" in activity_types:
            return "full_day_photo_food"
        if companion_type == "partner":
            return "partner_date"
        if companion_type == "classmates":
            return "classmate_budget_social"
        if "dining" in activity_types and "citywalk" in activity_types:
            return "after_work_social_evening"
        if "citywalk" in activity_types:
            return "citywalk_easy"
        return "urban_micro_trip"

    @staticmethod
    def _derive_time_context(text: str, activities: List[Dict[str, Any]]) -> Dict[str, Any]:
        tz = timezone(timedelta(hours=8))
        now = datetime.now(tz)
        duration_min = IntentionAgent._parse_duration_min(text) or sum(int(item.get("duration_min", 60)) for item in activities) + 45
        start = now + timedelta(minutes=15)
        relative = ""
        day_part = "daytime"
        range_match = re.search(r"([0-2]?\d)[:：]?([0-5]\d)?\s*[-~到至]\s*([0-2]?\d)[:：]?([0-5]\d)?", text)
        if range_match:
            sh = int(range_match.group(1))
            sm = int(range_match.group(2) or 0)
            eh = int(range_match.group(3))
            em = int(range_match.group(4) or 0)
            start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
            if end <= start:
                end += timedelta(days=1)
            duration_min = int((end - start).total_seconds() // 60)
            relative = "explicit_time_window"
        elif "下班" in text:
            relative = "下班"
            candidate = now.replace(hour=18, minute=30, second=0, microsecond=0)
            start = candidate if now <= candidate else now + timedelta(minutes=15)
            end = start + timedelta(minutes=duration_min)
        elif "下午" in text:
            relative = "今天下午"
            candidate = now.replace(hour=14, minute=0, second=0, microsecond=0)
            start = candidate if now <= candidate else now + timedelta(minutes=15)
            end = start + timedelta(minutes=duration_min)
        elif "刚起床" in text or "早上" in text:
            relative = "刚起床"
            candidate = now.replace(hour=8, minute=0, second=0, microsecond=0)
            start = candidate if now <= candidate else now + timedelta(minutes=15)
            end = start + timedelta(minutes=duration_min)
        else:
            end = start + timedelta(minutes=duration_min)
        if start.hour >= 18 or end.hour >= 22:
            day_part = "evening_to_late_night" if end.hour >= 22 or end.day != start.day else "evening"
        elif start.hour < 11:
            day_part = "morning"
        elif start.hour < 17:
            day_part = "afternoon"
        if duration_min >= 480:
            day_part = "full_day"
        return {
            "current_datetime": now.isoformat(),
            "timezone": "Asia/Shanghai",
            "relative_time_phrase": relative,
            "inferred_start_time": start.isoformat(),
            "inferred_end_time": end.isoformat(),
            "duration_min": duration_min,
            "day_part": day_part,
            "is_today_plan": True,
        }

    @staticmethod
    def _parse_duration_min(text: str) -> Optional[int]:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:小时|h|hour)", text, re.I)
        if match:
            return int(round(float(match.group(1)) * 60))
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:分钟|min)", text, re.I)
        if match:
            return int(round(float(match.group(1))))
        return None

    @staticmethod
    def _derive_city(text: str) -> str:
        for city in ("北京", "上海", "杭州", "深圳", "广州", "成都", "南京", "苏州", "西安", "重庆", "武汉", "长沙", "天津"):
            if city in text:
                return city
        return "北京"

    @staticmethod
    def _derive_moods(text: str) -> List[str]:
        moods = []
        if any(term in text for term in ("放松", "按摩", "轻松", "散步")):
            moods.append("relaxed")
        if any(term in text for term in ("朋友", "闺蜜", "同学", "同事", "伴侣", "约会")):
            moods.append("social")
        if any(term in text for term in ("拍照", "打卡", "美甲")):
            moods.append("photo_friendly")
        return moods or ["relaxed"]

    @classmethod
    def _normalize_urban_intent_profile(cls, result: dict, user_query: str) -> dict:
        if not isinstance(result, dict):
            return result
        schedule = result.get("agent_schedule", [])
        if not cls._has_itinerary_intent(result, schedule if isinstance(schedule, list) else []):
            return result
        existing = result.get("urban_intent_profile") if isinstance(result.get("urban_intent_profile"), dict) else {}
        result["urban_intent_profile"] = cls._derive_urban_intent_profile(user_query, existing)
        return result

    @classmethod
    def _derive_urban_intent_profile(cls, user_query: str, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        existing = existing or {}
        text = str(user_query or "")
        now = datetime.now(timezone(timedelta(hours=8)))
        city = cls._infer_city(text, existing)
        duration_min = cls._infer_micro_trip_duration(text, existing)
        start_dt, end_dt = cls._infer_micro_trip_window(text, now, duration_min)
        companions = cls._infer_companions(text, existing)
        scenario = cls._infer_micro_trip_scenario(text, companions)
        activities = cls._infer_activity_sequence(text, scenario, duration_min)
        profile = {
            "intent_type": "urban_micro_trip",
            "original_query": text,
            "scenario": existing.get("scenario") or scenario,
            "time_context": {
                "current_datetime": now.isoformat(),
                "timezone": "Asia/Shanghai",
                "inferred_start_time": start_dt.isoformat(),
                "inferred_end_time": end_dt.isoformat(),
                "duration_min": int(round((end_dt - start_dt).total_seconds() / 60)) or duration_min,
                "day_part": cls._day_part(start_dt, end_dt),
                "is_today_plan": start_dt.date() == now.date(),
            },
            "weather_context": {
                "source": "pending",
                "city": city,
                "query_time": now.isoformat(),
                "target_window": f"{start_dt.isoformat()}/{end_dt.isoformat()}",
                "condition": "unknown",
                "temperature_c": None,
                "precipitation_risk": "unknown",
                "wind_risk": "unknown",
                "comfort_level": "unknown",
                "outdoor_suitability": "unknown",
                "indoor_preferred": False,
                "warnings": [],
            },
            "companions": companions,
            "social_context": cls._infer_social_context(companions),
            "energy_level": "low" if cls._contains_any(text, ("下班", "放松", "轻松", "不累", "无事可做")) else "medium",
            "mood": cls._derive_moods(text),
            "activity_sequence": activities,
            "route_constraints": {
                "prefer_low_intensity": cls._contains_any(text, ("下班", "放松", "轻松", "散步", "不累", "无事可做", "少走路", "别太远", "不太远")),
                "max_transfer_count": 1,
                "prefer_near_start": True,
                "avoid_closed_venues": True,
                "require_opening_hours_check": True,
                "weather_adaptive": True,
            },
        }
        cls._apply_explicit_weather_signals(text, profile["weather_context"], profile["route_constraints"])
        return cls._merge_urban_profile(profile, existing)

    @staticmethod
    def _apply_explicit_weather_signals(text: str, weather_context: Dict[str, Any], route_constraints: Dict[str, Any]) -> None:
        if not isinstance(weather_context, dict):
            return
        warnings = weather_context.setdefault("warnings", [])
        if not isinstance(warnings, list):
            warnings = []
            weather_context["warnings"] = warnings

        rainy = any(term in text for term in ("下雨", "雨天", "有雨", "阵雨", "小雨", "中雨", "大雨", "暴雨"))
        hot = any(term in text for term in ("太热", "很热", "高温", "酷暑", "暴晒"))
        cold = any(term in text for term in ("太冷", "很冷", "寒冷"))
        indoor = any(term in text for term in ("室内", "有遮蔽", "别淋雨", "不晒", "避暑"))

        if rainy:
            weather_context.update(
                {
                    "source": "user_explicit",
                    "condition": "rain",
                    "precipitation_risk": "high",
                    "comfort_level": "poor_for_outdoor",
                    "outdoor_suitability": "low",
                    "indoor_preferred": True,
                    "prefer_indoor": True,
                }
            )
            if "rain_expected" not in warnings:
                warnings.append("rain_expected")
        elif hot:
            weather_context.update(
                {
                    "source": "user_explicit",
                    "condition": "hot",
                    "comfort_level": "poor_for_outdoor",
                    "outdoor_suitability": "low",
                    "indoor_preferred": True,
                    "prefer_indoor": True,
                }
            )
            if "high_temperature" not in warnings:
                warnings.append("high_temperature")
        elif cold:
            weather_context.update(
                {
                    "source": "user_explicit",
                    "condition": "cold",
                    "comfort_level": "poor_for_outdoor",
                    "outdoor_suitability": "low",
                    "indoor_preferred": True,
                    "prefer_indoor": True,
                }
            )
            if "low_temperature" not in warnings:
                warnings.append("low_temperature")

        if indoor:
            weather_context["indoor_preferred"] = True
            weather_context["prefer_indoor"] = True
            if str(weather_context.get("source") or "") in {"", "pending", "unavailable"}:
                weather_context["source"] = "user_explicit"
            route_constraints["prefer_indoor"] = True

    def _upgrade_fallback_if_needed(self, result: dict, user_query: str) -> dict:
        if not isinstance(result, dict):
            return result

        if not self._looks_like_itinerary_query(user_query):
            return result

        intents = result.get("intents", [])
        is_default_information = False
        if isinstance(intents, list) and len(intents) == 1 and isinstance(intents[0], dict):
            is_default_information = intents[0].get("type") == "information_query"

        schedule = result.get("agent_schedule", [])
        only_information_query = (
            isinstance(schedule, list)
            and len(schedule) == 1
            and isinstance(schedule[0], dict)
            and self._canonical_agent_name(schedule[0].get("agent_name", "")) == "information_query"
        )

        if not (is_default_information and only_information_query):
            return result

        return {
            "reasoning": "LLM意图识别失败，但本地规则识别为路线规划请求，使用确定性路线规划兜底。",
            "intents": [
                {
                    "type": "itinerary_planning",
                    "confidence": 0.7,
                    "description": "用户在描述出行或游玩路线需求",
                    "reason": "包含游玩、路线、时长、餐饮或少排队等路线规划信号",
                }
            ],
            "key_entities": self._fallback_entities(user_query),
            "rewritten_query": user_query,
            "agent_schedule": [
                {"agent_name": "event_collection", "priority": 1, "reason": "提取行程基础信息", "expected_output": "城市、时间、偏好和约束"},
                {"agent_name": "poi_search", "priority": 2, "reason": "调用高德获取真实POI候选", "expected_output": "餐饮与文化/娱乐POI候选"},
                {"agent_name": "route_planning", "priority": 3, "reason": "生成多方案路线", "expected_output": "结构化route_options"},
                {"agent_name": "itinerary_planning", "priority": 4, "reason": "生成可读行程", "expected_output": "完整行程和多方案对比"},
            ],
        }

    @staticmethod
    def _looks_like_itinerary_query(user_query: str) -> bool:
        text = str(user_query or "")
        terms = (
            "一日游", "游玩", "旅游", "旅行", "出行", "路线", "行程", "安排",
            "想吃", "吃好", "美食", "景点", "不想排队", "少排队", "小时",
        )
        urban_terms = (
            "路线", "行程", "安排", "短途游", "游玩", "规划", "小时", "分钟",
            "下班", "按摩", "足疗", "夜宵", "宵夜", "美甲", "做指甲", "小酒",
            "citywalk", "city work", "citywork", "散步", "约会", "闺蜜", "同学",
        )
        robust_terms = (
            "\u4e00\u65e5\u6e38", "\u6e38\u73a9", "\u65c5\u6e38", "\u65c5\u884c", "\u51fa\u884c",
            "\u8def\u7ebf", "\u884c\u7a0b", "\u5b89\u6392", "\u89c4\u5212", "\u77ed\u9014\u6e38",
            "\u5c0f\u65f6", "\u5206\u949f", "\u4e0b\u73ed", "\u665a\u996d", "\u6563\u6b65",
            "\u60f3\u5403", "\u7f8e\u98df", "\u666f\u70b9", "\u6253\u5361", "\u62cd\u7167",
            "\u4e0d\u60f3\u6392\u961f", "\u5c11\u6392\u961f", "\u6309\u6469", "\u8db3\u7597",
            "\u591c\u5bb5", "\u5bb5\u591c", "\u7f8e\u7532", "\u5c0f\u9152", "\u7ea6\u4f1a",
            "\u95fa\u871c", "\u540c\u5b66", "\u4f34\u4fa3", "\u60c5\u4fa3",
        )
        return any(term in text for term in (*terms, *urban_terms, *robust_terms))

    @staticmethod
    def _should_use_local_route_intent() -> bool:
        value = os.getenv("LIGHTROUTE_LOCAL_ROUTE_INTENT", "0").strip().lower()
        return value in {"1", "true", "yes", "on"}

    @staticmethod
    def _fallback_entities(user_query: str) -> dict:
        text = str(user_query or "")
        entities = {}
        for city in ("杭州", "北京", "上海", "深圳", "广州", "成都", "南京", "苏州"):
            if city in text:
                entities["destination"] = city
                break
        return entities

    def _normalize_agent_schedule(self, result: dict) -> dict:
        """
        Stabilize route-planning schedules after LLM intent recognition.

        The LLM still decides whether the user is asking for itinerary planning,
        but once that intent exists we deterministically ensure the new route
        pipeline is present:
        event_collection -> poi_search -> route_planning -> itinerary_planning.
        """
        if not isinstance(result, dict):
            return result

        schedule = result.get("agent_schedule", [])
        if not isinstance(schedule, list):
            return result

        is_itinerary_request = self._has_itinerary_intent(result, schedule)
        if not is_itinerary_request:
            return result

        by_name = {}
        for task in schedule:
            if not isinstance(task, dict):
                continue
            agent_name = self._canonical_agent_name(task.get("agent_name", ""))
            if not agent_name:
                continue
            normalized_task = dict(task)
            normalized_task["agent_name"] = agent_name
            by_name[agent_name] = normalized_task

        required = {
            "event_collection": {
                "priority": 1,
                "reason": "提取目的地、时间、出发地、预算和约束",
                "expected_output": "结构化行程基础信息",
            },
            "poi_search": {
                "priority": 2,
                "reason": "基于目的地调用高德获取餐饮和文化/娱乐POI候选",
                "expected_output": "带UGC信号的POI候选列表",
            },
            "route_planning": {
                "priority": 3,
                "reason": "根据时间、距离、排队、预算和偏好生成多方案路线",
                "expected_output": "结构化route_options",
            },
            "itinerary_planning": {
                "priority": 4,
                "reason": "把结构化路线方案表达成用户可读行程",
                "expected_output": "完整行程安排和多方案对比",
            },
        }

        for agent_name, defaults in required.items():
            task = by_name.get(agent_name, {})
            merged = {
                "agent_name": agent_name,
                "priority": defaults["priority"],
                "reason": task.get("reason") or defaults["reason"],
                "expected_output": task.get("expected_output") or defaults["expected_output"],
            }
            by_name[agent_name] = merged

        priority_defaults = {
            "memory_query": 1,
            "preference": 1,
            "information_query": 1,
            "rag_knowledge": 1,
        }
        for agent_name, priority in priority_defaults.items():
            if agent_name in by_name:
                by_name[agent_name]["priority"] = int(by_name[agent_name].get("priority", priority))

        normalized_schedule = sorted(by_name.values(), key=lambda item: (item.get("priority", 999), item.get("agent_name", "")))
        result["agent_schedule"] = normalized_schedule
        return result

    @staticmethod
    def _has_itinerary_intent(result: dict, schedule: list) -> bool:
        intents = result.get("intents", [])
        if isinstance(intents, list):
            for intent in intents:
                if isinstance(intent, dict) and intent.get("type") in {"itinerary_planning", "plan-trip"}:
                    return True

        for task in schedule:
            if not isinstance(task, dict):
                continue
            if IntentionAgent._canonical_agent_name(task.get("agent_name", "")) == "itinerary_planning":
                return True
        return False

    @staticmethod
    def _canonical_agent_name(agent_name: str) -> str:
        mapping = {
            "plan-trip": "itinerary_planning",
            "itinerary": "itinerary_planning",
            "poi-search": "poi_search",
            "route-planning": "route_planning",
            "memory-query": "memory_query",
            "query-info": "information_query",
            "ask-question": "rag_knowledge",
            "event-collection": "event_collection",
        }
        name = str(agent_name or "").strip()
        return mapping.get(name, name)
