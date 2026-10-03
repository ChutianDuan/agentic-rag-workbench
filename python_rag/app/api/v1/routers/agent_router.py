from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from python_rag.app.agent.models import (
    get_agent_run,
    list_agent_steps,
    list_agent_tool_calls,
)
from python_rag.app.agent.orchestrator import AgentOrchestrator, AgentOrchestratorError
from python_rag.app.agent.schemas import AgentChatRequest
from python_rag.app.agent.streaming.agent_streaming_service import stream_agent_chat
from python_rag.app.core.error_codes import (
    ERR_INTERNAL_ERROR,
    ERR_INVALID_REQUEST,
    ERR_SESSION_NOT_FOUND,
)
from python_rag.app.core.errors import AppError, SessionNotFoundError
from python_rag.app.modules.chat.repo import bulk_insert_citations
from python_rag.app.modules.messages.repo import create_message
from python_rag.app.modules.sessions.repo import get_session_by_id
from python_rag.app.shared.common import api_response
from python_rag.app.shared.schemas import ApiResponse


internal_router = APIRouter(prefix="/internal/agent", tags=["agent"])
legacy_router = APIRouter(
    prefix="/api/agent",
    tags=["agent"],
    include_in_schema=False,
)
router = APIRouter()


def _ensure_session(session_id: int) -> Dict[str, Any]:
    session = get_session_by_id(session_id)
    if not session:
        raise SessionNotFoundError(ERR_SESSION_NOT_FOUND, "session not found")
    return session


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if hasattr(value, "isoformat"):
            result[key] = value.isoformat()
    return result


def _steps_with_tool_calls(run_id: int) -> List[Dict[str, Any]]:
    tool_calls_by_step_id: Dict[int, List[Dict[str, Any]]] = {}
    for tool_call in list_agent_tool_calls(run_id):
        item = _serialize_row(tool_call)
        tool_calls_by_step_id.setdefault(item["step_id"], []).append(item)

    items = []
    for step in list_agent_steps(run_id):
        item = _serialize_row(step)
        item["tool_calls"] = tool_calls_by_step_id.get(item["id"], [])
        items.append(item)
    return items


def _save_agent_citations(message_id: int, citations: List[Dict[str, Any]]) -> None:
    if citations:
        bulk_insert_citations(message_id=message_id, hits=citations)


def _agent_streaming_response(
    req: AgentChatRequest,
    last_event_id: Optional[str] = None,
) -> StreamingResponse:
    _ensure_session(req.session_id)
    return StreamingResponse(
        stream_agent_chat(
            session_id=req.session_id,
            message=req.message,
            trace_id=req.trace_id,
            last_event_id=last_event_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@legacy_router.post("/chat", response_model=ApiResponse)
@internal_router.post("/chat", response_model=ApiResponse)
async def agent_chat(
    req: AgentChatRequest,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
):
    if req.stream:
        return _agent_streaming_response(req, last_event_id=last_event_id)

    _ensure_session(req.session_id)
    user_message = create_message(
        session_id=req.session_id,
        role="user",
        content=req.message,
        status="SUCCESS",
        meta={"source": "agent_chat"},
    )

    try:
        result = await AgentOrchestrator().run(
            question=req.message,
            trace_id=req.trace_id,
            session_id=req.session_id,
            user_message_id=user_message["message_id"],
        )
    except AgentOrchestratorError as exc:
        raise AppError(
            ERR_INTERNAL_ERROR,
            "agent chat failed: {0}".format(str(exc)),
            http_status=500,
        )

    assistant_message = create_message(
        session_id=req.session_id,
        role="assistant",
        content=result["answer"],
        status="SUCCESS",
        meta={
            "source": "agent",
            "answer_source": "agent",
            "agent_run_id": result["run_id"],
            "steps_used": result.get("steps_used"),
            "citation_count": len(result.get("citations") or []),
            "retrieval": result.get("retrieval") or {},
            "routing": result.get("routing") or {},
        },
    )
    citations = result.get("citations") or []
    _save_agent_citations(assistant_message["message_id"], citations)

    return api_response(
        {
            "run_id": result["run_id"],
            "message_id": assistant_message["message_id"],
            "answer": result["answer"],
            "citations": citations,
            "retrieval": result.get("retrieval") or {},
            "routing": result.get("routing") or {},
        }
    )


@legacy_router.post("/chat/stream")
@internal_router.post("/chat/stream")
async def agent_chat_stream(
    req: AgentChatRequest,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
):
    return _agent_streaming_response(req, last_event_id=last_event_id)


@legacy_router.get("/runs/{run_id}", response_model=ApiResponse)
@internal_router.get("/runs/{run_id}", response_model=ApiResponse)
def get_agent_run_endpoint(run_id: int):
    row = get_agent_run(run_id)
    if not row:
        raise AppError(ERR_INVALID_REQUEST, "agent run not found", http_status=404)

    return api_response({"run": _serialize_row(row)})


@legacy_router.get("/runs/{run_id}/steps", response_model=ApiResponse)
@internal_router.get("/runs/{run_id}/steps", response_model=ApiResponse)
def list_agent_run_steps_endpoint(run_id: int):
    row = get_agent_run(run_id)
    if not row:
        raise AppError(ERR_INVALID_REQUEST, "agent run not found", http_status=404)

    return api_response(
        {
            "run_id": run_id,
            "steps": _steps_with_tool_calls(run_id),
        }
    )


router.include_router(internal_router)
router.include_router(legacy_router)
