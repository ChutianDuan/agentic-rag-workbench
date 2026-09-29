from fastapi import APIRouter, File, Form, Query, UploadFile

from python_rag.app.core.logger import logger
from python_rag.app.modules.documents.service import (
    delete_document,
    save_uploaded_document,
    save_web_document,
    get_document_detail,
    list_document_items,
)
from python_rag.app.modules.documents.schemas import CreateWebDocumentRequest
from python_rag.app.modules.tasks.service import submit_ingest_job
from python_rag.app.shared.common import api_response
from python_rag.app.shared.schemas import ApiResponse

router = APIRouter(prefix="/internal", tags=["documents"])


@router.post("/documents/upload", response_model=ApiResponse)
def upload_document(
    file: UploadFile = File(...),
    user_id: int = Form(1),
):
    # 保存上传文件并创建文档记录；向量索引由独立的 ingest 任务生成。
    return api_response(save_uploaded_document(user_id=user_id, upload_file=file))


def _with_ingest_job(document_result: dict):
    doc_id = int(document_result["doc_id"])
    try:
        ingest_job = submit_ingest_job(doc_id)
    except Exception:
        try:
            delete_document(doc_id)
        except Exception:
            logger.exception("failed to rollback document after ingest queue failure")
        raise
    return {**document_result, **ingest_job}


@router.post("/documents/web", response_model=ApiResponse)
def create_web_document(req: CreateWebDocumentRequest):
    # 保存网页内容并创建文档记录；此接口不提交 ingest 任务。
    return api_response(save_web_document(user_id=req.user_id, url=req.url))


@router.post("/documents/web/ingest", response_model=ApiResponse)
def create_web_document_ingest(req: CreateWebDocumentRequest):
    document = save_web_document(user_id=req.user_id, url=req.url)
    return api_response(_with_ingest_job(document))


@router.get("/documents", response_model=ApiResponse)
def list_documents(
    user_id: int | None = Query(default=None, gt=0),
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=1000),
):
    return api_response(list_document_items(user_id=user_id, status=status, limit=limit))


@router.get("/documents/{doc_id}", response_model=ApiResponse)
def get_document(doc_id: int):
    return api_response(get_document_detail(doc_id))


@router.delete("/documents/{doc_id}", response_model=ApiResponse)
def remove_document(doc_id: int):
    return api_response(delete_document(doc_id))
