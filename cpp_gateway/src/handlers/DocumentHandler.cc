#include "DocumentHandler.h"

#include <cstdlib>
#include <algorithm>
#include <cctype>
#include <chrono>
#include <drogon/drogon.h>
#include <drogon/utils/Utilities.h>

#include <filesystem>
#include <fstream>
#include <random>
#include <sstream>
#include <string>

using namespace drogon;

namespace fs = std::filesystem;

namespace {
std::string fileExtension(std::string name) {
    auto pos = name.rfind('.');
    if (pos == std::string::npos) {
        return "";
    }

    auto ext = name.substr(pos + 1);
    std::transform(ext.begin(), ext.end(), ext.begin(), [](unsigned char ch) {
        return static_cast<char>(std::tolower(ch));
    });
    return ext;
}

bool isSupportedTextExtension(const std::string& ext) {
    return ext == "md" || ext == "txt" || ext == "json"
        || ext == "csv" || ext == "pdf" || ext == "docx"
        || ext == "xlsx";
}

std::string supportedDocumentTypesText() {
    return ".md, .txt, .json, .csv, .pdf, .docx, .xlsx";
}

std::string buildUniqueSuffix() {
    const auto now = std::chrono::high_resolution_clock::now().time_since_epoch().count();
    std::random_device rd;
    std::mt19937_64 gen(rd());
    std::uniform_int_distribution<unsigned long long> dist;

    std::ostringstream oss;
    oss << std::hex << now << dist(gen);
    return oss.str();
}

fs::path getRepoRoot() {
    if (const char* envRepoRoot = std::getenv("REPO_ROOT"); envRepoRoot && envRepoRoot[0] != '\0') {
        return fs::path(envRepoRoot);
    }

    return fs::current_path().parent_path();
}

fs::path getUploadDir() {
    const char* envUploadDir = std::getenv("UPLOAD_DIR");
    fs::path uploadDir = (envUploadDir && envUploadDir[0] != '\0')
        ? fs::path(envUploadDir)
        : fs::path("./data/uploads");

    if (uploadDir.is_relative()) {
        uploadDir = getRepoRoot() / uploadDir;
    }

    return uploadDir.lexically_normal();
}

long long maxDocumentSizeBytes() {
    const char* envValue = std::getenv("MAX_DOCUMENT_SIZE_BYTES");
    if (!envValue || envValue[0] == '\0') {
        return 100LL * 1024LL * 1024LL;
    }

    try {
        const auto parsed = std::stoll(envValue);
        return parsed > 0 ? parsed : 100LL * 1024LL * 1024LL;
    } catch (...) {
        return 100LL * 1024LL * 1024LL;
    }
}

void tryDeleteFile(const std::string& storagePath) {
    try {
        if (!storagePath.empty()) {
            fs::remove(storagePath);
        }
    } catch (...) {
    }
}

const Json::Value& responseDataOrSelf(const Json::Value& json) {
    if (json.isObject() && json.isMember("data") && !json["data"].isNull()) {
        return json["data"];
    }
    return json;
}
}  // namespace

DocumentService::DocumentService(std::shared_ptr<PythonApiClient> pythonClient)
    : pythonClient_(std::move(pythonClient)) {}

std::string DocumentService::sanitizeFileName(const std::string& name) {
    std::string out = name;
    for (auto& ch : out) {
        if (ch == '/' || ch == '\\' || ch == ':' || ch == '*' ||
            ch == '?' || ch == '"' || ch == '<' || ch == '>' || ch == '|') {
            ch = '_';
        }
    }
    if (out.empty()) {
        out = "upload.bin";
    }
    return out;
}

std::string DocumentService::buildStoredFileName(
    long long userId,
    const std::string& originalName,
    const std::string& sha256
) {
    const auto clean = sanitizeFileName(originalName);
    const auto ext = fs::path(clean).extension().string();
    const auto shaPrefix = sha256.substr(0, std::min<size_t>(sha256.size(), 16));
    return std::to_string(userId) + "_" + shaPrefix + "_" + buildUniqueSuffix() + ext;
}

