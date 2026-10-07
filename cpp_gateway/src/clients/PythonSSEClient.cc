#include "PythonSSEClient.h"

#include <curl/curl.h>

#include <chrono>
#include <mutex>
#include <sstream>
#include <string>
#include <utility>

namespace {
using Clock = std::chrono::steady_clock;

struct CurlWriteContext {
    // 每次上游连接的状态；接收 HTTP 头后才能决定正文是否可以作为 SSE 转发。
    PythonSSEClient::ChunkCallback onChunk;
    Clock::time_point startedAt{Clock::now()};
    long responseCode{0};
    bool seenFirstByte{false};
    bool cancelled{false};
    bool emitGatewayMetrics{true};
    std::string localError;
};

long long elapsedMs(Clock::time_point startedAt) {
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - startedAt).count();
}

std::string buildGatewayMetricsEvent(long long ttftMs) {
    Json::Value payload(Json::objectValue);
    payload["type"] = "gateway_metrics";
    payload["ttft_ms"] = Json::Int64(ttftMs < 0 ? 0 : ttftMs);

    Json::StreamWriterBuilder builder;
    builder["indentation"] = "";
    return "event: gateway_metrics\ndata: " + Json::writeString(builder, payload) + "\n\n";
}

bool shouldSuppressBody(const CurlWriteContext* ctx) {
    // 上游 HTTP 错误正文可能是普通 JSON，统一留给完成回调转换成 SSE error。
    return ctx && ctx->responseCode >= 400;
}

bool sendChunk(CurlWriteContext* ctx, const std::string& chunk) {
    if (!ctx->onChunk) {
        ctx->localError = "missing onChunk callback";
        ctx->cancelled = true;
        return false;
    }

    const bool ok = ctx->onChunk(chunk);
    if (!ok) {
        ctx->localError = "downstream stream closed";
        ctx->cancelled = true;
        return false;
    }
    return true;
}

size_t writeCallback(char* ptr, size_t size, size_t nmemb, void* userdata) {
    const size_t total = size * nmemb;
    if (total == 0 || userdata == nullptr) {
        return 0;
    }

    auto* ctx = static_cast<CurlWriteContext*>(userdata);
    if (shouldSuppressBody(ctx)) {
        return total;
    }

    if (!ctx->seenFirstByte) {
        ctx->seenFirstByte = true;
        if (ctx->emitGatewayMetrics) {
            const auto metrics = buildGatewayMetricsEvent(elapsedMs(ctx->startedAt));
            if (!sendChunk(ctx, metrics)) {
                return 0;
            }
        }
    }

    if (!sendChunk(ctx, std::string(ptr, total))) {
        // 结束已无订阅者的网关代理连接；这不会取消 Python 已经启动的独立生成。
        return 0;
    }

    return total;
}

int progressCallback(void* userdata, curl_off_t, curl_off_t, curl_off_t, curl_off_t) {
    auto* ctx = static_cast<CurlWriteContext*>(userdata);
    return ctx && ctx->cancelled ? 1 : 0;
}

size_t headerCallback(char* ptr, size_t size, size_t nmemb, void* userdata) {
    const size_t total = size * nmemb;
    if (total == 0 || userdata == nullptr) {
        return total;
    }

    auto* ctx = static_cast<CurlWriteContext*>(userdata);
    std::string header(ptr, total);
    if (header.rfind("HTTP/", 0) == 0) {
        std::istringstream stream(header);
        std::string httpVersion;
        long code = 0;
        stream >> httpVersion >> code;
        if (code > 0) {
            ctx->responseCode = code;
        }
    }
    return total;
}

void ensureCurlGlobalInit() {
    static std::once_flag once;
    std::call_once(once, []() {
        curl_global_init(CURL_GLOBAL_DEFAULT);
    });
}

