"""适配层离线验收：用本地假服务模拟 Dify 与 OpenAI 兼容接口。

为什么要写这个脚本：
真实 Dify 要 Docker + 模型 Key，CI 里跑不了。契约测试用假服务，
就能在 0 成本、0 网络的前提下验证"请求发对了、响应解析对了、错误分类对了"。

运行：python tools/verify_adapters.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rag_eval.dataset import load_cases  # noqa: E402
from rag_eval.doctor import diagnose  # noqa: E402
from rag_eval.metrics import evaluate  # noqa: E402
from rag_eval.models import Case, PassageRef  # noqa: E402
from rag_eval.targets import DifyTarget, MappingSpec, OpenAICompatTarget  # noqa: E402

CHECKS: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    if condition:
        CHECKS.append(f"PASS  {label}")
        print(f"PASS  {label}")
    else:
        CHECKS.append(f"FAIL  {label} {detail}")
        print(f"FAIL  {label} {detail}")


class FakeDify(BaseHTTPRequestHandler):
    mode = "ok"
    last_payload: dict = {}

    def log_message(self, *args) -> None:  # 静音
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
        if self.path.endswith("/parameters"):
            self._json(200, {"user_input_form": [], "opening_statement": ""})
        else:
            self._json(404, {"code": "not_found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        FakeDify.last_payload = payload

        if self.mode == "auth":
            self._json(401, {"code": "unauthorized", "message": "Access token is invalid"})
            return
        if not self.path.endswith("/chat-messages"):
            self._json(404, {"code": "not_found"})
            return

        resources = []
        if self.mode != "no_citation":
            resources = [
                {
                    "position": 1,
                    "dataset_id": "ds-1",
                    "document_id": "3f2a-uuid",
                    "document_name": "policy_return_v3.pdf",
                    "segment_id": "seg-001",
                    "score": 0.93,
                    "content": "七天无理由退货：自签收之日起 7 天内，商品未使用且包装完好，可申请无理由退货。",
                },
                {
                    "position": 2,
                    "dataset_id": "ds-1",
                    "document_id": "9b1c-uuid",
                    "document_name": "policy_shipping_v2.pdf",
                    "segment_id": "seg-042",
                    "score": 0.61,
                    "content": "单笔订单满 99 元包邮，未满 99 元收取 10 元运费。",
                },
            ]

        self._json(
            200,
            {
                "event": "message",
                "task_id": "task-1",
                "message_id": "msg-1",
                "conversation_id": "conv-1",
                "answer": "可以。自签收之日起 7 天内，商品未使用且包装完好即可申请无理由退货。",
                "metadata": {"retriever_resources": resources},
                "created_at": 1700000000,
            },
        )


class FakeOpenAI(BaseHTTPRequestHandler):
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
        if self.path.endswith("/models"):
            self._json(200, {"data": [{"id": "qwen2.5:7b-instruct"}]})
        else:
            self._json(404, {})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        json.loads(self.rfile.read(length) or b"{}")
        self._json(
            200,
            {
                "id": "chatcmpl-1",
                "model": "qwen2.5:7b-instruct",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "7 天内可以无理由退货。"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )


def serve(handler) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def case() -> Case:
    return Case(
        id="core_return_window",
        slice="core",
        question="签收后几天内可以无理由退货？",
        expected_answer="自签收之日起 7 天内，商品未使用且包装完好，可以申请无理由退货。",
        expected_passages=[PassageRef("policy_return_v3", "seg-001")],
    )


def main() -> int:
    dify_server = serve(FakeDify)
    openai_server = serve(FakeOpenAI)
    dify_base = f"http://127.0.0.1:{dify_server.server_port}/v1"
    openai_base = f"http://127.0.0.1:{openai_server.server_port}/v1"

    try:
        print("\n[1] Dify 正常路径")
        target = DifyTarget(base_url=dify_base, api_key="app-test-key", name="dify", version="1.x")
        check(target.health(), "健康检查通过")

        answer = target.ask(case())
        check(answer.error is None, "无采集错误", str(answer.error))
        check(len(answer.contexts) == 2, "解析出 2 段引用", str(len(answer.contexts)))
        check(answer.contexts[0].doc_id == "policy_return_v3", "document_name 去后缀映射为 doc_id")
        check(answer.contexts[0].chunk_id == "seg-001", "segment_id 映射为 chunk_id")
        check(answer.contexts[0].score == 0.93, "score 透传")
        check(answer.text.startswith("可以。"), "answer 字段解析正确")

        check(FakeDify.last_payload.get("conversation_id") is None, "未复用 conversation_id（避免上下文串味）")
        check(FakeDify.last_payload.get("user") == "rag-eval:core_return_window", "每条用例独立 user")
        check(FakeDify.last_payload.get("response_mode") == "blocking", "使用非流式模式")

        scores = evaluate(case(), answer, k=3)
        names = {s.name: s.value for s in scores}
        check(names.get("hit@3") == 1.0, "hit@3 计算为 1.0", str(names))
        check(names.get("mrr") == 1.0, "mrr 计算为 1.0")
        check(names.get("faithfulness_proxy", 0) >= 0.5, "faithfulness_proxy 命中引用")

        print("\n[2] Dify 鉴权失败")
        FakeDify.mode = "auth"
        check(not target.health(), "健康检查失败")
        ok, msg = target.probe()
        check(not ok and "鉴权失败" in msg, "错误信息指出鉴权问题", msg)
        bad = target.ask(case())
        check(bad.error is not None and "鉴权失败" in bad.error, "采集失败带归因", str(bad.error))

        print("\n[3] Dify 未开启引用")
        FakeDify.mode = "no_citation"
        check(target.health(), "健康检查仍通过")
        no_cite = target.ask(case())
        check(no_cite.error is None and no_cite.contexts == [], "答案可取但引用为空")
        no_cite_scores = {s.name: s.value for s in evaluate(case(), no_cite, k=3)}
        check(
            no_cite_scores.get("hit@3") == 0.0,
            "声明支持引用却没召回时记 0 分（真实失败，不静默跳过）",
            str(no_cite_scores),
        )

        report = diagnose(target, [case()], limit=1)
        check(any("引用片段" in w for w in report.warnings), "doctor 报出缺引用", str(report.warnings))

        print("\n[4] OpenAI 兼容接口")
        openai_target = OpenAICompatTarget(base_url=openai_base, model="qwen2.5:7b-instruct")
        check(openai_target.health(), "健康检查通过")
        oa = openai_target.ask(case())
        check(oa.error is None and oa.text == "7 天内可以无理由退货。", "choices 路径解析正确", oa.error or "")
        check(oa.contexts == [], "无引用时 contexts 为空")

        print("\n[5] doctor 映射诊断")
        FakeDify.mode = "ok"
        good = diagnose(target, [case()], limit=1)
        check(good.ready, "正常接入判定为可开跑", str(good.warnings))
        check(good.doc_id_coverage == 1.0, "doc_id 覆盖率 100%", f"{good.doc_id_coverage:.2f}")

        wrong = DifyTarget(
            base_url=dify_base,
            api_key="k",
            mapping=MappingSpec(doc_id_field="document_id"),
        )
        wrong_report = diagnose(wrong, [case()], limit=1)
        check(any("不匹配" in w for w in wrong_report.warnings), "doc_id 映射错时给出警告", str(wrong_report.warnings))
        check(wrong_report.doc_id_coverage == 0.0, "错误映射下覆盖率为 0")

        print("\n[6] 评测集真实加载")
        cases = load_cases([ROOT / "datasets/golden/qa_core.jsonl"])
        real = diagnose(target, cases, limit=2)
        check(len(real.probes) == 2, "doctor 批量探测 2 条")
    finally:
        dify_server.shutdown()
        openai_server.shutdown()

    failed = [c for c in CHECKS if c.startswith("FAIL")]
    print("\n" + "-" * 56)
    print(f"共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)} 项，失败 {len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
