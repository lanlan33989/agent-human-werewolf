# -*- coding: utf-8 -*-
"""AI 玩家的动作决策（夜刀/验/药/守、白天发言、投票、开枪）。

2026-10-06 P2（拆 Session 上帝类）：方法**原样搬移**自 game_api.Session，
首参 `self` 即 Session 实例（保留原名以免机械搬写出错）——
由 Session 的同名薄委托调用。不改任何行为。
"""
import os
import re
import random
import time
from typing import Any, Dict, List, Optional

from agents import JUDGE_VOICE, Decision, extract_control, voice_for_personality
from engine import Phase, Role

import re
from agents import Decision, extract_control
from engine import Role
from typing import Optional

from night_pre import WAIT, GIVEUP, MISS


def _pre_decide(self, seat: int, kind: str, compute):
    """取后台预跑的决策（2026-10-07 第二期）；没预跑就照旧同步算。

    返回 (decision, flag)：
      · flag is None    → decision 可直接用（预跑命中，或同步算出来的）
      · flag == "wait"  → 预跑还在后台跑、没到收割点 ⇒ 驱动器报 working，继续轮询
      · flag == "giveup"→ 过了收割点还没算完 ⇒ 结果丢弃，按"默认放弃"处理

    预跑线程里的失败（异常）当"模型调用失败"处理 —— 退化到原来的兜底，
    **不是**"放弃"（两者语义不同：失败是"没答上来"，放弃是"明确不做"）。
    """
    pre = getattr(self, "_pre", None)
    if pre is None:
        return compute(), None
    # 本次推进还剩多少预算（auto_advance 给的 deadline）——预跑没跑完时**等**它，
    # 但绝不超预算：超了就返回 WAIT，让上层报 working、下一次轮询接着等。
    _dl = getattr(self, "_adv_deadline", None)
    _to = None if _dl is None else max(0.0, _dl - time.monotonic())
    # 🔴 2026-10-07 第三期（定稿的"打断"）：到了本类的**收割节点**就**不再等** ——
    #    `_nn_hard` 由节点时钟在表里的 `harvest_start` / `harvest_end` 上点亮
    #    （狼刀节点开始收 [守卫, 狼]；预言家节点结束收 [女巫, 预言家]）。
    #    定稿："若未做出，则默认没发动技能，并直接打断，后续不接收其该夜决策"。
    if kind in (getattr(self, "_nn_hard", None) or ()):
        _to = 0.0
    v = pre.take(seat, kind, _to)
    if v is WAIT:
        return None, "wait"
    if v is GIVEUP:
        return None, "giveup"
    if v is not MISS:
        if isinstance(v, BaseException):
            return Decision(rejected=["预跑失败：" + type(v).__name__
                                      + "：" + str(v)[:60]]), None
        return v, None
    return compute(), None


# ---- 各步骤的 AI 执行（steps.NIGHT_STEPS 的 run 字段指过来）----

