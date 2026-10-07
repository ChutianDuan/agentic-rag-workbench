"""内部 API 组装入口：统一异常响应，注册薄路由；业务流程在 modules 和 agent 中实现。"""

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from python_rag.app.core.errors import AppError
from python_rag.app.core.exception_handlers import (
    app_error_handler,
    generic_exception_handler,
    http_exception_handler,
    request_validation_error_handler,
)
from python_rag.app.api.v1.routers import register_routers

app = FastAPI(
    title="Python RAG",
    version="0.1.0",
    description="Internal Python service for ingest, retrieval and chat tasks.",
)

app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, request_validation_error_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, generic_exception_handler)

register_routers(app)
