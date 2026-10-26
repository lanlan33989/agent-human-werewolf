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

import copy
import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/lab", tags=["lab"])

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)          # 2026-10-26 拆 backend/ 后：数据目录锚仓库根
LAB_DIR = os.path.join(_ROOT, "lab")
STEPS_PATH = os.path.join(LAB_DIR, "steps.jsonl")
CONTROL_PATH = os.path.join(LAB_DIR, "control.json")

_lock = threading.Lock()

# 由相位驱动器在每一步写入的上下文（哪一天 / 哪个相位 / 轮到谁）
_ctx: Dict[str, Any] = {"round": 0, "phase": "", "seat": None, "role": "",
                        "snap": 0}


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
            "snap": int(_ctx.get("snap") or 0),
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
    """相位驱动器每步开头问一次：True = 这一步先别走（返回 working 交给轮询）。

    🔴 2026-10-26 改额度扣法：这里**只看不扣**。旧版在放行瞬间扣额度，但
    "一步"可能中途多次返回 working（预跑收割等待、夜钟补播）—— 单步就退化成
    "半步"：真模型局里按一次单步只走到第一个 working 返回点就停，回退重跑
    时必现。现在额度由 `step_done()` 在**一步完整走完**后才扣。"""
    if not os.path.exists(CONTROL_PATH):
        return False
    d = _read_control()
    if not d.get("paused"):
        return False
    with _lock:
        d = _read_control()        # 重新读，避免与 request_step 抢
        return int(d.get("budget", 0)) <= 0


def step_done() -> None:
    """一个相位步**完整走完**（handler 返回了 continue/pending/ended，而不是
    working）后由驱动器调用：在这里扣单步额度。任何异常吞掉。"""
    try:
        if not os.path.exists(CONTROL_PATH):
            return
        with _lock:
            d = _read_control()
            if d.get("paused") and int(d.get("budget", 0)) > 0:
                d["budget"] = int(d["budget"]) - 1
                _write_control(d)
    except Exception:
        pass


# ---------------------------------------------------------------- 提示词覆盖（实验室 → 对局）
# 🔴 2026-10-26 用户要"AI 玩家的提示词跟着第①②列走"：实验室里改好的 prompt
#    （第②列块 + 选中步局面），单步重跑时直接替换服务端组装的那份。
#    一次性消费（下一次模型调用用完即弃）；key 仍是服务端的 —— 实验室席位
#    的 key 只用于本地试跑对比，不进对局（密钥不过进程边界）。
_override: Optional[Dict[str, str]] = None
_OVERRIDE_TTL = 120.0     # 秒：超时自动作废 —— 单步放行的那步可能是不调模型的
                          # 补播 continue，覆盖若无限期留着会落到之后错误的调用上


def set_override(system: str, user: str) -> Dict[str, str]:
    """实验室设置一次性 prompt 覆盖：下一次模型调用（不管哪个座位）用它，120 秒内有效。"""
    global _override
    with _lock:
        _override = {"system": str(system or ""), "user": str(user or ""),
                     "ts": time.time()}
    return {"ok": True, "note": "下一次模型调用生效（120 秒内），用完即弃"}


def consume_override() -> Optional[Dict[str, str]]:
    """agents._build 组装完 messages 后问一次：有覆盖就整体替换。取走即清。"""
    global _override
    with _lock:
        ov = _override
        _override = None
        if ov and time.time() - float(ov.get("ts") or 0) > _OVERRIDE_TTL:
            ov = None                       # 过期作废，不让旧覆盖污染后续调用
    return ov


def peek_override() -> bool:
    with _lock:
        ov = _override
    return bool(ov) and time.time() - float(ov.get("ts") or 0) <= _OVERRIDE_TTL


# ---------------------------------------------------------------- 局面快照 / 回退
# 🔴 2026-10-26 用户要「真的能回退」：不只回看日志，而是把引擎状态整个退回去，
#    改完提示词从那里重跑。可行性依据（2026-10-26 审计结论）：
#    · engine.Game 是**纯数据对象**（无锁/句柄/回调，rng 可 deepcopy）⇒ 整体 deepcopy 安全
#    · AI 的 Agent 实例在 Session 上、对局中途无状态 ⇒ 不进快照，沿用原实例
#      （deepcopy 函数对象是原引用，闭包里的 LLM 客户端不会被复制 —— 正好）
#    · 快照点 = night_driver.auto_advance 的「gate 放行之后、handler 分发之前」：
#      上一步已完整落定、下一步未动，粒度与暂停门/单步完全一致（一个相位步）。
#    ⚠️ 粒度说明：一次"步"可能含多次模型调用（狼队协商 = 每狼一次），
#       回退重跑的是**整步**，不是某一句话。
#    ⚠️ 重跑语义是「规则等价」而不是逐字复现：rng 在快照里所以规则层可复现，
#       但 LLM 本身非确定 —— 这正是"改提示词后对比"想要的。
_SNAP_MAX = 80                     # 内存环形缓冲：最近 80 步（一局几百步内够用）
_snaps: List[Dict[str, Any]] = []  # {"i","gid","round","phase","game","sess"}
_snap_seq = 0
_last_gid = ""
_lookup: Optional[Callable[[str], Any]] = None


