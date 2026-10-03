import asyncio
import json
import runpy
import threading
import time
from pathlib import Path

import dotenv
import pytest
import requests

from python_rag.app.agent import intent_router
from python_rag.app.agent.memory.schemas import SessionMemory
from python_rag.app.modules.llm import service as llm_service


@pytest.fixture(autouse=True)
def router_config(monkeypatch):
    monkeypatch.setattr(llm_service, "LLM_ENABLE", True)
    monkeypatch.setattr(llm_service, "ROUTER_LLM_MODEL", "test-low-model")
    monkeypatch.setattr(llm_service, "ROUTER_LLM_BASE_URL", "http://router.test/v1")
    monkeypatch.setattr(llm_service, "ROUTER_LLM_API_KEY", "router-test-key")
    monkeypatch.setattr(llm_service, "ROUTER_LLM_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(llm_service, "ROUTER_LLM_MAX_TOKENS", 96)
    monkeypatch.setattr(llm_service, "ROUTER_LLM_TOKEN_LIMIT_FIELD", "max_tokens")


@pytest.mark.parametrize("question,route", [
    ("根据项目文档总结系统架构", "rag"),
    ("从知识库中查询支付模块", "rag"),
    ("参考上传的资料解释部署流程", "rag"),
    ("文档里支持哪些能力？", "rag"),
    ("Summarize based on the project docs", "rag"),
    ("你好！", "agent"),
    ("  HELLO  ", "agent"),
    ("当前知识库有哪些文档？", "agent"),
    ("列出知识库中的文档", "agent"),
    ("查看 document_id=7 的文档详情", "agent"),
    ("查看 message_id=42 的已保存引用", "agent"),
    ("list indexed documents", "agent"),
    ("show document 7 details", "agent"),
    ("list citations for message 42", "agent"),
])
def test_confident_rules_skip_model(monkeypatch, question, route):
    def unexpected_model_call(messages):
        pytest.fail("confident rules must not call the low model")

    monkeypatch.setattr(llm_service, "generate_routing_decision", unexpected_model_call)
    decision = asyncio.run(intent_router.route_question(question))
    assert decision.route == route
    assert decision.source == "rule"
    assert decision.model is None
    assert decision.usage == {}


@pytest.mark.parametrize("question", [
    "什么是 embedding？", "怎么优化代码？", "介绍系统架构设计原则", "如何实现向量库？",
    "解释 Python 模块", "如何编写项目文档？", "继续解释上面的模块",
    "列出文档并根据项目文档总结架构", "你好，请解释 embedding",
    "how does a vector index work?", "explain source code optimization",
    "list indexed documents and summarize based on the project docs",
])
def test_ambiguous_questions_use_model(monkeypatch, question):
    calls = []

    def classify(messages):
        calls.append(messages)
        return {"answer": '{"route":"agent"}', "usage": {"total_tokens": 15}}

    monkeypatch.setattr(llm_service, "generate_routing_decision", classify)
    decision = asyncio.run(intent_router.route_question(question))
    assert len(calls) == 1
    assert decision.route == "agent"
    assert decision.source == "model"
    assert decision.model == "test-low-model"
    assert decision.usage == {"total_tokens": 15}


def test_classifier_receives_bounded_context(monkeypatch):
    memory = SessionMemory(
        summary="摘要" * 700,
        user_memory="不应传给分类器的长期记忆",
        recent_messages=[
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index) * 600}
            for index in range(6)
        ],
    )
    calls = []

    def classify(messages):
        calls.append(messages)
        return {"answer": '{"route":"rag"}'}

    monkeypatch.setattr(llm_service, "generate_routing_decision", classify)
    decision = asyncio.run(intent_router.route_question("继续解释上面的模块", memory))
    assert decision.route == "rag"
    payload = json.loads(calls[0][1]["content"])
    assert payload["question"] == "继续解释上面的模块"
    assert len(payload["summary"]) == 1000
    assert sum(len(item["content"]) for item in payload["history"]) == 2000
    assert [item["content"][0] for item in payload["history"]] == ["2", "3", "4", "5"]
    assert "不应传给分类器" not in json.dumps(calls, ensure_ascii=False)
    assert len(calls[0]) == 2


@pytest.mark.parametrize("answer", [
    "not json", "[]", "null", "{}", '{"route":"unknown"}', '{"route":true}',
    '{"route":"rag","answer":"invented"}', '```json\n{"route":"rag"}\n```',
])
def test_invalid_model_output_falls_back_and_retains_usage(monkeypatch, answer):
    monkeypatch.setattr(llm_service, "generate_routing_decision", lambda messages: {
        "answer": answer, "usage": {"total_tokens": 12},
    })
    decision = asyncio.run(intent_router.route_question("解释 embedding"))
    assert decision.route == "fallback"
    assert decision.reason == "invalid_model_output"
    assert decision.usage == {"total_tokens": 12}


