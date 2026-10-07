"""一次 Agent 运行的编排：加载记忆、路由、执行工具、记录 Trace 并汇总回答证据。"""

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from python_rag.app.agent import orchestration_config as config
from python_rag.app.agent.intent_router import (
    RouteDecision,
    build_forced_knowledge_search_tool_call as _build_forced_knowledge_search_tool_call,
    route_question,
)
from python_rag.app.agent.memory import session as session_memory
from python_rag.app.agent.schemas import AgentStepStatus, AgentToolCallStatus
from python_rag.app.agent.tool_protocol import (
    normalize_tool_result as _normalize_tool_result,
    parse_tool_arguments as _parse_tool_arguments,
    tool_call_id as _tool_call_id,
    tool_call_name as _tool_call_name,
    tool_call_signature as _tool_call_signature,
    tool_error_result as _tool_error_result,
    tool_result_data as _tool_result_data,
    tool_result_error as _tool_result_error,
    validate_tool_arguments as _validate_tool_arguments,
)
from python_rag.app.agent.tools.local.knowledge_tools import KNOWLEDGE_SEARCH_TOOL_NAME
from python_rag.app.agent.trace import trace_service as default_trace_service
from python_rag.app.modules.llm import service as default_llm_service


logger = logging.getLogger("python_rag.app.agent.orchestrator")


