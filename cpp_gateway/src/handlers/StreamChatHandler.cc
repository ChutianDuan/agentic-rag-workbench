#include "StreamChatHandler.h"

#include <exception>
#include <memory>
#include <thread>
#include <utility>

#include <json/json.h>

#include "clients/PythonApiClient.h"
#include "clients/PythonSSEClient.h"

using namespace drogon;

namespace {
std::string jsonToCompactString(const Json::Value& value) {
    Json::StreamWriterBuilder builder;
    builder["indentation"] = "";
    return Json::writeString(builder, value);
}

std::string getLastEventId(const HttpRequestPtr& req) {
    return req ? req->getHeader("Last-Event-ID") : std::string();
}

bool isDownstreamClosedError(const std::string& message) {
    return message == "downstream stream closed";
}
}  // namespace

struct StreamChatService::StreamSlotLease {
    // 槽位随最后一个持有者释放，覆盖响应创建、后台代理和提前失败等不同退出路径。
    explicit StreamSlotLease(std::shared_ptr<std::atomic<int>> activeStreams)
        : activeStreams_(std::move(activeStreams)) {}

    ~StreamSlotLease() {
        if (!activeStreams_) {
            return;
        }

        const int previous = activeStreams_->fetch_sub(1);
        if (previous <= 0) {
            activeStreams_->store(0);
        }
    }

private:
    std::shared_ptr<std::atomic<int>> activeStreams_;
};

StreamChatService::StreamChatService(
    std::shared_ptr<PythonSSEClient> pythonSSEClient,
    std::shared_ptr<PythonApiClient> pythonApiClient,
    GatewaySseProxyConfig config
)
    : pythonSSEClient_(std::move(pythonSSEClient)),
      pythonApiClient_(std::move(pythonApiClient)),
      maxConcurrentStreams_(config.maxConcurrentStreams),
      activeStreams_(std::make_shared<std::atomic<int>>(0)) {}

bool StreamChatService::validateRequestBody(const Json::Value& body, std::string& error) {
    if (!body.isObject()) {
        error = "request body must be a json object";
        return false;
    }

    if (!body.isMember("session_id") || !body["session_id"].isInt()) {
        error = "missing or invalid session_id";
        return false;
    }
    if (body.isMember("doc_id") && (!body["doc_id"].isInt() || body["doc_id"].asInt() <= 0)) {
        error = "invalid doc_id";
        return false;
    }
    if (body.isMember("doc_ids") && !body["doc_ids"].isArray()) {
        error = "invalid doc_ids";
        return false;
    }
    if (body.isMember("doc_ids") && body["doc_ids"].isArray()) {
        for (const auto& item : body["doc_ids"]) {
            if (!item.isInt() || item.asInt() <= 0) {
                error = "doc_ids must contain positive integers";
                return false;
            }
        }
    }
    const bool hasUserMessageId = body.isMember("user_message_id") && body["user_message_id"].isInt();
    const bool hasContent = body.isMember("content") && body["content"].isString()
        && !body["content"].asString().empty();
    if (!hasUserMessageId && !hasContent) {
        error = "missing user_message_id or content";
        return false;
    }

    if (body.isMember("top_k") && !body["top_k"].isInt()) {
        error = "invalid top_k";
        return false;
    }

    return true;
}

bool StreamChatService::validateAgentRequestBody(const Json::Value& body, std::string& error) {
    if (!body.isObject()) {
        error = "request body must be a json object";
        return false;
    }

    if (!body.isMember("session_id") || !body["session_id"].isInt()) {
        error = "missing or invalid session_id";
        return false;
    }
    if (!body.isMember("message") || !body["message"].isString()
        || body["message"].asString().empty()) {
        error = "missing or invalid message";
        return false;
    }
    if (body.isMember("trace_id") && !body["trace_id"].isString()) {
        error = "invalid trace_id";
        return false;
    }

    return true;
}

