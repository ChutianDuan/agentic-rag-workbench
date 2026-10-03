import asyncio
import json

import pytest

from python_rag.app.agent import orchestrator
from python_rag.app.agent.tools.base import BaseTool
from python_rag.app.agent.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def disable_external_router(monkeypatch):
    # 未显式模拟分类器的测试使用未配置降级，不读取开发者的远端模型配置。
    monkeypatch.setattr(orchestrator.llm_service, "ROUTER_LLM_MODEL", "")


class FakeKnowledgeSearchTool(BaseTool):
    name = "knowledge_search"
    description = "Search test knowledge."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    timeout_ms = 1000
    permission_level = "readonly"

    def __init__(self):
        self.calls = []
        super().__init__()

    async def run(self, arguments: dict) -> dict:
        self.calls.append(arguments)
        return {
            "results": [
                {
                    "chunk_id": 1,
                    "chunk_index": 0,
                    "doc_id": 7,
                    "document_id": 7,
                    "title": "architecture.md",
                    "content": "项目架构包含 C++ Gateway、FastAPI Internal Service、Celery Worker 和知识库检索。",
                    "score": 0.91,
                }
            ],
            "total": 1,
        }


class EmptyKnowledgeSearchTool(FakeKnowledgeSearchTool):
    async def run(self, arguments: dict) -> dict:
        self.calls.append(arguments)
        return {
            "results": [],
            "total": 0,
        }


class TimeoutKnowledgeSearchTool(FakeKnowledgeSearchTool):
    async def run(self, arguments: dict) -> dict:
        self.calls.append(arguments)
        return {
            "results": [],
            "total": 0,
            "error": "knowledge_search timeout",
        }


class SlowKnowledgeSearchTool(FakeKnowledgeSearchTool):
    timeout_ms = 1

    async def run(self, arguments: dict) -> dict:
        self.calls.append(arguments)
        await asyncio.sleep(0.05)
        return {
            "results": [],
            "total": 0,
        }


class FakeTraceRecorder:
    def __init__(self):
        self.runs = []
        self.finished_runs = []
        self.failed_runs = []
        self.steps = []
        self.finished_steps = []
        self.tool_calls = []
        self.finished_tool_calls = []
        self.failed_tool_calls = []

    def create_run(self, **kwargs):
        self.runs.append(kwargs)
        return 101

    def finish_run(self, **kwargs):
        self.finished_runs.append(kwargs)

    def fail_run(self, **kwargs):
        self.failed_runs.append(kwargs)

    def create_step(self, **kwargs):
        self.steps.append(kwargs)
        return 200 + len(self.steps)

    def finish_step(self, **kwargs):
        self.finished_steps.append(kwargs)

    def create_tool_call(self, **kwargs):
        self.tool_calls.append(kwargs)
        return 300 + len(self.tool_calls)

    def finish_tool_call(self, **kwargs):
        self.finished_tool_calls.append(kwargs)

    def fail_tool_call(self, **kwargs):
        self.failed_tool_calls.append(kwargs)


def _patch_trace(monkeypatch, recorder):
    monkeypatch.setattr(orchestrator.trace_service, "create_run", recorder.create_run)
    monkeypatch.setattr(orchestrator.trace_service, "finish_run", recorder.finish_run)
    monkeypatch.setattr(orchestrator.trace_service, "fail_run", recorder.fail_run)
    monkeypatch.setattr(orchestrator.trace_service, "create_step", recorder.create_step)
    monkeypatch.setattr(orchestrator.trace_service, "finish_step", recorder.finish_step)
    monkeypatch.setattr(
        orchestrator.trace_service,
        "create_tool_call",
        recorder.create_tool_call,
    )
    monkeypatch.setattr(
        orchestrator.trace_service,
        "finish_tool_call",
        recorder.finish_tool_call,
    )
    monkeypatch.setattr(
        orchestrator.trace_service,
        "fail_tool_call",
        recorder.fail_tool_call,
    )


