"""Dify 适配器契约测试（需要 pytest + pytest-asyncio 之外的零额外依赖）。

用本地假服务替代真实 Dify：CI 里没有 Docker 也能验证契约。
运行：pytest tests/test_targets_dify.py -q
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rag_eval.metrics import evaluate
from rag_eval.models import Case, PassageRef
from rag_eval.targets import DifyTarget, MappingSpec


class _Handler(BaseHTTPRequestHandler):
    mode = "ok"
    last_payload: dict = {}

    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.mode == "auth":
            self._json(401, {"code": "unauthorized", "message": "Access token is invalid"})
            return
        self._json(200, {"user_input_form": []})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        _Handler.last_payload = json.loads(self.rfile.read(length) or b"{}")
        if self.mode == "auth":
            self._json(401, {"code": "unauthorized", "message": "Access token is invalid"})
            return
        resources = [] if self.mode == "no_citation" else [
            {
                "document_id": "uuid-1",
                "document_name": "policy_return_v3.pdf",
                "segment_id": "seg-001",
                "score": 0.93,
                "content": "七天无理由退货：自签收之日起 7 天内可申请。",
            }
        ]
        self._json(
            200,
            {
                "event": "message",
                "answer": "7 天内可以无理由退货。",
                "conversation_id": "conv-1",
                "metadata": {"retriever_resources": resources},
            },
        )


@pytest.fixture()
def dify() -> DifyTarget:
    _Handler.mode = "ok"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield DifyTarget(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="test-key")
    server.shutdown()


def _case() -> Case:
    return Case(
        id="core_return_window",
        slice="core",
        question="签收后几天内可以无理由退货？",
        expected_answer="自签收之日起 7 天内可以申请无理由退货。",
        expected_passages=[PassageRef("policy_return_v3", "seg-001")],
    )


def test_health_and_answer_mapping(dify: DifyTarget) -> None:
    assert dify.health()
    answer = dify.ask(_case())
    assert answer.error is None
    assert answer.text == "7 天内可以无理由退货。"
    assert answer.contexts[0].doc_id == "policy_return_v3"
    assert answer.contexts[0].chunk_id == "seg-001"
    assert answer.contexts[0].score == 0.93


def test_each_case_uses_isolated_user_and_no_conversation(dify: DifyTarget) -> None:
    dify.ask(_case())
    assert _Handler.last_payload["user"] == "rag-eval:core_return_window"
    assert "conversation_id" not in _Handler.last_payload
    assert _Handler.last_payload["response_mode"] == "blocking"


def test_auth_failure_is_classified(dify: DifyTarget) -> None:
    _Handler.mode = "auth"
    assert not dify.health()
    answer = dify.ask(_case())
    assert answer.error is not None and "鉴权失败" in answer.error


def test_missing_citation_fails_retrieval_metrics(dify: DifyTarget) -> None:
    _Handler.mode = "no_citation"
    answer = dify.ask(_case())
    scores = {s.name: s.value for s in evaluate(_case(), answer, k=3)}
    assert scores["hit@3"] == 0.0


def test_openai_compat_skips_retrieval_metrics() -> None:
    from rag_eval.targets import OpenAICompatTarget

    target = OpenAICompatTarget(base_url="http://127.0.0.1:1/v1", model="x")
    assert target.supports_citations is False
    assert isinstance(MappingSpec().contexts_path, str)