def register_session_lookup(fn: Callable[[str], Any]) -> None:
    """由 server.py 注册「gid → Session」的查询，避免 lab_api 反向 import game_api。"""
    global _lookup
    _lookup = fn


# ---- 第①列席位接管对局（2026-10-26）：key/model/baseUrl 全部走实验室配置 ----
_AGENTS_PATH = os.path.join(LAB_DIR, "agents_runtime.json")
_apply_agents_fn: Optional[Callable[[List[Dict[str, Any]]], None]] = None


def register_apply_agents(fn: Callable[[List[Dict[str, Any]]], None]) -> None:
    """由 server.py 注册「席位数组 → 更新装配覆盖表」的回调。"""
    global _apply_agents_fn
    _apply_agents_fn = fn


def _norm_agent(a: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "label": str(a.get("label") or "").strip(),
        "baseUrl": str(a.get("baseUrl") or "").strip(),
        "model": str(a.get("model") or "").strip(),
        "apiKey": str(a.get("apiKey") or ""),
        "temperature": float(a.get("temperature") or 0.8),
        "timeout": float(a.get("timeout") or 60),
        "thinking": "enabled" if str(a.get("thinking")) == "enabled" else "disabled",
    }


def set_agents_config(agents: List[Dict[str, Any]]) -> Dict[str, Any]:
    """保存席位数组（第 1 个 AI 用第 1 个席位，依序循环）并立即生效于下一局。"""
    norm = [_norm_agent(a) for a in (agents or []) if isinstance(a, dict)]
    norm = [a for a in norm if a["baseUrl"] and a["model"]]
    os.makedirs(LAB_DIR, exist_ok=True)
    tmp = _AGENTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"agents": norm}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _AGENTS_PATH)
    if _apply_agents_fn:
        try:
            _apply_agents_fn(norm)
        except Exception as e:                       # noqa: BLE001
            print("[lab] 席位覆盖热应用失败（文件已存，下一局装配时重读）：" + repr(e),
                  flush=True)
    return {"ok": True, "count": len(norm)}


def get_agents_config() -> List[Dict[str, Any]]:
    try:
        with open(_AGENTS_PATH, "r", encoding="utf-8") as f:
            j = json.load(f)
        return [a for a in (j.get("agents") or []) if isinstance(a, dict)]
    except (OSError, ValueError):
        return []


def _sess_state(sess: Any) -> Dict[str, Any]:
    """与 game 推进深度耦合、必须**一起回退**的 Session 字段。
    漏回退任何一个都会重复播报 / 预案不再生成（审计列出的坑 #2）。"""
    keys = ("speech", "timeline", "_log_ptr", "_announced", "_harvested",
            "tactics", "day_plans", "day_summaries", "_planned_round",
            "pending", "human_wait_since", "human_wait_pending_since",
            "_nn_round", "_nn_nodes", "_nn_i", "_nn_at", "_nn_need_arm",
            "_nn_injecting")
    out: Dict[str, Any] = {}
    for k in keys:
        if not hasattr(sess, k):
            continue
        try:
            out[k] = copy.deepcopy(getattr(sess, k))
        except Exception:
            pass
    return out


def maybe_snapshot(sess: Any) -> int:
    """相位驱动器每步调用（gate 放行后）。任何异常都被吞掉 —— 快照不能影响对局。"""
    global _last_gid, _snap_seq
    if not log_enabled():
        return 0
    try:
        with _lock:
            _snap_seq += 1
            i = _snap_seq
            _ctx["snap"] = i                      # 之后发生的模型调用行都带这个号
            _snaps.append({
                "i": i,
                "gid": str(getattr(sess, "id", "") or ""),
                "round": _ctx.get("round"),
                "phase": _ctx.get("phase") or "",
                "game": copy.deepcopy(getattr(sess, "game", None)),
                "sess": _sess_state(sess),
            })
            if len(_snaps) > _SNAP_MAX:
                _snaps.pop(0)
            _last_gid = str(getattr(sess, "id", "") or "")
        return i
    except Exception as e:                     # 快照失败不能影响对局，但不能静默 ——
        print("[lab] 局面快照失败：" + repr(e), flush=True)   # 静默了回退就悄悄失效
        return 0