std::string StreamChatService::buildSseErrorEvent(
    const std::string& message,
    const std::string& code,
    long httpCode
) {
    Json::Value root;
    root["type"] = "error";
    root["code"] = code;
    root["message"] = message;
    if (httpCode > 0) {
        root["upstream_http_status"] = Json::Int64(httpCode);
    }
    return "data: " + jsonToCompactString(root) + "\n\n";
}

HttpResponsePtr StreamChatService::buildJsonErrorResponse(
    int code,
    const std::string& message,
    HttpStatusCode status
) {
    Json::Value root;
    root["code"] = code;
    root["message"] = message;
    root["data"] = Json::nullValue;

    auto resp = HttpResponse::newHttpJsonResponse(root);
    resp->setStatusCode(status);
    return resp;
}

HttpResponsePtr StreamChatService::buildStreamLimitResponse() {
    auto resp = buildJsonErrorResponse(
        4290,
        "too many active streams",
        k429TooManyRequests
    );
    resp->addHeader("Retry-After", "1");
    return resp;
}

std::shared_ptr<StreamChatService::StreamSlotLease> StreamChatService::acquireStreamSlot() const {
    // 用原子比较交换同时检查和占用槽位，避免多个请求越过并发上限。
    int current = activeStreams_->load();
    while (current < maxConcurrentStreams_) {
        if (activeStreams_->compare_exchange_weak(current, current + 1)) {
            return std::make_shared<StreamSlotLease>(activeStreams_);
        }
    }

    return nullptr;
}

void StreamChatService::startStreamResponse(
    const Json::Value& body,
    std::string upstreamPath,
    std::string lastEventId,
    std::shared_ptr<StreamSlotLease> streamSlot,
    std::function<void(const HttpResponsePtr&)>&& callback
) {
    // 响应与代理线程接力持有租约，线程退出时自动归还槽位；回调中的资源必须跨异步边界存活。
    auto resp = HttpResponse::newAsyncStreamResponse(
        [
            client = pythonSSEClient_,
            body,
            upstreamPath = std::move(upstreamPath),
            lastEventId = std::move(lastEventId),
            streamSlot = std::move(streamSlot)
        ](
            ResponseStreamPtr stream
        ) mutable {
            auto sharedStream = std::shared_ptr<drogon::ResponseStream>(std::move(stream));
            auto slot = std::move(streamSlot);

            try {
                std::thread([
                    client,
                    body,
                    upstreamPath,
                    lastEventId,
                    sharedStream,
                    streamSlot = std::move(slot)
                ]() mutable {
                    (void)streamSlot;

                    client->postStream(
                        upstreamPath,
                        body,
                        [sharedStream](const std::string& chunk) -> bool {
                            return sharedStream->send(chunk);
                        },
                        [sharedStream](bool ok, long httpCode, const std::string& errorMessage) mutable {
                            if (!ok) {
                                if (isDownstreamClosedError(errorMessage)) {
                                    // 客户端已断开，无法再发送 error；Python 的独立生成任务仍可继续。
                                    LOG_DEBUG << "SSE downstream closed before upstream completed";
                                    sharedStream->close();
                                    return;
                                }

                                const std::string msg = errorMessage.empty()
                                    ? ("gateway upstream stream failed, http=" + std::to_string(httpCode))
                                    : errorMessage;
                                const std::string code = httpCode >= 400
                                    ? "UPSTREAM_HTTP_ERROR"
                                    : "UPSTREAM_STREAM_ERROR";
                                sharedStream->send(buildSseErrorEvent(msg, code, httpCode));
                            }
                            sharedStream->close();
                        },
                        lastEventId
                    );
                }).detach();
            } catch (const std::exception& e) {
                LOG_ERROR << "Failed to start SSE proxy worker: " << e.what();
                sharedStream->send(buildSseErrorEvent(
                    "gateway failed to start stream worker",
                    "GATEWAY_STREAM_WORKER_UNAVAILABLE"
                ));
                sharedStream->close();
            }
        },
        true
    );

    resp->addHeader("Content-Type", "text/event-stream");
    resp->addHeader("Cache-Control", "no-cache");
    resp->addHeader("Connection", "keep-alive");
    resp->addHeader("X-Accel-Buffering", "no");
    resp->addHeader("X-Gateway-Max-Streams", std::to_string(maxConcurrentStreams_));
    if (body.isMember("user_message_id") && body["user_message_id"].isInt()) {
        resp->addHeader("X-User-Message-ID", std::to_string(body["user_message_id"].asInt()));
    }
    resp->setExpiredTime(0);

    callback(resp);
}

