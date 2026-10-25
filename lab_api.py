# -*- coding: utf-8 -*-
"""提示词实验室对接层（lab_api）。

这个模块**不碰任何规则与 AI 逻辑**，只做三件事：

  1) 落盘：每次模型调用写一行 JSONL
     —— 提示词快照（完整 messages）+ 模型原始输出 + 耗时 + 成功与否。
     这是实验室"回放 / 回退 / 重跑 / 归因"唯一需要的原料。
  2) 暂停 / 单步：给相位驱动器一个"门"，让纯 AI 对局可以停在任意一步，
     停住之后可以在实验室里改提示词、重跑这一步、和当时的结果对比。
  3) 步骤查询：实验室按步取快照。

默认行为（重要）：
  · 落盘默认**开**（`LAB_LOG=0` 可关）—— 一行 JSON 很便宜，关了实验室就没原料
  · 暂停默认**关**（paused=false）—— 不接实验室时，对局行为完全不变

为什么落盘放在 layers_llm 而不是 agents：
  所有模型调用（对局 / 摘要 / 探针）都必经 layers_llm.chat_messages 这一个
  收口点。放在这里才能做到"一次调用一行、不漏也不重"。
"""

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/lab", tags=["lab"])

_HERE = os.path.dirname(os.path.abspath(__file__))
LAB_DIR = os.path.join(_HERE, "lab")
STEPS_PATH = os.path.join(LAB_DIR, "steps.jsonl")
CONTROL_PATH = os.path.join(LAB_DIR, "control.json")

_lock = threading.Lock()

# 由相位驱动器在每一步写入的上下文（哪一天 / 哪个相位 / 轮到谁）
_ctx: Dict[str, Any] = {"round": 0, "phase": "", "seat": None, "role": ""}


# ---------------------------------------------------------------- 开关
def log_enabled() -> bool:
    return os.environ.get("LAB_LOG", "1") != "0"


def _ensure_dir() -> None:
    try:
        os.makedirs(LAB_DIR, exist_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------- 上下文
def set_ctx(*, round: Optional[int] = None, phase: Optional[str] = None,
            seat: Optional[int] = None, role: Optional[str] = None) -> None:
    """相位驱动器在每步开头调用，给接下来发生的模型调用打上"这是第几天的哪一步"。"""
    with _lock:
        if round is not None:
            _ctx["round"] = round
        if phase is not None:
            _ctx["phase"] = phase
        if seat is not None:
            _ctx["seat"] = seat
        if role is not None:
            _ctx["role"] = role


def get_ctx() -> Dict[str, Any]:
    with _lock:
        return dict(_ctx)


# ---------------------------------------------------------------- 落盘
def log_call(*, model: str, messages: List[dict], raw: str,
             ms: int, ok: bool, err: Optional[str] = None,
             extra: Optional[dict] = None) -> None:
    """写一行步骤日志。任何异常都必须被吞掉——日志不能把对局搞崩。"""
    if not log_enabled():
        return
    try:
        _ensure_dir()
        sys_msg = ""
        user_msg = ""
        for m in messages or []:
            role = (m or {}).get("role")
            body = (m or {}).get("content") or ""
            if role == "system" and not sys_msg:
                sys_msg = body
            elif role == "user" and not user_msg:
                user_msg = body
        row = {
            "ts": time.strftime("%H:%M:%S"),
            "epoch": round(time.time(), 3),
            "round": _ctx.get("round"),
            "phase": _ctx.get("phase") or "",
            "seat": _ctx.get("seat"),
            "role": _ctx.get("role") or "",
            "model": model,
            "ms": ms,
            "ok": ok,
            "fallback": (not ok),
            "raw": raw or "",
            "prompt": {"system": sys_msg, "user": user_msg},
        }
        if err:
            row["err"] = err
        if extra:
            row.update(extra)
        with _lock:
            with open(STEPS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_steps(limit: int = 500) -> List[dict]:
    if not os.path.exists(STEPS_PATH):
        return []
    out: List[dict] = []
    try:
        with open(STEPS_PATH, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
    except OSError:
        return []
    if limit and len(out) > limit:
        out = out[-limit:]
    return out


def clear_steps() -> None:
    try:
        if os.path.exists(STEPS_PATH):
            os.remove(STEPS_PATH)
    except OSError:
        pass


# ---------------------------------------------------------------- 暂停 / 单步
def _read_control() -> Dict[str, Any]:
    d = {"paused": False, "budget": 0}
    try:
        if os.path.exists(CONTROL_PATH):
            with open(CONTROL_PATH, "r", encoding="utf-8") as f:
                got = json.load(f)
            if isinstance(got, dict):
                d["paused"] = bool(got.get("paused", False))
                d["budget"] = int(got.get("budget", 0) or 0)
    except (OSError, ValueError):
        pass
    return d


def _write_control(d: Dict[str, Any]) -> None:
    _ensure_dir()
    tmp = CONTROL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CONTROL_PATH)


def set_paused(v: bool) -> Dict[str, Any]:
    with _lock:
        d = _read_control()
        d["paused"] = bool(v)
        if not v:
            d["budget"] = 0        # 继续跑的时候把没用完的单步额度清掉
        _write_control(d)
        return d


def request_step(n: int = 1) -> Dict[str, Any]:
    """暂停状态下"再走一步"：给一个额度，驱动器走完一步后重新停住。"""
    with _lock:
        d = _read_control()
        d["paused"] = True
        d["budget"] = int(d.get("budget", 0)) + max(1, int(n))
        _write_control(d)
        return d


def gate() -> bool:
    """相位驱动器每步开头问一次：True = 这一步先别走（返回 working 交给轮询）。"""
    if not os.path.exists(CONTROL_PATH):
        return False
    d = _read_control()
    if not d.get("paused"):
        return False
    with _lock:
        d = _read_control()        # 重新读，避免与 request_step 抢
        if int(d.get("budget", 0)) > 0:
            d["budget"] = int(d["budget"]) - 1
            _write_control(d)
            return False           # 放行这一步
    return True                    # 停住


# ---------------------------------------------------------------- HTTP
@router.get("/status")
def api_status() -> dict:
    d = _read_control()
    return {
        "log_enabled": log_enabled(),
        "log_path": STEPS_PATH,
        "paused": bool(d.get("paused")),
        "budget": int(d.get("budget", 0)),
        "steps": len(read_steps(limit=0)),
        "ctx": get_ctx(),
    }


@router.post("/pause")
def api_pause(value: bool = True) -> dict:
    return {"ok": True, "control": set_paused(value)}


@router.post("/step")
def api_step(n: int = 1) -> dict:
    return {"ok": True, "control": request_step(n)}


@router.get("/steps")
def api_steps(limit: int = 500) -> dict:
    rows = read_steps(limit)
    return {"count": len(rows), "steps": rows}


@router.get("/steps/{index}")
def api_step_one(index: int) -> dict:
    rows = read_steps(limit=0)
    if index < 0 or index >= len(rows):
        raise HTTPException(status_code=404, detail="没有这一步")
    return rows[index]


@router.delete("/steps")
def api_clear() -> dict:
    clear_steps()
    return {"ok": True}
