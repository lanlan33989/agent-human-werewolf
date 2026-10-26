# -*- coding: utf-8 -*-
"""
P0-talk · Day-1 技术探针

唯一目标：说一句话 -> 转成文字 -> LLM 回一句 -> 说出来
不含：游戏规则 / 多 AI / 夜间流程 / 保密时序 / 积分 / 打断 / 多设备

启动：
    uvicorn server:app --host 0.0.0.0 --port 8000

默认 MOCK=1，不需要任何 key 就能验证链路是否通。
把 MOCK 设为 0 即切到真实适配器（LLM 的 OpenAI 兼容实现是完整的；
ASR / TTS 的火山实现是骨架，见文件内 TODO）。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import struct
import tempfile
import threading
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

BASE = Path(__file__).parent


# ---------------------------------------------------------------- 环境变量

def _load_env() -> None:
    """极简 .env 读取，避免引入 python-dotenv。"""
    p = BASE / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

# 国内网络环境下 huggingface.co 常常不可达（在有代理的机器上会被拦成 502），
# 而 hf-mirror.com 可以。放在 import faster_whisper 之前生效。
# 想用官方源就在 .env 里显式设 HF_ENDPOINT。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# HuggingFace 新的 Xet 存储走 cas-server.xethub.hf.co，该域名不在镜像覆盖范围内，
# 会报 401 Unauthorized。必须退回传统 HTTP 下载。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

MOCK = os.environ.get("MOCK", "1") == "1"


def layer_mocked(name: str, fallback: bool) -> bool:
    """
    分层 mock 开关。

    为什么必须分层：不必一次接通全部。
    例如只想验证「远场到底听不听得清」时，可以只把 ASR 换成真的，
    LLM 与 TTS 继续 mock —— 否则会因为还没有 LLM key 而整条链路跑不动。

    优先读 MOCK_ASR / MOCK_LLM / MOCK_TTS，没设则跟随全局 MOCK。
    """
    v = os.environ.get(f"MOCK_{name}")
    if v is None:
        return fallback
    return v == "1"


# ---------------------------------------------------------------- 结果结构

# ================================================================
# 三层实现（ASR / LLM / TTS 类族）已拆到三个模块（2026-10-06 P4a）——
# 本文件只留：环境装配（build_pipeline / lifespan）、传输（路由 / WebSocket）、页面。
# ================================================================
from layers_asr import (Timings, Asr, MockAsr, DoubaoAsr, QwenAsr,
                        FasterWhisperAsr, _resample, analyze_input)
from layers_llm import Llm, MockLlm, OpenAICompatLlm, MockLlmSlow
from layers_tts import (Tts, MockTts, QwenTts, SapiTts, DoubaoHttpTts,
                        _tone_wav, _rewrite_wav_header)


# ---------------------------------------------------------------- 装配

def _real_asr() -> Asr:
    """ASR 选择：qwen（阿里云百炼，云端）| faster-whisper（本地，离线兜底）| doubao（骨架）。"""
    kind = os.environ.get("ASR", "doubao").lower()
    if kind in ("qwen", "dashscope", "bailian", "aliyun"):
        return QwenAsr()
    if kind in ("faster-whisper", "faster_whisper", "whisper", "local"):
        return FasterWhisperAsr(os.environ.get("ASR_MODEL", "small"))
    return DoubaoAsr()


def _real_tts() -> Tts:
    """TTS 选择：sapi（本地，Windows）| qwen（阿里云百炼，**多音色**）| doubao（骨架）。"""
    kind = os.environ.get("TTS", "sapi").lower()
    if kind in ("qwen", "dashscope", "bailian", "aliyun"):
        return QwenTts()
    if kind in ("sapi", "local", "windows"):
        try:
            import comtypes.client  # noqa: F401
            t = SapiTts()
            t.synth("测试")   # 先真跑一次，免得装配成功但运行时才炸
            print("[tts] 本地 SAPI 可用", flush=True)
            return t
        except Exception as e:  # noqa: BLE001
            print(f"[tts] 本地 SAPI 不可用（{type(e).__name__}: {e}），回退到 mock", flush=True)
            return MockTts()
    return DoubaoHttpTts()


def build_pipeline():
    asr: Asr = MockAsr() if layer_mocked("ASR", MOCK) else _real_asr()
    llm: Llm = MockLlm() if layer_mocked("LLM", MOCK) else OpenAICompatLlm()
    tts: Tts = MockTts() if layer_mocked("TTS", MOCK) else _real_tts()
    return asr, llm, tts


# ---------------------------------------------------------------- 运行时覆盖（配置台）
# 配置台的实现住在 `admin_api.py`：职责本来就独立（"运行时配置怎么读写" vs
# "语音链路怎么走"），而且全塞进这个文件会顶破 §2.1.4 锁着的 **1600 行拆分裂线**。
# 这里只做两件事：**装配前**把 `_runtime.json` 的覆盖应用下去，**装配后**把路由挂上。
import admin_api  # noqa: E402

admin_api.apply_runtime_state(BASE)      # ⚠️ 必须在 build_pipeline() **之前**
ADMIN_STATE = admin_api.STATE_PATH       # 面板与测试共用这一个状态文件路径


ASR, LLM, TTS = build_pipeline()

# 预热去重（见 lifespan 里的说明）：start.py 会起两个 uvicorn 实例，
# 每个都会跑一遍 lifespan，而预热**可能花钱**（云端 ASR），所以进程级只做一次。
_WARMED = {"local": False, "cloud": False}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    启动时预热 ASR。

    faster-whisper 首次加载模型要几秒到几十秒，不预热的话第一次请求会超时，
    很容易被误判成"远场听不清"。

    用 lifespan 而不是 @app.on_event：后者在较新的 FastAPI 里已经失效，
    之前那次预热没跑起来就是因为这个。

    ⚠️ `_WARMED` 是**进程级去重**：start.py 同时起 HTTP 与 HTTPS 两个 uvicorn
    （`uvicorn.Config(app, ...)` × 2），**每个都会跑一遍 lifespan** ⇒ 不去重就预热两次。
    本地那次只是白load一遍模型，云端那次是**真花钱**（每次调用都计费）——
    实测日志里两行"云端 ASR 已预热完成"就是这么来的。
    """
    # ⚠️ 必须**先占位再 await**：两个 uvicorn 的 lifespan 是并发跑的，
    #    中间一旦有 await（warmup 在线程池里跑），"检查后再做"就会双双通过 ⇒ 预热两遍。
    #    实测就是这么发生的（日志里两行）。失败时把位置放回去，下次还能再试。
    if isinstance(ASR, FasterWhisperAsr) and not _WARMED["local"]:
        _WARMED["local"] = True
        try:
            await run_in_threadpool(ASR.warmup)
            print("[server] 本地 ASR 已预热完成", flush=True)
        except Exception as e:  # noqa: BLE001
            _WARMED["local"] = False
            print(f"[server] 本地 ASR 预热失败：{type(e).__name__}: {e}", flush=True)
    # 云端 ASR 也预热：它的长尾压在**第一次**调用上
    # （实测 16.3 秒 vs 稳态 1.2~1.4 秒；预热后第一次真实请求回到 1.28 秒）。
    # 预热失败**不拦启动**：key 不对/断网时服务照常起，错误留到真实请求时带原文返回。
    if isinstance(ASR, QwenAsr) and not _WARMED["cloud"]:
        _WARMED["cloud"] = True
        try:
            await run_in_threadpool(ASR.warmup)
            print("[server] 云端 ASR（Qwen）已预热完成", flush=True)
        except Exception as e:  # noqa: BLE001
            _WARMED["cloud"] = False
            print(f"[server] 云端 ASR 预热失败（不影响启动）：{type(e).__name__}: {e}",
                  flush=True)
    _start_drive_pump()
    yield


