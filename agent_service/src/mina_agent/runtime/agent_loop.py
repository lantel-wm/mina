from __future__ import annotations

from mina_agent.runtime.turn_service import AgentServices, TurnService
from mina_agent.schemas import TurnResponse, TurnResumeRequest, TurnStartRequest


class AgentLoop:
    def __init__(self, services: AgentServices) -> None:
        self._turn_service = TurnService(services)

    def start_turn(self, request: TurnStartRequest) -> TurnResponse:
        return self._turn_service.start_turn(request)

    def resume_turn(self, continuation_id: str, request: TurnResumeRequest) -> TurnResponse:
        return self._turn_service.resume_turn(continuation_id, request)
