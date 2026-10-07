"""文档入库业务：先将解析结果保存为 MySQL chunks，再从这些记录生成向量索引。"""

import time

from python_rag.app.core.config import INGEST_CHUNK_OVERLAP, INGEST_CHUNK_SIZE
from python_rag.app.core.error_codes import ERR_CELERY_ERROR, TaskState
from python_rag.app.core.errors import AppError
from python_rag.app.core.logger import logger
from python_rag.app.infra.storage import resolve_storage_path
from python_rag.app.modules.documents.repo import (
    CHUNK_EMBEDDING_EMBEDDED,
    CHUNK_EMBEDDING_FAILED,
    CHUNK_VECTOR_FAILED,
    CHUNK_VECTOR_INDEXED,
    delete_chunks_by_doc_id,
    bulk_insert_chunks,
    get_document_by_id,
    list_chunks_by_doc_id,
    update_chunks_index_status,
    update_document_status,
    upsert_document_index,
)
from python_rag.app.modules.documents.schemas import DocumentIndexStatus, DocumentState
from python_rag.app.modules.ingest.chunking_service import (
    chunk_text_by_title,
    extract_text_from_document,
    validate_supported_document_filename,
)
from python_rag.app.modules.ingest.embedding_service import (
    embed_documents,
    get_embedding_model_name,
)
from python_rag.app.modules.monitor.request_metrics import (
    estimate_embedding_cost_usd,
    is_timeout_error,
    record_request_metric,
)
from python_rag.app.retrieval.indexing_service import upsert_document_chunk_vectors
from python_rag.app.modules.tasks.repo import update_task_record


def _emit_progress(celery_task_id, state, progress, meta, progress_callback=None, error=None):
    """先更新业务任务记录，再尝试同步 Celery 进度；回调失败不覆盖主任务的处理结果。"""
    update_task_record(
        celery_task_id=celery_task_id,
        state=state,
        progress=progress,
        meta=meta,
        error=error,
    )

    if progress_callback and state != TaskState.FAILURE:
        try:
            progress_callback(state=state, meta=dict(meta or {}, progress=progress))
        except Exception:
            logger.exception("progress_callback failed")


def _ready_latency_ms(doc):
    if not doc or not doc.get("created_at"):
        return None
    return int(max(0, (time.time() - doc["created_at"].timestamp()) * 1000))


def _get_document_or_raise(doc_id):
    doc = get_document_by_id(doc_id)
    if not doc:
        raise AppError(ERR_CELERY_ERROR, "document not found")
    validate_supported_document_filename(doc.get("filename") or "")
    return doc