# ---------------------------------------------------------------- 推进水泵
# 🔴 2026-10-23（用户点名，参考队友局域网版 server/room.ts + pacing.ts）：
#    他们的状态机是"服务端双层定时器"驱动：AI 水泵（单 setTimeout 链持续推进）
#    + 独立的 deadlineGuard（到点裁决）—— 推进**不依赖客户端轮询**。
#    我们原来把推进全挂在 GET /public 上：页面关了 / 轮询被卡住 / 客户端只在
#    面板页才轮询，状态机就停摆（夜间面板不弹、窗口到点不收人，都是这条链的病）。
#    现在补上服务端水泵：后台线程周期性把每一局往前推。/public 的 drive()
#    降级为"赶上进度"的补充信号（双层保留客户端这一拍：谁先到谁干活，s.lock 互斥）。
_PUMP_STARTED = {"v": False}


def _start_drive_pump() -> None:
    """启动推进水泵（**进程级去重**：start.py 的两个 uvicorn 实例共用本进程
    的模块状态，与上面的 `_WARMED` 同一套路；若按进程分开跑，各自 SESSIONS
    也是各自进程的，多一个空转水泵无害）。"""
    if _PUMP_STARTED["v"]:
        return
    _PUMP_STARTED["v"] = True

    def _loop() -> None:
        while True:
            time.sleep(0.3)
            for s in list(game_api.SESSIONS.values()):
                try:
                    if s.game.winner is None:
                        # drive() 内部自己拿 s.lock（与 /act、/public 互斥）；
                        # 人类有待办时 auto_advance 立刻返回，这里几乎零开销。
                        s.drive(0.15)
                except Exception as e:  # noqa: BLE001
                    # 单局故障不能拖死水泵：打日志、继续推别的局
                    print(f"[pump] {s.id}: {type(e).__name__}: {e}", flush=True)

    threading.Thread(target=_loop, name="drive-pump", daemon=True).start()
    print("[server] 推进水泵已启动（0.3s 节拍）", flush=True)


app = FastAPI(title="P0-talk", lifespan=lifespan)

# 对局 API（D4）：把规则引擎与 AI 玩家接到 HTTP 上
import game_api  # noqa: E402
import summarize  # noqa: E402

app.include_router(game_api.router)

# 提示词实验室对接（2026-10-26）：步骤落盘 + 暂停/单步 + 步骤查询。
# 独立模块、独立前缀 /api/lab，不碰对局逻辑；默认只落盘、不暂停。
import lab_api  # noqa: E402

app.include_router(lab_api.router)
# 局面回退（2026-10-26）：lab_api 需要「gid → Session」的查询能力，
# 注册回调而不是反向 import（game_api/night_driver 已经依赖 lab_api，反着来会循环）。
lab_api.register_session_lookup(lambda gid: game_api.SESSIONS.get(gid))


# ---------------------------------------------------------------- AI 玩家接模型
# ⚠️ 光在 .env 里填 LLM_MODEL、把 MOCK_LLM 设成 0 是**不够**的：
# `game_api.Session` 默认用 `RuleAgent`（规则拼句子，根本不调模型），
# 所以必须在这里把工厂显式注进去，AI 的发言才会来自真模型。
# 没接模型时给的仍然是 RuleAgent —— 也就是「不接模型」的行为一模一样。
def _build_llm_pool() -> list:
    """
    多厂商/多模型池（H3）。

    语法：`LLM_MODELS=deepseek-flash,deepseek-chat@https://api.deepseek.com/v1`
      · 只写模型名 ⇒ 复用 LLM_BASE_URL / LLM_API_KEY
      · `模型名@base_url` ⇒ 换端点（跨厂商时用这个）
      · **留空 ⇒ 退化成单通道**（= 以前的行为，一字不差）

    ⚠️ 诚实边界：跨厂商**各自的 API key** 现在还没有单独的槽位
       （全都用 LLM_API_KEY）。真要用两家不同 key 的服务，得再加一层配置 ——
       我没有提前造它，因为不知道你要接的第二家是哪个。
    """
    raw = os.environ.get("LLM_MODELS", "").strip()
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if isinstance(LLM, MockLlm) or not items:
        return [(LLM, getattr(LLM, "model", "mock") or "mock")]
    pool = []
    for it in items:
        if "@" in it:
            name, base = it.split("@", 1)
            pool.append((OpenAICompatLlm(model=name.strip(), base_url=base.strip()),
                         name.strip()))
        else:
            pool.append((OpenAICompatLlm(model=it), it))
    return pool


