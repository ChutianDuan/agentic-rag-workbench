#!/usr/bin/env python3
import argparse
import concurrent.futures
import json
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ModuleNotFoundError:
    requests = None


def percentile(values: List[float], p: int) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * p / 100.0
    low = int(rank)
    high = min(len(ordered) - 1, low + 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def latency_summary(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"p50": None, "p95": None, "p99": None, "avg": None}
    return {
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
        "p99": round(percentile(values, 99), 2),
        "avg": round(statistics.mean(values), 2),
    }


def get_json(method: str, url: str, **kwargs) -> Dict[str, Any]:
    if requests is None:
        raise RuntimeError("requests is not installed")
    response = requests.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
    response.raise_for_status()
    return response.json()


def create_session(gateway_base_url: str, user_id: int, title: str) -> int:
    payload = {"user_id": user_id, "title": title}
    data = get_json(
        "POST",
        f"{gateway_base_url}/v1/sessions",
        json=payload,
        timeout=15,
    )
    return int(data["data"]["session_id"])


def create_user_message(
    python_base_url: str,
    session_id: int,
    content: str,
) -> int:
    payload = {
        "role": "user",
        "content": content,
        "status": "PENDING",
    }
    data = get_json(
        "POST",
        f"{python_base_url}/internal/sessions/{session_id}/messages",
        json=payload,
        timeout=15,
    )
    return int(data["data"]["message_id"])


def upload_and_ingest_document(
    gateway_base_url: str,
    file_path: str,
    user_id: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    with open(file_path, "rb") as file_obj:
        response = requests.post(
            f"{gateway_base_url}/v1/documents",
            data={"user_id": str(user_id)},
            files={"file": (Path(file_path).name, file_obj)},
            timeout=60,
        )
    response.raise_for_status()
    payload = response.json()
    doc_id = int(payload["doc_id"])
    task_id = payload["task_id"]

    deadline = time.time() + timeout_seconds
    last_status: Dict[str, Any] = {}
    while time.time() < deadline:
        last_status = get_json(
            "GET",
            f"{gateway_base_url}/v1/tasks/{task_id}",
            timeout=15,
        )
        state = last_status.get("state")
        if state == "SUCCESS":
            return {
                "doc_id": doc_id,
                "task_id": task_id,
                "ingest_ready_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "task_meta": last_status.get("meta") or {},
            }
        if state in ("FAILURE", "FAILED"):
            raise RuntimeError(f"ingest failed: {json.dumps(last_status, ensure_ascii=False)}")
        time.sleep(1)

    raise TimeoutError(f"ingest task timed out after {timeout_seconds}s: {json.dumps(last_status, ensure_ascii=False)}")


class MonitorSampler:
    def __init__(self, python_base_url: str, interval_seconds: float):
        self.python_base_url = python_base_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.max_concurrent_sessions = 0
        self.max_worker_queue_depth = 0
        self.max_active_sse_connections = 0
        self.samples: List[Dict[str, Any]] = []

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> Dict[str, Any]:
        self.stop_event.set()
        self.thread.join(timeout=2)
        return {
            "max_concurrent_sessions": self.max_concurrent_sessions,
            "max_worker_queue_depth": self.max_worker_queue_depth,
            "max_active_sse_connections": self.max_active_sse_connections,
            "sample_count": len(self.samples),
        }

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                overview = get_json(
                    "GET",
                    f"{self.python_base_url}/internal/monitor/overview",
                    timeout=10,
                )
                throughput = overview.get("throughput") or {}
                self.max_concurrent_sessions = max(
                    self.max_concurrent_sessions,
                    int(throughput.get("concurrent_sessions") or 0),
                )
                self.max_worker_queue_depth = max(
                    self.max_worker_queue_depth,
                    int(throughput.get("worker_queue_depth") or 0),
                )
                self.max_active_sse_connections = max(
                    self.max_active_sse_connections,
                    int(throughput.get("active_sse_connections") or 0),
                )
                self.samples.append(
                    {
                        "ts": time.time(),
                        "throughput": throughput,
                        "quality": overview.get("quality") or {},
                    }
                )
            except Exception:
                pass
            time.sleep(self.interval_seconds)


def run_async_chat_request(
    gateway_base_url: str,
    user_id: int,
    doc_id: int,
    question: str,
    top_k: int,
    timeout_seconds: int,
    index: int,
) -> Dict[str, Any]:
    session_id = create_session(gateway_base_url, user_id, f"benchmark-async-{index}")
    started_at = time.perf_counter()
    submit_payload = {
        "doc_id": doc_id,
        "content": question,
        "top_k": top_k,
    }
    submit_resp = get_json(
        "POST",
        f"{gateway_base_url}/v1/sessions/{session_id}/messages",
        json=submit_payload,
        timeout=15,
    )
    task_id = submit_resp["data"]["task_id"]
    message_id = int(submit_resp["data"]["message_id"])

    deadline = time.time() + timeout_seconds
    last_status: Dict[str, Any] = {}
    while time.time() < deadline:
        last_status = get_json(
            "GET",
            f"{gateway_base_url}/v1/tasks/{task_id}",
            timeout=10,
        )
        state = last_status.get("state")
        if state == "SUCCESS":
            meta = last_status.get("meta") or {}
            return {
                "mode": "async",
                "success": True,
                "session_id": session_id,
                "message_id": message_id,
                "task_id": task_id,
                "client_e2e_latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "server_meta": meta,
            }
        if state in ("FAILURE", "FAILED"):
            return {
                "mode": "async",
                "success": False,
                "timed_out": False,
                "session_id": session_id,
                "message_id": message_id,
                "task_id": task_id,
                "error": last_status.get("error") or json.dumps(last_status, ensure_ascii=False),
            }
        time.sleep(0.5)

    return {
        "mode": "async",
        "success": False,
        "timed_out": True,
        "session_id": session_id,
        "message_id": message_id,
        "task_id": task_id,
        "error": f"async request timed out after {timeout_seconds}s",
    }


def run_stream_chat_request(
    python_base_url: str,
    gateway_base_url: str,
    user_id: int,
    doc_id: int,
    question: str,
    top_k: int,
    timeout_seconds: int,
    index: int,
) -> Dict[str, Any]:
    session_id = create_session(gateway_base_url, user_id, f"benchmark-stream-{index}")
    user_message_id = create_user_message(python_base_url, session_id, question)
    started_at = time.perf_counter()
    ttft_ms = None
    done_meta: Dict[str, Any] = {}

    try:
        with requests.post(
            f"{gateway_base_url}/v1/chat/stream",
            json={
                "session_id": session_id,
                "doc_id": doc_id,
                "user_message_id": user_message_id,
                "top_k": top_k,
            },
            stream=True,
            timeout=(10, timeout_seconds),
        ) as response:
            response.raise_for_status()

            for raw_line in response.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue
                line = raw_line.strip()
                if not line or not line.startswith("data: "):
                    continue

                if ttft_ms is None:
                    ttft_ms = round((time.perf_counter() - started_at) * 1000, 2)

                event = json.loads(line[6:])
                if event.get("type") == "done":
                    done_meta = event.get("meta") or {}
                    break
                if event.get("type") == "error":
                    return {
                        "mode": "stream",
                        "success": False,
                        "timed_out": False,
                        "session_id": session_id,
                        "message_id": user_message_id,
                        "error": event.get("message") or "stream error",
                    }
    except requests.Timeout:
        return {
            "mode": "stream",
            "success": False,
            "timed_out": True,
            "session_id": session_id,
            "message_id": user_message_id,
            "error": f"stream request timed out after {timeout_seconds}s",
        }

    return {
        "mode": "stream",
        "success": True,
        "session_id": session_id,
        "message_id": user_message_id,
        "ttft_ms": ttft_ms,
        "client_e2e_latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "server_meta": done_meta,
    }


def execute_phase(
    request_count: int,
    concurrency: int,
    runner,
) -> Dict[str, Any]:
    if request_count <= 0:
        return {
            "duration_seconds": 0,
            "results": [],
        }

    started_at = time.perf_counter()
    results: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = [executor.submit(runner, index) for index in range(request_count)]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    return {
        "duration_seconds": round(time.perf_counter() - started_at, 2),
        "results": results,
    }


def build_report(
    ingest_result: Optional[Dict[str, Any]],
    async_phase: Dict[str, Any],
    stream_phase: Dict[str, Any],
    monitor_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    async_results = async_phase.get("results") or []
    stream_results = stream_phase.get("results") or []
    all_results = async_results + stream_results
    success_results = [item for item in all_results if item.get("success")]

    def meta(item: Dict[str, Any]) -> Dict[str, Any]:
        return item.get("server_meta") or {}

    ttft_values = [
        float(meta(item).get("ttft_ms") or item.get("ttft_ms"))
        for item in stream_results
        if item.get("success") and (meta(item).get("ttft_ms") or item.get("ttft_ms")) is not None
    ]
    e2e_values = [
        float(meta(item).get("e2e_latency_ms") or item.get("client_e2e_latency_ms"))
        for item in success_results
        if (meta(item).get("e2e_latency_ms") or item.get("client_e2e_latency_ms")) is not None
    ]
    retrieval_values = [
        float(meta(item).get("retrieval_ms"))
        for item in success_results
        if meta(item).get("retrieval_ms") is not None
    ]
    citation_values = [
        float(meta(item).get("citation_count"))
        for item in success_results
        if meta(item).get("citation_count") is not None
    ]
    prompt_tokens = [
        float(meta(item).get("prompt_tokens"))
        for item in success_results
        if meta(item).get("prompt_tokens") is not None
    ]
    completion_tokens = [
        float(meta(item).get("completion_tokens"))
        for item in success_results
        if meta(item).get("completion_tokens") is not None
    ]
    costs = [
        float(meta(item).get("cost_usd"))
        for item in success_results
        if meta(item).get("cost_usd") is not None
    ]

    timed_out_count = len([item for item in all_results if item.get("timed_out")])
    failed_count = len([item for item in all_results if not item.get("success")])
    no_context_count = len([item for item in success_results if meta(item).get("no_context")])

    total_duration = (async_phase.get("duration_seconds") or 0) + (stream_phase.get("duration_seconds") or 0)
    qps = round(len(success_results) / total_duration, 3) if total_duration > 0 else 0.0

    return {
        "experience": {
            "ttft_ms": latency_summary(ttft_values),
            "e2e_latency_ms": latency_summary(e2e_values),
            "ingest_ready_time_ms": ingest_result.get("ingest_ready_ms") if ingest_result else None,
        },
        "cost": {
            "prompt_tokens_avg": round(statistics.mean(prompt_tokens), 2) if prompt_tokens else None,
            "completion_tokens_avg": round(statistics.mean(completion_tokens), 2) if completion_tokens else None,
            "cost_per_request_usd": round(statistics.mean(costs), 8) if costs else None,
            "cost_per_document_usd": ingest_result.get("task_meta", {}).get("cost_usd") if ingest_result else None,
        },
        "throughput": {
            "qps": qps,
            "concurrent_sessions": monitor_snapshot.get("max_concurrent_sessions"),
            "worker_queue_depth": monitor_snapshot.get("max_worker_queue_depth"),
            "active_sse_connections": monitor_snapshot.get("max_active_sse_connections"),
        },
        "stability_quality": {
            "error_rate": round(failed_count / len(all_results), 4) if all_results else None,
            "timeout_rate": round(timed_out_count / len(all_results), 4) if all_results else None,
            "retrieval_ms": latency_summary(retrieval_values),
            "citation_count_avg": round(statistics.mean(citation_values), 2) if citation_values else None,
            "no_context_ratio": round(no_context_count / len(success_results), 4) if success_results else None,
        },
        "counts": {
            "async_requests": len(async_results),
            "stream_requests": len(stream_results),
            "successful_requests": len(success_results),
            "failed_requests": failed_count,
            "timed_out_requests": timed_out_count,
        },
        "phases": {
            "async_duration_seconds": async_phase.get("duration_seconds"),
            "stream_duration_seconds": stream_phase.get("duration_seconds"),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark ingest / async chat / stream chat metrics.")
    parser.add_argument("--python-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--gateway-base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--file", default="./README.md")
    parser.add_argument("--doc-id", type=int, default=0)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--question", default="这份文档讲了什么？")
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--async-requests", type=int, default=6)
    parser.add_argument("--stream-requests", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--monitor-interval", type=float, default=0.5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if requests is None:
        raise SystemExit(
            "requests is not installed. Activate the project virtualenv or install `python_rag/requirements.txt` first."
        )

    python_base_url = args.python_base_url.rstrip("/")
    gateway_base_url = args.gateway_base_url.rstrip("/")

    ingest_result = None
    doc_id = args.doc_id
    if doc_id <= 0:
        ingest_result = upload_and_ingest_document(
            gateway_base_url=gateway_base_url,
            file_path=args.file,
            user_id=args.user_id,
            timeout_seconds=args.timeout_seconds,
        )
        doc_id = ingest_result["doc_id"]

    sampler = MonitorSampler(
        python_base_url=python_base_url,
        interval_seconds=max(0.1, args.monitor_interval),
    )
    sampler.start()

    try:
        async_phase = execute_phase(
            request_count=args.async_requests,
            concurrency=args.concurrency,
            runner=lambda index: run_async_chat_request(
                gateway_base_url=gateway_base_url,
                user_id=args.user_id,
                doc_id=doc_id,
                question=args.question,
                top_k=args.top_k,
                timeout_seconds=args.timeout_seconds,
                index=index,
            ),
        )
        stream_phase = execute_phase(
            request_count=args.stream_requests,
            concurrency=args.concurrency,
            runner=lambda index: run_stream_chat_request(
                python_base_url=python_base_url,
                gateway_base_url=gateway_base_url,
                user_id=args.user_id,
                doc_id=doc_id,
                question=args.question,
                top_k=args.top_k,
                timeout_seconds=args.timeout_seconds,
                index=index,
            ),
        )
    finally:
        monitor_snapshot = sampler.stop()

    report = build_report(
        ingest_result=ingest_result,
        async_phase=async_phase,
        stream_phase=stream_phase,
        monitor_snapshot=monitor_snapshot,
    )
    report["doc_id"] = doc_id
    report["monitor_snapshot"] = monitor_snapshot

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