void StreamChatService::handleStream(
    const HttpRequestPtr& req,
    std::function<void(const HttpResponsePtr&)>&& callback
) {
    auto jsonPtr = req->getJsonObject();
    if (!jsonPtr) {
        callback(buildJsonErrorResponse(
            4001,
            "request body must be valid json",
            k400BadRequest
        ));
        return;
    }

    Json::Value body = *jsonPtr;
    std::string error;
    if (!validateRequestBody(body, error)) {
        callback(buildJsonErrorResponse(4002, error, k400BadRequest));
        return;
    }

    auto streamSlot = acquireStreamSlot();
    if (!streamSlot) {
        callback(buildStreamLimitResponse());
        return;
    }

    if (body.isMember("user_message_id") && body["user_message_id"].isInt()) {
        // 已有消息 ID 的请求直接订阅后台流，Last-Event-ID 决定从哪条事件之后续传。
        startStreamResponse(
            body,
            "/internal/chat/stream",
            getLastEventId(req),
            std::move(streamSlot),
            std::move(callback)
        );
        return;
    }

    auto sharedCallback =
        std::make_shared<std::function<void(const HttpResponsePtr&)>>(std::move(callback));

    Json::Value createMessageBody;
    createMessageBody["role"] = "user";
    createMessageBody["content"] = body["content"].asString();
    createMessageBody["status"] = "PENDING";

    // 首次 Chat 先通过 Python 保存用户消息，再以返回的 ID 发起流，重连时不重复保存正文。
    const int sessionId = body["session_id"].asInt();
    pythonApiClient_->forwardJsonPost(
        "/internal/sessions/" + std::to_string(sessionId) + "/messages",
        createMessageBody,
        [
            this,
            sharedCallback,
            body,
            lastEventId = getLastEventId(req),
            streamSlot = std::move(streamSlot)
        ](
            const HttpResponsePtr& msgResp
        ) mutable {
            if (msgResp->statusCode() >= 400) {
                (*sharedCallback)(msgResp);
                return;
            }

            auto msgJsonPtr = msgResp->getJsonObject();
            if (!msgJsonPtr || !(*msgJsonPtr).isMember("data")) {
                (*sharedCallback)(buildJsonErrorResponse(
                    4003,
                    "invalid response while creating user message",
                    k400BadRequest
                ));
                return;
            }

            Json::Value nextBody = body;
            nextBody["user_message_id"] = (*msgJsonPtr)["data"]["message_id"].asInt();
            nextBody.removeMember("content");
            startStreamResponse(
                nextBody,
                "/internal/chat/stream",
                std::move(lastEventId),
                std::move(streamSlot),
                std::move(*sharedCallback)
            );
        }
    );
}

void StreamChatService::handleAgentStream(
    const HttpRequestPtr& req,
    std::function<void(const HttpResponsePtr&)>&& callback
) {
    auto jsonPtr = req->getJsonObject();
    if (!jsonPtr) {
        callback(buildJsonErrorResponse(
            4001,
            "request body must be valid json",
            k400BadRequest
        ));
        return;
    }

    Json::Value body = *jsonPtr;
    std::string error;
    if (!validateAgentRequestBody(body, error)) {
        callback(buildJsonErrorResponse(4002, error, k400BadRequest));
        return;
    }

    auto streamSlot = acquireStreamSlot();
    if (!streamSlot) {
        callback(buildStreamLimitResponse());
        return;
    }

    // Agent 由后端创建消息并按 trace_id 识别运行；网关原样保留问题和续传头。
    body["stream"] = true;
    startStreamResponse(
        body,
        "/api/agent/chat/stream",
        getLastEventId(req),
        std::move(streamSlot),
        std::move(callback)
    );
}
