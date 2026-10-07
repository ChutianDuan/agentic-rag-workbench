"""将检索片段及来源标识组装成 RAG Prompt，截断正文时仍保留可回查的来源信息。"""

from typing import Dict, List, Optional

from python_rag.app.core.config import CHAT_MAX_CHUNK_CHARS
from python_rag.app.modules.retrieval.schemas import PromptBuildResult, RetrievedChunk


SYSTEM_INSTRUCTION = (
    "你是一个RAG问答助手。\n"
    "请严格基于提供的检索内容回答问题。\n"
    "如果答案无法从检索内容中得到，请明确说明："
    "“根据当前检索内容无法确定”。\n"
    "不要编造未在检索内容中出现的事实。\n"
    "回答要简洁、准确、有条理。\n"
    "当检索内容中存在多个片段时，请优先整合共同信息。"
)


def _truncate_text(text: str, max_chars: Optional[int]) -> str:
    if not text:
        return ""
    if not max_chars or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def build_context_block(chunks: List[RetrievedChunk]) -> str:
    parts = []
    for chunk in chunks:
        header = "[Chunk %d]" % chunk.rank
        meta_parts = []

        if chunk.doc_id is not None:
            meta_parts.append("doc_id=%s" % chunk.doc_id)
        if chunk.chunk_index is not None:
            meta_parts.append("chunk_index=%s" % chunk.chunk_index)
        if chunk.score is not None:
            meta_parts.append("score=%.4f" % chunk.score)
        if chunk.bm25_score is not None:
            meta_parts.append("bm25_score=%.4f" % chunk.bm25_score)
        if chunk.rrf_score is not None:
            meta_parts.append("rrf_score=%.4f" % chunk.rrf_score)
        if chunk.rerank_score is not None:
            meta_parts.append("rerank_score=%.4f" % chunk.rerank_score)
        if chunk.original_rank is not None:
            meta_parts.append("original_rank=%s" % chunk.original_rank)

        if meta_parts:
            header += " | " + " | ".join(meta_parts)

        parts.append(header)
        parts.append(_truncate_text(chunk.content, CHAT_MAX_CHUNK_CHARS))
        parts.append("")

    return "\n".join(parts).strip()


def build_normal_prompt(question: str, chunks: List[RetrievedChunk]) -> PromptBuildResult:
    context_text = build_context_block(chunks)
    user_prompt = (
        "已检索内容：\n\n"
        "%s\n\n"
        "用户问题：\n"
        "%s\n\n"
        "请严格依据检索内容回答。"
        "如果无法从检索内容中确定答案，请明确说明无法确定。"
    ) % (context_text, question.strip())

    return PromptBuildResult(
        system_instruction=SYSTEM_INSTRUCTION,
        user_prompt=user_prompt,
        context_text=context_text,
        context_count=len(chunks),
        mode="normal",
    )


def build_low_confidence_prompt(question: str, chunks: List[RetrievedChunk]) -> PromptBuildResult:
    context_text = build_context_block(chunks)
    user_prompt = (
        "已检索内容（注意：以下片段与问题的相关性或置信度偏低，仅可作为弱参考）：\n\n"
        "%s\n\n"
        "用户问题：\n"
        "%s\n\n"
        "请严格遵守以下要求：\n"
        "1. 只能基于检索内容回答，不要补充外部知识，不要猜测。\n"
        "2. 如果检索内容不能直接支持答案，请明确回答：根据当前检索内容无法确定。\n"
        "3. 如果检索内容仅提供了部分线索，请用保守措辞回答，例如“从当前片段来看”或“现有片段仅显示”。\n"
        "4. 不要把不确定的信息表述成确定事实。"
    ) % (context_text, question.strip())

    return PromptBuildResult(
        system_instruction=SYSTEM_INSTRUCTION,
        user_prompt=user_prompt,
        context_text=context_text,
        context_count=len(chunks),
        mode="low_confidence",
    )


def build_no_context_prompt(question: str) -> PromptBuildResult:
    context_text = ""
    user_prompt = (
        "当前没有检索到可用文档片段。\n\n"
        "用户问题：\n"
        "%s\n\n"
        "请不要编造答案，请明确说明根据当前检索内容无法确定。"
    ) % question.strip()

    return PromptBuildResult(
        system_instruction=SYSTEM_INSTRUCTION,
        user_prompt=user_prompt,
        context_text=context_text,
        context_count=0,
        mode="no_context",
    )


def build_prompt(question: str, chunks: List[RetrievedChunk], mode: str) -> PromptBuildResult:
    if mode == "no_context":
        return build_no_context_prompt(question)
    if mode == "low_confidence":
        return build_low_confidence_prompt(question, chunks)
    return build_normal_prompt(question, chunks)


def to_messages(prompt_result: PromptBuildResult) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": prompt_result.system_instruction},
        {"role": "user", "content": prompt_result.user_prompt},
    ]

if __name__ == "__main__":
    chunks = [
        RetrievedChunk(rank=1, content="苹果是一种水果。", doc_id=1, chunk_index=0, score=0.91),
        RetrievedChunk(rank=2, content="苹果富含维生素。", doc_id=1, chunk_index=1, score=0.88),
    ]

    prompt_result = build_prompt("苹果有什么特点？", chunks, "normal")
    print(prompt_result)
    print(to_messages(prompt_result))