std::string DocumentService::guessMime(const HttpFile& file) {
    const auto ext = fileExtension(file.getFileName());
    if (ext.empty()) {
        return "application/octet-stream";
    }

    if (ext == "md") return "text/markdown";
    if (ext == "txt") return "text/plain";
    if (ext == "json") return "application/json";
    if (ext == "csv") return "text/csv";
    if (ext == "pdf") return "application/pdf";
    if (ext == "docx") {
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    }
    if (ext == "xlsx") {
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    }
    return "application/octet-stream";
}

std::string DocumentService::sha256Hex(const std::string& data) {
    return utils::getSha256(data);
}

// 文件上传链路：Gateway 校验并落盘 -> 写入 documents -> 调用 Python 提交异步入库任务。
void DocumentService::uploadAndSubmit(
    const HttpRequestPtr& req,
    std::function<void(const HttpResponsePtr&)>&& callback
) const {
    auto sharedCallback =
        std::make_shared<std::function<void(const HttpResponsePtr&)>>(std::move(callback));

    MultiPartParser parser;
    if (parser.parse(req) != 0) {
        Json::Value json;
        json["code"] = 400;
        json["message"] = "invalid multipart form data";
        json["data"] = Json::nullValue;
        auto resp = HttpResponse::newHttpJsonResponse(json);
        resp->setStatusCode(k400BadRequest);
        (*sharedCallback)(resp);
        return;
    }

    const auto& files = parser.getFiles();
    if (files.size() != 1) {
        Json::Value json;
        json["code"] = 400;
        json["message"] = "exactly one file is required";
        json["data"] = Json::nullValue;
        auto resp = HttpResponse::newHttpJsonResponse(json);
        resp->setStatusCode(k400BadRequest);
        (*sharedCallback)(resp);
        return;
    }

    const auto& file = files[0];

    long long userId = 1;
    try {
        auto userIdStr = parser.getParameter<std::string>("user_id");
        if (!userIdStr.empty()) {
            userId = std::stoll(userIdStr);
        }
    } catch (...) {
        userId = 1;
    }

    if (userId <= 0) {
        Json::Value json;
        json["code"] = 400;
        json["message"] = "user_id must be positive";
        json["data"] = Json::nullValue;
        auto resp = HttpResponse::newHttpJsonResponse(json);
        resp->setStatusCode(k400BadRequest);
        (*sharedCallback)(resp);
        return;
    }

    try {
        fs::create_directories(getUploadDir());
    } catch (const std::exception& e) {
        Json::Value json;
        json["code"] = 500;
        json["message"] = std::string("failed to create upload dir: ") + e.what();
        json["data"] = Json::nullValue;
        auto resp = HttpResponse::newHttpJsonResponse(json);
        resp->setStatusCode(k500InternalServerError);
        (*sharedCallback)(resp);
        return;
    }

    const std::string originalName = sanitizeFileName(file.getFileName());
    const std::string ext = fileExtension(originalName);
    if (!isSupportedTextExtension(ext)) {
        Json::Value json;
        json["code"] = 400;
        json["message"] =
            "unsupported file type; currently supported: " + supportedDocumentTypesText();
        json["data"] = Json::nullValue;
        auto resp = HttpResponse::newHttpJsonResponse(json);
        resp->setStatusCode(k400BadRequest);
        (*sharedCallback)(resp);
        return;
    }
    const std::string mime = guessMime(file);
    const std::string sha256 = sha256Hex(std::string(file.fileContent()));
    const auto sizeBytes = static_cast<long long>(file.fileContent().size());
    const auto maxSizeBytes = maxDocumentSizeBytes();
    if (sizeBytes > maxSizeBytes) {
        Json::Value json;
        json["code"] = 400;
        json["message"] = "upload file is too large";
        json["data"]["max_document_size_bytes"] = Json::Int64(maxSizeBytes);
        auto resp = HttpResponse::newHttpJsonResponse(json);
        resp->setStatusCode(k400BadRequest);
        (*sharedCallback)(resp);
        return;
    }

    const std::string storedName = buildStoredFileName(userId, originalName, sha256);
    const std::string storagePath = (getUploadDir() / storedName).string();

    // 元数据保留原文件名，磁盘使用带唯一后缀的名称；Python Worker 后续读取共享 storage_path。
    try {
        std::ofstream ofs(storagePath, std::ios::binary);
        if (!ofs) {
            throw std::runtime_error("cannot open target file");
        }
        ofs.write(file.fileContent().data(), static_cast<std::streamsize>(file.fileContent().size()));
        ofs.close();
    } catch (const std::exception& e) {
        Json::Value json;
        json["code"] = 500;
        json["message"] = std::string("failed to save file: ") + e.what();
        json["data"] = Json::nullValue;
        auto resp = HttpResponse::newHttpJsonResponse(json);
        resp->setStatusCode(k500InternalServerError);
        (*sharedCallback)(resp);
        return;
    }

    auto dbClient = app().getDbClient("default");
    dbClient->execSqlAsync(
        R"(
            INSERT INTO documents (
                user_id, filename, mime, sha256, size_bytes, storage_path, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        )",
        [this, dbClient, sharedCallback, originalName, storagePath]
        (const drogon::orm::Result& r) mutable {
            long long docId = 0;
            try {
                docId = r.insertId();
            } catch (...) {
                Json::Value json;
                json["code"] = 500;
                json["message"] = "inserted document but failed to read doc_id";
                json["data"] = Json::nullValue;
                auto resp = HttpResponse::newHttpJsonResponse(json);
                resp->setStatusCode(k500InternalServerError);
                (*sharedCallback)(resp);
                return;
            }

            // 保存 doc_id 后再派发任务，Worker 才能据此查找正文文件和业务记录。
            pythonClient_->submitIngestJob(
                docId,
                [dbClient, sharedCallback, docId, originalName, storagePath]
                (bool ok, const Json::Value& pythonJson, const std::string& err) mutable {
                    if (!ok) {
                        // 入队失败时尽力删除文件并回滚文档记录；数据库清理失败会反映在响应中。
                        tryDeleteFile(storagePath);
                        dbClient->execSqlAsync(
                            "DELETE FROM documents WHERE id=?",
                            [sharedCallback, docId, err](const drogon::orm::Result&) mutable {
                                Json::Value json;
                                json["code"] = 502;
                                json["message"] = err;
                                json["data"]["doc_id"] = Json::Int64(docId);
                                json["data"]["rolled_back"] = true;
                                auto resp = HttpResponse::newHttpJsonResponse(json);
                                resp->setStatusCode(k502BadGateway);
                                (*sharedCallback)(resp);
                            },
                            [sharedCallback, docId, err](const drogon::orm::DrogonDbException& e) mutable {
                                Json::Value json;
                                json["code"] = 502;
                                json["message"] = err + "; rollback document cleanup failed: " + e.base().what();
                                json["data"]["doc_id"] = Json::Int64(docId);
                                json["data"]["rolled_back"] = false;
                                auto resp = HttpResponse::newHttpJsonResponse(json);
                                resp->setStatusCode(k502BadGateway);
                                (*sharedCallback)(resp);
                            },
                            docId
                        );
                        return;
                    }

                    const Json::Value& jobJson = responseDataOrSelf(pythonJson);

                    Json::Value data;
                    data["doc_id"] = Json::Int64(docId);
                    data["filename"] = originalName;
                    data["task_id"] = jobJson["task_id"];
                    data["db_task_id"] = jobJson["db_task_id"];
                    data["state"] = jobJson["state"];
                    data["status_url"] = "/v1/tasks/" + jobJson["task_id"].asString();

                    Json::Value out;
                    out["code"] = 0;
                    out["message"] = "ok";
                    out["data"] = data;

                    auto resp = HttpResponse::newHttpJsonResponse(out);
                    resp->setStatusCode(k200OK);
                    (*sharedCallback)(resp);
                }
            );
        },
        [sharedCallback, storagePath](const drogon::orm::DrogonDbException& e) mutable {
            tryDeleteFile(storagePath);

            const std::string dbError = e.base().what();
            Json::Value json;
            json["code"] = dbError.find("Duplicate entry") != std::string::npos ? 409 : 500;
            json["message"] = dbError.find("Duplicate entry") != std::string::npos
                ? "document already exists for this user"
                : std::string("db insert document failed: ") + dbError;
            json["data"] = Json::nullValue;
            auto resp = HttpResponse::newHttpJsonResponse(json);
            resp->setStatusCode(
                dbError.find("Duplicate entry") != std::string::npos
                    ? k409Conflict
                    : k500InternalServerError
            );
            (*sharedCallback)(resp);
        },
        userId,
        originalName,
        mime,
        sha256,
        sizeBytes,
        storagePath,
        "uploaded"
    );
}