@pytest.mark.parametrize("extra", [
    {"tool_calls": [{"id": "unexpected"}]}, {"finish_reason": "length"},
])
def test_tool_calls_and_truncated_classifications_fall_back(monkeypatch, extra):
    monkeypatch.setattr(llm_service, "generate_routing_decision", lambda messages: {
        "answer": '{"route":"rag"}', **extra,
    })
    decision = asyncio.run(intent_router.route_question("继续解释"))
    assert decision.route == "fallback"
    assert decision.reason == "invalid_model_output"


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "choices": [{"message": {"content": '{"route":"agent"}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
        }


def test_low_model_request_is_independent_and_single_round(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_service, "LLM_MODEL", "main-model")
    monkeypatch.setattr(llm_service, "LLM_MAX_GENERATION_ROUNDS", 3)
    monkeypatch.setattr(llm_service, "ROUTER_LLM_TOKEN_LIMIT_FIELD", "max_completion_tokens")

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(llm_service.http_client, "post", post)
    decision = asyncio.run(intent_router.route_question("解释 embedding"))
    assert decision.route == "agent"
    assert decision.usage["total_tokens"] == 12
    assert len(calls) == 1
    request = calls[0]
    assert request["url"] == "http://router.test/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer router-test-key"
    assert request["timeout"] == 5
    assert request["allow_redirects"] is False
    assert request["json"]["model"] == "test-low-model"
    assert request["json"]["max_completion_tokens"] == 96
    assert request["json"]["temperature"] == 0
    assert request["json"]["stream"] is False
    assert "tools" not in request["json"]
    assert "tool_choice" not in request["json"]
    assert "[[LLM_DONE]]" not in json.dumps(request["json"])
    assert llm_service.LLM_MODEL == "main-model"


@pytest.mark.parametrize("failure,reason", [
    (requests.Timeout("secret-address"), "model_timeout"),
    (requests.ConnectionError("secret-key"), "model_request_failed"),
    (503, "model_request_failed"),
    (302, "model_request_failed"),
    ("invalid_response", "model_request_failed"),
])
def test_request_failures_do_not_retry_or_expose_details(monkeypatch, failure, reason):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        if isinstance(failure, Exception):
            raise failure
        response = FakeResponse()
        if isinstance(failure, int):
            response.status_code = failure
        else:
            response.json = lambda: {"choices": []}
        return response

    monkeypatch.setattr(llm_service.http_client, "post", post)
    decision = asyncio.run(intent_router.route_question("解释 embedding"))
    assert decision.route == "fallback"
    assert decision.reason == reason
    assert len(calls) == 1
    assert "secret" not in json.dumps(decision.to_dict())


def test_missing_model_does_not_use_main_model(monkeypatch):
    monkeypatch.setattr(llm_service, "ROUTER_LLM_MODEL", "")
    monkeypatch.setattr(llm_service, "LLM_MODEL", "main-model")
    monkeypatch.setattr(llm_service.http_client, "post", lambda *args, **kwargs: pytest.fail("unexpected HTTP"))
    decision = asyncio.run(intent_router.route_question("解释 embedding"))
    assert decision.route == "fallback"
    assert decision.reason == "model_not_configured"
    assert decision.model is None


def test_classification_runs_off_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()
    released = threading.Event()

    def classify(messages):
        assert threading.get_ident() != event_loop_thread
        assert released.wait(1)
        return {"answer": '{"route":"agent"}'}

    monkeypatch.setattr(llm_service, "generate_routing_decision", classify)

    async def run():
        task = asyncio.create_task(intent_router.route_question("解释 embedding"))
        await asyncio.sleep(0)
        released.set()
        return await task

    assert asyncio.run(run()).route == "agent"


def test_classification_has_overall_timeout(monkeypatch):
    monkeypatch.setattr(llm_service, "ROUTER_LLM_TIMEOUT_SECONDS", 0.001)

    def classify(messages):
        time.sleep(0.03)
        return {"answer": '{"route":"rag"}'}

    monkeypatch.setattr(llm_service, "generate_routing_decision", classify)
    decision = asyncio.run(intent_router.route_question("解释 embedding"))
    assert decision.route == "fallback"
    assert decision.reason == "model_timeout"


@pytest.mark.parametrize("independent", [False, True])
def test_router_connection_config_inheritance(monkeypatch, independent):
    monkeypatch.setattr(dotenv, "load_dotenv", lambda: None)
    monkeypatch.setenv("MIMO_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "https://main.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "main-key")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    monkeypatch.setenv("LLM_TOKEN_LIMIT_FIELD", "max_completion_tokens")
    monkeypatch.setenv("ROUTER_LLM_MODEL", "")
    monkeypatch.setenv("ROUTER_LLM_BASE_URL", "https://low.test/v1/" if independent else "")
    monkeypatch.setenv("ROUTER_LLM_API_KEY", "low-key" if independent else "")
    monkeypatch.setenv("ROUTER_LLM_TOKEN_LIMIT_FIELD", "max_tokens" if independent else "")
    monkeypatch.delenv("ROUTER_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("ROUTER_LLM_MAX_TOKENS", raising=False)
    config = runpy.run_path(str(Path(__file__).resolve().parents[1] / "python_rag/app/core/config.py"))
    assert config["ROUTER_LLM_MODEL"] == ""
    assert config["ROUTER_LLM_BASE_URL"] == ("https://low.test/v1" if independent else "https://main.test/v1")
    assert config["ROUTER_LLM_API_KEY"] == ("low-key" if independent else "main-key")
    assert config["ROUTER_LLM_TOKEN_LIMIT_FIELD"] == ("max_tokens" if independent else "max_completion_tokens")
    assert config["ROUTER_LLM_TIMEOUT_SECONDS"] == 5
    assert config["ROUTER_LLM_MAX_TOKENS"] == 96
