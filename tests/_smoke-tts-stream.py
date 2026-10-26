# -*- coding: utf-8 -*-
"""
流式 TTS（`/ws/tts` 中继）的冒烟测试。

验的是这条链：文本 → 我们的 WebSocket → 百炼 realtime TTS → PCM 分片回来。

为什么值得单独一个文件：
  · 这一条的价值**全在时间上**（首片音频多快能出声）。不真跑，任何 mock 都测不出"快"。
  · 而且它是**协议翻译层**：上游是 `response.audio.delta`(base64)，
    我们给前端的是**裸 PCM 二进制帧** + start/done 两个控制事件 ——
    翻错了（比如把 base64 原样丢过去）前端只会放出噪音。

前置：服务在 http://127.0.0.1:8000 上跑着（start.py），且 .env 里配了 TTS 的 key。
退出码：0 = 全过；1 = 有失败；2 = **没跑成**（服务不在 / 没装 websockets）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/

WS = os.environ.get("PLAY_BASE_WS", "ws://127.0.0.1:8000")
TEXT = "天亮了，请各位睁眼。昨晚是平安夜，没有发生任何事。"

PASS: list = []
FAIL: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if detail and not ok else ""))


def server_alive() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


async def stream_once(voice: str = "Neil") -> dict:
    import websockets
    from urllib.parse import quote

    url = f"{WS}/ws/tts?text={quote(TEXT)}&voice={voice}"
    t0 = time.perf_counter()
    frames, first_at, done = 0, None, False
    start_ev, err = {}, ""
    async with websockets.connect(url, open_timeout=15, max_size=None) as ws:
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                break
            if isinstance(raw, bytes):
                if first_at is None:
                    first_at = time.perf_counter() - t0
                frames += 1
                continue
            ev = json.loads(raw)
            t = ev.get("type")
            if t == "start":
                start_ev = ev
            elif t == "done":
                done = True
                break
            elif t == "error":
                err = ev.get("message") or "?"
                break
    return {"frames": frames, "first": first_at, "done": done,
            "start": start_ev, "err": err, "total_s": time.perf_counter() - t0}


async def run() -> None:
    r = await stream_once()
    print(f"  分片 {r['frames']} 片，首片 "
          f"{'—' if r['first'] is None else str(round(r['first'] * 1000)) + ' ms'}，"
          f"整体 {r['total_s'] * 1000:.0f} ms")
    check("★ 连上就收到 start（带格式与采样率，前端据此建 AudioBuffer）",
          r["start"].get("format") == "pcm" and r["start"].get("sample_rate") == 24000,
          str(r["start"]))
    check("★ 收到**二进制 PCM 分片**（不是 base64 文本、也不是一堆 JSON）",
          r["frames"] >= 3, f"{r['frames']} 片")
    check("★ 收到 done（前端据此收尾，不靠超时兜）", r["done"], r["err"] or "没收到 done")
    check("没有错误事件", not r["err"], r["err"])

    # 第二句：**每句都是新连接**（不做复用，理由见 server.py 里"做过又撤了"那段），
    # 所以这里**不验"更快"** —— 两句的建连成本一样，比快慢只是比噪声。
    # 要验的是：连着来两句都拿得到完整音频（不能第一句好第二句哑）。
    r2 = await stream_once()
    print(f"  第二句：分片 {r2['frames']} 片，首片 "
          f"{'—' if r2['first'] is None else str(round(r2['first'] * 1000)) + ' ms'}"
          f"（每句都新建连接，与第一句同量级）")
    check("★ 连着两句都拿得到完整音频（不能第一句好、第二句哑）",
          r2["frames"] >= 3 and r2["done"] and not r2["err"],
          f"分片 {r2['frames']} done={r2['done']} err={r2['err']}")

    # 对照："合成完再播"那条路要等多久才有第一个字节
    t0 = time.perf_counter()
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/tts?text=" + urllib.parse.quote(TEXT) + "&voice=Neil")
    with urllib.request.urlopen(req, timeout=60) as resp:
        blob = resp.read()
    blob_first = time.perf_counter() - t0
    print(f"  对照（合成完再播）：{blob_first * 1000:.0f} ms 才拿到 {len(blob)} 字节")
    check("★ 流式的首片音频**比「合成完再播」快**（这才是做流式的理由）",
          r["first"] is not None and r["first"] < blob_first * 0.7,
          f"流式首片 {r['first'] and round(r['first'] * 1000)} ms vs "
          f"非流式 {blob_first * 1000:.0f} ms")

    # 错误路径：超长文本直接回 error（不该静默什么都不放）
    import websockets
    from urllib.parse import quote

    async with websockets.connect(f"{WS}/ws/tts?text={quote('字' * 700)}",
                                  open_timeout=15) as ws:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        ev = json.loads(raw)
    check("★ 超长文本回 error（不静默吞掉）", ev.get("type") == "error",
          str(ev)[:100])


def main() -> int:
    try:
        import websockets  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"没装 websockets，这个测试跑不了：{e}")
        return 2
    if not server_alive():
        print("服务不在 127.0.0.1:8000 上（先跑 start.py）—— 这是**没跑成**，不是通过")
        return 2
    print("=== 流式 TTS（/ws/tts）冒烟 ===\n")
    asyncio.run(run())
    print("\n" + "=" * 74)
    print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
