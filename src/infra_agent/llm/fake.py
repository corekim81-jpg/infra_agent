"""테스트용 가짜 모델. 목적(purpose)별로 미리 정한 응답을 돌려주고 요청을 기록합니다."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from infra_agent.llm.base import LLMError, LLMRequest, LLMResponse

Responder = dict[str, Any] | Callable[[LLMRequest], dict[str, Any]] | LLMError


class FakeLLM:
    name = "fake"

    def __init__(self, responses: Mapping[str, Responder] | None = None) -> None:
        self.responses = dict(responses or {})
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        responder = self.responses.get(request.purpose)
        if responder is None:
            raise LLMError(f"가짜 모델에 등록되지 않은 목적입니다: {request.purpose}")
        if isinstance(responder, LLMError):
            raise responder
        data = responder(request) if callable(responder) else responder
        return LLMResponse(text="", data=data, cost_usd=0.0, model="fake")