LLM_POOL = _build_llm_pool()


def _model_for_seat(seat: int, pool: list) -> tuple:
    """
    座位 → 池里的哪一项。**按座位取模，确定性的**。

    为什么不用随机：跑完一局要能回答"这个座位是哪个模型在演"——
    随机分配的话日志和复盘都对不上账，混编跑出来的数据就没法归因。
    """
    return pool[int(seat) % len(pool)]


# ---- 第①列席位接管（2026-10-26）：实验室推过来的席位数组，按"第 n 个 AI"映射 ----
# key/model/baseUrl/temperature 全走第①列配置（用户点名）；文件持久，重启后仍生效。
# 第 1 个创建的 AI 用第 1 个席位、依序循环（覆盖应用时清零计数，保证每局从席位A开始）。
_SEAT_OVERRIDES: list = []
_OV_SEQ = {"n": 0}


def _load_agent_overrides() -> list:
    try:
        import lab_api as _lab
        return _lab.get_agents_config()
    except Exception:                                # noqa: BLE001
        return []


def _apply_agent_overrides(agents_list: list) -> None:
    global _SEAT_OVERRIDES
    _SEAT_OVERRIDES = list(agents_list or [])
    _OV_SEQ["n"] = 0                     # 新覆盖生效：下一局从席位A重新开始
    print("[lab] 对局装配改用实验室席位 ×" + str(len(_SEAT_OVERRIDES)), flush=True)


try:
    import lab_api as _lab
    _lab.register_apply_agents(_apply_agent_overrides)
    _SEAT_OVERRIDES = _lab.get_agents_config()
except Exception:                                    # noqa: BLE001
    pass


def _llm_agent_factory(seat: int, rng):
    from agents import LlmAgent, RuleAgent

    # ① 实验室席位覆盖（优先级最高：key/model/baseUrl/temperature 全走第①列；
    #    覆盖存在时即使全局是 MOCK 也给真通道 —— 覆盖本身就是"我明确要真模型"）
    if _SEAT_OVERRIDES:
        o = _SEAT_OVERRIDES[_OV_SEQ["n"] % len(_SEAT_OVERRIDES)]
        _OV_SEQ["n"] += 1
        try:
            from layers_llm import OpenAICompatLlm
            llm = OpenAICompatLlm(
                base_url=o["baseUrl"], api_key=o["apiKey"], model=o["model"],
                turn_timeout=float(o.get("timeout") or 60),
                temperature=float(o.get("temperature") or 0.8),
                thinking="1" if o.get("thinking") == "enabled" else "0")
            ag = LlmAgent(seat, chat_fn=lambda msgs, _llm=llm: _llm.chat_messages(
                msgs, getattr(_llm, "turn_timeout", None)), rng=rng)
            ag.model = o["model"] or o.get("label") or "lab-agent"
            return ag
        except Exception as e:                       # noqa: BLE001
            print("[lab] 席位通道创建失败，退化默认装配：" + repr(e), flush=True)
    # ② 全局 MOCK：没覆盖时保持旧行为（规则发言器）
    if isinstance(LLM, MockLlm):
        return RuleAgent(seat, rng)
    # ③ 默认：env 模型池按座位取模
    llm, name = _model_for_seat(seat, LLM_POOL)
    # ⚠️ `rng` 必须传进去（2026-09-29 修）：LlmAgent 在模型超时/空串时会退化成
    #    RuleAgent 的模板句，而 RuleAgent 不传 rng 就用 `random.Random(seat * 7919)`
    #    —— **固定种子**。后果是：模型每超时一次，同一个座位就吐出**逐字相同**的句子，
    #    桌上听到的观感正是"这个 AI 好像记得上一局"（用户实测反馈）。
    #    传本局的 rng 之后，退化句每局不同；一局之内同一个座位仍然是稳定的。
    ag = LlmAgent(seat, chat_fn=lambda msgs: llm.chat_messages(
        msgs, getattr(llm, "turn_timeout", None)), rng=rng)
    ag.model = name                     # 记到 Agent 上 ⇒ 会话会收进 replay/scoreboard
    return ag


game_api.AI_AGENT_FACTORY = _llm_agent_factory


# ---------------------------------------------------------------- 摘要通道（G2 + H4）
# G2 的上下文压缩要一个"便宜的第二个模型"。**没配就不装**：
# agents 那边会退回硬截断（= 旧行为），所以这个能力是"配了才开"，
# 不会因为忘记配置而悄悄多花钱。
def _wire_summarizer() -> None:
    model = os.environ.get("SUMMARY_MODEL", "").strip()
    # ⚠️ 这几个 print 里**不许出现 GBK 编不了的字符**（比如 ⇒ U+21D2）。
    #    实测教训：start.py 拉起的进程没有 PYTHONIOENCODING=utf-8，
    #    一个 ⇒ 就是 import 期 UnicodeEncodeError ⇒ **整个服务起不来**，
    #    而所有测试都绿（测试进程自己设了 utf-8）。
    if not model:
        print("[llm] 摘要通道未配置（SUMMARY_MODEL 为空），上下文压缩退回硬截断",
              flush=True)
        return
    if isinstance(LLM, MockLlm):
        print("[llm] MOCK_LLM=1，不装摘要通道（mock 局不需要压缩）", flush=True)
        return
    try:
        sm = OpenAICompatLlm(
            base_url=(os.environ.get("SUMMARY_BASE_URL", "").strip() or None),
            api_key=(os.environ.get("SUMMARY_API_KEY", "").strip() or None),
            model=model,
            timeout=float(os.environ.get("SUMMARY_TIMEOUT", "25")),
            turn_timeout=float(os.environ.get("SUMMARY_TIMEOUT", "25")))
        game_api.SUMMARIZER = summarize.make_summarizer(
            lambda msgs, t=None: sm.chat_messages(msgs, t))
        print(f"[llm] 摘要通道已装：{model}（公开发言超 15 条时压缩）", flush=True)
    except Exception as e:                      # noqa: BLE001
        print(f"[llm] 摘要通道装配失败（{type(e).__name__}: {e}），退回硬截断",
              flush=True)


