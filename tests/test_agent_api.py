from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from python_rag.app.api.v1.routers import agent_router as agent_api
from python_rag.app.core.errors import AppError
from python_rag.app.core.exception_handlers import app_error_handler


api_test_app = FastAPI()
api_test_app.add_exception_handler(AppError, app_error_handler)
api_test_app.include_router(agent_api.router)
client = TestClient(api_test_app)


class FakeAgentOrchestrator:
    calls = []
    routing = {"route": "rag", "source": "rule", "reason": "project_document_code_intent"}

    async def run(self, question, session_id=None, user_message_id=None, trace_id=None):
        self.calls.append(
            {
                "question": question,
                "session_id": session_id,
                "user_message_id": user_message_id,
                "trace_id": trace_id,
            }
        )
        return {
            "run_id": 1001,
            "answer": "项目架构包含网关、FastAPI、Celery 和知识库检索。",
            "citations": [
                {
                    "doc_id": 7,
                    "chunk_id": 11,
                    "chunk_index": 0,
                    "score": 0.91,
                    "snippet": "架构说明",
                }
            ],
            "steps_used": 2,
            "routing": self.routing,
        }


def test_agent_chat_endpoint_calls_agent_and_returns_answer(monkeypatch):
    FakeAgentOrchestrator.calls = []
    created_messages = []
    saved_citations = []

    monkeypatch.setattr(
        agent_api,
        "get_session_by_id",
        lambda session_id: {"id": session_id, "user_id": 1},
    )

    def fake_create_message(session_id, role, content, status="SUCCESS", meta=None, **kwargs):
        message_id = 3000 + len(created_messages) + 1
        row = {
            "message_id": message_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "status": status,
            "meta": meta or {},
        }
        created_messages.append(row)
        return row

    monkeypatch.setattr(agent_api, "create_message", fake_create_message)
    monkeypatch.setattr(agent_api, "AgentOrchestrator", FakeAgentOrchestrator)
    monkeypatch.setattr(
        agent_api,
        "bulk_insert_citations",
        lambda message_id, hits: saved_citations.append(
            {"message_id": message_id, "hits": hits}
        ),
    )

    response = client.post(
        "/internal/agent/chat",
        json={
            "session_id": 1,
            "message": "根据知识库总结项目架构",
            "stream": False,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "ok",
        "data": {
            "run_id": 1001,
            "message_id": 3002,
            "answer": "项目架构包含网关、FastAPI、Celery 和知识库检索。",
            "citations": [
                {
                    "doc_id": 7,
                    "chunk_id": 11,
                    "chunk_index": 0,
                    "score": 0.91,
                    "snippet": "架构说明",
                }
            ],
            "retrieval": {},
            "routing": FakeAgentOrchestrator.routing,
        },
    }
    assert created_messages[0]["role"] == "user"
    assert created_messages[0]["content"] == "根据知识库总结项目架构"
    assert created_messages[1]["role"] == "assistant"
    assert created_messages[1]["meta"]["agent_run_id"] == 1001
    assert created_messages[1]["meta"]["citation_count"] == 1
    assert created_messages[1]["meta"]["retrieval"] == {}
    assert created_messages[1]["meta"]["routing"] == FakeAgentOrchestrator.routing
    assert saved_citations == [
        {
            "message_id": 3002,
            "hits": [
                {
                    "doc_id": 7,
                    "chunk_id": 11,
                    "chunk_index": 0,
                    "score": 0.91,
                    "snippet": "架构说明",
                }
            ],
        }
    ]
    assert FakeAgentOrchestrator.calls == [
        {
            "question": "根据知识库总结项目架构",
            "session_id": 1,
            "user_message_id": 3001,
            "trace_id": None,
        }
    ]


def test_agent_chat_stream_returns_sse(monkeypatch):
    monkeypatch.setattr(
        agent_api,
        "get_session_by_id",
        lambda session_id: {"id": session_id, "user_id": 1},
    )

    async def fake_stream_agent_chat(
        session_id, message, trace_id=None, last_event_id=None
    ):
        yield 'event: agent_step\ndata: {"type":"agent_step","step_index":0}\n\n'
        yield 'event: final\ndata: {"type":"final","answer":"hello"}\n\n'
        yield 'data: {"type":"done","meta":{"run_id":1001}}\n\n'

    monkeypatch.setattr(agent_api, "stream_agent_chat", fake_stream_agent_chat)

    response = client.post(
        "/api/agent/chat",
        json={
            "session_id": 1,
            "message": "hello",
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type":"agent_step"' in response.text
    assert '"type":"final"' in response.text
    assert '"type":"done"' in response.text


def test_get_agent_run_endpoint(monkeypatch):
    monkeypatch.setattr(
        agent_api,
        "get_agent_run",
        lambda run_id: {
            "id": run_id,
            "agent_name": "rag-agent",
            "status": "SUCCESS",
            "input_json": {"question": "hello"},
            "output_json": {"answer": "world"},
            "created_at": datetime(2026, 5, 26, 12, 0, 0),
        },
    )

    response = client.get("/internal/agent/runs/1001")

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "ok",
        "data": {
            "run": {
                "id": 1001,
                "agent_name": "rag-agent",
                "status": "SUCCESS",
                "input_json": {"question": "hello"},
                "output_json": {"answer": "world"},
                "created_at": "2026-05-26T12:00:00",
            }
        },
    }


def test_get_agent_run_steps_endpoint(monkeypatch):
    monkeypatch.setattr(
        agent_api,
        "get_agent_run",
        lambda run_id: {"id": run_id, "status": "SUCCESS"},
    )
    monkeypatch.setattr(
        agent_api,
        "list_agent_steps",
        lambda run_id: [
            {
                "id": 21,
                "run_id": run_id,
                "step_index": 0,
                "status": "SUCCESS",
                "decision": "tool_call",
            },
            {
                "id": 22,
                "run_id": run_id,
                "step_index": 1,
                "status": "SUCCESS",
                "decision": "final_answer",
            },
        ],
    )
    monkeypatch.setattr(
        agent_api,
        "list_agent_tool_calls",
        lambda run_id: [
            {
                "id": 31,
                "run_id": run_id,
                "step_id": 21,
                "tool_name": "knowledge_search",
                "status": "SUCCESS",
                "arguments_json": {"query": "hello"},
                "result_json": {"total": 1},
            }
        ],
    )

    response = client.get("/internal/agent/runs/1001/steps")

    assert response.status_code == 200
    assert response.json() == {
        "code": 0,
        "message": "ok",
        "data": {
            "run_id": 1001,
            "steps": [
                {
                    "id": 21,
                    "run_id": 1001,
                    "step_index": 0,
                    "status": "SUCCESS",
                    "decision": "tool_call",
                    "tool_calls": [
                        {
                            "id": 31,
                            "run_id": 1001,
                            "step_id": 21,
                            "tool_name": "knowledge_search",
                            "status": "SUCCESS",
                            "arguments_json": {"query": "hello"},
                            "result_json": {"total": 1},
                        }
                    ],
                },
                {
                    "id": 22,
                    "run_id": 1001,
                    "step_index": 1,
                    "status": "SUCCESS",
                    "decision": "final_answer",
                    "tool_calls": [],
                },
            ],
        },
    }


def test_agent_router_is_in_unified_router_registry():
    from python_rag.app.api.v1.routers import ROUTER_MODULES

    assert "python_rag.app.api.v1.routers.agent_router" in ROUTER_MODULES


def test_agent_routes_register_internal_primary_and_api_compatibility():
    paths = {route.path for route in api_test_app.routes}
    chat_router_source = Path("python_rag/app/api/v1/routers/chat_router.py").read_text(
        encoding="utf-8",
    )

    assert "/internal/agent/chat" in paths
    assert "/internal/agent/chat/stream" in paths
    assert "/internal/agent/runs/{run_id}" in paths
    assert "/internal/agent/runs/{run_id}/steps" in paths
    assert "/api/agent/chat" in paths
    assert "/api/agent/chat/stream" in paths
    assert 'APIRouter(prefix="/internal/jobs"' in chat_router_source
    assert '@router.post("/chat", response_model=ApiResponse)' in chat_router_source


def test_agent_openapi_uses_internal_api_response_schema():
    paths = api_test_app.openapi()["paths"]
    response_schema = paths["/internal/agent/chat"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]

    assert response_schema == {"$ref": "#/components/schemas/ApiResponse"}
    assert "/internal/agent/runs/{run_id}" in paths
    assert "/api/agent/chat" not in paths
