import asyncio
import json

import pytest

from python_rag.app.agent import orchestrator
from python_rag.app.agent.memory.schemas import SessionMemory
from python_rag.app.agent.schemas import AgentChatRequest
from python_rag.app.agent.streaming import agent_streaming_service as streaming
from python_rag.app.agent.tools.registry import ToolRegistry
from python_rag.app.api.v1.routers import agent_router as agent_api
from test_agent_orchestrator import FakeKnowledgeSearchTool, FakeTraceRecorder, _patch_trace, _tool_call
from test_agent_streaming_service import _patch_persistence


def _patch_runtime(monkeypatch, route, knowledge_tool=None):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = knowledge_tool or FakeKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    classification_calls = []
    main_calls = []
    monkeypatch.setattr(orchestrator.session_memory, "load_session_memory", lambda **kwargs: SessionMemory())
    monkeypatch.setattr(orchestrator.llm_service, "ROUTER_LLM_MODEL", "test-low-model")
    monkeypatch.setattr(orchestrator.llm_service, "ROUTER_LLM_TIMEOUT_SECONDS", 5)

    def classify(messages):
        classification_calls.append(messages)
        if route == "fallback":
            raise orchestrator.llm_service.LLMServiceError("router request failed")
        return {"answer": json.dumps({"route": route}), "usage": {"total_tokens": 9}}

    def answer(messages, tools=None, tool_choice=None):
        main_calls.append(messages)
        if route == "fallback" and not any(item["role"] == "tool" for item in messages):
            return {"tool_calls": [_tool_call({"query": "系统架构"})]}
        return {"answer": "回答正文", "usage": {"total_tokens": 20}}

    monkeypatch.setattr(orchestrator.llm_service, "generate_routing_decision", classify)
    monkeypatch.setattr(orchestrator.llm_service, "generate_from_messages", answer)
    factory = lambda: orchestrator.AgentOrchestrator(registry=registry)
    monkeypatch.setattr(streaming, "AgentOrchestrator", factory)
    monkeypatch.setattr(agent_api, "AgentOrchestrator", factory)
    return classification_calls, main_calls, knowledge_tool


def _parse_events(raw_events):
    return [
        json.loads(line[6:])
        for event in raw_events for line in event.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.parametrize("route", ["rag", "agent", "fallback"])
def test_sync_and_stream_share_routing_and_persist_before_done(monkeypatch, route):
    streaming._reset_agent_stream_registry_for_tests()
    created_messages, saved_citations = _patch_persistence(monkeypatch)
    monkeypatch.setattr(agent_api, "get_session_by_id", lambda session_id: {"id": session_id})
    monkeypatch.setattr(agent_api, "create_message", streaming.create_message)
    monkeypatch.setattr(agent_api, "bulk_insert_citations", streaming.bulk_insert_citations)
    classification_calls, _, knowledge_tool = _patch_runtime(monkeypatch, route)
    original_append = streaming._append_event

    def append(state, event):
        if event["type"] == "done":
            assert [item["role"] for item in created_messages] == ["user", "assistant", "user", "assistant"]
            assert len(saved_citations) == (0 if route == "agent" else 2)
            assert created_messages[-1]["meta"]["routing"] == event["meta"]["routing"]
        return original_append(state, event)

    monkeypatch.setattr(streaming, "_append_event", append)

    async def run():
        response = await agent_api.agent_chat(
            AgentChatRequest(session_id=1, message="继续解释上面的模块"), last_event_id=None,
        )
        events = [event async for event in streaming.stream_agent_chat(
            session_id=2, message="继续解释上面的模块", trace_id="routing-transport",
        )]
        return response, _parse_events(events)

    try:
        response, events = asyncio.run(run())
    finally:
        streaming._reset_agent_stream_registry_for_tests()
    assert response["code"] == 0
    final = next(item for item in events if item["type"] == "final")
    assert events[-1]["type"] == "done"
    assert final["answer"] == response["data"]["answer"]
    assert final["citations"] == response["data"]["citations"]
    sync_routing = response["data"]["routing"]
    assert sync_routing["route"] == route
    assert {key: value for key, value in sync_routing.items() if key != "latency_ms"} == {
        key: value for key, value in final["routing"].items() if key != "latency_ms"
    }
    assert events[-1]["meta"]["routing"] == final["routing"]
    assert [event["event_id"] for event in events] == list(range(1, len(events) + 1))
    assert len(classification_calls) == 2
    assert len(knowledge_tool.calls) == (0 if route == "agent" else 2)
    assert created_messages[1]["meta"]["routing"] == sync_routing


def test_disconnect_and_resume_do_not_repeat_classification_or_retrieval(monkeypatch):
    streaming._reset_agent_stream_registry_for_tests()
    created_messages, saved_citations = _patch_persistence(monkeypatch)
    released = asyncio.Event()

    class GatedKnowledgeTool(FakeKnowledgeSearchTool):
        async def run(self, arguments):
            await released.wait()
            return await super().run(arguments)

    classification_calls, main_calls, knowledge_tool = _patch_runtime(
        monkeypatch, "rag", knowledge_tool=GatedKnowledgeTool(),
    )

    async def run():
        stream = streaming.stream_agent_chat(session_id=3, message="继续解释上面的模块", trace_id="routing-resume")
        first = await stream.__anext__()
        await stream.aclose()
        state = streaming._STREAMS[streaming._stream_key(3, "继续解释上面的模块", "routing-resume")]
        assert not state.task.done()
        released.set()
        await state.task
        replay = [event async for event in streaming.stream_agent_chat(
            session_id=3, message="继续解释上面的模块", trace_id="routing-resume", last_event_id="1",
        )]
        return first, _parse_events(replay)

    try:
        first, replay = asyncio.run(run())
    finally:
        streaming._reset_agent_stream_registry_for_tests()
    assert first.startswith("id: 1\n")
    assert replay[0]["event_id"] == 2
    assert replay[-1]["type"] == "done"
    assert replay[-1]["meta"]["routing"]["route"] == "rag"
    assert len(classification_calls) == len(main_calls) == len(knowledge_tool.calls) == 1
    assert len(created_messages) == 2
    assert len(saved_citations) == 1


def test_expired_resume_does_not_start_classification(monkeypatch):
    streaming._reset_agent_stream_registry_for_tests()
    monkeypatch.setattr(streaming, "AgentOrchestrator", lambda: pytest.fail("expired state must not restart work"))

    async def run():
        return [event async for event in streaming.stream_agent_chat(
            session_id=4, message="继续解释上面的模块", trace_id="expired-routing", last_event_id="9",
        )]

    try:
        events = _parse_events(asyncio.run(run()))
    finally:
        streaming._reset_agent_stream_registry_for_tests()
    assert events[0]["type"] == "error"
    assert events[0]["event_id"] == 10