_wire_summarizer()


# ================================================================ 页面与静态资源
# 一律**禁用缓存**。理由：这套是「改完刷新即生效」的开发形态（阶段 A 的 H5），
# 而浏览器对没有校验头的 HTML 会做启发式缓存 —— 改了 play.html 却看到旧行为，
# 排查时根本想不到是缓存（实测就这么白跑过一轮）。
NO_STORE = {"Cache-Control": "no-store, must-revalidate"}


def _page(name: str) -> Response:
    return Response((BASE / name).read_text(encoding="utf-8"),
                    media_type="text/html; charset=utf-8", headers=NO_STORE)


def _asset(name: str, media: str) -> Response:
    return Response((BASE / name).read_text(encoding="utf-8"),
                    media_type=media, headers=NO_STORE)


@app.get("/", include_in_schema=False)
def index() -> Response:
    # 🔴 2026-10-23（用户："这几个页面全删了吧，非游戏画面，好几次拉起你就拉起这个"）：
    #    根路径原来挂着 Day-1 的 P0-talk 探针页（index.html，只验证"说一句话→
    #    听→回→说"那条链）—— 对局早已不需要它，每次开局还要再拼 /play。
    #    探针页已删，根路径现在直接落对局页。
    return RedirectResponse("/play", status_code=302)


@app.get("/rec.js")
def rec_js() -> Response:
    """
    共享录音模块。

    抽出来是因为两个页面都要用（语音探针 + 对局界面），
    复制两份必然漂移 —— 上一轮那个「阈值用了 16k 去比 48k」的 bug
    就是分散实现的典型后果。
    """
    return _asset("rec.js", "application/javascript")


@app.get("/table.css")
def table_css() -> Response:
    """牌桌样式。与 demo/index.html 同源 —— 是从它的 <style> 原样提取的（见文件头）。"""
    return _asset("table.css", "text/css")


@app.get("/table.js")
def table_js() -> Response:
    """牌桌渲染 / 椭圆排座 / 座位特效。同样提取自 demo/index.html，未改动一个字符。"""
    return _asset("table.js", "application/javascript")


@app.get("/speech_ui.js")
def speech_ui_js() -> Response:
    """播报队列 + 夜间门控（2026-10-07 从 `tableui.js` 拆出）。

    ⚠️ 本站的静态资源是**一个文件一条路由**（不是 StaticFiles 挂目录）——
    所以**拆出/新增 js/html 时，必须同时在 server.py 里补一条路由**，
    否则浏览器拿到 404、页面静默少一块（这次差点让整块播报队列消失）。
    """
    return _asset("speech_ui.js", "application/javascript")


@app.get("/judge_voice.js")
def judge_voice_js() -> Response:
    """法官播报本地合成通道（2026-10-22 双通道改造）。

    法官台词（sys）在 speech_ui 播报队列里优先走浏览器 speechSynthesis 本地合成
    （零 HTTP 往返，夜间相位切换不再被云端链路拖住）；AI 发言仍走云端。
    本通道不可用（无 speechSynthesis / 无中文音色）时由播报队列逐句回落云端。
    """
    return _asset("judge_voice.js", "application/javascript")


@app.get("/tableui.js")
def tableui_js() -> Response:
    """大屏 UI 状态机（2026-10-02 从 play.html 抽出）：
    单个大圆 / 座位小圆 + 行动光标 / 票型大字 / 大圆发言模式。
    只被对局页用（demo 不共享），抽出去是 play.html 超行数红线的直接后果。"""
    return _asset("tableui.js", "application/javascript")


@app.get("/play", response_class=HTMLResponse)
def play_page() -> str:
    """
    对局页：**界面形式以 demo/index.html 为绝对参照**，但由真引擎驱动。

    与 `/game` 的分工：`/game` 是早期那版功能型界面（信息密度高、样式朴素）；
    这一页复用 demo 的竖屏列 + 椭圆排座 + 字幕区，目的是「打开就能体验」。
    两者都只通过 `get_view_for(seat)` 取私有信息，隔离没有区别。
    """
    return _page("play.html")


@app.get("/board", response_class=HTMLResponse)
def board_page() -> Response:
    """
    榜单与战报入口（G7）。

    纯展示页：只读 `/api/game/stats`（按档聚合）与 `/api/game/games`（已结束清单）。
    它**不碰任何对局状态**，也不含游戏逻辑 —— 所以它可以随便刷新，
    不存在"刷新把别人那一局搞坏"的可能。
    """
    return _page("board.html")


@app.get("/lab", response_class=HTMLResponse)
def lab_page() -> Response:
    """提示词实验室（2026-10-26）：快照 / 回退 / 重跑 / 线上契约编辑。

    为什么由引擎同源服务：页面要直连 `/api/admin/prompts`（改提示词契约）
    与 `/api/lab/*`（步骤落盘与暂停门）—— 同源就没有 CORS 问题，
    也不用把配置台的鉴权面暴露给别的域。
    """
    return _page("promptlab.html")


DS_REALTIME_URL = os.environ.get("DASHSCOPE_REALTIME_URL",
                                 "wss://dashscope.aliyuncs.com/api-ws/v1/realtime")
