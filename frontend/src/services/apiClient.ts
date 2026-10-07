/** 非 SSE 请求共用的传输层：处理 HTTP 错误，再按项目响应外壳检查业务错误。 */
import type { ApiEnvelope } from "../types/api";

export function joinUrl(baseUrl: string, path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const normalizedBase = baseUrl.trim().replace(/\/$/, "");
  return `${normalizedBase}${normalizedPath}`;
}

function extractErrorMessage(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== "object") {
    return fallback;
  }

  const record = payload as Record<string, unknown>;

  if (typeof record.message === "string") {
    return record.message;
  }
  if (typeof record.error === "string") {
    return record.error;
  }
  if (typeof record.detail === "string") {
    return record.detail;
  }
  if (record.detail && typeof record.detail === "object") {
    const detail = record.detail as Record<string, unknown>;
    if (typeof detail.message === "string") {
      return detail.message;
    }
  }

  return fallback;
}

async function parseJsonPayload(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) {
    return {};
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return { message: text };
  }
}

export async function requestJson<T>(
  baseUrl: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(joinUrl(baseUrl, path), init);
  const payload = await parseJsonPayload(response);

  if (!response.ok) {
    throw new Error(extractErrorMessage(payload, `${response.status} ${response.statusText}`));
  }

  return payload as T;
}

export async function requestEnvelope<T>(
  baseUrl: string,
  path: string,
  init?: RequestInit,
): Promise<T> {
  // HTTP 成功不代表业务成功；只有 code=0 时才把 data 交给页面或业务客户端。
  const payload = await requestJson<ApiEnvelope<T>>(baseUrl, path, init);
  if (payload.code !== 0) {
    throw new Error(payload.message || `api error ${payload.code}`);
  }
  return payload.data;
}