def list_snaps(limit: int = 200) -> List[Dict[str, Any]]:
    with _lock:
        rows = [{"i": s["i"], "gid": s["gid"], "round": s["round"],
                 "phase": s["phase"]} for s in _snaps[-max(1, limit):]]
    return rows


def _truncate_steps_after(i: int) -> int:
    """截掉落盘日志里快照号 >= i 的行（被否决的未来）。旧的、无 snap 号的行保留。"""
    kept: List[str] = []
    removed = 0
    try:
        if os.path.exists(STEPS_PATH):
            with open(STEPS_PATH, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.rstrip("\n")
                    if not ln.strip():
                        continue
                    try:
                        row = json.loads(ln)
                        if int(row.get("snap") or 0) >= i:
                            removed += 1
                            continue
                    except ValueError:
                        pass
                    kept.append(ln)
            tmp = STEPS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(kept) + ("\n" if kept else ""))
            os.replace(tmp, STEPS_PATH)
    except OSError:
        pass
    return removed


def rewind(i: int) -> Dict[str, Any]:
    """把对局整个退回第 i 步**之前**（快照 i = 第 i 步动作前的完整状态），
    并自动暂停 —— 之后在实验室改提示词，再单步/继续从那里重跑。"""
    if _lookup is None:
        raise RuntimeError("还没装配（服务未启动完成）")
    with _lock:
        snap = next((s for s in _snaps if s["i"] == int(i)), None)
    if snap is None:
        raise KeyError("没有这个快照（可能已被环形缓冲挤出，或还没走到这一步）")
    sess = _lookup(snap["gid"])
    if sess is None:
        raise KeyError("这一局已经不在内存里了（服务重启或被淘汰），无法回退")
    # 驱动泵持 sess.lock 推进 —— 拿到锁 = 没有半步动作正在进行
    with getattr(sess, "lock", threading.Lock()):
        with _lock:
            snap = next((s for s in _snaps if s["i"] == int(i)), None)
        if snap is None:
            raise KeyError("快照刚好被挤出了环形缓冲，重试一次")
        # ① 引擎状态：deepcopy 回去（副本，别把快照本体交出去被继续改）
        sess.game = copy.deepcopy(snap["game"])
        # ② 展示层 / 推进游标：与 game 一起回退
        for k, v in snap["sess"].items():
            try:
                setattr(sess, k, copy.deepcopy(v))
            except Exception:
                pass
        # ③ 过期预跑决策必须全弃（审计列出的坑 #1：唯一能跨快照污染引擎的通道）
        try:
            sess._pre.discard_all()
        except Exception:
            pass
        try:
            sess._pre = None
            sess._pre_round = None
            sess._pre_vote_round = None
            sess._vote_grace_at = None
            sess._adv_deadline = None
        except Exception:
            pass
        # ④ 快照流：回退点之后 = 已被否决的未来，删掉（步号不回退，重跑生成新步）
        with _lock:
            _snaps[:] = [s for s in _snaps if s["i"] <= int(i)]
        removed = _truncate_steps_after(int(i))
        # ⑤ 停在原地：改完提示词由人决定单步还是继续
        ctl = set_paused(True)
    return {"ok": True, "rewound_to": int(i), "round": snap["round"],
            "phase": snap["phase"], "removed_log_rows": removed,
            "control": ctl}


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