def parse_document_for_chunks(doc_id, celery_task_id, progress_callback=None):
    """解析已落盘文档并替换其 chunks，返回解析统计；本阶段完成时尚不能检索。"""
    started_at = time.perf_counter()
    text_extract_ms = None
    chunking_ms = None
    chunk_insert_ms = None
    chunk_count = 0

    try:
        doc = _get_document_or_raise(doc_id)
        update_document_status(
            doc_id,
            DocumentState.UPLOADED,
            None,
            index_status=DocumentIndexStatus.PARSING,
        )
        _emit_progress(
            celery_task_id=celery_task_id,
            state=TaskState.STARTED,
            progress=5,
            meta={
                "stage": "parse_started",
                "doc_id": doc_id,
                "filename": doc["filename"],
                "document_status": DocumentState.UPLOADED,
                "index_status": DocumentIndexStatus.PARSING,
            },
            progress_callback=progress_callback,
        )

        text_started_at = time.perf_counter()
        text = extract_text_from_document(
            path=resolve_storage_path(doc["storage_path"]),
            filename=doc.get("filename") or "",
        )
        text_extract_ms = int((time.perf_counter() - text_started_at) * 1000)
        if not text or not text.strip():
            raise AppError(ERR_CELERY_ERROR, "document text is empty")

        _emit_progress(
            celery_task_id=celery_task_id,
            state=TaskState.PROGRESS,
            progress=35,
            meta={
                "stage": "document_loaded",
                "doc_id": doc_id,
                "filename": doc["filename"],
                "char_count": len(text),
            },
            progress_callback=progress_callback,
        )

        chunking_started_at = time.perf_counter()
        chunks = chunk_text_by_title(
            text=text,
            filename=doc.get("filename") or "",
            chunk_size=INGEST_CHUNK_SIZE,
            overlap=INGEST_CHUNK_OVERLAP,
        )
        chunking_ms = int((time.perf_counter() - chunking_started_at) * 1000)
        if not chunks:
            raise AppError(ERR_CELERY_ERROR, "chunk result is empty")

        _emit_progress(
            celery_task_id=celery_task_id,
            state=TaskState.PROGRESS,
            progress=65,
            meta={
                "stage": "document_chunked",
                "doc_id": doc_id,
                "filename": doc["filename"],
                "chunk_count": len(chunks),
                "chunk_size": INGEST_CHUNK_SIZE,
                "chunk_overlap": INGEST_CHUNK_OVERLAP,
            },
            progress_callback=progress_callback,
        )

        # MySQL 是正文来源；后续向量化必须读取这里生成的 chunk ID 和正文，保持两者对应。
        insert_started_at = time.perf_counter()
        delete_chunks_by_doc_id(doc_id)
        chunk_count = bulk_insert_chunks(doc_id, chunks)
        chunk_insert_ms = int((time.perf_counter() - insert_started_at) * 1000)
        if chunk_count <= 0:
            raise AppError(ERR_CELERY_ERROR, "bulk_insert_chunks inserted 0 rows")

        # 正文落库只推进到 PARSED，文档仍为 UPLOADED；向量索引完成后才推进到 INDEXED。
        update_document_status(
            doc_id,
            DocumentState.UPLOADED,
            None,
            index_status=DocumentIndexStatus.PARSED,
        )
        result = {
            "stage": "chunks_written",
            "doc_id": doc_id,
            "filename": doc["filename"],
            "document_status": DocumentState.UPLOADED,
            "index_status": DocumentIndexStatus.PARSED,
            "chunk_count": chunk_count,
            "chunk_embedding_status": "pending",
            "next_stage": "build_embedding_task",
            "timings_ms": {
                "text_extract_ms": text_extract_ms,
                "chunking_ms": chunking_ms,
                "chunk_insert_ms": chunk_insert_ms,
            },
        }
        _emit_progress(
            celery_task_id=celery_task_id,
            state=TaskState.SUCCESS,
            progress=100,
            meta=result,
            progress_callback=progress_callback,
        )
        return result
    except Exception as exc:
        logger.exception("parse_document_for_chunks failed")
        try:
            update_document_status(
                doc_id,
                DocumentState.FAILED,
                str(exc),
                index_status=DocumentIndexStatus.FAILED,
            )
        except Exception:
            logger.exception("failed to mark document parse failure")
        try:
            _emit_progress(
                celery_task_id=celery_task_id,
                state=TaskState.FAILURE,
                progress=100,
                meta={
                    "stage": "parse_failed",
                    "doc_id": doc_id,
                    "error": str(exc),
                },
                error=str(exc),
                progress_callback=progress_callback,
            )
        except Exception:
            logger.exception("failed to update parse task failure")
        record_request_metric(
            request_type="ingest",
            status="error",
            channel="celery",
            doc_id=doc_id,
            celery_task_id=celery_task_id,
            e2e_latency_ms=int((time.perf_counter() - started_at) * 1000),
            timed_out=is_timeout_error(exc),
            error_message=str(exc),
            extra={
                "stage": "parse_document",
                "text_extract_ms": text_extract_ms,
                "chunking_ms": chunking_ms,
                "chunk_insert_ms": chunk_insert_ms,
                "chunk_count": chunk_count,
            },
        )
        raise


