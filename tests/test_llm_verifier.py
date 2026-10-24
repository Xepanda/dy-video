#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_llm_verifier.py - B15 LLM 审核落地测试
==========================================
用本地 mock OpenAI 兼容服务器验证 LLM 分支全链路：
  1. 正常纠错路径（corrections 应用 + 时间戳绝对守恒）
  2. 零价格护栏（纠错引入价格 → 拦截，决策 3）
  3. 长度比护栏（疑似整句幻觉 → 拦截）
  4. index 越界（拦截）
  5. 无 API Key → 自动降级确定性引擎
  6. LLM 服务不可达 → 自动降级
  7. LLM 输出非法 JSON → 自动降级

运行: python -u tests/test_llm_verifier.py
"""
import os
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.core.asr_agent_verifier import AsrContextVerifier

SPEC = {
    "product": {"title_line1": "半岛棉镂空钩花", "title_line2": "气质显瘦针织衫"},
    "brand": {"title_en": "MIXCITY"},
    "options": {"highlight_keywords": ["半岛棉", "镂空", "显瘦", "针织"]},
}

SEGMENTS = [
    {"start": 0.0, "end": 2.5, "duration": 2.5, "text": "今天我们来看这个半导棉的开衫"},
    {"start": 2.5, "end": 5.0, "duration": 2.5, "text": "穿上去超选瘦的"},
    {"start": 5.0, "end": 7.5, "duration": 2.5, "text": "这个价格只要299块钱"},
]


def _mock_server(response_body: str, status: int = 200):
    """启动一次性 mock OpenAI 兼容服务器，返回 (base_url, shutdown_fn)。"""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps(response_body).encode("utf-8") if isinstance(response_body, dict) \
                else response_body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{srv.server_port}/v1", srv.shutdown


def _completion(content: str):
    return {"choices": [{"message": {"content": content}}]}


def _set_env(base_url=None, key="test-key"):
    os.environ["LLM_BASE_URL"] = base_url or "http://127.0.0.1:1/v1"
    if key:
        os.environ["LLM_API_KEY"] = key
    else:
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("GEMINI_API_KEY", None)
    os.environ["LLM_TIMEOUT"] = "10"


def case_llm_correction():
    """用例 1：LLM 正常纠错 + 时间戳守恒"""
    base_url, shutdown = _mock_server(_completion(json.dumps({
        "corrections": [
            {"index": 0, "original": "今天我们来看这个半导棉的开衫",
             "corrected": "今天我们来看这个半岛棉的开衫", "reason": "面料名纠错"},
            {"index": 1, "original": "穿上去超选瘦的", "corrected": "穿上去超显瘦的", "reason": "卖点词纠错"},
        ]
    }, ensure_ascii=False)))
    _set_env(base_url)
    try:
        v = AsrContextVerifier(SPEC)
        segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    finally:
        shutdown()
    assert report["engine_used"].startswith("LLM"), f"引擎应为 LLM: {report['engine_used']}"
    assert segs[0]["text"] == "今天我们来看这个半岛棉的开衫", segs[0]["text"]
    assert segs[1]["text"] == "穿上去超显瘦的", segs[1]["text"]
    # 时间戳绝对守恒
    for orig, new in zip(SEGMENTS, segs):
        assert orig["start"] == new["start"] and orig["end"] == new["end"], "时间戳被篡改!"
    # 价格句块未被触碰
    assert "299" in segs[2]["text"]
    print("[PASS] 用例1 LLM 正常纠错 + 时间戳守恒")


def case_price_guardrail():
    """用例 2：纠错引入价格 → 护栏拦截（决策 3）"""
    base_url, shutdown = _mock_server(_completion(json.dumps({
        "corrections": [
            {"index": 2, "original": "这个价格只要299块钱", "corrected": "这个到手价299", "reason": "顺手补全"},
        ]
    }, ensure_ascii=False)))
    _set_env(base_url)
    try:
        v = AsrContextVerifier(SPEC)
        segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    finally:
        shutdown()
    assert segs[2]["text"] == "这个价格只要299块钱", "价格护栏未生效"
    assert len(report["llm_rejected_corrections"]) == 1
    assert "价格" in report["llm_rejected_corrections"][0]["reason"]
    print("[PASS] 用例2 零价格护栏拦截")


def case_length_guardrail():
    """用例 3：长度比超界 → 疑似整句幻觉拦截"""
    base_url, shutdown = _mock_server(_completion(json.dumps({
        "corrections": [
            {"index": 0, "original": "今天我们来看这个半导棉的开衫",
             "corrected": "大家好欢迎来到我们的直播间今天给大家带来一款非常显瘦的针织开衫欢迎大家选购", "reason": "扩写"},
        ]
    }, ensure_ascii=False)))
    _set_env(base_url)
    try:
        v = AsrContextVerifier(SPEC)
        segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    finally:
        shutdown()
    assert segs[0]["text"] == "今天我们来看这个半导棉的开衫", "长度护栏未生效"
    print("[PASS] 用例3 长度比护栏拦截")


def case_index_out_of_range():
    """用例 4：index 越界 → 拦截"""
    base_url, shutdown = _mock_server(_completion(json.dumps({
        "corrections": [
            {"index": 99, "original": "x", "corrected": "半岛棉", "reason": "越界"},
        ]
    }, ensure_ascii=False)))
    _set_env(base_url)
    try:
        v = AsrContextVerifier(SPEC)
        segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    finally:
        shutdown()
    assert all(s["text"] == o["text"] for s, o in zip(segs, SEGMENTS)), "越界纠错被应用"
    print("[PASS] 用例4 index 越界拦截")


def case_no_api_key():
    """用例 5：无 API Key → 自动降级确定性引擎"""
    _set_env(key=None)
    v = AsrContextVerifier(SPEC)
    segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    assert "Deterministic" in report["engine_used"], report["engine_used"]
    assert segs[0]["text"] == "今天我们来看这个半岛棉的开衫", "确定性引擎应纠错半导棉"
    print("[PASS] 用例5 无 Key 自动降级（确定性引擎正常工作）")


def case_service_unreachable():
    """用例 6：LLM 服务不可达 → 自动降级"""
    _set_env(base_url="http://127.0.0.1:1/v1")  # 保留 key，指向不可达端口
    v = AsrContextVerifier(SPEC)
    segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    assert "Deterministic" in report["engine_used"], report["engine_used"]
    assert "降级原因" in report["engine_used"]
    print("[PASS] 用例6 服务不可达自动降级")


def case_invalid_json():
    """用例 7：LLM 输出非法 JSON → 自动降级"""
    base_url, shutdown = _mock_server(_completion("这不是JSON，是一段废话"))
    _set_env(base_url)
    try:
        v = AsrContextVerifier(SPEC)
        segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    finally:
        shutdown()
    assert "Deterministic" in report["engine_used"], report["engine_used"]
    print("[PASS] 用例7 非法 JSON 自动降级")


def case_markdown_fenced_json():
    """用例 8（附加）：markdown 围栏包裹的 JSON 也能正确解析"""
    fenced = "```json\n" + json.dumps({
        "corrections": [
            {"index": 0, "original": "今天我们来看这个半导棉的开衫",
             "corrected": "今天我们来看这个半岛棉的开衫", "reason": "面料名纠错"},
        ]
    }, ensure_ascii=False) + "\n```"
    base_url, shutdown = _mock_server(_completion(fenced))
    _set_env(base_url)
    try:
        v = AsrContextVerifier(SPEC)
        segs, report = v.verify_and_correct([dict(s) for s in SEGMENTS])
    finally:
        shutdown()
    assert segs[0]["text"] == "今天我们来看这个半岛棉的开衫", "围栏 JSON 解析失败"
    print("[PASS] 用例8 markdown 围栏 JSON 解析")


def main():
    cases = [
        case_llm_correction, case_price_guardrail, case_length_guardrail,
        case_index_out_of_range, case_no_api_key, case_service_unreachable,
        case_invalid_json, case_markdown_fenced_json,
    ]
    failures = []
    for fn in cases:
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"[FAIL] {fn.__name__}: {e}")
        except Exception as e:
            failures.append(f"{fn.__name__}: {type(e).__name__}: {e}")
            print(f"[ERROR] {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n[TEST-B15] {'全部通过' if not failures else '失败: ' + str(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
