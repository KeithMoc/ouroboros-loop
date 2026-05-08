"""Wiring lock for the nested-MCP interview ``strict_mcp_config`` opt-in.

The nested ``ouroboros_interview`` MCP-tool entrypoint
(``InterviewHandler.handle()``) MUST request ``strict_mcp_config=True``
when constructing its question-generation adapter — otherwise the
spawned Claude subprocess can rediscover the plugin-provided ouroboros
MCP server and recurse on ``ouroboros_interview`` until it hits the
``--max-turns 1`` boundary.

CLI interview entrypoints (``ooo init`` / ``ooo pm``) are NOT exercised
here; they are confirmed by ``test_factory.py`` to leave the flag at
its default ``False`` so they keep plugin/project ``.mcp.json`` servers
reachable for brownfield repository tooling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler


@dataclass(slots=True)
class _FakeInterviewEngine:
    """Minimal injected engine that satisfies the handler's contract.

    ``start_interview`` returns a persisted state, ``ask_next_question``
    returns a deterministic question, ``save_state`` is a no-op probe.
    The handler is exercised purely for its factory-call wiring.
    """

    state_dir: Path
    saved_states: list[InterviewState] = field(default_factory=list)

    async def start_interview(
        self,
        initial_context: str,
        cwd: str | None = None,
        interview_id: str | None = None,
    ) -> Result[InterviewState, MCPServerError]:
        sid = interview_id or "interview_strictwiring001"
        state = InterviewState(
            interview_id=sid,
            initial_context=initial_context,
            status=InterviewStatus.IN_PROGRESS,
        )
        self.saved_states.append(state)
        return Result.ok(state)

    async def ask_next_question(self, state: InterviewState) -> Result[str, MCPServerError]:
        return Result.ok("What is the primary user goal?")

    async def save_state(self, state: InterviewState) -> Result[Path, MCPServerError]:
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text("{}", encoding="utf-8")
        self.saved_states.append(state)
        return Result.ok(path)

    async def load_state(
        self, session_id: str
    ) -> Result[InterviewState, MCPServerError]:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.asyncio
async def test_nested_mcp_interview_handler_requests_strict_mcp_config(
    tmp_path: Path,
) -> None:
    """``InterviewHandler.handle()`` MUST opt into strict MCP isolation."""
    engine = _FakeInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    fake_adapter = MagicMock()

    with patch(
        "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
        return_value=fake_adapter,
    ) as mock_factory:
        outcome = await handler.handle({"initial_context": "Build a CLI", "cwd": str(tmp_path)})

    assert outcome.is_ok, "handler must complete successfully on the happy path"
    assert mock_factory.called, "handler must construct an LLM adapter via the factory"

    factory_kwargs = mock_factory.call_args.kwargs
    assert factory_kwargs.get("use_case") == "interview", (
        "the question-generation adapter is built with use_case='interview'"
    )
    assert factory_kwargs.get("strict_mcp_config") is True, (
        "the nested MCP interview entrypoint MUST request strict_mcp_config=True "
        "to prevent the spawned subprocess from rediscovering plugin-provided "
        "ouroboros and recursing on ouroboros_interview"
    )
