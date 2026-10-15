# -*- coding: utf-8 -*-
"""
验证 P0-talk 服务端（mock 模式）：/ 与 /api/health 与 /api/utter 是否正常。

用法（在 h5 目录下）：
    ./.venv/Scripts/python.exe _smoke-talk.py
"""
import base64
import io
import math
import os
import struct
import sys
import wave

# 接口回归必须在确定的 mock 环境下跑。
# 否则 .env 里把某一层切成真实实现后，这个测试就会被环境污染（之前就发生过）。
os.environ["MOCK"] = "1"
os.environ["MOCK_ASR"] = "1"
os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_TTS"] = "1"

sys.path.insert(0, ".")

import server  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))


def make_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


# ---- health ----
r = client.get("/api/health")
j = r.json() if r.status_code == 200 else {}
check("health 返回 200", r.status_code == 200, str(r.status_code))
check("mock 模式启用 MockAsr", j.get("asr") == "MockAsr", str(j))
check("mock 模式启用 MockLlm", j.get("llm") == "MockLlm", str(j))
check("mock 模式启用 MockTts", j.get("tts") == "MockTts", str(j))

# ---- 首页 ----
r = client.get("/")
check("首页返回 HTML", r.status_code == 200 and "P0-talk" in r.text, str(r.status_code))

# ---- 正常一次说话 ----
wav = make_wav()
r = client.post(
    "/api/utter",
    content=wav,
    headers={"Content-Type": "audio/wav", "X-Sample-Rate": "16000"},
)
check("utter 返回 200", r.status_code == 200, str(r.status_code) + " " + r.text[:120])

j = r.json() if r.status_code == 200 else {}
check("返回转写文本", bool(j.get("text")), str(j.get("text"))[:40])
check("返回 AI 回复", bool(j.get("reply")), str(j.get("reply"))[:40])
check("返回音频 base64", bool(j.get("audio_b64")))
check("记录上行字节数", isinstance(j.get("in_bytes"), int) and j["in_bytes"] > 0,
      str(j.get("in_bytes")))

tm = j.get("timings", {})
check("返回四段耗时", all(k in tm for k in ("asr_ms", "llm_ms", "tts_ms", "total_ms")), str(tm))
check("总耗时大于 0", tm.get("total_ms", 0) > 0, str(tm.get("total_ms")))

# ---- only=asr：对局界面只要转写，不该顺带跑 LLM / TTS ----
r2 = client.post(
    "/api/utter?only=asr",
    content=wav,
    headers={"Content-Type": "audio/wav", "X-Sample-Rate": "16000"},
)
check("utter?only=asr 返回 200", r2.status_code == 200,
      str(r2.status_code) + " " + r2.text[:120])
j2 = r2.json() if r2.status_code == 200 else {}
check("only=asr 仍然给出转写文本", bool(j2.get("text")), str(j2.get("text"))[:40])
check("only=asr 不跑 LLM / TTS（reply 与音频都为空）",
      j2.get("reply") == "" and j2.get("audio_b64") == "",
      "reply=" + repr(j2.get("reply")) + " audio_len=" + str(len(j2.get("audio_b64") or "")))
tm2 = j2.get("timings", {})
check("only=asr 的 llm_ms / tts_ms 都是 0",
      tm2.get("llm_ms") == 0 and tm2.get("tts_ms") == 0, str(tm2))

# ---- 返回音频是合法 WAV ----
ok_wav = False
detail = ""
try:
    raw = base64.b64decode(j["audio_b64"])
    with wave.open(io.BytesIO(raw), "rb") as w:
        ok_wav = w.getnchannels() == 1 and w.getframerate() > 8000
        detail = f"{w.getnchannels()}ch {w.getframerate()}Hz {w.getnframes()}frames"
except Exception as e:  # noqa: BLE001
    detail = f"{type(e).__name__}: {e}"
check("返回音频是合法单声道 WAV", ok_wav, detail)

