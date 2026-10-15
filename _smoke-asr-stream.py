# -*- coding: utf-8 -*-
"""
流式 ASR（`/ws/asr` 中继）的冒烟测试。

验的是这条链：夹具 PCM → 我们的 WebSocket → 百炼 realtime → 增量/最终文本回来。

为什么值得单独一个文件：
    · 流式这条路有**两个只有真跑才会暴露**的性质（都已经踩过）：
        ① 上游 VAD 会自动断句 ⇒ 一次会话有**多个** final（只取最后一个会丢半句）
        ② 增量分 `text`（已确认）+ `stash`（草稿）两段，只显示 text 会像"没识别出来"
    · 而且它必须**真连**：中继层的事件转发（哪些上游事件翻成哪种前端事件）错了，
      单测怎么 mock 都测不出来语义。

前置：服务在 http://127.0.0.1:8000 上跑着（start.py），且 .env 里配了 DASHSCOPE_API_KEY。
退出码（三个必须分得清，同 _smoke-play.js 的约定）：
    0 = 跑了，全过；1 = 跑了，有失败；2 = **没跑成**（服务不在 / 没装 websockets）
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import time
import wave
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

WS_URL = os.environ.get("PLAY_BASE_WS", "ws://127.0.0.1:8000/ws/asr")
CHUNK_MS = 100          # 每片 100 毫秒（文档建议发小块，响应更快）

PASS: list = []
FAIL: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if detail and not ok else ""))


def fixture_pcm16() -> bytes:
    """夹具 → 16k 单声道 PCM16（上游要的就是它，不是 WAV）。"""
    import numpy as np

    from server import _resample

    p = BASE / "_test_zh1.wav"
    with wave.open(str(p), "rb") as w:
        raw, rate, ch = w.readframes(w.getnframes()), w.getframerate(), w.getnchannels()
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    if rate != 16000:
        pcm = _resample(pcm, rate, 16000)
    return (pcm * 32767).astype(np.int16).tobytes()


def server_alive() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


async def run() -> int:
    import websockets

    pcm = fixture_pcm16()
    print(f"\n夹具：{len(pcm)} 字节 ≈ {len(pcm) / 2 / 16000:.2f} 秒（16k 单声道 PCM16）\n")

    partials: list = []
    finals: list = []
    events: list = []
    ready = False
    finished = False
    error: str = ""
    # 「第一个增量是在**说到第几秒**出现的」——这才是"边说边出字"的指标。
    # ⚠️ 第一版我在发送循环**之后**才记时间，量到的是"发送总时长"而不是首字延迟
    #    （测试自己的错，不是产品的）。
    marks = {"t0": 0.0, "first_partial": None}

    async with websockets.connect(WS_URL, open_timeout=15, max_size=None) as ws:
        # 一边发一边收：先起一个收报任务，避免"发送阻塞住收不到"的假死
        async def recv_loop() -> None:
            nonlocal ready, finished, error
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=25)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    return
                try:
                    ev = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                t = ev.get("type")
                events.append(t)
                if t == "ready":
                    ready = True
                elif t == "partial":
                    partials.append((ev.get("text") or "") + (ev.get("stash") or ""))
                    if marks["first_partial"] is None and marks["t0"]:
                        marks["first_partial"] = time.perf_counter() - marks["t0"]
                elif t == "final":
                    finals.append(ev.get("transcript") or "")
                elif t == "finished":
                    finished = True
                    return
                elif t == "error":
                    error = ev.get("message") or "?"
                    return

        rt = asyncio.create_task(recv_loop())
        # 等 ready 再喂音频（上游会话建好之前发出去的数据会被丢）
        for _ in range(60):
            if ready or error:
                break
            await asyncio.sleep(0.2)

        step = 16000 * 2 * CHUNK_MS // 1000
        marks["t0"] = time.perf_counter()
        for i in range(0, len(pcm), step):
            await ws.send(pcm[i:i + step])
            await asyncio.sleep(CHUNK_MS / 1000 * 0.9)      # 接近真人语速
        _sent = time.perf_counter() - marks["t0"]
        await ws.send(json.dumps({"type": "finish"}))
        try:
            await asyncio.wait_for(rt, timeout=25)
        except asyncio.TimeoutError:
            rt.cancel()

    joined = "".join(finals)
    print(f"  事件序列（去重前 12 个）: {events[:12]}")
    print(f"  增量条数: {len(partials)}  最终条数: {len(finals)}")
    print(f"  拼接最终文本: {joined!r}")
    _fp = marks["first_partial"]
    print(f"  首字延迟 {_fp:.2f} 秒（音频总长 {len(pcm) / 2 / 16000:.2f} 秒，"
          f"发送用了 {_sent:.2f} 秒）—— 小于音频总长才叫「边说边出字」")
    print()

    check("★ 拿到 ready（上游会话建好了才喂音频，不靠猜时机）", ready, error or "没收到 ready")
    check("★ 有增量（text+stash 拼起来能看到「边说边出字」），不是等说完才出",
          len(partials) >= 1 and any(p.strip() for p in partials), f"{len(partials)} 条")
    check("★ 有最终结果（final.transcript）", len(finals) >= 1 and bool(joined.strip()),
          f"{len(finals)} 条")
    check("★ 首字延迟 **小于** 音频总长（这才叫流式；等于总长就只是「说完才出字」）",
          marks["first_partial"] is not None
          and marks["first_partial"] < len(pcm) / 2 / 16000,
          f"首字 {marks['first_partial']} 秒 / 音频 {len(pcm) / 2 / 16000:.2f} 秒")
    check("★ VAD 自动断句 ⇒ 允许/预期出现**多个** final（只取最后一个会丢半句）",
          len(finals) >= 1, f"{len(finals)} 条")
    check("★ 识别内容对得上夹具（含「三号」「玩家」）",
          ("三号" in joined or "3号" in joined) and "玩家" in joined, joined)
    check("★ 发 finish 之后收到 finished（收尾不靠超时兜）", finished, error or "没收到 finished")
    check("没有 error 事件", not error, error)
    return 0


def main() -> int:
    try:
        import websockets  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"没装 websockets，这个测试跑不了：{e}")
        return 2
    if not server_alive():
        print("服务不在 127.0.0.1:8000 上（先跑 start.py）—— 这是**没跑成**，不是通过")
        return 2
    print("=== 流式 ASR（/ws/asr）冒烟 ===\n")
    asyncio.run(run())
    print("=" * 74)
    print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
