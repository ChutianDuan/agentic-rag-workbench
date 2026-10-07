"""Celery 入库入口：解析与向量化分别排队和记录进度，向量化失败仍保留解析成果。"""

import uuid

from python_rag.app.core.error_codes import TaskState
from python_rag.app.core.logger import logger
from python_rag.app.modules.documents.repo import update_document_index_status
from python_rag.app.modules.documents.schemas import DocumentIndexStatus
from python_rag.app.modules.retrieval.maintenance_repo import update_vector_index_job_by_task_id
from python_rag.app.modules.ingest.service import (
    build_embedding_index_for_document,
    parse_document_for_chunks,
    run_ingest_for_document,
)
from python_rag.app.modules.tasks.repo import create_task_record, update_task_record
from python_rag.app.workers.celery_app import celery_app


def _progress_callback(task):
    def callback(state: str, meta: dict):
        task.update_state(state=state, meta=meta)

    return callback


def _update_vector_index_job(celery_task_id, **kwargs):
    try:
        update_vector_index_job_by_task_id(celery_task_id, **kwargs)
    except Exception:
        logger.exception(
            "failed to update vector_index_jobs celery_task_id=%s",
            celery_task_id,
        )


@celery_app.task(bind=True, name="python_rag.tasks.parse_document")
def parse_document_task(self, doc_id: int):
    """完成解析后创建独立向量化任务，返回其 ID，供客户端继续跟踪第二阶段。"""
    celery_task_id = self.request.id
    result = parse_document_for_chunks(
        doc_id=doc_id,
        celery_task_id=celery_task_id,
        progress_callback=_progress_callback(self),
    )

    # 先保存业务任务记录再派发，并显式指定同一个 Celery ID，避免查询时找不到任务元数据。
    embedding_task_id = str(uuid.uuid4())
    db_task_id = create_task_record(
        celery_task_id=embedding_task_id,
        task_type="build_embedding",
        entity_type="document",
        entity_id=doc_id,
        state=TaskState.PENDING,
        progress=0,
        meta={
            "stage": "queued",
            "doc_id": doc_id,
            "parent_task_id": celery_task_id,
        },
    )
    try:
        build_embedding_task.apply_async(
            kwargs={"doc_id": doc_id},
            task_id=embedding_task_id,
        )
    except Exception as exc:
        # 解析已经完成，但向量化未能入队：文档索引和父子任务都要报告失败。
        logger.exception("failed to queue build_embedding_task")
        update_document_index_status(doc_id, DocumentIndexStatus.FAILED, str(exc))
        update_task_record(
            celery_task_id=celery_task_id,
            state=TaskState.FAILURE,
            progress=100,
            meta={
                "stage": "embedding_queue_failed",
                "doc_id": doc_id,
            },
            error=str(exc),
        )
        update_task_record(
            celery_task_id=embedding_task_id,
            state=TaskState.FAILURE,
            progress=100,
            meta={
                "stage": "queue_failed",
                "doc_id": doc_id,
                "parent_task_id": celery_task_id,
            },
            error=str(exc),
        )
        raise

    result = dict(result)
    result.update(
        {
            "embedding_task_id": embedding_task_id,
            "embedding_db_task_id": db_task_id,
            "embedding_status_url": "/internal/tasks/{0}".format(embedding_task_id),
        }
    )
    update_task_record(
        celery_task_id=celery_task_id,
        state=TaskState.SUCCESS,
        progress=100,
        meta=result,
    )
    return result


@celery_app.task(bind=True, name="python_rag.tasks.build_embedding")
def build_embedding_task(self, doc_id: int):
    """执行第二阶段向量化，同步维护索引作业状态，成功后文档才进入可检索集合。"""
    celery_task_id = self.request.id
    _update_vector_index_job(
        celery_task_id,
        status="indexing",
        meta={
            "stage": "embedding_started",
            "doc_id": doc_id,
            "provider": "lancedb",
        },
    )
    try:
        result = build_embedding_index_for_document(
            doc_id=doc_id,
            celery_task_id=celery_task_id,
            progress_callback=_progress_callback(self),
        )
    except Exception as exc:
        _update_vector_index_job(
            celery_task_id,
            status="failed",
            error_message=str(exc),
            meta={
                "stage": "embedding_failed",
                "doc_id": doc_id,
                "provider": "lancedb",
            },
        )
        raise

    _update_vector_index_job(
        celery_task_id,
        status="indexed",
        chunk_count=result.get("chunk_count"),
        meta=result,
    )
    return result


@celery_app.task(bind=True, name="python_rag.tasks.ingest_document")
def ingest_task(self, doc_id: int):
    return run_ingest_for_document(
        doc_id=doc_id,
        celery_task_id=self.request.id,
        progress_callback=_progress_callback(self),
    )
