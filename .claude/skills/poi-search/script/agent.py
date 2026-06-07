"""
POI search Skill Agent.

The actual POI retrieval logic lives in tools.poi_search_tool. This wrapper
keeps compatibility with the existing Traveler Skill/Agent orchestration.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Union

from agentscope.agent import AgentBase
from agentscope.message import Msg

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from services.amap_client import AmapClient
from services.ugc_service import UGCService
from tools.poi_search_tool import run_poi_search


class PoiSearchAgent(AgentBase):
    """Agent wrapper for the deterministic POI search tool."""

    def __init__(
        self,
        name: str = "PoiSearchAgent",
        model=None,
        amap_client: Optional[Any] = None,
        ugc_service: Optional[UGCService] = None,
        **kwargs,
    ) -> None:
        super().__init__()
        self.name = name
        self.model = model
        self.amap_client = amap_client or AmapClient()
        self.ugc_service = ugc_service or UGCService()

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        if x is None:
            return Msg(name=self.name, content=json.dumps({}), role="assistant")

        payload = self._parse_input(x)
        result = run_poi_search(
            context=payload.get("context", {}),
            previous_results=payload.get("previous_results", []),
            amap_client=self.amap_client,
            ugc_service=self.ugc_service,
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