MAX_STEPS_FINALIZATION_PROMPT = (
    "已达到工具调用上限。请不要再调用任何工具，只能基于已有工具观察给出当前结论。"
    "如果已有证据不足，请明确说明证据不足和无法继续补充的原因。"
)
MAX_STEPS_FALLBACK_ANSWER = (
    "已达到工具调用上限，以下结论仅基于已有观察；当前证据不足以继续补充，"
    "建议缩小问题或提高 max_steps 后重试。"
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


async def _emit_agent_event(
    event_sink: Optional[config.AgentEventSink],
    event_type: str,
    payload: Dict[str, Any],
) -> None:
    if event_sink is None:
        return

    event = dict(payload)
    event["type"] = event_type
    result = event_sink(event)
    if inspect.isawaitable(result):
        await result


def _extract_usage(result: Dict[str, Any]) -> Dict[str, Optional[int]]:
    usage = result.get("usage") or {}
    prompt_tokens = _coerce_usage_int(usage.get("prompt_tokens"))
    if prompt_tokens is None:
        prompt_tokens = _coerce_usage_int(usage.get("input_tokens"))

    completion_tokens = _coerce_usage_int(usage.get("completion_tokens"))
    if completion_tokens is None:
        completion_tokens = _coerce_usage_int(usage.get("output_tokens"))

    total_tokens = _coerce_usage_int(usage.get("total_tokens"))
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _coerce_usage_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _add_usage(
    total_usage: Dict[str, Optional[int]],
    usage: Dict[str, Optional[int]],
) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if value is None:
            continue
        total_usage[key] = (total_usage.get(key) or 0) + value

    if usage.get("total_tokens") is None:
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if prompt_tokens is not None and completion_tokens is not None:
            total_usage["total_tokens"] = (
                (total_usage.get("total_tokens") or 0)
                + prompt_tokens
                + completion_tokens
            )


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_score(value: Any) -> float:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return 0.0


def _extract_observation_citations(
    observations: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """从知识检索观察中提取并去重引用；当前引用代表检索证据，不是逐句使用标记。"""
    citations: List[Dict[str, Any]] = []
    seen: Set[Tuple[int, int]] = set()

    for observation in observations:
        if observation.get("tool_name") != KNOWLEDGE_SEARCH_TOOL_NAME:
            continue

        result = _tool_result_data(observation.get("result"))

        results = result.get("results") or []
        if not isinstance(results, list):
            continue

        for item in results:
            if not isinstance(item, dict):
                continue

            doc_id_value = item.get("doc_id")
            if doc_id_value is None:
                doc_id_value = item.get("document_id")
            chunk_index_value = item.get("chunk_index")
            if chunk_index_value is None:
                chunk_index_value = item.get("index", item.get("seq"))

            doc_id = _coerce_int(doc_id_value)
            chunk_id = _coerce_int(item.get("chunk_id") or item.get("id"))
            chunk_index = _coerce_int(chunk_index_value)
            if doc_id is None or chunk_id is None or chunk_index is None:
                continue

            key = (doc_id, chunk_id)
            if key in seen:
                continue
            seen.add(key)

            content = str(item.get("content") or item.get("snippet") or "")
            citations.append(
                {
                    "rank": len(citations) + 1,
                    "doc_id": doc_id,
                    "chunk_id": chunk_id,
                    "chunk_index": chunk_index,
                    "score": _coerce_score(item.get("score")),
                    "snippet": str(item.get("snippet") or content)[:300],
                    "content": content,
                    "title": item.get("title") or "",
                }
            )

    return citations


def _extract_observation_retrieval_summary(
    observations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for observation in observations:
        if observation.get("tool_name") != KNOWLEDGE_SEARCH_TOOL_NAME:
            continue

        result = _tool_result_data(observation.get("result"))

        retrieval = result.get("retrieval")
        if isinstance(retrieval, dict):
            summary = dict(retrieval)
            total = result.get("total")
            if total is not None:
                summary["retrieved_count"] = total
    return summary


def _build_tool_observation_message(observation: Dict[str, Any]) -> Dict[str, Any]:
    """把工具观察写回模型上下文；调用 ID 与 assistant.tool_calls 对应，失败也需回传。"""
    return {
        "role": "tool",
        "tool_call_id": observation["tool_call_id"],
        "name": observation["tool_name"],
        "content": _json_dumps(observation["result"]),
    }


class AgentRunExecutor:
    """维护单次运行的工具白名单；工具循环完成后返回回答、引用和路由诊断。"""

    def __init__(
        self,
        orchestrator: Any,
        trace_service_module: Any = default_trace_service,
        llm_service_module: Any = default_llm_service,
    ):
        self.orchestrator = orchestrator
        self.trace_service = trace_service_module
        self.llm_service = llm_service_module
        self.allowed_tool_names: Optional[Set[str]] = None

    def _apply_routing(
        self,
        decision: RouteDecision,
        readonly_tool_names: List[str],
    ) -> Tuple[Dict[str, Any], str, List[dict]]:
        """统一生成工具限制、提示词和持久化诊断，执行端使用相同白名单。"""
        tool_schemas = self.orchestrator._tool_schemas()
        self.allowed_tool_names = {
            schema["function"]["name"] for schema in tool_schemas
            if schema["function"]["name"] in readonly_tool_names
        }
        knowledge_available = KNOWLEDGE_SEARCH_TOOL_NAME in self.allowed_tool_names
        routing = decision.to_dict()
        routing["knowledge_search_available"] = knowledge_available
        routing["force_knowledge_search"] = decision.route == "rag" and knowledge_available

        if decision.route == "agent":
            # 同时收紧工具声明和执行白名单，防止模型绕过无需检索的路由结果。
            self.allowed_tool_names.discard(KNOWLEDGE_SEARCH_TOOL_NAME)
            instruction = (
                "本次路由为 agent：不需要知识库检索，禁止调用 knowledge_search。"
                "可使用会话上下文、通用知识以及已注册的其他只读工具回答。"
            )
        elif decision.route == "rag":
            instruction = "本次路由为 rag：回答需要知识库证据。"
            if knowledge_available:
                instruction += "先检索再回答。"
        else:
            instruction = "本次路由降级：请自主判断是否需要知识库证据，并在需要时先检索。"
        if not knowledge_available:
            routing["tool_unavailable_reason"] = "knowledge_search_unavailable"
            instruction += (
                "当前没有可用的 knowledge_search，无法访问知识库内容。"
                "如问题需要知识库证据，必须明确说明缺少知识库访问能力，不得编造文档结论。"
            )

        tool_schemas = [
            schema for schema in tool_schemas
            if schema["function"]["name"] in self.allowed_tool_names
        ]
        return routing, config.SYSTEM_PROMPT + "\n\n" + instruction, tool_schemas

    async def _finish_failed_tool_call(
        self,
        run_id: int,
        step_id: int,
        tool_row_id: int,
        external_tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
        error_message: str,
        event_sink: Optional[config.AgentEventSink],
        latency_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        self.trace_service.fail_tool_call(
            tool_call_id=tool_row_id,
            error_message=error_message,
            result=result,
            result_preview=self.trace_service.build_tool_result_preview(
                tool_name or "unknown",
                result,
            ),
            latency_ms=latency_ms,
        )
        event = {
            "run_id": run_id,
            "step_id": step_id,
            "tool_call_row_id": tool_row_id,
            "tool_call_id": external_tool_call_id,
            "tool_name": tool_name or "unknown",
            "arguments": arguments,
            "result": result,
            "status": AgentToolCallStatus.FAILED,
            "error_message": error_message,
        }
        if latency_ms is not None:
            event["latency_ms"] = latency_ms
        await _emit_agent_event(event_sink, "tool_result", event)
        return {
            "tool_call_id": external_tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
            "error": error_message,
        }

    async def _finish_successful_tool_call(
        self,
        run_id: int,
        step_id: int,
        tool_row_id: int,
        external_tool_call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Dict[str, Any],
        event_sink: Optional[config.AgentEventSink],
        latency_ms: int,
    ) -> Dict[str, Any]:
        self.trace_service.finish_tool_call(
            tool_call_id=tool_row_id,
            result=result,
            result_preview=self.trace_service.build_tool_result_preview(
                tool_name,
                result,
            ),
            latency_ms=latency_ms,
        )
        await _emit_agent_event(
            event_sink,
            "tool_result",
            {
                "run_id": run_id,
                "step_id": step_id,
                "tool_call_row_id": tool_row_id,
                "tool_call_id": external_tool_call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "result": result,
                "status": AgentToolCallStatus.SUCCESS,
                "latency_ms": latency_ms,
            },
        )
        return {
            "tool_call_id": external_tool_call_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
        }

    async def _execute_tool_call(
        self,
        run_id: int,
        step_id: int,
        tool_call: Dict[str, Any],
        fallback_index: int,
        event_sink: Optional[config.AgentEventSink] = None,
        seen_tool_calls: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """校验并执行一个工具调用，将成功、失败和重复调用都记录为可回查的观察。"""
        external_tool_call_id = _tool_call_id(tool_call, fallback_index)
        tool_name = _tool_call_name(tool_call)

        try:
            arguments = _parse_tool_arguments(tool_call)
        except Exception as exc:
            arguments = {}
            tool_row_id = self.trace_service.create_tool_call(
                run_id=run_id,
                step_id=step_id,
                tool_name=tool_name or "unknown",
                arguments=arguments,
                tool_call_id=external_tool_call_id,
            )
            await _emit_agent_event(
                event_sink,
                "tool_call",
                {
                    "run_id": run_id,
                    "step_id": step_id,
                    "tool_call_row_id": tool_row_id,
                    "tool_call_id": external_tool_call_id,
                    "tool_name": tool_name or "unknown",
                    "arguments": arguments,
                    "status": AgentToolCallStatus.RUNNING,
                },
            )
            error_message = str(exc)
            result = _tool_error_result(error_message)
            return await self._finish_failed_tool_call(
                run_id=run_id,
                step_id=step_id,
                tool_row_id=tool_row_id,
                external_tool_call_id=external_tool_call_id,
                tool_name=tool_name or "unknown",
                arguments=arguments,
                result=result,
                error_message=error_message,
                event_sink=event_sink,
            )

        duplicate_error = None
        if seen_tool_calls is not None:
            # 签名按工具名和排序后的参数生成；同一次运行中相同调用只执行一次。
            signature = _tool_call_signature(tool_name, arguments)
            if signature in seen_tool_calls:
                duplicate_error = "duplicate tool call skipped: {0}".format(
                    tool_name or "unknown"
                )
            else:
                seen_tool_calls.add(signature)

        tool_row_id = self.trace_service.create_tool_call(
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            arguments=arguments,
            tool_call_id=external_tool_call_id,
        )
        await _emit_agent_event(
            event_sink,
            "tool_call",
            {
                "run_id": run_id,
                "step_id": step_id,
                "tool_call_row_id": tool_row_id,
                "tool_call_id": external_tool_call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "status": AgentToolCallStatus.RUNNING,
            },
        )

        if duplicate_error:
            result = _tool_error_result(
                duplicate_error,
                {
                    "skipped": True,
                    "reason": "duplicate_tool_call",
                },
            )
            return await self._finish_failed_tool_call(
                run_id=run_id,
                step_id=step_id,
                tool_row_id=tool_row_id,
                external_tool_call_id=external_tool_call_id,
                tool_name=tool_name or "unknown",
                arguments=arguments,
                result=result,
                error_message=duplicate_error,
                event_sink=event_sink,
                latency_ms=0,
            )

        started_at = time.time()
        try:
            # 路由白名单、只读权限和参数校验各司其职，全部通过后才能调用工具。
            if self.allowed_tool_names is not None and tool_name not in self.allowed_tool_names:
                raise config.AgentOrchestratorError(
                    "tool is not allowed by routing: {0}".format(tool_name)
                )
            tool = self.orchestrator._get_readonly_tool(tool_name)
            validation_error = _validate_tool_arguments(
                arguments,
                getattr(tool, "input_schema", None),
            )
            if validation_error:
                result = _tool_error_result(validation_error)
                return await self._finish_failed_tool_call(
                    run_id=run_id,
                    step_id=step_id,
                    tool_row_id=tool_row_id,
                    external_tool_call_id=external_tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                    error_message=validation_error,
                    event_sink=event_sink,
                    latency_ms=int((time.time() - started_at) * 1000),
                )

            timeout_ms = int(getattr(tool, "timeout_ms", 30000) or 30000)
            result = await asyncio.wait_for(
                tool.run(arguments),
                timeout=timeout_ms / 1000,
            )
            latency_ms = int((time.time() - started_at) * 1000)
            result = _normalize_tool_result(result)
            error_message = _tool_result_error(result)
            if error_message:
                return await self._finish_failed_tool_call(
                    run_id=run_id,
                    step_id=step_id,
                    tool_row_id=tool_row_id,
                    external_tool_call_id=external_tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result,
                    error_message=error_message,
                    event_sink=event_sink,
                    latency_ms=latency_ms,
                )

            return await self._finish_successful_tool_call(
                run_id=run_id,
                step_id=step_id,
                tool_row_id=tool_row_id,
                external_tool_call_id=external_tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                event_sink=event_sink,
                latency_ms=latency_ms,
            )
        except asyncio.TimeoutError:
            timeout_ms = int(getattr(tool, "timeout_ms", 30000) or 30000)
            error_message = "tool timeout after {0}ms".format(timeout_ms)
            result = _tool_error_result(error_message)
            return await self._finish_failed_tool_call(
                run_id=run_id,
                step_id=step_id,
                tool_row_id=tool_row_id,
                external_tool_call_id=external_tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                error_message=error_message,
                event_sink=event_sink,
                latency_ms=int((time.time() - started_at) * 1000),
            )
        except Exception as exc:
            error_message = str(exc)
            result = _tool_error_result(error_message)
            return await self._finish_failed_tool_call(
                run_id=run_id,
                step_id=step_id,
                tool_row_id=tool_row_id,
                external_tool_call_id=external_tool_call_id,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                error_message=error_message,
                event_sink=event_sink,
                latency_ms=int((time.time() - started_at) * 1000),
            )

    async def _run_forced_knowledge_search(
        self,
        question: str,
        run_id: int,
        messages: List[Dict[str, Any]],
        reason: str,
        event_sink: Optional[config.AgentEventSink],
        seen_tool_calls: Set[str],
    ) -> Dict[str, Any]:
        """执行路由指定的首次检索，复用工具校验、去重和 Trace 流程。"""
        step_index = 0
        tool_call = _build_forced_knowledge_search_tool_call(question)
        step_name = "forced_knowledge_search_{0}".format(step_index)
        step_type = "forced_tool_call"
        step_id = self.trace_service.create_step(
            run_id=run_id,
            step_index=step_index,
            step_type=step_type,
            name=step_name,
            input_data={
                "messages": messages,
                "tool_call": tool_call,
                "reason": reason,
            },
        )
        await _emit_agent_event(
            event_sink,
            "agent_step",
            {
                "run_id": run_id,
                "step_id": step_id,
                "step_index": step_index,
                "step_type": step_type,
                "name": step_name,
                "status": AgentStepStatus.RUNNING,
                "reason": reason,
            },
        )

        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call],
            }
        )
        observation = await self._execute_tool_call(
            run_id=run_id,
            step_id=step_id,
            tool_call=tool_call,
            fallback_index=0,
            event_sink=event_sink,
            seen_tool_calls=seen_tool_calls,
        )
        messages.append(_build_tool_observation_message(observation))

        self.trace_service.finish_step(
            step_id=step_id,
            output_data={
                "observations": [observation],
                "reason": reason,
            },
            decision="forced_tool_call",
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            latency_ms=None,
        )
        await _emit_agent_event(
            event_sink,
            "agent_step",
            {
                "run_id": run_id,
                "step_id": step_id,
                "step_index": step_index,
                "step_type": step_type,
                "name": step_name,
                "status": AgentStepStatus.SUCCESS,
                "decision": "forced_tool_call",
                "tool_call_count": 1,
            },
        )
        return observation

    def _finish_run_result(
        self,
        run_id: int,
        answer: str,
        messages: List[Dict[str, Any]],
        observations: List[Dict[str, Any]],
        routing: Dict[str, Any],
        total_usage: Dict[str, Optional[int]],
        steps_used: int,
        termination_reason: str,
    ) -> Dict[str, Any]:
        output = {
            "answer": answer,
            "observations": observations,
            "citations": _extract_observation_citations(observations),
            "retrieval": _extract_observation_retrieval_summary(observations),
            "routing": routing,
            "steps_used": steps_used,
            "termination_reason": termination_reason,
        }
        # 分类用量保存在 routing.usage，不混入主模型的生成用量。
        self.trace_service.finish_run(
            run_id=run_id,
            output_data=output,
            prompt_tokens=total_usage["prompt_tokens"],
            completion_tokens=total_usage["completion_tokens"],
            total_tokens=total_usage["total_tokens"],
        )
        return dict(output, run_id=run_id, messages=messages)

    async def run(
        self,
        question: str,
        trace_id: Optional[str] = None,
        session_id: Optional[int] = None,
        user_message_id: Optional[int] = None,
        event_sink: Optional[config.AgentEventSink] = None,
    ) -> Dict[str, Any]:
        """完成一次业务运行；Trace 在这里结束，assistant 消息与引用由 HTTP/SSE 入口落库。"""
        question = str(question or "").strip()
        if not question:
            raise config.AgentOrchestratorError("question is required")

        # HTTP 入口已保存 user message 时，通过其 ID 避免把本轮问题重复放进历史上下文。
        memory = session_memory.load_session_memory(
            session_id=session_id,
            current_user_message_id=user_message_id,
        )
        memory_debug_context = session_memory.format_memory_debug_context(memory)
        if memory_debug_context:
            logger.info(
                "agent memory context session_id=%s user_id=%s user_message_id=%s message_count=%s user_memory_message_id=%s user_memory_task_queued=%s summary_message_id=%s summary_task_queued=%s\n%s",
                session_id,
                memory.user_id,
                user_message_id,
                memory.message_count,
                memory.user_memory_message_id,
                memory.user_memory_task_queued,
                memory.summary_message_id,
                memory.summary_task_queued,
                memory_debug_context,
            )

        readonly_tool_names = self.orchestrator._readonly_tool_names()
        # 前置分类不占 Agent 步数，决策完成后再创建带 routing metadata 的 run。
        decision = await route_question(
            question,
            memory=memory,
            llm_service_module=self.llm_service,
        )
        routing, system_prompt, tool_schemas = self._apply_routing(decision, readonly_tool_names)
        force_knowledge_search = routing["force_knowledge_search"]

        run_id = self.trace_service.create_run(
            agent_name=self.orchestrator.agent_name,
            agent_version=config.AGENT_VERSION,
            trace_id=trace_id,
            session_id=session_id,
            user_message_id=user_message_id,
            input_data={"question": question},
            meta={
                "agent_version": config.AGENT_VERSION,
                "prompt_version": config.PROMPT_VERSION,
                "max_steps": self.orchestrator.max_steps,
                "tools": [name for name in readonly_tool_names if name in self.allowed_tool_names],
                "permission_level": config.READONLY_PERMISSION_LEVEL,
                "retrieval_router": {
                    "force_knowledge_search": force_knowledge_search,
                    "reason": (
                        decision.reason
                        if force_knowledge_search
                        else None
                    ),
                },
                "routing": routing,
                "memory": {
                    "user_id": memory.user_id,
                    "message_count": memory.message_count,
                    "recent_message_count": len(memory.recent_messages),
                    "has_user_memory": bool(memory.user_memory),
                    "user_memory_message_id": memory.user_memory_message_id,
                    "user_memory_task_queued": memory.user_memory_task_queued,
                    "user_memory_updated": memory.user_memory_updated,
                    "has_summary": bool(memory.summary),
                    "summary_message_id": memory.summary_message_id,
                    "summary_task_queued": memory.summary_task_queued,
                    "summary_updated": memory.summary_updated,
                },
            },
        )
        messages = session_memory.build_agent_messages(
            question=question,
            system_prompt=system_prompt,
            memory=memory,
        )
        observations: List[Dict[str, Any]] = []
        seen_tool_calls: Set[str] = set()
        total_usage: Dict[str, Optional[int]] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        run_closed = False

        try:
            step_index = 0
            if force_knowledge_search:
                # 首次强制检索计为第 0 步，随后从第 1 步开始让回答模型决策。
                observation = await self._run_forced_knowledge_search(
                    question=question,
                    run_id=run_id,
                    messages=messages,
                    reason=decision.reason,
                    event_sink=event_sink,
                    seen_tool_calls=seen_tool_calls,
                )
                observations.append(observation)
                step_index = 1

            # 每轮模型决策记为一步，一步可以含多个工具调用；max_steps 并非工具调用次数上限。
            while step_index < self.orchestrator.max_steps:
                step_name = "agent_step_{0}".format(step_index)
                step_type = "llm_decision"
                effective_tool_schemas = tool_schemas or None
                effective_tool_choice = (
                    "auto" if effective_tool_schemas is not None else None
                )
                step_id = self.trace_service.create_step(
                    run_id=run_id,
                    step_index=step_index,
                    step_type=step_type,
                    name=step_name,
                    input_data={
                        "messages": messages,
                        "tools": effective_tool_schemas,
                    },
                )
                await _emit_agent_event(
                    event_sink,
                    "agent_step",
                    {
                        "run_id": run_id,
                        "step_id": step_id,
                        "step_index": step_index,
                        "step_type": step_type,
                        "name": step_name,
                        "status": AgentStepStatus.RUNNING,
                    },
                )
                llm_result = self.llm_service.generate_from_messages(
                    messages,
                    tools=effective_tool_schemas,
                    tool_choice=effective_tool_choice,
                )
                usage = _extract_usage(llm_result)
                _add_usage(total_usage, usage)
                tool_calls = llm_result.get("tool_calls") or []

                if not tool_calls:
                    final_answer = str(
                        llm_result.get("answer")
                        or (llm_result.get("message") or {}).get("content")
                        or ""
                    ).strip()
                    self.trace_service.finish_step(
                        step_id=step_id,
                        output_data={
                            "answer": final_answer,
                            "llm": llm_result,
                        },
                        decision="final_answer",
                        prompt_tokens=usage["prompt_tokens"],
                        completion_tokens=usage["completion_tokens"],
                        total_tokens=usage["total_tokens"],
                        latency_ms=llm_result.get("latency_ms"),
                    )
                    await _emit_agent_event(
                        event_sink,
                        "agent_step",
                        {
                            "run_id": run_id,
                            "step_id": step_id,
                            "step_index": step_index,
                            "step_type": step_type,
                            "name": step_name,
                            "status": AgentStepStatus.SUCCESS,
                            "decision": "final_answer",
                            "answer": final_answer,
                        },
                    )
                    result = self._finish_run_result(
                        run_id=run_id,
                        answer=final_answer,
                        messages=messages,
                        observations=observations,
                        routing=routing,
                        total_usage=total_usage,
                        steps_used=step_index + 1,
                        termination_reason="final_answer",
                    )
                    run_closed = True
                    return result

                assistant_message = {
                    "role": "assistant",
                    "content": (llm_result.get("message") or {}).get("content") or "",
                    "tool_calls": tool_calls,
                }
                messages.append(assistant_message)

                step_observations = []
                for call_index, tool_call in enumerate(tool_calls):
                    observation = await self._execute_tool_call(
                        run_id=run_id,
                        step_id=step_id,
                        tool_call=tool_call,
                        fallback_index=call_index,
                        event_sink=event_sink,
                        seen_tool_calls=seen_tool_calls,
                    )
                    observations.append(observation)
                    step_observations.append(observation)
                    messages.append(_build_tool_observation_message(observation))

                self.trace_service.finish_step(
                    step_id=step_id,
                    output_data={
                        "llm": llm_result,
                        "observations": step_observations,
                    },
                    decision="tool_call",
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                    latency_ms=llm_result.get("latency_ms"),
                )
                await _emit_agent_event(
                    event_sink,
                    "agent_step",
                    {
                        "run_id": run_id,
                        "step_id": step_id,
                        "step_index": step_index,
                        "step_type": step_type,
                        "name": step_name,
                        "status": AgentStepStatus.SUCCESS,
                        "decision": "tool_call",
                        "tool_call_count": len(step_observations),
                    },
                )
                step_index += 1

            # 步数预算耗尽后仍安排一次无工具收尾，让模型基于已有观察说明结论或证据不足。
            finalization_messages = messages + [
                {
                    "role": "system",
                    "content": MAX_STEPS_FINALIZATION_PROMPT,
                }
            ]
            final_step_id = self.trace_service.create_step(
                run_id=run_id,
                step_index=step_index,
                step_type="llm_finalization",
                name="agent_finalization_{0}".format(step_index),
                input_data={
                    "messages": finalization_messages,
                    "tools": None,
                    "reason": "max_steps",
                },
            )
            await _emit_agent_event(
                event_sink,
                "agent_step",
                {
                    "run_id": run_id,
                    "step_id": final_step_id,
                    "step_index": step_index,
                    "step_type": "llm_finalization",
                    "name": "agent_finalization_{0}".format(step_index),
                    "status": AgentStepStatus.RUNNING,
                },
            )
            llm_result = self.llm_service.generate_from_messages(
                finalization_messages,
                tools=None,
                tool_choice=None,
            )
            usage = _extract_usage(llm_result)
            _add_usage(total_usage, usage)
            final_answer = str(
                llm_result.get("answer")
                or (llm_result.get("message") or {}).get("content")
                or ""
            ).strip()
            if not final_answer:
                final_answer = MAX_STEPS_FALLBACK_ANSWER

            self.trace_service.finish_step(
                step_id=final_step_id,
                output_data={
                    "answer": final_answer,
                    "llm": llm_result,
                    "termination_reason": "max_steps",
                },
                decision="max_steps_final_answer",
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                latency_ms=llm_result.get("latency_ms"),
            )
            await _emit_agent_event(
                event_sink,
                "agent_step",
                {
                    "run_id": run_id,
                    "step_id": final_step_id,
                    "step_index": step_index,
                    "step_type": "llm_finalization",
                    "name": "agent_finalization_{0}".format(step_index),
                    "status": AgentStepStatus.SUCCESS,
                    "decision": "max_steps_final_answer",
                    "answer": final_answer,
                },
            )
            result = self._finish_run_result(
                run_id=run_id,
                answer=final_answer,
                messages=finalization_messages,
                observations=observations,
                routing=routing,
                total_usage=total_usage,
                steps_used=step_index + 1,
                termination_reason="max_steps",
            )
            run_closed = True
            return result
        except Exception as exc:
            if not run_closed:
                self.trace_service.fail_run(
                    run_id=run_id,
                    error_message=str(exc),
                    output_data={
                        "observations": observations,
                    },
                    prompt_tokens=total_usage["prompt_tokens"],
                    completion_tokens=total_usage["completion_tokens"],
                    total_tokens=total_usage["total_tokens"],
                )
            raise


__all__ = ["AgentRunExecutor"]
