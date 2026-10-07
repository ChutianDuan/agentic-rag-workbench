#pragma once

#include <atomic>
#include <memory>
#include <string>

#include <drogon/drogon.h>

#include "common/GatewayConfig.h"

class PythonSSEClient;
class PythonApiClient;

// SSE 请求适配与并发控制；实际生成在 Python 后端，网关只代理连接与事件。
class StreamChatService {
public:
    StreamChatService(
        std::shared_ptr<PythonSSEClient> pythonSSEClient,
        std::shared_ptr<PythonApiClient> pythonApiClient,
        GatewaySseProxyConfig config = {}
    );

    void handleStream(
        const drogon::HttpRequestPtr& req,
        std::function<void(const drogon::HttpResponsePtr&)>&& callback
    );

    void handleAgentStream(
        const drogon::HttpRequestPtr& req,
        std::function<void(const drogon::HttpResponsePtr&)>&& callback
    );

private:
    struct StreamSlotLease;

    std::shared_ptr<PythonSSEClient> pythonSSEClient_;
    std::shared_ptr<PythonApiClient> pythonApiClient_;
    int maxConcurrentStreams_{64};
    std::shared_ptr<std::atomic<int>> activeStreams_;

    static bool validateRequestBody(const Json::Value& body, std::string& error);
    static bool validateAgentRequestBody(const Json::Value& body, std::string& error);
    static std::string buildSseErrorEvent(
        const std::string& message,
        const std::string& code = "UPSTREAM_STREAM_ERROR",
        long httpCode = 0
    );
    static drogon::HttpResponsePtr buildJsonErrorResponse(
        int code,
        const std::string& message,
        drogon::HttpStatusCode status
    );
    static drogon::HttpResponsePtr buildStreamLimitResponse();
    std::shared_ptr<StreamSlotLease> acquireStreamSlot() const;
    void startStreamResponse(
        const Json::Value& body,
        std::string upstreamPath,
        std::string lastEventId,
        std::shared_ptr<StreamSlotLease> streamSlot,
        std::function<void(const drogon::HttpResponsePtr&)>&& callback
    );
};
