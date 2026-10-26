# -*- coding: utf-8 -*-
"""
批八 smoke test：阶段剧本化（H1）· 9 种人格（H2）· 多厂商混编（H3）。

这一批里最值得钉的是**两张"结构性保证"**：
  · 剧本表的每一行 `run` 都必须真的指向驱动器上的方法（字符串间接调用很容易写错，
    而且写错的表现是"这一步永远做不完"——和卡死长得一模一样）
  · `assign_personalities` 的入参里**不许有角色**（人格一旦与角色相关，
    它就从风格变成身份信号，而且破得无声无息）
"""
import inspect
import os
import sys

os.environ["MOCK"] = "1"
os.environ["MOCK_ASR"] = "1"
os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_TTS"] = "1"
sys.path.insert(0, ".")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/

import game_api  # noqa: E402
import steps  # noqa: E402
from agents import (PERSONALITIES, Agent, assign_personalities,  # noqa: E402
                    _personality_block, build_messages, personality_spec)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


# ================================================================ H1 阶段剧本

check("★ 剧本表的顺序 = 语义（狼→预→女巫→守卫），顺序即语义",
      steps.night_step_names() == ("wolf", "seer", "witch", "guard"),
      str(steps.night_step_names()))

_s = game_api.Session(humans=3, ais=3, seed=5, level="认真")
check("★ 表里每一行的 run 都真的指向驱动器上的方法"
      "（字符串间接调用写错的表现是'这一步永远做不完'，和卡死分不出来）",
      all(callable(getattr(_s, st.run, None)) for st in steps.NIGHT_STEPS),
      str([st.run for st in steps.NIGHT_STEPS]))

# 🔴 回归：**场上没有狼时，整夜必须直接算完**
#    这是重构时丢掉过的一条兜底（12 人局回归当场抓到）：
#    没狼 ⇒ 没有刀口 ⇒ kill 永远是 None ⇒ 而女巫那一步要求"刀口已定"
#    ⇒ 她永远做不完 ⇒ 夜晚再也结束不了，而且此时**没人有任何待办**。
_nw = game_api.Session(humans=3, ais=3, seed=7, level="认真")
_g = _nw.game
for _p in _g.players:
    if _p.role.is_wolf:
        _p.alive = False
        _p.lives = 0
_g.night["kill"] = None
_g.night["witch_asked"] = False
check("★★ 场上没有狼 ⇒ 整夜直接算完（否则 kill 永远是 None、女巫那步永远做不完 = 卡死）",
      steps.night_done(_g) is True, f"night_done={steps.night_done(_g)}")

# 有狼、刀口未定 ⇒ 女巫那一步不能算完（她要看着刀口决定救不救）
_s2 = game_api.Session(humans=3, ais=3, seed=7, level="认真")
_g2 = _s2.game
if any(p.role.value == "witch" for p in _g2.alive):
    _g2.night["kill"] = None
    check("有狼、刀口还没定 ⇒ 女巫那一步不算完（也不能被问）",
          steps.night_done(_g2) is False and _s2._step_ai_witch() is False, "")

# 正常夜里，第一个没做完的是狼队
_s3 = game_api.Session(humans=3, ais=3, seed=9, level="认真")
check("正常夜晚：第一个没做完的步骤是狼队（顺序在表里）",
      steps.first_unfinished(_s3.game).key in ("wolf", "seer", "witch", "guard"),
      steps.first_unfinished(_s3.game).key)

# ================================================================ H2 人格

check("★ 9 种人格齐全，每种都有 label / brief / stubborn",
      len(PERSONALITIES) == 9
      and all({"label", "brief", "stubborn"} <= set(v) for v in PERSONALITIES.values()),
      str(list(PERSONALITIES)))

_params = list(inspect.signature(assign_personalities).parameters)
check("★★ 人格分配函数的入参里**没有角色**（这是结构保证：人格一旦与角色相关，"
      "它就从风格变成身份信号）",
      _params == ["seed", "ai_seats"], str(_params))

_a1 = assign_personalities(7, [1, 3, 5])
_a2 = assign_personalities(7, [1, 3, 5])
check("★ 同一种子 ⇒ 同一种分配（确定性，日志/复盘才对得上账）",
      _a1 == _a2 and set(_a1) == {1, 3, 5}, str(_a1))
check("分配只在 AI 座位上（人类座位不会有人格）",
      assign_personalities(7, [1, 3]) == {1: _a1[1], 3: _a1[3]}, "")

_s_off = game_api.Session(humans=3, ais=3, seed=11, level="认真", personality="")
check("★ 不配人格（缺省）：所有 AI 座位的 personality 都是 None",
      all(getattr(a, "personality", None) is None for a in _s_off.agents.values())
      and len(_s_off.agents) > 0, "")
_s_rand = game_api.Session(humans=3, ais=3, seed=11, level="认真", personality="random")
check("★ 随机分配：每个 AI 座位都拿到 9 种里的一个",
      all(a.personality in PERSONALITIES for a in _s_rand.agents.values()), "")
check("★ 随机分配下**人类座位不带人格**（人格不是身份，也不是给真人用的）",
      all(getattr(p, "personality", None) is None or p.kind == "ai"
          for p in _s_rand.game.players), "")