# ---------------------------------------------------------------- 直播 / 回退
@router.get("/live")
def api_live(limit: int = 150) -> dict:
    """实验室群聊面板的数据源：最近的时间线条目 + 当前局面 + 门状态。
    只读公开层（timeline 的 sys/say 本来就是全桌可见的东西），无泄露面。"""
    d = _read_control()
    out: Dict[str, Any] = {"gid": _last_gid, "found": False,
                           "paused": bool(d.get("paused")),
                           "budget": int(d.get("budget", 0)),
                           "steps": _snap_seq, "log_steps": len(read_steps(limit=0))}
    if _lookup is None:
        return out
    sess = _lookup(_last_gid) if _last_gid else None
    if sess is None:
        return out
    g = getattr(sess, "game", None)
    if g is None:
        return out
    # ★ 谁在看直播，谁就是推进器：本服务的推进一直靠客户端轮询 drive()
    #   （/play 的语义；v4 说明里写的"服务端水泵"在代码里并不存在——2026-10-26
    #   实测发现，直播面板如果不 drive，围观时对局是静止的）。预算与 /public 同档。
    try:
        sess.drive(0.15)
    except Exception:
        pass
    out.update({
        "found": True,
        "round": getattr(g, "round", 0),
        "phase": getattr(getattr(g, "phase", None), "value", ""),
        "winner": getattr(g, "winner", None),
        "pending": str(getattr(sess, "pending", "") or ""),
        "public_log": [str(x) for x in (getattr(g, "public_log", None) or [])[-40:]],
        "timeline": list(getattr(sess, "timeline", None) or [])[-max(10, min(400, int(limit))):],
        "players": [{"seat": p.seat + 1, "kind": p.kind, "alive": p.alive,
                     "lives": p.lives} for p in getattr(g, "players", [])],
    })
    # 👁 上帝视角（2026-10-26 用户要的）：看直播的人类是旁观者，AI 不知道的
    # （身份、狼刀、女巫药、守卫挡刀、验人结果）人类全都能看 —— 有开关可折叠。
    # 数据全来自引擎已有记录，不新增泄漏面（接口本来就在本机）。
    try:
        roles = {}
        for p in getattr(g, "players", []):
            roles[str(p.seat)] = str(getattr(p.role, "value", "") or "")
        nr = list(getattr(g, "night_records", None) or [])
        cur = getattr(g, "night", None) or {}
        out["god"] = {
            "roles": roles,
            "night_records": nr,
            "night_now": {k: cur.get(k) for k in
                          ("kill", "heal", "poison", "guard", "investigate",
                           "wolf_points", "wolf_confirm", "kill_settled")},
        }
    except Exception:                                    # noqa: BLE001
        pass
    return out


@router.get("/export")
def api_export(gid: str = "") -> dict:
    """对局完整导出（2026-10-26）：时间线（含真人发言与主持人播报）+ 上帝视角 +
    模型调用步骤 —— /play 封面的「导出对局文件」下载的就是这个；
    实验室「导入对局日志」识别该格式后在直播间完整重放。"""
    target = gid or _last_gid
    sess = _lookup(target) if (_lookup and target) else None
    out: Dict[str, Any] = {"kind": "lab-export", "gid": target or "",
                           "timeline": [], "public_log": [], "steps": [],
                           "roles": {}, "night_records": []}
    if sess is not None:
        g = getattr(sess, "game", None)
        if g is not None:
            out.update({
                "round": getattr(g, "round", 0),
                "phase": getattr(getattr(g, "phase", None), "value", ""),
                "winner": getattr(g, "winner", None),
                "public_log": [str(x) for x in (getattr(g, "public_log", None) or [])],
                "timeline": list(getattr(sess, "timeline", None) or []),
                "roles": {str(p.seat): str(getattr(p.role, "value", "") or "")
                          for p in getattr(g, "players", [])},
                "night_records": list(getattr(g, "night_records", None) or []),
            })
    out["steps"] = read_steps(limit=0)
    return out


@router.get("/snapshots")
def api_snapshots(limit: int = 200) -> dict:
    return {"count": len(_snaps), "snapshots": list_snaps(limit)}


@router.post("/override")
async def api_override(request: Request) -> dict:
    """实验室 → 对局：把实验室组装好的 prompt 设为一次性覆盖（下一次模型调用）。"""
    try:
        body = await request.json()
    except Exception:                                    # noqa: BLE001
        raise HTTPException(status_code=400, detail="body 必须是 JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是对象")
    return set_override(str(body.get("system") or ""), str(body.get("user") or ""))


@router.get("/override")
def api_override_status() -> dict:
    return {"pending": peek_override()}


@router.post("/agents-config")
async def api_agents_config(request: Request) -> dict:
    """第①列席位 → 对局装配覆盖（key/model/baseUrl 全走第①列；下一局生效）。"""
    try:
        body = await request.json()
    except Exception:                                    # noqa: BLE001
        raise HTTPException(status_code=400, detail="body 必须是 JSON")
    if not isinstance(body, dict) or not isinstance(body.get("agents"), list):
        raise HTTPException(status_code=400, detail="body 需要 {agents: [...] }")
    return set_agents_config(body["agents"])


@router.get("/agents-config")
def api_agents_config_get() -> dict:
    return {"agents": get_agents_config()}


@router.post("/rewind")
def api_rewind(i: int) -> dict:
    try:
        return rewind(i)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
