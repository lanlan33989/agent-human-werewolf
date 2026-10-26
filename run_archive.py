# -*- coding: utf-8 -*-
"""
对局自动存档（2026-10-26，用户要求）：每跑完一局，自动在 runs/ 下生成一个目录，
之后不看代码、不重跑，只看这个目录就能算出度量指标、对比两个版本有没有变好。

目录结构：runs/<日期时间>_<局号>_v<git短哈希>/
    meta.json        开局即写：局号/种子/版本/座位表/模型配置（复现对比的锚点）
    trace.jsonl      一次模型调用追加一行（prompt 与 raw_output 全文，不截断）
    timeline.jsonl   局末一次性写全（公开动作流，逐行 JSON）
    result.json      局末写：胜负/票型/死亡/夜间记录/metrics/存档期间的全部错误

四条硬要求对应的实现：
    1. visible_info / raw_output 记全文 —— 不 strip、不截断、不摘要
    2. prompt_system / prompt_user 记**实际渲染后**的文本（msgs 里第一条
       system / 第一条 user 的 content 原文）
    3. few_shot_ids 保留字段 —— 当前代码**没有 few-shot 机制**（agents.py 无
       示例注入），如实记 null，不造假；将来上了 few-shot 在同一字段补
    4. model / temperature / seed / thinking 全记 —— 来自 layers_llm 实际
       发请求时用的值与 Session 的 rng 种子；max_tokens 当前 payload 未设，
       如实记 null

五个工程约束：
    1. 追加写永不覆盖 —— 目录名带秒级时间戳 + 局号，每次开局新目录
    2. 记录失败不中断对局 —— 本模块所有对外函数全部 try/except，错误攒进
       Archive.errs，局末写进 result.json；runs/ 不可写时对局照常跑完
    3. 局末强制 flush + close —— 每次写入都是 open/append/close，天然满足
    4. runs/ 已进 .gitignore；脱敏样例与格式说明在 runs-sample/
    5. 温度/seed 从配置读 —— temperature 取自 layers_llm 实例实际值，
       seed 取自 Session 的 rng 种子；本模块零硬编码

诚实边界（给交接人）：
    · trace 行是**模型调用粒度**：parsed / postprocess / engine_verdict 当前
      记 null（决策定型点在 agents 的调用方引擎里，未逐行打通）；
      "这次模型说了什么 → 桌上最终采纳了什么"请用 raw_output + timeline.jsonl
      人工对齐。五个指标（发言合规/投票有据/自相矛盾/信息引用/越权）都能只凭
      visible_info + raw_output 人工判读 —— 这正是验收标准 1 的口径。
    · 当前归因假设"同一时刻只有一局在推进"（与 lab_api._ctx 的既有约定一致）；
      将来若要多局并发存档，begin/trace 需要带上 per-session 上下文。
    · first_token_ms 恒为 null —— 调用是非流式的，没有首 token 时刻可记。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(_HERE, "runs")

# ---------------------------------------------------------------- 版本短哈希
_ver_cache: Optional[str] = None


def version_tag() -> str:
    """git 短哈希（在 h5 目录跑一次并缓存）。拿不到就 nogit —— 不编造。"""
    global _ver_cache
    if _ver_cache is not None:
        return _ver_cache
    v = "nogit"
    try:
        v = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_HERE,
            capture_output=True, text=True, timeout=5).stdout.strip() or "nogit"
    except Exception:                                    # noqa: BLE001
        pass
    _ver_cache = v
    return v


# ---------------------------------------------------------------- 线程局部
# 模型侧信息由 layers_llm 在真实请求返回时写入，agents 的调用栈（同线程）取走。
# 取走即清 ⇒ 串行调用不会串味；线程局部 ⇒ 后台预跑池并发也不会互踩。
_tls = threading.local()
_TLS_ATTR = "model_info"
_TLS_CTX = "ask_ctx"


def note_model(model: str, temperature: Any, thinking: Any,
               ms: int, usage: Optional[dict]) -> None:
    """layers_llm 每次真实调用后写：本次请求实际用的模型参数与计量。"""
    try:
        setattr(_tls, _TLS_ATTR, {
            "model": model, "temperature": temperature, "thinking": thinking,
            "ms": int(ms), "usage": usage or {}})
    except Exception:                                    # noqa: BLE001
        pass


def _pop_model() -> dict:
    try:
        mi = getattr(_tls, _TLS_ATTR, None) or {}
    except Exception:                                    # noqa: BLE001
        mi = {}
    try:
        setattr(_tls, _TLS_ATTR, None)
    except Exception:                                    # noqa: BLE001
        pass
    return mi


def set_ask_ctx(kind: str, view: dict) -> None:
    """agents._build 末尾调：本线程下一次 _ask 属于哪个决策（kind + view 全文）。"""
    try:
        setattr(_tls, _TLS_CTX, {"kind": kind, "view": view})
    except Exception:                                    # noqa: BLE001
        pass


def _pop_ctx() -> dict:
    try:
        c = getattr(_tls, _TLS_CTX, None) or {}
    except Exception:                                    # noqa: BLE001
        c = {}
    try:
        setattr(_tls, _TLS_CTX, None)
    except Exception:                                    # noqa: BLE001
        pass
    return c


# ---------------------------------------------------------------- 活跃存档
class Archive:
    """一局的存档器。所有方法都吞异常 —— 存档永远不能弄坏对局。"""

    def __init__(self, gid: str) -> None:
        self.gid = gid
        self.dir = os.path.join(
            RUNS_DIR,
            time.strftime("%Y%m%d-%H%M%S") + "_" + gid + "_v" + version_tag())
        self.errs: List[str] = []
        self.traced = 0
        self.ok = False

    def _err(self, where: str, e: Exception) -> None:
        self.errs.append(where + ": " + type(e).__name__ + ": " + str(e)[:200])

    def _append(self, name: str, obj: dict) -> bool:
        """追加一行 JSONL。每次 open/append/close：崩溃不丢已写的行。"""
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(os.path.join(self.dir, name), "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            return True
        except Exception as e:                           # noqa: BLE001
            self._err("append:" + name, e)
            return False

    # ---- 开局 ----
    def begin(self, sess: Any) -> None:
        seats = []
        try:
            for p in sess.game.players:
                seats.append({"seat": p.seat, "kind": p.kind,
                              "display": getattr(p, "display", ""),
                              "model": (sess.models or {}).get(p.seat)
                              if p.kind == "ai" else None})
        except Exception as e:                           # noqa: BLE001
            self._err("meta:seats", e)
        meta = {
            "gid": self.gid,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version": version_tag(),
            "seed": None,
            "level": getattr(sess, "level", None),
            "humans": sum(1 for s in seats if s["kind"] == "human"),
            "ais": sum(1 for s in seats if s["kind"] == "ai"),
            "seats": seats,
            "personality": getattr(sess, "personality_mode", None),
            "mock": os.environ.get("MOCK_LLM", "") == "1",
            "llm_temperature_env": os.environ.get("LLM_TEMPERATURE", ""),
            "llm_thinking_env": os.environ.get("LLM_THINKING", ""),
            "llm_model_env": os.environ.get("LLM_MODEL", ""),
            "few_shot": None,
        }
        # seed 的单一真相在 Session 上（rng 从它来）；取不到不编造
        try:
            meta["seed"] = sess.seed
        except Exception:                                # noqa: BLE001
            pass
        self.ok = self._append("meta.json", meta)

    # ---- 一次模型调用一行 ----
    def trace(self, agent: Any, messages: List[dict], raw: str,
              err: Optional[Exception]) -> None:
        try:
            ctx = _pop_ctx()
            view = ctx.get("view") or {}
            mi = _pop_model()
            usage = mi.get("usage") or {}
            sys_t, usr_t = "", ""
            for m in messages or []:
                if not sys_t and m.get("role") == "system":
                    sys_t = str(m.get("content") or "")
                if not usr_t and m.get("role") == "user":
                    usr_t = str(m.get("content") or "")
                if sys_t and usr_t:
                    break
            legal = None
            try:
                legal = [p["seat"] for p in view.get("players", [])
                         if p.get("alive") and p.get("seat") != agent.seat]
            except Exception:                            # noqa: BLE001
                pass
            row = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "round": view.get("round"), "phase": view.get("phase"),
                "seat": getattr(agent, "seat", None),
                "role": view.get("role"),
                "action": ctx.get("kind"),
                "legal_targets": legal,
                "visible_info": view,                    # 全文，不截断
                "prompt_system": sys_t, "prompt_user": usr_t,
                "few_shot_ids": None,                    # 无 few-shot 机制，如实 null
                "model": mi.get("model"),
                "model_version": None,                   # 无独立模型版本概念
                "temperature": mi.get("temperature"),
                "seed": _active_seed.get(self.gid),
                "thinking": mi.get("thinking"),
                "max_tokens": None,                      # payload 未设，如实 null
                "raw_output": raw,                       # 原样，不 strip 不摘要
                "parsed": None,                          # 见模块头「诚实边界」
                "postprocess": [],
                "latency": {"first_token_ms": None,        # 非流式，没有首 token
                            "total_ms": mi.get("ms")},
                "tokens": {"prompt": usage.get("prompt_tokens"),
                           "completion": usage.get("completion_tokens")},
                "engine_verdict": "error" if err is not None else None,
                "reject_reason": (type(err).__name__ + ": " + str(err))[:200]
                if err is not None else None,
            }
            if self._append("trace.jsonl", row):
                self.traced += 1
        except Exception as e:                           # noqa: BLE001
            self._err("trace", e)

    # ---- 局末 ----
    def finalize(self, sess: Any, replay: dict) -> None:
        try:
            # timeline.jsonl：公开动作流逐行。round 从 sys 播报的「第X夜/第X天」
            # 推进补齐（条目本身不带 round）；解析不出的记 null，不硬猜。
            rnd = None
            seq = 0
            for it in (getattr(sess, "timeline", None) or []):
                t = str(it.get("text") or "")
                m = re.search(r"第\s*(\d+)\s*夜", t)
                if m:
                    rnd = int(m.group(1))
                else:
                    m2 = re.search(r"第\s*(\d+)\s*天", t)
                    if m2:
                        rnd = int(m2.group(1))
                seat = it.get("seat")
                kinds = _active_kinds.get(self.gid) or {}
                self._append("timeline.jsonl", {
                    "round": rnd, "seq": seq,
                    "seat": seat,
                    "is_ai": kinds.get(seat) == "ai" if seat is not None else None,
                    "type": it.get("type"),
                    "text": t,
                    "target": it.get("target"),
                    "claimed_role": None,   # 发言里自称什么身份属人工判读口径
                })
                seq += 1
        except Exception as e:                           # noqa: BLE001
            self._err("timeline", e)
        try:
            g = sess.game
            result = {
                "gid": self.gid, "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "winner": getattr(g, "winner", None),
                "rounds": getattr(g, "day", None),
                "seed": getattr(g, "seed", None),
                "version": version_tag(),
                "deaths": replay.get("deaths"),
                "votes": replay.get("votes"),
                "nights": replay.get("nights"),
                "metrics": replay.get("metrics"),
                "trace_calls": self.traced,
                "archive_errors": self.errs,
            }
            self._append("result.json", result)
        except Exception as e:                           # noqa: BLE001
            self._err("result", e)
        print("[runs] 已存档 " + self.dir
              + "（trace " + str(self.traced) + " 行"
              + ("，错误 " + str(len(self.errs)) if self.errs else "") + "）",
              flush=True)


# 活跃局的种子 / 座位类型表（begin 填、finalize 清）
_active_seed: Dict[str, Any] = {}
_active_kinds: Dict[str, Dict[int, str]] = {}
_ACTIVE: Dict[str, Archive] = {}


# ---------------------------------------------------------------- 对外钩子
def begin(sess: Any) -> None:
    """开局建档。game_api.new_game 调。失败只打日志，绝不影响开局。"""
    try:
        gid = sess.id
        arch = Archive(gid)
        arch.begin(sess)
        _ACTIVE[gid] = arch
        _active_seed[gid] = getattr(sess, "seed", None)
        _active_kinds[gid] = {p.seat: p.kind for p in sess.game.players}
        if arch.ok:
            print("[runs] 建档 " + arch.dir, flush=True)
        else:
            print("[runs] 建档失败（对局继续）：" + "; ".join(arch.errs[:2]),
                  flush=True)
    except Exception as e:                               # noqa: BLE001
        print("[runs] 建档失败（对局继续）：" + repr(e), flush=True)


def on_ask(agent: Any, messages: List[dict], raw: str) -> None:
    """agents._ask 成功返回后调：写 trace 行。"""
    try:
        gid = getattr(agent, "gid", None)
        arch = _ACTIVE.get(gid) if gid else None
        if arch is not None:
            arch.trace(agent, messages, raw, None)
    except Exception:                                    # noqa: BLE001
        pass


def on_ask_error(agent: Any, messages: List[dict], err: Exception) -> None:
    """agents._ask 抛异常后调：失败调用也留一行（raw 空、verdict=error）。"""
    try:
        gid = getattr(agent, "gid", None)
        arch = _ACTIVE.get(gid) if gid else None
        if arch is not None:
            arch.trace(agent, messages, "", err)
    except Exception:                                    # noqa: BLE001
        pass


def on_drive_end(sess: Any) -> None:
    """game_api.Session.drive 末尾调：检测到终局就落 result/timeline。"""
    try:
        arch = _ACTIVE.get(sess.id)
        if arch is None:
            return
        if getattr(sess, "pending", "") != "ended":
            return
        _ACTIVE.pop(sess.id, None)
        _active_seed.pop(sess.id, None)
        _active_kinds.pop(sess.id, None)
        arch.finalize(sess, sess.replay())
    except Exception as e:                               # noqa: BLE001
        print("[runs] 局末落盘失败（对局已正常结束）：" + repr(e), flush=True)


def on_evict(sess: Any) -> None:
    """会话被淘汰（MAX_SESSIONS 上限）时兜底落盘，不让半截档案永远悬着。"""
    try:
        arch = _ACTIVE.get(sess.id)
        if arch is not None and getattr(sess, "pending", "") == "ended":
            on_drive_end(sess)
        elif arch is not None:
            _ACTIVE.pop(sess.id, None)
            _active_seed.pop(sess.id, None)
            _active_kinds.pop(sess.id, None)
            arch.finalize(sess, sess.replay())
    except Exception:                                    # noqa: BLE001
        pass