def _step_ai_wolf(self) -> bool:
    """
    狼队刀人 —— 含"按钮协商"那套状态机（2026-09-17 改：从"一只狼定刀"变成"组内逐步协商"）。

    老写法有个硬性让位：**只要场上有任何人类狼，AI 狼就完全不动**。
    结果是"人类狼独裁、AI 狼闭嘴"，白天两只狼各投各的、一跟大势一咬好人 ⇒
    立刻被好人看穿（用户实测报的正是这个）。
    现在两步推进，每步只动一只狼：
      ① 还没点过的狼先点（它的视图里有队友此刻的点，默认会跟随）
      ② 点过但没确定的狼去确定（**第一个确定的人 = 它的点先落地**）
      ③ 全员确定 ⇒ 引擎自动定案（一致用一致，分歧用先落地的）
    人类狼照旧拿手机做同样两件事（actions 里是 kill + confirm），
    所以"AI 抢走人类狼的操作"不存在：**票没齐就定不了案**。
    """
    g = self.game
    if g.night.get("kill_settled"):
        return False
    wolves = [p for p in g.alive if p.role.is_wolf]
    if not wolves:
        return False
    ai_wolves = [p for p in wolves if p.kind == "ai"]
    pts = g.night.get("wolf_points") or {}
    conf = g.night.get("wolf_confirm") or []
    # 步数上限兜底：只有**纯 AI 狼队**才可能无限来回（互相跟随/改主意）；
    # 有人类狼时人类会把它推着往前走。
    if (ai_wolves and not any(p.kind == "human" for p in wolves)
            and g.night.get("wolf_steps", 0) >= 4 * len(wolves)):
        g.wolf_force_settle()
        return True
    # ① 先让**所有**狼都点完。人类狼没点过 ⇒ 这一支落空 ⇒ 返回 False
    #    ⇒ 驱动器报 human_night，手机交到他手上（他点完才轮到 ②）。
    actor = next((p for p in ai_wolves if p.seat not in pts), None)
    if actor is not None:
        # 走后台预跑：可能 True（点了）或 "wait"（还在算）—— 原样返回给驱动器
        return self._ai_wolf_propose(actor.seat)
    if all(p.seat in pts for p in wolves):
        # ② 都点完了才开始"确定"。此刻 AI 看得见人狼那一票 ——
        #    这就是"先手定完之后，AI 决定跟随还是固执"的时机。
        actor = next((p for p in ai_wolves if p.seat not in conf), None)
        if actor is not None:
            if self._ai_wolf_maybe_follow(actor.seat):
                return True
            g.night_confirm(actor.seat)
            return True
    return False


def _step_ai_seer(self):
    """预言家验人（不依赖任何别的步骤 ⇒ 不需要前置门）。

    2026-10-07 第二期：走后台预跑；到收割点（预言家节点结束）没算完 ⇒ 不验（默认放弃）。
    """
    g = self.game
    actor = next((p for p in g.alive if p.role is Role.SEER and p.kind == "ai"), None)
    if actor is None:
        return False
    r = self._pick_ai_target(actor.seat, "investigate")   # 走模型（可能 "wait"）
    if r == "wait":
        return "wait"
    if r == "giveup":
        g.night_skip_investigate(actor.seat)              # 默认放弃 = 今晚不验
        return True
    if r is None:
        g.night["investigate"] = -1
    else:
        g.night_investigate(actor.seat, r)
    return True


def _step_ai_witch(self):
    """
    AI 女巫用药（**走模型**；以前是 `rng.random() < 0.6` 一刀切，
    而且毒药在这条 AI 路径上从来没被调用过 —— 她永远不毒人）。

    ⚠️ 前置门必须在：狼刀没定就不问（引擎里也有同一道门）。
    少了它，AI 女巫会在 victim 还是 None 时被问、直接 decline，
    然后 witch_asked 被写死 ⇒ 这一夜再也不会问她 ⇒ 永远救不了人。
    """
    g = self.game
    # 🔴 2026-10-07：判据同 engine._actions_available —— "刀已定案"就问她，
    # 空刀（无人被刀）也要走那一问，否则这一步永远做不完、夜晚结束不了。
    if g.knife_target() is None and not g.night.get("kill_settled"):
        return False
    witch = next((p for p in g.alive
                  if p.role is Role.WITCH and p.kind == "ai"), None)
    if witch is None:
        return False
    return self._ai_witch_act(witch.seat)


def _step_ai_guard(self):
    """AI 守卫守人（不依赖别的步骤；`_ai_guard_act` 自己会写 guard_asked）。"""
    g = self.game
    guard = next((p for p in g.alive
                  if p.role is Role.GUARD and p.kind == "ai"), None)
    if guard is None:
        return False
    return self._ai_guard_act(guard.seat)


