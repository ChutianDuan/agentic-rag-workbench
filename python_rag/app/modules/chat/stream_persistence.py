"""普通 Chat 流的最终落库边界：保存回答及其引用后，调用方才能发送业务 done。"""

from typing import Any, Dict, List

from python_rag.app.modules.chat.common import build_citations_from_hits
from python_rag.app.modules.messages.repo import create_message
from python_rag.app.modules.chat.repo import bulk_insert_citations


def persist_stream_result(
    session_id: int,
    answer_text: str,
    retrieval_hits: List[Dict[str, Any]],
    answer_source: str,
    context_mode: str,
    extra_meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    保存回答及检索引用，返回可用于结束事件的 assistant message 信息。

    引用保存失败时异常继续向上传播，不能把部分完成的落库过程报告成成功流。
    """

    meta = {
        "answer_source": answer_source,
        "context_mode": context_mode,
        "retrieved_count": len(retrieval_hits),
    }
    if extra_meta:
        meta.update(extra_meta)

    assistant_message = create_message(
        session_id=session_id,
        role="assistant",
        content=answer_text,
        status="SUCCESS",
        meta_json=meta,
    )

    citation_rows = build_citations_from_hits(retrieval_hits)

    if citation_rows:
        bulk_insert_citations(
            message_id=assistant_message["message_id"],
            hits=citation_rows,
        )

    return assistant_message
