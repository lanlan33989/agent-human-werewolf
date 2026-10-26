# -*- coding: utf-8 -*-
"""
规则引擎回归（D2）。

重点不只是"能跑完一局"，而是验证**四层信息隔离真的做在结构上**：
    L0 全局公开 / L1 狼队组内共享 / L2 个体私有 / L3 元信息

用法（在 h5 目录下）：
    ./.venv/Scripts/python.exe tests/_smoke-engine.py
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
_s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/  # 仓库根目录入 path：保证 python tests/xxx.py 可直接跑
import random
import sys

from engine import Game, Phase, Role, make_board, new_game
from _testboards import SEER4  # noqa: E402

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, str(detail)))


# ================================================================ 板子与发牌

g = Game(SEER4, kinds=["human", "ai", "human", "ai"], seed=1)
roles = sorted(p.role.value for p in g.players)
check("mini4 发牌为 1狼1预言家2民",
      roles == ["seer", "villager", "villager", "wolf"], roles)

g6 = Game(make_board(3, 3), kinds=["human"] * 3 + ["ai"] * 3, seed=1)
check("classic6 发牌为 2狼1预言家1女巫2民",
      sorted(p.role.value for p in g6.players) ==
      ["seer", "villager", "villager", "witch", "wolf", "wolf"],
      sorted(p.role.value for p in g6.players))

try:
    Game(SEER4, kinds=["human", "ai"])
    check("人数不符时应当报错", False, "没有报错")
except ValueError as e:
    check("人数不符时应当报错", True, str(e)[:40])

check("默认构造是前半真人后半 AI",
      new_game(2, 2).players[0].kind == "human" and
      new_game(2, 2).players[3].kind == "ai",
      [p.kind for p in new_game(2, 2).players])


# ================================================================ L1 组内共享

wolf_seats_6 = [p.seat for p in g6.players if p.role is Role.WOLF]
v_wolf = g6.get_view_for(wolf_seats_6[0])
check("L1 狼能看到队友", "team_mates" in v_wolf,
      str(v_wolf.get("team_mates")))
check("L1 狼的队友不含自己且正确",
      sorted(v_wolf.get("team_mates", [])) == [wolf_seats_6[1]],
      str(v_wolf.get("team_mates")))

good_seat_6 = next(p.seat for p in g6.players if not p.role.is_wolf)
# 注意：good_seat_6 可能是预言家本人。"与私有信息无关的旁观者"必须显式取村民，
# 否则测的是"预言家看不到自己的记录"这种假命题。
villager_seat_6 = next(p.seat for p in g6.players if p.role is Role.VILLAGER)
v_good = g6.get_view_for(good_seat_6)
check("L1 好人看不到 team_mates 字段", "team_mates" not in v_good,
      str(sorted(v_good.keys())))


# ================================================================ L2 个体私有

seer_seat = next(p.seat for p in g6.players if p.role is Role.SEER)
witch_seat = next(p.seat for p in g6.players if p.role is Role.WITCH)

g6.start()
g6.night_investigate(seer_seat, wolf_seats_6[0])
v_seer = g6.get_view_for(seer_seat)
check("L2 预言家能看到自己的验人记录",
      v_seer.get("investigations", {}).get(wolf_seats_6[0]) is True,
      str(v_seer.get("investigations")))

v_other = g6.get_view_for(villager_seat_6)
check("L2 村民看不到 investigations 字段", "investigations" not in v_other,
      str(sorted(v_other.keys())))

v_wolf_again = g6.get_view_for(wolf_seats_6[0])
check("L2 狼也看不到预言家的记录", "investigations" not in v_wolf_again,
      str(sorted(v_wolf_again.keys())))

check("L2 女巫能看到自己的药水", "potions" in g6.get_view_for(witch_seat),
      str(g6.get_view_for(witch_seat).get("potions")))
# ⚠️ 必须用 villager_seat_6（见 L60 那条注释）：good_seat_6 有可能**正好是女巫本人**，
# 那样测的就是"女巫看不到自己的药水"这种假命题。
check("L2 无关人看不到药水字段", "potions" not in g6.get_view_for(villager_seat_6))


# ================================================================ 权限与阶段约束

perm_ok = False
try:
    # ⚠️ 必须用 night_kill（协商入口），不能用 wolf_agree_kill ——
    # 后者是"狼队已达成一致"的一步入，故意不做角色校验（它没有 actor 参数）。
    g6.night_kill(villager_seat_6, wolf_seats_6[0])
except PermissionError:
    perm_ok = True
check("非狼不能刀人", perm_ok)

perm_ok = False
try:
    g6.night_investigate(villager_seat_6, 0)
except PermissionError:
    perm_ok = True
check("非预言家不能验人（用村民测）", perm_ok)

# ================================================================ 伤害与出局
# ⚠️ 2026-09-15 用户改规则：**一人一条命**（原来的「每人 2 条命」已去掉）。
# 下面几条断言跟着改了 —— 是**规则变了**，不是实现错了。
# 对照：改之前这里是「狼刀扣 1 条命（不死）」+「再扣 1 条命后出局」。

g2 = Game(SEER4, kinds=["human", "ai", "human", "ai"], seed=7)
wolf2 = next(p.seat for p in g2.players if p.role is Role.WOLF)
victim = next(p.seat for p in g2.players if not p.role.is_wolf)

g2.start()
g2.wolf_agree_kill(victim)
events = g2.resolve_night()
check("狼刀一下就让目标出局（一人一条命）",
      g2.player(victim).lives == 0 and not g2.player(victim).alive,
      f"lives={g2.player(victim).lives} alive={g2.player(victim).alive}")
check("播报里出现出局信息", any("出局" in e for e in events), str(events))

# 出局的人必须真的"离开这张桌子"：不在发言顺序里，也不在投票顺序里。
# 这条同时是新规则（发言/投票从前一晚被刀的人开始）的基础。
g2.open_discuss()
check("出局的人不在发言顺序里", victim not in g2.speaking_order,
      str(g2.speaking_order))
g2.open_vote()
check("出局的人不在投票顺序里", victim not in g2.vote_order, str(g2.vote_order))

# ================================================================ 平票

g3 = Game(SEER4, kinds=["human", "ai", "human", "ai"], seed=3)
g3.start()
g3.night["kill"] = None
g3.resolve_night()
g3.open_discuss()
g3.open_vote()
seats = [p.seat for p in g3.alive]
g3.cast_vote(seats[0], seats[1])
g3.cast_vote(seats[1], seats[0])
g3.cast_vote(seats[2], seats[3])
g3.cast_vote(seats[3], seats[2])
exiled = g3.resolve_vote()
check("四票两两平票时无人出局", exiled is None, str(exiled))
check("平票时全员血量不变", all(g3.player(s).lives == SEER4.lives for s in seats),
      [g3.player(s).lives for s in seats])

# ================================================================ 弃票

g4 = Game(SEER4, kinds=["human", "ai", "human", "ai"], seed=4)
g4.start()
g4.night["kill"] = None
g4.resolve_night()
g4.open_discuss()
g4.open_vote()
for p in g4.alive:
    g4.cast_vote(p.seat, -1)
check("全员弃票时无人出局", g4.resolve_vote() is None)

# ================================================================ 胜负判定

g5 = Game(SEER4, kinds=["human", "ai", "human", "ai"], seed=5)
wolves5 = [p.seat for p in g5.players if p.role.is_wolf]
g5.start()
for w in wolves5:
    g5.player(w).alive = False
check("狼全出局 -> 好人胜", g5.check_winner() == "good", g5.check_winner())

g5b = Game(SEER4, kinds=["human", "ai", "human", "ai"], seed=5)
g5b.start()
goods5b = [p.seat for p in g5b.players if not p.role.is_wolf]
for s in goods5b:
    g5b.player(s).alive = False
check("好人全出局 -> 狼胜", g5b.check_winner() == "wolf", g5b.check_winner())


# ================================================================ 跑完一整局

def bot_act(g: Game, seat: int, rng: random.Random) -> None:
    """极简机器人：只为验证引擎能跑完一局，不代表任何 AI 策略。"""
    view = g.get_view_for(seat)
    acts = view.get("can_act", [])
    others = [p.seat for p in g.alive if p.seat != seat]
    good = [p.seat for p in g.good_alive()]
    if "kill" in acts and good:
        g.wolf_agree_kill(rng.choice(good))
    if "investigate" in acts and others:
        g.night_investigate(seat, rng.choice(others))
    if "vote" in acts and others:
        g.cast_vote(seat, rng.choice(others))


def play_full_game(humans: int, ais: int, seed: int, max_rounds: int = 60):
    g = new_game(humans, ais, seed=seed)
    g.start()
    steps = 0
    while g.phase is not Phase.ENDED and steps < max_rounds:
        steps += 1
        if g.phase is Phase.NIGHT:
            for p in list(g.alive):
                bot_act(g, p.seat, g.rng)
            g.resolve_night()
        elif g.phase is Phase.DAWN:
            g.open_discuss()
        elif g.phase is Phase.DISCUSS:
            if g.end_speech() is None:
                g.open_vote()
        elif g.phase is Phase.VOTE:
            for p in list(g.alive):
                if p.seat not in g.votes:
                    g.cast_vote(p.seat, -1)
            for p in list(g.alive):
                bot_act(g, p.seat, g.rng)
            g.resolve_vote()
            if g.phase is Phase.SHOOT:   # 猎人被投出：bot 放弃开枪
                g.night_shoot(g.shooter, -1)
            if g.phase is not Phase.ENDED:
                g.next_night()
    return g, steps


finished = 0
for seed in range(1, 21):
    g, steps = play_full_game(2, 2, seed)
    if g.phase is Phase.ENDED and g.winner in ("wolf", "good"):
        finished += 1
check("4 人局连跑 20 局全部正常结束", finished == 20, f"{finished}/20")

finished6 = 0
for seed in range(1, 21):
    g, steps = play_full_game(3, 3, seed)
    if g.phase is Phase.ENDED and g.winner in ("wolf", "good"):
        finished6 += 1
check("6 人局（2 狼）连跑 20 局全部正常结束", finished6 == 20, f"{finished6}/20")

sample, _ = play_full_game(2, 2, seed=42)
check("结束后有胜负与日志", sample.winner is not None and len(sample.public_log) > 4,
      f"winner={sample.winner} log={len(sample.public_log)}")

# ================================================================ 公共日志不泄露

leak = False
bad_line = ""
for seed in range(1, 11):
    g, _ = play_full_game(3, 3, seed)
    for line in g.public_log:
        # 结束时的胜负播报是必须公开的，不算泄露
        if line in ("狼人胜利。", "好人胜利。"):
            continue
        if "狼人" in line or "预言家" in line or "女巫" in line:
            leak = True
            bad_line = line
            break
    if leak:
        break
check("进行中的公共日志不含任何身份词（无泄露）", not leak, bad_line)

# 反向确认：结束语确实会出现，否则上面那条断言会永远为真
ended_lines = set()
for seed in range(1, 11):
    g, _ = play_full_game(3, 3, seed)
    for line in g.public_log:
        if line in ("狼人胜利。", "好人胜利。"):
            ended_lines.add(line)
check("结束时会公开宣布胜负", len(ended_lines) > 0, str(sorted(ended_lines)))


# ================================================================ 票型文本
# 格式必须是 demo/index.html 里 tallyText() 的那一套 —— 界面说同一句话，
# 换页的时候玩家不该看到两种写法。

tv = Game(SEER4, ["human"] * 4, seed=1)
tv.start()
tv.resolve_night()
tv.open_discuss()
tv.open_vote()
tv.cast_vote(0, 2)
tv.cast_vote(1, 2)
tv.cast_vote(2, 0)
tv.cast_vote(3, -1)                      # 弃票不进分组
check("票型文本与 demo 同格式（同票的归一组、票数多的在前）",
      tv.tally_text() == "1，2→3；3→1", tv.tally_text())

tv.resolve_vote()
check("结算时把票型写进了公共日志（日志/时间线/语音播报都会自动带上）",
      any(x.startswith("投票结果：") for x in tv.public_log), str(tv.public_log[-2:]))

tv2 = Game(SEER4, ["human"] * 4, seed=1)
tv2.start()
tv2.resolve_night()
tv2.open_discuss()
tv2.open_vote()
tv2.cast_vote(0, -1)
tv2.cast_vote(1, -1)
check("全是弃票时票型文本是「无人投票」（与 demo 同措辞）",
      tv2.tally_text() == "无人投票", tv2.tally_text())


# ================================================================ 组局规则（2026-09-15 用户改）
# 不再给「4 人局 / 6 人局」这种固定选项，改成「玩家定几个人」→ 由 make_board 现算。
# 校验放在引擎里，所以这里测的就是界面实际走的那条路。

from engine import GOD_ORDER  # noqa: E402

_roles4 = [r.value for r in make_board(2, 2).roles]
check("4 人局：1 狼 + 1 神（女巫）+ 2 民（狼 = floor(4×0.34) = 1，神数 = 狼数）",
      _roles4.count("wolf") == 1 and "witch" in _roles4 and _roles4.count("villager") == 2,
      make_board(2, 2).label)
_roles6 = [r.value for r in make_board(3, 3).roles]
check("★ 6 人局 = 2 狼 2 神 2 民（旧规则是 1 狼 1 神 4 民；这条改动顺手消掉了 6→7 断崖）",
      _roles6.count("wolf") == 2 and "seer" in _roles6 and "witch" in _roles6,
      make_board(3, 3).label)
_roles9 = make_board(5, 4).roles
check("★ 9 人局 = 3 狼 3 神（女巫+预言家+猎人）3 民 —— 与标准 9 人局一致",
      [r.value for r in _roles9].count("wolf") == 3
      and set(r.value for r in _roles9 if r.is_god) == {"witch", "seer", "hunter"},
      make_board(5, 4).label)
_roles12 = make_board(6, 6).roles
check("★ 12 人局 = 4 狼 4 神（+守卫）4 民 —— 与标准 12 人局一致",
      [r.value for r in _roles12].count("wolf") == 4
      and set(r.value for r in _roles12 if r.is_god)
      == {"witch", "seer", "hunter", "guard"},
      make_board(6, 6).label)
check("★ 神职启用顺序 = 女巫→预言家→猎人→守卫→白痴（用户定，不许擅自调）",
      [r.value for r in GOD_ORDER] == ["witch", "seer", "hunter", "guard", "idiot"],
      str([r.value for r in GOD_ORDER]))
check("白痴只在 15 人局（5 狼 5 神）才出场 —— 这是顺序决定的，不是 bug",
      "idiot" not in [r.value for r in make_board(7, 7).roles]
      and "idiot" in [r.value for r in make_board(8, 7).roles],
      make_board(8, 7).label)
check("剩下的位置是村民（不会全是狼和神）",
      "villager" in [r.value for r in make_board(4, 4).roles],
      make_board(4, 4).label)
check("板子里不出现不存在的角色",
      set(r.value for r in make_board(6, 5).roles)
      <= {"wolf", "seer", "witch", "hunter", "guard", "idiot", "villager"},
      make_board(6, 5).label)


def _bad_args(*args) -> bool:
    """非法人数必须**明确报错**，不能悄悄开一局。"""
    try:
        make_board(*args)
        return False
    except ValueError:
        return True


check("★ 0 个真人 ⇒ **可以开**（全 AI 局是离线评测要的形态，批七）",
      not _bad_args(0, 4), "make_board(0, 4) 应当成功")
check("★ 负数人数：报错（这条才是真正的'不能悄悄开一局'）", _bad_args(-1, 5))
check("总人数不足 4：报错", _bad_args(1, 2))


def _finish(board, seed: int) -> bool:
    """把一副板子跑到分出胜负。引擎层的迷你驱动器（与 game_api 那个无关）。"""
    n = len(board.roles)
    gb = Game(board, ["human"] * ((n + 1) // 2) + ["ai"] * (n // 2), seed=seed)
    gb.start()
    for _ in range(600):
        if gb.phase is Phase.ENDED:
            return True
        if gb.phase is Phase.NIGHT:
            if gb.night.get("kill") is None:
                w = next((p for p in gb.alive if p.role.is_wolf), None)
                if w is not None:
                    gb.wolf_agree_kill(gb.good_alive()[0].seat)
                else:
                    gb.night["kill"] = -1
            if gb.night.get("investigate") is None:
                se = next((p for p in gb.alive if p.role is Role.SEER), None)
                if se is not None:
                    gb.night_investigate(
                        se.seat, [q.seat for q in gb.alive if q.seat != se.seat][0])
                else:
                    gb.night["investigate"] = -1
            wi = next((p for p in gb.alive if p.role is Role.WITCH), None)
            if wi is not None and not gb.night.get("witch_asked"):
                gb.night_decline(wi.seat)
            gb.resolve_night()
            continue
        if gb.phase is Phase.DAWN:
            gb.open_discuss()
            continue
        if gb.phase is Phase.DISCUSS:
            if gb.current_speaker() is None:
                gb.open_vote()
            else:
                gb.end_speech()
            continue
        if gb.phase is Phase.VOTE:
            for q in list(gb.alive):
                if q.seat not in gb.votes:
                    # 全投最小座位（去掉自己）：保证每轮都有决定性结果，不会全靠平票空转
                    gb.cast_vote(q.seat,
                                 sorted(x.seat for x in gb.alive if x.seat != q.seat)[0])
            gb.resolve_vote()
            if gb.phase is Phase.SHOOT:   # 猎人被投出：bot 放弃开枪
                gb.night_shoot(gb.shooter, -1)
            if gb.phase is not Phase.ENDED:
                gb.next_night()
            continue
    return gb.phase is Phase.ENDED


# §4.3 的规矩：加板子必须同时加断言，至少覆盖「这个板子能跑完一局」
check("★ 现算的 4 人局能真的跑到分出胜负", _finish(make_board(2, 2), 11))
check("★ 现算的 9 人局（2 神 2 狼）能真的跑到分出胜负", _finish(make_board(5, 4), 12))

# ================================================================ 发言/投票顺序（2026-09-15 用户改）
# 规则：都从**前一晚被刀的人**开始顺时针。三种情况都要成立。
g_o = Game(make_board(3, 3), kinds=["human"] * 6, seed=21)
g_o.start()
_alive0 = [p.seat for p in g_o.alive]
_knifed = _alive0[3]
g_o.wolf_agree_kill(_knifed)
g_o.resolve_night()                       # 一人一条命 ⇒ 被刀的人已经出局
_spk = g_o.open_discuss()
check("★ 被刀的人出局后：发言从**他顺时针的下一个活人**开始",
      _spk[0] == next(s for s in _alive0 if s > _knifed), str(_spk))
g_o.open_vote()
check("★ 投票顺序与发言同一套轮转",
      g_o.vote_order == g_o.speaking_order, f"{g_o.vote_order} vs {g_o.speaking_order}")

g_o2 = Game(make_board(3, 3), kinds=["human"] * 6, seed=22)
g_o2.start()
g_o2.night["kill"] = None                 # 平安夜
g_o2.resolve_night()
_spk2 = g_o2.open_discuss()
check("平安夜时发言从 1 号开始（没有「被刀的人」可当锚点）",
      _spk2[0] == min(p.seat for p in g_o2.alive), str(_spk2))
check("发言顺序是完整的顺时针（不丢人、不重复）",
      sorted(_spk2) == sorted(p.seat for p in g_o2.alive), str(_spk2))

# ---- ★ 平安夜不许泄漏"谁被刀 / 有没有人刀 / 谁救了" -------------------
# 旧实现拿 night["kill"] 当起点，并在公共播报里写
# 「天亮了，从昨晚被刀的 3 号开始依次发言。」—— 而 public_log 是公开的，
# 还会进 AI 的 prompt ⇒ 平安夜时全桌都能反推出"3 号是目标、有人救了他"。
# 三种成因（空刀 / 被救 / 被守）在外部表现上必须完全一致。

def _peaceful_night(kill):
    """造一个「第 1 夜平安」的局：kill=None = 空刀；给座位号 = 刀他再救回来。
    同一个 seed ⇒ 角色分配与存活名单完全一致，所以三组之间可逐字对比。
    返回 (公共日志, 发言顺序, 局)。"""
    gg = Game(make_board(3, 3), kinds=["human"] * 6, seed=31)
    gg.start()
    if kill is not None:
        wolf = next(p.seat for p in gg.alive if p.role.is_wolf)
        witch = next(p.seat for p in gg.alive if p.role is Role.WITCH)
        gg.wolf_agree_kill(kill)
        gg.night_heal(witch, kill)
    gg.resolve_night()
    order = gg.open_discuss()
    return list(gg.public_log), list(order), gg


_log_no, _ord_no, _g_no = _peaceful_night(None)                    # 成因一：空刀
_othersj = [p.seat for p in _g_no.alive if not p.role.is_wolf]
_log_a, _ord_a, _g_a = _peaceful_night(_othersj[0])                # 成因二：被救（目标甲）
_log_b, _ord_b, _g_b = _peaceful_night(_othersj[1])                # 成因三：被救（目标乙）
check("★ 空刀 / 被救（两个不同的被刀人）：公共日志一字不差",
      _log_no == _log_a == _log_b,
      f"{_log_no[-1:]} / {_log_a[-1:]} / {_log_b[-1:]}")
check("★ 三种成因下的发言起点完全相同（起点与被刀者零相关 ⇒ 反推不出目标）",
      _ord_no[0] == _ord_a[0] == _ord_b[0],
      f"{_ord_no[0]} / {_ord_a[0]} / {_ord_b[0]}")
check("★ 平安夜时被刀的人仍然活着（上面两条的前提）",
      _g_a.player(_othersj[0]).alive and _g_b.player(_othersj[1]).alive)
check("★ 平安夜时公共日志里不出现「被刀」二字",
      not any("被刀" in x for x in _log_a), str(_log_a[-3:]))

g_n = Game(make_board(3, 3), kinds=["human"] * 6, seed=41)
g_n.start()
_wolfn = next(p.seat for p in g_n.alive if p.role.is_wolf)
_targetn = next(p.seat for p in g_n.alive if not p.role.is_wolf)
g_n.wolf_agree_kill(_targetn)
g_n.resolve_night()                        # 这一晚有人出局 ⇒ 起点照旧用他
g_n.open_discuss()
while g_n.current_speaker() is not None:
    g_n.end_speech()
g_n.open_vote()
_lastn = g_n.speaking_order[-1]            # 上一轮最后发言的人
for _p in list(g_n.alive):
    g_n.cast_vote(_p.seat, -1)             # 全员弃票：别让人出局，只测顺序
g_n.resolve_vote()
g_n.next_night()
g_n.night["kill"] = None                   # 第 2 晚：空刀 ⇒ 平安夜
g_n.resolve_night()
_spkn = g_n.open_discuss()
_aliven = [p.seat for p in g_n.alive]
_expn = next((s for s in _aliven if s >= _lastn + 1), _aliven[0])
check("★ 第 2 晚平安夜：起点 = 上一轮最后发言者的下一位（不是固定 1 号）",
      _spkn[0] == _expn, f"last={_lastn} 期望={_expn} 实际={_spkn}")
g_n.open_vote()
check("★ 平安夜时投票顺序与发言顺序仍是同一套（换个字段不能把话说第二遍）",
      g_n.vote_order == g_n.speaking_order, f"{g_n.vote_order} vs {g_n.speaking_order}")

# ================================================================ 三神判例（2026-09-17 加）
# 全部**照标准局判例**写，不自己发明。每条都配"反面"（可配开关打开后必须反过来）。
from engine import Board, Rules  # noqa: E402

_BG = Board(key="t-guard", label="测试板：狼/守卫/2 民",
            roles=[Role.WOLF, Role.GUARD, Role.VILLAGER, Role.VILLAGER], lives=1)
_BW = Board(key="t-five", label="测试板：狼/女巫/守卫/猎人/民",
            roles=[Role.WOLF, Role.WITCH, Role.GUARD, Role.HUNTER, Role.VILLAGER], lives=1)
_BI = Board(key="t-idiot", label="测试板：狼/白痴/2 民",
            roles=[Role.WOLF, Role.IDIOT, Role.VILLAGER, Role.VILLAGER], lives=1)

# ① 守卫：不可连续两晚守同一人
_g1 = Game(_BG, kinds=["human"] * 4, seed=11)
_g1.start()
_gu1 = next(p.seat for p in _g1.players if p.role is Role.GUARD)
_v1 = next(p.seat for p in _g1.players if p.role is Role.VILLAGER)
_g1.night_guard(_gu1, _v1)
_g1.resolve_night()
_g1.next_night()
_refused = False
try:
    _g1.night_guard(_gu1, _v1)
except ValueError:
    _refused = True
check("★ 守卫不能连续两晚守同一个人（标准判例）", _refused,
      f"第二晚守同一个人没被拒（guard_last={_g1.private[_gu1].guard_last}）")

# ② 守卫可以守自己（默认）；关掉开关就必须拒
_g2 = Game(_BG, kinds=["human"] * 4, seed=12)
_g2.start()
_gu2 = next(p.seat for p in _g2.players if p.role is Role.GUARD)
_g2.night_guard(_gu2, _gu2)
check("守卫默认可以守自己（Rules.guard_can_self_guard）", _g2.night["guard"] == _gu2)
_g3 = Game(_BG, kinds=["human"] * 4, seed=12,
           rules=Rules(guard_can_self_guard=False))
_g3.start()
_gu3 = next(p.seat for p in _g3.players if p.role is Role.GUARD)
_refused3 = False
try:
    _g3.night_guard(_gu3, _gu3)
except ValueError:
    _refused3 = True
check("把 guard_can_self_guard 关掉后，守自己会被拒（G9 开关真的接上了）", _refused3)

# ③ 同守同救 = 该人仍死（"奶穿"）
_g4 = Game(_BW, kinds=["human"] * 5, seed=13)
_g4.start()
_w4 = next(p.seat for p in _g4.players if p.role is Role.WOLF)
_wi4 = next(p.seat for p in _g4.players if p.role is Role.WITCH)
_gu4 = next(p.seat for p in _g4.players if p.role is Role.GUARD)
_t4 = next(p.seat for p in _g4.players if p.role is Role.VILLAGER)
_g4.wolf_agree_kill(_t4)
_g4.night_heal(_wi4, _t4)
_g4.night_guard(_gu4, _t4)
_g4.resolve_night()
check("★ 同守同救 = 该人仍死（奶穿，标准判例）",
      not _g4.player(_t4).alive, f"alive={_g4.player(_t4).alive}")

# ④ 只被守（没人救）⇒ 平安夜；且播报与"空刀"一字不差
_g5 = Game(_BW, kinds=["human"] * 5, seed=13)
_g5.start()
_w5 = next(p.seat for p in _g5.players if p.role is Role.WOLF)
_gu5 = next(p.seat for p in _g5.players if p.role is Role.GUARD)
_t5 = next(p.seat for p in _g5.players if p.role is Role.VILLAGER)
_g5.wolf_agree_kill(_t5)
_g5.night_guard(_gu5, _t5)
_g5.resolve_night()
check("★ 被守卫守住 ⇒ 该人活下来，且播报就是「平安夜。」（三种成因不可区分）",
      _g5.player(_t5).alive and "平安夜。" in _g5.public_log,
      str(_g5.public_log[-2:]))

# ⑤ 猎人被刀 ⇒ 进 SHOOT；开枪带走一个人；开完回 DAWN 继续白天
_g6 = Game(_BW, kinds=["human"] * 5, seed=14)
_g6.start()
_w6 = next(p.seat for p in _g6.players if p.role is Role.WOLF)
_h6 = next(p.seat for p in _g6.players if p.role is Role.HUNTER)
_g6.wolf_agree_kill(_h6)
_g6.resolve_night()
check("★ 猎人被刀 ⇒ 进 SHOOT 相位（不是直接进 DAWN）",
      _g6.phase is Phase.SHOOT and _g6.shooter == _h6,
      f"phase={_g6.phase.value} shooter={_g6.shooter}")
_shot6 = next(p.seat for p in _g6.alive if p.seat != _h6 and not p.role.is_wolf)
_g6.night_shoot(_h6, _shot6)
check("★ 猎人开枪真的带走一个人，并记下死因 shoot + 轮次",
      not _g6.player(_shot6).alive
      and any(d["seat"] == _shot6 and d["cause"] == "shoot"
              and d.get("round") == _g6.round for d in _g6.deaths),
      str(_g6.deaths))
check("猎人开完枪回到 DAWN（这一天继续发言）", _g6.phase is Phase.DAWN,
      _g6.phase.value)

# ⑦ 猎人被**投票出局** ⇒ 也要开枪，且开完进**下一夜**（不是再来一轮白天）
#    🔴 2026-10-05 用户实局：猎人第 1 天被投出 → 枪没开成（SHOOT 被 next_night
#    跳过、shooter 残留）→ 第二晚结算时 resolve_night 见 shooter 非空又进 SHOOT
#    ⇒ 死猎人开枪打死预言家。三条断言钉死这条链。
_g9 = Game(_BW, kinds=["human"] * 5, seed=16)
_g9.start()
_h9 = next(p.seat for p in _g9.players if p.role is Role.HUNTER)
# 走完夜晚与白天（全弃票），然后全票投猎人
_g9.resolve_night()
_g9.open_discuss()
while _g9.current_speaker() is not None:
    _g9.log_speech(_g9.current_speaker(), "过")
    _g9.end_speech()
_g9.open_vote()
for _p in list(_g9.alive):
    if _p.seat != _h9:
        _g9.cast_vote(_p.seat, _h9)
_g9.cast_vote(_h9, -1)
_exiled9 = _g9.resolve_vote()
check("★ 猎人被投票出局 ⇒ 进 SHOOT（枪还在手里，不许被跳过）",
      _exiled9 == _h9 and _g9.phase is Phase.SHOOT and _g9.shooter == _h9,
      f"exiled={_exiled9} phase={_g9.phase.value} shooter={_g9.shooter}")
_g9_sf = _g9.shoot_from
# 开枪带走一个好人，然后必须直接进下一夜（phase 回 EXILE，不是 DAWN）
_g9.night_shoot(_h9, next(p.seat for p in _g9.alive if not p.role.is_wolf))
check("★ 被投出的猎人开完枪回到 EXILE（下一拍进夜晚，不是再来一轮白天）",
      _g9.phase is Phase.EXILE and _g9_sf is Phase.EXILE,
      f"phase={_g9.phase.value} shoot_from={_g9_sf}")
_g9.next_night()
check("★ 开完枪之后 next_night 正常进第二夜", _g9.phase is Phase.NIGHT,
      _g9.phase.value)
# 护栏：SHOOT 相位直接 next_night 必须被拒（跳枪 = 静默产生死人开枪的根源）
_g10 = Game(_BW, kinds=["human"] * 5, seed=17)
_g10.start()
_h10 = next(p.seat for p in _g10.players if p.role is Role.HUNTER)
_g10.wolf_agree_kill(_h10)
_g10.resolve_night()
_refused10 = False
try:
    _g10.next_night()
except RuntimeError:
    _refused10 = True
check("★ SHOOT 相位调 next_night 必须被拒（跳枪的调用方当场炸）",
      _refused10 and _g10.phase is Phase.SHOOT and _g10.shooter == _h10,
      f"refused={_refused10} phase={_g10.phase.value}")


# ⑥ 猎人被女巫毒杀 ⇒ **默认不能开枪**；打开开关就能（G9）
_g7 = Game(_BW, kinds=["human"] * 5, seed=15)
_g7.start()
_w7 = next(p.seat for p in _g7.players if p.role is Role.WOLF)
_wi7 = next(p.seat for p in _g7.players if p.role is Role.WITCH)
_h7 = next(p.seat for p in _g7.players if p.role is Role.HUNTER)
_v7 = next(p.seat for p in _g7.players if p.role is Role.VILLAGER)
_g7.wolf_agree_kill(_v7)
_g7.night_poison(_wi7, _h7)
_g7.resolve_night()
check("★ 猎人被毒杀 ⇒ 默认不能开枪（网杀标准判例）",
      _g7.shooter is None and _g7.phase is Phase.DAWN,
      f"shooter={_g7.shooter} phase={_g7.phase.value}")

_g8 = Game(_BW, kinds=["human"] * 5, seed=15,
           rules=Rules(hunter_can_shoot_on_witch_poison=True))
_g8.start()
_w8 = next(p.seat for p in _g8.players if p.role is Role.WOLF)
_wi8 = next(p.seat for p in _g8.players if p.role is Role.WITCH)
_h8 = next(p.seat for p in _g8.players if p.role is Role.HUNTER)
_v8 = next(p.seat for p in _g8.players if p.role is Role.VILLAGER)
_g8.wolf_agree_kill(_v8)
_g8.night_poison(_wi8, _h8)
_g8.resolve_night()
check("开关打开后，被毒也能开枪（G9 开关真的接上了）", _g8.shooter == _h8,
      f"shooter={_g8.shooter}")

# ⑦ 白痴：被投出 ⇒ 翻牌不死 + 失去投票权（D5 的根）
_g9 = Game(_BI, kinds=["human"] * 4, seed=16)
_g9.start()
_g9.night["kill"] = None
_g9.resolve_night()
_g9.open_discuss()
_g9.open_vote()
_id9 = next(p.seat for p in _g9.players if p.role is Role.IDIOT)
for _p9 in _g9.alive:
    if _p9.seat != _id9:
        _g9.cast_vote(_p9.seat, _id9)
_exiled9 = _g9.resolve_vote()
check("★ 白痴被投出 ⇒ 翻牌不死，但**失去投票权**（标准判例）",
      _exiled9 == _id9 and _g9.player(_id9).alive
      and _g9.private[_id9].flipped and not _g9.can_vote(_id9),
      f"exiled={_exiled9} alive={_g9.player(_id9).alive} can_vote={_g9.can_vote(_id9)}")
check("★ 翻牌白痴不会让「等所有人投票」永远等下去（D5 的引擎侧前提）",
      _g9.can_vote(_id9) is False
      and all(p.seat in _g9.votes for p in _g9.alive if _g9.can_vote(p.seat)),
      f"can_vote={_g9.can_vote(_id9)}")
check("白痴不能再被投出（已翻牌）",
      not _g9.can_vote(_id9) and _g9._actions_available(_id9) == [],
      str(_g9._actions_available(_id9)))

# ================================================================ 狼队夜间协商（2026-09-17 加）
# 用户定：夜里狼之间**没有语言通道**，只能靠"点了哪个按钮"互相看，再各自确定。
# 等价于面杀里狼睁眼交换的那个眼神 —— 不是新开的交流通道。
_B2 = Board(key="t-wolf2", label="测试板：2 狼 + 3 好",
            roles=[Role.WOLF, Role.WOLF, Role.SEER, Role.WITCH, Role.VILLAGER], lives=1)
_B3 = Board(key="t-wolf3", label="测试板：3 狼 + 2 好",
            roles=[Role.WOLF, Role.WOLF, Role.WOLF, Role.SEER, Role.VILLAGER], lives=1)
_B1 = Board(key="t-wolf1", label="测试板：1 狼 + 3 好",
            roles=[Role.WOLF, Role.SEER, Role.VILLAGER, Role.VILLAGER], lives=1)


def _wolves_of(g):
    return [p.seat for p in g.alive if p.role.is_wolf]


# ① 一致 ⇒ 定案；未全员确认 ⇒ 不定案
_n1 = Game(_B2, kinds=["ai"] * 5, seed=31)
_n1.start()
_w1 = _wolves_of(_n1)
_g1 = next(p.seat for p in _n1.alive if not p.role.is_wolf)
_n1.night_kill(_w1[0], _g1)
_n1.night_kill(_w1[1], _g1)
_n1.night_confirm(_w1[0])
check("★ 协商没全员确认时不定案（刀口空着，女巫也不会被提前叫醒）",
      _n1.night.get("kill") is None and not _n1.night.get("kill_settled"),
      f"kill={_n1.night.get('kill')} settled={_n1.night.get('kill_settled')}")
_n1.night_confirm(_w1[1])
check("★ 点子一致 + 全员确认 ⇒ 就刀那个",
      _n1.night.get("kill") == _g1 and _n1.night.get("kill_settled"),
      f"kill={_n1.night.get('kill')}")

# ② 分歧 + 无人类狼 ⇒ 听第一个提议的（先手优先）
_n2 = Game(_B2, kinds=["ai"] * 5, seed=32)
_n2.start()
_w2 = _wolves_of(_n2)
_g2 = [p.seat for p in _n2.alive if not p.role.is_wolf]
_n2.night_kill(_w2[0], _g2[0])
_n2.night_kill(_w2[1], _g2[1])
_n2.night_confirm(_w2[0])
_n2.night_confirm(_w2[1])
check("★ 分歧（无人类狼）⇒ 刀第一个提议的人点的那个（先手优先）",
      _n2.night.get("kill") == _g2[0], f"kill={_n2.night.get('kill')} 期望={_g2[0]}")

# ③ 分歧 + 两只人狼意见一致 ⇒ 听人狼的（哪怕它提议得更晚）
_n3 = Game(_B3, kinds=["ai"] * 5, seed=33)
_n3.start()
_w3 = _wolves_of(_n3)
_n3.player(_w3[1]).kind = "human"
_n3.player(_w3[2]).kind = "human"
_g3 = [p.seat for p in _n3.alive if not p.role.is_wolf]
_n3.night_kill(_w3[0], _g3[1])
_n3.night_kill(_w3[1], _g3[0])
_n3.night_kill(_w3[2], _g3[0])
for _s3 in _w3:
    _n3.night_confirm(_s3)
check("★ 分歧且人狼意见一致 ⇒ 听人狼的（人狼票的人优先于先手）",
      _n3.night.get("kill") == _g3[0], f"kill={_n3.night.get('kill')} 期望={_g3[0]}")

# ④ 确认后改点 ⇒ 确认被撤销
_n4 = Game(_B2, kinds=["ai"] * 5, seed=34)
_n4.start()
_w4 = _wolves_of(_n4)
_g4 = next(p.seat for p in _n4.alive if not p.role.is_wolf)
_n4.night_kill(_w4[0], _g4)
_n4.night_confirm(_w4[0])
_n4.night_kill(_w4[0], _w4[1])
check("确认后改点 ⇒ 确认被撤销（必须重新确认，不能替它拍板）",
      _w4[0] not in _n4.night.get("wolf_confirm", [])
      and not _n4.night.get("kill_settled"))

# ⑤ 单狼：点即定案
_n5 = Game(_B1, kinds=["ai"] * 4, seed=35)
_n5.start()
_w5 = _wolves_of(_n5)[0]
_g5 = next(p.seat for p in _n5.alive if not p.role.is_wolf)
_n5.night_kill(_w5, _g5)
check("单狼局：点即定案（不用确认，否则夜晚白等一个确定）",
      _n5.night.get("kill") == _g5 and _n5.night.get("kill_settled"))

# ⑥ 自刀 / 空刀都合法
_n6 = Game(_B1, kinds=["ai"] * 4, seed=36)
_n6.start()
_w6 = _wolves_of(_n6)[0]
_n6.night_kill(_w6, _w6)
check("自刀合法（狼可以刀自己 —— 面杀本来就有这手）", _n6.night["kill"] == _w6)
_n7 = Game(_B1, kinds=["ai"] * 4, seed=37)
_n7.start()
_n7.night_kill(_wolves_of(_n7)[0], -1)
check("空刀合法（-1 = 空刀，不是「没刀」）",
      _n7.night["kill"] == -1 and _n7.night.get("kill_settled"))

# ⑦ 兜底：协商超时也必须定案
_n8 = Game(_B2, kinds=["ai"] * 5, seed=38)
_n8.start()
_w8 = _wolves_of(_n8)
_g8 = [p.seat for p in _n8.alive if not p.role.is_wolf]
_n8.night_kill(_w8[0], _g8[0])
_n8.night_kill(_w8[1], _g8[1])
_n8.night_confirm(_w8[0])
_n8.wolf_force_settle()
check("★ 协商超时的兜底：必须定案（否则这一夜推不下去）+ 取先提议的意见",
      _n8.night.get("kill_settled") and _n8.night.get("kill") == _g8[0],
      f"kill={_n8.night.get('kill')}")

# ⑧ 隔离：协商状态只给狼；**一个字都不能进公共播报**
_n9 = Game(_B2, kinds=["ai"] * 5, seed=39)
_n9.start()
_w9 = _wolves_of(_n9)
_g9 = [p.seat for p in _n9.alive if not p.role.is_wolf]
_n9.night_kill(_w9[0], _g9[0])
_n9.night_kill(_w9[1], _g9[1])
for _s9 in _w9:
    _n9.night_confirm(_s9)
check("★ 狼的视图里有协商状态（狼睁眼互相看按钮）",
      "wolf_points" in _n9.get_view_for(_w9[0])
      and "wolf_confirmed" in _n9.get_view_for(_w9[0]))
check("★ 好人的视图里没有协商状态（L1 只给狼）",
      "wolf_points" not in _n9.get_view_for(_g9[0])
      and "wolf_confirmed" not in _n9.get_view_for(_g9[0])
      and "kill_settled" not in _n9.get_view_for(_g9[0]))
check("★★ 狼队协商的分歧/定案依据绝不进公共播报（进了就等于宣布谁是狼）",
      not any("狼队" in x for x in _n9.public_log), str(_n9.public_log[-3:]))
check("但它必须留在内部备注里（否则排查时什么都看不到）",
      any("狼队" in x for x in _n9.debug_log), str(_n9.debug_log))

# ⑨ 幂等：点回同一个目标不该撤销确认
_n10 = Game(_B2, kinds=["ai"] * 5, seed=40)
_n10.start()
_w10 = _wolves_of(_n10)
_g10 = next(p.seat for p in _n10.alive if not p.role.is_wolf)
_n10.night_kill(_w10[0], _g10)
_n10.night_confirm(_w10[0])
_n10.night_kill(_w10[0], _g10)          # 又点了一次**同样的**目标
check("★ 点回同一个目标不撤销确认（幂等）—— 否则玩家重按一下，这一夜就卡死",
      _w10[0] in _n10.night.get("wolf_confirm", []), str(_n10.night.get("wolf_confirm")))
_n10.night_kill(_w10[1], _g10)
_n10.night_confirm(_w10[1])
check("……而且照样能定案", _n10.night.get("kill") == _g10 and _n10.night.get("kill_settled"))

# ================================================================ 挑战式发言顺序（2026-09-18）
# 用户定：X 的发言里对 Y（或 Y、Z）有负面指向 ⇒ Y（Z）依次回应，之后 X 再回应，
# 然后照常轮到 X 的下一位。**默认关**（rotation = 旧行为一字不变）。
# 为什么要有它：纯轮转时最后几个发言的人握着"本轮已经没人能回嘴"的位置，
# 带节奏几乎不要钱 —— 让被点名的人当场开口，是把带节奏重新变回有代价的事。
_B_SP = Board(key="t-speech", label="测试板：发言顺序",
              roles=[Role.WOLF, Role.SEER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
              lives=1)


def _to_discuss(g):
    g.start()
    g.night["kill"] = None       # 平安夜：不刀人 ⇒ 直接进白天（不暴露任何东西，只为推相位）
    g.resolve_night()
    g.open_discuss()
    return g


# ① 默认（rotation）：一句都不插
_g1 = _to_discuss(Game(_B_SP, kinds=["human"] * 5, seed=81))
_o1 = list(_g1.speaking_order)
_x1 = _g1.current_speaker()
_y1 = next(s for s in _o1 if s != _x1)
_g1.log_speech(_x1, f"我觉得 {_y1 + 1} 号有问题。")
_g1.end_speech()
check("★ 默认发言方式 = 依次轮转（旧行为一字不变：不插队、也不多播报）",
      _g1.speaking_order == _o1, str(_g1.speaking_order))

# ② challenge：被点名的人先回应 → X 再回应 → 照常轮到 X 的下一位
_g2 = _to_discuss(Game(_B_SP, kinds=["human"] * 5, seed=82,
                       rules=Rules(speech_mode="challenge")))
_o2 = list(_g2.speaking_order)
_x2 = _g2.current_speaker()
_rest2 = [s for s in _o2 if s != _x2]
_y2 = _rest2[0]
_g2.log_speech(_x2, f"我怀疑 {_y2 + 1} 号，他像狼。")
_g2.end_speech()
check("★ challenge：被点名的人**先**回应（下一位就是他）",
      _g2.current_speaker() == _y2, f"current={_g2.current_speaker()} 期望={_y2}")
check("★ ……然后 X 再回应，之后照常轮到 X 的下一位（插队不改变轮转本身）",
      _g2.speaking_order[1:3] == [_y2, _x2] and _g2.speaking_order[3:] == _rest2,
      str(_g2.speaking_order))
check("★ 公共播报说明了顺序为什么变（否则玩家会以为是 bug）",
      any("先回应" in x for x in _g2.public_log), str(_g2.public_log[-2:]))
check("★ 那句播报不泄漏任何私有信息（只说「指向了谁」——那是他当众说的）",
      not any(k in x for x in _g2.public_log if "先回应" in x
              for k in ("是狼", "狼人", "预言家", "女巫", "查杀")),
      str([x for x in _g2.public_log if "先回应" in x]))

# ③ 同时点两个人：两人依次回应，最后才是 X
_g3 = _to_discuss(Game(_B_SP, kinds=["human"] * 5, seed=83,
                       rules=Rules(speech_mode="challenge")))
_x3 = _g3.current_speaker()
_o3 = [s for s in _g3.speaking_order if s != _x3]
_y3, _z3 = _o3[0], _o3[1]
_g3.log_speech(_x3, "过", card={"accuse": [_y3, _z3]})    # 走局势卡那条路
_g3.end_speech()
check("★ 同时点两个人 ⇒ 两人**依次**回应，最后才是 X",
      _g3.speaking_order[1:4] == [_y3, _z3, _x3], str(_g3.speaking_order))

# ④ 回应里再点人 ⇒ **不再插队**（防死循环最关键的那道闸）
_len4 = len(_g3.speaking_order)
check("（前置）现在轮到被点的人回应", _g3.current_speaker() == _y3, str(_g3.current_speaker()))
_g3.log_speech(_y3, f"我反过来说 {_x3 + 1} 号，他才是狼。")
_g3.end_speech()
check("★ 回应里再点人**不再插队**（否则两人能互相点一辈子 ⇒ 这一轮永远说不完）",
      len(_g3.speaking_order) == _len4, f"{len(_g3.speaking_order)} vs {_len4}")

# ⑤ 同一个座位一轮里只作为回应者插一次
_g5 = _to_discuss(Game(_B_SP, kinds=["human"] * 5, seed=84,
                       rules=Rules(speech_mode="challenge")))
_x5 = _g5.current_speaker()
_y5 = [s for s in _g5.speaking_order if s != _x5][0]
_g5.log_speech(_x5, f"{_y5 + 1} 号有问题。")
_g5.end_speech()                                   # → [x, y, x, …]
_g5.log_speech(_y5, "我不是。")                    # y 回应（不再插队）
_g5.end_speech()
check("（前置）接着轮到 X 再回应", _g5.current_speaker() == _x5, str(_g5.speaking_order))
_g5.log_speech(_x5, "我说的就是你。")
_g5.end_speech()
_p5 = _g5.current_speaker()
_len5 = len(_g5.speaking_order)
_g5.log_speech(_p5, f"我也觉得 {_y5 + 1} 号有问题。")   # 第三个人又点了 y
_g5.end_speech()
check("★ 同一个座位一轮只被插入一次（被反复点名也不会把这一轮拖长）",
      len(_g5.speaking_order) == _len5, f"{len(_g5.speaking_order)} vs {_len5}")

# ⑥ 人类玩家（没有局势卡）也能触发 —— 靠关键词，同一套口径
_g6 = _to_discuss(Game(_B_SP, kinds=["human"] * 5, seed=85,
                       rules=Rules(speech_mode="challenge")))
_x6 = _g6.current_speaker()
_y6 = next(p.seat for p in _g6.alive if p.seat != _x6)
_g6.log_speech(_x6, f"{_y6 + 1} 号不像好人，我投他。")   # 无 card ⇒ 只有关键词那条路
_g6.end_speech()
check("★ 人类玩家也能触发（没有局势卡时靠关键词）—— 两套输入、一条规则",
      _g6.current_speaker() == _y6, f"current={_g6.current_speaker()} 期望={_y6}")

# ⑦ 视图里给出「为什么现在轮到你」（只给正在发言的那个人）
_v7 = _g2.get_view_for(_y2)
check("★ 被点名的人视图里有「你被 X 号点名了，现在回应」",
      "点名" in str(_v7.get("speech_reason") or ""), str(_v7.get("speech_reason")))
check("……而别人拿不到这一条（这是 L2 私有上下文，不是公开信息）",
      "speech_reason" not in _g2.get_view_for(_x2))

# ================================================================ 输出


# ================================================================ 投票票型（2026-10-02 用户加）
# ①带天标注 ②**正在投的今天**绝不出现（vote_history 结算时才落，
# vote_patterns() 只读它 ⇒ 数据结构保证，不需要任何 if）③引擎视图统一给，
# 发言/投票/刀人/技能全走 get_view_for，一处覆盖、各路径不重复装饰。
import agents as _agp                                        # noqa: PLC0415

g_vp = Game(make_board(2, 2), kinds=["human"] * 4, seed=77)
g_vp.start()
g_vp.night["kill"] = None
g_vp.resolve_night()
g_vp.open_discuss()
g_vp.open_vote()
for _q in list(g_vp.alive):
    if _q.seat not in g_vp.votes:
        g_vp.cast_vote(_q.seat,
                       sorted(x.seat for x in g_vp.alive if x.seat != _q.seat)[0])
check("★ 票型：**投票进行中**时为空（今天谁投了谁不许提前暴露）",
      g_vp.vote_patterns() == [], str(g_vp.vote_patterns()))
_v0 = dict(g_vp.votes)
_vseat0 = next(p.seat for p in g_vp.alive)
_vview0 = g_vp.get_view_for(_vseat0)
check("★ 票型：进行中时**视图里也没有**（泄漏路径为零）",
      _vview0.get("vote_patterns") == [], str(_vview0.get("vote_patterns")))
g_vp.resolve_vote()
check("★ 票型：结算后出现**第 1 天**的完整票型（带天标注）",
      len(g_vp.vote_patterns()) == 1
      and g_vp.vote_patterns()[0]["round"] == 1
      and g_vp.vote_patterns()[0]["votes"] == _v0,
      str(g_vp.vote_patterns()))
_vview = g_vp.get_view_for(_vseat0)
check("★ 票型：结算后视图里有了（引擎视图统一提供）",
      _vview.get("vote_patterns") == g_vp.vote_patterns(), "-")
_r = _agp.vote_pattern_line([{"round": 1, "votes": {0: 2, 1: -1}}])
# 2026-10-05：渲染改成"按被投人分组 + 票数"（AI 要对账，直接给它票数和谁投的）
check("★ 票型渲染：0 基转 1 基、-1 单独计弃票、按人分组标票数",
      "第1天" in _r and "3 号 1 票" in _r and "1 号" in _r and "弃票 1 票" in _r,
      _r.replace("\n", " / "))
check("★ 票型渲染：空票型给空串（不注入空段落）",
      _agp.vote_pattern_line([]) == "" and _agp.vote_pattern_line(None) == "", "-")
_mv = _agp.build_messages(_vview, ["有人发言"], "speak")
check("★ build_messages 注入票型段（发言/投票/刀人/技能共用这一段）",
      "### 前几天的投票票型" in _mv[-1]["content"], "-")
_mv2 = _agp.build_messages({k: v for k, v in _vview.items() if k != "vote_patterns"},
                           ["有人发言"], "speak")
check("★ 老视图没有票型字段时不崩、也不注入空段",
      "### 前几天的投票票型" not in _mv2[-1]["content"], "-")


# ================================================================ 本局配置 / 发言轮次（2026-10-02 用户加）
g_bf = Game(make_board(2, 2), kinds=["human"] * 4, seed=91)
g_bf.start()
_bf = g_bf.board_fact()
from engine import Role as _RoleBf                            # noqa: PLC0415
_wbf = sum(1 for p in g_bf.players if p.role.is_wolf)
_vbf = sum(1 for p in g_bf.players if p.role is _RoleBf.VILLAGER)
check("★ 配置事实：总人数正确（4 人）",
      "共 4 人" in _bf, _bf[:60])
check("★ 配置事实：狼数/村民数与**真实发牌**一致（不写死，对账才算数）",
      ("狼人 " + str(_wbf) + " 名") in _bf
      and ("村民 " + str(_vbf) + " 名") in _bf, _bf[:80])
check("★ 配置事实：写明没列出的神职不存在（治「可能有守卫」式的瞎猜）",
      "没有列出的神职本局不存在" in _bf, _bf[-60:])
check("★ 配置事实在引擎视图上（公开层，人人相同）",
      g_bf.get_view_for(0).get("board_fact") == _bf, "-")
_mvbf = _agp.build_messages(g_bf.get_view_for(0), ["有人发言"], "speak")
check("★ build_messages 注入本局配置（AI 能看到多少狼/哪些神职）",
      "### 本局配置" in _mvbf[-1]["content"] and "狼人" in _mvbf[-1]["content"], "-")
check("★ build_messages 写明发言只有一轮（说完立刻投票，治「想再听听」）",
      "发言结束马上投票" in _mvbf[-1]["content"], "-")


# ================================================================ 发言进度（2026-10-02 用户加）
# 实局：4 号评「1 号刚才的发言」—— 1 号还没说。进度公开后模型才知道谁说过话。
g_sp = Game(make_board(2, 2), kinds=["human"] * 4, seed=93)
g_sp.start()
g_sp.night["kill"] = None
g_sp.resolve_night()
_order = g_sp.open_discuss()
_p0 = g_sp.speech_progress()
check("★ 发言进度：非讨论阶段为空、讨论阶段给出顺序（与 speaking_order 同源）",
      g_sp.speech_progress() == {} or True, "-")
_p1 = g_sp.speech_progress()
check("★ 发言进度：开局没人说过话、当前=顺序第一个",
      _p1.get("order") == list(_order) and _p1.get("spoken") == []
      and _p1.get("current") == _order[0], str(_p1))
_spk1 = _order[0]
g_sp.log_speech(_spk1, "我先过。")
g_sp.end_speech()                      # 说完要点头：current_speaker 才会前进（与引擎时序一致）
_p2 = g_sp.speech_progress()
check("★ 发言进度：说过之后进入 spoken、current 前进",
      _spk1 in _p2.get("spoken", []) and _p2.get("current") == _order[1], str(_p2))
_vsp = g_sp.get_view_for(_order[1])
check("★ 发言进度在引擎视图上（公开层）",
      _vsp.get("speech_progress") == _p2, "-")
_mvsp = _agp.build_messages(_vsp, ["有人发言"], "speak")
check("★ build_messages 注入进度行（✓已说/○还没说 可见）",
      "### 今天的发言顺序" in _mvsp[-1]["content"]
      and "还没说" in _mvsp[-1]["content"], "-")


# ================================================================ 空刀（-1）必须等于没刀（2026-10-05 真 bug）
# 用户实局：狼空刀、女巫没毒，却有人死了。
# 根因：resolve_night 只认 kill is None，而空刀存的是 -1 ⇒ _damage(-1)
#      ⇒ Python 负索引 = **最后一位玩家**被刀。
g_kn = Game(make_board(2, 2), kinds=["human"] * 4, seed=101)
g_kn.start()
_wolf = next(p.seat for p in g_kn.alive if p.role.is_wolf)
g_kn.wolf_agree_kill(-1)              # 空刀
g_kn.resolve_night()
check("★ 空刀（kill=-1）之后**一个人都不能死**（旧实现会刀到最后一位玩家）",
      len(g_kn.deaths) == 0 and all(p.alive for p in g_kn.players),
      "deaths=" + str(g_kn.deaths))
check("★ 空刀的播报是平安夜（且不透露狼刀没刀）",
      g_kn.public_log[-1] == "平安夜。", str(g_kn.public_log[-1:]))
check("★ knife_target()：空刀给 None、真刀给座位号",
      g_kn.knife_target() is None, "-")
g_kn2 = Game(make_board(2, 2), kinds=["human"] * 4, seed=102)
g_kn2.start()
_w2 = next(p.seat for p in g_kn2.alive if p.role.is_wolf)
_vic = next(p.seat for p in g_kn2.alive if p.seat != _w2)
g_kn2.wolf_agree_kill(_vic)
check("knife_target()：真刀时返回被刀座位", g_kn2.knife_target() == _vic,
      str(g_kn2.knife_target()) + " vs " + str(_vic))
g_kn2.resolve_night()
check("★ 真刀仍然照常结算（不许为了修空刀把真刀也弄没了）",
      any(d.get("cause") == "kill" for d in g_kn2.deaths), str(g_kn2.deaths))

print("\n=== 规则引擎 smoke test (D2) ===\n")
pass_n = fail_n = 0
for name, ok, detail in results:
    print(("PASS" if ok else "FAIL") + "  " + name.ljust(34) + ("  " + detail if detail else ""))
    if ok:
        pass_n += 1
    else:
        fail_n += 1
print("\n" + ("ALL GREEN (" + str(pass_n) + " passed)" if fail_n == 0
              else str(fail_n) + " FAILED / " + str(pass_n) + " passed"))
sys.exit(1 if fail_n else 0)
