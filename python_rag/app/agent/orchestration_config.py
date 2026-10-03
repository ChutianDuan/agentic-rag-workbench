from typing import Any, Callable, Dict

from python_rag.app.agent.tools.local.citation_tools import (
    LIST_MESSAGE_CITATIONS_TOOL_NAME,
)
from python_rag.app.agent.tools.local.document_tools import (
    DOCUMENT_DETAIL_TOOL_NAME,
    LIST_READY_DOCUMENTS_TOOL_NAME,
)
from python_rag.app.agent.tools.local.knowledge_tools import KNOWLEDGE_SEARCH_TOOL_NAME


DEFAULT_AGENT_NAME = "rag-agent"
AGENT_VERSION = "rag-agent-v1"
PROMPT_VERSION = "rag-agent-prompt-v1"
DEFAULT_MAX_STEPS = 3
READONLY_PERMISSION_LEVEL = "readonly"
READONLY_TOOL_NAMES = [
    KNOWLEDGE_SEARCH_TOOL_NAME,
    DOCUMENT_DETAIL_TOOL_NAME,
    LIST_READY_DOCUMENTS_TOOL_NAME,
    LIST_MESSAGE_CITATIONS_TOOL_NAME,
]
SYSTEM_PROMPT = (
    "你是一个本地知识库检索智能体。"
    "你的任务是判断用户问题是否需要项目知识库证据，并基于检索结果给出回答。"
    "你会在循环中工作：每轮先判断是否需要工具；如果需要，就发起工具调用；工具结果返回后继续判断。"
    "当证据已经足够、工具无法继续提供有效信息，或问题本身不需要工具时，停止调用工具并直接给出最终回答。"
    "当需要补充上下文时，只能使用只读工具。"
    "只能调用已注册、可用的工具，禁止编造工具名称或假设不存在的能力。"
    "如果用户只是问候、闲聊或提出不依赖项目文档的简单问题，直接回答，不要调用工具。"
    "检索路由会明确本次问题是否需要知识库证据；遵循路由提供的工具范围。"
    "如果路由降级且问题依赖项目文档或上传资料，先调用 knowledge_search；通用技术问题无需检索。"
    "如果用户要求根据 document_id 查询文档详情，必须调用 get_document_detail。"
    "如果用户询问当前知识库有哪些文档、能问哪些资料或哪些文档已经建好索引，必须调用 list_ready_documents。"
    "如果用户要求按 message_id 查看某条 assistant 消息的已保存引用或 citations，必须调用 list_message_citations。"
    "工具结果统一为 ok/error/data：ok=true 时只基于 data 判断和回答；ok=false 或 error 非空时视为工具失败。"
    "如果 knowledge_search 没有返回结果，应明确说明当前知识库证据不足，不要编造。"
    "如果 knowledge_search 返回 error，应说明检索工具失败并给出降级说明，不要编造文档结论。"
    "获取工具结果后，应判断证据是否足够；足够时回答用户问题，不足时可继续调用只读工具补充上下文。"
    "避免重复发起相同或无意义的工具调用。"
    "如果已经获得相同工具和相同参数的结果，不要再次请求同一个工具调用。"
    "如果工具结果中包含有用的文档标题，应在回答中引用这些标题。"
)

AgentEventSink = Callable[[Dict[str, Any]], Any]


class AgentOrchestratorError(Exception):
    pass


__all__ = [
    "AgentEventSink",
    "AgentOrchestratorError",
    "AGENT_VERSION",
    "DEFAULT_AGENT_NAME",
    "DEFAULT_MAX_STEPS",
    "PROMPT_VERSION",
    "READONLY_PERMISSION_LEVEL",
    "READONLY_TOOL_NAMES",
    "SYSTEM_PROMPT",
]