DS_REALTIME_MODEL = os.environ.get("DASHSCOPE_REALTIME_MODEL", "qwen3-asr-flash-realtime")


@app.websocket("/ws/asr")
async def ws_asr(browser: WebSocket, lang: str = "zh") -> None:
    """
    **流式 ASR 中继**（2026-09-19）：浏览器 ⇄ 我们 ⇄ 百炼 Qwen-ASR-Realtime。

    为什么必须中继：key 只能待在服务端。让浏览器直连就得把 key 塞进页面 ——
    谁打开页面谁就能拿走（本项目其它地方也是这个原则：`.env` 里的东西不下发）。

    ## 对浏览器这一侧（刻意做得比上游简单）

        浏览器 → 我们：二进制帧 = **PCM16 小端 16k 单声道**；文本帧 = {"type":"finish"}
        我们 → 浏览器：{"type":"partial","text":…,"stash":…}    ← 边说边出字
                       {"type":"final","transcript":…}           ← 一句的权威结果
                       {"type":"speech_started" / "speech_stopped"}
                       {"type":"finished"}                       ← 上游收尾了，可以断
                       {"type":"error","message":…}

    ## 两个实测得来的坑（别按直觉改）

    1. **上游 VAD 会自动断句 ⇒ 一次会话会有多个 final**。实测 5.2 秒的片段被切成
       两段（"我是三号玩家。" / "昨晚我没有看…"）。所以调用方要把 **所有** final
       累积起来 —— 只取最后一个会丢半句，而且丢得毫无提示。
    2. **增量分两段**：`text` 是"已确认的前缀"，`stash` 是"仍在改的草稿"。
       要显示"当前最完整的一句"必须 `text + stash`（只显示 text 会一直像是没识别出来）。

    ## 上游事件名（照文档 + 实测核对）
       session.created / session.updated / input_audio_buffer.speech_started /
       speech_stopped / committed / conversation.item.created /
       conversation.item.input_audio_transcription.text / .completed / .failed /
       session.finished / error
    """
    await browser.accept()
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        await browser.send_json({"type": "error",
                                 "message": "未配置 DASHSCOPE_API_KEY（云端流式 ASR 需要它）"})
        await browser.close()
        return

    try:
        import websockets  # noqa: PLC0415  延迟导入：只有走流式这条路才需要
    except Exception as e:  # noqa: BLE001
        await browser.send_json({"type": "error",
                                 "message": f"服务端缺少 websockets 库：{e}"})
        await browser.close()
        return

    url = f"{DS_REALTIME_URL}?model={DS_REALTIME_MODEL}"
    up = None
    try:
        up = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {key}", "X-DashScope-DataInspection": "enable"},
            open_timeout=15, max_size=None)
        await up.send(json.dumps({
            "event_id": "s1", "type": "session.update",
            "session": {
                "input_audio_format": "pcm",
                "sample_rate": 16000,
                "input_audio_transcription": {"language": lang or "zh"},
                # VAD 模式（服务端自动断句）。阈值/静音时长照文档推荐值：
                # threshold 0.0（推荐）、silence_duration_ms 400（比默认 800 更跟手）
                "turn_detection": {"type": "server_vad", "threshold": 0.0,
                                   "silence_duration_ms": 400},
            }}))

        async def up_to_browser() -> None:
            """上游 → 浏览器：只转发调用方需要的几类事件，其余丢掉。"""
            async for raw in up:
                try:
                    ev = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                t = ev.get("type", "")
                if t == "conversation.item.input_audio_transcription.text":
                    await browser.send_json({"type": "partial", "text": ev.get("text") or "",
                                             "stash": ev.get("stash") or ""})
                elif t == "conversation.item.input_audio_transcription.completed":
                    await browser.send_json({"type": "final",
                                             "transcript": ev.get("transcript") or "",
                                             "language": ev.get("language"),
                                             "emotion": ev.get("emotion")})
                elif t in ("input_audio_buffer.speech_started",
                           "input_audio_buffer.speech_stopped"):
                    await browser.send_json({"type": t.split(".")[-1]})
                elif t == "conversation.item.input_audio_transcription.failed":
                    await browser.send_json({"type": "error",
                                             "message": str(ev.get("error"))})
                elif t == "error":
                    await browser.send_json({"type": "error",
                                             "message": str(ev.get("error"))})
                elif t == "session.finished":
                    state["finished"] = True        # 上游给了正式收尾
                    await browser.send_json({"type": "finished"})
                    return
                elif t == "session.created":
                    # 会话建好了 ⇒ 告诉浏览器"可以开始喂音频"，省得它自己猜时机
                    state["ready"] = True
                    await browser.send_json({"type": "ready",
                                             "model": (ev.get("session") or {}).get("model")})

        # ⚠️ 2026-09-30 新增 ready / finished：**用来区分"上游提前死了"和"正常收尾"**。
        #    没有它俩就没法判断一次"干净关闭"到底是成功还是失败（见下面那段）。
        state = {"finish": False, "ready": False, "finished": False}

        async def browser_to_up() -> None:
            """浏览器 → 上游：二进制帧当 PCM，文本帧当控制。"""
            while True:
                msg = await browser.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
                data = msg.get("bytes")
                text = msg.get("text")
                if data:
                    await up.send(json.dumps({
                        "event_id": "a", "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(data).decode()}))
                elif text:
                    try:
                        ctl = json.loads(text)
                    except Exception:  # noqa: BLE001
                        continue
                    if ctl.get("type") == "finish":
                        # ⚠️ 收尾**必须发 session.finish**：不发的话最后一句永远等不到 final
                        #    （上游只在收到它之后才把缓冲区里剩下的音频识别完）。
                        state["finish"] = True
                        await up.send(json.dumps({"event_id": "f1",
                                                  "type": "session.finish"}))
                        return
                    if ctl.get("type") == "cancel":
                        return

        t1 = asyncio.create_task(browser_to_up())
        t2 = asyncio.create_task(up_to_browser())
        try:
            done, _ = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            # 🔴 收尾顺序（实测踩过）：浏览器说 finish 之后**不能立刻把上游掐掉** ——
            #    上游还欠着「最后一句的 final + session.finished」，掐早了就是
            #    "最后半句永远收不到 finished"（第一版就是这么错的，冒烟里红了一条）。
            #    取消/直接断线才不需要等。
            if t1 in done and t2 not in done and state["finish"]:
                try:
                    await asyncio.wait_for(t2, timeout=8)
                except asyncio.TimeoutError:
                    pass
            # 🔴 2026-09-30：**上游提前结束，也必须让浏览器知道**（用户实测报的
            #    「麦没法用、诊断面板却显示正常」就是这一段缺了）。
            #    实测（阿里云账号欠费）：上游既不报错、也不回任何结果，**直接正常关闭**；
            #    于是我们这边也把浏览器连接"干净地"关掉 —— 客户端只看到 1000 OK，
            #    界面上既没有字也没有提示，面板那行"最近错误"是空的。
            #    ⇒ 两端都把它当成了"说完了"，而用户看到的是"没反应"。
            #    **静默降级比失败危险**：失败会让人去看，静默降级不会。
            if t2 in done and t1 not in done and not state["finished"]:
                if not state["ready"]:
                    # 连 session.created 都没等到：上游根本没把会话建起来
                    await browser.send_json({"type": "error", "message":
                                             "识别通道没能建立会话就结束了（上游没回 session.created）"
                                             "—— 常见原因：账号欠费 / 未开通实时语音识别 / 额度用尽"})
                else:
                    # 会话建起来了、只是没给正式收尾：按"正常结束"收尾，
                    # 免得把"用户只是没说话"误报成故障。
                    await browser.send_json({"type": "finished"})
        finally:
            for t in (t1, t2):
                t.cancel()
    except Exception as e:  # noqa: BLE001
        # 任何失败都要**告诉浏览器**（否则前端表现就是"按着说话没反应"）
        try:
            await browser.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if up is not None:
                await up.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass


DS_TTS_REALTIME_MODEL = os.environ.get("DASHSCOPE_TTS_REALTIME_MODEL",
                                       "qwen3-tts-flash-realtime")


# 上游 TTS 连接池 —— **做过，又撤了**（2026-09-19），结论留一句免得有人再试：
# 热连接首片 306ms vs 每句重建 1000ms（实测），但复用连接有"state=OPEN 却
# 一个分片都不回来"的静默失败，且无法可靠检测 ⇒ 对局里某个 AI 忽然没声。
# 取舍：用 700ms 换"永不丢音"。（websockets 17 的 .state 判活、session.update
# 单次限制等实现细节见 HANDOFF §10.7 与 git 历史。）


@app.websocket("/ws/tts")
async def ws_tts(browser: WebSocket, text: str = "", voice: str = "") -> None:
    """
    **流式 TTS 中继**（2026-09-19）：浏览器 ⇄ 我们 ⇄ 百炼 Qwen-TTS-Realtime。

    句子走 URL 参数（`?text=…&voice=…`）：这一侧的调用方**本来就知道要念哪一句**
    （就是时间线上那一行），所以不需要双向协议 —— 中继是单向下行的管道，简单得多。

    ## 对浏览器这一侧

        → 先发一条 JSON：{"type":"start","format":"pcm","sample_rate":24000,"voice":…}
        → 然后**二进制帧**：PCM16 小端 24k 单声道（已从 base64 解出来，省 33% 体积）
        → 结束：{"type":"done"}；出错：{"type":"error","message":…}

    ## 为什么值得（实测 2026-09-19，同一句 4.56 秒的话）

        非流式 /api/tts：合成完（1.05 秒）才拿到第一个字节 ⇒ 才开始出声
        流式 /ws/tts：  **首片音频 471 毫秒**就出声，合成在后台继续（整句 1.15 秒完成）

    ## 上游协议（都实测核对过，别照 OpenAI Realtime 的样子改）

        session.update(voice/response_format/sample_rate/mode)
        input_text_buffer.append(text) → input_text_buffer.commit
        ← session.updated / input_text_buffer.committed / response.created
          / response.audio.delta(base64 PCM) × N / response.audio.done
        session.finish ← session.finished
    ⚠️ 是 `input_text_buffer.*`，**不是** `conversation.item.create` + `response.create`。
    """
    await browser.accept()
    # 🔴 2026-09-30：**这条通道只能由"流式合成"的实现来服务**。
    #    它是中继到云端 realtime TTS 的，**不经过 `TTS=` 那个开关** ——
    #    于是 `TTS=sapi`（本地、免费）时，播报会**悄悄继续走云端**：
    #    用户以为切到了本地，其实还在花钱，而且**根本听不出差别**（两条都能出声）。
    #    ⇒ 宁可当场说清，让客户端回落到 `/api/tts`
    #      （那条**确实**用的是配置里的实现，见 §10.3 的 `/api/tts` 那条契约）。
    #    ⚠️ 代价：`_smoke-tts-stream.py`(7 条) 因此**只能在 `TTS=qwen` 下跑** ——
    #       它测的就是这条云端通道，配置成非流式实现时它必然红，这不是 bug。
    if os.environ.get("TTS", "sapi").strip().lower() not in (
            "qwen", "dashscope", "bailian", "aliyun"):
        await browser.send_json({"type": "error", "message":
                                 "当前 TTS 不是流式实现（TTS="
                                 + os.environ.get("TTS", "sapi")
                                 + "），本通道不可用，请回落 /api/tts"})
        await browser.close()
        return
    text = (text or "").strip()
    voice = (voice or os.environ.get("DASHSCOPE_TTS_VOICE", "Ethan")).strip()
    key = (os.environ.get("DASHSCOPE_TTS_API_KEY")
           or os.environ.get("DASHSCOPE_API_KEY", ""))
    if not key:
        await browser.send_json({"type": "error",
                                 "message": "未配置 DASHSCOPE_TTS_API_KEY / DASHSCOPE_API_KEY"})
        await browser.close()
        return
    if not text:
        await browser.send_json({"type": "done"})
        await browser.close()
        return
    if len(text) > 600:
        await browser.send_json({"type": "error",
                                 "message": f"这一段太长（{len(text)} 字），请拆句后重试"})
        await browser.close()
        return
    try:
        import websockets  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        await browser.send_json({"type": "error", "message": f"服务端缺少 websockets：{e}"})
        await browser.close()
        return

    up = None
    try:
        # 每句一条新连接（**不做连接复用**，理由见文件上方那段"做过又撤了"）
        up = await websockets.connect(
            f"{DS_REALTIME_URL}?model={DS_TTS_REALTIME_MODEL}",
            additional_headers={"Authorization": f"Bearer {key}"},
            open_timeout=15, max_size=None)
        await up.send(json.dumps({
            "event_id": "s1", "type": "session.update",
            "session": {"voice": voice, "response_format": "pcm",
                        "sample_rate": 24000, "mode": "server_commit"}}))
        await up.send(json.dumps({"event_id": "t1", "type": "input_text_buffer.append",
                                  "text": text}))
        await up.send(json.dumps({"event_id": "t2", "type": "input_text_buffer.commit"}))
        await browser.send_json({"type": "start", "format": "pcm",
                                 "sample_rate": 24000, "voice": voice})

        # ⚠️ 只有见到**本轮的** response.created 之后才认 audio.delta / audio.done：
        #    复用连接时，上一轮的尾巴（response.done 之类）可能还在缓冲里，
        #    不拦的话会立刻收到一个"上一轮的结束"，这一句就静默没声音了。
        seen_created = False
        async for raw in up:
            try:
                ev = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            t = ev.get("type", "")
            if t == "response.created":
                seen_created = True
                continue
            if t == "response.audio.delta":
                if not seen_created:
                    continue
                payload = base64.b64decode(ev.get("delta") or ev.get("audio") or "")
                if payload:
                    await browser.send_bytes(payload)      # 给裸 PCM，前端不用解 base64
            elif t in ("response.audio.done", "response.done"):
                if not seen_created:
                    continue                              # 上一轮的残留，别当成本轮结束
                await browser.send_json({"type": "done"})
                break
            elif t == "error":
                await browser.send_json({"type": "error", "message": str(ev.get("error"))})
                break
        # 收尾：告诉上游这条会话结束了（它在 session.finished 之后才释放资源）
        try:
            await up.send(json.dumps({"event_id": "f1", "type": "session.finish"}))
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        try:
            await browser.send_json({"type": "error", "message": f"{type(e).__name__}: {e}"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if up is not None:
                await up.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass


@app.get("/api/tts")
async def tts(text: str, rate: int = 0, volume: int = 100, voice: str = "") -> Response:
    """
    合成一句话，返回 WAV。

    对局界面用它播报：法官台词（rate 偏慢、稳重）与 AI 发言（rate 正常）。

    `voice`：**云端 TTS 的音色**（2026-09-19 起可用）—— 法官一个音色、
    每个 AI 座位各自一个，这才让"9 种人格"在**听感上**也能区分开。
    本地 SAPI 只有一个中文音色，传了它会被 `**kw` 吞掉（不报错、静默忽略）——
    所以前端可以照传，切到云端就自动生效，不用改前端。
    """
    text = (text or "").strip()
    if not text:
        return Response(content=b"", media_type="audio/wav")
    try:
        out = await run_in_threadpool(TTS.synth, text, rate, volume,
                                      voice=(voice or None))
    except NotImplementedError as e:
        return JSONResponse({"error": str(e), "kind": "not_implemented"}, status_code=501)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "kind": "runtime"},
                            status_code=500)
    return Response(content=out, media_type="audio/wav")


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({
        "mock": MOCK,
        "layers": {
            "asr": {"impl": type(ASR).__name__, "mocked": isinstance(ASR, MockAsr)},
            "llm": {"impl": type(LLM).__name__, "mocked": isinstance(LLM, MockLlm)},
            "tts": {"impl": type(TTS).__name__, "mocked": isinstance(TTS, MockTts)},
        },
        "asr": type(ASR).__name__,
        "llm": type(LLM).__name__,
        "tts": type(TTS).__name__,
    })


@app.get("/api/net")
def net() -> JSONResponse:
    """
    "手机该用哪个地址打开" —— 页面的诊断面板要显示它。

    为什么必须在服务端算：页面只知道**自己被谁访问**，不知道该从哪个地址去访问 ——
    它在电脑上打开时看到的是 localhost:8000，那个地址在手机上指向手机自己。

    ⚠️ 手机必须走 **https**：`http://<局域网IP>:8000` **不是安全上下文**，
    浏览器会直接把麦克风接口收走（连授权弹窗都不给，看着就像"页面坏了"）。
    localhost 是唯一豁免的 http 地址 —— 但手机上要走到本机的 localhost，
    得先做端口转发（adb reverse），比直接用局域网 https 麻烦。
    """
    import socket as _socket

    ips: set = set()
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))       # 不真发包：只为拿到"出口网卡"的地址
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:                     # noqa: BLE001  没有默认路由时会失败，正常
        pass
    try:
        for info in _socket.getaddrinfo(_socket.gethostname(), None, _socket.AF_INET):
            ips.add(info[4][0])
    except Exception:                     # noqa: BLE001
        pass
    lan = sorted(i for i in ips
                 if not i.startswith("127.") and not i.startswith("169.254."))
    port = os.environ.get("HTTPS_PORT", "8443")
    return JSONResponse({
        "phones": [f"https://{ip}:{port}/play" for ip in lan],
        "note": "手机上必须用 https 打开（http 的局域网地址不是安全上下文，"
                "麦克风会被浏览器直接禁掉）；证书是自己签的，浏览器会警告，"
                "选「继续访问 / 高级 → 继续」即可。",
    })


@app.post("/api/utter")
async def utter(request: Request, only: str = "") -> JSONResponse:
    """
    接收 16k 单声道 WAV，返回 {text, reply, audio_b64, timings, input_audio}。

    `only="asr"` 时**只跑 ASR**，跳过 LLM 与 TTS。

    为什么需要这一档：对局界面（`play.html`）的录音只要转写结果 ——
    识别完直接提交。它先前借用的这条链路会顺带跑一次 LLM + 一次 TTS，
    然后把结果整个丢掉。LLM 现在是 mock 所以看不出来；接真模型后，
    每按一次「按住说话」就会白花一次模型调用 + 一次语音合成。
    （原探针页 index.html 走完整链路不带这个参数——该页已于 2026-10-23 删除。）
    """
    audio = await request.body()
    rate = int(request.headers.get("X-Sample-Rate", "16000"))
    mime = request.headers.get("Content-Type", "audio/wav")

    # 无论后面成不成功，先把「收到了多少声音」算出来。
    # 这样「转写为空」时也能立刻分清是拾音问题还是模型问题。
    input_audio = analyze_input(audio)

    # 留一份原始录音，方便事后离线分析（只有最近一次，探针阶段够用）
    if os.environ.get("SAVE_UPLOADS", "1") == "1":
        try:
            (BASE / "_last_upload.wav").write_bytes(audio)
        except Exception:  # noqa: BLE001
            pass

    asr_only = only.strip().lower() == "asr"
    reply = ""
    out = b""

    t = Timings()
    t0 = time.perf_counter()

    try:
        s = time.perf_counter()
        # 三层都是同步阻塞调用（本地模型尤其慢），必须丢到线程池，
        # 否则会卡住事件循环，并发时互相拖死。
        text = await run_in_threadpool(ASR.transcribe, audio, mime, rate)
        t.asr_ms = (time.perf_counter() - s) * 1000

        if not asr_only:
            s = time.perf_counter()
            reply = await run_in_threadpool(LLM.chat, text)
            t.llm_ms = (time.perf_counter() - s) * 1000

            s = time.perf_counter()
            out = await run_in_threadpool(TTS.synth, reply)
            t.tts_ms = (time.perf_counter() - s) * 1000
    except NotImplementedError as e:
        return JSONResponse({"error": str(e), "kind": "not_implemented",
                             "input_audio": input_audio}, status_code=501)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}", "kind": "runtime",
                             "input_audio": input_audio}, status_code=500)

    t.total_ms = (time.perf_counter() - t0) * 1000

    try:
        with wave.open(io.BytesIO(out), "rb") as w:
            audio_seconds = w.getnframes() / w.getframerate()
    except Exception:  # noqa: BLE001
        audio_seconds = 0.0

    asr_mocked = isinstance(ASR, MockAsr)

    return JSONResponse({
        "text": text,
        "reply": reply,
        "audio_b64": base64.b64encode(out).decode("ascii"),
        "audio_seconds": round(audio_seconds, 2),
        "in_bytes": len(audio),
        "input_audio": input_audio,
        "asr_mocked": asr_mocked,
        # 让调用方一眼看出这次跑的是完整链路还是只跑了 ASR
        "only": "asr" if asr_only else "full",
        # 转写为空时给出最可能的原因，免得对着空白界面猜
        "note": ("当前 ASR 是模拟的（MockAsr），这段文字是固定文案，"
                 "不代表它听懂了你")
                if asr_mocked else ("" if text else "没有转出文字：先看下面的响度与信噪比"),
        "timings": {
            "asr_ms": round(t.asr_ms),
            "llm_ms": round(t.llm_ms),
            "tts_ms": round(t.tts_ms),
            "total_ms": round(t.total_ms),
        },
    })


