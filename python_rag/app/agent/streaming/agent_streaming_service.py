"""Agent SSE 的生成任务、事件缓存与订阅管理；客户端重连只重放同一次运行。"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

from python_rag.app.agent.orchestrator import AgentOrchestrator
from python_rag.app.core.error_codes import ERR_STREAM_ABORTED
from python_rag.app.modules.chat.repo import bulk_insert_citations
from python_rag.app.modules.messages.repo import create_message
from python_rag.app.shared.sse import (
    build_sse_comment,
    build_sse_event,
    parse_last_event_id,
    resume_requested,
)


logger = logging.getLogger(__name__)

AGENT_EVENT_TYPES = {"agent_step", "tool_call", "tool_result", "final"}
STREAM_RESUME_TTL_SECONDS = 15 * 60
MAX_COMPLETED_STREAMS = 128
STREAM_HEARTBEAT_SECONDS = 15.0


@dataclass
class _BufferedAgentEvent:
    event_id: int
    event: Dict[str, Any]


@dataclass
class _AgentStreamState:
    """一条流的进程内状态；task 属于生成过程，subscribers 属于各次客户端连接。"""

    key: Tuple[int, str, str]
    session_id: int
    message: str
    trace_id: Optional[str]
    created_at: float
    updated_at: float
    next_event_id: int = 1
    events: List[_BufferedAgentEvent] = field(default_factory=list)
    subscribers: Set[asyncio.Queue] = field(default_factory=set)
    completed_at: Optional[float] = None
    task: Optional[asyncio.Task] = None
    lock: RLock = field(default_factory=RLock)


_STREAMS: Dict[Tuple[int, str, str], _AgentStreamState] = {}
_STREAMS_LOCK = RLock()


def _stream_key(
    session_id: int,
    message: str,
    trace_id: Optional[str],
) -> Tuple[int, str, str]:
    """优先以会话和 trace_id 标识运行，未提供 trace_id 时兼容按问题文本识别。"""
    normalized_trace_id = str(trace_id or "").strip()
    if normalized_trace_id:
        return (session_id, "trace", normalized_trace_id)
    return (session_id, "message", str(message or "").strip())


def _is_terminal_event(event: Dict[str, Any]) -> bool:
    return event.get("type") in {"done", "error"}


def _cleanup_stream_registry(now: Optional[float] = None) -> None:
    """只清理已结束的流：先按完成时间过期，再淘汰超出数量上限的最早完成流。"""
    now = now if now is not None else time.monotonic()
    expired_keys = [
        key
        for key, state in _STREAMS.items()
        if state.completed_at is not None
        and now - state.completed_at > STREAM_RESUME_TTL_SECONDS
    ]
    for key in expired_keys:
        _STREAMS.pop(key, None)

    completed = [
        state
        for state in _STREAMS.values()
        if state.completed_at is not None
    ]
    if len(completed) <= MAX_COMPLETED_STREAMS:
        return

    completed.sort(key=lambda item: item.completed_at or item.updated_at)
    for state in completed[:len(completed) - MAX_COMPLETED_STREAMS]:
        _STREAMS.pop(state.key, None)


def _sse_for_event(event: Dict[str, Any], event_id: Optional[int] = None) -> str:
    """将内部事件转换为既有 SSE 格式，保留各事件的字段与可选 event 名称。"""
    event_type = str(event.get("type") or "")

    if event_type == "delta":
        return build_sse_event(
            {
                "type": "delta",
                "delta": str(event.get("delta") or ""),
                "index": int(event.get("index") or 0),
            },
            event_id=event_id,
        )

    if event_type == "done":
        meta = event.get("meta")
        payload = {
            "type": "done",
            "message": "stream finished",
        }
        if isinstance(meta, dict):
            payload["meta"] = meta
        return build_sse_event(payload, event_id=event_id)

    if event_type == "error":
        payload = {
            "type": "error",
            "code": event.get("code", ERR_STREAM_ABORTED),
            "message": str(event.get("message") or "agent stream error"),
            "data": event.get("data"),
        }
        return build_sse_event(
            payload,
            event_id=event_id,
        )

    if event_type in AGENT_EVENT_TYPES:
        return build_sse_event(event, event=event_type, event_id=event_id)

    return build_sse_event(event, event_id=event_id)


def _append_event(
    state: _AgentStreamState,
    event: Dict[str, Any],
) -> _BufferedAgentEvent:
    """先分配递增编号并缓存事件，再通知在线订阅者，确保断线期间也能积累续传数据。"""
    now = time.monotonic()
    with state.lock:
        record = _BufferedAgentEvent(
            event_id=state.next_event_id,
            event=dict(event),
        )
        state.next_event_id += 1
        state.events.append(record)
        state.updated_at = now
        if _is_terminal_event(event):
            state.completed_at = now
        subscribers = list(state.subscribers)

    for subscriber in subscribers:
        subscriber.put_nowait(record)
    return record


def _subscribe(
    state: _AgentStreamState,
    last_event_id: int,
) -> Tuple[Optional[asyncio.Queue], List[_BufferedAgentEvent]]:
    """在同一把锁内获取历史和注册订阅，避免重放与实时接收之间漏掉事件。"""
    queue: asyncio.Queue = asyncio.Queue()
    with state.lock:
        replay = [
            record
            for record in state.events
            if record.event_id > last_event_id
        ]
        if state.completed_at is not None:
            return None, replay
        state.subscribers.add(queue)
    return queue, replay


def _unsubscribe(state: _AgentStreamState, queue: Optional[asyncio.Queue]) -> None:
    if queue is None:
        return
    with state.lock:
        state.subscribers.discard(queue)


async def _run_agent_task(state: _AgentStreamState) -> None:
    """创建消息并运行 Agent；工具事件实时缓存，最终答案和引用保存后再发送结束事件。"""
    started_at = time.perf_counter()

    async def emit(event: Dict[str, Any]) -> None:
        _append_event(state, event)

    try:
        question = str(state.message or "").strip()
        if not question:
            raise ValueError("message is required")

        user_message = create_message(
            session_id=state.session_id,
            role="user",
            content=question,
            status="SUCCESS",
            meta={"source": "agent_chat_stream"},
        )

        result = await AgentOrchestrator().run(
            question=question,
            trace_id=state.trace_id,
            session_id=state.session_id,
            user_message_id=user_message["message_id"],
            event_sink=emit,
        )

        # Agent 编排返回的是业务结果；这里补齐消息和引用持久化，失败时不能发送 done。
        assistant_message = create_message(
            session_id=state.session_id,
            role="assistant",
            content=result["answer"],
            status="SUCCESS",
            meta={
                "source": "agent",
                "answer_source": "agent",
                "agent_run_id": result["run_id"],
                "steps_used": result.get("steps_used"),
                "citation_count": len(result.get("citations") or []),
                "routing": result.get("routing") or {},
            },
        )
        citations = result.get("citations") or []
        retrieval = result.get("retrieval") or {}
        citation_doc_ids = sorted({
            int(item["doc_id"])
            for item in citations
            if isinstance(item, dict) and item.get("doc_id") is not None
        })
        if citations:
            bulk_insert_citations(
                message_id=assistant_message["message_id"],
                hits=citations,
            )

        e2e_latency_ms = int((time.perf_counter() - started_at) * 1000)
        meta = {
            "run_id": result["run_id"],
            "agent_run_id": result["run_id"],
            "assistant_message_id": assistant_message["message_id"],
            "message_id": assistant_message["message_id"],
            "steps_used": result.get("steps_used"),
            "e2e_latency_ms": e2e_latency_ms,
            "answer_source": "agent",
            "citation_count": len(citations),
            "retrieved_count": retrieval.get("retrieved_count"),
            "raw_hit_count": retrieval.get("candidate_count"),
            "retrieval_ms": retrieval.get("retrieval_latency_ms"),
            "lancedb_ms": retrieval.get("vector_search_latency_ms"),
            "rerank_ms": retrieval.get("rerank_latency_ms"),
            "doc_ids": citation_doc_ids,
            "routing": result.get("routing") or {},
        }

        # 当前 Agent 的回答以完整文本 delta 发送，实时工具 Trace 不代表逐 token 生成。
        if result["answer"]:
            await emit({"type": "delta", "delta": result["answer"], "index": 1})

        await emit(
            {
                "type": "final",
                "run_id": result["run_id"],
                "message_id": assistant_message["message_id"],
                "answer": result["answer"],
                "citations": citations,
                "retrieval": retrieval,
                "routing": result.get("routing") or {},
                "steps_used": result.get("steps_used"),
                "e2e_latency_ms": e2e_latency_ms,
            }
        )
        await emit({"type": "done", "meta": meta})
    except Exception as exc:
        logger.exception(
            "agent stream failed session_id=%s trace_id=%s",
            state.session_id,
            state.trace_id,
        )
        await emit({"type": "error", "message": str(exc)})


def _get_or_start_stream_state(
    session_id: int,
    message: str,
    trace_id: Optional[str],
    last_event_id: Optional[str],
) -> _AgentStreamState:
    """复用活跃流或续传缓存；只有没有续传标记的新请求才能启动新的业务运行。"""
    key = _stream_key(session_id, message, trace_id)
    now = time.monotonic()
    with _STREAMS_LOCK:
        _cleanup_stream_registry(now)
        existing = _STREAMS.get(key)
        if existing is not None:
            with existing.lock:
                is_active = existing.completed_at is None
            if is_active or resume_requested(last_event_id):
                return existing

        if resume_requested(last_event_id):
            # 缓存可能因过期、数量淘汰或进程重启丢失；明确报错，不能重新分类和生成。
            state = _AgentStreamState(
                key=key,
                session_id=session_id,
                message=message,
                trace_id=trace_id,
                created_at=now,
                updated_at=now,
                next_event_id=parse_last_event_id(last_event_id) + 1,
            )
            _append_event(
                state,
                {
                    "type": "error",
                    "code": ERR_STREAM_ABORTED,
                    "message": "stream resume state expired",
                },
            )
            _STREAMS[key] = state
            return state

        state = _AgentStreamState(
            key=key,
            session_id=session_id,
            message=message,
            trace_id=trace_id,
            created_at=now,
            updated_at=now,
        )
        state.task = asyncio.create_task(_run_agent_task(state))
        _STREAMS[key] = state
        return state


async def stream_agent_chat(
    session_id: int,
    message: str,
    trace_id: Optional[str] = None,
    last_event_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """订阅缓存和实时队列；连接关闭只取消订阅，不取消独立创建的生成 task。"""
    state = _get_or_start_stream_state(
        session_id=session_id,
        message=message,
        trace_id=trace_id,
        last_event_id=last_event_id,
    )
    queue, replay = _subscribe(state, parse_last_event_id(last_event_id))

    try:
        for record in replay:
            yield _sse_for_event(record.event, record.event_id)
            if _is_terminal_event(record.event):
                return

        while queue is not None:
            try:
                record = await asyncio.wait_for(
                    queue.get(),
                    timeout=STREAM_HEARTBEAT_SECONDS,
                )
            except asyncio.TimeoutError:
                yield build_sse_comment()
                continue
            yield _sse_for_event(record.event, record.event_id)
            if _is_terminal_event(record.event):
                break
    finally:
        _unsubscribe(state, queue)


def _reset_agent_stream_registry_for_tests() -> None:
    with _STREAMS_LOCK:
        tasks = [state.task for state in _STREAMS.values() if state.task is not None]
        _STREAMS.clear()
    for task in tasks:
        if not task.done():
            task.cancel()


__all__ = ["stream_agent_chat"]