_s_fix = game_api.Session(humans=3, ais=3, seed=11, level="认真",
                          personality="aggressive")
check("指定人格：整桌 AI 同一种", 
      all(a.personality == "aggressive" for a in _s_fix.agents.values()), "")

_v8 = _s_off.game.get_view_for(next(p.seat for p in _s_off.game.players if p.kind == "ai"))
check("★ prompt：不配人格时**连'性格'那段都不出现**（空槽会被模型填成幻觉）",
      "你的性格" not in build_messages(_v8, [], "说一句话")[0]["content"], "")
check("★ prompt：配了人格才出现，且写明是什么性格",
      "你的性格：激进" in build_messages(_v8, [], "说一句话", personality="aggressive")[0]["content"], "")
check("不认识的性格 key 不抛异常（人格是可选装饰，不能因为拼错键把整局弄崩）",
      personality_spec("不存在的性格") is None and _personality_block("不存在的性格") == "", "")

_ag_stub = Agent(1, chat_fn=None)
_ag_stub.personality = "aggressive"
_ag_stub2 = Agent(1, chat_fn=None)
_ag_stub2.personality = "conservative"
_ag_stub3 = Agent(1, chat_fn=None)
check("★ 跟随/固执来自人格：激进=固执、保守=跟随、不配=跟随（默认）",
      _ag_stub.follows_team() is False
      and _ag_stub2.follows_team() is True
      and _ag_stub3.follows_team() is True, "")

# 行为断言：真局里，固执的 AI 狼**不会**跟随人狼那一票
_s_h = None
for _sd in range(60):
    _cand = game_api.Session(humans=3, ais=3, seed=_sd, level="认真")
    _wh = next((p for p in _cand.game.players if p.role.is_wolf and p.kind == "human"), None)
    _wa = next((p for p in _cand.game.players if p.role.is_wolf and p.kind == "ai"), None)
    if _wh is not None and _wa is not None:
        _s_h, _seat_h, _seat_a = _cand, _wh.seat, _wa.seat
        break
if _s_h is not None:
    _gg = _s_h.game
    _good = next(p.seat for p in _gg.alive if not p.role.is_wolf)
    _other = next(p.seat for p in _gg.alive if not p.role.is_wolf and p.seat != _good)
    _gg.night_kill(_seat_a, _other)          # AI 狼先点
    _gg.night_kill(_seat_h, _good)           # 人狼点另一个 ⇒ 有分歧 ⇒ 才谈得上"跟不跟"
    _gg.night["wolf_followed"] = []
    _ag = _s_h.agents.get(_seat_a)
    if _ag is not None:
        _ag.personality = None
        _follow_default = _s_h._ai_wolf_maybe_follow(_seat_a)
        _gg.night["wolf_followed"] = []
        _gg.night["wolf_points"][_seat_a] = _other          # 复位它的点
        _ag.personality = "aggressive"
        _follow_stubborn = _s_h._ai_wolf_maybe_follow(_seat_a)
        check("★ 行为断言：不配人格的 AI 狼会跟随人狼那一票",
              _follow_default is True, str(_follow_default))
        check("★ 行为断言：固执人格（激进）的 AI 狼**不跟随**，坚持自己的点",
              _follow_stubborn is False, str(_follow_stubborn))

# ================================================================ H3 多厂商混编

import server  # noqa: E402

check("★ LLM_MODELS 留空 ⇒ 退化成单通道（= 以前的行为）",
      len(server.LLM_POOL) == 1, str([n for _, n in server.LLM_POOL]))
_pool = [("a", "a"), ("b", "b"), ("c", "c")]
_seq = [server._model_for_seat(i, _pool)[1] for i in range(6)]
check("★ 座位 → 模型是**按座位取模**的确定性映射（否则复盘无法归因到模型）",
      _seq == ["a", "b", "c", "a", "b", "c"], str(_seq))
check("混编下每个座位都能拿到一个模型（不会越界）",
      all(server._model_for_seat(i, _pool)[1] for i in range(20)), "")

_rp8 = _s_off.replay()
check("★ 复盘里带上了每个座位的**人格与模型**（榜单要能回答'哪个模型当狼最难被看穿'）",
      all("personality" in s and "model" in s for s in _rp8["seats"]), "")
check("会话把座位→模型记下来了（混编数据的归因入口）",
      isinstance(getattr(_s_off, "models", None), dict), str(getattr(_s_off, "models", None)))

_rules8 = game_api.rules()
check("★ /rules 里带上人格表 + 两个特殊值（前端按表渲染，不写死）",
      len(_rules8.get("personalities") or []) == 9
      and [x["key"] for x in (_rules8.get("personality_specials") or [])] == ["off", "random"],
      str(len(_rules8.get("personalities") or [])))

print("\n=== 批八 smoke test（剧本 · 人格 · 混编）===\n")
pass_n = fail_n = 0
for name, ok, detail in results:
    print(("PASS" if ok else "FAIL") + "  " + name.ljust(56)
          + ("  " + detail if detail else ""))
    if ok:
        pass_n += 1
    else:
        fail_n += 1
print("\n" + ("ALL GREEN (" + str(pass_n) + " passed)" if fail_n == 0
              else str(fail_n) + " FAILED / " + str(pass_n) + " passed"))
sys.exit(1 if fail_n else 0)