# ---- 空音频不应抛异常 ----
r = client.post("/api/utter", content=b"", headers={"Content-Type": "audio/wav"})
check("空音频被优雅处理（非 500 崩溃栈）", r.status_code in (200, 400, 422),
      str(r.status_code))

# ---- 真实模式下的错误路径应给出可读信息 ----
orig_mock = server.MOCK
server.MOCK = False
try:
    bad_asr = server.DoubaoAsr()
    try:
        bad_asr.transcribe(b"x", "audio/wav", 16000)
        raised = False
        msg = ""
    except NotImplementedError as e:
        raised = True
        msg = str(e)
    except Exception as e:  # noqa: BLE001
        raised = True
        msg = f"{type(e).__name__}: {e}"
    check("火山 ASR 未就绪时抛出可读错误（先报配置缺失）",
          raised and ("未配置" in msg or "未接" in msg), msg[:60])
finally:
    server.MOCK = orig_mock

# ---- AI 玩家接模型的装配 ----
# 光在 .env 里填 LLM_MODEL 是接不上对局的：game_api 默认用规则发言器。
# 所以 server 必须显式注入工厂 —— 这两条断言守着那一步。
import game_api as _gapi  # noqa: E402

check("server 装配时注入了 AI Agent 工厂（只填 .env 是不够的）",
      _gapi.AI_AGENT_FACTORY is not None)
check("mock 环境下工厂给的是规则发言器（不会误接成真模型）",
      type(_gapi.AI_AGENT_FACTORY(0, None)).__name__ == "RuleAgent",
      type(_gapi.AI_AGENT_FACTORY(0, None)).__name__)

# ---- 重试策略：429 要重试，超时不要 ----
# 这两条守着一个很实际的取舍：429 是「请求被挡回来了」，退了再发很便宜；
# 超时是「已经等满了一整段预算」，再重试只会让桌上多等一轮。
import httpx as _hx  # noqa: E402

_calls = {"post": 0, "sleep": 0}
_saved_client = server.httpx.Client
_saved_sleep = server.time.sleep


class _FakeResp:
    status_code = 429
    request = None

    def raise_for_status(self):
        pass


class _FakeClientJit:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        _calls["post"] += 1
        return _FakeResp()


class _TimeoutClient:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        _calls["post"] += 1
        raise _hx.TimeoutException("simulated timeout")


def _fake_sleep(_s):
    _calls["sleep"] += 1


_lm = server.OpenAICompatLlm()
_lm.base_url, _lm.api_key, _lm.model = "http://example.invalid", "k", "m"

server.time.sleep = _fake_sleep
try:
    server.httpx.Client = lambda *a, **k: _FakeClientJit()
    _calls.update(post=0, sleep=0)
    try:
        _lm.chat_messages([{"role": "user", "content": "hi"}], timeout=1)
        _ok = False
    except RuntimeError:
        _ok = True
    check("429 会退避重试满 3 次才放弃", _ok and _calls["post"] == 3 and _calls["sleep"] == 2,
          str(_calls))

    server.httpx.Client = lambda *a, **k: _TimeoutClient()
    _calls.update(post=0, sleep=0)
    try:
        _lm.chat_messages([{"role": "user", "content": "hi"}], timeout=1)
        _ok2 = False
    except RuntimeError:
        _ok2 = True
    check("超时不重试（预算已烧光，再试只会多等一轮）",
          _ok2 and _calls["post"] == 1 and _calls["sleep"] == 0, str(_calls))
finally:
    server.httpx.Client = _saved_client
    server.time.sleep = _saved_sleep

# ---- 输出 ----
print("\n=== P0-talk smoke test (server, mock mode) ===\n")
pass_n = fail_n = 0
for name, ok, detail in results:
    print(("PASS" if ok else "FAIL") + "  " + name.ljust(30) + ("  " + detail if detail else ""))
    if ok:
        pass_n += 1
    else:
        fail_n += 1
print("\n" + ("ALL GREEN (" + str(pass_n) + " passed)" if fail_n == 0
              else str(fail_n) + " FAILED / " + str(pass_n) + " passed"))
sys.exit(1 if fail_n else 0)