def _ai_wolf_propose(self, seat: int):
    """
    AI 狼**点人**（走模型）。三种合法输出：座位号 / 「空刀」/「自刀」。

    · 「空刀」「自刀」必须从**原始文本**里认：座位号解析器会把自己排除掉，
      所以"自刀"在它那里永远是 None（这也是为什么这里要自己认一遍 raw）。
    · 模型没给出可用目标时的兜底**不是随机乱刀**，而是：
      点过的队友有谁就跟随谁（第一步跟随），一个都没有才随机点一个好人。
      "跟随"是机制层面的默认，不是让模型背战术 —— 它看得见队友的点，自己权衡。

    2026-10-07 第二期：决策走**后台预跑**（天黑即跑）。返回 True（已点）/ "wait"（还在算）；
    到收割点还没算完 ⇒ 用户定"默认放弃" = **空刀**（不是随机点，也不是跟随）。
    """
    g = self.game
    target = None

    def _compute():
        return self.agents[seat].decide_target(
            g.get_view_for(seat), self.speech, "kill")

    try:
        d, flag = _pre_decide(self, seat, "kill", _compute)
    except Exception as e:                 # noqa: BLE001
        # 不吞错：Agent 缺方法/模型挂了都必须留痕（实测被这个坑过一次 ——
        # 测试替身没实现 decide_target，驱动器静默兜底，表现为"决策没经过 Agent"）
        print(f"[agent] 座位 {seat + 1} 的刀人决策失败，走兜底：{type(e).__name__}: {e}",
              flush=True)
        d, flag = None, None
    if flag == "wait":
        return "wait"
    if flag == "giveup":
        g.night_kill(seat, -1)             # 空刀（默认放弃）
        return True
    try:
        target = d.target
        raw = d.raw or ""
        if re.search(r"空刀", raw):
            target = -1
        elif re.search(r"自刀", raw):
            target = seat
    except Exception:                      # noqa: BLE001
        target = None
    if target is None:
        pts = g.night.get("wolf_points") or {}
        mates = [t for s, t in pts.items() if s != seat and t is not None]
        if mates:
            target = mates[0]              # 跟随队友（默认行为）
        else:
            pool = [q.seat for q in g.alive if not q.role.is_wolf]
            target = self.rng.choice(pool) if pool else -1
    g.night_kill(seat, target)
    return True


def _ai_wolf_maybe_follow(self, seat: int) -> bool:
    """
    AI 狼在「确定」之前的**一次调整机会**：跟随人狼（用户定的默认策略：优先跟随）。

    为什么放在这个时机而不是一开始点的时候：刚开局人类狼还没点（手机在他手上），
    AI 无从跟随；等**所有狼都点完**，它才看得到人狼那一票。
    每晚每只 AI 只跟一次（wolf_followed 记着）—— 否则两只 AI 会你跟我、我跟你，
    来回改成一团（那就是死循环，兜底都救不回来）。
    批 8 的人格会接管这个默认值（bold/aggressive 才坚持己见）。
    """
    g = self.game
    pts = g.night.get("wolf_points") or {}
    mine = pts.get(seat)
    humans = [p.seat for p in g.alive if p.role.is_wolf and p.kind == "human"]
    followed = g.night.setdefault("wolf_followed", [])
    # ★ H2：跟不跟随是**人格参数**，不是写死的策略。
    #   激进/狡诈/大胆/雄辩/策略型 = 固执（坚持自己的点）；其余跟随。
    #   没配人格 ⇒ 跟随（保持旧行为）。
    if not getattr(self.agents.get(seat), "follows_team", lambda: True)():
        return False
    if seat in followed or not humans:
        return False
    want = next((pts.get(s) for s in humans
                 if pts.get(s) is not None and pts.get(s) != mine), None)
    if want is None:
        return False
    followed.append(seat)
    g.night_kill(seat, want)      # 改点（引擎会自动撤掉它自己的"确定"）
    return True


