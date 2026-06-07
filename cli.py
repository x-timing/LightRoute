#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
轻途 - CLI 交互界面
使用 Rich 库实现清晰的终端交互
"""
import asyncio
import sys
import os
import threading
import time
import re
from typing import Any, Dict, Mapping, Optional
from datetime import datetime
from contextlib import nullcontext

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
import json

# 导入系统组件
from agentscope.model import OpenAIChatModel
from config_agentscope import init_agentscope
from config import LLM_CONFIG, SYSTEM_CONFIG, RESILIENCE_CONFIG
from context.memory_manager import MemoryManager
from utils.circuit_breaker import CircuitBreaker, CircuitOpenError
from utils.llm_resilience import retry_with_backoff, run_health_check as check_llm_health
from agents.intention_agent import IntentionAgent
from agents.orchestration_agent import OrchestrationAgent
from tools.registry import ToolRegistry
# 移除其他智能体的导入，改用懒加载

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout
except Exception:  # pragma: no cover - optional interactive dependency fallback
    PromptSession = None
    patch_stdout = None


class AligoCLI:
    """轻途 CLI"""

    def __init__(self):
        """初始化 CLI"""
        # prompt_toolkit redraws input lines while background tasks print progress.
        # Plain text avoids raw ANSI color codes on Windows terminals that do not
        # interpret Rich styles correctly under patched stdout.
        self.console = Console(no_color=True, force_terminal=False, color_system=None)
        self.user_id = None
        self.session_id = None
        self.memory_manager = None
        self.orchestrator = None
        self.intention_agent = None
        self.model = None
        self._agent_cache = {}  # 智能体缓存
        self.circuit_breaker = None  # 在 initialize_system 中从 RESILIENCE_CONFIG 初始化
        self.current_task = None
        self.current_query = None
        self.current_request_id = None
        self.current_started_at = None
        self.current_stage = None
        self.current_progress_task = None
        self.current_last_progress_at = None
        self.current_last_heartbeat_stage = None
        self.current_same_stage_heartbeat_count = 0
        self.progress_heartbeat_interval_sec = 10.0
        self._request_counter = 0
        self._prompt_session = None
        self._last_clean_route_render_signature = None

    def print_banner(self):
        self.console.print("\n[bold cyan]轻途[/bold cyan]")
        self.console.print("把一句想法整理成一条能出发的城市小路线。\n", style="dim")

    def print_help(self):
        """打印帮助信息"""
        table = Table(title="命令列表", show_header=True, header_style="bold magenta")
        table.add_column("命令", style="cyan", width=20)
        table.add_column("说明", style="white")

        table.add_row("help", "显示此帮助信息")
        table.add_row("status", "查看当前状态和记忆")
        table.add_row("health", "检查规划服务是否可用")
        table.add_row("clear", "清空当前任务（保留长期记忆）")
        table.add_row("history", "查看历史行程")
        table.add_row("preferences", "查看用户偏好")
        table.add_row("exit", "退出程序")
        table.add_row("", "")
        table.add_row("[自然语言]", "直接输入您的需求，如：")
        table.add_row("", "  - 北京短途游，从国贸出发，6小时，想吃本地特色，不想排队")
        table.add_row("", "  - 我在天安门，想进行3小时轻松散步，请推荐一条路线")
        table.add_row("", "  - 下雨了，想和女朋友在北京约会，看看展览，再找个安静小酒馆，4小时")

        self.console.print(table)

    async def initialize_system(self):
        """初始化系统 - 使用懒加载优化启动速度"""
        # 获取用户信息
        self.user_id = Prompt.ask(
            "用户ID",
            default="default_user"
        )

        # 生成会话ID
        import uuid
        self.session_id = str(uuid.uuid4())[:8]

        with self.console.status("初始化中...", spinner="dots"):
            # 初始化AgentScope
            init_agentscope()

            # 初始化模型
            timeout_sec = SYSTEM_CONFIG.get("timeout", 60)
            self.model = OpenAIChatModel(
                model_name=LLM_CONFIG["model_name"],
                api_key=LLM_CONFIG["api_key"],
                client_kwargs={
                    "base_url": LLM_CONFIG["base_url"],
                    "timeout": float(timeout_sec),
                },
                # temperature=LLM_CONFIG.get("temperature", 0.7),
                # max_tokens=LLM_CONFIG.get("max_tokens", 2000),
            )

            # 初始化记忆管理器（传入LLM模型用于总结）
            self.memory_manager = MemoryManager(
                user_id=self.user_id,
                session_id=self.session_id,
                llm_model=self.model
            )

            # 初始化意图识别智能体（必须预加载）
            self.intention_agent = IntentionAgent(
                name="IntentionAgent",
                model=self.model
            )

            # 使用懒加载注册器（智能体在首次使用时才加载）
            from agents.lazy_agent_registry import LazyAgentRegistry
            self._agent_cache = {}
            lazy_registry = LazyAgentRegistry(
                model=self.model, 
                cache=self._agent_cache,
                memory_manager=self.memory_manager
            )
            lazy_registry.console = self.console

            # 预先加载关键智能体（可选，利用 preload）
            # lazy_registry.preload("memory_query", "preference")

            # 初始化协调器
            self.orchestrator = OrchestrationAgent(
                name="OrchestrationAgent",
                agent_registry=lazy_registry,
                memory_manager=self.memory_manager,
                tool_registry=ToolRegistry(
                    tool_kwargs={
                        "route_planning": {
                            "auto_use_amap_route_matrix": True,
                            "strict_no_fallback": True,
                        },
                        "poi_search": {
                            "strict_no_fallback": True,
                        },
                    },
                ),
            )

            # 熔断器（连接与可用性）
            rc = RESILIENCE_CONFIG
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=rc.get("circuit_failure_threshold", 5),
                recovery_timeout_sec=rc.get("circuit_recovery_timeout_sec", 60.0),
                half_open_successes=rc.get("circuit_half_open_successes", 2),
            )

        self.console.print(f"已就绪 (用户: {self.user_id}) - 输入 help 查看帮助\n", style="green")

    async def process_query(
        self,
        user_input: str,
        request_id: Optional[int] = None,
        preset_route_type: Optional[str] = None,
        start_location: Optional[dict] = None,
        ask_route_preference: bool = True,
    ):
        """
        处理用户查询（原逻辑保留；仅在入口加熔断检查、对 LLM 调用加重试）
        """
        import time
        start_time = time.time()
        if not self._is_current_request(request_id):
            return

        # ---------- 仅新增：熔断检查 ----------
        if self.circuit_breaker:
            try:
                self.circuit_breaker.raise_if_open()
            except CircuitOpenError:
                self.console.print(
                    "\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]\n",
                    style="dim"
                )
                return

        rc = RESILIENCE_CONFIG
        max_retries = rc.get("max_retries", 3)
        recent_context = []
        memory_preference_context = {}
        planning_turn_decision = {}
        if self.memory_manager:
            try:
                recent_context = self.memory_manager.short_term.get_recent_context(n_turns=5)
            except Exception:
                recent_context = []
        memory_preference_context = self._build_memory_preference_context(user_input, recent_context)
        if self._looks_like_preference_query(user_input):
            self.show_preferences()
            self.memory_manager.add_message("user", user_input)
            self.memory_manager.add_message("assistant", "Displayed saved preferences.")
            return
        if self.intention_agent and hasattr(self.intention_agent, "classify_planning_turn"):
            try:
                planning_turn_decision = await self.intention_agent.classify_planning_turn(
                    user_query=user_input,
                    memory_preference_context=memory_preference_context,
                )
            except Exception:
                planning_turn_decision = {}
        if not isinstance(planning_turn_decision, dict):
            planning_turn_decision = {}

        if planning_turn_decision.get("requires_confirmation"):
            question = str(planning_turn_decision.get("confirmation_question") or "").strip()
            self.console.print(question or "你想沿用上一条路线调整，还是重新规划一条新路线？", style="yellow")
            return

        if ask_route_preference:
            if planning_turn_decision.get("should_ask_route_preference", True):
                preset_route_type = self._ask_route_preference_if_needed(user_input)
            if planning_turn_decision.get("should_ask_start_location", True):
                start_location = self._ask_start_location_if_needed(user_input)
            if isinstance(start_location, dict) and start_location.get("_cancelled"):
                self.console.print("\u5df2\u53d6\u6d88\u672c\u6b21\u8def\u7ebf\u89c4\u5212\u3002", style="yellow")
                return
        self._print_initial_feedback(user_input, preset_route_type)

        try:
            status_context = self.console.status("思考中...", spinner="dots") if request_id is None else nullcontext()
            with status_context:
                from agentscope.message import Msg

                # 1. 获取长期记忆摘要与上下文（原逻辑不变）
                self._set_current_stage("正在读取长期记忆...", request_id)
                stage_started_at = time.monotonic()
                long_term_summary = await self._get_long_term_summary(user_input)
                self._print_stage_timing("long_term_memory", stage_started_at, request_id)
                if not self._is_current_request(request_id):
                    return
                context_messages = []
                if long_term_summary:
                    context_messages.append(Msg(name="system", content=long_term_summary, role="system"))
                if memory_preference_context:
                    context_messages.append(Msg(
                        name="system",
                        content=json.dumps({"memory_preference_context": memory_preference_context}, ensure_ascii=False),
                        role="system",
                    ))
                if planning_turn_decision:
                    context_messages.append(Msg(
                        name="system",
                        content=json.dumps({"planning_turn_decision": planning_turn_decision}, ensure_ascii=False),
                        role="system",
                    ))
                for msg in recent_context:
                    context_messages.append(Msg(name=msg["role"], content=msg["content"], role=msg["role"]))
                if preset_route_type:
                    context_messages.append(Msg(
                        name="system",
                        content=json.dumps({"preset_route_type": preset_route_type}, ensure_ascii=False),
                        role="system",
                    ))
                if start_location:
                    context_messages.append(Msg(
                        name="system",
                        content=json.dumps({"start_location": start_location}, ensure_ascii=False),
                        role="system",
                    ))
                context_messages.append(Msg(name="user", content=user_input, role="user"))

                # 2. 意图识别（仅此调用加重试，原逻辑不变）
                self._set_current_stage("正在识别你的出行意图...", request_id)
                stage_started_at = time.monotonic()
                intention_result = None
                try:
                    intention_call = retry_with_backoff(
                        lambda: self.intention_agent.reply(context_messages),
                        max_retries=max_retries,
                        base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                        max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
                    )
                    intention_result = await intention_call
                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()
                except asyncio.TimeoutError:
                    if self.circuit_breaker:
                        self.circuit_breaker.record_failure()
                    self._emit_strict_failure(
                        stage="intent_recognition",
                        error_type="intent_recognition_timeout",
                        message="意图识别超时，本次规划已中止，未生成路线。",
                        request_id=request_id,
                        started_at=start_time,
                        diagnostics={
                            "timeout_sec": float(rc.get("route_intent_recognition_timeout_sec", 0) or 0),
                            "timeout_disabled": float(rc.get("route_intent_recognition_timeout_sec", 0) or 0) <= 0,
                            "preset_route_type": preset_route_type,
                            "intent_debug": getattr(self.intention_agent, "last_intent_debug", {}),
                        },
                    )
                    return
                except CircuitOpenError:
                    raise
                except Exception as e:
                    if self.circuit_breaker:
                        self.circuit_breaker.record_failure()
                    raise
                finally:
                    self._print_stage_timing("intent_recognition", stage_started_at, request_id)

                if not self._is_current_request(request_id):
                    return

                # 3. 解析意图识别结果（原逻辑不变：解析失败则友好提示并 return）
                try:
                    intention_data = json.loads(intention_result.content)
                except json.JSONDecodeError:
                    self._emit_strict_failure(
                        stage="intent_recognition",
                        error_type="intent_json_parse_failed",
                        message="需求识别结果格式异常，本次规划已中止。",
                        request_id=request_id,
                        started_at=start_time,
                        diagnostics={"raw_preview": str(getattr(intention_result, "content", ""))[:500]},
                    )
                    return
                    if self._is_current_request(request_id):
                        self.console.print("暂时无法理解您的需求，请换一种说法。", style="bold red")
                    return

                if long_term_summary:
                    intention_data["required_long_term_context"] = long_term_summary
                if memory_preference_context:
                    intention_data["memory_preference_context"] = memory_preference_context
                if planning_turn_decision:
                    intention_data["planning_turn_decision"] = planning_turn_decision
                    intention_data = self._apply_planning_turn_decision_to_intention(
                        intention_data,
                        planning_turn_decision,
                    )
                if isinstance(memory_preference_context.get("user_preferences"), dict):
                    intention_data["user_preferences"] = memory_preference_context["user_preferences"]
                if start_location:
                    intention_data["start_location"] = start_location
                    key_entities = intention_data.get("key_entities")
                    if not isinstance(key_entities, dict):
                        key_entities = {}
                    key_entities["start_location"] = start_location
                    intention_data["key_entities"] = key_entities
                intention_data = self._apply_preset_route_preference_constraints(
                    intention_data,
                    preset_route_type,
                    user_input,
                )

                if hasattr(self.intention_agent, "_normalize_agent_schedule"):
                    intention_data = self.intention_agent._normalize_agent_schedule(intention_data)
                    intention_result = Msg(
                        name=intention_result.name,
                        content=json.dumps(intention_data, ensure_ascii=False),
                        role=intention_result.role,
                    )

            if not self._is_current_request(request_id):
                return

            # 4. 调度处理链路
            self._set_current_stage("正在协调地点检索和路线计算...", request_id)
            stage_started_at = time.monotonic()
            orchestration_result = None
            try:
                orchestration_result = await retry_with_backoff(
                    lambda: self.orchestrator.reply(intention_result),
                    max_retries=max_retries,
                    base_delay_sec=rc.get("retry_base_delay_sec", 1.0),
                    max_delay_sec=rc.get("retry_max_delay_sec", 30.0),
                )
                if self.circuit_breaker:
                    self.circuit_breaker.record_success()
            except CircuitOpenError:
                raise
            except Exception as e:
                if self.circuit_breaker:
                    self.circuit_breaker.record_failure()
                raise
            finally:
                self._print_stage_timing("orchestration", stage_started_at, request_id)

            if not self._is_current_request(request_id):
                return

            # 5. 解析执行结果（原逻辑不变）
            self._set_current_stage("正在整理结果...", request_id)
            try:
                result_data = json.loads(orchestration_result.content)
            except json.JSONDecodeError:
                self._emit_strict_failure(
                    stage="orchestration",
                    error_type="orchestration_json_parse_failed",
                    message="路线整理结果格式异常，本次规划已中止。",
                    request_id=request_id,
                    started_at=start_time,
                    diagnostics={"raw_preview": str(getattr(orchestration_result, "content", ""))[:500]},
                )
                return
                result_data = {"error": "解析结果失败"}

                # 6. 显示处理进度与最终结果
            self._emit_query_result_if_current(user_input, result_data, request_id)
            strict_error = self._strict_failure_from_result(result_data, request_id, start_time)
            if strict_error:
                self._display_strict_failure(strict_error)
                self.memory_manager.add_message("user", user_input)
                self.memory_manager.add_message("assistant", json.dumps(strict_error, ensure_ascii=False))
                return
            self._emit_query_result_if_current(user_input, result_data, request_id)
        except asyncio.CancelledError:
            raise

    def _emit_query_result_if_current(self, user_input: str, result_data: dict, request_id: Optional[int]) -> bool:
        if not self._is_current_request(request_id):
            return False
        self._display_agents_called(result_data)
        self.console.print()
        self._display_results(result_data)
        self._save_route_food_preference_from_query(user_input, result_data)
        self.memory_manager.add_message("user", user_input)
        self.memory_manager.add_message("assistant", json.dumps(result_data, ensure_ascii=False))
        return True

    @staticmethod
    def _looks_like_preference_query(user_input: str) -> bool:
        text = str(user_input or "").strip()
        if not text:
            return False
        query_terms = ("我的偏好", "我有什么偏好", "我喜欢什么", "我爱吃什么", "我喜欢吃什么", "preferences")
        return any(term in text for term in query_terms) and any(term in text for term in ("什么", "哪些", "查看", "查询", "吗", "?","？", "preferences"))

    def _save_route_food_preference_from_query(self, user_input: str, result_data: dict) -> None:
        if not self.memory_manager or not isinstance(result_data, dict):
            return
        if result_data.get("status") == "failed":
            return
        if not self._has_successful_route_result(result_data):
            return
        cuisine_data = IntentionAgent._cuisine_preference_from_text(user_input)
        cuisine = str(cuisine_data.get("cuisine") or "").strip() if isinstance(cuisine_data, Mapping) else ""
        if not cuisine:
            return
        current = self.memory_manager.long_term.get_preference("meal_preference")
        values = []
        if isinstance(current, list):
            values = [str(item).strip() for item in current if str(item).strip()]
        elif str(current or "").strip():
            values = [str(current).strip()]
        if cuisine not in values:
            values.append(cuisine)
        self.memory_manager.long_term.save_preference("meal_preference", values if len(values) > 1 else values[0])

    @staticmethod
    def _has_successful_route_result(result_data: dict) -> bool:
        results = result_data.get("results") if isinstance(result_data.get("results"), list) else []
        for item in results:
            if not isinstance(item, dict) or item.get("status") != "success":
                continue
            if item.get("agent_name") in {"route_planning", "itinerary_planning"}:
                data = item.get("data") if isinstance(item.get("data"), dict) else {}
                route_options = data.get("route_options")
                nested = data.get("data") if isinstance(data.get("data"), dict) else {}
                itinerary = data.get("itinerary") if isinstance(data.get("itinerary"), dict) else nested.get("itinerary") if isinstance(nested.get("itinerary"), dict) else {}
                if route_options or (isinstance(itinerary, dict) and itinerary.get("route_options")):
                    return True
        return False

    def _emit_strict_failure(
        self,
        stage: str,
        error_type: str,
        message: str,
        request_id: Optional[int],
        started_at: float,
        diagnostics: Optional[dict] = None,
    ) -> dict:
        import time

        payload = {
            "status": "failed",
            "stage": stage,
            "error_type": error_type,
            "message": message,
            "request_id": request_id,
            "elapsed_sec": round(max(0.0, time.time() - float(started_at or time.time())), 2),
            "diagnostics": diagnostics or {},
        }
        self._display_strict_failure(payload)
        return payload

    def _display_strict_failure(self, payload: Mapping[str, Any]) -> None:
        error_type = str(payload.get("error_type") or "")
        message = self._friendly_failure_message(error_type, payload.get("message"))
        self.console.print("\n[bold red]本次没有生成可出发的路线[/bold red]")
        self.console.print(message)
        friendly_reasons = self._friendly_warning_lines([error_type])
        diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
        if diagnostics:
            friendly_reasons.extend(self._friendly_warning_lines(diagnostics.get("warnings", [])))
        if friendly_reasons:
            self.console.print("\n可以这样调整：", style="dim")
            for item in friendly_reasons[:4]:
                self.console.print(f"  - {item}", style="dim")
        self.console.print("你可以换一个更具体的起点、放宽区域，或减少必须完成的活动。", style="dim")

    def _friendly_failure_message(self, error_type: str, message: Any = "") -> str:
        mapping = {
            "connector_slot_empty": "必须活动已有候选点，但缺少适合串联路线的第三个地点，本次没有生成可出发路线。",
            "required_activity_slot_empty": "有一个必须完成的活动没有找到合适地点，本次没有生成可出发路线。",
            "poi_search_empty": "没有找到足够合适的真实地点，本次没有生成可出发路线。",
            "route_cost_matrix_failed": "地图路线服务暂时不可用，无法可靠计算真实通勤成本。",
        }
        if error_type in mapping:
            return mapping[error_type]
        text = str(message or "").strip()
        return text or "这次没有生成可出发的路线。"

    def _strict_failure_from_result(
        self,
        result_data: dict,
        request_id: Optional[int],
        started_at: float,
    ) -> Optional[dict]:
        import time

        if not isinstance(result_data, dict):
            return {
                "status": "failed",
                "stage": "orchestration",
                "error_type": "invalid_orchestration_result",
                "message": "工具编排结果不是对象，本次规划已中止。",
                "request_id": request_id,
                "elapsed_sec": round(max(0.0, time.time() - float(started_at or time.time())), 2),
                "diagnostics": {"result_type": type(result_data).__name__},
            }
        if result_data.get("status") not in {"failed", "partial_failure"} and not result_data.get("error"):
            return None
        errors = result_data.get("error_details")
        if not isinstance(errors, list) or not errors:
            errors = result_data.get("results") if isinstance(result_data.get("results"), list) else []
        first = next(
            (
                item for item in errors
                if isinstance(item, dict) and (
                    item.get("status") == "error"
                    or item.get("error")
                    or item.get("error_type")
                    or (isinstance(item.get("data"), dict) and item["data"].get("error"))
                )
            ),
            {},
        )
        data = first.get("data") if isinstance(first.get("data"), dict) else {}
        return {
            "status": "failed",
            "stage": str(first.get("agent_name") or result_data.get("stage") or "orchestration"),
            "error_type": str(data.get("error_type") or first.get("error_type") or result_data.get("error_type") or "stage_failed"),
            "message": str(data.get("error") or first.get("message") or result_data.get("message") or "规划过程中有一步没有完成，本次已中止。"),
            "request_id": request_id,
            "elapsed_sec": round(max(0.0, time.time() - float(started_at or time.time())), 2),
            "diagnostics": data.get("diagnostics") if isinstance(data.get("diagnostics"), dict) else data,
        }

    def _ask_route_preference_if_needed(self, user_input: str) -> Optional[str]:
        if not self._looks_like_route_query(user_input):
            return None

        self.console.print("\n[bold cyan]请选择路线偏好，直接回车将由系统自动判断：[/bold cyan]")
        self.console.print("1. 打卡路线：更注重景点、拍照打卡和游玩项目")
        self.console.print("2. 美食路线：更注重特色餐饮、本地小吃和餐厅体验")
        self.console.print("3. 景点和餐饮兼顾：观光和吃饭都照顾")
        self.console.print("4. 跳过：让系统自动判断")

        choice = self.console.input("请输入对应选项数字: ")
        route_type = self._parse_route_preference_choice(choice)
        if route_type is None:
            self.console.print("未识别选项，已默认由系统自动判断。", style="yellow")
            return "auto"
        return route_type

    def _ask_start_location_if_needed(self, user_input: str) -> Optional[dict]:
        if not self._looks_like_route_query(user_input):
            return None
        explicit = self._extract_start_location_from_route_text(user_input)
        if explicit:
            return explicit
        city = self._infer_city_from_text(user_input)
        remembered_start = self._remembered_start_location(city)
        if remembered_start:
            confirmed = self._confirm_or_replace_start_location(remembered_start, city)
            if confirmed:
                return confirmed

        self.console.print(
            "\n[bold cyan]\u8bf7\u544a\u8bc9\u6211\u4f60\u7684\u521d\u59cb\u5730\uff0c\u6211\u624d\u80fd\u89c4\u5212\u771f\u5b9e\u8def\u7ebf\u3002[/bold cyan]"
        )
        self.console.print(
            "\u4f8b\u5982\uff1a\u56fd\u8d38\u3001\u5929\u5b89\u95e8\u3001\u897f\u5355\u3001\u671b\u4eacSOHO\u3002\u8f93\u5165 /cancel \u53ef\u53d6\u6d88\u672c\u6b21\u89c4\u5212\u3002",
            style="dim",
        )
        for _ in range(2):
            value = self.console.input("\u8bf7\u8f93\u5165\u521d\u59cb\u5730: ").strip()
            if value == "/cancel":
                return {"_cancelled": True}
            if value:
                if self._is_city_only_location_text(value, city):
                    self.console.print("\u8bf7\u8f93\u5165\u66f4\u5177\u4f53\u7684\u521d\u59cb\u5730\uff0c\u4f8b\u5982\u5929\u5b89\u95e8\u3001\u56fd\u8d38\u3001\u897f\u5355\u6216\u67d0\u4e2a\u5730\u94c1\u7ad9\u3002", style="yellow")
                    continue
                return {
                    "name": value,
                    "address": value,
                    "city": city,
                    "location": None,
                    "source": "cli_user_prompt",
                }
            self.console.print("\u521d\u59cb\u5730\u4e0d\u80fd\u4e3a\u7a7a\uff0c\u8bf7\u8f93\u5165\u4e00\u4e2a\u5546\u5708\u3001\u5730\u6807\u6216\u5730\u5740\u3002", style="yellow")
        return {"_cancelled": True}

    def _remembered_start_location(self, city: str) -> Optional[dict]:
        previous_start = self._previous_route_start_location(city)
        if previous_start:
            return previous_start
        return self._memory_start_location(city)

    def _previous_route_start_location(self, city: str) -> Optional[dict]:
        memory_manager = getattr(self, "memory_manager", None)
        if not memory_manager:
            return None
        try:
            recent_context = memory_manager.short_term.get_recent_context(n_turns=5)
        except Exception:
            return None
        previous = self._extract_previous_route_turn(recent_context or [])
        candidate = previous.get("start_location") if isinstance(previous, dict) else None
        if not candidate:
            previous_route = previous.get("previous_route") if isinstance(previous, dict) else {}
            candidate = previous_route.get("start_location") if isinstance(previous_route, dict) else None
        return self._normalize_start_location_candidate(candidate, city, "previous_route_start")

    def _confirm_or_replace_start_location(self, candidate: dict, city: str) -> Optional[dict]:
        name = str(candidate.get("name") or candidate.get("address") or "").strip()
        if not name:
            return None
        source = str(candidate.get("source") or "")
        source_label = "\u4e0a\u6b21\u8def\u7ebf" if source == "previous_route_start" else "\u8bb0\u5fc6"
        self.console.print(
            f"\n[bold cyan]\u6211\u4ece{source_label}\u4e2d\u627e\u5230\u4e86\u51fa\u53d1\u5730\uff1a{name}[/bold cyan]"
        )
        self.console.print(
            "\u76f4\u63a5\u56de\u8f66\u6cbf\u7528\uff1b\u8f93\u5165\u65b0\u5730\u70b9\u53ef\u4fee\u6539\uff1b\u8f93\u5165 /cancel \u53ef\u53d6\u6d88\u672c\u6b21\u89c4\u5212\u3002",
            style="dim",
        )
        value = self.console.input("\u8bf7\u786e\u8ba4\u521d\u59cb\u5730: ").strip()
        if value == "/cancel":
            return {"_cancelled": True}
        if not value:
            confirmed = dict(candidate)
            confirmed["source"] = f"{source or 'remembered_start'}_confirmed"
            return confirmed
        if self._is_city_only_location_text(value, city):
            self.console.print("\u8bf7\u8f93\u5165\u66f4\u5177\u4f53\u7684\u521d\u59cb\u5730\uff0c\u4f8b\u5982\u5929\u5b89\u95e8\u3001\u56fd\u8d38\u3001\u897f\u5355\u6216\u67d0\u4e2a\u5730\u94c1\u7ad9\u3002", style="yellow")
            return self._prompt_start_location(city)
        return {
            "name": value,
            "address": value,
            "city": city,
            "location": None,
            "source": "cli_user_override_start",
            "replaced_start_location": {
                "name": candidate.get("name"),
                "source": candidate.get("source"),
            },
        }

    def _prompt_start_location(self, city: str) -> Optional[dict]:
        for _ in range(2):
            value = self.console.input("\u8bf7\u8f93\u5165\u521d\u59cb\u5730: ").strip()
            if value == "/cancel":
                return {"_cancelled": True}
            if value:
                if self._is_city_only_location_text(value, city):
                    self.console.print("\u8bf7\u8f93\u5165\u66f4\u5177\u4f53\u7684\u521d\u59cb\u5730\uff0c\u4f8b\u5982\u5929\u5b89\u95e8\u3001\u56fd\u8d38\u3001\u897f\u5355\u6216\u67d0\u4e2a\u5730\u94c1\u7ad9\u3002", style="yellow")
                    continue
                return {
                    "name": value,
                    "address": value,
                    "city": city,
                    "location": None,
                    "source": "cli_user_prompt",
                }
            self.console.print("\u521d\u59cb\u5730\u4e0d\u80fd\u4e3a\u7a7a\uff0c\u8bf7\u8f93\u5165\u4e00\u4e2a\u5546\u5708\u3001\u5730\u6807\u6216\u5730\u5740\u3002", style="yellow")
        return {"_cancelled": True}

    def _normalize_start_location_candidate(self, candidate: Any, city: str, source: str) -> Optional[dict]:
        if isinstance(candidate, Mapping):
            name = str(candidate.get("name") or candidate.get("address") or "").strip()
            if not name or self._is_city_only_location_text(name, city):
                return None
            normalized = dict(candidate)
            normalized.setdefault("name", name)
            normalized.setdefault("address", name)
            normalized.setdefault("city", city)
            normalized.setdefault("location", candidate.get("location"))
            normalized["source"] = source
            return normalized
        text = str(candidate or "").strip()
        if not text or self._is_city_only_location_text(text, city):
            return None
        return {
            "name": text,
            "address": text,
            "city": city,
            "location": None,
            "source": source,
        }

    def _memory_start_location(self, city: str) -> Optional[dict]:
        memory_manager = getattr(self, "memory_manager", None)
        if not memory_manager:
            return None
        try:
            prefs = memory_manager.long_term.get_preference()
        except Exception:
            return None
        if not isinstance(prefs, dict):
            return None
        home = str(prefs.get("home_location") or "").strip()
        if not home or self._is_city_only_location_text(home, city):
            return None
        return {
            "name": home,
            "address": home,
            "city": city,
            "location": None,
            "source": "memory_home_location",
        }

    @staticmethod
    def _extract_start_location_from_route_text(user_input: str) -> Optional[dict]:
        text = str(user_input or "")
        patterns = (
            r"(?:\u6211\u5728|\u5f53\u524d\u4f4d\u7f6e\u5728|\u73b0\u5728\u5728)([\u4e00-\u9fa5A-Za-z0-9\s\-]{2,30})(?:\u9644\u8fd1|\u5468\u8fb9|\u8fd9\u8fb9|\u51fa\u53d1|\uff0c|,|\u3002|\.|\u60f3|\u8981|$)",
            r"(?:\u6211\u5728|\u5f53\u524d\u4f4d\u7f6e\u5728|\u73b0\u5728\u5728)([\u4e00-\u9fa5A-Za-z0-9路\-\s]{2,30})(?:\u9644\u8fd1|\u5468\u8fb9|\u8fd9\u8fb9|\u51fa\u53d1|\uff0c|,|\u3002|\.|\u60f3|\u8981|$)",
            r"(?:\u4ece|\u7531)([\u4e00-\u9fa5A-Za-z0-9·\-\s]{2,30})(?:\u51fa\u53d1|\u5f00\u59cb|\u8d77\u6b65)",
            r"(?:\u6211\u5728|\u5f53\u524d\u4f4d\u7f6e\u5728|\u73b0\u5728\u5728)([\u4e00-\u9fa5A-Za-z0-9·\-\s]{2,30})(?:\u9644\u8fd1|\u5468\u8fb9|\u8fd9\u8fb9|\u51fa\u53d1|$)",
        )
        patterns = (
            r"(?:\u6211\u4e00\u4e2a\u4eba\u5728|\u4e00\u4e2a\u4eba\u5728)([\u4e00-\u9fa5A-Za-z0-9\s\-]{2,30})(?:\u9644\u8fd1|\u5468\u8fb9|\u8fd9\u8fb9|\u51fa\u53d1|\uff0c|,|\u3002|\.|\u60f3|\u8981|$)",
            r"\u5728([\u4e00-\u9fa5A-Za-z0-9\s\-]{2,30})(?:\u9644\u8fd1|\u5468\u8fb9)",
            r"(?:\u6211\u5728|\u5f53\u524d\u4f4d\u7f6e\u5728|\u73b0\u5728\u5728)([\u4e00-\u9fa5A-Za-z0-9\s\-]{2,30})(?:\u9644\u8fd1|\u5468\u8fb9|\u8fd9\u8fb9|\u51fa\u53d1|\uff0c|,|\u3002|\.|\u60f3|\u8981|$)",
            r"(?:\u4ece|\u7531)([\u4e00-\u9fa5A-Za-z0-9\s\-]{2,30})(?:\u51fa\u53d1|\u5f00\u59cb|\u8d77\u6b65)",
            r"(?:\u6211\u5728|\u5f53\u524d\u4f4d\u7f6e\u5728|\u73b0\u5728\u5728)([\u4e00-\u9fa5A-Za-z0-9\s\-]{2,30})(?:\u9644\u8fd1|\u5468\u8fb9|\u8fd9\u8fb9|\u51fa\u53d1|$)",
        )
        for pattern in patterns:
            try:
                match = re.search(pattern, text)
            except re.error:
                continue
            if not match:
                continue
            name = match.group(1).strip(" \t\r\n\uff0c,\u3002.")
            if name and name not in {"\u5317\u4eac", "\u5317\u4eac\u5e02", "\u4e0a\u6d77", "\u4e0a\u6d77\u5e02"}:
                return {
                    "name": name,
                    "address": name,
                    "city": AligoCLI._infer_city_from_text(text),
                    "location": None,
                    "source": "user_query",
                }
        return None

    @staticmethod
    def _infer_city_from_text(text: str) -> str:
        value = str(text or "")
        for city in ("\u5317\u4eac", "\u4e0a\u6d77", "\u5e7f\u5dde", "\u6df1\u5733", "\u676d\u5dde", "\u6210\u90fd", "\u5357\u4eac", "\u897f\u5b89", "\u6b66\u6c49", "\u91cd\u5e86", "\u5929\u6d25"):
            if city in value:
                return city
        return "\u5317\u4eac"

    @staticmethod
    def _is_city_only_location_text(value: str, city: str) -> bool:
        text = str(value or "").strip()
        city_text = str(city or "").strip()
        if not text:
            return False
        known_cities = {
            "\u5317\u4eac", "\u4e0a\u6d77", "\u5e7f\u5dde", "\u6df1\u5733", "\u676d\u5dde", "\u6210\u90fd",
            "\u5357\u4eac", "\u897f\u5b89", "\u6b66\u6c49", "\u91cd\u5e86", "\u5929\u6d25",
        }
        aliases = set()
        for item in known_cities:
            aliases.add(item)
            aliases.add(f"{item}\u5e02")
        if city_text:
            aliases.add(city_text)
            aliases.add(f"{city_text}\u5e02")
        return text in aliases

    def _print_initial_feedback(self, user_input: str, preset_route_type: Optional[str]) -> None:
        if not self._looks_like_route_query(user_input):
            return
        label_map = {
            "sightseeing": "拍照打卡",
            "food": "美食优先",
            "balanced": "景点和餐饮兼顾",
            "auto": "自动判断",
        }
        preference_label = label_map.get(str(preset_route_type or "auto"), "自动判断")
        self.console.print(
            f"\n已收到路线需求。偏好：{preference_label}。正在结合真实地点、天气、营业时间和交通成本整理方案...",
            style="green",
        )
        return

        """Print a stable first response before any slow LLM or API call."""
        if not self._looks_like_route_query(user_input):
            return

        label_map = {
            "sightseeing": "打卡路线",
            "food": "美食路线",
            "balanced": "景点和餐饮兼顾",
            "auto": "系统自动判断路线偏好",
        }
        preference_label = label_map.get(str(preset_route_type or "auto"), "系统自动判断路线偏好")
        self.console.print(
            f"\n已收到你的路线需求。偏好：{preference_label}。轻途正在结合你的描述、真实地点和出行约束整理方案...",
            style="green",
        )

    @staticmethod
    def _parse_route_preference_choice(choice: str) -> Optional[str]:
        text = str(choice or "").strip()
        if not text:
            return "auto"
        if text.startswith("1"):
            return "sightseeing"
        if text.startswith("2"):
            return "food"
        if text.startswith("3"):
            return "balanced"
        if text.startswith("4"):
            return "auto"
        return None

    @staticmethod
    def _looks_like_route_query(user_input: str) -> bool:
        text = str(user_input or "")
        terms = (
            "一日游", "游玩", "旅游", "旅行", "出行", "路线", "行程", "安排",
            "想吃", "吃好", "美食", "景点", "不想排队", "少排队", "小时",
        )
        normal_terms = (
            "\u4e00\u65e5\u6e38", "\u6e38\u73a9", "\u65c5\u6e38", "\u65c5\u884c", "\u51fa\u884c", "\u8def\u7ebf", "\u884c\u7a0b", "\u5b89\u6392",
            "\u60f3\u5403", "\u7f8e\u98df", "\u666f\u70b9", "\u4e0d\u60f3\u6392\u961f", "\u5c11\u6392\u961f", "\u5c0f\u65f6",
            "\u77ed\u9014\u6e38", "citywork",
            "\u5bb5\u591c",
            "\u4e0b\u73ed", "\u6309\u6469", "\u591c\u5bb5", "\u5b99\u591c", "citywalk", "\u6563\u6b65",
        )
        return any(term in text for term in (*terms, *normal_terms))

    def _display_agents_called(self, result_data: dict):
        results = result_data.get("results", []) if isinstance(result_data, dict) else []
        if not results:
            return
        status_labels = {"success": "完成", "error": "未完成"}
        if self._last_clean_route_render_signature:
            for result in results:
                if not isinstance(result, dict) or result.get("status") != "success":
                    continue
                data = result.get("data") if isinstance(result.get("data"), dict) else {}
                nested = data.get("data") if isinstance(data.get("data"), dict) else {}
                route_options = []
                if result.get("agent_name") == "itinerary_planning":
                    itinerary = data.get("itinerary") or nested.get("itinerary")
                    if isinstance(itinerary, dict):
                        route_options = itinerary.get("route_options") or []
                elif result.get("agent_name") == "route_planning":
                    route_options = data.get("route_options") or nested.get("route_options") or []
                if self._clean_route_render_signature(route_options) == self._last_clean_route_render_signature:
                    return
        parts = []
        for result in results:
            if not isinstance(result, dict):
                continue
            display_name = self._get_agent_display_name(result.get("agent_name", ""))
            status = status_labels.get(str(result.get("status") or ""), "处理中")
            elapsed = result.get("elapsed_sec")
            try:
                elapsed_text = f" {float(elapsed):.2f}s" if elapsed not in (None, "", []) else ""
            except (TypeError, ValueError):
                elapsed_text = ""
            parts.append(f"{display_name}{status}{elapsed_text}")
        if parts:
            self.console.print()
            self.console.print(f"处理进度: {', '.join(parts)}", style="dim")
        return

        """显示用户可理解的处理进度"""
        results = result_data.get("results", [])
        if results and self._display_clean_route_response(results):
            self.console.print()
            return
        if not results:
            return

        agents_called = []
        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")

            display_name = self._get_agent_display_name(agent_name)

            # 根据状态添加标记
            if status == "success":
                agents_called.append(f"{display_name}完成")
            elif status == "error":
                agents_called.append(f"{display_name}未完成")
            else:
                agents_called.append(f"{display_name}处理中")

        if agents_called:
            self.console.print()
            self.console.print(f"处理进度: {', '.join(agents_called)}", style="dim")

    def _display_results(self, result_data: dict):
        """显示执行结果 - 确保永远有回复"""
        self.console.print()

        # 获取结果列表
        results = result_data.get("results", [])
        if results and self._display_clean_route_response(results):
            self.console.print()
            return

        if not results:
            # 情况1: 没有任何智能体被调用
            status = result_data.get("status", "unknown")
            if status == "no_agents":
                self.console.print("好的，我已记录下来。", style="green")
                self.console.print("\n提示: 您可以继续补充信息，或者尝试：", style="dim")
                self.console.print("  • 规划路线：「北京短途游，从国贸出发，6小时，想吃本地特色」", style="dim")
                self.console.print("  • 查询信息：「北京的天气怎么样」", style="dim")
                self.console.print("  • 补充偏好：「我不想排队，尽量少走路」", style="dim")
            else:
                self.console.print("未能获取有效结果，请重新描述您的需求。", style="yellow")
        else:
            # 情况2: 有智能体被调用，生成人性化回复
            has_output = self._generate_human_response(results)

            # 情况3: 智能体执行了但没有显示内容（兜底）
            if not has_output:
                self.console.print("已处理您的请求。", style="green")

        self.console.print()

    def _display_clean_route_response(self, results: list) -> bool:
        """Render itinerary and route-planning results with a product-facing layout."""
        itinerary = None
        route_data = None
        for result in results:
            if not isinstance(result, dict) or result.get("status") != "success":
                continue
            agent_name = result.get("agent_name")
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            if agent_name == "itinerary_planning":
                candidate = data.get("itinerary") or nested.get("itinerary")
                if isinstance(candidate, dict):
                    itinerary = candidate
            elif agent_name == "route_planning":
                if isinstance(data.get("route_options"), list):
                    route_data = data
                elif isinstance(nested.get("route_options"), list):
                    route_data = nested

        if not itinerary and not route_data:
            return False

        payload = itinerary or route_data or {}
        route_options = []
        if isinstance(itinerary, dict) and isinstance(itinerary.get("route_options"), list):
            route_options = itinerary.get("route_options") or []
        if not route_options and isinstance(route_data, dict):
            route_options = route_data.get("route_options") or []
        if not route_options:
            return False
        signature = self._clean_route_render_signature(route_options)
        if signature and signature == self._last_clean_route_render_signature:
            return True
        self._last_clean_route_render_signature = signature

        primary = route_options[0] if isinstance(route_options[0], dict) else {}
        title = self._clean_route_title(payload, route_data, primary)
        self.console.print(f"\n[bold cyan]轻途：{title}[/bold cyan]")
        alternatives = [option for option in route_options[1:3] if isinstance(option, dict)]
        if alternatives:
            self.console.print("下面是一条按顺序可执行的城市微行程，并附上备选方案。", style="dim")
        else:
            self.console.print("下面是一条按顺序可执行的城市微行程。", style="dim")

        self._display_clean_trip_brief(payload, route_data, primary)
        self._print_result_section("主路线")
        self._display_clean_route_option(primary, index=1, primary=True)

        if alternatives:
            self._print_result_section("备选方案")
            for index, option in enumerate(alternatives, 2):
                self._display_clean_route_option(option, index=index, primary=False)

        warnings = []
        for source in (payload, route_data, primary):
            if isinstance(source, dict):
                warnings.extend(source.get("warnings") or [])
        warning_lines = self._friendly_warning_lines_clean(warnings)
        if warning_lines:
            self._print_result_section("出行提醒")
            for line in warning_lines:
                self.console.print(f"- {line}", style="yellow")
        return True

    @staticmethod
    def _clean_route_render_signature(route_options: list) -> str:
        if not route_options or not isinstance(route_options[0], Mapping):
            return ""
        first = route_options[0]
        sequence = first.get("poi_sequence")
        if not sequence and isinstance(first.get("pois"), list):
            sequence = [
                poi.get("name")
                for poi in first.get("pois")
                if isinstance(poi, Mapping) and poi.get("name")
            ]
        return "|".join(str(item) for item in (sequence or [])) + f"@{first.get('estimated_duration_min')}"

    def _clean_route_title(self, payload: Mapping[str, Any], route_data: Optional[Mapping[str, Any]], primary: Mapping[str, Any]) -> str:
        urban_profile = self._clean_urban_profile(payload, route_data)
        raw_scenario = urban_profile.get("scenario")
        if isinstance(raw_scenario, Mapping):
            label = str(raw_scenario.get("label") or "").strip()
            raw_scenario = (
                raw_scenario.get("family")
                or raw_scenario.get("type")
                or raw_scenario.get("id")
                or label
            )
        scenario = str(raw_scenario or "").strip()
        if scenario:
            inferred_title = self._title_from_urban_profile(urban_profile, scenario)
            if inferred_title:
                return inferred_title
            return self._urban_scenario_label(scenario)
        for source in (payload, primary):
            if isinstance(source, Mapping):
                title = str(source.get("display_title") or source.get("title") or source.get("name") or "").strip()
                if title and not self._looks_mojibake(title):
                    return self._display_itinerary_title(title)
        return "城市微行程"

    def _title_from_urban_profile(self, urban_profile: Mapping[str, Any], scenario: str = "") -> str:
        scenario_text = str(scenario or "").casefold()
        generic_scenarios = {"custom", "general", "general_urban_micro_trip", "urban_micro_trip", "other", "unknown"}
        if scenario_text and scenario_text not in generic_scenarios:
            return ""
        activities = urban_profile.get("activity_sequence") if isinstance(urban_profile.get("activity_sequence"), list) else []
        activity_text = " ".join(
            str(item.get("label") or item.get("activity_label") or item.get("type") or item.get("activity_type") or "")
            for item in activities
            if isinstance(item, Mapping)
        ).casefold()
        weather = urban_profile.get("weather_context") if isinstance(urban_profile.get("weather_context"), Mapping) else {}
        condition = str(weather.get("condition") or "").casefold()
        indoor_preferred = bool(weather.get("indoor_preferred"))
        rainy = condition in {"rain", "storm"} or str(weather.get("precipitation_risk") or "").casefold() in {"medium", "high"} or indoor_preferred
        companions = urban_profile.get("companions") if isinstance(urban_profile.get("companions"), list) else []
        companion_text = " ".join(str(item.get("type") or item.get("label") or "") for item in companions if isinstance(item, Mapping)).casefold()
        social = urban_profile.get("social_context") if isinstance(urban_profile.get("social_context"), Mapping) else {}
        social_text = " ".join(str(value or "") for value in social.values()).casefold()
        romantic = any(term in companion_text or term in social_text for term in ("partner", "romantic", "\u4f34\u4fa3", "\u5973\u670b\u53cb", "\u60c5\u4fa3", "\u7ea6\u4f1a"))

        has_exhibition = any(term in activity_text for term in ("exhibition", "gallery", "museum", "art", "\u5c55\u89c8", "\u770b\u5c55", "\u7f8e\u672f\u9986", "\u535a\u7269\u9986"))
        has_drinks = any(term in activity_text for term in ("drink", "drinks", "bar", "wine", "cocktail", "\u5c0f\u9152", "\u5c0f\u914c", "\u70b9\u5c0f\u9152", "\u559d\u9152", "\u9152\u9986", "\u9152\u5427", "\u6e05\u5427"))
        has_wellness = any(term in activity_text for term in ("wellness", "massage", "spa", "\u6309\u6469", "\u8db3\u7597", "\u653e\u677e"))
        has_late_food = any(term in activity_text for term in ("late", "snack", "\u591c\u5bb5", "\u6df1\u591c", "\u5c0f\u5403"))
        has_beauty = any(term in activity_text for term in ("nail", "beauty", "manicure", "\u7f8e\u7532", "\u505a\u6307\u7532", "\u6307\u7532", "\u7f8e\u776b", "\u95fa\u871c"))

        if has_exhibition and has_drinks and romantic and rainy:
            return "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf"
        if has_exhibition and has_drinks:
            return "\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf"
        if has_wellness and has_late_food:
            return "\u4e0b\u73ed\u6309\u6469\u591c\u5bb5\u8def\u7ebf"
        if has_beauty and has_drinks:
            return "\u95fa\u871c\u7f8e\u7532\u5c0f\u9152\u8def\u7ebf"
        return ""

    def _clean_urban_profile(self, payload: Optional[Mapping[str, Any]], route_data: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        for source in (payload, route_data):
            if isinstance(source, Mapping):
                profile = source.get("urban_intent_profile")
                if isinstance(profile, dict):
                    return profile
                nested = source.get("data") if isinstance(source.get("data"), dict) else {}
                profile = nested.get("urban_intent_profile")
                if isinstance(profile, dict):
                    return profile
        return {}

    def _results_indicate_micro_trip(self, results: list) -> bool:
        if not isinstance(results, list):
            return False
        for result in results:
            if not isinstance(result, Mapping):
                continue
            agent_name = str(result.get("agent_name") or "")
            if agent_name in {"poi_search", "route_planning", "itinerary_planning"}:
                return True
            data = result.get("data") if isinstance(result.get("data"), Mapping) else result.get("result")
            if isinstance(data, Mapping) and self._clean_urban_profile(data):
                return True
        return False

    def _filter_event_collection_missing_info(self, missing_info: Any, results: list) -> list:
        values = [str(item).strip() for item in (missing_info or []) if str(item).strip()]
        if not self._results_indicate_micro_trip(results):
            return values
        suppress = {
            "destination",
            "return_location",
            "start_date",
            "end_date",
            "departure_date",
            "arrival_date",
        }
        return [item for item in values if item not in suppress]

    def _event_collection_destination_display(
        self,
        event_data: Mapping[str, Any],
        destination: Any,
        results: list,
    ) -> str:
        destination_text = str(destination or "").strip()
        if not destination_text:
            return "\u5f85\u89c4\u5212"
        nested = event_data.get("data") if isinstance(event_data.get("data"), Mapping) else {}
        city = str(
            event_data.get("city")
            or nested.get("city")
            or event_data.get("destination_city")
            or nested.get("destination_city")
            or ""
        ).strip()
        has_route_planning = self._results_indicate_micro_trip(results)
        urban_profile = self._clean_urban_profile(event_data)
        if not urban_profile:
            for result in results or []:
                if not isinstance(result, Mapping):
                    continue
                data = result.get("data") if isinstance(result.get("data"), Mapping) else result.get("result")
                if isinstance(data, Mapping):
                    urban_profile = self._clean_urban_profile(data)
                    if urban_profile:
                        break
        is_micro_trip = bool(urban_profile) or has_route_planning
        if is_micro_trip and city and destination_text == city:
            return "\u5f85\u89c4\u5212"
        scenario = urban_profile.get("scenario") if isinstance(urban_profile, Mapping) else ""
        if is_micro_trip and destination_text in {"\u5317\u4eac", "\u4e0a\u6d77", "\u5e7f\u5dde", "\u6df1\u5733"} and scenario:
            return "\u5f85\u89c4\u5212"
        return destination_text

    def _event_collection_origin_display(
        self,
        event_data: Mapping[str, Any],
        origin: Any,
        results: list,
    ) -> str:
        origin_text = str(origin or "").strip()
        start_location = self._start_location_from_results(results)
        if isinstance(start_location, Mapping):
            start_name = str(start_location.get("name") or start_location.get("address") or "").strip()
            start_city = str(start_location.get("city") or "").strip()
            if start_name and start_name not in {"\u5317\u4eac", "\u4e0a\u6d77", "\u5e7f\u5dde", "\u6df1\u5733"}:
                return start_name
            if start_name and origin_text and start_name != origin_text:
                return start_name
            if start_city and origin_text == start_city and start_name:
                return start_name
        return origin_text

    def _start_location_from_results(self, results: list) -> Dict[str, Any]:
        for result in results or []:
            if not isinstance(result, Mapping):
                continue
            data = result.get("data") if isinstance(result.get("data"), Mapping) else result.get("result")
            if not isinstance(data, Mapping):
                continue
            for source in (
                data,
                data.get("data") if isinstance(data.get("data"), Mapping) else {},
            ):
                if isinstance(source, Mapping) and isinstance(source.get("start_location"), Mapping):
                    return dict(source.get("start_location") or {})
        return {}

    def _display_clean_trip_brief(
        self,
        payload: Mapping[str, Any],
        route_data: Optional[Mapping[str, Any]],
        primary: Mapping[str, Any],
    ) -> None:
        urban_profile = self._clean_urban_profile(payload, route_data)
        rows = []

        start_location = None
        for source in (primary, payload, route_data):
            if isinstance(source, Mapping) and isinstance(source.get("start_location"), dict):
                start_location = source.get("start_location")
                break
        if start_location:
            start_name = start_location.get("name") or start_location.get("address")
            if start_name:
                rows.append(("出发", str(start_name)))

        time_context = urban_profile.get("time_context") if isinstance(urban_profile.get("time_context"), dict) else {}
        start_text = self._format_iso_clock(time_context.get("inferred_start_time"))
        end_text = self._format_iso_clock(time_context.get("inferred_end_time"))
        if start_text and end_text:
            rows.append(("时段", f"{start_text}-{end_text}"))

        duration = primary.get("estimated_duration_min")
        if duration is None and isinstance(primary.get("metrics"), dict):
            duration = primary.get("metrics", {}).get("estimated_duration_min") or primary.get("metrics", {}).get("total_minutes")
        if duration is not None:
            rows.append(("用时", self._format_duration(duration)))

        distance = primary.get("total_distance_m")
        if distance is None and isinstance(primary.get("metrics"), dict):
            distance = primary.get("metrics", {}).get("total_distance_m")
        if distance is not None:
            rows.append(("距离", self._format_distance(distance)))

        weather_context = {}
        for source in (payload, route_data, urban_profile):
            if isinstance(source, Mapping) and isinstance(source.get("weather_context"), dict):
                weather_context = source.get("weather_context")
                break
        weather_line = self._format_weather_context(weather_context) if weather_context else ""
        if weather_line:
            rows.append(("天气", weather_line.replace("天气: ", "", 1)))

        transport = self._format_transport_summary(primary, urban_profile)
        if transport:
            rows.append(("交通", transport))

        activities = urban_profile.get("activity_sequence") if isinstance(urban_profile.get("activity_sequence"), list) else []
        labels = [
            str(item.get("label") or item.get("type") or "").strip()
            for item in activities
            if isinstance(item, dict) and str(item.get("label") or item.get("type") or "").strip()
        ]
        if labels:
            rows.append(("顺序", " -> ".join(labels)))

        if not rows:
            return
        self._print_result_section("本次识别")
        for label, value in rows:
            self.console.print(f"{label}: {value}", style="dim")

    def _display_clean_route_option(self, option: Mapping[str, Any], index: int, primary: bool = False) -> None:
        metrics = option.get("metrics") if isinstance(option.get("metrics"), dict) else {}
        duration = option.get("estimated_duration_min", metrics.get("estimated_duration_min", metrics.get("total_minutes")))
        distance = option.get("total_distance_m", metrics.get("total_distance_m"))
        header_parts = [f"路线 {index}"]
        metric_parts = []
        if duration is not None:
            metric_parts.append(self._format_duration(duration))
        if distance is not None:
            metric_parts.append(self._format_distance(distance))
        if metric_parts:
            header_parts.append("；".join(metric_parts))
        self.console.print("，".join(header_parts))

        legs = option.get("legs") if isinstance(option.get("legs"), list) else []
        if legs:
            for leg in legs:
                if not isinstance(leg, Mapping):
                    continue
                from_name = str(leg.get("from") or leg.get("from_name") or "").strip()
                to_name = str(leg.get("to") or leg.get("to_name") or "").strip()
                mode = self._mode_label(leg.get("selected_mode") or leg.get("mode"))
                duration_min = leg.get("duration_min")
                distance_m = leg.get("distance_m")
                details = []
                if mode:
                    details.append(mode)
                if duration_min is not None:
                    details.append(self._format_duration(duration_min))
                if distance_m is not None:
                    details.append(self._format_distance(distance_m))
                route_text = " 到 ".join(part for part in (from_name, to_name) if part)
                if route_text:
                    self.console.print(f"  - {route_text}（{'，'.join(details)}）", style="dim")
            for leg in legs:
                if not isinstance(leg, Mapping):
                    continue
                step_summary = self._format_transit_step_summary(leg)
                if step_summary:
                    self.console.print(f"    {step_summary}", style="dim")
            return

        sequence = option.get("poi_sequence")
        if not sequence and isinstance(option.get("pois"), list):
            sequence = [poi.get("name") for poi in option.get("pois") if isinstance(poi, Mapping) and poi.get("name")]
        if isinstance(sequence, list) and sequence:
            self.console.print("  " + " → ".join(str(item) for item in sequence), style="dim")

    def _format_transit_step_summary(self, leg: Mapping[str, Any]) -> str:
        mode = str(leg.get("selected_mode") or leg.get("mode") or "").strip().casefold()
        if mode != "transit":
            return ""
        steps = leg.get("steps") if isinstance(leg.get("steps"), list) else []
        instructions = []
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            instruction = str(step.get("instruction") or "").strip()
            if not instruction:
                line_name = str(step.get("line_name") or step.get("road") or "").strip()
                departure = str(step.get("departure_stop") or "").strip()
                arrival = str(step.get("arrival_stop") or "").strip()
                if line_name and (departure or arrival):
                    instruction = f"乘坐{line_name}，{departure or '上车'} 到 {arrival or '下车'}"
            if instruction and instruction not in instructions:
                instructions.append(instruction)
            if len(instructions) >= 3:
                break
        if not instructions:
            return ""
        return "换乘提示：" + " -> ".join(instructions)

    def _format_transport_summary(self, option: Mapping[str, Any], urban_profile: Mapping[str, Any]) -> str:
        summary = option.get("transport_mode_summary")
        if isinstance(summary, str) and summary.strip():
            return self._mode_label(summary.strip())
        transport_mode = urban_profile.get("transport_mode") if isinstance(urban_profile.get("transport_mode"), dict) else {}
        mode = str(transport_mode.get("mode") or option.get("route_mode") or "").strip()
        if mode:
            return self._mode_label(mode)
        legs = option.get("legs") if isinstance(option.get("legs"), list) else []
        modes = [self._mode_label(leg.get("selected_mode") or leg.get("mode")) for leg in legs if isinstance(leg, Mapping)]
        modes = [item for item in modes if item]
        return " + ".join(self._unique_display_lines(modes))

    @staticmethod
    def _mode_label(mode: Any) -> str:
        labels = {
            "walking": "步行",
            "bicycling": "骑行",
            "electrobike": "电动车",
            "transit": "公共交通",
            "driving": "驾车",
            "multimodal": "步行/骑行/公共交通组合",
            "multimodal_low_friction": "低阻力组合交通",
        }
        key = str(mode or "").strip()
        lowered = key.casefold()
        if lowered in labels:
            return labels[lowered]
        if "_" in lowered or re.search(r"[a-zA-Z]", lowered):
            return "组合交通"
        return key

    @staticmethod
    def _looks_mojibake(value: Any) -> bool:
        text = str(value or "")
        mojibake_markers = (
            "\u9351",
            "\u93c3",
            "\u74ba",
            "\u93ba",
            "\u7f07",
            "\u95ab",
            "\u20ac",
            "\u7efe",
        )
        return any(token in text for token in mojibake_markers)

    def _friendly_warning_lines_clean(self, warnings: list) -> list:
        mapping = {
            "missing_start_location_coordinates": "出发地还不够具体，请改成商圈、地标或地铁站。",
            "start_location_without_coordinates": "出发地暂时无法定位，请换一个更明确的地点。",
            "start_location_geocode_failed": "出发地定位失败，请换一个更常见的地标名称。",
            "required_activity_slot_empty": "有一个必须完成的活动没有找到合适地点，可以换活动类型或放宽区域。",
            "empty_poi_candidates": "没有找到足够合适的地点，可以换商圈或放宽偏好。",
            "route_cost_matrix_failed": "地图路线服务暂时不可用，无法可靠计算真实通勤成本。",
            "amap_route_matrix_failed": "高德路线服务暂时不可用，建议稍后重试。",
            "opening_hours_unknown_used": "部分地点营业时间暂不明确，已降低推荐优先级。",
            "filtered_closed_poi": "已避开明确关闭的地点。",
            "weather_query_failed": "天气查询暂时不可用，已按中性天气处理。",
            "weather_user_real_conflict": "用户描述的天气和实时天气不一致，本次按用户设定场景规划。",
            "no_urban_activity_route_within_time_budget": "最贴近的路线可能略超出目标时长，时间已作为参考处理。",
            "route_exceeds_time_budget": "该路线预计会略超目标时长，已作为参考提醒。",
            "rainy_route_long_walking_leg": "雨天场景中存在较长步行路段，建议优先查看公共交通更顺的备选方案。",
            "non_citywalk_long_walking": "当前不是散步路线，但步行时间偏长，已降低这类方案优先级。",
            "low_route_confidence": "当前路线把握不高，建议换一个出发地、放宽区域或减少一个活动。",
            "connector_slot_empty": "必须活动已有候选点，但缺少适合串联路线的第三个地点，可以放宽区域或补充一个想顺路停留的活动。",
            "insufficient_poi_candidates_for_min_route_size": "合格地点数量不足，暂时无法组成至少 3 个 POI 的路线。",
            "connector_slot_relaxed_with_theme_poi": "中性休息点不足，已用同主题短停留地点补足路线。",
            "citywalk_quality_relaxed_with_supplemental_pois": "散步路线已补入顺路停留点，保证路线完整。",
            "walk_slot_citywalk_quality_fill": "散步活动已用更适合步行串联的地点补足。",
            "walk_slot_citywalk_supplemental_fill": "散步路线已补入短停留地点。",
            "walk_slot_quality_filtered": "已过滤部分不适合散步串联的候选点。",
            "walk_slot_low_quality_candidates_rejected": "已排除部分步行体验较弱的候选点。",
            "activity_slot_low_quality_candidates_rejected": "已排除部分不够合适的候选点。",
            "amap_leg_detail_failed_using_matrix_cost": "部分路段详情暂时不可用，已按路线矩阵用时展示。",
        }
        lines = []
        for item in warnings or []:
            key = str(item or "").strip()
            if not key:
                continue
            normalized = key.split(":", 1)[0].strip()
            if normalized == "activity_slot_quality_filtered":
                slot = key.split(":", 1)[1].strip() if ":" in key else ""
                slot_labels = {
                    "drinks": "小酒馆",
                    "bar": "小酒馆",
                    "exhibition": "展览",
                    "wellness": "按摩放松",
                    "beauty": "美甲",
                    "dining": "餐饮",
                }
                label = slot_labels.get(slot, "活动")
                lines.append(f"已过滤部分不够匹配的{label}候选点。")
                continue
            if normalized in {
                "walk_slot_citywalk_quality_fill",
                "walk_slot_citywalk_supplemental_fill",
                "walk_slot_quality_filtered",
                "walk_slot_low_quality_candidates_rejected",
            }:
                line = mapping.get(normalized)
                if line:
                    lines.append(line)
                    continue
            line = mapping.get(key) or mapping.get(normalized)
            if not line and ("_" in key or re.search(r"[a-zA-Z]", key)):
                line = "部分候选信息已做保守处理，最终路线已按可执行性重新筛选。"
            lines.append(line or key)
        return self._unique_display_lines(lines)

    def _apply_planning_turn_decision_to_intention(self, intention_data: dict, decision: Mapping[str, Any]) -> dict:
        """Merge route-turn carry-over and requested changes into structured intent."""
        if not isinstance(intention_data, dict) or not isinstance(decision, Mapping):
            return intention_data

        action = str(decision.get("action") or "")
        if action not in {"revise_previous_plan", "expand_previous_plan"}:
            return intention_data

        carry_over = decision.get("carry_over") if isinstance(decision.get("carry_over"), Mapping) else {}
        changes = decision.get("changes") if isinstance(decision.get("changes"), Mapping) else {}

        key_entities = intention_data.get("key_entities")
        if not isinstance(key_entities, dict):
            key_entities = {}
        city = carry_over.get("city") or carry_over.get("destination")
        if city and not key_entities.get("destination"):
            key_entities["destination"] = city
        if city and not key_entities.get("city"):
            key_entities["city"] = city
        start_location = carry_over.get("start_location")
        if start_location and not key_entities.get("start_location"):
            key_entities["start_location"] = start_location
            key_entities.setdefault("origin", start_location)
            intention_data.setdefault("start_location", start_location)
        intention_data["key_entities"] = key_entities

        urban_profile = intention_data.get("urban_intent_profile")
        if not isinstance(urban_profile, dict):
            urban_profile = {}
        time_context = urban_profile.get("time_context")
        if not isinstance(time_context, dict):
            time_context = {}
        duration_min = carry_over.get("duration_min")
        if duration_min and not time_context.get("duration_min"):
            time_context["duration_min"] = duration_min
        urban_profile["time_context"] = time_context
        if carry_over.get("transport_mode") and not urban_profile.get("transport_mode"):
            urban_profile["transport_mode"] = carry_over.get("transport_mode")

        activities = urban_profile.get("activity_sequence")
        if not isinstance(activities, list):
            activities = []
        extra_slots = changes.get("add_activity_slots") if isinstance(changes.get("add_activity_slots"), list) else []
        if extra_slots:
            existing_keys = {
                str(item.get("activity_type") or item.get("type") or item.get("activity_label") or item.get("label") or "")
                for item in activities
                if isinstance(item, Mapping)
            }
            next_order = max(
                [int(item.get("order", 0) or 0) for item in activities if isinstance(item, Mapping)]
                or [0]
            ) + 1
            for slot in extra_slots:
                if not isinstance(slot, Mapping):
                    continue
                slot_key = str(slot.get("activity_type") or slot.get("type") or slot.get("activity_label") or slot.get("label") or "")
                if slot_key and slot_key in existing_keys:
                    continue
                merged_slot = dict(slot)
                merged_slot["order"] = next_order
                merged_slot.setdefault("required", False)
                activities.append(merged_slot)
                existing_keys.add(slot_key)
                next_order += 1
        dining_preference = changes.get("dining_preference") if isinstance(changes.get("dining_preference"), Mapping) else {}
        if dining_preference:
            cuisine = str(dining_preference.get("cuisine") or "").strip()
            cuisine_keywords = [
                str(item).strip()
                for item in (
                    dining_preference.get("keywords")
                    if isinstance(dining_preference.get("keywords"), list)
                    else []
                )
                if str(item).strip()
            ]
            recall_phrases = [
                str(item).strip()
                for item in (
                    dining_preference.get("recall_phrases")
                    if isinstance(dining_preference.get("recall_phrases"), list)
                    else cuisine_keywords
                )
                if str(item).strip()
            ]
            semantic_tags = [
                str(item).strip()
                for item in (
                    dining_preference.get("semantic_tags")
                    if isinstance(dining_preference.get("semantic_tags"), list)
                    else []
                )
                if str(item).strip()
            ]
            if cuisine:
                semantic_tags = [f"cuisine:{cuisine}", cuisine, *semantic_tags]
                cuisine_keywords = cuisine_keywords or [f"{cuisine} 餐厅"]
                recall_phrases = recall_phrases or cuisine_keywords

            def unique(values: list) -> list:
                result = []
                seen = set()
                for value in values:
                    text = str(value or "").strip()
                    if text and text not in seen:
                        result.append(text)
                        seen.add(text)
                return result

            def values_list(value: Any) -> list:
                if isinstance(value, list):
                    return value
                if isinstance(value, tuple):
                    return list(value)
                if isinstance(value, str):
                    return [value] if value.strip() else []
                return []

            route_preference = intention_data.get("route_preference")
            if not isinstance(route_preference, dict):
                route_preference = {}
            route_preference["food_cuisine"] = cuisine
            route_preference["semantic_tags"] = unique(
                [*semantic_tags, *values_list(route_preference.get("semantic_tags"))]
            )[:10]
            route_preference["recall_phrases"] = unique(
                [*recall_phrases, *values_list(route_preference.get("recall_phrases"))]
            )[:10]
            intention_data["route_preference"] = route_preference

            local_food_terms = {"本地特色", "本地小吃", "老字号", "北京菜", "老北京", "京味", "炸酱面", "烤鸭"}
            dining_found = False
            for activity in activities:
                if not isinstance(activity, dict):
                    continue
                activity_type = str(activity.get("activity_type") or activity.get("type") or "")
                activity_label = str(activity.get("activity_label") or activity.get("label") or "")
                if activity_type not in {"dining", "late_night_food", "social_dining"} and "餐" not in activity_label and "吃" not in activity_label:
                    continue
                dining_found = True
                old_keywords = [
                    str(item).strip()
                    for item in (activity.get("poi_keywords") if isinstance(activity.get("poi_keywords"), list) else [])
                    if str(item).strip()
                ]
                old_keywords = [
                    keyword
                    for keyword in old_keywords
                    if not any(term in keyword for term in local_food_terms)
                ]
                activity["poi_keywords"] = unique([*cuisine_keywords, *old_keywords])[:8]
                activity["activity_label"] = f"{cuisine}餐厅" if cuisine else activity_label or "餐饮"
                activity["label"] = activity["activity_label"]
                soft_preferences = activity.get("soft_preferences")
                if not isinstance(soft_preferences, dict):
                    soft_preferences = {}
                if cuisine:
                    soft_preferences["cuisine"] = cuisine
                activity["soft_preferences"] = soft_preferences

            if not dining_found and cuisine_keywords:
                next_order = max(
                    [int(item.get("order", 0) or 0) for item in activities if isinstance(item, Mapping)]
                    or [0]
                ) + 1
                activities.append(
                    {
                        "slot_id": f"dining_{next_order}",
                        "activity_type": "dining",
                        "activity_label": f"{cuisine}餐厅" if cuisine else "餐饮",
                        "activity_group": "food",
                        "poi_category": "dining",
                        "order": next_order,
                        "required": True,
                        "duration_min": 60,
                        "poi_keywords": cuisine_keywords,
                        "opening_hours_need": "open_now",
                        "weather_fit": "indoor_or_sheltered",
                        "soft_preferences": {"cuisine": cuisine} if cuisine else {},
                    }
                )
        urban_profile["activity_sequence"] = activities
        intention_data["urban_intent_profile"] = urban_profile

        rewritten = str(decision.get("rewritten_query_for_planning") or "").strip()
        if rewritten:
            intention_data["rewritten_query"] = rewritten
        return intention_data

    def _apply_preset_route_preference_constraints(
        self,
        intention_data: dict,
        preset_route_type: Optional[str],
        user_input: str,
    ) -> dict:
        """Keep explicit route preference choices from being diluted by LLM extras."""
        route_type = str(preset_route_type or "auto").strip().casefold()
        if route_type != "sightseeing":
            return intention_data
        query_text = str(user_input or "")
        explicit_food = any(
            term in query_text
            for term in (
                "\u5403",
                "\u9910",
                "\u996d",
                "\u7f8e\u98df",
                "\u5c0f\u5403",
                "\u591c\u5bb5",
                "\u751c\u54c1",
                "\u559d",
                "food",
                "dining",
                "meal",
                "dessert",
            )
        )
        if explicit_food:
            return intention_data
        urban_profile = intention_data.get("urban_intent_profile")
        if not isinstance(urban_profile, dict):
            return intention_data
        activities = urban_profile.get("activity_sequence")
        if not isinstance(activities, list):
            return intention_data
        food_terms = (
            "dining",
            "dinner",
            "food",
            "meal",
            "restaurant",
            "snack",
            "dessert",
            "late_night_food",
            "\u5403",
            "\u9910",
            "\u996d",
            "\u7f8e\u98df",
            "\u5c0f\u5403",
            "\u751c\u54c1",
        )
        kept = []
        for activity in activities:
            if not isinstance(activity, Mapping):
                continue
            activity_text = " ".join(
                str(activity.get(key) or "")
                for key in (
                    "type",
                    "activity_type",
                    "label",
                    "activity_label",
                    "activity_group",
                    "poi_category",
                    "poi_keywords",
                )
            ).casefold()
            if any(term in activity_text for term in food_terms):
                continue
            kept.append(dict(activity))
        if kept:
            for index, activity in enumerate(kept, start=1):
                activity["order"] = index
            urban_profile["activity_sequence"] = kept
            intention_data["urban_intent_profile"] = urban_profile
        return intention_data

    def _build_memory_preference_context(self, user_input: str, recent_context: list) -> dict:
        """Build compact structured context for multi-turn route intent recognition."""
        memory_manager = getattr(self, "memory_manager", None)
        preferences = {}
        if memory_manager:
            try:
                preferences = memory_manager.long_term.get_preference()
            except Exception:
                preferences = {}
        if not isinstance(preferences, dict):
            preferences = {}

        recent_messages = []
        for msg in (recent_context or [])[-6:]:
            if not isinstance(msg, dict):
                continue
            content = str(msg.get("content") or "").replace("\n", " ").strip()
            if len(content) > 320:
                content = content[:320] + "..."
            recent_messages.append(
                {
                    "role": str(msg.get("role") or ""),
                    "content": content,
                    "timestamp": msg.get("timestamp"),
                }
            )

        context = {
            "schema_version": "memory_preference_context.v1",
            "current_query": str(user_input or ""),
            "user_preferences": {
                key: value for key, value in preferences.items() if value not in (None, "", [])
            },
            "recent_messages": recent_messages,
            "previous_route_turn": self._extract_previous_route_turn(recent_context or []),
            "usage_policy": {
                "current_user_query_overrides_memory": True,
                "use_previous_route_turn_for_ellipsis_or_revision": True,
                "preferences_are_soft_poi_recall_signals": True,
                "do_not_turn_preferences_into_route_hard_constraints": True,
            },
        }
        return context

    def _extract_previous_route_turn(self, recent_context: list) -> dict:
        previous_user = ""
        previous_assistant = None
        for msg in reversed(recent_context or []):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "")
            content = str(msg.get("content") or "")
            if role == "assistant" and previous_assistant is None:
                try:
                    parsed = json.loads(content)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    previous_assistant = parsed
            elif role == "user" and not previous_user:
                previous_user = content
            if previous_user and previous_assistant is not None:
                break

        summary = {"user_query": previous_user}
        if not isinstance(previous_assistant, dict):
            return summary

        intention = previous_assistant.get("intention") if isinstance(previous_assistant.get("intention"), dict) else {}
        route_preference = intention.get("route_preference") if isinstance(intention.get("route_preference"), dict) else {}
        urban_profile = intention.get("urban_intent_profile") if isinstance(intention.get("urban_intent_profile"), dict) else {}
        key_entities = intention.get("key_entities") if isinstance(intention.get("key_entities"), dict) else {}
        summary.update(
            {
                "destination": key_entities.get("destination") or key_entities.get("city"),
                "start_location": key_entities.get("start_location") or key_entities.get("origin"),
                "route_preference": {
                    "route_type": route_preference.get("route_type"),
                    "semantic_tags": route_preference.get("semantic_tags"),
                    "recall_phrases": route_preference.get("recall_phrases"),
                },
                "scenario": urban_profile.get("scenario"),
                "transport_mode": urban_profile.get("transport_mode"),
                "activity_sequence": self._compact_activity_sequence_for_memory(
                    urban_profile.get("activity_sequence") if isinstance(urban_profile, dict) else []
                ),
            }
        )

        route_data = self._find_previous_route_data(previous_assistant)
        if route_data:
            first_route = (route_data.get("route_options") or [{}])[0] if isinstance(route_data.get("route_options"), list) else {}
            summary["previous_route"] = {
                "route_count": len(route_data.get("route_options") or []),
                "first_sequence": first_route.get("poi_sequence") or [],
                "duration_min": first_route.get("estimated_duration_min"),
                "distance_m": first_route.get("total_distance_m"),
                "start_location": first_route.get("start_location") or route_data.get("start_location"),
            }
        return summary

    @staticmethod
    def _compact_activity_sequence_for_memory(activities: Any) -> list:
        if not isinstance(activities, list):
            return []
        compact = []
        for item in activities[:6]:
            if not isinstance(item, dict):
                continue
            compact.append(
                {
                    "order": item.get("order"),
                    "activity_type": item.get("activity_type") or item.get("type"),
                    "activity_label": item.get("activity_label") or item.get("label"),
                    "poi_keywords": item.get("poi_keywords"),
                }
            )
        return compact

    @staticmethod
    def _find_previous_route_data(payload: dict) -> dict:
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
        for item in results:
            if not isinstance(item, dict) or item.get("agent_name") != "route_planning":
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            data = result.get("data") if isinstance(result.get("data"), dict) else result
            if isinstance(data, dict):
                return data
        return {}

    async def _get_long_term_summary(self, user_input: str = "") -> str:
        """
        生成长期记忆摘要，用于传递给IntentionAgent
        使用LLM总结历史聊天记录 + 结构化偏好

        Args:
            user_input: 用户输入，用于筛选相关历史行程

        Returns:
            格式化的长期记忆摘要
        """
        rc = RESILIENCE_CONFIG
        return await self.memory_manager.get_required_long_term_context_async(
            user_input=user_input,
            max_messages=rc.get("memory_summary_max_messages", 20),
            timeout_sec=rc.get("memory_summary_timeout_sec", 3.0),
            max_trips=rc.get("memory_context_trip_records", 3),
            max_chat_messages=rc.get("memory_context_chat_messages", 4),
        )

        summary_parts = []

        # 1. 用户偏好信息（始终加载）
        prefs = self.memory_manager.long_term.get_preference()
        if prefs:
            pref_lines = ["【用户背景信息】（来自长期记忆，可用于推断缺失信息）"]

            # 遍历所有偏好，全部加载
            for pref_key, pref_value in prefs.items():
                if pref_value:  # 只添加有值的偏好
                    # 如果是列表，用逗号连接
                    if isinstance(pref_value, list):
                        pref_lines.append(f"• {pref_key}: {', '.join(pref_value)}")
                    else:
                        pref_lines.append(f"• {pref_key}: {pref_value}")

            # 只有在有具体偏好内容时才添加
            if len(pref_lines) > 1:
                summary_parts.extend(pref_lines)

        # 2. 使用LLM总结历史聊天记录
        chat_summary = await self.memory_manager.get_long_term_summary_async(max_messages=50)
        if chat_summary:
            summary_parts.append("\n【历史会话总结】")
            summary_parts.append(chat_summary)

        # 3. 智能筛选相关历史行程
        all_trips = self.memory_manager.long_term.get_trip_history(limit=None)
        if all_trips:
            # 筛选相关的行程（地点匹配）
            relevant_trips = []
            other_trips = []

            for trip in all_trips:
                origin = trip.get("origin", "") or ""
                destination = trip.get("destination", "") or ""

                # 如果用户输入提到了这个行程的地点，标记为相关
                if (origin and origin in user_input) or (destination and destination in user_input):
                    relevant_trips.append(trip)
                else:
                    other_trips.append(trip)

            # 优先显示相关的，再补充最近的
            trips_to_show = relevant_trips[:2] + other_trips[:1]  # 2条相关 + 1条最近

            if trips_to_show:
                summary_parts.append("\n【历史行程】")
                for i, trip in enumerate(trips_to_show[:3], 1):
                    origin = trip.get("origin", "未知")
                    destination = trip.get("destination", "未知")
                    start_date = trip.get("start_date", "")
                    purpose = trip.get("purpose", "")

                    # 标记相关性
                    relevance_mark = "✦ " if trip in relevant_trips else ""
                    summary_parts.append(
                        f"{i}. {relevance_mark}{origin} → {destination} ({start_date}) - {purpose}"
                    )

        return "\n".join(summary_parts) if summary_parts else ""

    def _generate_human_response(self, results: list) -> bool:
        """
        根据结果生成人性化的回复
        """
        has_output = False
        has_itinerary_result = self._has_displayable_itinerary(results)

        for result in results:
            agent_name = result.get("agent_name", "")
            status = result.get("status", "")
            data = result.get("data", {})
            current_agent_shown = False  # 标记当前Agent是否有内容展示

            # 处理失败的智能体
            if status == "error":
                error_msg = self._friendly_failure_message(str(data.get("error_type") or ""), data.get("error", "未知错误"))
                agent_display_name = self._get_agent_display_name(agent_name)
                self.console.print(f"{agent_display_name}没有完成: {error_msg}", style="red")
                has_output = True
                continue

            # 只处理成功的智能体 (RAG 的 no_knowledge 视为一种特殊的成功/提示)
            if status != "success" and not (agent_name == "rag_knowledge" and status == "no_knowledge"):
                continue

            # --- 特定 Agent 处理 ---

            # 行程规划
            if agent_name == "itinerary_planning":
                itinerary = data.get("itinerary")
                # 增强：支持从 data.data.itinerary 获取
                if not itinerary and "data" in data and isinstance(data["data"], dict):
                    itinerary = data["data"].get("itinerary")
                
                if itinerary:
                    title = itinerary.get('title', '行程规划')
                    fallback_minutes, fallback_distance_m = self._itinerary_primary_route_metrics(itinerary)
                    itinerary_duration = str(itinerary.get("duration", "") or "").strip()
                    duration_display = itinerary_duration if itinerary_duration else "未知"
                    if self._is_unknown_duration_text(duration_display) and fallback_minutes is not None:
                        duration_display = self._format_duration(fallback_minutes)
                    display_title = self._display_itinerary_title(title)
                    self.console.print(f"\n[bold cyan]轻途：{display_title}[/bold cyan]")
                    self.console.print("把你的需求整理成下面这条可出发的小路线。", style="dim")

                    start_location = itinerary.get("start_location") if isinstance(itinerary.get("start_location"), dict) else None
                    self._display_trip_brief(itinerary, duration_display, start_location)
                    if start_location:
                        start_name = start_location.get("name") or start_location.get("address")
                        if start_name:
                            self.console.print(f"出发地: {start_name}", style="dim")

                    for day_plan in itinerary.get("daily_plans", []):
                        section_title = str(day_plan.get("section_title", "") or "").strip()
                        self._print_result_section(section_title or "推荐路线")

                        # 兼容 activities 和 time_slots
                        activities = day_plan.get("activities") or day_plan.get("time_slots") or []
                        for index, slot in enumerate(activities, 1):
                            time = slot.get("time", "")
                            # 兼容 activity 和 location
                            activity = slot.get("activity") or slot.get("location") or ""
                            description = slot.get("description", "")
                            transport = slot.get("transport", "")

                            prefix = f"{index}. "
                            time_text = f"{time}  " if time else ""
                            self.console.print(f"{prefix}{time_text}{activity}")
                            if description:
                                self.console.print(f"   推荐理由: {description}", style="dim")
                            if transport:
                                self.console.print(f"   交通建议: {transport}", style="dim")

                        # 餐食建议
                        meals = day_plan.get("meals", {})
                        if meals:
                            self.console.print()
                            if meals.get("lunch"):
                                self.console.print(f"午餐建议: {meals['lunch']}", style="dim")
                            if meals.get("dinner"):
                                self.console.print(f"晚餐建议: {meals['dinner']}", style="dim")
                        self.console.print()

                    # 注意事项
                    notes = itinerary.get("notes", [])
                    if notes and fallback_distance_m is not None and fallback_distance_m > 0:
                        normalized_notes = []
                        for note in notes:
                            text = str(note)
                            if "总距离约 0 米" in text:
                                text = text.replace("总距离约 0 米", f"总距离约 {int(round(fallback_distance_m))} 米")
                            normalized_notes.append(text)
                        notes = normalized_notes
                    if notes:
                        self._print_result_section("出行提醒")
                        for note in self._friendly_note_lines(notes):
                            self.console.print(f"- {note}")

                    route_options = itinerary.get("route_options", [])
                    if route_options:
                        if self._is_unknown_duration_text(duration_display):
                            if fallback_minutes is not None:
                                self.console.print(f"[dim]预计用时: {self._format_duration(fallback_minutes)}[/dim]")
                        self._print_result_section("备选路线")
                        for index, option in enumerate(route_options[:3], start=1):
                            metrics = option.get("metrics", {}) if isinstance(option.get("metrics"), dict) else {}
                            sequence = " -> ".join(option.get("poi_sequence", []))
                            total_minutes_raw = metrics.get("total_minutes")
                            if total_minutes_raw is None:
                                total_minutes_raw = metrics.get("estimated_duration_min")
                            if total_minutes_raw is None:
                                total_minutes_raw = option.get("estimated_duration_min")
                            total_minutes = self._format_duration(total_minutes_raw)
                            queue_risk = self._format_queue_wait(metrics.get("avg_queue_risk"))
                            if metrics.get("estimated_cost") is None:
                                metrics["estimated_cost"] = option.get("estimated_cost")
                            cost = metrics.get("estimated_cost", "未知")
                            self.console.print(f"- 路线 {index}: {sequence}")
                            self.console.print(
                                f"  用时 {total_minutes}，{queue_risk}，预算约 {cost} 元",
                                style="dim"
                            )
                    current_agent_shown = True

            # 路线规划（当 itinerary_planning 未执行或未返回可读行程时，展示结构化路线摘要）
            elif agent_name == "route_planning":
                route_options = data.get("route_options", [])
                warnings = data.get("warnings", []) or []
                policy = data.get("composition_policy", {}) if isinstance(data.get("composition_policy"), dict) else {}
                policy_type = policy.get("policy_type", "")

                if route_options:
                    self.console.print("\n[bold cyan]轻途：候选路线已整理[/bold cyan]")
                    self.console.print(f"共找到 {len(route_options)} 条可选路线。", style="dim")

                    first = route_options[0] if isinstance(route_options[0], dict) else {}
                    seq = first.get("poi_sequence", [])
                    metrics = first.get("metrics", {}) if isinstance(first.get("metrics"), dict) else {}
                    est_min = first.get("estimated_duration_min", metrics.get("estimated_duration_min"))
                    dist_m = first.get("total_distance_m", metrics.get("total_distance_m"))
                    sequence_text = " -> ".join(seq) if isinstance(seq, list) else str(seq or "")
                    start_location = first.get("start_location") if isinstance(first.get("start_location"), dict) else data.get("start_location")
                    if isinstance(start_location, dict):
                        start_name = start_location.get("name") or start_location.get("address")
                        if start_name:
                            self.console.print(f"出发地: {start_name}", style="dim")

                    self._display_trip_brief(data, self._format_duration(est_min), start_location)
                    if sequence_text:
                        self._print_result_section("路线 1")
                        self.console.print(sequence_text)
                    if est_min is not None or dist_m is not None:
                        self.console.print(
                            f"预计用时 {self._format_duration(est_min)}，距离 {self._format_distance(dist_m)}",
                            style="dim",
                        )
                    if warnings:
                        self._print_result_section("出行提醒")
                        for line in self._friendly_warning_lines(warnings):
                            self.console.print(f"- {line}", style="yellow")
                    current_agent_shown = True
                elif warnings:
                    self.console.print("\n[bold yellow]这次没有找到合适路线[/bold yellow]")
                    for line in self._friendly_warning_lines(warnings):
                        self.console.print(f"- {line}", style="yellow")
                    current_agent_shown = True

            # 偏好管理
            elif agent_name == "preference":
                raw_prefs = data.get("preferences")
                # 增强：支持从 data.data.preferences 获取
                if not raw_prefs and "data" in data and isinstance(data["data"], dict):
                    raw_prefs = data["data"].get("preferences")

                if isinstance(raw_prefs, dict):
                    prefs_list = raw_prefs.get("preferences", [])
                else:
                    prefs_list = raw_prefs if isinstance(raw_prefs, list) else []

                if prefs_list:
                    self.console.print("[bold green]已更新您的偏好设置[/bold green]")
                    type_names = {
                        "home_location": "常驻地",
                        "transportation_preference": "交通偏好",
                        "hotel_brands": "酒店偏好",
                        "airlines": "航空公司偏好",
                        "seat_preference": "座位偏好",
                        "meal_preference": "餐食偏好",
                        "budget_level": "预算等级"
                    }
                    for pref in prefs_list:
                        pref_type = pref.get("type", "")
                        pref_value = pref.get("value", "")
                        action = pref.get("action", "replace")
                        display_type = type_names.get(pref_type, pref_type)
                        action_text = "追加" if action == "append" else "设置为"
                        self.console.print(f"  • {display_type} {action_text} [cyan]{pref_value}[/cyan]")
                    current_agent_shown = True
                    has_itinerary = any(r.get("agent_name") == "itinerary_planning" for r in results)
                    if not has_itinerary:
                        self.console.print("\n提示: 下次规划行程时会参考这些偏好。", style="dim")
                else:
                    # 检查是否有错误信息
                    err = data.get("error", "")
                    if err:
                        self.console.print(f"偏好未保存: {err}", style="yellow")
                        current_agent_shown = True
                    # 如果只是没提取到，可能就是没偏好，不强求显示，交给兜底逻辑

            # 事项收集
            elif agent_name == "event_collection":
                # 增强：支持从 data.data 获取
                origin = data.get("origin") or data.get("data", {}).get("origin")
                destination = data.get("destination") or data.get("data", {}).get("destination")
                start_date = data.get("start_date") or data.get("data", {}).get("start_date")
                end_date = data.get("end_date") or data.get("data", {}).get("end_date")
                missing_info = data.get("missing_info") or data.get("data", {}).get("missing_info") or []
                origin_display = self._event_collection_origin_display(data, origin, results)
                destination_display = self._event_collection_destination_display(data, destination, results)
                missing_info = self._filter_event_collection_missing_info(missing_info, results)

                has_itinerary = any(r.get("agent_name") == "itinerary_planning" for r in results)
                info_shown = False
                if not has_itinerary:
                    if destination_display or origin_display:
                        self.console.print("[bold green]已收集行程信息[/bold green]")
                        if origin_display: self.console.print(f"  • 出发地: [cyan]{origin_display}[/cyan]")
                        if destination_display: self.console.print(f"  • 目的地: [cyan]{destination_display}[/cyan]")
                        if start_date: self.console.print(f"  • 出发日期: [cyan]{start_date}[/cyan]")
                        if end_date: self.console.print(f"  • 返程日期: [cyan]{end_date}[/cyan]")
                        info_shown = True

                if missing_info:
                    self.console.print(f"\n还需要补充: {', '.join(missing_info)}", style="yellow")
                    info_shown = True
                
                if info_shown:
                    current_agent_shown = True

            # 信息查询
            elif agent_name == "information_query":
                query_results = data.get("results")
                if not query_results and "data" in data and isinstance(data["data"], dict):
                    query_results = data["data"].get("results")
                if not query_results:
                    query_results = data # 兜底：data 本身就是 results

                if not isinstance(query_results, dict):
                    query_results = {}

                summary = query_results.get("summary", "")
                sources = query_results.get("sources", []) or []
                message = query_results.get("message", "")
                error = query_results.get("error", "")

                if summary:
                    self.console.print(f"\n{summary}")
                    current_agent_shown = True
                elif message:
                    self.console.print(f"\n{message}", style="dim")
                    current_agent_shown = True
                elif error:
                    self.console.print(f"\n{error}", style="yellow")
                    current_agent_shown = True

                if sources:
                    self.console.print("\n[bold]参考来源[/bold]")
                    for i, source in enumerate(sources[:3], 1):
                        url = source.get("url", "") if isinstance(source, dict) else str(source)
                        self.console.print(f"  {i}. {url}", style="dim")
                    current_agent_shown = True

            # RAG知识库查询
            elif agent_name == "rag_knowledge":
                answer = data.get("answer")
                if not answer and "data" in data and isinstance(data["data"], dict):
                    answer = data["data"].get("answer")
                
                # 增强：也查找 content
                if not answer:
                    answer = data.get("content") or data.get("data", {}).get("content")

                # 深度清洗
                if isinstance(answer, dict):
                    answer = answer.get("answer", str(answer))
                
                if isinstance(answer, str) and answer.strip().startswith("{") and answer.strip().endswith("}"):
                    try:
                        import json
                        json_obj = json.loads(answer)
                        if isinstance(json_obj, dict) and "answer" in json_obj:
                            answer = json_obj["answer"]
                    except:
                        pass

                if answer:
                    self.console.print(f"\n{answer}")
                    current_agent_shown = True

            # 记忆查询
            elif agent_name == "memory_query":
                if has_itinerary_result:
                    continue

                query_result = data.get("answer") or data.get("result") or data.get("content")
                if not query_result and "data" in data and isinstance(data["data"], dict):
                    inner = data["data"]
                    query_result = inner.get("answer") or inner.get("result") or inner.get("content")

                if query_result:
                    self.console.print(f"\n{query_result}")
                    current_agent_shown = True

            # --- 通用兜底 (如果特定逻辑未生效) ---
            if not current_agent_shown:
                # 尝试查找通用字段
                common_keys = ["answer", "content", "result", "message", "summary", "text", "description"]
                fallback_content = ""
                
                # 扁平查找
                for k in common_keys:
                    if k in data and isinstance(data[k], str) and data[k].strip():
                        fallback_content = data[k]
                        break
                
                # 嵌套查找 data.data
                if not fallback_content and "data" in data and isinstance(data["data"], dict):
                    for k in common_keys:
                        if k in data["data"] and isinstance(data["data"][k], str) and data["data"][k].strip():
                            fallback_content = data["data"][k]
                            break

                if fallback_content:
                    self.console.print(f"\n{fallback_content}")
                    current_agent_shown = True
                else:
                    # 实在啥也没有，打印个成功标记，避免完全静默
                    agent_display_name = self._get_agent_display_name(agent_name)
                    self.console.print(f"{agent_display_name}已完成", style="green")
                    current_agent_shown = True

            if current_agent_shown:
                has_output = True

        return has_output

    def _has_displayable_itinerary(self, results: list) -> bool:
        for result in results:
            if result.get("status") != "success":
                continue

            agent_name = result.get("agent_name", "")
            if agent_name not in ("itinerary_planning", "plan-trip", "plan_trip"):
                continue

            data = result.get("data", {}) or {}
            if data.get("itinerary"):
                return True

            inner = data.get("data")
            if isinstance(inner, dict) and inner.get("itinerary"):
                return True

        return False

    def _display_itinerary_title(self, raw_title: str) -> str:
        title = str(raw_title or "").strip()
        title = title.replace("LightRoute", "").replace("lightroute", "").replace("轻途", "")
        title = re.sub(r"\s*[|｜]\s*", " ", title).strip(" ：:-")
        lowered = title.casefold().replace("-", "_").replace(" ", "_")
        title_map = {
            "romantic_date": "情侣雨天展览小酒路线",
            "partner_rainy_date": "情侣雨天展览小酒路线",
            "rainy_day_date": "情侣雨天展览小酒路线",
            "exhibition_drinks": "展览小酒路线",
            "after_work_relax_late_food": "下班按摩夜宵路线",
            "besties_beauty_drinks": "闺蜜美甲小酒路线",
            "easy_citywalk": "轻松散步路线",
            "citywalk_easy": "轻松散步路线",
        }
        if lowered in title_map:
            return title_map[lowered]
        if "_" in lowered or re.fullmatch(r"[a-z0-9_]+", lowered):
            return "城市小路线"
        if not title or title in {"行程规划", "路线规划"}:
            return "城市小路线"
        return title

    def _print_result_section(self, title: str) -> None:
        clean_title = str(title or "本次路线").strip()
        self.console.print(f"\n[bold]{clean_title}[/bold]")
        self.console.print("-" * max(8, min(28, len(clean_title) * 2)), style="dim")

    def _display_trip_brief(self, payload: dict, duration_display: str, start_location: Optional[dict] = None) -> None:
        if not isinstance(payload, dict):
            return

        urban_profile = payload.get("urban_intent_profile") if isinstance(payload.get("urban_intent_profile"), dict) else {}
        weather_context = payload.get("weather_context") if isinstance(payload.get("weather_context"), dict) else {}
        if not weather_context and isinstance(urban_profile.get("weather_context"), dict):
            weather_context = urban_profile.get("weather_context")

        rows = []
        scenario = str(urban_profile.get("scenario") or "").strip()
        if scenario:
            rows.append(("场景", self._urban_scenario_label(scenario)))

        if start_location:
            start_name = start_location.get("name") or start_location.get("address")
            if start_name:
                rows.append(("出发", str(start_name)))

        if duration_display and not self._is_unknown_duration_text(duration_display):
            rows.append(("时间", duration_display))

        time_context = urban_profile.get("time_context") if isinstance(urban_profile.get("time_context"), dict) else {}
        start_text = self._format_iso_clock(time_context.get("inferred_start_time"))
        end_text = self._format_iso_clock(time_context.get("inferred_end_time"))
        if start_text and end_text:
            rows.append(("时段", f"{start_text}-{end_text}"))

        weather_line = self._format_weather_context(weather_context) if isinstance(weather_context, dict) else ""
        if weather_line:
            rows.append(("天气", weather_line.replace("天气: ", "", 1)))

        constraints = urban_profile.get("route_constraints") if isinstance(urban_profile.get("route_constraints"), dict) else {}
        if constraints.get("require_opening_hours_check"):
            rows.append(("营业", "已避开明确关闭的地点，营业时间未知的地点会降低优先级"))

        activities = urban_profile.get("activity_sequence") if isinstance(urban_profile.get("activity_sequence"), list) else []
        labels = []
        for item in activities:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("type") or "").strip()
            if label:
                labels.append(label)
        if labels:
            rows.append(("顺序", " -> ".join(labels)))

        if not rows:
            return

        self._print_result_section("本次识别")
        for label, value in rows:
            self.console.print(f"{label}: {value}", style="dim")

    def _friendly_note_lines(self, notes: list) -> list:
        lines = []
        for note in notes or []:
            text = str(note or "").strip()
            if not text:
                continue
            if "_" in text or "amap" in text.lower() or "warning" in text.lower():
                lines.extend(self._friendly_warning_lines(re.split(r"[:：,，、\s]+", text)))
            else:
                lines.append(text)
        return self._unique_display_lines(lines)

    def _friendly_warning_lines(self, warnings: list) -> list:
        mapping = {
            "missing_destination_city": "还缺少目的地城市，可以补充城市名。",
            "missing_start_location_coordinates": "出发地还不够具体，可以改成商圈、地标或地铁站。",
            "start_location_without_coordinates": "出发地暂时无法定位，可以换一个更明确的地点。",
            "start_location_geocode_failed": "出发地定位失败，可以换一个更常见的地标名称。",
            "required_activity_slot_empty": "有一个必须完成的活动没有找到合适地点，可以放宽活动要求。",
            "empty_poi_candidates": "没有找到足够合适的地点，可以换一个商圈或放宽偏好。",
            "route_cost_matrix_failed": "路线用时计算暂时不可用，建议稍后重试或切换交通方式。",
            "amap_route_matrix_failed": "地图路线服务暂时不可用，建议稍后重试。",
            "amap_leg_detail_failed_using_matrix_cost": "部分路段详情暂时不可用，已按估算用时继续展示。",
            "opening_hours_unknown_used": "部分地点营业时间暂不明确，已降低推荐优先级。",
            "filtered_closed_poi": "已避开明确关闭的地点。",
            "weather_query_failed": "天气查询暂时不可用，已按中性天气继续整理路线。",
            "weather_user_real_conflict": "你描述的天气和实时天气可能不一致，路线已按更保守的方式处理。",
            "rainy_route_long_walking_leg": "雨天场景中存在较长步行路段，建议优先查看公共交通更顺的备选方案。",
            "non_citywalk_long_walking": "当前不是散步路线，但步行时间偏长，已降低这类方案优先级。",
            "low_route_confidence": "当前路线把握不高，建议换一个出发地、放宽区域或减少一个活动。",
            "connector_slot_empty": "必须活动已有候选点，但缺少适合串联路线的第三个地点，可以放宽区域或补充一个想顺路停留的活动。",
            "insufficient_poi_candidates_for_min_route_size": "合格地点数量不足，暂时无法组成至少 3 个 POI 的路线。",
            "connector_slot_relaxed_with_theme_poi": "中性休息点不足，已用同主题短停留地点补足路线。",
            "missing_dining_pois": "餐饮选择不足，可以放宽口味或距离要求。",
            "missing_culture_entertainment_pois": "文化休闲类选择不足，可以放宽活动类型或距离要求。",
            "default_start_location_tiananmen": "系统使用了默认出发地，建议补充更准确的位置。",
        }

        lines = []
        for item in warnings or []:
            key = str(item or "").strip()
            if not key or key in {"约束提醒", "warnings", "warning"}:
                continue
            normalized = key.split(":", 1)[0].strip()
            line = mapping.get(key) or mapping.get(normalized)
            if not line:
                lowered = key.lower()
                if "amap_request_failed" in lowered or "poi_search_failed" in lowered:
                    line = "外部地图服务暂时没有返回足够结果，可以稍后重试或换一个区域。"
                elif "_" in key:
                    line = "部分外部信息暂不完整，已在推荐中做保守处理。"
                else:
                    line = key
            lines.append(line)
        return self._unique_display_lines(lines)

    @staticmethod
    def _unique_display_lines(lines: list) -> list:
        seen = set()
        unique = []
        for line in lines:
            text = str(line or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            unique.append(text)
        return unique

    def _format_distance(self, distance_m) -> str:
        try:
            value = float(distance_m)
        except (TypeError, ValueError):
            return "未知"
        if value >= 1000:
            return f"{value / 1000:.1f}公里"
        return f"{int(round(value))}米"

        try:
            value = float(distance_m)
        except (TypeError, ValueError):
            return "未知"
        if value >= 1000:
            return f"{value / 1000:.1f}公里"
        return f"{int(round(value))}米"

    def _format_score(self, score) -> str:
        try:
            value = float(score)
        except (TypeError, ValueError):
            return "可参考"
        if value <= 1:
            value *= 100
        return f"{int(round(value))}分"

        try:
            value = float(score)
        except (TypeError, ValueError):
            return "可参考"
        if value <= 1:
            value *= 100
        return f"{int(round(value))}分"

    @staticmethod
    def _friendly_policy_label(policy_type: str) -> str:
        labels = {
            "food": "美食优先",
            "sightseeing": "拍照打卡优先",
            "balanced": "观光和餐饮兼顾",
            "low_queue": "少排队优先",
            "efficient": "效率优先",
            "experience": "体验优先",
            "auto": "自动判断",
        }
        return labels.get(str(policy_type or "").strip(), str(policy_type or "自动判断"))

    def _get_route_option_display_title(self, option: dict) -> str:
        profile = str(option.get("profile", "")).strip().lower().replace("-", "_")
        raw_title = str(option.get("title") or "").strip()
        normalized_title = raw_title.lower().replace("-", "_").replace(" ", "_")

        title_map = {
            "low_queue": "少排队路线",
            "low_queue_route": "少排队路线",
            "balanced": "路线",
            "balanced_route": "路线",
            "efficient": "效率优先路线",
            "efficient_route": "效率优先路线",
            "experience": "体验优先路线",
            "experience_route": "体验优先路线",
            "experience_first": "体验优先路线",
            "experience_first_route": "体验优先路线",
            "fastest": "用时更短路线",
            "shortest": "距离更短路线",
            "fewest_transfers": "少换乘路线",
            "low_walking": "少步行路线",
            "romantic_date": "情侣约会路线",
            "partner_rainy_date": "雨天约会路线",
            "rainy_day_date": "雨天约会路线",
            "exhibition_drinks": "展览小酒路线",
            "easy_citywalk": "轻松散步路线",
            "citywalk_easy": "轻松散步路线",
        }

        mapped = title_map.get(normalized_title) or title_map.get(profile)
        if mapped:
            return mapped
        if raw_title and not ("_" in normalized_title or re.fullmatch(r"[a-z0-9_]+", normalized_title)):
            return self._display_itinerary_title(raw_title)
        return "路线"

    def _format_duration(self, minutes) -> str:
        try:
            value = float(minutes)
        except (TypeError, ValueError):
            return "未知"
        rounded = int(round(value))
        hours = rounded // 60
        mins = rounded % 60
        if hours and mins:
            return f"{hours}小时{mins}分钟"
        if hours:
            return f"{hours}小时"
        return f"{mins}分钟"

        try:
            value = float(minutes)
        except (TypeError, ValueError):
            return "未知"

        rounded = int(round(value))
        hours = rounded // 60
        mins = rounded % 60
        if hours and mins:
            return f"{hours}小时{mins}分钟"
        if hours:
            return f"{hours}小时"
        return f"{mins}分钟"

    @staticmethod
    def _transport_icon(mode: str) -> str:
        icons = {
            "walking": "\U0001f6b6",
            "bicycling": "\U0001f6b2",
            "transit": "\U0001f687",
            "driving": "\U0001f697",
            "electrobike": "\U0001f6f5",
        }
        return icons.get(str(mode or "").strip().casefold(), "\U0001f9ed")

    @staticmethod
    def _is_unknown_duration_text(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        if text in {"未知", "unknown"}:
            return True
        return not any(ch.isdigit() for ch in text)

    @staticmethod
    def _pick_first_number(*values):
        for value in values:
            if value in (None, "", []):
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _itinerary_primary_route_metrics(self, itinerary: dict):
        metrics = itinerary.get("metrics", {}) if isinstance(itinerary.get("metrics"), dict) else {}
        route_options = itinerary.get("route_options", [])
        first_option = route_options[0] if route_options and isinstance(route_options[0], dict) else {}
        first_metrics = first_option.get("metrics", {}) if isinstance(first_option.get("metrics"), dict) else {}

        duration_min = self._pick_first_number(
            itinerary.get("estimated_duration_min"),
            metrics.get("total_minutes"),
            metrics.get("estimated_duration_min"),
            first_metrics.get("total_minutes"),
            first_metrics.get("estimated_duration_min"),
            first_option.get("total_minutes"),
            first_option.get("estimated_duration_min"),
        )
        distance_m = self._pick_first_number(
            itinerary.get("total_distance_m"),
            metrics.get("distance_m"),
            metrics.get("total_distance_m"),
            first_metrics.get("distance_m"),
            first_metrics.get("total_distance_m"),
            first_option.get("distance_m"),
            first_option.get("total_distance_m"),
        )
        return duration_min, distance_m

    def _display_urban_micro_trip_context(self, payload: dict):
        if not isinstance(payload, dict):
            return
        urban_profile = payload.get("urban_intent_profile") if isinstance(payload.get("urban_intent_profile"), dict) else {}
        weather_context = payload.get("weather_context") if isinstance(payload.get("weather_context"), dict) else {}
        if not weather_context and isinstance(urban_profile.get("weather_context"), dict):
            weather_context = urban_profile.get("weather_context")
        if not urban_profile and not weather_context:
            return

        time_context = urban_profile.get("time_context") if isinstance(urban_profile.get("time_context"), dict) else {}
        scenario = str(urban_profile.get("scenario") or "").strip()
        if scenario:
            self.console.print(f"\u573a\u666f: {self._urban_scenario_label(scenario)}", style="dim")

        start_text = self._format_iso_clock(time_context.get("inferred_start_time"))
        end_text = self._format_iso_clock(time_context.get("inferred_end_time"))
        current_text = self._format_iso_clock(time_context.get("current_datetime"))
        if start_text and end_text:
            prefix = f"\u6309\u5f53\u524d\u65f6\u95f4 {current_text}\uff0c" if current_text else ""
            self.console.print(f"{prefix}\u4e3a\u4f60\u5b89\u6392 {start_text}-{end_text}", style="dim")

        if isinstance(weather_context, dict) and weather_context.get("source"):
            weather_line = self._format_weather_context(weather_context)
            if weather_line:
                self.console.print(weather_line, style="dim")

        constraints = urban_profile.get("route_constraints") if isinstance(urban_profile.get("route_constraints"), dict) else {}
        if constraints.get("require_opening_hours_check"):
            self.console.print(
                "\u8425\u4e1a\u65f6\u95f4\u5df2\u7eb3\u5165\u6821\u9a8c\uff1a"
                "\u660e\u786e\u5173\u95ed\u7684\u5730\u70b9\u4e0d\u4f1a\u8fdb\u5165\u4e3b\u8def\u7ebf\uff0c"
                "\u672a\u77e5\u8425\u4e1a\u65f6\u95f4\u4f1a\u964d\u6743\u515c\u5e95\u3002",
                style="dim",
            )

        activities = urban_profile.get("activity_sequence") if isinstance(urban_profile.get("activity_sequence"), list) else []
        if activities:
            labels = []
            for item in activities:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or item.get("type") or "").strip()
                if label:
                    labels.append(label)
            if labels:
                self.console.print("\u6d3b\u52a8\u987a\u5e8f: " + " -> ".join(labels), style="dim")

    def _format_iso_clock(self, value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return datetime.fromisoformat(text).strftime("%H:%M")
        except ValueError:
            return text[11:16] if len(text) >= 16 and text[10:11] == "T" else text

    def _format_weather_context(self, weather_context: dict) -> str:
        condition = str(weather_context.get("condition") or "unknown")
        temp = weather_context.get("temperature_c")
        indoor = weather_context.get("indoor_preferred") is True
        warnings = weather_context.get("warnings") if isinstance(weather_context.get("warnings"), list) else []
        labels = {
            "rain": "\u6709\u96e8",
            "storm": "\u96f7\u96e8/\u5f3a\u5bf9\u6d41",
            "snow": "\u964d\u96ea",
            "hot": "\u9ad8\u6e29",
            "windy": "\u5927\u98ce",
            "clear": "\u6674\u6717",
            "cloudy": "\u591a\u4e91",
            "unknown": "\u5929\u6c14\u6682\u4e0d\u660e\u786e",
        }
        condition_label = labels.get(condition, condition)
        if "weather_user_real_conflict" in warnings:
            real_weather = weather_context.get("real_weather_context") if isinstance(weather_context.get("real_weather_context"), dict) else {}
            real_condition = str(real_weather.get("condition") or weather_context.get("real_condition") or "unknown")
            real_label = labels.get(real_condition, real_condition)
            return (
                f"\u5929\u6c14: \u7528\u6237\u8bbe\u5b9a\u4e3a{condition_label}\uff0c"
                f"\u5b9e\u65f6\u5929\u6c14\u4e3a{real_label}\uff0c"
                "\u672c\u6b21\u6309\u7528\u6237\u8bbe\u5b9a\u7684\u573a\u666f\u89c4\u5212\u3002"
            )
        temp_text = ""
        try:
            if temp is not None:
                temp_text = f"\uff0c\u7ea6{float(temp):.0f}\u2103"
        except (TypeError, ValueError):
            temp_text = ""
        if indoor:
            return f"\u5929\u6c14: {condition_label}{temp_text}\uff0c\u5df2\u4f18\u5148\u9009\u62e9\u5ba4\u5185/\u6709\u906e\u853d\u5730\u70b9\u3002"
        if warnings:
            return f"\u5929\u6c14: {condition_label}{temp_text}\uff0c\u5df2\u7eb3\u5165\u8def\u7ebf\u8bc4\u5206\u3002"
        return f"\u5929\u6c14: {condition_label}{temp_text}\uff0c\u6237\u5916\u6d3b\u52a8\u53ef\u6b63\u5e38\u53c2\u4e0e\u8bc4\u5206\u3002"

    def _urban_scenario_label(self, scenario: str) -> str:
        labels = {
            "after_work_relax_late_food": "\u4e0b\u73ed\u6309\u6469\u591c\u5bb5\u8def\u7ebf",
            "besties_beauty_drinks": "\u95fa\u871c\u7f8e\u7532\u5c0f\u9152\u8def\u7ebf",
            "girls_afternoon_evening": "\u95fa\u871c\u7f8e\u7532\u5c0f\u9152\u8def\u7ebf",
            "after_work_social_evening": "\u4e0b\u73ed\u665a\u996d\u6563\u6b65\u8def\u7ebf",
            "photo_food_day_trip": "\u62cd\u7167\u6253\u5361\u5403\u996d\u8def\u7ebf",
            "full_day_photo_food": "\u62cd\u7167\u6253\u5361\u5403\u996d\u8def\u7ebf",
            "romantic_date_micro_trip": "\u60c5\u4fa3\u7ea6\u4f1a\u5fae\u884c\u7a0b",
            "romantic_date": "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf",
            "rainy_day_date": "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf",
            "partner_date": "\u60c5\u4fa3\u7ea6\u4f1a\u5fae\u884c\u7a0b",
            "date": "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf",
            "\u7ea6\u4f1a": "\u60c5\u4fa3\u7ea6\u4f1a\u5fae\u884c\u7a0b",
            "\u96e8\u5929\u7ea6\u4f1a": "\u60c5\u4fa3\u96e8\u5929\u5c55\u89c8\u5c0f\u9152\u8def\u7ebf",
            "classmates_budget_gathering": "\u540c\u5b66\u5e73\u4ef7\u805a\u4f1a\u8def\u7ebf",
            "classmate_budget_social": "\u540c\u5b66\u5e73\u4ef7\u805a\u4f1a\u8def\u7ebf",
            "easy_citywalk": "\u8f7b\u677e\u6563\u6b65\u8def\u7ebf",
            "citywalk_easy": "\u8f7b\u677e\u6563\u6b65\u8def\u7ebf",
            "general_urban_micro_trip": "\u57ce\u5e02\u5fae\u884c\u7a0b",
            "urban_micro_trip": "\u57ce\u5e02\u5fae\u884c\u7a0b",
        }
        return labels.get(str(scenario or ""), str(scenario or "\u57ce\u5e02\u5fae\u884c\u7a0b"))

    def _format_queue_wait(self, risk) -> str:
        try:
            value = float(risk)
        except (TypeError, ValueError):
            return "排队情况暂不明确，建议到店前确认"

        if value < 0.25:
            return "大概率不用久等"
        if value < 0.4:
            return "有小可能需要等候，预计排队约5-10分钟"
        if value < 0.6:
            return "可能需要短暂等候，预计排队约10-20分钟"
        if value < 0.75:
            return "排队概率偏高，预计等候约20-35分钟"
        return "排队概率较高，预计等候35分钟以上"

    def _get_agent_display_name(self, agent_name: str) -> str:
        clean_names = {
            "event_collection": "出行信息整理",
            "preference": "偏好读取",
            "itinerary_planning": "行程方案生成",
            "information_query": "相关信息查询",
            "rag_knowledge": "知识库查询",
            "memory_query": "长期记忆读取",
            "memory_save": "记忆保存",
            "poi_search": "真实地点检索",
            "route_planning": "路线优化",
            "poi-search": "真实地点检索",
            "route-planning": "路线优化",
            "route_preference": "路线偏好识别",
            "weather": "天气查询",
        }
        if agent_name in clean_names:
            return clean_names[agent_name]

        """获取智能体的显示名称"""
        # 与 README / LazyAgentRegistry 保持一致
        agent_display_names = {
            "event_collection": "出行信息整理",
            "preference": "偏好读取",
            "itinerary_planning": "行程方案生成",
            "information_query": "相关信息查询",
            "rag_knowledge": "相关知识查询",
            "memory_query": "历史偏好查看",
            "poi_search": "真实地点检索",
            "route_planning": "路线优化",
            "poi-search": "真实地点检索",
            "route-planning": "路线优化",
        }
        return agent_display_names.get(agent_name, "处理步骤")

    def show_status(self):
        """显示当前状态"""
        # 记忆统计
        full_context = self.memory_manager.get_full_context()
        short_term_stats = full_context["short_term"]["statistics"]
        long_term_stats = full_context["long_term"]["statistics"]

        memory_table = Table(title="记忆状态", show_header=True, header_style="bold magenta")
        memory_table.add_column("类型", style="cyan")
        memory_table.add_column("状态", style="white")

        memory_table.add_row(
            "短期记忆",
            f"{short_term_stats['total_messages']} 条消息"
        )
        memory_table.add_row(
            "长期记忆",
            f"{long_term_stats['total_trips']} 次行程"
        )
        memory_table.add_row(
            "已加载能力",
            f"{len(self._agent_cache)} 个"
        )

        self.console.print(memory_table)
        self.console.print()

        # 历史对话
        recent_messages = self.memory_manager.short_term.get_recent_context(n_turns=5)
        if recent_messages:
            dialogue_table = Table(title="最近对话 (最多5轮)", show_header=True, header_style="bold cyan")
            dialogue_table.add_column("角色", style="cyan", width=8)
            dialogue_table.add_column("内容", style="white", width=60)
            dialogue_table.add_column("时间", style="dim", width=12)

            for msg in recent_messages:
                role_name = "用户" if msg["role"] == "user" else "轻途"
                content = msg["content"]

                # 截断过长的内容
                if len(content) > 100:
                    content = content[:100] + "..."

                # 格式化时间
                timestamp = msg.get("timestamp", "")
                if timestamp:
                    from datetime import datetime
                    try:
                        dt = datetime.fromisoformat(timestamp)
                        time_str = dt.strftime("%H:%M:%S")
                    except:
                        time_str = ""
                else:
                    time_str = ""

                dialogue_table.add_row(role_name, content, time_str)

            self.console.print(dialogue_table)
            self.console.print()

    async def run_health_check(self):
        """在会话内执行健康检查并显示熔断器状态"""
        if self.circuit_breaker:
            status = self.circuit_breaker.get_status()
            state_labels = {
                "closed": "正常",
                "open": "暂时保护中",
                "half_open": "恢复检测中",
                "CLOSED": "正常",
                "OPEN": "暂时保护中",
                "HALF_OPEN": "恢复检测中",
            }
            state_text = state_labels.get(str(status.get("state")), str(status.get("state") or "未知"))
            self.console.print(f"[bold]服务状态[/bold]: {state_text}", style="cyan")
        ok, msg = await check_llm_health(
            base_url=LLM_CONFIG["base_url"],
            api_key=LLM_CONFIG["api_key"],
            model_name=LLM_CONFIG["model_name"],
            timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
        )
        if ok:
            self.console.print("规划服务: [green]正常[/green]", style="bold")
        else:
            self.console.print(f"规划服务: [red]不可用[/red] - {msg}", style="bold")
        self.console.print()

    def show_history(self):
        """显示历史行程"""
        history = self.memory_manager.long_term.get_trip_history(10)

        if not history:
            self.console.print("暂无历史行程", style="yellow")
            return

        table = Table(title="历史行程", show_header=True, header_style="bold magenta")
        table.add_column("ID", style="cyan")
        table.add_column("出发地", style="white")
        table.add_column("目的地", style="white")
        table.add_column("日期", style="white")
        table.add_column("目的", style="white")

        for trip in history:
            table.add_row(
                trip.get("trip_id", ""),
                trip.get("origin", ""),
                trip.get("destination", ""),
                trip.get("start_date", ""),
                trip.get("purpose", "")
            )

        self.console.print(table)

    def show_preferences(self):
        """显示用户偏好"""
        prefs = self.memory_manager.long_term.get_preference()

        table = Table(title="用户偏好", show_header=True, header_style="bold magenta")
        table.add_column("类型", style="cyan")
        table.add_column("值", style="white")

        for key, value in prefs.items():
            if value:
                table.add_row(key, str(value))

        self.console.print(table)

    async def _prompt_async(self, default_text: Optional[str] = None, prompt_text: str = "> ") -> str:
        """Read one CLI input line, optionally prefilled for editing."""
        if PromptSession is None:
            raise RuntimeError("缺少 prompt_toolkit，请先安装：pip install prompt_toolkit")
        if self._prompt_session is None:
            self._prompt_session = PromptSession()

        prompt = f"\n{prompt_text}"
        if patch_stdout is not None:
            with patch_stdout():
                return await self._prompt_session.prompt_async(
                    prompt,
                    default=default_text or "",
                )
        return await self._prompt_session.prompt_async(prompt, default=default_text or "")

    def _is_current_request(self, request_id: Optional[int]) -> bool:
        return request_id is None or request_id == self.current_request_id

    def _set_current_stage(self, stage: str, request_id: Optional[int] = None) -> None:
        if not self._is_current_request(request_id):
            return
        self.current_stage = stage

    def _print_stage_timing(self, stage: str, started_at: float, request_id: Optional[int] = None) -> None:
        if not self._is_current_request(request_id):
            return
        elapsed_sec = max(0.0, time.monotonic() - float(started_at))
        stage_labels = {
            "long_term_memory": "读取偏好",
            "intent_recognition": "理解需求",
            "orchestration": "整理路线",
        }
        label = stage_labels.get(str(stage), "处理")
        self.console.print(f"{label}用时 {elapsed_sec:.2f}s", style="dim")

    @staticmethod
    def _normalize_busy_command(text: str) -> Optional[str]:
        value = str(text or "").strip()
        if value == "/edit":
            return "edit"
        if value == "/cancel":
            return "cancel"
        return None

    async def _start_query_task(self, user_input: str) -> None:
        if self.current_task and not self.current_task.done():
            self._cancel_current_task(show_message=False)

        preset_route_type = self._ask_route_preference_if_needed(user_input)
        start_location = self._ask_start_location_if_needed(user_input)
        if isinstance(start_location, dict) and start_location.get("_cancelled"):
            self.console.print("\u5df2\u53d6\u6d88\u672c\u6b21\u8def\u7ebf\u89c4\u5212\u3002", style="yellow")
            return
        self._request_counter += 1
        request_id = self._request_counter
        self.current_query = user_input
        self.current_request_id = request_id
        self.current_started_at = datetime.now()
        self.current_stage = "正在准备处理请求..."
        self.current_last_progress_at = datetime.now()
        self.current_last_heartbeat_stage = None
        self.current_same_stage_heartbeat_count = 0
        self.current_task = asyncio.create_task(
            self._run_query_task(user_input, request_id, preset_route_type, start_location)
        )
        self.current_progress_task = self._start_progress_heartbeat(request_id)
        self.console.print("可输入 /edit 修改当前请求，或 /cancel 取消。", style="dim")

    def _start_progress_heartbeat(self, request_id: int):
        return asyncio.create_task(self._run_progress_heartbeat(request_id))

    async def _run_progress_heartbeat(self, request_id: int) -> None:
        try:
            while self._is_current_request(request_id):
                await asyncio.sleep(float(self.progress_heartbeat_interval_sec))
                if not self._is_current_request(request_id):
                    return
                stage = self.current_stage or "正在处理请求..."
                self.current_last_progress_at = datetime.now()
                if stage != self.current_last_heartbeat_stage:
                    self.current_last_heartbeat_stage = stage
                    self.current_same_stage_heartbeat_count = 0
                    self.console.print(stage)
                    continue
                self.current_same_stage_heartbeat_count += 1
                if self.current_same_stage_heartbeat_count % 6 == 0:
                    self.console.print("请求仍在处理中，请稍候...")
        except asyncio.CancelledError:
            return

    def _stop_progress_heartbeat(self) -> None:
        task = self.current_progress_task
        self.current_progress_task = None
        if task and not task.done():
            task.cancel()

    async def _run_query_task(
        self,
        user_input: str,
        request_id: int,
        preset_route_type: Optional[str],
        start_location: Optional[dict],
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            done = loop.create_future()

            def runner():
                try:
                    self._run_query_in_worker_thread(user_input, request_id, preset_route_type, start_location)
                except BaseException as exc:
                    loop.call_soon_threadsafe(self._finish_worker_future, done, exc)
                else:
                    loop.call_soon_threadsafe(self._finish_worker_future, done, None)

            threading.Thread(target=runner, daemon=True).start()
            await done
        except asyncio.CancelledError:
            return
        except CircuitOpenError:
            if self._is_current_request(request_id):
                self.console.print("\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]", style="dim")
        except Exception as e:
            if self._is_current_request(request_id):
                self.console.print(f"\n错误: {e}", style="red")
        finally:
            if self._is_current_request(request_id):
                self._stop_progress_heartbeat()
                self.current_task = None
                self.current_query = None
                self.current_request_id = None
                self.current_started_at = None
                self.current_stage = None
                self.current_last_progress_at = None
                self.current_last_heartbeat_stage = None
                self.current_same_stage_heartbeat_count = 0

    @staticmethod
    def _finish_worker_future(future, exception) -> None:
        if future.done():
            return
        if exception is None:
            future.set_result(None)
        else:
            future.set_exception(exception)

    def _run_query_in_worker_thread(
        self,
        user_input: str,
        request_id: int,
        preset_route_type: Optional[str],
        start_location: Optional[dict],
    ) -> None:
        asyncio.run(
            self.process_query(
                user_input,
                request_id=request_id,
                preset_route_type=preset_route_type,
                start_location=start_location,
                ask_route_preference=False,
            )
        )

    def _cancel_current_task(self, show_message: bool = True) -> None:
        task = self.current_task
        self._stop_progress_heartbeat()
        self.current_task = None
        self.current_query = None
        self.current_request_id = None
        self.current_started_at = None
        self.current_stage = None
        self.current_last_progress_at = None
        self.current_last_heartbeat_stage = None
        self.current_same_stage_heartbeat_count = 0
        if task and not task.done():
            task.cancel()
        if show_message:
            self.console.print("已取消当前请求", style="yellow")

    async def _handle_busy_command(self, user_input: str) -> None:
        command = self._normalize_busy_command(user_input)
        if command == "edit":
            await self._edit_current_query()
            return
        if command == "cancel":
            self._cancel_current_task()
            return
        self.console.print(
            "当前请求仍在处理。如需修改，请准确输入 /edit；如需取消，请准确输入 /cancel。",
            style="yellow",
        )

    async def _edit_current_query(self) -> None:
        original_query = self.current_query or ""
        self._cancel_current_task(show_message=False)
        edited_query = (
            await self._prompt_async(
                default_text=original_query,
                prompt_text="请输入修改后的完整请求: ",
            )
        ).strip()
        if not edited_query:
            self.console.print("未输入新需求，已取消当前请求。", style="yellow")
            return
        self.console.print("已收到修改，正在重新生成...", style="green")
        await self._start_query_task(edited_query)

    async def run(self):
        """运行 CLI"""
        # 打印横幅
        self.print_banner()

        # 初始化系统
        await self.initialize_system()

        if PromptSession is None:
            self.console.print(
                "\n错误: 缺少 prompt_toolkit，请先安装：pip install prompt_toolkit\n",
                style="red",
            )
            try:
                self.memory_manager.end_session()
            except Exception:
                pass
            return

        # 主循环
        while True:
            try:
                # 获取用户输入
                try:
                    user_input = await self._prompt_async()
                except EOFError:
                    try:
                        self.memory_manager.end_session()
                    except Exception:
                        pass
                    self.console.print("\n输入已结束，轻途已退出。", style="dim")
                    break

                if not user_input.strip():
                    continue

                command = user_input.strip().lower()
                if command == "exit":
                    if self.current_task and not self.current_task.done():
                        self._cancel_current_task(show_message=False)
                    self.memory_manager.end_session()
                    self.console.print("再见！", style="cyan")
                    break

                if self.current_task and not self.current_task.done():
                    await self._handle_busy_command(user_input)
                    continue

                # 处理命令
                if command == "help":
                    self.print_help()
                elif command == "status":
                    self.show_status()
                elif command == "health":
                    await self.run_health_check()
                elif command == "clear":
                    self.memory_manager.short_term.clear()
                    self.console.print("已清空短期记忆", style="green")
                elif command == "history":
                    self.show_history()
                elif command == "preferences":
                    self.show_preferences()
                elif command in {"/edit", "/cancel"}:
                    self.console.print("当前没有正在处理的请求。", style="dim")
                else:
                    # 处理自然语言查询
                    await self._start_query_task(user_input)

            except KeyboardInterrupt:
                if self.current_task and not self.current_task.done():
                    self._cancel_current_task()
                else:
                    self.console.print("\n使用 'exit' 退出", style="dim")
            except CircuitOpenError:
                self.console.print("\n[bold yellow]⚠ 服务暂时不可用，请稍后再试。[/bold yellow]", style="dim")
            except RuntimeError as e:
                if "prompt_toolkit" in str(e):
                    self.console.print(f"\n错误: {e}", style="red")
                    try:
                        self.memory_manager.end_session()
                    except Exception:
                        pass
                    break
                self.console.print(f"\n错误: {e}", style="red")
            except Exception as e:
                self.console.print(f"\n错误: {e}", style="red")

def run_health_check_standalone() -> int:
    """
    独立执行健康检查（用于 `python cli.py health`）。
    不进入交互式 CLI，只检测 LLM 是否可达。
    Returns:
        0 成功，1 失败（便于脚本/监控）
    """
    import asyncio
    init_agentscope()
    ok, msg = asyncio.run(check_llm_health(
        base_url=LLM_CONFIG["base_url"],
        api_key=LLM_CONFIG["api_key"],
        model_name=LLM_CONFIG["model_name"],
        timeout_sec=RESILIENCE_CONFIG.get("health_check_timeout_sec", 10.0),
    ))
    if ok:
        print("OK")
        return 0
    print(f"FAIL: {msg}")
    return 1


def main():
    """主函数"""
    if len(sys.argv) > 1 and sys.argv[1].strip().lower() == "health":
        exit(run_health_check_standalone())
    cli = AligoCLI()
    asyncio.run(cli.run())


if __name__ == "__main__":
    main()
