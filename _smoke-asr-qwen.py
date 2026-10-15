# -*- coding: utf-8 -*-
"""
百炼 Qwen-ASR 适配器（server.QwenAsr）的**离线**冒烟测试。

和 `_smoke-asr-local.py` 的分工：
    _smoke-asr-local.py  真调云端，验**识别质量**（4 段夹具 + 关键词命中）。需要 key。
    本文件                不联网，验**适配器自己**：请求形状、装配、错误路径。随时可跑。

为什么值得单独一个文件：这个适配器的正确性有一半在"请求长什么样"上 ——
    · `data:` URL 前缀写错、model 名写错、音频字段名写错 ⇒ 服务端只会回一句笼统的 400
    · 多塞一段文本 ⇒ 这个服务直接 400（**实测踩过**，见下面那条断言）
    · 400/401/404 是配置错，重试没意义；429/5xx 才该退避重试；超时**绝不重试**
      这四条判断没有任何一条能靠"真调一次"测出来（真调只能覆盖成功那条路）。

跑法：python _smoke-asr-qwen.py
退出码：0 = 全过；1 = 有失败
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ["MOCK"] = "1"          # 别让 import server 时去连真服务
os.environ["MOCK_ASR"] = "1"

import httpx  # noqa: E402

import server  # noqa: E402

PASS: list = []
FAIL: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if detail and not ok else ""))


# ---------------------------------------------------------------- 假 HTTP 层
class _FakeResp:
    def __init__(self, status: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload

    @property
    def request(self):
        return httpx.Request("POST", "http://fake/")


class _FakeClient:
    """按剧本返回响应；记录每一次请求，供断言检查。"

    `script` 里的元素要么是 _FakeResp，要么是要抛的异常。
    """

    calls: list = []
    script: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002
        _FakeClient.calls.append({"url": url, "headers": headers or {}, "json": json or {}})
        item = (_FakeClient.script.pop(0) if _FakeClient.script
                else _FakeResp(200, {"choices": [{"message": {"content": "默认文本"}}]}))
        if isinstance(item, Exception):
            raise item
        return item


def setup(script=None) -> None:
    _FakeClient.calls = []
    _FakeClient.script = list(script or [])
    server.httpx.Client = _FakeClient          # type: ignore[assignment]
    server.time.sleep = lambda *_: None        # 别让退避真的睡（0.6s 也别睡）


def asr(**kw):
    """造一个适配器，key 必填（不然连请求都发不出去）。"""
    kw.setdefault("api_key", "sk-test-fake")
    kw.setdefault("base_url", "https://example.invalid/compatible-mode/v1")
    kw.setdefault("model", "qwen3-asr-flash")
    return server.QwenAsr(**kw)


print("\n=== 百炼 Qwen-ASR 适配器冒烟测试（离线，不联网）===\n")

# ① 装配：ASR=qwen 时要选到它，而且**不联网也不报错**
_old_asr = os.environ.get("ASR")
os.environ["ASR"] = "qwen"
try:
    _impl = server._real_asr()
    check("★ ASR=qwen 时装配到 QwenAsr（云端那条路真的接上了）",
          isinstance(_impl, server.QwenAsr), type(_impl).__name__)
finally:
    if _old_asr is None:
        os.environ.pop("ASR", None)
    else:
        os.environ["ASR"] = _old_asr

# ② 没配 key：报错必须**说清缺什么**（而不是笼统的"调用失败"）
#
# ⚠️ 必须**真的把环境变量摘掉**，不能只传 api_key=""：这个类和 OpenAICompatLlm
#    一样是"参数 → 环境变量"的 or 兜底，空串会被环境变量接住（本机 .env 里就有 key）
#    ⇒ 传空串根本走不到那条分支。第一版就是这么写的，断言假绿。
_keep_key = os.environ.pop("DASHSCOPE_API_KEY", None)
_keep_key2 = os.environ.pop("ASR_API_KEY", None)
setup()
_error = ""
try:
    server.QwenAsr(base_url="https://x.invalid", model="m").transcribe(
        b"x" * 10, "audio/wav", 16000)
except RuntimeError as e:
    _error = str(e)
finally:
    if _keep_key is not None:
        os.environ["DASHSCOPE_API_KEY"] = _keep_key
    if _keep_key2 is not None:
        os.environ["ASR_API_KEY"] = _keep_key2
check("★ 缺 key 时报错点名 DASHSCOPE_API_KEY（排查要一句话到位）",
      "DASHSCOPE_API_KEY" in _error, _error[:80] or "（没抛异常）")
check("……而且不发请求（没 key 就别试了）", not _FakeClient.calls,
      f"发了 {len(_FakeClient.calls)} 次")

# ③ 空音频：直接返回空串，**不打网络**（省一次调用也省一次计费）
setup()
_ok = asr().transcribe(b"", "audio/wav", 16000) == ""
check("★ 空音频不发请求，直接返回空串（别为空白音频付费）",
      _ok and not _FakeClient.calls, f"发出 {len(_FakeClient.calls)} 次")

# ④ 请求形状：这一条是本文件存在的主要理由
setup(script=[_FakeResp(200, {"choices": [{"message": {"content": " 我怀疑三号 "}}]})])
_text = asr().transcribe(b"RIFFfakewav", "audio/wav", 16000)
_req = _FakeClient.calls[0]
_body = _req["json"]
_content = (_body.get("messages") or [{}])[0].get("content") or [{}]
check("★ 打的是 {base}/chat/completions（兼容协议那条路，不是原生协议）",
      _req["url"].endswith("/compatible-mode/v1/chat/completions"), _req["url"])
check("★ 鉴权头是 Bearer（key 从环境变量来，不写死在代码里）",
      _req["headers"].get("Authorization", "").startswith("Bearer "),
      str(_req["headers"].get("Authorization"))[:20])
check("★ 音频按 input_audio.data 传，且是 data: URL（服务端只认这两种之一）",
      _content[0].get("type") == "input_audio"
      and str(_content[0].get("input_audio", {}).get("data", "")).startswith(
          "data:audio/wav;base64,"),
      str(_content[0])[:90])
check("★ 请求里**一个字的文本都没有**（system 有内容会 400："
      "The dedicated task `asr` ... does not support this input —— 实测踩过）",
      all(m.get("role") == "user" for m in _body["messages"])
      and all(isinstance(c, dict) and c.get("type") == "input_audio"
              for m in _body["messages"] for c in m["content"]),
      str(_body["messages"])[:120])
check("★ 语种与数字规范化在 asr_options 里（不是瞎猜的字段名）",
      _body.get("asr_options", {}).get("language") == "zh"
      and _body["asr_options"].get("enable_itn") is True,
      str(_body.get("asr_options")))
check("模型名取自配置（换模型只改 .env，不动代码）",
      _body.get("model") == "qwen3-asr-flash", str(_body.get("model")))
check("返回文本做了 strip（服务端会给首尾空白）", _text == "我怀疑三号", repr(_text))

# ⑤ 400 是配置错：**不重试**，而且把服务端原文带出来
setup(script=[_FakeResp(400, {}, text='{"error":{"message":"UNSUPPORTED_FORMAT"}}')])
_error = ""
try:
    asr(retries=3).transcribe(b"wav", "audio/wav", 16000)
except RuntimeError as e:
    _error = str(e)
check("★ 400 不重试（配置错重试只是白等 + 白花钱）",
      len(_FakeClient.calls) == 1, f"发了 {len(_FakeClient.calls)} 次")
check("★ 400 的报错里带服务端原文（否则排查只能看到「调用失败」四个字）",
      "UNSUPPORTED_FORMAT" in _error, _error[:90])

# ⑥ 429 是限流：要重试到上限
setup(script=[_FakeResp(429, {}), _FakeResp(429, {}), _FakeResp(429, {})])
_error = ""
try:
    asr(retries=3).transcribe(b"wav", "audio/wav", 16000)
except RuntimeError as e:
    _error = str(e)
check("★ 429 会退避重试到上限（限流不该直接判死）",
      len(_FakeClient.calls) == 3 and "已重试 3 次" in _error,
      f"{len(_FakeClient.calls)} 次 / {_error[:60]}")

# ⑦ 超时：**绝不重试**（重试只会让桌上多等一轮）
setup(script=[httpx.TimeoutException("读超时"), httpx.TimeoutException("读超时")])
_error = ""
try:
    asr(retries=3).transcribe(b"wav", "audio/wav", 16000)
except RuntimeError as e:
    _error = str(e)
check("★ 超时不重试（代价是嘴上多等一轮，比重复烧一遍预算好）",
      len(_FakeClient.calls) == 1 and "超时" in _error,
      f"{len(_FakeClient.calls)} 次 / {_error[:60]}")

# ⑧ 重试时不能把上一次的失败状态带进去（第二次成功就要返回成功）
setup(script=[_FakeResp(503, {}),
              _FakeResp(200, {"choices": [{"message": {"content": "第二次成功"}}]})])
check("★ 503 之后重试成功 ⇒ 正常返回（限流/抖动不该让这一句丢掉）",
      asr(retries=3).transcribe(b"wav", "audio/wav", 16000) == "第二次成功",
      f"发了 {len(_FakeClient.calls)} 次")

print("\n" + "=" * 74)
print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("\n说明：本文件**不验识别质量**（那要真调云端，见 _smoke-asr-local.py）。")
sys.exit(1 if FAIL else 0)
