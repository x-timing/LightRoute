"""
Route planning Skill Agent.

The actual route planning logic lives in tools.route_planning_tool. This
wrapper keeps compatibility with the existing Traveler Skill/Agent
orchestration.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Union

from agentscope.agent import AgentBase
from agentscope.message import Msg

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from planning.route_optimizer import RouteOptimizer
from tools.route_planning_tool import run_route_planning


class RoutePlanningAgent(AgentBase):
    """Agent wrapper for the deterministic route planning tool."""

    def __init__(
        self,
        name: str = "RoutePlanningAgent",
        model=None,
        optimizer: Optional[RouteOptimizer] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        self.name = name
        self.model = model
        self.optimizer = optimizer or RouteOptimizer()

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        payload = self._parse_input(x)
        result = run_route_planning(
            context=payload.get("context", {}),
            previous_results=payload.get("previous_results", []),
            optimizer=self.optimizer,
        )
        return Msg(name=self.name, content=json.dumps(result, ensure_ascii=False), role="assistant")

    @staticmethod
    def _parse_input(x: Union[Msg, List[Msg]]) -> Dict[str, Any]:
        content = x[-1].content if isinstance(x, list) else x.content
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return {"context": {"rewritten_query": content}}
        return {"context": {"rewritten_query": str(content)}}
