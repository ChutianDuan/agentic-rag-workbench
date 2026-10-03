import copy
import json
import re
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

import requests

from python_rag.app.core.config import (
    LLM_API_KEY,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_TOKEN_LIMIT_FIELD,
    LLM_MAX_GENERATION_ROUNDS,
    LLM_ENABLE,
    LLM_PROVIDER,
    LLM_BASE_URL,
    LLM_TIMEOUT_SECONDS,
    LLM_TOP_P,
    LLM_FREQUENCY_PENALTY,
    LLM_PRESENCE_PENALTY,
    ROUTER_LLM_MODEL,
    ROUTER_LLM_BASE_URL,
    ROUTER_LLM_API_KEY,
    ROUTER_LLM_TIMEOUT_SECONDS,
    ROUTER_LLM_MAX_TOKENS,
    ROUTER_LLM_TOKEN_LIMIT_FIELD,
)
from python_rag.app.shared import http_client


class LLMServiceError(Exception):
    pass


_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think>", re.IGNORECASE)
_LOOP_DONE_MARKER = "[[LLM_DONE]]"
_LOOP_DONE_MARKER_RE = re.compile(r"\s*" + re.escape(_LOOP_DONE_MARKER) + r"\s*", re.IGNORECASE)
_LOOP_CONTROL_INSTRUCTION = (
    "你需要自主判断最终答案是否已经完整。\n"
    "当答案完整时，请在答案最后单独输出一行：%s\n"
    "如果答案尚未完整，请继续输出正文，不要输出结束标记。\n"
    "除这个结束标记外，不要输出任何循环控制说明。"
) % _LOOP_DONE_MARKER
_LOOP_CONTINUE_PROMPT = (
    "上一轮回答还没有完成，或者响应被长度限制截断。"
    "请只从上一轮结尾处继续补全，不要重复已经输出的内容。"
    "当最终答案完整时，请在答案最后单独输出一行：%s"
) % _LOOP_DONE_MARKER
_STOP_FINISH_REASONS = {"stop", "eos_token", "end_turn", "content_filter", "tool_calls"}
_USAGE_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
)


class _ThinkingContentFilter:
    def __init__(self) -> None:
        self._buffer = ""
        self._inside_think = False

    def feed(self, text: str) -> str:
        if not text:
            return ""

        self._buffer += text
        output_parts: List[str] = []

        while self._buffer:
            if self._inside_think:
                close_match = _THINK_CLOSE_RE.search(self._buffer)
                if close_match is None:
                    self._buffer = self._keep_possible_close_tag_suffix(self._buffer)
                    break

                self._buffer = self._buffer[close_match.end():]
                self._inside_think = False
                continue

            open_match = _THINK_OPEN_RE.search(self._buffer)
            if open_match is not None:
                output_parts.append(self._buffer[:open_match.start()])
                self._buffer = self._buffer[open_match.end():]
                self._inside_think = True
                continue

            safe_prefix_len = self._safe_visible_prefix_len(self._buffer)
            if safe_prefix_len <= 0:
                break

            output_parts.append(self._buffer[:safe_prefix_len])
            self._buffer = self._buffer[safe_prefix_len:]

        return "".join(output_parts)

    def flush(self) -> str:
        if self._inside_think:
            self._buffer = ""
            self._inside_think = False
            return ""

        output = self._buffer
        self._buffer = ""
        return output

    @staticmethod
    def _safe_visible_prefix_len(text: str) -> int:
        lower_text = text.lower()
        last_tag_start = lower_text.rfind("<")
        if last_tag_start < 0:
            return len(text)

        suffix = lower_text[last_tag_start:]
        if "<think".startswith(suffix):
            return last_tag_start

        if suffix.startswith("<think") and ">" not in suffix:
            return last_tag_start

        return len(text)

    @staticmethod
    def _keep_possible_close_tag_suffix(text: str) -> str:
        lower_text = text.lower()
        last_tag_start = lower_text.rfind("<")
        if last_tag_start < 0:
            return ""

        suffix = lower_text[last_tag_start:]
        if "</think>".startswith(suffix):
            return text[last_tag_start:]

        return ""