def _tool_call(arguments, call_id="call_knowledge_1", name="knowledge_search"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def test_agent_orchestrator_forces_knowledge_search_for_document_intent(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        assert any(message["role"] == "tool" for message in messages)
        tool_message = [message for message in messages if message["role"] == "tool"][0]
        assert "项目架构包含" in tool_message["content"]
        return {
            "answer": "项目架构包含 C++ Gateway、FastAPI、Celery 和知识库检索。",
            "message": {
                "content": "项目架构包含 C++ Gateway、FastAPI、Celery 和知识库检索。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            "latency_ms": 12,
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    events = []

    result = asyncio.run(
        orchestrator.AgentOrchestrator(
            registry=registry,
            max_steps=3,
        ).run("根据项目文档总结系统架构", event_sink=events.append)
    )

    assert result["run_id"] == 101
    assert result["answer"] == "项目架构包含 C++ Gateway、FastAPI、Celery 和知识库检索。"
    assert result["citations"] == [
        {
            "rank": 1,
            "doc_id": 7,
            "chunk_id": 1,
            "chunk_index": 0,
            "score": 0.91,
            "snippet": "项目架构包含 C++ Gateway、FastAPI Internal Service、Celery Worker 和知识库检索。",
            "content": "项目架构包含 C++ Gateway、FastAPI Internal Service、Celery Worker 和知识库检索。",
            "title": "architecture.md",
        }
    ]
    assert result["steps_used"] == 2
    assert knowledge_tool.calls == [{"query": "根据项目文档总结系统架构"}]
    assert len(llm_calls) == 1
    assert llm_calls[0]["tool_choice"] == "auto"
    assert [tool["function"]["name"] for tool in llm_calls[0]["tools"]] == [
        "knowledge_search",
    ]

    assert len(recorder.runs) == 1
    assert recorder.runs[0]["agent_version"] == "rag-agent-v1"
    assert recorder.runs[0]["input_data"] == {"question": "根据项目文档总结系统架构"}
    assert recorder.runs[0]["meta"]["agent_version"] == "rag-agent-v1"
    assert recorder.runs[0]["meta"]["prompt_version"] == "rag-agent-prompt-v1"
    assert recorder.runs[0]["meta"]["retrieval_router"] == {
        "force_knowledge_search": True,
        "reason": "project_document_code_intent",
    }
    assert len(recorder.steps) == 2
    assert len(recorder.finished_steps) == 2
    assert recorder.finished_steps[0]["decision"] == "forced_tool_call"
    assert recorder.finished_steps[1]["decision"] == "final_answer"
    assert len(recorder.tool_calls) == 1
    assert recorder.tool_calls[0]["tool_name"] == "knowledge_search"
    assert recorder.tool_calls[0]["tool_call_id"] == "forced_knowledge_search_0"
    assert len(recorder.finished_tool_calls) == 1
    assert recorder.finished_tool_calls[0]["result"]["ok"] is True
    assert recorder.finished_tool_calls[0]["result"]["data"]["total"] == 1
    assert recorder.finished_runs[0]["run_id"] == 101
    assert recorder.finished_runs[0]["prompt_tokens"] == 12
    assert recorder.finished_runs[0]["completion_tokens"] == 8
    assert recorder.finished_runs[0]["total_tokens"] == 20
    assert recorder.finished_runs[0]["output_data"]["answer"] == result["answer"]
    assert recorder.finished_runs[0]["output_data"]["citations"] == result["citations"]
    assert recorder.failed_runs == []

    event_types = [event["type"] for event in events]
    assert "agent_step" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types


def test_agent_orchestrator_allows_multiple_tool_rounds_before_final_answer(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        if len(llm_calls) == 1:
            return {
                "answer": "先查系统架构。",
                "message": {
                    "content": "先查系统架构。",
                    "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                },
                "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                "usage": {},
            }

        if len(llm_calls) == 2:
            tool_messages = [message for message in messages if message["role"] == "tool"]
            assert len(tool_messages) == 1
            assert tools is not None
            assert tool_choice == "auto"
            return {
                "answer": "还需要查 Celery。",
                "message": {
                    "content": "还需要查 Celery。",
                    "tool_calls": [
                        _tool_call(
                            {"query": "Celery Worker", "top_k": 5},
                            call_id="call_knowledge_2",
                        )
                    ],
                },
                "tool_calls": [
                    _tool_call(
                        {"query": "Celery Worker", "top_k": 5},
                        call_id="call_knowledge_2",
                    )
                ],
                "usage": {},
            }

        tool_messages = [message for message in messages if message["role"] == "tool"]
        assert len(tool_messages) == 2
        return {
            "answer": "项目架构和 Celery Worker 信息已补齐。",
            "message": {
                "content": "项目架构和 Celery Worker 信息已补齐。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=3).run(
            "multi round search fixture"
        )
    )

    assert result["answer"] == "项目架构和 Celery Worker 信息已补齐。"
    assert result["steps_used"] == 3
    assert knowledge_tool.calls == [
        {"query": "系统架构", "top_k": 5},
        {"query": "Celery Worker", "top_k": 5},
    ]
    assert len(llm_calls) == 3
    assert [tool["function"]["name"] for tool in llm_calls[1]["tools"]] == [
        "knowledge_search",
    ]
    assert llm_calls[1]["tool_choice"] == "auto"
    assert [step["decision"] for step in recorder.finished_steps] == [
        "tool_call",
        "tool_call",
        "final_answer",
    ]
    assert len(recorder.tool_calls) == 2
    assert recorder.tool_calls[1]["tool_call_id"] == "call_knowledge_2"
    assert recorder.finished_runs


def test_agent_orchestrator_answers_greeting_without_tool(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return {
            "answer": "你好，我可以帮你基于项目文档回答问题。",
            "message": {
                "content": "你好，我可以帮你基于项目文档回答问题。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 8, "total_tokens": 15},
            "latency_ms": 9,
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    events = []
    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=3).run(
            "你好",
            event_sink=events.append,
        )
    )

    assert result["answer"] == "你好，我可以帮你基于项目文档回答问题。"
    assert result["steps_used"] == 1
    assert knowledge_tool.calls == []
    assert recorder.tool_calls == []
    assert recorder.finished_steps[0]["decision"] == "final_answer"
    assert recorder.finished_runs[0]["output_data"]["observations"] == []
    assert [event["type"] for event in events].count("tool_call") == 0
    assert llm_calls[0]["tools"] is None
    assert llm_calls[0]["tool_choice"] is None
    assert result["routing"]["route"] == "agent"
    assert result["routing"]["source"] == "rule"


def test_agent_orchestrator_reports_insufficient_evidence_when_search_empty(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = EmptyKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(messages)
        if len(llm_calls) == 1:
            return {
                "answer": "先检索知识库。",
                "message": {
                    "content": "先检索知识库。",
                    "tool_calls": [_tool_call({"query": "区块链支付模块", "top_k": 5})],
                },
                "tool_calls": [_tool_call({"query": "区块链支付模块", "top_k": 5})],
                "usage": {},
            }

        tool_message = [message for message in messages if message["role"] == "tool"][0]
        assert '"total": 0' in tool_message["content"]
        return {
            "answer": "当前知识库证据不足，无法确认文档里有区块链支付模块。",
            "message": {
                "content": "当前知识库证据不足，无法确认文档里有区块链支付模块。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=3).run(
            "empty search fixture"
        )
    )

    assert "证据不足" in result["answer"]
    assert knowledge_tool.calls == [{"query": "区块链支付模块", "top_k": 5}]
    assert recorder.finished_tool_calls[0]["result"] == {
        "ok": True,
        "error": None,
        "data": {"results": [], "total": 0},
    }
    assert recorder.failed_tool_calls == []
    assert recorder.finished_steps[0]["decision"] == "tool_call"
    assert recorder.finished_steps[1]["decision"] == "final_answer"


def test_agent_orchestrator_records_tool_error_result_as_failed_trace(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = TimeoutKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(messages)
        if len(llm_calls) == 1:
            return {
                "answer": "",
                "message": {
                    "content": "",
                    "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                },
                "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                "usage": {},
            }

        tool_message = [message for message in messages if message["role"] == "tool"][0]
        assert "knowledge_search timeout" in tool_message["content"]
        return {
            "answer": "检索工具超时，已降级返回：当前无法可靠基于知识库总结系统架构。",
            "message": {
                "content": "检索工具超时，已降级返回：当前无法可靠基于知识库总结系统架构。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    events = []
    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=3).run(
            "timeout search fixture",
            event_sink=events.append,
        )
    )

    assert "检索工具超时" in result["answer"]
    assert knowledge_tool.calls == [{"query": "系统架构", "top_k": 5}]
    assert recorder.finished_tool_calls == []
    assert len(recorder.failed_tool_calls) == 1
    assert recorder.failed_tool_calls[0]["error_message"] == "knowledge_search timeout"
    assert recorder.failed_tool_calls[0]["result"]["ok"] is False
    assert recorder.failed_tool_calls[0]["result"]["error"] == "knowledge_search timeout"
    assert recorder.failed_tool_calls[0]["result"]["data"] == {"results": [], "total": 0}
    failed_events = [
        event
        for event in events
        if event["type"] == "tool_result" and event["status"] == "FAILED"
    ]
    assert failed_events[0]["error_message"] == "knowledge_search timeout"
    assert recorder.finished_runs


def test_agent_orchestrator_records_tool_failure_and_continues(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    registry = ToolRegistry([FakeKnowledgeSearchTool()])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(messages)
        if len(llm_calls) == 1:
            return {
                "answer": "",
                "message": {
                    "content": "",
                    "tool_calls": [_tool_call({"query": "x"})],
                },
                "tool_calls": [_tool_call({"query": "x"})],
                "usage": {},
            }
        tool_message = [message for message in messages if message["role"] == "tool"][0]
        assert "permission denied" in tool_message["content"]
        return {
            "answer": "I could not access the tool, so no answer is available.",
            "message": {
                "content": "I could not access the tool, so no answer is available.",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )
    monkeypatch.setattr(
        orchestrator.AgentOrchestrator,
        "_get_readonly_tool",
        lambda self, name: (_ for _ in ()).throw(RuntimeError("permission denied")),
    )

    result = asyncio.run(
        orchestrator.AgentOrchestrator(
            registry=registry,
            max_steps=3,
        ).run("search something")
    )

    assert result["answer"] == "I could not access the tool, so no answer is available."
    assert len(recorder.failed_tool_calls) == 1
    assert recorder.failed_tool_calls[0]["error_message"] == "permission denied"
    assert recorder.failed_tool_calls[0]["result"] == {
        "ok": False,
        "error": "permission denied",
        "data": {},
    }
    assert recorder.finished_runs


def test_agent_orchestrator_enforces_tool_timeout(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = SlowKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(messages)
        if len(llm_calls) == 1:
            return {
                "answer": "",
                "message": {
                    "content": "",
                    "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                },
                "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                "usage": {},
            }

        tool_message = [message for message in messages if message["role"] == "tool"][0]
        assert "tool timeout after 1ms" in tool_message["content"]
        return {
            "answer": "工具超时，无法继续检索。",
            "message": {
                "content": "工具超时，无法继续检索。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=2).run(
            "slow search fixture"
        )
    )

    assert result["answer"] == "工具超时，无法继续检索。"
    assert knowledge_tool.calls == [{"query": "系统架构", "top_k": 5}]
    assert len(recorder.failed_tool_calls) == 1
    assert recorder.failed_tool_calls[0]["error_message"] == "tool timeout after 1ms"
    assert recorder.failed_tool_calls[0]["result"] == {
        "ok": False,
        "error": "tool timeout after 1ms",
        "data": {},
    }
    assert recorder.finished_tool_calls == []
    assert recorder.finished_runs


def test_agent_orchestrator_rejects_invalid_tool_arguments(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(messages)
        if len(llm_calls) == 1:
            return {
                "answer": "",
                "message": {
                    "content": "",
                    "tool_calls": [_tool_call({"query": "系统架构", "top_k": "5"})],
                },
                "tool_calls": [_tool_call({"query": "系统架构", "top_k": "5"})],
                "usage": {},
            }

        tool_message = [message for message in messages if message["role"] == "tool"][0]
        assert "tool argument 'top_k' must be integer" in tool_message["content"]
        return {
            "answer": "工具参数不合法，无法检索。",
            "message": {
                "content": "工具参数不合法，无法检索。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=2).run(
            "invalid arguments fixture"
        )
    )

    assert result["answer"] == "工具参数不合法，无法检索。"
    assert knowledge_tool.calls == []
    assert len(recorder.failed_tool_calls) == 1
    assert recorder.failed_tool_calls[0]["error_message"] == (
        "tool argument 'top_k' must be integer"
    )
    assert recorder.failed_tool_calls[0]["result"] == {
        "ok": False,
        "error": "tool argument 'top_k' must be integer",
        "data": {},
    }
    assert recorder.finished_tool_calls == []
    assert recorder.finished_runs


def test_agent_orchestrator_skips_duplicate_tool_calls(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(messages)
        if len(llm_calls) == 1:
            return {
                "answer": "先检索。",
                "message": {
                    "content": "先检索。",
                    "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                },
                "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                "usage": {},
            }

        if len(llm_calls) == 2:
            return {
                "answer": "重复检索。",
                "message": {
                    "content": "重复检索。",
                    "tool_calls": [
                        _tool_call(
                            {"query": "系统架构", "top_k": 5},
                            call_id="call_duplicate",
                        )
                    ],
                },
                "tool_calls": [
                    _tool_call(
                        {"query": "系统架构", "top_k": 5},
                        call_id="call_duplicate",
                    )
                ],
                "usage": {},
            }

        tool_messages = [message for message in messages if message["role"] == "tool"]
        assert len(tool_messages) == 2
        assert "duplicate tool call skipped" in tool_messages[1]["content"]
        return {
            "answer": "已根据已有检索结果回答。",
            "message": {
                "content": "已根据已有检索结果回答。",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=3).run(
            "duplicate search fixture"
        )
    )

    assert result["answer"] == "已根据已有检索结果回答。"
    assert result["termination_reason"] == "final_answer"
    assert knowledge_tool.calls == [{"query": "系统架构", "top_k": 5}]
    assert len(recorder.tool_calls) == 2
    assert len(recorder.finished_tool_calls) == 1
    assert len(recorder.failed_tool_calls) == 1
    assert recorder.failed_tool_calls[0]["error_message"] == (
        "duplicate tool call skipped: knowledge_search"
    )
    assert recorder.failed_tool_calls[0]["result"] == {
        "ok": False,
        "error": "duplicate tool call skipped: knowledge_search",
        "data": {
            "skipped": True,
            "reason": "duplicate_tool_call",
        },
    }
    assert [step["decision"] for step in recorder.finished_steps] == [
        "tool_call",
        "tool_call",
        "final_answer",
    ]


def test_agent_orchestrator_degrades_when_max_steps_reached(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    registry = ToolRegistry([knowledge_tool])
    llm_calls = []

    def fake_generate_from_messages(messages, tools=None, tool_choice=None):
        llm_calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        if len(llm_calls) == 1:
            return {
                "answer": "先检索。",
                "message": {
                    "content": "先检索。",
                    "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                },
                "tool_calls": [_tool_call({"query": "系统架构", "top_k": 5})],
                "usage": {"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
            }

        assert tools is None
        assert tool_choice is None
        assert messages[-1]["role"] == "system"
        assert "已达到工具调用上限" in messages[-1]["content"]
        return {
            "answer": "",
            "message": {
                "content": "",
                "tool_calls": [],
            },
            "tool_calls": [],
            "usage": {"prompt_tokens": 6, "completion_tokens": 7, "total_tokens": 13},
        }

    monkeypatch.setattr(
        orchestrator.llm_service,
        "generate_from_messages",
        fake_generate_from_messages,
    )

    result = asyncio.run(
        orchestrator.AgentOrchestrator(registry=registry, max_steps=1).run(
            "max steps search fixture"
        )
    )

    assert "已达到工具调用上限" in result["answer"]
    assert result["termination_reason"] == "max_steps"
    assert result["steps_used"] == 2
    assert knowledge_tool.calls == [{"query": "系统架构", "top_k": 5}]
    assert len(llm_calls) == 2
    assert [step["decision"] for step in recorder.finished_steps] == [
        "tool_call",
        "max_steps_final_answer",
    ]
    assert recorder.failed_runs == []
    assert recorder.finished_runs[0]["output_data"]["termination_reason"] == "max_steps"
    assert recorder.finished_runs[0]["prompt_tokens"] == 10
    assert recorder.finished_runs[0]["completion_tokens"] == 12
    assert recorder.finished_runs[0]["total_tokens"] == 22


def _patch_classifier(monkeypatch, route):
    calls = []
    monkeypatch.setattr(orchestrator.llm_service, "ROUTER_LLM_MODEL", "test-low-model")
    monkeypatch.setattr(orchestrator.llm_service, "ROUTER_LLM_TIMEOUT_SECONDS", 5)

    def classify(messages):
        calls.append(messages)
        return {"answer": json.dumps({"route": route}), "usage": {"total_tokens": 11}}

    monkeypatch.setattr(orchestrator.llm_service, "generate_routing_decision", classify)
    return calls


@pytest.mark.parametrize("max_steps", [1, 3])
def test_low_model_rag_route_forces_search_without_consuming_extra_step(monkeypatch, max_steps):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    routing_calls = _patch_classifier(monkeypatch, "rag")
    main_calls = []

    def answer(messages, tools=None, tool_choice=None):
        main_calls.append(tools)
        assert len(knowledge_tool.calls) == 1
        assert any(item["role"] == "tool" for item in messages)
        return {"answer": "项目架构说明", "usage": {"total_tokens": 20}}

    monkeypatch.setattr(orchestrator.llm_service, "generate_from_messages", answer)
    result = asyncio.run(orchestrator.AgentOrchestrator(
        registry=ToolRegistry([knowledge_tool]), max_steps=max_steps,
    ).run("继续解释上面的模块"))
    assert len(routing_calls) == 1
    assert len(main_calls) == 1
    assert result["steps_used"] == 2
    assert knowledge_tool.calls == [{"query": "继续解释上面的模块"}]
    assert result["routing"]["source"] == "model"
    assert result["routing"]["force_knowledge_search"] is True
    assert result["routing"]["usage"] == {"total_tokens": 11}
    assert result["citations"][0]["doc_id"] == 7
    assert recorder.finished_runs[0]["total_tokens"] == 20
    assert recorder.runs[0]["meta"]["routing"] == result["routing"]
    assert recorder.finished_runs[0]["output_data"]["routing"] == result["routing"]
    if max_steps == 1:
        assert main_calls[0] is None
        assert result["termination_reason"] == "max_steps"


class FakeListDocumentsTool(BaseTool):
    name = "list_ready_documents"
    description = "List ready documents."
    input_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    permission_level = "readonly"

    def __init__(self):
        self.calls = []
        super().__init__()

    async def run(self, arguments):
        self.calls.append(arguments)
        return {"documents": [{"doc_id": 7, "title": "architecture.md"}]}


@pytest.mark.parametrize("question,use_model", [("解释 embedding", True), ("列出文档", False)])
def test_agent_route_enforces_search_denial_and_keeps_metadata_tools(monkeypatch, question, use_model):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    document_tool = FakeListDocumentsTool()
    routing_calls = _patch_classifier(monkeypatch, "agent")
    main_calls = []

    def answer(messages, tools=None, tool_choice=None):
        main_calls.append(messages)
        assert [item["function"]["name"] for item in tools] == ["list_ready_documents"]
        assert "禁止调用 knowledge_search" in messages[0]["content"]
        if len(main_calls) == 1:
            # 即使主模型忽略声明并请求检索，执行端也必须拒绝。
            return {"tool_calls": [
                _tool_call({"query": "不允许检索"}),
                _tool_call({}, call_id="list_1", name="list_ready_documents"),
            ]}
        assert any("tool is not allowed by routing" in item["content"] for item in messages if item["role"] == "tool")
        return {"answer": "可用文档为 architecture.md"}

    monkeypatch.setattr(orchestrator.llm_service, "generate_from_messages", answer)
    result = asyncio.run(orchestrator.AgentOrchestrator(
        registry=ToolRegistry([knowledge_tool, document_tool]),
    ).run(question))
    assert len(routing_calls) == int(use_model)
    assert result["routing"]["route"] == "agent"
    assert result["routing"]["force_knowledge_search"] is False
    assert result["citations"] == []
    assert knowledge_tool.calls == []
    assert document_tool.calls == [{}]
    assert recorder.failed_tool_calls[0]["error_message"] == "tool is not allowed by routing: knowledge_search"
    assert len(recorder.finished_tool_calls) == 1


def test_failed_classifier_returns_tool_choice_to_agent(monkeypatch):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()

    def classify(messages):
        raise orchestrator.llm_service.LLMServiceError("router request failed")

    monkeypatch.setattr(orchestrator.llm_service, "generate_routing_decision", classify)
    calls = []

    def answer(messages, tools=None, tool_choice=None):
        calls.append(messages)
        assert [item["function"]["name"] for item in tools] == ["knowledge_search"]
        assert "本次路由降级" in messages[0]["content"]
        if len(calls) == 1:
            assert knowledge_tool.calls == []
            return {"tool_calls": [_tool_call({"query": "系统架构"})]}
        return {"answer": "根据检索结果说明架构"}

    monkeypatch.setattr(orchestrator.llm_service, "generate_from_messages", answer)
    result = asyncio.run(orchestrator.AgentOrchestrator(
        registry=ToolRegistry([knowledge_tool]),
    ).run("继续解释上面的模块"))
    assert result["routing"]["route"] == "fallback"
    assert result["routing"]["reason"] == "model_request_failed"
    assert result["routing"]["force_knowledge_search"] is False
    assert knowledge_tool.calls == [{"query": "系统架构"}]
    assert result["citations"]


@pytest.mark.parametrize("non_readonly", [False, True])
def test_unavailable_search_is_recorded_and_not_forced(monkeypatch, non_readonly):
    recorder = FakeTraceRecorder()
    _patch_trace(monkeypatch, recorder)
    knowledge_tool = FakeKnowledgeSearchTool()
    knowledge_tool.permission_level = "write"
    registry = ToolRegistry([knowledge_tool] if non_readonly else [])

    def answer(messages, tools=None, tool_choice=None):
        assert tools is None
        assert "缺少知识库访问能力" in messages[0]["content"]
        return {"answer": "当前缺少知识库访问能力，无法确认项目文档中的结论。"}

    monkeypatch.setattr(orchestrator.llm_service, "generate_from_messages", answer)
    result = asyncio.run(orchestrator.AgentOrchestrator(registry=registry).run("根据项目文档总结系统架构"))
    assert result["routing"]["route"] == "rag"
    assert result["routing"]["tool_unavailable_reason"] == "knowledge_search_unavailable"
    assert result["routing"]["force_knowledge_search"] is False
    assert result["steps_used"] == 1
    assert knowledge_tool.calls == []
    assert recorder.tool_calls == []
