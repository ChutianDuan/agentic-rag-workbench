"""Agent 对外调用入口，负责提供只读工具集合并将每次运行交给独立执行器。"""

from typing import Any, Dict, List, Optional

from python_rag.app.agent.agent_runner import AgentRunExecutor
from python_rag.app.agent.orchestration_config import (
    AgentEventSink,
    AgentOrchestratorError,
    DEFAULT_AGENT_NAME,
    DEFAULT_MAX_STEPS,
    READONLY_PERMISSION_LEVEL,
    READONLY_TOOL_NAMES,
    SYSTEM_PROMPT,
)
# 保留该模块引用：现有调用和测试通过此入口访问、替换会话记忆实现。
from python_rag.app.agent.memory import session as session_memory
from python_rag.app.agent.trace import trace_service
from python_rag.app.agent.tools.registry import ToolRegistry, default_registry
from python_rag.app.modules.llm import service as llm_service


class AgentOrchestrator:
    """保存运行配置；每次 run 新建执行器，避免不同请求共享路由后的工具白名单。"""

    def __init__(
        self,
        registry: ToolRegistry = default_registry,
        max_steps: int = DEFAULT_MAX_STEPS,
        agent_name: str = DEFAULT_AGENT_NAME,
    ):
        self.registry = registry
        self.max_steps = max(1, int(max_steps or DEFAULT_MAX_STEPS))
        self.agent_name = agent_name

    def _readonly_tool_names(self) -> List[str]:
        """返回约定名单中已注册的工具名；实际权限还要由 Schema 和执行端检查。"""
        return [
            name
            for name in READONLY_TOOL_NAMES
            if self.registry.has(name)
        ]

    def _tool_schemas(self) -> List[dict]:
        """只导出已注册且权限为 readonly 的工具声明，供模型生成工具调用。"""
        return self.registry.export_openai_tools_schema(
            names=self._readonly_tool_names(),
            permission_level=READONLY_PERMISSION_LEVEL,
        )

    def _get_readonly_tool(self, name: str):
        """执行前再次检查工具名单和权限，不能仅相信模型看到的工具声明。"""
        if name not in READONLY_TOOL_NAMES:
            raise AgentOrchestratorError("tool is not allowed: {0}".format(name))

        tool = self.registry.get(name)
        if tool.permission_level != READONLY_PERMISSION_LEVEL:
            raise AgentOrchestratorError(
                "tool permission denied: {0}".format(name)
            )
        return tool

    async def run(
        self,
        question: str,
        trace_id: Optional[str] = None,
        session_id: Optional[int] = None,
        user_message_id: Optional[int] = None,
        event_sink: Optional[AgentEventSink] = None,
    ) -> Dict[str, Any]:
        """统一同步与 SSE 的业务流程；事件接收器可选，结果由调用入口负责保存为消息。"""
        runner = AgentRunExecutor(
            orchestrator=self,
            trace_service_module=trace_service,
            llm_service_module=llm_service,
        )
        return await runner.run(
            question=question,
            trace_id=trace_id,
            session_id=session_id,
            user_message_id=user_message_id,
            event_sink=event_sink,
        )


async def run_agent(
    question: str,
    trace_id: Optional[str] = None,
    session_id: Optional[int] = None,
    user_message_id: Optional[int] = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> Dict[str, Any]:
    orchestrator = AgentOrchestrator(max_steps=max_steps)
    return await orchestrator.run(
        question=question,
        trace_id=trace_id,
        session_id=session_id,
        user_message_id=user_message_id,
    )


__all__ = [
    "AgentEventSink",
    "AgentOrchestrator",
    "AgentOrchestratorError",
    "DEFAULT_AGENT_NAME",
    "DEFAULT_MAX_STEPS",
    "READONLY_PERMISSION_LEVEL",
    "READONLY_TOOL_NAMES",
    "SYSTEM_PROMPT",
    "run_agent",
]