def _strip_thinking_content(text: str) -> str:
    content_filter = _ThinkingContentFilter()
    return (content_filter.feed(text) + content_filter.flush()).strip()


def _build_headers() -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
    }
    if LLM_API_KEY:
        headers["Authorization"] = "Bearer %s" % LLM_API_KEY
    return headers


def _build_payload(
    messages: List[Dict[str, Any]],
    stream: bool = False,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
) -> Dict[str, Any]:
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "stream": stream,
    }
    payload[LLM_TOKEN_LIMIT_FIELD] = LLM_MAX_TOKENS
    if LLM_TOP_P is not None:
        payload["top_p"] = LLM_TOP_P
    if LLM_FREQUENCY_PENALTY is not None:
        payload["frequency_penalty"] = LLM_FREQUENCY_PENALTY
    if LLM_PRESENCE_PENALTY is not None:
        payload["presence_penalty"] = LLM_PRESENCE_PENALTY
    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return payload


def _with_loop_control_instruction(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    loop_messages = [dict(message) for message in messages]
    for message in loop_messages:
        if message.get("role") == "system":
            content = _normalize_content_parts(message.get("content"))
            message["content"] = (content.rstrip() + "\n\n" + _LOOP_CONTROL_INSTRUCTION).strip()
            return loop_messages

    return [{"role": "system", "content": _LOOP_CONTROL_INSTRUCTION}] + loop_messages


def _strip_loop_done_marker(text: str) -> Tuple[str, bool]:
    if not text:
        return "", False

    marker_found = _LOOP_DONE_MARKER_RE.search(text) is not None
    if not marker_found:
        return text, False

    return _LOOP_DONE_MARKER_RE.sub("", text).strip(), True


def _is_stop_finish_reason(finish_reason: Any) -> bool:
    if finish_reason is None:
        return False
    return str(finish_reason).strip().lower() in _STOP_FINISH_REASONS


def _coerce_usage_number(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_usage(total_usage: Optional[Dict[str, Any]], usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not usage:
        return total_usage

    merged = dict(total_usage or {})
    for key in _USAGE_TOKEN_KEYS:
        value = _coerce_usage_number(usage.get(key))
        if value is None:
            continue
        current = _coerce_usage_number(merged.get(key)) or 0
        merged[key] = current + value

    return merged or None


class _LoopDoneMarkerFilter:
    def __init__(self) -> None:
        self._buffer = ""
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def feed(self, text: str) -> str:
        if self._done or not text:
            return ""

        self._buffer += text
        marker_index = self._buffer.lower().find(_LOOP_DONE_MARKER.lower())
        if marker_index >= 0:
            output = self._buffer[:marker_index].rstrip()
            self._buffer = ""
            self._done = True
            return output

        safe_prefix_len = self._safe_visible_prefix_len(self._buffer)
        if safe_prefix_len <= 0:
            return ""

        output = self._buffer[:safe_prefix_len]
        self._buffer = self._buffer[safe_prefix_len:]
        return output

    def flush(self) -> str:
        if self._done:
            self._buffer = ""
            return ""

        output = self._buffer
        self._buffer = ""
        return output

    @staticmethod
    def _safe_visible_prefix_len(text: str) -> int:
        marker = _LOOP_DONE_MARKER.lower()
        lower_text = text.lower()
        max_suffix_len = min(len(marker) - 1, len(lower_text))
        for suffix_len in range(max_suffix_len, 0, -1):
            if marker.startswith(lower_text[-suffix_len:]):
                return len(text) - suffix_len
        return len(text)


def _normalize_content_parts(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") in ("text", "output_text") and item.get("text"):
                    text_parts.append(str(item["text"]))
                elif item.get("type") == "input_text" and item.get("text"):
                    text_parts.append(str(item["text"]))
            elif item is not None:
                text_parts.append(str(item))
        return "".join(text_parts)
    return str(value)


def _normalize_tool_calls(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, dict)
    ]


def _extract_answer(resp_json: Dict[str, Any]) -> Dict[str, Any]:
    choices = resp_json.get("choices") or []
    if not choices:
        raise LLMServiceError("llm response missing choices")

    first = choices[0] or {}
    message = first.get("message") or {}
    answer = message.get("content")
    tool_calls = _normalize_tool_calls(message.get("tool_calls") or first.get("tool_calls"))

    # 兼容部分 OpenAI-compatible 服务
    if answer is None:
        answer = first.get("text")

    if answer is None and not tool_calls:
        raise LLMServiceError("llm response missing answer content")

    answer = _strip_thinking_content(_normalize_content_parts(answer))
    if not answer and not tool_calls:
        raise LLMServiceError("llm response answer is empty")

    return {
        "answer": answer,
        "message_content": answer,
        "message": {
            "content": answer,
            "tool_calls": tool_calls,
        },
        "tool_calls": tool_calls,
        "model": resp_json.get("model") or LLM_MODEL,
        "usage": resp_json.get("usage"),
        "finish_reason": first.get("finish_reason"),
    }


def _flush_sse_event_lines(event_lines: List[str]) -> Optional[str]:
    if not event_lines:
        return None

    data_lines = []
    for line in event_lines:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())

    if not data_lines:
        return None
    return "\n".join(data_lines)


def _iter_sse_data(response: requests.Response) -> Generator[str, None, None]:
    event_lines: List[str] = []

    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.rstrip("\r")
        if line == "":
            payload = _flush_sse_event_lines(event_lines)
            event_lines = []
            if payload is not None:
                yield payload
            continue

        if line.startswith(":"):
            continue

        event_lines.append(line)

    payload = _flush_sse_event_lines(event_lines)
    if payload is not None:
        yield payload


def _extract_stream_delta(chunk_json: Dict[str, Any]) -> str:
    choices = chunk_json.get("choices") or []
    if not choices:
        return ""

    first = choices[0] or {}
    delta = first.get("delta")
    if isinstance(delta, dict):
        if delta.get("content") is not None:
            return _normalize_content_parts(delta.get("content"))

    if first.get("text") is not None:
        return _normalize_content_parts(first.get("text"))

    message = first.get("message")
    if isinstance(message, dict) and message.get("content") is not None:
        return _normalize_content_parts(message.get("content"))

    return ""


def _extract_stream_tool_call_deltas(chunk_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    choices = chunk_json.get("choices") or []
    if not choices:
        return []

    first = choices[0] or {}
    delta = first.get("delta")
    if isinstance(delta, dict):
        tool_calls = _normalize_tool_calls(delta.get("tool_calls"))
        if tool_calls:
            return tool_calls

    message = first.get("message")
    if isinstance(message, dict):
        return _normalize_tool_calls(message.get("tool_calls"))

    return []


def _merge_stream_tool_call_deltas(
    tool_calls: List[Dict[str, Any]],
    deltas: List[Dict[str, Any]],
) -> None:
    for delta in deltas:
        index = delta.get("index")
        if isinstance(index, bool) or index is None:
            index = len(tool_calls)
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = len(tool_calls)

        while len(tool_calls) <= index:
            tool_calls.append({})

        target = tool_calls[index]
        for key in ("id", "type"):
            if delta.get(key) is not None:
                target[key] = delta[key]

        function_delta = delta.get("function")
        if isinstance(function_delta, dict):
            function_target = target.setdefault("function", {})
            if function_delta.get("name") is not None:
                function_target["name"] = function_delta["name"]
            if function_delta.get("arguments") is not None:
                function_target["arguments"] = (
                    str(function_target.get("arguments") or "")
                    + str(function_delta["arguments"])
                )


def generate_routing_decision(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """独立分类请求：不附加回答续写指令，不传工具，也不重试。"""
    if not LLM_ENABLE:
        raise LLMServiceError("LLM service is disabled by config")
    if not ROUTER_LLM_MODEL:
        raise LLMServiceError("ROUTER_LLM_MODEL is not configured")
    if not ROUTER_LLM_BASE_URL:
        raise LLMServiceError("ROUTER_LLM_BASE_URL is not configured")
    if ROUTER_LLM_TIMEOUT_SECONDS <= 0 or ROUTER_LLM_MAX_TOKENS <= 0:
        raise LLMServiceError("router timeout and token limit must be positive")

    headers = {"Content-Type": "application/json"}
    if ROUTER_LLM_API_KEY:
        headers["Authorization"] = "Bearer %s" % ROUTER_LLM_API_KEY
    payload = {
        "model": ROUTER_LLM_MODEL,
        "messages": messages,
        "temperature": 0,
        "stream": False,
        ROUTER_LLM_TOKEN_LIMIT_FIELD: ROUTER_LLM_MAX_TOKENS,
    }
    started_at = time.perf_counter()
    try:
        response = http_client.post(
            ROUTER_LLM_BASE_URL + "/chat/completions",
            headers=headers,
            json=payload,
            timeout=ROUTER_LLM_TIMEOUT_SECONDS,
            # 保持单次分类预算，避免重定向导致第二次请求。
            allow_redirects=False,
        )
    except requests.Timeout:
        raise LLMServiceError("router request timed out") from None
    except requests.RequestException:
        raise LLMServiceError("router request failed") from None
    if not 200 <= response.status_code < 300:
        # 不将远端响应正文或连接凭据写入公开 routing 信息。
        raise LLMServiceError("router http error status=%s" % response.status_code)
    try:
        result = _extract_answer(response.json())
    except (ValueError, TypeError, AttributeError, LLMServiceError):
        raise LLMServiceError("router response is invalid") from None
    result["model"] = ROUTER_LLM_MODEL
    result["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
    return result


def generate_from_messages(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
) -> Dict[str, Any]:
    if not LLM_ENABLE:
        raise LLMServiceError("LLM service is disabled by config")

    if LLM_PROVIDER != "openai_compatible":
        raise LLMServiceError("unsupported LLM provider: %s" % LLM_PROVIDER)

    if not LLM_BASE_URL:
        raise LLMServiceError("LLM_BASE_URL is not configured")

    if not LLM_MODEL:
        raise LLMServiceError("LLM_MODEL is not configured")

    url = LLM_BASE_URL + "/chat/completions"
    headers = _build_headers()
    tool_calling_request = tools is not None or tool_choice is not None
    loop_messages = (
        [copy.deepcopy(message) for message in messages]
        if tool_calling_request
        else _with_loop_control_instruction(messages)
    )
    max_rounds = 1 if tool_calling_request else max(1, int(LLM_MAX_GENERATION_ROUNDS or 1))

    start_ts = time.time()
    answer_parts: List[str] = []
    message_content = ""
    tool_calls: List[Dict[str, Any]] = []
    total_usage: Optional[Dict[str, Any]] = None
    model = LLM_MODEL
    finish_reason = None
    stop_reason = "max_rounds"
    stopped_by_model = False
    rounds_used = 0

    for round_index in range(max_rounds):
        rounds_used = round_index + 1
        payload = _build_payload(
            loop_messages,
            stream=False,
            tools=tools,
            tool_choice=tool_choice,
        )

        try:
            response = http_client.post(
                url,
                headers=headers,
                json=payload,
                timeout=LLM_TIMEOUT_SECONDS,
            )
        except requests.Timeout as e:
            raise LLMServiceError("llm request timed out: %s" % str(e))
        except requests.RequestException as e:
            raise LLMServiceError("llm request failed: %s" % str(e))

        if response.status_code >= 400:
            text = response.text[:1000]
            raise LLMServiceError(
                "llm http error status=%s body=%s" % (response.status_code, text)
            )

        try:
            resp_json = response.json()
        except ValueError:
            raise LLMServiceError("llm response is not valid json: %s" % response.text[:1000])

        result = _extract_answer(resp_json)
        model = result.get("model") or model
        finish_reason = result.get("finish_reason")
        message_content = result.get("message_content") or ""
        tool_calls = result.get("tool_calls") or []
        total_usage = _merge_usage(total_usage, result.get("usage"))

        if tool_calls:
            stopped_by_model = True
            stop_reason = "tool_calls"
            break

        answer_text, done_marker_found = _strip_loop_done_marker(result["answer"])
        if answer_text:
            answer_parts.append(answer_text)

        if done_marker_found:
            stopped_by_model = True
            stop_reason = "done_marker"
            break

        if _is_stop_finish_reason(finish_reason):
            stopped_by_model = True
            stop_reason = "finish_reason:%s" % finish_reason
            break

        loop_messages.append({"role": "assistant", "content": result["answer"]})
        loop_messages.append({"role": "user", "content": _LOOP_CONTINUE_PROMPT})

    answer = "\n".join(part for part in answer_parts if part).strip()
    if not answer and tool_calls:
        answer = message_content
    if not answer and not tool_calls:
        raise LLMServiceError("llm response answer is empty")

    return {
        "answer": answer,
        "message_content": message_content,
        "message": {
            "content": message_content,
            "tool_calls": tool_calls,
        },
        "tool_calls": tool_calls,
        "model": model,
        "usage": total_usage,
        "finish_reason": finish_reason,
        "latency_ms": int((time.time() - start_ts) * 1000),
        "rounds": rounds_used,
        "max_rounds": max_rounds,
        "stopped_by_model": stopped_by_model,
        "stop_reason": stop_reason,
    }


def stream_from_messages(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
) -> Generator[Dict[str, Any], None, None]:
    if not LLM_ENABLE:
        raise LLMServiceError("LLM service is disabled by config")

    if LLM_PROVIDER != "openai_compatible":
        raise LLMServiceError("unsupported LLM provider: %s" % LLM_PROVIDER)

    if not LLM_BASE_URL:
        raise LLMServiceError("LLM_BASE_URL is not configured")

    if not LLM_MODEL:
        raise LLMServiceError("LLM_MODEL is not configured")

    url = LLM_BASE_URL + "/chat/completions"
    headers = _build_headers()
    tool_calling_request = tools is not None or tool_choice is not None
    loop_messages = (
        [copy.deepcopy(message) for message in messages]
        if tool_calling_request
        else _with_loop_control_instruction(messages)
    )
    max_rounds = 1 if tool_calling_request else max(1, int(LLM_MAX_GENERATION_ROUNDS or 1))

    start_ts = time.time()
    first_delta_ts = None
    answer_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    total_usage: Optional[Dict[str, Any]] = None
    finish_reason = None
    model = LLM_MODEL
    stop_reason = "max_rounds"
    stopped_by_model = False
    rounds_used = 0

    for round_index in range(max_rounds):
        rounds_used = round_index + 1
        payload = _build_payload(
            loop_messages,
            stream=True,
            tools=tools,
            tool_choice=tool_choice,
        )
        round_answer_parts: List[str] = []
        content_filter = _ThinkingContentFilter()
        marker_filter = _LoopDoneMarkerFilter()

        try:
            with http_client.post(
                url,
                headers=headers,
                json=payload,
                timeout=(10, LLM_TIMEOUT_SECONDS),
                stream=True,
            ) as response:
                if response.status_code >= 400:
                    text = response.text[:1000]
                    raise LLMServiceError(
                        "llm http error status=%s body=%s" % (response.status_code, text)
                    )

                for payload_text in _iter_sse_data(response):
                    if payload_text == "[DONE]":
                        break

                    try:
                        chunk_json = json.loads(payload_text)
                    except ValueError:
                        raise LLMServiceError(
                            "llm stream chunk is not valid json: %s" % payload_text[:500]
                        )

                    if chunk_json.get("model"):
                        model = chunk_json["model"]
                    if chunk_json.get("usage") is not None:
                        total_usage = _merge_usage(total_usage, chunk_json.get("usage"))

                    choices = chunk_json.get("choices") or []
                    if choices:
                        finish_reason = choices[0].get("finish_reason") or finish_reason

                    _merge_stream_tool_call_deltas(
                        tool_calls,
                        _extract_stream_tool_call_deltas(chunk_json),
                    )

                    raw_delta_text = _extract_stream_delta(chunk_json)
                    visible_text = marker_filter.feed(content_filter.feed(raw_delta_text))
                    if visible_text:
                        if first_delta_ts is None:
                            first_delta_ts = time.time()

                        answer_parts.append(visible_text)
                        round_answer_parts.append(visible_text)
                        yield {
                            "type": "delta",
                            "delta": visible_text,
                            "model": model,
                        }

                    if marker_filter.done:
                        break

                if not marker_filter.done:
                    visible_text = marker_filter.feed(content_filter.flush())
                    if visible_text:
                        if first_delta_ts is None:
                            first_delta_ts = time.time()

                        answer_parts.append(visible_text)
                        round_answer_parts.append(visible_text)
                        yield {
                            "type": "delta",
                            "delta": visible_text,
                            "model": model,
                        }

                if not marker_filter.done:
                    visible_text = marker_filter.flush()
                    if visible_text:
                        if first_delta_ts is None:
                            first_delta_ts = time.time()

                        answer_parts.append(visible_text)
                        round_answer_parts.append(visible_text)
                        yield {
                            "type": "delta",
                            "delta": visible_text,
                            "model": model,
                        }
        except requests.Timeout as e:
            raise LLMServiceError("llm request timed out: %s" % str(e))
        except requests.RequestException as e:
            raise LLMServiceError("llm request failed: %s" % str(e))

        if marker_filter.done:
            stopped_by_model = True
            stop_reason = "done_marker"
            break

        if tool_calls:
            stopped_by_model = True
            stop_reason = "tool_calls"
            break

        if _is_stop_finish_reason(finish_reason):
            stopped_by_model = True
            stop_reason = "finish_reason:%s" % finish_reason
            break

        round_answer = "".join(round_answer_parts).strip()
        if round_answer:
            loop_messages.append({"role": "assistant", "content": round_answer})
        loop_messages.append({"role": "user", "content": _LOOP_CONTINUE_PROMPT})

    answer = "".join(answer_parts).strip()
    if not answer and not tool_calls:
        raise LLMServiceError("llm stream produced empty answer")

    yield {
        "type": "done",
        "answer": answer,
        "message_content": answer,
        "message": {
            "content": answer,
            "tool_calls": tool_calls,
        },
        "tool_calls": tool_calls,
        "model": model,
        "usage": total_usage,
        "finish_reason": finish_reason,
        "latency_ms": int((time.time() - start_ts) * 1000),
        "ttft_ms": int((first_delta_ts - start_ts) * 1000) if first_delta_ts else None,
        "rounds": rounds_used,
        "max_rounds": max_rounds,
        "stopped_by_model": stopped_by_model,
        "stop_reason": stop_reason,
    }


def generate_answer(
    question: str,
    chunks: List[Dict[str, Any]],
    messages: List[Dict[str, str]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    question/chunks 暂时保留在签名里，后续可用于埋点、重试策略、provider routing。
    当前真实调用只依赖 messages。
    """
    _ = question
    _ = chunks
    return generate_from_messages(
        messages,
        tools=tools,
        tool_choice=tool_choice,
    )


def stream_answer(
    question: str,
    chunks: List[Dict[str, Any]],
    messages: List[Dict[str, str]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[Any] = None,
) -> Generator[Dict[str, Any], None, None]:
    _ = question
    _ = chunks
    yield from stream_from_messages(
        messages,
        tools=tools,
        tool_choice=tool_choice,
    )
