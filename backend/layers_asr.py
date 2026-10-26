# -*- coding: utf-8 -*-
"""ASR 层实现（Mock / 火山豆包 / 通义 Qwen / faster-whisper 本地） + 拾音质量分析（analyze_input）。

2026-10-06 P4a：自 server.py 机械搬移（原样保留，不改任何行为）——
server.py 只留装配（build_pipeline/lifespan）与传输（路由/WS）。
"""
import base64
import io
import math
import os
import time
import wave
from dataclasses import dataclass
import httpx


@dataclass
class Timings:
    asr_ms: float = 0.0
    llm_ms: float = 0.0
    tts_ms: float = 0.0
    total_ms: float = 0.0


# ⚠️ 2026-09-25 删掉了这里的 @dataclass `UtterResult`：全仓只出现一次（=只有定义、
#    从没被引用），是早期 /api/utter 的结构残留 —— 现在那条路直接返回 dict。
#    （哪天要把它改回强类型，上下文在 `狼人杀旧版本参照.md` 的决策记录里。）
# ---------------------------------------------------------------- ASR 层

class Asr:
    def transcribe(self, audio: bytes, mime: str, rate: int) -> str:
        raise NotImplementedError


class MockAsr(Asr):
    def transcribe(self, audio: bytes, mime: str, rate: int) -> str:
        return "（mock）我是三号玩家，昨晚我没看到什么特别的。"


class DoubaoAsr(Asr):
    """
    火山引擎语音识别 —— 骨架，待接。

    TODO(需要用户提供):
      1. AppID / Access Token / Cluster（火山语音技术控制台）
      2. 当前使用的接口是「大模型录音文件识别」还是老版 /api/v1/asr，
         两者请求体与服务地址不同，我不能凭记忆写死
      3. 返回结构里转写文本的字段路径

    参考实现骨架如下（把 TODO 处补上即可）：
    """

    def __init__(self) -> None:
        self.appid = os.environ.get("VOLC_ASR_APPID", "")
        self.token = os.environ.get("VOLC_ASR_TOKEN", "")
        self.cluster = os.environ.get("VOLC_ASR_CLUSTER", "")

    def transcribe(self, audio: bytes, mime: str, rate: int) -> str:
        if not (self.appid and self.token):
            raise RuntimeError("未配置 VOLC_ASR_APPID / VOLC_ASR_TOKEN")
        # TODO: 组装真实请求。音频为 16k 单声道 PCM-WAV。
        raise NotImplementedError(
            "火山 ASR 尚未接入：需要确认接口版本与服务地址，见 docs/README-P0.md"
        )