def build_embedding_index_for_document(doc_id, celery_task_id, progress_callback=None):
    """读取持久化 chunks、生成向量并写入 LanceDB；全部完成后才标记文档为 indexed。"""
    started_at = time.perf_counter()
    embedding_ms = None
    index_ms = None
    embedding_tokens = None

    try:
        embedding_model_name = get_embedding_model_name()
        doc = _get_document_or_raise(doc_id)
        chunk_rows = list_chunks_by_doc_id(doc_id, limit=None)
        if not chunk_rows:
            raise AppError(ERR_CELERY_ERROR, "no chunks found for embedding")

        update_document_status(
            doc_id,
            DocumentState.UPLOADED,
            None,
            index_status=DocumentIndexStatus.INDEXING,
        )
        _emit_progress(
            celery_task_id=celery_task_id,
            state=TaskState.STARTED,
            progress=10,
            meta={
                "stage": "embedding_started",
                "doc_id": doc_id,
                "filename": doc["filename"],
                "chunk_count": len(chunk_rows),
                "embedding_model": embedding_model_name,
                "index_status": DocumentIndexStatus.INDEXING,
            },
            progress_callback=progress_callback,
        )

        texts = [row.get("content") or row.get("text") or "" for row in chunk_rows]
        embedding_started_at = time.perf_counter()
        vectors = embed_documents(texts)
        embedding_ms = int((time.perf_counter() - embedding_started_at) * 1000)
        if vectors is None or len(vectors) != len(chunk_rows):
            raise AppError(ERR_CELERY_ERROR, "embedding result count mismatch")

        chunk_ids = [row["id"] for row in chunk_rows]
        update_chunks_index_status(
            doc_id,
            chunk_ids=chunk_ids,
            embedding_status=CHUNK_EMBEDDING_EMBEDDED,
        )
        _emit_progress(
            celery_task_id=celery_task_id,
            state=TaskState.PROGRESS,
            progress=65,
            meta={
                "stage": "embedding_finished",
                "doc_id": doc_id,
                "filename": doc["filename"],
                "chunk_count": len(chunk_rows),
                "embedding_model": embedding_model_name,
            },
            progress_callback=progress_callback,
        )

        # 向量索引可重建；写入成功后再更新 MySQL 索引元数据和文档可检索状态。
        index_started_at = time.perf_counter()
        index_meta = upsert_document_chunk_vectors(
            document=doc,
            chunk_rows=chunk_rows,
            vectors=vectors,
        )
        upsert_document_index(
            doc_id=doc_id,
            index_type=index_meta["index_type"],
            embedding_model=embedding_model_name,
            dimension=index_meta["dimension"],
            index_path="",
            mapping_path="",
            chunk_count=index_meta["chunk_count"],
            status="READY",
        )
        index_ms = int((time.perf_counter() - index_started_at) * 1000)
        update_chunks_index_status(
            doc_id,
            chunk_ids=chunk_ids,
            vector_index_status=CHUNK_VECTOR_INDEXED,
        )
        update_document_status(
            doc_id,
            DocumentState.UPLOADED,
            None,
            index_status=DocumentIndexStatus.INDEXED,
        )

        embedding_tokens = sum(int(row.get("tokens_est") or 0) for row in chunk_rows)
        total_ingest_ms = int((time.perf_counter() - started_at) * 1000)
        ready_latency_ms = _ready_latency_ms(doc)
        cost_usd = estimate_embedding_cost_usd(embedding_tokens)
        result = {
            "stage": "finished",
            "doc_id": doc_id,
            "filename": doc["filename"],
            "chunk_count": len(chunk_rows),
            "document_status": DocumentState.UPLOADED,
            "index_status": DocumentIndexStatus.INDEXED,
            "chunk_embedding_status": CHUNK_EMBEDDING_EMBEDDED,
            "chunk_vector_index_status": CHUNK_VECTOR_INDEXED,
            "embedding_model": embedding_model_name,
            "index_type": index_meta["index_type"],
            "lancedb_uri": index_meta["uri"],
            "lancedb_table": index_meta["table_name"],
            "dimension": index_meta["dimension"],
            "ingest_ready_ms": ready_latency_ms,
            "ingest_runtime_ms": total_ingest_ms,
            "embedding_tokens": embedding_tokens,
            "cost_usd": cost_usd,
            "timings_ms": {
                "embedding_ms": embedding_ms,
                "index_ms": index_ms,
            },
        }
        record_request_metric(
            request_type="ingest",
            status="success",
            channel="celery",
            doc_id=doc_id,
            celery_task_id=celery_task_id,
            e2e_latency_ms=total_ingest_ms,
            ready_latency_ms=ready_latency_ms,
            embedding_tokens=embedding_tokens,
            cost_usd=cost_usd,
            answer_source="embedding",
            extra={
                **result["timings_ms"],
                "chunk_count": len(chunk_rows),
                "document_size_bytes": int(doc.get("size_bytes") or 0),
                "index_type": index_meta["index_type"],
            },
        )
        _emit_progress(
            celery_task_id=celery_task_id,
            state=TaskState.SUCCESS,
            progress=100,
            meta=result,
            progress_callback=progress_callback,
        )
        return result
    except Exception as exc:
        logger.exception("build_embedding_index_for_document failed")
        try:
            update_document_status(
                doc_id,
                DocumentState.FAILED,
                str(exc),
                index_status=DocumentIndexStatus.FAILED,
            )
            update_chunks_index_status(
                doc_id,
                embedding_status=CHUNK_EMBEDDING_FAILED,
                vector_index_status=CHUNK_VECTOR_FAILED,
            )
        except Exception:
            logger.exception("failed to mark embedding/index failure")
        try:
            _emit_progress(
                celery_task_id=celery_task_id,
                state=TaskState.FAILURE,
                progress=100,
                meta={
                    "stage": "embedding_failed",
                    "doc_id": doc_id,
                    "error": str(exc),
                },
                error=str(exc),
                progress_callback=progress_callback,
            )
        except Exception:
            logger.exception("failed to update embedding task failure")
        record_request_metric(
            request_type="ingest",
            status="error",
            channel="celery",
            doc_id=doc_id,
            celery_task_id=celery_task_id,
            e2e_latency_ms=int((time.perf_counter() - started_at) * 1000),
            embedding_tokens=embedding_tokens,
            cost_usd=estimate_embedding_cost_usd(embedding_tokens),
            timed_out=is_timeout_error(exc),
            error_message=str(exc),
            extra={
                "stage": "build_embedding",
                "embedding_ms": embedding_ms,
                "index_ms": index_ms,
            },
        )
        raise


def run_ingest_for_document(doc_id, celery_task_id, progress_callback=None):
    parse_document_for_chunks(
        doc_id=doc_id,
        celery_task_id=celery_task_id,
        progress_callback=progress_callback,
    )
    return build_embedding_index_for_document(
        doc_id=doc_id,
        celery_task_id=celery_task_id,
        progress_callback=progress_callback,
    )