def _ai_guard_act(self, seat: int):
    """
    AI 守卫守谁（走模型）。

    「不可连续两晚守同一人」是**引擎级硬规则**（night_guard 会拒），
    所以这里必须先把上一晚那个目标排除掉 —— 否则模型选了同一个人，
    引擎一拒，这一夜就可能推不下去。

    2026-10-07 第二期：走后台预跑；过收割点没算完 ⇒ 空守（默认放弃）。
    """
    g = self.game
    last = g.private[seat].guard_last
    try:
        d, flag = _pre_decide(self, seat, "guard",
                              lambda: self.agents[seat].decide_target(
                                  g.get_view_for(seat), self.speech, "guard"))
    except Exception:                      # noqa: BLE001
        d, flag = None, None
    if flag == "wait":
        return "wait"
    if flag == "giveup":
        g.night_guard(seat, -1)            # 空守（默认放弃）
        return True
    try:
        target = d.target
    except Exception:                      # noqa: BLE001
        target = None
    if target is None or target == last:
        pool = [q.seat for q in g.alive
                if q.seat != last
                and (g.rules.guard_can_self_guard or q.seat != seat)]
        target = self.rng.choice(pool) if pool else None
    if target is None:
        g.night["guard_asked"] = True      # 实在没人可守（极端情况）：别把夜晚卡住
        return True
    g.night_guard(seat, target)
    return True


def _ai_witch_act(self, seat: int):
    """
    让 AI 女巫自己决定这一夜用不用药（走模型，不再掷骰子）。

    三条规则都是刻意的：
      · **一晚只用一瓶**：引擎侧 witch_asked 一旦置位就不会再问她（标准规则），
        所以这里只需要「先看解药、再看毒药」。
      · **解药只问是/否，不问座位号**：她唯一能救的就是今晚被刀的那个人，
        让模型自己点座位只会点错 —— 点了别人等于白耗一瓶，被刀的人照样死。
      · **毒药必须能说「不毒」**：没有这个出口，模型就只能硬点一个人，
        那正是盲毒（误伤好人的主要来源）。
    模型挂了**不随机乱毒**，只在解药那一步退回原来的 60% 掷骰子。

    2026-10-07 第二期：两个问句各自走后台预跑（刀口定了才起跑）；
    过收割点没算完 ⇒ 不用药（默认放弃）。
    """
    g = self.game
    victim = g.knife_target()
    potions = g.private[seat].potions
    agent = self.agents[seat]
    view = g.get_view_for(seat)

    if victim not in (None, -1) and potions["heal"]:
        try:
            d, flag = _pre_decide(self, seat, "heal",
                                  lambda: agent.decide_yes(view, self.speech, "heal"))
        except Exception:                      # noqa: BLE001
            d, flag = Decision(rejected=["模型调用失败"]), None
        if flag == "wait":
            return "wait"
        if flag == "giveup":
            g.night_decline(seat)
            return True
        use = d.yes if d.yes is not None else (self.rng.random() < 0.6)
        if use:
            g.night_heal(seat, victim)
        else:
            g.night_decline(seat)
        return True

    if potions["poison"]:
        try:
            d, flag = _pre_decide(self, seat, "poison",
                                  lambda: agent.decide_target(view, self.speech, "poison"))
        except Exception:                      # noqa: BLE001
            d, flag = Decision(rejected=["模型调用失败"]), None
        if flag == "wait":
            return "wait"
        if flag == "giveup":
            g.night_decline(seat)
            return True
        if d.target is not None:
            g.night_poison(seat, d.target)
        else:
            g.night_decline(seat)
        return True

    g.night_decline(seat)
    return True


# ---- 白天 ----