class QwenAsr(Asr):
    """
    阿里云百炼 Qwen-ASR（2026-09-19 接入）—— 云端识别，用于**真上线那条路**。

    走 **OpenAI 兼容协议**（和 OpenAICompatLlm 同一个思路）：
        POST {DASHSCOPE_BASE_URL}/chat/completions
        {"model": "...", "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": "data:audio/wav;base64,..."}}]}],
         "asr_options": {"language": "zh", "enable_itn": true}}
        识别文本在 `choices[0].message.content`。

    ## 为什么是兼容协议而不是 DashScope 原生协议（同一把 key、同一个模型）

    实测（2026-09-19，同一个 5 秒夹具，各跑一次）：
        兼容协议  1.24 ~ 1.51 秒      ← 用这条
        原生协议  22.30 秒            ← 同一个模型，慢 15 倍
    没有猜：两条都真调过才定的。（原生协议的写法：input.messages[].content[].audio
    + parameters.asr_options，文本在 output.choices[0].message.content[0].text。）

    ## 关于「Qwen-Audio-3.0-ASR-Flash」

    它在模型清单里（`qwen-audio-3.0-asr-flash`），但**两种协议用 base64 都报
    UNSUPPORTED_FORMAT: format is empty** ⇒ 它要么只接受**公网 URL**，
    要么走异步文件识别（提交-轮询）那条路。我们的音频是内存里的字节流
    （手机直传，没有公网 URL），所以当前形态下用它不划算也没必要 ——
    `qwen3-asr-flash` 实测四段夹具**逐字全对**。
    想换模型只改 .env 的 DASHSCOPE_ASR_MODEL，代码不用动。

    ## 实测质量（4 段 16k 单声道夹具，狼人杀黑话全对）

        「我是三号玩家，昨晚我没有看到什么特别的。」
        「我觉得五号有问题，他的发言前后矛盾，今天先把票归给他。」
        「金水是四号，查杀六号，我建议今天先出六号。」
        「昨晚是平安夜，女巫可能用了解药，大家先不要急着站边。」

    ⚠️ 音频一律是 16k 单声道 WAV（上游 `analyze_input` / 本地实现都用 wave 头解析，
    客户端也统一发 WAV），这里只负责 base64 与请求，不做转码。
    """

    # 这些状态先退避重试（和 OpenAICompatLlm 一致）：
    #   429 = 平台限流；5xx = 平台侧瞬时故障。超时**不重试**（它已经把预算烧光了）。
    RETRY_STATUS = (429, 500, 502, 503, 504)

    # 🔴 不要给它塞"上下文词表"：这一版服务**不接受任何文本输入**。
    #    （我照文档试过 system 上下文，实测 2026-09-19：
    #       system 有内容 → 400 InternalError.Algo.InvalidParameter；
    #       system 空串   → 200；
    #       文本和音频混在一条 user 里 → 400。
    #     也就是说"金水/查杀"这类黑话词表喂不进去 —— 本地 whisper 那条
    #     initial_prompt 在云端没有对应物。好在实测黑话全对，不喂也行。）

    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, timeout: float | None = None,
                 language: str | None = None, retries: int | None = None) -> None:
        # 参数优先、环境变量兜底（和 OpenAICompatLlm 同一个写法：将来要装成两条通道时不用改签名）
        self.base_url = (base_url or os.environ.get("DASHSCOPE_BASE_URL")
                         or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        # DASHSCOPE_API_KEY 是百炼 SDK 的通用环境变量名；ASR_API_KEY 留作覆盖口
        # （比如将来换一家云 ASR，不必动这个类）。
        self.api_key = (api_key or os.environ.get("ASR_API_KEY")
                        or os.environ.get("DASHSCOPE_API_KEY", ""))
        self.model = model or os.environ.get("DASHSCOPE_ASR_MODEL", "qwen3-asr-flash")
        self.timeout = float(timeout or os.environ.get("DASHSCOPE_TIMEOUT", "15"))
        # 语种**能确定才填**：文档明确说"语种不确定或混合语种时不要指定"。
        # 我们是中文局，所以默认 zh；想让它自动判别就把 DASHSCOPE_ASR_LANG 设成空。
        self.language = (language if language is not None
                         else os.environ.get("DASHSCOPE_ASR_LANG", "zh")).strip()
        self.retries = max(1, int(retries if retries is not None
                                 else os.environ.get("DASHSCOPE_RETRIES", "2")))
        # 健康检查与 _smoke-asr-local.py 都靠这个字段显示"用的哪个实现/模型"。
        self.model_path = f"{self.model} @ {self.base_url}"

    def transcribe(self, audio: bytes, mime: str, rate: int) -> str:
        if not self.api_key:
            raise RuntimeError("未配置 DASHSCOPE_API_KEY（阿里云百炼的 key）")
        if not audio:
            return ""
        data_url = (f"data:{mime or 'audio/wav'};base64,"
                    + base64.b64encode(audio).decode())
        opts: dict = {"enable_itn": True}
        if self.language:
            opts["language"] = self.language
        messages = [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": data_url}}]}]
        payload = {"model": self.model, "messages": messages, "asr_options": opts}

        last: Exception | None = None
        for i in range(self.retries):
            try:
                with httpx.Client(timeout=self.timeout) as c:
                    r = c.post(f"{self.base_url}/chat/completions",
                               headers={"Authorization": f"Bearer {self.api_key}",
                                        "Content-Type": "application/json"},
                               json=payload)
                if r.status_code in self.RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"HTTP {r.status_code}", request=r.request, response=r)
                if r.status_code != 200:
                    # 400/401/404 这类是**配置或参数错**，重试没意义 ⇒ 直接把原文带出来，
                    # 否则排查时只能看到"调用失败"四个字（doc 里的 UNSUPPORTED_FORMAT
                    # 就是这么被看见的）。
                    raise RuntimeError(
                        f"Qwen-ASR HTTP {r.status_code}：{r.text[:300]}")
                body = r.json()
                msg = (body.get("choices") or [{}])[0].get("message") or {}
                return str(msg.get("content") or "").strip()
            except httpx.TimeoutException as e:
                raise RuntimeError(f"Qwen-ASR 超时（{self.timeout}s）：{e}") from e
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
                last = e
                if i + 1 < self.retries:
                    time.sleep(min(4.0, 0.6 * (2 ** i)))       # 0.6s / 1.2s
        raise RuntimeError(f"Qwen-ASR 调用失败（已重试 {self.retries} 次）：{last}")

    def warmup(self) -> None:
        """
        启动时先打一次**极短**的请求（0.2 秒的啁啾声）。

        为什么需要（实测，不是推理）：常态 1.2~1.4 秒，但**重启后第一次**调用
        量到 **16.3 秒** —— 建连 + 平台侧冷启动都压在这一句上。
        桌上没人能等 16 秒，所以把这笔钱挪到启动时付：代价不到 1 秒音频的计费。

        失败**不算致命**：预热挂了（key 不对 / 断网）服务照样起，
        真正的错误会在第一次真实请求时带着服务端原文返回 —— 那比"启动就崩"好排查。
        """
        self.transcribe(_tone_wav(0.2, 440.0, 16000), "audio/wav", 16000)


def _resample(pcm, src_rate: int, dst_rate: int = 16000):
    """线性插值重采样。够用就行，不做抗混叠。"""
    import numpy as np

    if src_rate == dst_rate or len(pcm) == 0:
        return pcm
    n = max(1, int(len(pcm) * dst_rate / src_rate))
    idx = np.linspace(0, len(pcm) - 1, n)
    return np.interp(idx, np.arange(len(pcm)), pcm).astype(np.float32)


# ---------------------------------------------------------------- 拾音质量

def analyze_input(audio: bytes) -> dict:
    """
    量一下这段录音「收到了多少声音」，与用什么 ASR 无关。

    为什么必须有这个：判断「单机形态（手机平放桌中央、对面 1.5m）成不成立」，
    如果只看 ASR 转写，就分不清两种失败 ——
      · 麦克风没收到/收得太弱（形态问题，换 ASR 也救不了）
      · 收到了但模型不行（换云端 ASR 就能解决）
    声学指标能把这两件事分开。

    返回的都是 dBFS（0 = 满刻度，负得越多越轻）。
    """
    import numpy as np

    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            width = w.getsampwidth()
            frames = w.readframes(w.getnframes())
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}

    if width != 2 or not frames:
        return {"error": f"不是 16bit PCM（{width * 8}bit）"}

    x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    n = x.size
    if n == 0:
        return {"error": "空音频"}

    rms = float(np.sqrt(np.mean(x ** 2)) + 1e-12)
    peak = float(np.max(np.abs(x)) + 1e-12)
    clip_pct = float(np.mean(np.abs(x) >= 0.99) * 100)

    # 20ms 一帧，取帧能量的上 30% 当「说话」、下 30% 当「底噪」
    fl = max(1, int(rate * 0.02))
    nf = n // fl
    if nf >= 5:
        fe = np.sqrt(np.mean(x[:nf * fl].reshape(nf, fl) ** 2, axis=1))
        srt = np.sort(fe)
        k = max(1, int(nf * 0.3))
        noise = float(np.mean(srt[:k]) + 1e-12)
        voice = float(np.mean(srt[-k:]) + 1e-12)
        snr = 20 * math.log10(voice / noise)
        voice_ratio = float(np.mean(fe > noise * 3.16)) * 100   # 高于底噪 10dB
    else:
        snr, voice_ratio = 0.0, 0.0

    dur = n / rate
    # 全静音时 20log10(1e-12) = −240 dBFS，显示出来会让人莫名其妙。
    # 夹到 −100 表示「低到没有意义」，判读文字才是重点。
    rms_db = max(-100.0, 20 * math.log10(rms))
    peak_db = max(-100.0, 20 * math.log10(peak))

    # 给一句人话判读，避免用户对着数字猜
    if rms_db < -55:
        verdict = "几乎没收到声音 —— 麦克风静音、被遮挡，或距离过远"
    elif rms_db < -40:
        verdict = "声音很轻 —— 远场会明显吃力，建议调近或提音量"
    elif clip_pct > 1.0:
        verdict = "有明显爆音（离太近/音量太大），识别也会变差"
    elif snr < 6:
        # 帧能量几乎没有起伏 ⇒ 没有「说话 vs 停顿」的对比。
        # 可能是稳态噪声（麦克风被静音后的底噪、空调声），也可能是语音被噪声淹没。
        # 真实人说话一定有明显的音节起伏，所以这一条能把「没收到人声」认出来。
        verdict = "信号很平稳：不像人说话 —— 要么是稳态噪声，要么语音被环境噪声淹没了"
    elif snr < 10:
        verdict = "底噪偏高 —— 环境吵，信噪比不足"
    else:
        verdict = "拾音正常"

    return {
        "seconds": round(dur, 2),
        "sample_rate": rate,
        "channels": ch,
        "rms_dbfs": round(rms_db, 1),
        "peak_dbfs": round(peak_db, 1),
        "clipping_pct": round(clip_pct, 2),
        "snr_db": round(snr, 1),
        "voice_ratio_pct": round(voice_ratio, 1),
        "verdict": verdict,
    }


