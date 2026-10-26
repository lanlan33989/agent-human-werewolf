# -*- coding: utf-8 -*-
"""
权威规则对照表（可执行版）。

## 这个文件是什么

`05b-三神与新机制.html` 第一节是一张**人读的判例表**（猎人 / 守卫 / 白痴，共 11 条），
来源是标准局判例。它早就写好了，但一直**只是文档** —— 代码改它、没人拦得住。

05b 自己给的做法（原文）：

    先写"规则表"（人读的标准判例）→ 由规则表生成断言 → 人工复核规则表。
    **不要按实现写断言** —— `_smoke-fixes.py` 当年就是因为断言跟着错误实现走，
    把"女巫狼刀前睁眼"这条错规则锁了一个月。

这个文件就是那句话的落实：`SPEC` 是判例表（原文照抄 + 代码位置），
下面每一条断言**从表里逐行生成**（`record(id, ...)` 按 id 回表），
最后打印"判例 → 结论"的对照表，并且检查**表里没有任何一行被静默跳过**。

⚠️ 断言写的是**判例**（外部可观察的行为），不是实现细节：
   比如"猎人被投出后能开枪"验的是"这一枪真的打出去了"，不是 `phase is SHOOT`。
   实现换了（相位名改掉、结算顺序重排）这条断言照样有效。

跑法：python tests/_smoke-rules.py
退出码：0 = 全一致；1 = 有判例与实现不一致（那就是 bug）。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import os  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/

from engine import Board, Game, Phase, Role, Rules  # noqa: E402

# ================================================================ 判例表
#
# (id, 角色, 判例（05b 原文，逐字）, 代码位置)
# 🔴 这张表是**人读的**：改规则先改这里，再改代码和断言 —— 不是反过来。
SPEC = [
    ("H1", "猎人", "① 被狼刀出局 → 可开枪带走一人",
     "engine.resolve_night → _open_shoot_if_hunter → night_shoot"),
    ("H2", "猎人", "② 被投票投出 → 可开枪",
     "engine.resolve_vote → _open_shoot_if_hunter"),
    ("H3", "猎人", "③ 被女巫毒杀 → 一般不可开枪（开关 hunter_can_shoot_on_witch_poison）",
     "engine._open_shoot_if_hunter"),
    ("H4", "猎人", "④ 开枪结算完成后才判胜负",
     "engine.night_shoot（damage → 记死因 → 再 check_winner）"),
    ("G1", "守卫", "① 每夜守一人，可守自己（开关 guard_can_self_guard）",
     "engine.night_guard"),
    ("G2", "守卫", "② 不可连续两晚守同一人（开关 guard_can_repeat）",
     "engine.night_guard + private.guard_last"),
    ("G3", "守卫", "③ 同守同救 = 该人仍死（「奶穿」）",
     "engine.resolve_night 的救/守结算"),
    ("G4", "守卫", "④ 守卫当晚死掉，他这一晚的守护仍然生效",
     "engine.night_guard + resolve_night 的结算顺序"),
    ("I1", "白痴", "① 被投票出局时翻牌不死，只失去投票权",
     "engine.resolve_vote + can_vote"),
    ("I2", "白痴", "② 之后仍可发言",
     "engine._actions_available（DISCUSS 分支在 can_vote 之前）"),
    ("I3", "白痴", "③ 已翻牌的白痴不能再被投票出局（多数版本）",
     "engine.resolve_vote"),
    ("T1", "全局", "平票默认无人出局（开关 tie_vote_rule）",
     "engine.resolve_vote"),
    # ⚠️ T2 不在 05b 那张表里：它是 2026-09-17 用户定的（引擎里写在 cast_vote 的 docstring）。
    #    补进来的理由：**只写在注释里的规则等于没有规则** —— 前端目标列表、引擎校验、
    #    测试三处要一致，而"投自己算不算合法"正是那种三处会各说各话的地方
    #    （_smoke-play.js 就是因为这条红了两次：自刀、自投）。
    ("T2", "全局", "允许投自己（自投 ≈ 弃权的一种表达，2026-09-17 用户定）",
     "engine.cast_vote + 前端投票目标列表"),
    # T4 同理不在 05b 里：2026-09-18 用户定的发言机制（默认关）。
    ("T4", "全局", "发言方式：被点名的人先回应（小讨论，2026-09-18 用户定）",
     "engine.end_speech → _queue_challenge（局势卡 / 关键词 → metrics.accuse_targets）"),
]

PASS: list = []
FAIL: list = []
SEEN: dict = {}


def record(rid: str, ok: bool, detail: str = "") -> None:
    """记一条判例的结论。rid 必须在 SPEC 里（否则是拼错了 id，直接炸）。"""
    if rid not in {s[0] for s in SPEC}:
        raise KeyError(f"断言用了表里没有的 id：{rid}")
    SEEN[rid] = bool(ok)
    row = next(s for s in SPEC if s[0] == rid)
    name = f"{row[1]}·{row[2]}"
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"\n       <- {detail}" if detail else ""))


def log(s: str) -> None:
    print("       " + s)


# 板子：显式给角色（不靠 make_board —— 这里要的是"一定有猎人/守卫/白痴"）
_B_GUARD = Board(key="t-rules-guard", label="规则表：狼/守卫/2 民",
                 roles=[Role.WOLF, Role.GUARD, Role.VILLAGER, Role.VILLAGER], lives=1)
_B_WITCH = Board(key="t-rules-witch", label="规则表：狼/女巫/守卫/猎人/民",
                 roles=[Role.WOLF, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER], lives=1)
_B_IDIOT = Board(key="t-rules-idiot", label="规则表：狼/白痴/2 民",
                 roles=[Role.WOLF, Role.IDIOT, Role.VILLAGER, Role.VILLAGER], lives=1)
# 判胜顺序专用：1 狼 + 猎人 + 1 民 —— 这样猎人那一枪**能翻转胜负**（见 H4）
_B_HUNT = Board(key="t-rules-hunt", label="规则表：1 狼/猎人/1 民",
                roles=[Role.WOLF, Role.HUNTER, Role.VILLAGER], lives=1)
_K5 = ["human"] * 5
_K4 = ["human"] * 4


def seat_of(g: Game, role: Role) -> int:
    return next(p.seat for p in g.players if p.role is role)


def peaceful_night(g: Game) -> None:
    """走一个平安夜并进入白天（不刀人 ⇒ 不暴露任何东西，只想推进相位）。"""
    g.night["kill"] = None
    g.resolve_night()


print("\n=== 权威规则对照表（05b 判例表 → 断言）===\n")

# ---------------------------------------------------------------- 猎人
# H1 被狼刀 ⇒ 可开枪
_g = Game(_B_WITCH, kinds=_K5, seed=61)
_g.start()
_h = seat_of(_g, Role.HUNTER)
_g.wolf_agree_kill(_h)
_g.resolve_night()
_w = seat_of(_g, Role.WOLF)
_shot = False
_err = ""
try:
    _g.night_shoot(_h, _w)
    _shot = not _g.player(_w).alive
except Exception as e:                     # noqa: BLE001
    _err = f"{type(e).__name__}: {e}"
record("H1", _g.shooter == _h or _shot, f"shooter={_g.shooter} shot={_shot} {_err}")

# H2 被投票投出 ⇒ 可开枪
#
# ⚠️ 这一条以前**完全没被测过**（能查到的断言只有"被刀"和"被毒"两种）。
#    它和 H1 是两条不同的代码路径：死因来自 resolve_vote，不是 resolve_night。
_g = Game(_B_WITCH, kinds=_K5, seed=62)
_g.start()
_h = seat_of(_g, Role.HUNTER)
_w = seat_of(_g, Role.WOLF)
peaceful_night(_g)
_g.open_discuss()
_g.open_vote()
for _p in _g.alive:                        # 全场把猎人投出去
    if _p.seat != _h:
        _g.cast_vote(_p.seat, _h)
_g.resolve_vote()
_shot, _err = False, ""
try:
    _g.night_shoot(_h, _w)                 # 验的是"这一枪真的打得出去"
    _shot = not _g.player(_w).alive
except Exception as e:                     # noqa: BLE001
    _err = f"{type(e).__name__}: {e}"
record("H2", _shot and not _g.player(_h).alive,
       f"hunter_alive={_g.player(_h).alive} shooter(开枪前)={_g.shooter} "
       f"phase={_g.phase.value} shot={_shot} {_err}")

# H3 被女巫毒杀 ⇒ 默认不能开枪；打开开关就能
_g = Game(_B_WITCH, kinds=_K5, seed=63)
_g.start()
_h, _w, _wi = seat_of(_g, Role.HUNTER), seat_of(_g, Role.WOLF), seat_of(_g, Role.WITCH)
_v = seat_of(_g, Role.VILLAGER)
_g.wolf_agree_kill(_v)
_g.night_poison(_wi, _h)
_g.resolve_night()
_off_ok = _g.shooter is None
_g2 = Game(_B_WITCH, kinds=_K5, seed=63, rules=Rules(hunter_can_shoot_on_witch_poison=True))
_g2.start()
_h2, _wi2 = seat_of(_g2, Role.HUNTER), seat_of(_g2, Role.WITCH)
_v2 = seat_of(_g2, Role.VILLAGER)
_g2.wolf_agree_kill(_v2)
_g2.night_poison(_wi2, _h2)
_g2.resolve_night()
record("H3", _off_ok and _g2.shooter == _h2,
       f"默认 shooter={_g.shooter}；开开关后 shooter={_g2.shooter}")

# H4 开枪结算完成**之后**才判胜负
#
# ⚠️ 05b 点名的"坑 3"：如果先判胜，会出现"已经宣布狼人胜利了，猎人手里还拿着枪"。
#    构造必须是"这一枪**能翻转结果**"的场景，否则测不出顺序：
#      板子 = 1 狼 + 猎人 + 1 民。狼刀猎人 ⇒ 场上 1 狼 1 好（w >= g ⇒ 朴素判胜=狼胜），
#      但猎人这一枪打死最后一只狼 ⇒ 0 狼 2 好 ⇒ **好人翻盘**。
#    所以夜里结算完**不能**已经判成狼胜，且这一枪打完后胜负必须翻过来。
#
# 📌 我第一版把这条写错了：用了"2 狼 + 3 好"，以为打死一只狼就能让好人赢 ——
#    错。好人要**狼全死**（w == 0）才算赢，1 狼 2 好 的局面还得继续打。
#    断言红了但代码是对的 ⇒ 修的是我的场景构造，不是放宽判例。
_g = Game(_B_HUNT, kinds=["human"] * 3, seed=64)
_g.start()
_h = seat_of(_g, Role.HUNTER)
_wolves = [p.seat for p in _g.players if p.role is Role.WOLF]
_g.wolf_agree_kill(_h)
_g.resolve_night()
_deferred_winner = _g.winner              # ← 必须在开枪**之前**取，否则测的是别的东西
_deferred_phase = _g.phase
_shot_ok, _err = False, ""
try:
    _g.night_shoot(_h, _wolves[0])
    _shot_ok = not _g.player(_wolves[0]).alive
except Exception as e:                     # noqa: BLE001
    _err = f"{type(e).__name__}: {e}"
record("H4", _deferred_winner is None and _deferred_phase is not Phase.ENDED
       and _shot_ok and _g.winner == "good",
       f"夜里结算后 winner={_deferred_winner} phase={_deferred_phase.value}"
       f"（必须是 None/未结束，否则就是「先宣布狼胜、枪还在手上」）；"
       f"开枪后 winner={_g.winner} shot={_shot_ok} {_err}")

# ---------------------------------------------------------------- 守卫
# G1 可守自己；开关关掉就必须拒
_g = Game(_B_GUARD, kinds=_K4, seed=65)
_g.start()
_gu = seat_of(_g, Role.GUARD)
_g.night_guard(_gu, _gu)
_self_ok = _g.night["guard"] == _gu
_g2 = Game(_B_GUARD, kinds=_K4, seed=65, rules=Rules(guard_can_self_guard=False))
_g2.start()
_gu2 = seat_of(_g2, Role.GUARD)
_refused = False
try:
    _g2.night_guard(_gu2, _gu2)
except ValueError:
    _refused = True
record("G1", _self_ok and _refused, f"守自己={_self_ok}；开关关闭后被拒={_refused}")

# G2 不可连续两晚守同一人
_g = Game(_B_GUARD, kinds=_K4, seed=66)
_g.start()
_gu = seat_of(_g, Role.GUARD)
_v = seat_of(_g, Role.VILLAGER)
_g.night_guard(_gu, _v)
_g.resolve_night()
_g.next_night()
_refused = False
try:
    _g.night_guard(_gu, _v)
except ValueError:
    _refused = True
record("G2", _refused, f"第二晚守同一个人：guard_last={_g.private[_gu].guard_last} 被拒={_refused}")

# G3 同守同救 = 该人仍死（奶穿）
_g = Game(_B_WITCH, kinds=_K5, seed=67)
_g.start()
_w, _wi, _gu = seat_of(_g, Role.WOLF), seat_of(_g, Role.WITCH), seat_of(_g, Role.GUARD)
_t = seat_of(_g, Role.VILLAGER)
_g.wolf_agree_kill(_t)
_g.night_heal(_wi, _t)                  # 女巫救
_g.night_guard(_gu, _t)                 # 守卫守 —— 两个一起加在同一个人身上
_g.resolve_night()
record("G3", not _g.player(_t).alive,
       f"alive={_g.player(_t).alive}（同守同救必须仍死，否则「守着 + 救着」就无敌了）")

# G4 守卫当晚死掉，他这一晚的守护仍然生效
#
# 可观察的形式：守卫**守自己** + 狼刀守卫本人 ⇒ 他必须活下来。
# 若实现先把死亡结算掉再看守护（或者按"守卫死了 ⇒ 他的守护作废"处理），
# 这里就会死 —— 那正是这条判例要防的。
_g = Game(_B_GUARD, kinds=_K4, seed=68)
_g.start()
_gu = seat_of(_g, Role.GUARD)
_g.night_guard(_gu, _gu)
_g.wolf_agree_kill(_gu)
_g.resolve_night()
_rec = (_g.night_records or [{}])[-1]
record("G4", _g.player(_gu).alive and bool(_rec.get("saved_by_guard")),
       f"守卫 alive={_g.player(_gu).alive} night_record={_rec}")

# ---------------------------------------------------------------- 白痴
# I1 被投出 ⇒ 翻牌不死 + 失去投票权
_g = Game(_B_IDIOT, kinds=_K4, seed=69)
_g.start()
_id = seat_of(_g, Role.IDIOT)
peaceful_night(_g)
_g.open_discuss()
_g.open_vote()
for _p in _g.alive:
    if _p.seat != _id:
        _g.cast_vote(_p.seat, _id)
_exiled = _g.resolve_vote()
record("I1", _exiled == _id and _g.player(_id).alive
       and _g.private[_id].flipped and not _g.can_vote(_id),
       f"exiled={_exiled} alive={_g.player(_id).alive} flipped={_g.private[_id].flipped} "
       f"can_vote={_g.can_vote(_id)}")

# I3 已翻牌的白痴不能再被投票出局
#
# ⚠️ 这条的**口径**很关键：判例说的是"不能再**出局**"，不是"票不能再落到他身上"。
#    所以验的是可观察结果 —— 他活着、这一轮没有任何人出局、播报说明了原因。
#    `resolve_vote()` 的返回值仍然是"票落在谁身上"（= 他），那是返回值语义，
#    拿它当"有没有出局"的证据就会得出错误结论（我第一版就是这么误报的）。
_g.next_night()
peaceful_night(_g)
_g.open_discuss()
_g.open_vote()
for _p in _g.alive:
    if _p.seat != _id:
        _g.cast_vote(_p.seat, _id)
_exiled2 = _g.resolve_vote()
_all_alive = all(p.alive for p in _g.players)
_no_vote_death = not any(d.get("cause") == "vote" for d in _g.deaths)
_said = any("不再出局" in x for x in _g.public_log)
record("I3", _g.player(_id).alive and _all_alive and _no_vote_death and _said,
       f"票落点={_exiled2}（返回值，不代表出局）alive={_g.player(_id).alive} "
       f"全场无出局={_all_alive} 无 vote 死因={_no_vote_death} 播报说明={_said}")

# I2 翻牌之后**仍可发言**
#
# ⚠️ 这一条和 I1 是配套的：05b 写的是"翻牌不死，只失去**投票**权"，不是"闭嘴"。
#    实现上它很容易被一起砍掉（一行 `if not can_vote: return []` 就把它带走了）。
_g.next_night()
peaceful_night(_g)
_g.open_discuss()
_guard_loop = 0
while _g.current_speaker() is not None and _g.current_speaker() != _id and _guard_loop < 10:
    _g.log_speech(_g.current_speaker(), "过")
    _g.end_speech()
    _guard_loop += 1
_speaks = "speak" in _g.actions_for(_id)
record("I2", _g.current_speaker() == _id and _speaks,
       f"轮到白痴发言时 current={_g.current_speaker()} actions={_g.actions_for(_id)}")

# ---------------------------------------------------------------- 全局
# T1 平票默认无人出局
_g = Game(_B_IDIOT, kinds=_K4, seed=70)
_g.start()
peaceful_night(_g)
_g.open_discuss()
_g.open_vote()
_alive = [p.seat for p in _g.alive]
_g.cast_vote(_alive[0], _alive[1])       # 2 票投 A
_g.cast_vote(_alive[2], _alive[1])
_g.cast_vote(_alive[1], _alive[3])       # 2 票投 B
_g.cast_vote(_alive[3], _alive[3])
_exiled3 = _g.resolve_vote()
record("T1", _exiled3 is None and _g.tie_count == 1 and all(p.alive for p in _g.players),
       f"exiled={_exiled3} tie_count={_g.tie_count} votes={_g.vote_history[-1] if _g.vote_history else {}}")

# T2 允许投自己（自投 ≈ 弃权的一种表达）
_g = Game(_B_IDIOT, kinds=_K4, seed=71)
_g.start()
peaceful_night(_g)
_g.open_discuss()
_g.open_vote()
_v0 = next(p.seat for p in _g.alive)
_refused, _err = False, ""
try:
    _g.cast_vote(_v0, _v0)                  # 投自己
except Exception as e:                     # noqa: BLE001
    _refused, _err = True, f"{type(e).__name__}: {e}"
_counted = _g.votes.get(_v0) == _v0
_in_tally = f"{_v0 + 1}→{_v0 + 1}" in _g.tally_text()
record("T2", not _refused and _counted and _in_tally,
       f"被拒={_refused}{_err} votes={_g.votes} tally={_g.tally_text()}")

# T4 发言方式：默认依次轮转（不插队）；开了「被点名的人先回应」才插队
#
# 判例只说**行为**（谁先说），不绑实现：顺序变了就算一致，用什么数据结构插队不管。
def _discuss(g):
    g.start()
    g.night["kill"] = None
    g.resolve_night()
    g.open_discuss()
    return g


_g = _discuss(Game(_B_IDIOT, kinds=_K4, seed=72))
_o = list(_g.speaking_order)
_x = _g.current_speaker()
_y = next(s for s in _o if s != _x)
_g.log_speech(_x, f"我怀疑 {_y + 1} 号。")
_g.end_speech()
_rot_ok = _g.speaking_order == _o          # 默认档：一句都不插（旧行为）

_g = _discuss(Game(_B_IDIOT, kinds=_K4, seed=73, rules=Rules(speech_mode="challenge")))
_x2 = _g.current_speaker()
_y2 = next(s for s in _g.speaking_order if s != _x2)
_g.log_speech(_x2, f"我怀疑 {_y2 + 1} 号。")
_g.end_speech()
_ch_ok = _g.speaking_order[1:3] == [_y2, _x2]   # 被点的人先回应，然后点名的人再回应
record("T4", _rot_ok and _ch_ok,
       f"默认不插队={_rot_ok}；挑战式插队={_ch_ok} 顺序={_g.speaking_order}")

# ================================================================ 覆盖检查：表里的每一行都必须被验过
#
# 为什么这条必须有：断言是手写的、表是手写的，**两边会漂**。
# 上一批就出过"断言插在计数循环后面 ⇒ 没计数、也不让退出码变红"（等价于测试在骗我）。
# 这一条让"表里有、断言没有"直接变红。
_missing = [f"{s[1]}·{s[2]}" for s in SPEC if s[0] not in SEEN]
print("\n" + "=" * 74)
print("判例 → 结论（这张表由 SPEC + record() 生成，不会和实现漂开）")
print("=" * 74)
for _rid, _role, _text, _code in SPEC:
    _mark = "一致  " if SEEN.get(_rid) else "**不一致**"
    print(f"  [{_mark}] {_role} {_text}")
    print(f"            ↳ {_code}")
check_coverage = not _missing
if _missing:
    FAIL.append("表里有判例没被验过")
    print("\n!! 这些判例在表里但没被验过：" + "、".join(_missing))

print("\n" + "=" * 74)
print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 一致")
if FAIL:
    print("不一致的判例（先改代码或先改表——**不许为了让测试变绿而改断言**）：")
    for f in FAIL:
        print("  - " + f)
sys.exit(1 if FAIL else 0)