def _ai_speak(self, seat: int) -> None:
    g = self.game
    if g.current_speaker() != seat:
        # 不变量：只有"当前发言人"能说话。走到这里说明驱动被重入了
        # （历史上就是 /public 与 /act 并发推进），跳过而不是让他说第二遍。
        print(f"[race] 跳过重复发言：座位 {seat + 1} 不是当前发言人", flush=True)
        return
    p = g.player(seat)
    v = g.get_view_for(seat)
    v["my_tactics"] = self.tactics.get(seat) or []
    v["my_plan"] = self.day_plans.get(seat) or ""
    v["day_summaries"] = dict(self.day_summaries)
    d = self.agents[seat].speak(v, self._round_speech(g))
    # 战术备忘存档（thought，私有）：下一轮发言/投票时喂回给它自己
    _t = extract_control(getattr(d, "raw", "") or "").get("thought")
    if _t and str(_t).strip():
        _hist = self.tactics.setdefault(seat, [])
        _hist.append("第" + str(g.round) + "天发言：" + str(_t).strip()[:160])
        del _hist[:-3]
    # 局势卡（G4）跟着这句话一起记：它是"这段发言的要点"，所以算公开信息。
    self._say(f"{p.display}：{d.text}", seat, d.text,
              card=getattr(d, "card", None))
    # 两类问题分开处理，别混：
    #   ·「格式：…」= 模型照抄了 prompt 的格式（带标签 / 带座位号前缀 / 多段续写）。
    #     文本已经被 sanitize_speech 收拾干净、照常播报，只把原始输出打到控制台。
    #   · 其它 = 真的被输出检查拦下（自认狼身份 / 引用系统消息）。
    #     这时 d.text 是兜底话术，必须在桌上说明"这句被拦了"。
    fmt = [x for x in (d.rejected or []) if x.startswith("格式：")]
    hard = [x for x in (d.rejected or []) if not x.startswith("格式：")]
    if fmt:
        print(f"[fmt] {p.display} 输出格式异常（已自动收拾）：{'、'.join(fmt)}"
              f" | raw={d.raw[:80]!r}", flush=True)
    if hard:
        self._say(f"（{p.display} 的发言被输出检查拦下：{'、'.join(hard)}）")
        # ★ 把**原始输出**打到控制台：否则误拦时，从界面上完全看不出它本来想说什么。
        #   实测就有一次：一个好人被拦，理由是"自认狼身份"，而桌上只看到兜底句。
        print(f"[leak] {p.display} 原始输出={d.raw[:160]!r}", flush=True)


def _run_one_ai_shoot(self) -> bool:
    """
    轮到猎人开枪（AI 猎人）。返回 False = 该由人类开枪（pending 变 human_shoot）。

    模型明确说"不开枪"就真的不开（rejected 为空 = 明确不做）；
    解析失败/调用失败才随机兜底一个活人 —— 保证这一枪（或放弃）一定落定，
    否则会卡在 SHOOT 相位。
    """
    g = self.game
    seat = g.shooter
    if seat is None:
        return False
    if g.player(seat).kind != "ai":
        return False
    target = None
    try:
        d = self.agents[seat].decide_target(g.get_view_for(seat), self.speech, "shoot")
        target = d.target
        if target is None and d.rejected:
            pool = [q.seat for q in g.alive if q.seat != seat]
            target = self.rng.choice(pool) if pool else None
    except Exception:                      # noqa: BLE001
        pool = [q.seat for q in g.alive if q.seat != seat]
        target = self.rng.choice(pool) if pool else None
    g.night_shoot(seat, target if target is not None else -1)
    return True


# ---------------------------------------------------------- AI 决策走哪

