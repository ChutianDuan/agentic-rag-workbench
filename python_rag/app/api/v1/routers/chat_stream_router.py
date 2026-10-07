"""普通 Chat 的 SSE 入口；生成和续传生命周期由业务模块管理。"""

from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from python_rag.app.modules.chat.schemas import ChatStreamRequest
from python_rag.app.modules.chat.resumable_stream import stream_resumable_chat

router = APIRouter(prefix="/internal/chat", tags=["chat-stream"])


@router.post("/stream")
def chat_stream(
    req: ChatStreamRequest,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
):
    """用稳定 user_message_id 和 Last-Event-ID 订阅已有生成或启动首次生成。"""
    generator = stream_resumable_chat(
        session_id=req.session_id,
        doc_id=req.doc_id,
        doc_ids=req.doc_ids,
        user_message_id=req.user_message_id,
        top_k=req.top_k or 3,
        last_event_id=last_event_id,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
