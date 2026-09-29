import os

from python_rag.app.core.config import MAX_DOCUMENT_SIZE_BYTES
from python_rag.app.modules.documents.schemas import DocumentIndexStatus, DocumentState
from python_rag.app.core.error_codes import (
    ERR_DB_ERROR,
    ERR_DOCUMENT_NOT_FOUND,
    ERR_INVALID_REQUEST,
)
from python_rag.app.core.errors import AppError
from python_rag.app.core.logger import logger
from python_rag.app.infra.storage import build_upload_path, resolve_storage_path, save_bytes_to_path
from python_rag.app.modules.documents.repo import (
    create_document_record,
    delete_document_by_id,
    get_document_by_id,
    get_document_index_by_doc_id,
    list_documents,
)
from python_rag.app.modules.ingest.chunking_service import validate_supported_document_filename
from python_rag.app.modules.ingest.web_page_service import fetch_web_page_document
from python_rag.app.shared.hash_utils import sha256_bytes
from python_rag.app.retrieval.indexing_service import delete_document_vectors


def save_uploaded_document(user_id, upload_file):
    """校验上传文件，保存到本地，并创建数据库记录。"""
    file_path = None
    try:
        validate_supported_document_filename(upload_file.filename or "")

        content = upload_file.file.read()
        if not content:
            raise AppError(ERR_INVALID_REQUEST, "empty upload file")
        if len(content) > MAX_DOCUMENT_SIZE_BYTES:
            raise AppError(
                ERR_INVALID_REQUEST,
                "upload file is too large; max supported size is {0} bytes".format(
                    MAX_DOCUMENT_SIZE_BYTES,
                ),
            )

        file_path = build_upload_path(upload_file.filename)
        save_bytes_to_path(content, file_path)

        try:
            doc_id = create_document_record(
                user_id=user_id,
                filename=upload_file.filename,
                mime=upload_file.content_type or "application/octet-stream",
                sha256=sha256_bytes(content),
                size_bytes=len(content),
                storage_path=file_path,
                status=DocumentState.UPLOADED,
            )
        except Exception:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    logger.exception("failed to cleanup uploaded file after document insert failure")
            raise

        return {
            "doc_id": doc_id,
            "filename": upload_file.filename,
            "status": DocumentState.UPLOADED,
            "index_status": DocumentIndexStatus.NOT_INDEXED,
        }
    except AppError:
        raise
    except Exception as e:
        logger.exception("save_uploaded_document failed")
        raise AppError(ERR_DB_ERROR, "save_uploaded_document failed: {0}".format(e))


def save_web_document(user_id: int, url: str):
    file_path = None
    try:
        web_document = fetch_web_page_document(url)
        content = web_document.content_bytes
        if not content:
            raise AppError(ERR_INVALID_REQUEST, "empty web page content")
        if len(content) > MAX_DOCUMENT_SIZE_BYTES:
            raise AppError(
                ERR_INVALID_REQUEST,
                "web page content is too large; max supported size is {0} bytes".format(
                    MAX_DOCUMENT_SIZE_BYTES,
                ),
            )

        file_path = build_upload_path(web_document.filename)
        save_bytes_to_path(content, file_path)

        try:
            doc_id = create_document_record(
                user_id=user_id,
                filename=web_document.filename,
                mime=web_document.mime,
                sha256=sha256_bytes(content),
                size_bytes=len(content),
                storage_path=file_path,
                status=DocumentState.UPLOADED,
            )
        except Exception:
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    logger.exception("failed to cleanup web document after insert failure")
            raise

        return {
            "doc_id": doc_id,
            "filename": web_document.filename,
            "status": DocumentState.UPLOADED,
            "index_status": DocumentIndexStatus.NOT_INDEXED,
            "source_url": web_document.url,
            "final_url": web_document.final_url,
            "title": web_document.title,
        }
    except AppError:
        raise
    except Exception as e:
        logger.exception("save_web_document failed")
        raise AppError(ERR_DB_ERROR, "save_web_document failed: {0}".format(e))


def _remove_file_if_exists(path):
    if not path:
        return False

    resolved_path = resolve_storage_path(path)
    if not resolved_path or not os.path.exists(resolved_path):
        return False

    os.remove(resolved_path)
    return True


def delete_document(doc_id):
    try:
        row = get_document_by_id(doc_id)
        if not row:
            raise AppError(ERR_DOCUMENT_NOT_FOUND, "document not found", http_status=404)

        index_row = get_document_index_by_doc_id(doc_id)
        index_paths = []
        if index_row:
            index_paths.extend([index_row.get("index_path"), index_row.get("mapping_path")])

        stats = delete_document_by_id(doc_id)
        if stats.get("deleted_documents", 0) <= 0:
            raise AppError(ERR_DOCUMENT_NOT_FOUND, "document not found", http_status=404)

        try:
            delete_document_vectors(doc_id)
        except Exception:
            logger.exception("failed to delete LanceDB vectors for doc_id=%s", doc_id)

        deleted_files = []
        for path in [row.get("storage_path"), *index_paths]:
            try:
                if _remove_file_if_exists(path):
                    deleted_files.append(path)
            except Exception:
                logger.exception("failed to delete document file: %s", path)
                raise

        return {
            "doc_id": doc_id,
            "deleted": True,
            "deleted_files": deleted_files,
            **stats,
        }
    except AppError:
        raise
    except Exception as e:
        logger.exception("delete_document failed")
        raise AppError(ERR_DB_ERROR, "delete_document failed: {0}".format(e))


def get_document_detail(doc_id):
    try:
        row = get_document_by_id(doc_id)
        if not row:
            raise AppError(ERR_DOCUMENT_NOT_FOUND, "document not found", http_status=404)

        return {
            "doc_id": row["id"],
            "user_id": row["user_id"],
            "filename": row["filename"],
            "mime": row["mime"],
            "size_bytes": row["size_bytes"],
            "status": row["status"],
            "index_status": row.get("index_status"),
            "storage_path": row["storage_path"],
            "error_message": row["error_message"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        }
    except AppError:
        raise
    except Exception as e:
        logger.exception("get_document_detail failed")
        raise AppError(ERR_DB_ERROR, "get_document_detail failed: {0}".format(e))


def list_document_items(user_id=None, status=None, limit=100):
    try:
        rows = list_documents(user_id=user_id, status=status, limit=limit)
        result = []
        for row in rows:
            chunk_count = row.get("chunk_count")
            index_status = row.get("index_status")
            result.append(
                {
                    "doc_id": row["id"],
                    "user_id": row["user_id"],
                    "filename": row["filename"],
                    "mime": row["mime"],
                    "size_bytes": row["size_bytes"],
                    "status": row["status"],
                    "index_status": row.get("index_status"),
                    "storage_path": row["storage_path"],
                    "error_message": row.get("error_message"),
                    "error": row.get("error_message"),
                    "chunks": chunk_count,
                    "vectorized": str(index_status or "").lower() in ("indexed", "ready"),
                    "index_status": index_status,
                    "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                    "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
                }
            )
        return {"items": result}
    except Exception as e:
        logger.exception("list_document_items failed")
        raise AppError(ERR_DB_ERROR, "list_document_items failed: {0}".format(e))