def _pick_ai_target(self, seat: int, kind: str):
    """
    让这个 AI **自己决定**目标（走它的 Agent → 真模型），驱动器只做兜底。

    为什么要经过 Agent：用户实测报过「AI 的投票跟场上发言毫无关系」。
    根因就是这里原来直接 `self.rng.choice(...)` —— **决策类动作根本没调模型**，
    只有「发言」调了。现在投票 / 刀人 / 验人一律走这里。

    2026-10-07 第二期：优先取后台预跑的结果；返回 "wait"/"giveup" 时由调用方处置。
    """
    g = self.game
    d, flag = _pre_decide(self, seat, kind,
                          lambda: self.agents[seat].act_on_target(
                              g.get_view_for(seat), self.speech, kind))
    if flag:
        return flag
    target = d.target
    # 「狼不刀自己人」只能在驱动器里判 —— Agent 拿到的视图里没有别人的身份，
    # 那是隔离，不能为了这条规则去破它。
    if target is None or (kind == "kill" and g.player(target).role.is_wolf):
        return self._fallback_target(seat, kind)
    return target


def _fallback_target(self, seat: int, kind: str) -> Optional[int]:
    """模型没给出可用目标（超时 / 空串 / 解析不出 / 指了自己人）时的保底。"""
    g = self.game
    if kind == "kill":
        pool = [p.seat for p in g.good_alive() if p.seat != seat]
    else:                       # vote / investigate：任何活人都可以
        pool = [p.seat for p in g.alive if p.seat != seat]
    return self.rng.choice(pool) if pool else None


def _run_one_ai_vote(self):
    g = self.game
    # 按 vote_order 走（用户定的规则：投票从前一晚被刀的人开始顺时针）。
    # 不能用 `next(...)` 按座位号取第一个 —— 那会让投票顺序和发言顺序脱钩。
    order = g.vote_order or [p.seat for p in g.alive]
    actor = next((g.player(s) for s in order
                  if s not in g.votes and g.player(s).kind == "ai"
                  and g.player(s).alive), None)
    if actor is None:
        return False
    r = self._pick_ai_vote(actor.seat)
    if r == "wait":
        return "wait"
    if r == "giveup":
        g.cast_vote(actor.seat, -1)        # 超时未决策 ⇒ 弃票
        return True
    g.cast_vote(actor.seat, r)
    return True


def _pick_ai_vote(self, seat: int):
    """
    AI 的投票决策：返回座位号；**返回 -1 = 主动弃票**；
    2026-10-07 第二期还可能返回 "wait"（后台预跑中）/ "giveup"（超时 ⇒ 弃票）。

    以前弃票只发生在「解析彻底失败、且连一个合法目标都找不到」时 ——
    也就是说 AI 实际上**从不弃票**。但信息不足时弃票（尤其好人）是正常打法，
    模型应该能选它（TASKS["vote"] 里给了「弃票」这个出口）。

    三种情况不能混：
      · 模型给了座位号      → 投他
      · 模型明确说「弃票」  → 弃票（rejected 为空，这是"明确不做"而不是"没答上来"）
      · 解析失败 / 调用失败 → 随机兜底一个合法目标（这一票必须投出去，
                              否则投票永远结束不了 —— 那是另一种死锁）

    thought 落库（战术备忘）**只在收割时做**（本函数在主线程里跑）——
    预跑线程不写任何会话状态。
    """
    g = self.game

    def _compute():
        v = g.get_view_for(seat)
        v["my_tactics"] = self.tactics.get(seat) or []
        v["my_plan"] = self.day_plans.get(seat) or ""
        v["day_summaries"] = dict(self.day_summaries)
        return self.agents[seat].decide_target(v, self._round_speech(g), "vote")

    try:
        d, flag = _pre_decide(self, seat, "vote", _compute)
    except Exception:                      # noqa: BLE001
        d, flag = Decision(rejected=["模型调用失败"]), None
    if flag:
        return flag
    _t = extract_control(getattr(d, "raw", "") or "").get("thought")
    if _t and str(_t).strip():
        _hist = self.tactics.setdefault(seat, [])
        _hist.append("第" + str(g.round) + "天投票：" + str(_t).strip()[:160])
        del _hist[:-3]
    if d.target is not None:
        return d.target
    if not d.rejected:
        return -1
    t = self._fallback_target(seat, "vote")
    return -1 if t is None else t

