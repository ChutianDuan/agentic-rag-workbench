/** 会话请求与 SSE 客户端：解析事件、分发业务回调，并在已有运行上恢复断开的连接。 */
import { joinUrl, requestEnvelope } from "./apiClient";
import type { ChatMessage } from "../types/message";
import type { ChatSubmitData, MessageListData, Session } from "../types/session";

export function createSession(
  baseUrl: string,
  userId: number,
  title: string,
): Promise<Session> {
  return requestEnvelope<Session>(baseUrl, "/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, title }),
  });
}

export function submitChat(
  baseUrl: string,
  sessionId: number,
  content: string,
  topK: number,
): Promise<ChatSubmitData> {
  return requestEnvelope<ChatSubmitData>(
    baseUrl,
    `/v1/sessions/${sessionId}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, top_k: topK }),
    },
  );
}

export async function listMessages(baseUrl: string, sessionId: number): Promise<ChatMessage[]> {
  const data = await requestEnvelope<MessageListData>(
    baseUrl,
    `/v1/sessions/${sessionId}/messages`,
  );
  return data.items;
}

export interface StreamChatRequest {
  session_id: number;
  doc_id?: number;
  doc_ids?: number[];
  content: string;
  top_k: number;
}

export interface StreamChatDoneMeta {
  run_id?: number;
  agent_run_id?: number;
  assistant_message_id?: number;
  message_id?: number;
  answer_source?: string;
  context_mode?: string;
  retrieved_count?: number;
  citation_count?: number;
  retrieval_ms?: number;
  lancedb_ms?: number;
  rerank_ms?: number;
  raw_hit_count?: number;
  ttft_ms?: number;
  e2e_latency_ms?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cost_usd?: number;
  no_context?: boolean;
  doc_ids?: number[];
  steps_used?: number;
}

export interface AgentChatStreamRequest {
  session_id: number;
  message: string;
  trace_id?: string;
}

export interface AgentStepEvent {
  type: "agent_step";
  run_id?: number;
  step_id?: number;
  step_index?: number;
  step_type?: string;
  name?: string;
  status?: string;
  decision?: string;
  answer?: string;
  latency_ms?: number;
  tool_call_count?: number;
  [key: string]: unknown;
}

export interface AgentToolCallEvent {
  type: "tool_call";
  run_id?: number;
  step_id?: number;
  tool_call_row_id?: number;
  tool_call_id?: string;
  tool_name?: string;
  arguments?: unknown;
  status?: string;
  latency_ms?: number;
  [key: string]: unknown;
}

export interface AgentToolResultEvent {
  type: "tool_result";
  run_id?: number;
  step_id?: number;
  tool_call_row_id?: number;
  tool_call_id?: string;
  tool_name?: string;
  arguments?: unknown;
  result?: unknown;
  status?: string;
  error_message?: string;
  latency_ms?: number;
  [key: string]: unknown;
}

export interface AgentFinalEvent {
  type: "final";
  run_id?: number;
  message_id?: number;
  answer: string;
  citations?: unknown[];
  steps_used?: number;
  e2e_latency_ms?: number;
  [key: string]: unknown;
}

export interface StreamTransportEvent {
  type: string;
  eventId: string | null;
  payload: Record<string, unknown>;
}

export interface StreamResumeEvent {
  lastEventId: string;
  attempt: number;
}

export interface StreamChatCallbacks {
  onDelta?: (delta: string) => void;
  onDone?: (meta: StreamChatDoneMeta) => void;
  onAgentStep?: (event: AgentStepEvent) => void;
  onToolCall?: (event: AgentToolCallEvent) => void;
  onToolResult?: (event: AgentToolResultEvent) => void;
  onFinal?: (event: AgentFinalEvent) => void;
  onTransportEvent?: (event: StreamTransportEvent) => void;
  onResume?: (event: StreamResumeEvent) => void;
}

interface ProcessedSseEvent {
  done: boolean;
  eventId: string | null;
}

interface StreamSseOptions {
  resumeOnDisconnect?: boolean;
  maxResumeAttempts?: number;
  resolveResumeRequest?: (request: unknown, response: Response) => unknown | null;
}

function appendDecodedText(
  current: string,
  chunk: Uint8Array,
  decoder: TextDecoder,
): string {
  // 一个网络分片可能截断 UTF-8 字符或 CRLF；持续使用同一个 decoder 和累积缓冲。
  return (current + decoder.decode(chunk, { stream: true })).replace(/\r\n/g, "\n");
}

/** 解析一条完整 SSE；先通知传输观察者，再触发对应的回答或 Agent 回调。 */
function processSseEvent(
  rawEvent: string,
  callbacks: StreamChatCallbacks,
  beforeDispatch?: (eventId: string | null) => void,
): ProcessedSseEvent {
  const lines = rawEvent.split("\n");
  const trimmedLines = lines.map((line) => line.trim());
  const eventName = trimmedLines
    .find((line) => line.startsWith("event:"))
    ?.slice(6)
    .trim();
  const eventId = trimmedLines
    .find((line) => line.startsWith("id:"))
    ?.slice(3)
    .trim() || null;
  const dataLines = trimmedLines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trim());

  if (dataLines.length === 0) {
    return { done: false, eventId };
  }

  const payloadText = dataLines.join("\n");
  const payload = JSON.parse(payloadText) as {
    type?: string;
    delta?: string;
    message?: string;
    meta?: StreamChatDoneMeta;
    event_id?: string | number;
    [key: string]: unknown;
  };
  const type = payload.type || eventName;
  const nextEventId = eventId || (payload.event_id === undefined ? null : String(payload.event_id));
  // 收到恢复后的首个编号事件才宣布续传成功，并且先于该事件的其他回调。
  beforeDispatch?.(nextEventId);
  callbacks.onTransportEvent?.({
    type: type || "message",
    eventId: nextEventId,
    payload,
  });

  if (type === "delta") {
    callbacks.onDelta?.(payload.delta || "");
    return { done: false, eventId: nextEventId };
  }

  if (type === "done") {
    callbacks.onDone?.(payload.meta || {});
    return { done: true, eventId: nextEventId };
  }

  if (type === "error") {
    throw new Error(payload.message || "stream error");
  }

  if (type === "agent_step") {
    callbacks.onAgentStep?.({ ...payload, type: "agent_step" } as AgentStepEvent);
    return { done: false, eventId: nextEventId };
  }

  if (type === "tool_call") {
    callbacks.onToolCall?.({ ...payload, type: "tool_call" } as AgentToolCallEvent);
    return { done: false, eventId: nextEventId };
  }

  if (type === "tool_result") {
    callbacks.onToolResult?.({ ...payload, type: "tool_result" } as AgentToolResultEvent);
    return { done: false, eventId: nextEventId };
  }

  if (type === "final") {
    callbacks.onFinal?.({ ...payload, type: "final" } as AgentFinalEvent);
    return { done: false, eventId: nextEventId };
  }

  return { done: false, eventId: nextEventId };
}

/** 同一请求的连接循环；只有获得续传游标及稳定请求标识后，断线才允许重连。 */
async function streamSse(
  baseUrl: string,
  path: string,
  request: unknown,
  callbacks: StreamChatCallbacks = {},
  options: StreamSseOptions = {},
): Promise<void> {
  // 默认最多重连 3 次，计数覆盖整个请求，不会在成功连接后清零。
  const maxResumeAttempts = options.maxResumeAttempts ?? 3;
  let resumeAttempts = 0;
  let lastEventId: string | null = null;
  let resumeFromEventId: string | null = null;
  let activeRequest = request;
  let resumeRequestReady = options.resolveResumeRequest === undefined;

  const canResume = () => Boolean(
    options.resumeOnDisconnect
    && lastEventId
    && resumeRequestReady
    && resumeAttempts < maxResumeAttempts,
  );

  const scheduleResume = () => {
    if (!lastEventId) {
      return;
    }
    resumeFromEventId = lastEventId;
    resumeAttempts += 1;
  };

  const announceResumed = (receivedEventId: string | null) => {
    if (!resumeFromEventId || !receivedEventId) return;
    callbacks.onResume?.({ lastEventId: resumeFromEventId, attempt: resumeAttempts });
    resumeFromEventId = null;
  };

  while (true) {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    };
    if (lastEventId) {
      headers["Last-Event-ID"] = lastEventId;
    }

    let response: Response;
    try {
      response = await fetch(joinUrl(baseUrl, path), {
        method: "POST",
        headers,
        body: JSON.stringify(activeRequest),
      });
    } catch (error) {
      if (!canResume()) {
        throw error;
      }
      scheduleResume();
      continue;
    }

    if (options.resolveResumeRequest) {
      const resolvedRequest = options.resolveResumeRequest(request, response);
      if (resolvedRequest !== null) {
        activeRequest = resolvedRequest;
        resumeRequestReady = true;
      }
    }

    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || `${response.status} ${response.statusText}`);
    }

    if (!response.body) {
      throw new Error("stream response body is empty");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sawDone = false;
    let readFailed = false;

    // 完整事件和 EOF 前的最后一段缓冲使用相同处理，避免遗漏游标或结束状态更新。
    const consumeEvent = (rawEvent: string) => {
      const processed = processSseEvent(rawEvent, callbacks, announceResumed);
      if (processed.eventId) {
        lastEventId = processed.eventId;
      }
      sawDone = processed.done || sawDone;
    };

    while (true) {
      let result: ReadableStreamReadResult<Uint8Array>;
      try {
        result = await reader.read();
      } catch (error) {
        if (!canResume()) {
          throw error;
        }
        readFailed = true;
        break;
      }

      if (result.done) {
        break;
      }

      buffer = appendDecodedText(buffer, result.value, decoder);
      // 网络 chunk 与 SSE event 没有一一对应关系；只分发空行结束的完整事件。
      const events = buffer.split("\n\n");
      buffer = events.pop() || "";

      for (const rawEvent of events) {
        consumeEvent(rawEvent);
      }
    }

    if (!readFailed && buffer.trim()) {
      consumeEvent(buffer.trim());
    }

    // EOF 只表示连接关闭；没有业务 done 时必须续传或报告失败，不能当作回答完成。
    if (sawDone) {
      return;
    }

    if (!canResume()) {
      throw new Error("stream closed before done event");
    }

    scheduleResume();
  }
}

/** 普通 Chat 首次发送正文；拿到网关保存的消息 ID 后，续传改为订阅同一条消息。 */
export async function streamChat(
  baseUrl: string,
  request: StreamChatRequest,
  callbacks: StreamChatCallbacks = {},
): Promise<void> {
  return streamSse(
    baseUrl,
    "/v1/chat/stream",
    request,
    callbacks,
    {
      resumeOnDisconnect: true,
      resolveResumeRequest: (initialRequest, response) => {
        const rawMessageId = response.headers.get("X-User-Message-ID");
        const userMessageId = Number(rawMessageId);
        if (
          !rawMessageId
          || !Number.isInteger(userMessageId)
          || userMessageId <= 0
          || typeof initialRequest !== "object"
          || initialRequest === null
        ) {
          return null;
        }

        const resumeRequest: Record<string, unknown> = {
          ...(initialRequest as Record<string, unknown>),
          user_message_id: userMessageId,
        };
        // 续传用已有消息 ID 表达订阅意图；移除仅在首次提交时需要的正文。
        delete resumeRequest.content;
        return resumeRequest;
      },
    },
  );
}

/** Agent 续传保留 message 与 trace_id，由后端用稳定运行标识复用生成任务。 */
export async function streamAgentChat(
  baseUrl: string,
  request: AgentChatStreamRequest,
  callbacks: StreamChatCallbacks = {},
): Promise<void> {
  return streamSse(
    baseUrl,
    "/v1/agent/chat/stream",
    { ...request, stream: true },
    callbacks,
    { resumeOnDisconnect: true },
  );
}