def _admin_snapshot() -> dict:
    """当前装配（配置台用来显示"现在到底在用什么"）。"""
    return {"asr": type(ASR).__name__, "llm": type(LLM).__name__, "tts": type(TTS).__name__,
            "asr_mocked": isinstance(ASR, MockAsr),
            "llm_mocked": isinstance(LLM, MockLlm),
            "tts_mocked": isinstance(TTS, MockTts)}


def reload_pipeline() -> dict:
    """按**当前** os.environ 重新装配三层，并把 LLM 侧重新接进 agents/game_api。

    能热换的前提都是既有的结构（不是为它新造的）：
      · ASR / LLM / TTS 是模块级全局 ⇒ 重新赋值就生效；
      · `_llm_agent_factory` 是**调用时**才读 LLM / LLM_POOL 的 ⇒ 换掉这两个全局即可，
        **不需要**重设 `game_api.AI_AGENT_FACTORY`；
      · 摘要通道本来就写成了函数 `_wire_summarizer()` ⇒ 直接再调一次。
    ⚠️ 进行中的对局不会换（Agent 是开局时建的）—— 下一局才用新配置。
    """
    global ASR, LLM, TTS, LLM_POOL
    ASR, LLM, TTS = build_pipeline()
    LLM_POOL = [] if isinstance(LLM, MockLlm) else _build_llm_pool()
    game_api.SUMMARIZER = None                               # 先清掉，再按新配置装
    _wire_summarizer()
    return _admin_snapshot()


# 配置台：/admin（页面）+ /api/admin/*（改 key / 通道 / 音色 / prompt）。
# 三件事**注入**进去，不让 admin_api 反向 import 这个文件（依赖图不许成环）。
admin_api.register(app, reload_fn=reload_pipeline, snapshot_fn=_admin_snapshot,
                   page_fn=lambda: _page("admin.html"))
