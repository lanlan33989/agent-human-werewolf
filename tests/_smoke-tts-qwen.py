# -*- coding: utf-8 -*-
"""
百炼 Qwen-TTS 适配器（server.QwenTts）+ 人格→音色映射的**离线**冒烟测试。

和 `_smoke-asr-qwen.py` 同一个套路：不联网，验适配器自己（请求形状、装配、错误路径）。
真·音质和音色差异只能人耳听，这里不假装能测。

为什么这几条必须钉住（都是实测踩过的）：
  · 文本超长：**不许静默截断**（截了就是"少一句话"，桌上没人查得出来）
  · 云端返回的 WAV **头里的长度字段可能是错的**（实测一次算出 44739 秒）⇒ 必须重写头
  · 400（配置错）不重试；429（限流）重试；超时**绝不**重试

跑法：python tests/_smoke-tts-qwen.py
退出码：0 = 全过；1 = 有失败
"""
from __future__ import annotations

import io
import json
import os
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/
os.environ["MOCK"] = "1"

import httpx  # noqa: E402

import agents  # noqa: E402
import server  # noqa: E402

PASS: list = []
FAIL: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if detail and not ok else ""))


def _wav(n: int = 800, rate: int = 24000, bogus_size: bool = False) -> bytes:
    """造一段 WAV。bogus_size=True 时把头里的长度字段写成**错的**（模拟云端那次）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<" + "h" * n, *([1000] * n)))
    data = bytearray(buf.getvalue())
    if bogus_size:
        data[4:8] = struct.pack("<I", 999999999)          # RIFF size 写坏
        data[40:44] = struct.pack("<I", 2000000000)       # data size 写坏
    return bytes(data)


class _FakeResp:
    def __init__(self, status: int = 200, payload=None, text: str = "", content: bytes = b""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=self.request, response=self)

    @property
    def request(self):
        return httpx.Request("POST", "http://fake/")


class _FakeClient:
    calls: list = []
    script: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002
        _FakeClient.calls.append({"m": "post", "url": url, "json": json or {}})
        return _next()

    def get(self, url, **kw):
        _FakeClient.calls.append({"m": "get", "url": url})
        return _next()


def _next():
    item = (_FakeClient.script.pop(0) if _FakeClient.script
            else _FakeResp(200, {"output": {"audio": {"url": "http://oss/x.wav"}}},
                           content=_wav()))
    if isinstance(item, Exception):
        raise item
    return item


def setup(script=None) -> None:
    _FakeClient.calls = []
    _FakeClient.script = list(script or [])
    server.httpx.Client = _FakeClient            # type: ignore[assignment]
    server.time.sleep = lambda *_: None


def ok_tts(**kw):
    """一个"请求成功 + 下载成功"的剧本。"""
    return [_FakeResp(200, {"output": {"audio": {"url": "http://oss/x.wav"}}}),
            _FakeResp(200, content=_wav())] + list(kw.get("extra", []))


print("\n=== 百炼 Qwen-TTS 适配器冒烟测试（离线，不联网）===\n")

# ① 装配：TTS=qwen 要选到它
_old = os.environ.get("TTS")
os.environ["TTS"] = "qwen"
try:
    check("★ TTS=qwen 时装配到 QwenTts（多音色那条路真的接上了）",
          isinstance(server._real_tts(), server.QwenTts), type(server._real_tts()).__name__)
finally:
    if _old is None:
        os.environ.pop("TTS", None)
    else:
        os.environ["TTS"] = _old

# ② 缺 key：说清缺什么，且不发请求
_keep = os.environ.pop("DASHSCOPE_TTS_API_KEY", None)
_keep2 = os.environ.pop("DASHSCOPE_API_KEY", None)
setup()
_err = ""
try:
    server.QwenTts().synth("测试")
except RuntimeError as e:
    _err = str(e)
finally:
    if _keep is not None:
        os.environ["DASHSCOPE_TTS_API_KEY"] = _keep
    if _keep2 is not None:
        os.environ["DASHSCOPE_API_KEY"] = _keep2
check("★ 缺 key 时报错点名 DASHSCOPE_TTS_API_KEY / DASHSCOPE_API_KEY",
      "DASHSCOPE" in _err, _err[:80] or "（没抛异常）")
check("……而且不发请求", not _FakeClient.calls, f"{len(_FakeClient.calls)} 次")

# ③ 文本超长：**报错**，不静默截断
setup()
_err = ""
try:
    server.QwenTts(api_key="k").synth("字" * (server.QwenTts.MAX_CHARS + 1))
except RuntimeError as e:
    _err = str(e)
check("★ 超长文本直接报错（静默截断 = 少一句话，桌上没人查得出来）",
      "太长" in _err and not _FakeClient.calls, _err[:80] or "（没抛异常）")

# ④ 请求形状 + 下载
setup(ok_tts())
_out = server.QwenTts(api_key="k", voice="Moon").synth("我是三号。")
_req = _FakeClient.calls[0]["json"]
check("★ 打的是 DashScope 原生 multimodal-generation（TTS 没有 OpenAI 兼容形态）",
      "/multimodal-generation/generation" in _FakeClient.calls[0]["url"],
      _FakeClient.calls[0]["url"])
check("★ 文本/音色/语种都在 input 里（不是瞎猜的字段名）",
      _req.get("input", {}).get("text") == "我是三号。"
      and _req["input"].get("voice") == "Moon"
      and _req["input"].get("language_type") == "Chinese", str(_req)[:140])
check("★ 返回的是 **URL**，所以要再下载一次（不下载就只有一串链接）",
      len([c for c in _FakeClient.calls if c["m"] == "get"]) == 1
      and _out.startswith(b"RIFF"), f"{len(_FakeClient.calls)} 次调用")

# ⑤ 坏掉的 WAV 头必须被重写（实测踩过：某次头里的长度算出来 44739 秒）
setup([_FakeResp(200, {"output": {"audio": {"url": "http://oss/bad.wav"}}}),
       _FakeResp(200, content=_wav(800, bogus_size=True))])
_fixed = server.QwenTts(api_key="k").synth("测试")
with wave.open(io.BytesIO(_fixed), "rb") as w:
    _nf = w.getnframes()
check("★ 下载到的 WAV 头被重写（坏头会让播放器/波形按错长度算，实测出现过）",
      _nf == 800, f"重写后帧数={_nf}（应为 800）")

# ⑥ 没给音频地址 ⇒ 明确报错
setup([_FakeResp(200, {"output": {}})])
_err = ""
try:
    server.QwenTts(api_key="k").synth("测试")
except RuntimeError as e:
    _err = str(e)
check("★ 响应里没有 audio.url 时明确报错（不静默返回空音频）",
      "没给音频地址" in _err, _err[:90] or "（没抛异常）")

# ⑦ 400 不重试；429 重试
setup([_FakeResp(400, {}, text="InvalidParameter: url error")])
_err = ""
try:
    server.QwenTts(api_key="k").synth("测试")
except RuntimeError as e:
    _err = str(e)
check("★ 400 不重试且带服务端原文（配置错重试只是白等 + 白花钱）",
      len(_FakeClient.calls) == 1 and "InvalidParameter" in _err,
      f"{len(_FakeClient.calls)} 次 / {_err[:70]}")

setup([_FakeResp(429, {}), _FakeResp(429, {})])
_err = ""
try:
    server.QwenTts(api_key="k").synth("测试")
except RuntimeError as e:
    _err = str(e)
check("★ 429 会重试到上限（限流不该直接判死）",
      len(_FakeClient.calls) == 2 and "调用失败" in _err,
      f"{len(_FakeClient.calls)} 次 / {_err[:70]}")

# ⑧ instructions 只对 instruct 系列生效（配了别的模型不该悄悄丢掉）
setup(ok_tts())
server.QwenTts(api_key="k", model="qwen3-tts-flash", instructions="语速慢一点").synth("测试")
_no_i = "instructions" not in _FakeClient.calls[0]["json"]["input"]
setup(ok_tts())
server.QwenTts(api_key="k", model="qwen3-tts-instruct-flash",
               instructions="语速慢一点").synth("测试")
_has_i = _FakeClient.calls[0]["json"]["input"].get("instructions") == "语速慢一点"
check("★ instructions 只给 instruct 系列（不给 flash，免得「配了但没生效」看不出来）",
      _no_i and _has_i, f"flash里没有={_no_i} instruct里有={_has_i}")

# ⑨ 人格 → 音色映射：每个人格都要有，而且**互不相同**（否则等于没分）
_voices = {k: agents.voice_for_personality(k) for k in agents.PERSONALITY_KEYS}
check("★ 9 种人格都映射到了音色", len(_voices) == len(agents.PERSONALITY_KEYS)
      and all(_voices.values()), str(_voices))
check("★ 音色互不相同（相同就等于「换人格不换声音」）",
      len(set(_voices.values())) == len(_voices), str(sorted(_voices.values())))
check("★ 法官音色与所有人格音色都不同（否则听不出谁在主持）",
      agents.JUDGE_VOICE not in _voices.values(), agents.JUDGE_VOICE)
check("认不出的人格给兜底音色，不抛（音色是装饰，不能弄崩一局）",
      agents.voice_for_personality(None) == agents.DEFAULT_VOICE
      and agents.voice_for_personality("不存在的人格") == agents.DEFAULT_VOICE,
      agents.voice_for_personality("不存在的人格"))

print("\n" + "=" * 74)
print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("\n说明：音质与音色差异只能人耳听，本文件不假装能测。")
sys.exit(1 if FAIL else 0)
