# -*- coding: utf-8 -*-
"""TTS 层实现（Mock / 通义 Qwen 流式 / 本地 SAPI / 火山 HTTP）。

2026-10-06 P4a：自 server.py 机械搬移（原样保留，不改任何行为）——
server.py 只留装配（build_pipeline/lifespan）与传输（路由/WS）。
"""
import io
import math
import os
import struct
import tempfile
import threading
import time
import wave
from pathlib import Path
import httpx


class Tts:
    def synth(self, text: str, **kw) -> bytes:
        raise NotImplementedError


def _tone_wav(seconds: float = 0.8, freq: float = 440.0, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        n = int(rate * seconds)
        for i in range(n):
            frames += struct.pack("<h", int(9000 * math.sin(2 * math.pi * freq * i / rate)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


class MockTts(Tts):
    def synth(self, text: str, **kw) -> bytes:
        return _tone_wav()


class QwenTts(Tts):
    """
    阿里云百炼 Qwen-TTS（2026-09-19 接入）—— 云端语音合成，**多音色**。

    为什么需要它：本地 SAPI 只有一个中文音色，"法官 / 玩家"此前只能靠**语速**区分，
    9 种人格更是完全没法区分。云端这条能按音色（voice）分开 ——
    法官一个音色、每个 AI 座位各自一个音色。

    协议（DashScope 原生，**没有** OpenAI 兼容形态，文档确认）：
        POST https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
        {"model": "qwen3-tts-flash",
         "input": {"text": "...", "voice": "Cherry", "language_type": "Chinese"}}
        非流式返回 `output.audio.url`（OSS，24 小时有效）⇒ **要再下载一次**才拿到字节。

    ## 实测（2026-09-19）

        qwen3-tts-flash            HTTP 200  1.05 秒  下载 24k 单声道 WAV ✓
        qwen3-tts-instruct-flash   HTTP 200  1.13 秒（支持 `instructions` 指令控制）✓
        qwen-audio-3.1-tts-flash   HTTP 400  InvalidParameter: url error ✗
            （这个名字**服务端不认**，也在 /models 清单外；用户给的 key 是按模型授权的。
             对外叫法可能是控制台里的产品名，调 API 要用清单里的 `qwen3-tts-*`。）

    ## 两个必须处理的坑

    1. **下载下来的 WAV 头可能不可信**：实测某一版的 `data` 长度字段是错的
       （按头算出来 44739 秒）。所以这里**重新写一遍头**（见 `_rewrite_wav_header`）——
       直接转发原始字节的话，播放器/波形图会按错长度算。
    2. **文本有上限**：qwen-TTS 系列每次 512 token。AI 的发言很短（≤25 字），
       但法官台词可能长 ⇒ 超长就**分段合成再拼**（简单起见这里直接拒绝超长并说清楚，
       不悄悄截断 —— 悄悄截断的表现是"少了一句话"，没人查得出来）。
    """

    RETRY_STATUS = (429, 500, 502, 503, 504)
    URL = ("https://dashscope.aliyuncs.com/api/v1/services/aigc/"
           "multimodal-generation/generation")
    MAX_CHARS = 500          # 512 token 的中文保守估计；超了直接报错，不静默截断

    def __init__(self, model: str | None = None, voice: str | None = None,
                 api_key: str | None = None, timeout: float | None = None,
                 language_type: str = "Chinese", instructions: str = "") -> None:
        self.model = model or os.environ.get("DASHSCOPE_TTS_MODEL", "qwen3-tts-flash")
        self.voice = voice or os.environ.get("DASHSCOPE_TTS_VOICE", "Cherry")
        # TTS 用**第二把 key**（按模型授权）：留空回落到 ASR 那把。
        self.api_key = (api_key or os.environ.get("DASHSCOPE_TTS_API_KEY")
                        or os.environ.get("DASHSCOPE_API_KEY", ""))
        self.timeout = float(timeout or os.environ.get("DASHSCOPE_TTS_TIMEOUT", "30"))
        self.language_type = language_type
        self.instructions = instructions or os.environ.get("DASHSCOPE_TTS_INSTRUCT", "")
        self.model_path = f"{self.model} voice={self.voice}"     # 健康检查显示用

    def synth(self, text: str, rate: int = 0, volume: int = 100,
              voice: str | None = None, **kw) -> bytes:
        text = (text or "").strip()
        if not text:
            return b""
        if len(text) > self.MAX_CHARS:
            raise RuntimeError(
                f"这一段太长（{len(text)} 字 > {self.MAX_CHARS}）—— 云端 TTS 单次有上限，"
                f"不要静默截断（那会少一句话且没人发现），请在调用方拆句。")
        if not self.api_key:
            raise RuntimeError("未配置 DASHSCOPE_TTS_API_KEY / DASHSCOPE_API_KEY")
        inp: dict = {"text": text, "voice": voice or self.voice,
                     "language_type": self.language_type}
        # `instructions` 只有 instruct 系列认；带上它对 flash 系列没用但也不报错，
        # 所以按模型名判断一下更稳（避免"配了个指令但完全没生效"）。
        if self.instructions and "instruct" in self.model:
            inp["instructions"] = self.instructions
        payload = {"model": self.model, "input": inp}
        last: Exception | None = None
        for i in range(max(1, int(os.environ.get("DASHSCOPE_TTS_RETRIES", "2")))):
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    r = c.post(self.URL,
                               headers={"Authorization": f"Bearer {self.api_key}",
                                        "Content-Type": "application/json"},
                               json=payload)
                    if r.status_code in self.RETRY_STATUS:
                        raise httpx.HTTPStatusError(
                            f"HTTP {r.status_code}", request=r.request, response=r)
                    if r.status_code != 200:
                        raise RuntimeError(f"Qwen-TTS HTTP {r.status_code}：{r.text[:300]}")
                    url = (((r.json().get("output") or {}).get("audio") or {})
                           .get("url") or "")
                    if not url:
                        raise RuntimeError(f"Qwen-TTS 没给音频地址：{str(r.json())[:300]}")
                    a = c.get(url)
                    a.raise_for_status()
                    return _rewrite_wav_header(a.content)
            except httpx.TimeoutException as e:
                raise RuntimeError(f"Qwen-TTS 超时（{self.timeout}s）：{e}") from e
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
                last = e
                if i + 1 < max(1, int(os.environ.get("DASHSCOPE_TTS_RETRIES", "2"))):
                    time.sleep(min(4.0, 0.6 * (2 ** i)))
        raise RuntimeError(f"Qwen-TTS 调用失败：{last}")


def _rewrite_wav_header(data: bytes) -> bytes:
    """
    把 WAV 的 RIFF/data 长度字段**按真实字节数重写**，然后返回。

    为什么必须做：实测云端返回的 WAV 有一次头里的长度字段是错的
    （按它算出来 44739 秒）。我们原样转发的话，播放器、波形图、以及
    `analyze_input` 都会按错长度算 —— 而"音频听起来没问题"会让这种错很难被发现。

    不是标准 WAV 就原样返回（宁可原样转发，也不要在这里把数据弄坏）。
    """
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            params = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            frames = w.readframes(w.getnframes())
        if not frames:
            return data
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(params[0])
            out.setsampwidth(params[1])
            out.setframerate(params[2])
            out.writeframes(frames)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return data


class SapiTts(Tts):
    """
    Windows 自带语音合成（SAPI）—— 零成本、零 key、离线。

    为什么需要它：TTS 一直挂在 mock 上，听到的只是一声蜂鸣，
    于是「法官台词遮盖延迟」这类设计**根本没法评估** ——
    法官那句「请 3 号发言」本身就要 1.5–2 秒，是我们用来盖住 LLM 延迟的手段，
    它必须是真人语音才有意义。

    局限（用之前必须知道）：
      · 只有 Windows 有；中文音色通常只有一个（Microsoft Huihui）
      · 所以「法官音色 ≠ AI 音色」目前只能靠**语速**区分，真上线要换 CosyVoice 多音色
      · 同步阻塞且 COM 不是线程安全的 ⇒ 加锁 + 由调用方丢线程池
      · 输出 22050Hz / 16bit / mono

    实测：comtypes 调 SAPI.SpVoice + SAPI.SpFileStream 写文件。
    （PowerShell 的 Add-Type / COM 在本机被安全策略拦，Python 侧可用。）
    """

    _lock = threading.Lock()

    def __init__(self) -> None:
        self.voice_hint = os.environ.get("SAPI_VOICE", "ZH-CN")
        self.base_rate = int(os.environ.get("SAPI_RATE", "2"))   # SAPI: -10..10

    def synth(self, text: str, rate: int = 0, volume: int = 100, **kw) -> bytes:
        import comtypes.client

        text = (text or "").strip()
        if not text:
            return _tone_wav(0.1)

        with self._lock:
            stream = comtypes.client.CreateObject("SAPI.SpFileStream")
            voice = comtypes.client.CreateObject("SAPI.SpVoice")
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            try:
                picked = None
                for v in voice.GetVoices():
                    desc = f"{getattr(v, 'Id', '')} {v.GetDescription()}"
                    if self.voice_hint in desc:
                        picked = v
                        break
                if picked is not None:
                    voice.Voice = picked
                voice.Rate = max(-10, min(10, self.base_rate + int(rate)))
                voice.Volume = max(0, min(100, int(volume)))
                stream.Open(tmp.name, 3, False)     # 3 = SSFMCreateForWrite
                voice.AudioOutputStream = stream
                voice.Speak(text)
                stream.Close()
                return Path(tmp.name).read_bytes()
            finally:
                try:
                    stream.Close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass


class DoubaoHttpTts(Tts):
    """
    火山引擎语音合成 —— 骨架，待接。

    TODO(需要用户提供):
      1. AppID / Access Token / Cluster
      2. endpoint 与请求体字段（音色 voice_type、编码 encoding、语速 speed）
      3. 返回的音频是 base64 还是二进制流
    """

    def __init__(self) -> None:
        self.appid = os.environ.get("VOLC_TTS_APPID", "")
        self.token = os.environ.get("VOLC_TTS_TOKEN", "")
        self.voice = os.environ.get("VOLC_TTS_VOICE", "")

    def synth(self, text: str, **kw) -> bytes:
        if not (self.appid and self.token):
            raise RuntimeError("未配置 VOLC_TTS_APPID / VOLC_TTS_TOKEN")
        raise NotImplementedError(
            "火山 TTS 尚未接入：需要确认 endpoint 与字段，见 README-P0.md"
        )