class FasterWhisperAsr(Asr):
    """
    本地 faster-whisper —— 不需要任何云端 key，适合先验证「远场能不能听清」。

    需要：pip install faster-whisper

    ⚠️ 它的用途是判断拾音质量，不是最终方案：CPU 上比云端慢得多，
    真上线要换成云端流式 ASR。
    """

    def __init__(self, model_size: str = "small") -> None:
        self.model_size = model_size
        self._model = None
        # 允许直接指向本地模型目录（含 model.bin / config.json / tokenizer.json / vocabulary.txt）。
        # 这是最省事也最稳的路：完全绕开 HuggingFace 与网络问题。
        self.model_path = os.environ.get("ASR_MODEL_PATH", "").strip()

    def _lazy(self):
        if self._model is not None:
            return self._model
        from faster_whisper import WhisperModel  # 延迟导入

        if self.model_path:
            if not os.path.isdir(self.model_path):
                raise RuntimeError(f"ASR_MODEL_PATH 指向的目录不存在：{self.model_path}")
            self._model = WhisperModel(self.model_path, device="cpu", compute_type="int8")
            return self._model

        # 默认只用本地缓存，不联网。
        # 原因：huggingface_hub 加载时会去检查模型是否更新，在有代理的环境里
        # 会被拦成 502（ProxyError），而模型其实早就下过了。
        offline = os.environ.get("ASR_OFFLINE", "1") == "1"
        try:
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8",
                local_files_only=offline,
            )
        except Exception as e:  # noqa: BLE001
            if not offline:
                raise
            print(f"[asr] 本地缓存不可用（{type(e).__name__}: {e}），改为联网下载…", flush=True)
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8",
                local_files_only=False,
            )
        return self._model

    def warmup(self) -> None:
        """提前加载模型，避免第一次请求超时被误判成"听不清"。"""
        self._lazy()

    def transcribe(self, audio: bytes, mime: str, rate: int) -> str:
        import numpy as np

        model = self._lazy()
        with wave.open(io.BytesIO(audio), "rb") as w:
            src_rate = w.getframerate()
            channels = w.getnchannels()
            width = w.getsampwidth()
            frames = w.readframes(w.getnframes())

        if width != 2:
            raise ValueError(f"只支持 16bit PCM，收到 {width * 8}bit")

        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1)

        # 采样率以 WAV 头为准，不信调用方传进来的 rate —— 实测里 SAPI 导出的是 22k，
        # 若按 16k 硬解，识别结果会整段错乱。
        if src_rate != 16000:
            pcm = _resample(pcm, src_rate, 16000)

        segments, _ = model.transcribe(
            pcm, language="zh", beam_size=5, vad_filter=True,
            initial_prompt="狼人杀游戏对局。金水、查杀、悍跳、警徽流、归票、守卫、女巫、预言家。",
        )
        return "".join(s.text for s in segments).strip()