std::string curlErrorMessage(CURLcode rc) {
    switch (rc) {
        case CURLE_OPERATION_TIMEDOUT:
            return "upstream stream timed out";
        case CURLE_COULDNT_CONNECT:
        case CURLE_COULDNT_RESOLVE_HOST:
        case CURLE_COULDNT_RESOLVE_PROXY:
            return std::string("upstream connection failed: ") + curl_easy_strerror(rc);
        case CURLE_ABORTED_BY_CALLBACK:
        case CURLE_WRITE_ERROR:
            return "downstream stream closed";
        default:
            return curl_easy_strerror(rc);
    }
}
}  // namespace

PythonSSEClient::PythonSSEClient(std::string baseUrl, GatewaySseProxyConfig config)
    : baseUrl_(std::move(baseUrl)),
      config_(config) {}

std::string PythonSSEClient::joinUrl(const std::string& baseUrl, const std::string& path) {
    if (baseUrl.empty()) {
        return path;
    }
    if (path.empty()) {
        return baseUrl;
    }

    const bool baseEndsWithSlash = baseUrl.back() == '/';
    const bool pathStartsWithSlash = path.front() == '/';

    if (baseEndsWithSlash && pathStartsWithSlash) {
        return baseUrl + path.substr(1);
    }
    if (!baseEndsWithSlash && !pathStartsWithSlash) {
        return baseUrl + "/" + path;
    }
    return baseUrl + path;
}

std::string PythonSSEClient::jsonToString(const Json::Value& value) {
    Json::StreamWriterBuilder builder;
    builder["indentation"] = "";
    return Json::writeString(builder, value);
}

void PythonSSEClient::postStream(
    const std::string& path,
    const Json::Value& body,
    const ChunkCallback& onChunk,
    const FinishCallback& onFinish,
    const std::string& lastEventId
) const {
    // 此方法在代理线程中同步消费 curl 数据，chunk 回调只负责转发，不解析业务事件。
    ensureCurlGlobalInit();

    const std::string url = joinUrl(baseUrl_, path);
    const std::string bodyStr = jsonToString(body);

    CURL* curl = curl_easy_init();
    if (!curl) {
        if (onFinish) {
            onFinish(false, 0, "curl_easy_init failed");
        }
        return;
    }

    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: application/json");
    headers = curl_slist_append(headers, "Accept: text/event-stream");
    headers = curl_slist_append(headers, "Cache-Control: no-cache");
    std::string lastEventIdHeader;
    if (!lastEventId.empty()) {
        lastEventIdHeader = "Last-Event-ID: " + lastEventId;
        headers = curl_slist_append(headers, lastEventIdHeader.c_str());
    }

    CurlWriteContext writeCtx{onChunk};
    writeCtx.emitGatewayMetrics = config_.emitGatewayMetrics;

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_NOPROXY, "*");
    curl_easy_setopt(curl, CURLOPT_PROXY, "");
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, bodyStr.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(bodyStr.size()));

    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &writeCtx);
    curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, headerCallback);
    curl_easy_setopt(curl, CURLOPT_HEADERDATA, &writeCtx);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, progressCallback);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &writeCtx);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_TCP_NODELAY, 1L);

    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, config_.connectTimeoutSeconds);
    // 不限制生成总时长，改用低速/空闲超时检测失去响应的上游连接。
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 0L);
    curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, config_.upstreamLowSpeedLimitBytesPerSecond);
    curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, config_.upstreamIdleTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_BUFFERSIZE, config_.curlBufferSizeBytes);

    CURLcode rc = curl_easy_perform(curl);

    long httpCode = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &httpCode);

    std::string errorMessage;
    bool ok = true;

    if (rc != CURLE_OK) {
        ok = false;
        errorMessage = writeCtx.localError.empty() ? curlErrorMessage(rc) : writeCtx.localError;
    } else if (httpCode >= 400) {
        ok = false;
        std::ostringstream oss;
        oss << "upstream http error: " << httpCode;
        errorMessage = oss.str();
    }

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (onFinish) {
        onFinish(ok, httpCode, errorMessage);
    }
}
