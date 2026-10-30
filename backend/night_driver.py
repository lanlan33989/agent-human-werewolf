# -*- coding: utf-8 -*-
"""相位驱动器：状态机推进、夜间流程播报、AI 夜间动作编排、摘要任务。

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

from speech_io import NIGHT_GAP
import speech_io
import night_script
from night_pre import PrePool, WAIT as _PRE_WAIT, GIVEUP as _PRE_GIVEUP

# 人类夜间操作倒计时（2026-10-06 用户定）：窗口秒数**只在 night_script 那张表里**
# （`window_of` / `window_for_acts` / `window_for_act`）。
# 🔴 2026-10-07 第三期：这里原来还有一份窗口表（`kill`/`*` 两个键的字典），
#    和剧本表里的 `WIN_*` 是**同一个数字的两份拷贝** —— 两份必然漂移，已删。
# 超时由 drive() 注入默认动作 —— 排队、播报、超时全在服务端同一处裁决，
# 前端只渲染 seconds_left，**不做任何裁决**（这是"播报与操作同步"的结构保证）。

# 2026-10-07 用户定稿：夜间剧本表（night_script.py）—— 节点固定节拍：
#  · 节点只按初始板子过滤；药已用/已死**不跳过**（防泄露，女巫没药也走满毒药问 15s）
#  · AI 决策异步化（第二期）：天黑即后台并发预跑（守卫/狼/预言家），
#    收割点按定稿（狼刀开始收[守卫,狼]，预言家结束收[女巫,预言家]），
#    超时未完成 ⇒ 结果丢弃、默认放弃（见 night_pre.PrePool）
#  · 狼刀人类未决策 → 跟随 AI 狼（无脑跟随不触发 LLM）；全没操作 → 空刀
#  · 遗言接口：night_script.on_executed（占位）
ENABLE_DAY_PLANS = os.environ.get("DAY_PLANS", "0") == "1"  # (P2 随驱动器搬来；与 game_api 各自读 env，值一致)

import experience
import steps
import time
from agents import JUDGE_VOICE, extract_control
from engine import Phase
from typing import Optional
import steps


# ================================================================ AI 决策后台预跑（第二期）
#
# 为什么：一次 AI 决策 = 一次 LLM（2~3s）。旧路径是"窗口开始触发、drive 预算内同步跑"，
# 一夜 = 狼刀 + 验人 + 用药 + 守人串行好几秒。用户定稿要的是**天黑即并发预跑**。
#
# 关键：**计算与写入解耦** —— 预跑线程只调 Agent（拿快照 view + speech 拷贝），
# 绝不碰引擎；结果由驱动器在收割点取出来，**在锁内**写进引擎。
# 本地规则 Agent（无 chat_fn）不预跑：它零延迟，同步算就等于预跑（也让测试保持确定性）。

def _pre_kick(self, seat: int, kind: str, window: float) -> bool:
    """给一个 AI 提交一条后台决策（只在有真模型时）。"""
    pre = getattr(self, "_pre", None)
    ag = self.agents.get(seat)
    if pre is None or ag is None:
        return False
    if getattr(ag, "chat_fn", None) is None:
        return False                       # 本地 Agent：不算延迟，同步即可
    view = self.game.get_view_for(seat)    # 快照（get_view_for 的返回值）
    speech = list(self.speech)             # 快照（拷贝）
    if kind == "investigate":
        fn = lambda: ag.act_on_target(view, speech, kind)      # noqa: E731
    elif kind == "heal":
        fn = lambda: ag.decide_yes(view, speech, kind)         # noqa: E731
    else:
        fn = lambda: ag.decide_target(view, speech, kind)      # noqa: E731
    win = night_script.window_for_act(kind, _table_size(self))   # 窗口只认剧本表
    if window:
        win = float(window)
    return pre.submit(seat, kind, fn, win)


def _pump_night_pre(self) -> None:
    """入夜（或换夜）时把该预跑的 AI 决策一次性发出去。

    顺序：守卫 → 狼 → 预言家（互不依赖，所以"安全顺序"= 可以同时跑）。
    女巫**不能**此刻跑：她的决策依赖"今晚谁被刀"，等刀口定下来（`_pump_witch_pre`）。
    """
    g = self.game
    if g.phase is not Phase.NIGHT:
        return
    if getattr(self, "_pre_round", None) != g.round:
        self._pre_round = g.round
        pre = getattr(self, "_pre", None)
        if pre is not None:
            pre.discard_all()              # 上一夜的残余一律作废
        self._pre = PrePool()
    roles = {p.role.value for p in g.players}
    ai = [p for p in g.players if p.kind == "ai" and p.alive]
    if "guard" in roles:
        for p in ai:
            if p.role is Role.GUARD:
                _pre_kick(self, p.seat, "guard", 0)
    if "wolf" in roles:
        for p in ai:
            if p.role.is_wolf:
                _pre_kick(self, p.seat, "kill", 0)
    if "seer" in roles:
        for p in ai:
            if p.role is Role.SEER:
                _pre_kick(self, p.seat, "investigate", 0)


def _pump_witch_pre(self) -> None:
    """刀口定下来之后才给 AI 女巫预跑（她要看刀口）—— 解药问 yes、毒药问 target。"""
    g = self.game
    pre = getattr(self, "_pre", None)
    if pre is None or g.night.get("witch_asked"):
        return
    if g.knife_target() is None:
        return
    for p in g.players:
        if p.kind != "ai" or p.role is not Role.WITCH or not p.alive:
            continue
        potions = g.private[p.seat].potions
        if potions["heal"]:
            _pre_kick(self, p.seat, "heal", 0)
        if potions["poison"]:
            _pre_kick(self, p.seat, "poison", 0)


# ================================================================ 夜间节点时钟（第三期：窗口节拍）
#
# 定稿（逐字）：每个角色段都是
#   【播报】XX请睁眼 →（念完 +2 秒）开始倒计时 W 秒 → 窗口结束的**同时**
#   面板消失 + **同时【播报】XX请闭眼** → 再等 3 秒 → 下一个节点的开眼句。
# 所以播报**不能**入夜时一次发完，必须按窗口增量补播：
#   · 节点开始 → 播 open 句；窗口结束 → 播 close 句 + 开下一个节点。
#   · 窗口结束的判据 = base + W，base = max(节点 open 发出时刻, 该节点人类的 arm 时刻)。
#     人类 arm 由前端在"那句念完 +2 秒"时 POST /arm 通知（服务端不知道播报进度）；
#     页面失联 30 秒没 arm 就强制起算（宁可空刀也不能整局卡死）。
#   · **没有人类参与的节点也走满窗口**（定稿：读秒不可跳过、固定节拍防泄露）。
#   · 全部节点走完之前，夜**不许结算** —— 否则先落定的一步会把后面的窗口截掉
#     （老实现是自由的：AI 秒刀后同一拍就能天亮）。
#
# 状态（都在 Session 上，随夜/随节点重置）：`_nn_round/_nn_nodes/_nn_i/_nn_at/_nn_hard`。

def _nn_cur(self) -> Optional[str]:
    """当前节点 id（None = 今夜还没开 / 已走完）。"""
    i = getattr(self, "_nn_i", -1)
    nodes = getattr(self, "_nn_nodes", None) or []
    if 0 <= i < len(nodes):
        return nodes[i]["id"]
    return None


def _nn_humans_of(self, node_id: str) -> List[int]:
    """这个节点上还挂着的人类座位。

    判据是"待办里含**本节点的动作**"（不是"待办所属节点 == 本节点"）——
    女巫没答解药时她的待办是 `[heal, poison, pass]`（属 witch_heal），
    但毒药窗口到点时也得能把她收口；`confirm` 跟着 `kill` 走，所以狼也收得到。
    """
    g = self.game
    na = set(night_script.acts_of(node_id))
    if not na:
        return []
    out: List[int] = []
    for p in g.players:
        if p.kind != "human" or not p.alive:
            continue
        acts = g.actions_for(p.seat)
        if not acts:
            continue
        if set(acts) & na:
            out.append(p.seat)
        elif acts == ["noop"] and night_script.accepts_noop(node_id):
            # 女巫的占位待办也归她这两个窗口（否则没人 arm、面板也没倒计时）
            out.append(p.seat)
    return out


def _nn_ready_mono(self, node_id: str) -> Optional[float]:
    """估算「本节点那句播完 + 2 秒」的时刻（**monotonic**）。

    🔴 2026-10-22 v4（参考队友局域网版架构，源码存 `_teammate_ref/`）：
    他们的状态机只认「phase + deadline」，客户端拿 deadline 自己渲染倒计时，
    **根本没有"前端说播完了才起算"这条链** —— 我们这几天修的 arm 兜底
    v2/v3、stall 放行、过期闸，全都是这条链上的补丁。
    v4 把"起算权"收回服务端：timeline 里每句都有 at/delay/文本，服务端自己
    按**本地合成口径**（rate 0.98 的中文 ≈ 每 3.4 字 1 秒 + 0.5s 起步）模拟
    客户端的逐句播放，推到本节点那句念完，再 +2s（用户定稿口径）。
      · 估算 ≤ 真实（音频慢/云 TTS 卡）：客户端 arm（真实念完）会把窗口
        往后延 —— `max(est, armed)`，谁晚听谁的；
      · 估算 ≥ 真实（本地合成正常）：面板与倒计时按估算同弹出，满窗起走。
    返回 None = 时间线里找不到该节点（老对局/无播报句），调用方自行兜底。
    """
    tl = self.timeline or []
    idx = next((i for i, x in enumerate(tl) if x.get("node") == node_id), -1)
    if idx < 0:
        return None
    # ⚠️ 估算的每个成分（留白/字数时长/+2s 尾巴）都按 win_scale 缩放：
    #    测试把窗口缩到 2% 时没有真实语音，播报节奏必须跟着缩，
    #    否则"测试无客户端 → est 拖 3s/句 → 短跑测试直接超预算"（实测踩坑）。
    _sc = night_script.win_scale()
    # 🔴 2026-10-23 实机修复（用户："狼队、女巫毒药面板都没弹出"）：
    #    回溯累加只统计**夜间剧本句**（带 `node` 字段的）—— timeline 里还躺着
    #    上一个白天的玩家长发言（几百字/句，按 3.4 字/秒一算单句就虚增几十秒），
    #    把起算时刻推到一分钟后 ⇒ 面板/倒计时全被拖死。
    #    白天句不进夜间播报队列；万一真有队列积压（上一节点的闭眼句还没念完），
    #    它带 node，串行累加照样会算进去，客户端 arm（真实念完）再兜一层。
    # ★ 2026-10-29（用户定：「等法官念完投票结果和出局信息 → 喊完『天黑请闭眼』
    #   才进夜间页面」）：上面那条"只统计带 node 的句"**跳过头了** ——
    #   投票结果 / 出局信息 / 天黑请闭眼前面的死讯全都是 `engine.log()` 的产物
    #   （`t == "sys"`、**没有 node 字段**），却被当成"不在夜间队列里"直接跳过。
    #   后果：白天那几句还在播，守卫面板就已经弹出来了。
    #   现在把**法官句（t == "sys"）**也算进串行累加：它们短（十几字），
    #   而且 `max(t, start)` 会自动把"客户端积压"这一段如实并进估计值
    #   ⇒ 面板、读秒、播报三者仍然同拍，只是那一拍真的挪到了「天黑请闭眼」念完之后。
    #   ⚠️ **玩家发言（t == "say"）必须继续跳过**：那是几百字的正文，一句就虚增
    #      几十秒；而且白天的发言时长由驱动器的定时器裁决，跟客户端队列不同步，
    #      把积压串进来会把起算点推到一分钟后（2026-10-23 实机事故，见上）。
    return _queue_end_mono(self, idx)


def _queue_end_mono(self, idx: int) -> Optional[float]:
    """timeline 第 `idx` 句**念完**的时刻（**monotonic**）。

    串行累加口径（夜间面板与白天发言共用同一份，绝不各写一套）：
      · 参与累加的：夜间剧本句（带 `node`）+ 法官句（`t == "sys"`）
      · 跳过的：玩家长发言（`t == "say"`，几百字一句会虚增几十秒）
      · `max(t, start)` 把"客户端积压"如实并进来 —— 上一句没念完不能开下一句
      · 每句时长 = `0.5 + 字数/3.4`（rate 0.98 的中文本地合成口径）
      · 全部按 `win_scale` 缩放（测试缩窗时没有真实语音，节奏必须跟着缩）

    ★ 2026-10-30：从 `_nn_ready_mono` 里抽出来的。用户实机报
      「夜间还没结束就显示 4 号正在发言、倒计时还在减」——
      根因是**发言的 120 秒从"驱动推进到 DISCUSS 那一拍"起算**，
      而那一刻法官还在念「天亮了…昨夜 X 出局…4号请发言」。
      现在 `_sp_state` 也调这个函数，面板/读秒/播报同拍。
    """
    tl = self.timeline or []
    if idx < 0 or idx >= len(tl):
        return None
    _sc = night_script.win_scale()
    t = None
    for j in range(idx + 1):
        x = tl[j]
        if "node" not in x and x.get("t") != "sys":
            continue                  # 玩家发言 / 私有记录：不在法官播报队列里
        start = (x.get("at") or x.get("ts") or 0.0) + _sc * (x.get("delay") or 0.0)
        if t is not None and t > start:
            start = t                 # 队列串行：上一句没念完不能开下一句
        t = start + _sc * (0.5 + len(x.get("s") or "") / 3.4)
    if t is None:
        return None
    # ★ 2026-10-29（用户定：「喊完XX请睁眼后**立即**弹出窗口」）：
    #   尾巴的 +2 秒去掉了 —— 那一截是 2026-10-06 "播报最后一个字后 2 秒起算"
    #   的旧口径，用户实机的观感就是"念完了面板还要愣两秒才出来"。
    #   现在起算点 = 那句**念完**的那一刻，面板、读秒、播报三者同拍。
    return time.monotonic() + (t - time.time())            # 墙钟 → monotonic


def _speech_ready_mono(self, kind: str, seat: int) -> Optional[float]:
    """「N 号请发言」/「N 号请留遗言」那句**念完**的时刻（monotonic）。

    找不到那条播报（老对局 / 语音关闭 / AI 发言没有开场句）时返回 None，
    调用方按"立刻开始"处理 —— 宁可早，不可把人的麦锁住。

    ⚠️ 从后往前找：同一个座位在一轮里可能说两次（平票加时），
       要找的是**刚刚这一条**，不是第一轮那条。
    """
    say = f"{seat + 1}号请留遗言" if kind == "lastword" else f"{seat + 1}号请发言"
    tl = self.timeline or []
    for i in range(len(tl) - 1, -1, -1):
        x = tl[i]
        if x.get("t") == "sys" and (x.get("s") or "") == say:
            end = _queue_end_mono(self, i)
            if end is None:
                return None
            # ★ 夹一个上限：**最多只等这一句的朗读时长**。
            #   串行累加在"积压"时会一路往后推（服务端一口气写了好几句，
            #   实测能推出十几秒），而客户端的 `lines_due` 对落后太多的旧句是
            #   **直接跳过**的 —— 它不会真按服务端的积压节奏一句句补完。
            #   不夹的话，一旦积压，人就要干等十几秒才拿到麦。
            _sc = night_script.win_scale()
            cap = time.monotonic() + _sc * (0.5 + len(say) / 3.4)
            return min(end, cap)
    return None


def _table_size(self) -> int:
    """这一桌多少人（狼刀窗口按人数算：6→30s / 9→60s / 12→90s）。

    只读 `game.players`，不缓存 —— 人数在一局里不会变，但缓存一个
    "开局人数" 会让测试里临时拼的小局算错档位。
    """
    return len(getattr(self.game, "players", []) or [])


def _nn_deadline(self, node_id: str) -> float:
    """窗口结束时刻（monotonic 时间戳）。

    🔴 2026-10-07 第六起（用户实机："人狼操作面板未出现，AI 狼的决策直接作为了
    最终决策"）：**没 arm 时不许按 `open + 窗口` 到点收人** —— 那等于"面板没出来
    ⇒ 人类一次都没动 ⇒ 到点注入默认（狼刀 = 跟随 AI 狼）"。
    没有 arm 就把窗口**往后推半个窗口**再起算 ⇒ 不变式：**人类永远拿得到一个整窗**。

    ⚠️ 这条保证必须写在**这里**（只要看"armed 有没有"），不能写成"计数器攒够 N 秒"
    —— 那个计数会被 `pending` 在 `working`/`human_night` 之间来回切反复清零，
    实测**时灵时不灵**（预言家段生效、狼/女巫段不生效，正是这个原因）。
    """
    win = night_script.window_of(node_id, _table_size(self))
    base = self._nn_at
    if getattr(self, "_nn_need_arm", False):
        # 🔴 v4：没 arm 时不再"往后推半个窗口"（粗兜底），改用**服务端自己的
        #    播报完成估算**精确起算（念完 +2s）—— 倒计时从满窗起走，面板与
        #    倒计时同拍。
        # 🔴 2026-10-23 实机（用户："毒药读秒 12→14→13、预言家从 16 开始"）：
        #    arm **不再延长窗口**。原来 max(est, armed) 谁晚听谁 —— 面板弹出
        #    （音频门晚于 est）那一刻窗口已走到中段，arm 一落地 deadline 又
        #    往后跳 ⇒ 读秒先见到 12、又回涨到 14。起算权既已收回服务端（v4），
        #    窗口就只由 est 裁决：面板弹出（= started）与读秒同拍、整窗走满。
        #    估算失效（None）⇒ 才退回 arm / 半窗兜底（老对局没有估算依据）。
        est = _nn_ready_mono(self, node_id)
        if est is not None:
            base = max(base, est)
        else:
            armed = getattr(self, "human_wait_since", None)
            if armed is not None:
                base = max(base, armed)     # 老对局：arm 是唯一"念完了"的信号
            else:
                base = base + win * 0.5     # 连 arm 都没有：退回旧的半窗兜底
    return base + win


def _already_answered(self, seat: int, node_id: Optional[str]) -> bool:
    """这个人是不是在**本节点**上已经表过态了（2026-10-29 窗口内可改）。

    用途只有一个：`_inject_default` 到点注入默认动作时**别把他改过的选择盖掉**。
    以前守卫/女巫/预言家"点了就从待办消失"，到点注入根本不会命中；
    现在窗口内一直可改，"到点注入"就有了覆盖用户选择的风险 —— 必须拦。
    """
    n = self.game.night
    if node_id == "wolf":
        return seat in (n.get("wolf_points") or {})
    if node_id == "guard":
        return bool(n.get("guard_asked"))
    if node_id == "witch_heal":
        return bool(n.get("heal_picked"))
    if node_id == "witch_poison":
        return bool(n.get("poison_picked"))
    if node_id == "seer":
        return n.get("investigate") is not None
    return False


def _inject_default(self, seat: int, acts: List[str],
                    node_id: Optional[str] = None) -> None:
    """到点未操作 ⇒ 注入默认动作（用户定）。**必须在 self.lock 内调用**。

    🔴 2026-10-08 表化①（用户定的「alarm 架构」）：**注入什么由剧本表的
    `default` 列说了算**，不再按 acts 猜 —— 第四起事故（解药到点注入 pass
    把毒药那一问掐死）的案发地就是原来那串分支。现在只有三种：
      · 'pass'        → 放弃（守卫 / 女巫毒药 / 预言家；猎人开枪兜底同此）
      · 'follow_kill' → 狼刀跟随任何已点过的狼（人/AI 都算），全没点过才空刀
      · None          → **什么都不注入**（女巫解药那一屏 —— 无论有没有刀口：
        注入 pass 会把毒药那一问掐死；解药的默认本来就是"不用"，引擎只认显式 heal）
    """
    if not acts or acts == ["noop"]:
        return          # noop 占位 = 没有可决策的东西（前端照常走满窗口，这里什么都不注入）
    # ★ 2026-10-29：他已经选过了 ⇒ 绝不注入（到点注入的意义是"什么都没做的兜底"，
    #   不是"把最后一秒改过的选择盖回去"）。
    if _already_answered(self, seat, node_id):
        return
    dflt = night_script.default_of(node_id)
    if dflt is None:
        return
    try:
        if dflt == "follow_kill":
            # 🔴 第八起（"轮到女巫显示无人被刀"）：到点默认**绝不注入空刀** ——
            #    跟**任何一只已经点过的狼**（人 / AI 都算，按座位序取第一个），
            #    用**它点的目标**、跳过空刀提议（-1）；真的谁都没点过才 -1。
            g = self.game
            pts = g.night.get("wolf_points") or {}
            # ★ 2026-10-30（用户定「真人选5号、AI 都选6号 ⇒ 刀5号」）：
            #   真人狼**整窗没点**就是"没表态"，绝不替他点刀。
            #   以前这里给他注入"跟随第一只已点的狼"，等于**伪造真人意志** ——
            #   新规则下"真人表态即定刀"，这一注入会让 AFK 的真人把 AI 的刀口
            #   抢成自己的（而且只跟随座位序第一只狼，未必是 AI 的多数）。
            #   未表态 ⇒ 结算时回落到 AI 多数决（`engine._settle_wolf_kill` ②）。
            if g.player(seat).kind != "ai":
                g.note(f"{seat + 1}号（真人狼）整窗没表态，刀口交给 AI 多数决。")
                return
            follow = None
            for pp in g.players:
                if pp.role.value == "wolf" and pts.get(pp.seat) not in (None, -1):
                    follow = pts[pp.seat]
                    break
            self._act_locked(seat, "kill", follow if follow is not None else -1, None)
            if "confirm" in g.actions_for(seat):
                self._act_locked(seat, "confirm", None, None)
            return
        self._act_locked(seat, "pass", None, None)
    except Exception as e:                      # noqa: BLE001
        # 注入失败必须留痕（静默降级会让"到点没反应"变成查不出的谜）
        print(f"[night] 到点注入默认动作失败 seat={seat} acts={acts}：{e}", flush=True)


def _close_say_secs(nd: Dict) -> float:
    """闭眼句念完要多久（秒，已按 `win_scale` 缩放）。没有闭眼句 = 0。

    用它当"节点推进的延迟"：面板要活到这句话念完（2026-10-29 用户定），
    所以节点不能一关就跳。估算口径与 `_nn_ready_mono` 同源（0.5s 起步 + 3.4 字/秒），
    别再立第二个数字。
    """
    s = nd.get("close") or ""
    if not s:
        return 0.0
    return night_script._scaled(0.5 + len(s) / 3.4)


def _nn_open(self, i: int) -> None:
    """开一个节点：播 open 句 + 记窗口起点 + 收 `harvest_start` 的决策。"""
    nd = self._nn_nodes[i]
    nid = nd["id"]
    self._nn_i = i
    self._nn_at = time.monotonic()
    # ★ 2026-10-29（用户定：「执行顺序与播报顺序一致」+ 窗口内可改）：
    #   把"现在问的是哪一问"写给引擎。引擎没有时钟、也不该有时钟，
    #   但它必须知道当前是解药窗口还是毒药窗口 —— 以前用一个 witch_asked
    #   糊两问，结果是用了解药就再也不会毒人。
    self.game.night["stage"] = nid
    self._nn_closing = None
    self._nn_need_arm = bool(_nn_humans_of(self, nid))
    self._nn_tick15 = False                  # 每个节点重置「还剩15秒」只喊一次
    self.human_wait_since = None            # 换节点 ⇒ 换窗口：旧 arm 一律作废
    self.human_wait_pending_since = None
    if nd.get("say"):
        # ★ 2026-10-29：预言家确认的那一刻已经念过「他是这个」了 —— 这个节点
        #   再念一遍就是全场听两次。节点本身照旧留着走它的 3 秒留白。
        _already = (nid == "seer_result"
                    and self.game.night.get("seer_result_spoken"))
        # `at` = 这一行**产生**的时刻（墙上时间）—— 定稿的验收标准是
        # "闭眼句必须在倒计时归零之后才念出来"，能不能验收就看它能不能被量出来。
        if not _already:
            self.timeline.append({"t": "sys", "s": nd["say"], "voice": JUDGE_VOICE,
                                  "delay": nd.get("gap") or 0,
                                  "stage": night_script.stage_of(nid),
                                  "node": nid, "at": time.time()})
    for kind in (nd.get("harvest_start") or ()):
        self._nn_hard.add(kind)             # 定稿：狼刀节点**开始**收 [守卫, 狼]


def _nn_close(self, nd: Dict) -> None:
    """结算一个节点：注入人类默认动作 → 播 close 句 → 收 `harvest_end`。

    🔴 2026-10-07 真机事故修复：注入默认动作走的是 `_act_locked`，而它内部会**回调
    `auto_advance`** ⇒ 又进 `_advance_night` ⇒ 又进本时钟 ⇒ 又"到点"收口同一个节点
    ⇒ **无限递归**（实测 124 层）。每一层解栈时都往 timeline 补一句「狼人请闭眼」
    ⇒ 真人那局听见的就是"狼人请闭眼穿插在很多节点里重复播报"，而且那堆句子把前端
    播报队列灌满 ⇒ 门控（念到该角色那句才放行面板）永远轮不到 ⇒ 人类（狼）面板
    不弹、也没读秒。
    ⇒ 注入期间立 `_nn_injecting` 旗，内层的 `_night_clock` 直接返回（不拨钟、不收口）。
    """
    nid = nd["id"]
    self._nn_injecting = True
    try:
        for seat in _nn_humans_of(self, nid):
            _inject_default(self, seat, self.game.actions_for(seat), nid)
    finally:
        self._nn_injecting = False
    # ★ 2026-10-28（用户定）：狼刀**在窗口结束的这一刻**结算 ——
    #   2026-10-30 起：真人狼表过态就以他为准（空刀也算表态），
    #   真人整窗没动才按 AI 刀口多数决、平票随机（见 engine._settle_wolf_kill）。
    #   顺序很重要：先注入人类默认动作（**只对没表态的 AI 狼**注入 follow_kill，
    #   真人狼不注入 —— 他"没点"就是没表态，不能替他伪造一票），再结算。
    if nid == "wolf":
        self.game.settle_wolf_window()
    # ★ 2026-10-29（用户定：窗口内可改、以结束时为准）：另外四个窗口同理，
    #   **扣药 / 定案 / 置 asked 全部推到这一刻**。顺序同样重要 ——
    #   先注入人类默认动作（没动的人按默认），再结算。
    elif nid == "guard":
        self.game.settle_guard_window()
    elif nid == "witch_heal":
        self.game.settle_witch_heal()
    elif nid == "witch_poison":
        self.game.settle_witch_poison()
    elif nid == "seer":
        self.game.settle_seer_window()
    if nd.get("close"):
        self.timeline.append({"t": "sys", "s": nd["close"], "voice": JUDGE_VOICE,
                              "delay": nd.get("close_gap", night_script.GAP),
                              "node": nid, "at": time.time()})
    for kind in (nd.get("harvest_end") or ()):
        self._nn_hard.add(kind)             # 定稿：预言家节点**结束**收 [女巫, 预言家]


def _night_clock(self) -> bool:
    """按窗口把节点时钟往前拨（**必须在 self.lock 内**）。返回 True = 有播报产出。"""
    g = self.game
    if g.phase is not Phase.NIGHT:
        return False
    # 🔴 注入默认动作会回调 auto_advance 再进到这里（见 _nn_close 的注释）——
    #    里层一律不拨钟，否则同一个节点会被反复收口（真机事故：124 条「狼人请闭眼」）。
    if getattr(self, "_nn_injecting", False):
        return False
    if getattr(self, "_nn_round", None) != g.round:
        self._nn_round = g.round
        roles = {p.role.value for p in g.players}
        self._nn_nodes = [nd for nd in night_script.build(roles, g.round)
                          if nd["id"] not in night_script.SKIP_IN_ANNOUNCE]
        self._nn_i = -1
        self._nn_at = 0.0
        self._nn_need_arm = False
        self._nn_hard: set = set()
        self._nn_tick15 = False
        self.human_wait_since = None
        self.human_wait_pending_since = None
    out = False
    for _ in range(12):                     # 一拍最多连推 12 个节点（零窗口的收尾三连）
        if self._nn_i < 0:
            _nn_open(self, 0)
            # "天黑请闭眼"那一句是**引擎**自己喊的（表里的 `open` 节点由引擎承担，
            # 见 SKIP_IN_ANNOUNCE）—— 它的 3 秒留白只能在这儿补上：
            # 定稿「播报最后一个字之后等待 3 秒 → 【播报】守卫请睁眼」。
            # 判据 = 刚由 `_flush_log` 落进 timeline 的最后一条（文本含"天黑请闭眼"）。
            if self.timeline:
                _last = self.timeline[-1]
                if (_last.get("t") == "sys" and not _last.get("delay")
                        and "天黑请闭眼" in (_last.get("s") or "")):
                    _last["delay"] = night_script.GAP
            out = True
            continue
        if self._nn_i >= len(self._nn_nodes):
            break
        nd = self._nn_nodes[self._nn_i]
        # 🩹 2026-10-07（I9 的女巫两问）：`_nn_need_arm` 是**节点打开那一刻**算的 ——
        #    那时候女巫的待办可能还没出现（她的动作要等狼刀定案之后才有）⇒ 整段被当成
        #    "没有人类要点" ⇒ 没有整窗保证（I9 的女巫两问就是这么红的）。
        #    这里每拍补判一次：待办一出现，就把这一段升级成"要点"。
        if not self._nn_need_arm and _nn_humans_of(self, nd["id"]):
            self._nn_need_arm = True
            self.human_wait_pending_since = None
        # ★ 2026-10-28（用户定）：狼刀窗口**剩 15 秒**时法官喊一句「还剩15秒」。
        #   只对狼刀节点、只在窗口比 15 秒长的时候发；每个节点只发一次。
        #   ⚠️ 这一条**故意不带 `node` 字段**：前端的 `linePlayed(node)` 取的是
        #     "最后一条带该 node 的播报"，挂上 node 会让狼刀面板等到这句念完才弹
        #     （= 面板晚 15 秒出现）。它只是播报，不是门控。
        if (nd["id"] == "wolf" and not getattr(self, "_nn_tick15", False)
                and night_script.wolf_has_last_call(_table_size(self))):
            _size = _table_size(self)
            _dl15 = _nn_deadline(self, nd["id"])
            _win15 = night_script.window_of("wolf", _size)
            _off = night_script.wolf_last_call_offset(_size)
            _earliest = self._nn_at + max(0.0, _win15 - _off)
            if time.monotonic() >= _earliest and time.monotonic() >= _dl15 - _off:
                self._nn_tick15 = True
                self.timeline.append({"t": "sys", "s": night_script.WOLF_LAST_CALL_SAY,
                                      "voice": JUDGE_VOICE, "delay": 0,
                                      "tick": "wolf_15", "at": time.time()})
                out = True
        if time.monotonic() < _nn_deadline(self, nd["id"]):
            break
        # ★ 2026-10-29（用户定：「喊完『守卫请闭眼』后立即退出守卫窗口」）：
        #   闭眼句是窗口结束的**同一刻**产生的。以前这一刻就推进到下一节点
        #   ⇒ 面板先被收掉、闭眼句还在念，用户听到的是"窗口比闭眼句退得早"。
        #   现在：窗口结束 → 产生闭眼句 → **等它念完** → 才推进节点。
        #   这期间 `_nn_cur` 仍是本节点，`now_state` 会带上 `closing`，
        #   前端据此把面板留到这句话念完。
        if not getattr(self, "_nn_closing", None):
            _nn_close(self, nd)
            self._nn_closing = {"node": nd["id"],
                                "until": time.monotonic() + _close_say_secs(nd)}
            out = True
        if time.monotonic() < self._nn_closing["until"]:
            break
        self._nn_closing = None
        self._nn_i += 1
        self._nn_need_arm = False
        self._nn_tick15 = False
        self.human_wait_since = None
        self.human_wait_pending_since = None
        if self._nn_i < len(self._nn_nodes):
            _nn_open(self, self._nn_i)
        out = True
    return out


def _night_clock_done(self) -> bool:
    """今夜的节点都走完了没有（没走完 ⇒ 夜不许结算）。"""
    if getattr(self, "_nn_round", None) != self.game.round:
        return False
    return getattr(self, "_nn_i", -1) >= len(getattr(self, "_nn_nodes", None) or [])


def _harvest(self) -> None:
    """
    局末收割**经验候选**（G3），只做一次。

    为什么挂在"状态变化之后"而不是"判出胜负的那一刻"：一局可能从
    /act 里结束，也可能从 /public 的推进里结束（两条路都要覆盖），
    挂在两个入口各自调一次最省心，靠 _harvested 保证幂等。

    ⚠️ 收到的只是**候选**（pending），不是生效的经验 —— 必须复核入库后
    才会进下一局的 prompt（离线评审，不做自动进化）。
    """
    if getattr(self, "_harvested", False):
        return
    if self.game.winner not in ("wolf", "good"):
        return
    self._harvested = True
    experience.BANK.add_candidates(
        experience.generate_candidates(self.game, self.level, self.id))


def drive(self, budget: float = 1.5) -> str:
    """
    把 AI 的回合再往前推最多 budget 秒。由轮询（/public）调用。

    ⚠️ 闸门判据是「**现在还有没有该推进的事**」，不是 pending 这个字符串。
    这里以前写的是：

        if self.pending == "working" or (
                not self.pending.startswith("human_") and self.pending != "ended"):

    它造成过一个**真死锁**（用户实测报的「投票后归票卡住、归票没出」）：

        act() 里 cast_vote() 成功、紧接着的 auto_advance() 抛错
        ⇒ /act 返回 400，而 pending 就停在 "human_vote"。
        可票其实已经齐了 ⇒ 之后每一次 GET /public 都被这道闸门挡住
        ⇒ 永远不归票、不写 timeline、连票型都看不到（VOTE 阶段不公布票型）。

    现在改成**无条件推进**，安全理由：
      · auto_advance 内部只让 AI 出手（_run_one_ai_night / _run_one_ai_vote
        都按 kind == "ai" 筛人），所以「替人类那一步」不会发生；
      · 遇到人类待办它会**立刻**返回 human_*（不做事、不花模型调用）；
      · 于是 pending 过期时，这一行就是自愈点。
    """
    # ★ 收割要放在**提前返回之前**：局已经结束的局不会再往下推，
    #   如果收割排在 return 之后，那么"由 /act 结束的那一局"就永远收不到经验
    #   （前端结束时不会再来 /public 推进，而这里本来会提前返回）。
    self._harvest()
    if self.pending == "ended":
        return self.pending
    self.ensure_night_summaries()         # 🔴 天黑投票后：前几天压成逐日摘要
    # 🔴 天亮预盘：默认停用（见 ENABLE_DAY_PLANS 的说明），接口保留
    if ENABLE_DAY_PLANS:
        self.ensure_day_plans()
    _t_end = time.monotonic() + budget
    while True:
        with self.lock:
            _human_night_timeout(self)        # 🔴 失联兜底：arm 迟迟不来就强制起算窗口
            self.pending = self.auto_advance(deadline=_t_end)
        # 🔴 2026-10-07 第三期：夜间节点时钟走的是**墙上时间**（定稿：读秒不可跳过），
        #    窗口没走完时 auto_advance 只能报 working。既然"一次请求有一个预算"，
        #    就在这里把预算用掉 —— 但要**在锁外等**：捏着锁等 1.5 秒的话，
        #    人类按号码的 /act 会排在后面（手感上就是"点了没反应"）。
        if self.pending != "working" or time.monotonic() >= _t_end:
            break
        time.sleep(0.03)
    self._harvest()
    return self.pending


def _human_night_timeout(self) -> None:
    """人类待办的兜底裁决（**必须在 self.lock 内调用**）。

    🔴 2026-10-07 第三期：**夜间的窗口与"到点注入默认"搬到节点时钟了**
    （见 `_night_clock` / `_nn_close`）—— 因为闭眼句必须与窗口结束**同时**念，
    而"窗口什么时候结束"只能由节点时钟裁定（老实现按"第一个有动作的人类"算窗口，
    和播报节点对不上）。这里只剩两件事：

      · 夜间：**arm 失联兜底** —— 页面关了/网络断时 pending 挂 30 秒还没 arm，
        就强制起算窗口（宁可到点空刀，也不能让整局卡死）。
      · 开枪：猎人开枪**不在剧本节点里**（定稿没给猎人节点），仍按自己的 15 秒窗口
        到点注入"放弃开枪"。
    """
    g = self.game
    # 🔴 2026-10-07 第六起（用户实机："人狼操作面板未出现，AI 狼的决策直接作为了
    #    最终决策"）—— **真凶在这里**：下面这句"pending 不是 human_night/shoot 就清空
    #    arm 状态"，而夜里 `pending` 会在 **`working`（时钟在走/AI 在算）与
    #    `human_night`（该人类动）之间来回切** ⇒ `human_wait_pending_since` 被反复清零
    #    ⇒ 那条"arm 失联兜底"**从来没有真正累积到阈值** ⇒ 面板没出来（没人 arm）时，
    #    节点照样按 `open + 窗口` 到点收口 ⇒ 注入默认（狼刀 = 跟随 AI 狼）
    #    ⇒ 人类狼一次都没动，AI 的刀成了定案。
    _night_clocking = (g.phase is Phase.NIGHT
                       and bool(getattr(self, "_nn_need_arm", False)))
    if self.pending not in ("human_night", "human_shoot") and not _night_clocking:
        self.human_wait_since = None
        self.human_wait_pending_since = None
        return
    now = time.monotonic()
    if self.pending == "human_night" or _night_clocking:
        if not getattr(self, "_nn_need_arm", False):
            return
        if getattr(self, "human_wait_since", None) is None:
            # 未 arm：倒计时本该从「播报最后一个字后 2 秒」起算，由前端 POST /arm 通知
            #（服务端不知道播报进度，不能自己起算）。失联兜底见上。
            if getattr(self, "human_wait_pending_since", None) is None:
                self.human_wait_pending_since = now
            else:
                # 🔴 2026-10-07 第六起（用户实机："人狼操作面板未出现，AI 狼的决策直接
                #    作为了最终决策"）：这里原来是**固定的 30 秒** —— 而狼刀窗口只有 20 秒！
                #    ⇒ "面板没出来 ⇒ 前端不 arm ⇒ 窗口按 open+W 先跑完 ⇒ 到点注入默认"
                #    ⇒ 人类狼一次都没动，AI 的刀直接成了定案。
                #    病根不只是"30 太大"，而是**它是个定值**：窗口 15/20 秒，兜底必须
                #    比窗口**短**才来得及。现在改成 **半个窗口内必起算** ⇒
                #    不变式：**任何情况下人类都拿得到一个整窗**（arm 之后满 W，
                #    因为 `_nn_deadline` = max(节点开始, arm 时刻) + 窗口）。
                _win = night_script.window_of(_nn_cur(self) or "", _table_size(self))
                if now - self.human_wait_pending_since > min(8.0, _win * 0.5):
                    pass   # 🔴 2026-10-07 第九起：**绝不替前端 arm** —— 一置上
                    # `now.started` 就变真、前端立刻开始读秒（用户实机："倒计时开始
                    # 的时点不对，应该在『狼人请睁眼』播报完成后"）。"窗口别太短"
                    # 由 `_nn_deadline` 保证（没 arm 就往后推半个窗口）。
        return
    # ---- human_shoot：不在剧本节点里，自己管窗口 ----
    wait = None
    for p in g.players:
        if p.kind == "human" and g.actions_for(p.seat):
            wait = p
            break
    if wait is None:
        self.human_wait_since = None
        return
    if getattr(self, "human_wait_since", None) is None:
        if getattr(self, "human_wait_pending_since", None) is None:
            self.human_wait_pending_since = now
        elif now - self.human_wait_pending_since > 30:
            self.human_wait_since = now
        return
    acts = g.actions_for(wait.seat)
    if now - self.human_wait_since < night_script.window_for_acts(acts, _table_size(self)):
        return
    _inject_default(self, wait.seat, acts)
    self.human_wait_since = None

# ---------------------------------------------------------- 驱动器

def auto_advance(self, max_steps: int = 300,
                 deadline: Optional[float] = None) -> str:
    """
    把状态机推到下一个「需要人类输入」的点，途中自动跑完 AI 的动作。

    `deadline`（time.monotonic 的时间戳）是**单个 HTTP 请求的时间预算**：
    到点就返回 `"working"`（= 还在推进，接下来靠 /public 轮询接着推）。
    没有它的话，大人数局的一次构造会跑几十秒 —— 页面表现成"开局卡死"。
    """
    g = self.game
    # 2026-10-07 第二期：把本次推进的预算挂给"预跑收割"用 ——
    # AI 决策在后台跑时，收割点**在预算内等它**（不干等、也不无限等），
    # 超预算就返回 "working"，下一次轮询接着等（见 ai_behaviors._pre_decide）。
    self._adv_deadline = deadline
    try:
        # 🔴 胜负保险丝（2026-10-02 用户要求：狼人数 ≥ 好人数即狼胜，
        #    无论死在白天投票还是夜里刀/毒/带）。各死亡点本已判胜
        #    （夜结算 / 猎人开枪 / 投票结算），这里是兜底。
        if g.phase is not Phase.ENDED and g.winner is None:
            _w = g.check_winner()
            if _w:
                g.winner = _w
                g.phase = Phase.ENDED
                g.log("游戏结束，" + ("狼人胜利。" if _w == "wolf" else "好人胜利。"))
                self.pending = "ended"
                return self.pending
        for _ in range(max_steps):
            if deadline is not None and time.monotonic() > deadline:
                return "working"
            if g.phase is Phase.ENDED:
                return "ended"

            # ★ 提示词实验室的"门"（2026-10-26）：
            #   实验室按了暂停 ⇒ 这一步先不走，返回 "working"（= 还在推进，
            #   靠 /public 轮询接着推，是这个返回值的既有语义，不需要改调用方）。
            #   按了"单步"⇒ 放行一步，走完自动停住。
            #   没有控制文件时（默认）gate() 直接返回 False，行为完全不变。
            try:
                import lab_api as _lab
                _lab.set_ctx(round=getattr(g, "round", 0),
                             phase=getattr(getattr(g, "phase", None), "value", None)
                             or str(getattr(g, "phase", "") or ""))
                if _lab.gate():
                    return "working"
                # ★ 局面快照（2026-10-26）：gate 放行后、handler 动引擎之前 ——
                #   上一步已完整落定、下一步未动的干净点。回退=恢复到这里，
                #   重跑的是**整步**（一步可能含多次模型调用，如狼队协商）。
                _lab.maybe_snapshot(self)
            except Exception:
                pass

            # ★ 相位剧本（H1）：循环只问"这个相位交给谁"，具体逻辑在各段剧本里。
            #   以前这里是一串 `if g.phase is …`，加一个相位就要在循环里再插一段。
            handler = steps.handler_for(g.phase)
            if handler is None:
                # 没配剧本的相位：**明确报错**，不要静默转圈 ——
                # 静默转圈的表现是"这一局不动了"，而根因只是一个拼错的相位名。
                print(f"[phase] 没有配剧本的相位：{g.phase.value}", flush=True)
                return "stuck"
            res = getattr(self, handler)()
            # ★ 单步额度在**一步完整走完**后才扣（lab_api.gate 只放行不扣）。
            #   "working" = 这一步还没走完（预跑等待/夜钟补播中途交回轮询），
            #   不能算完成 —— 否则单步退化成半步（2026-10-26 回退重跑时暴露）。
            if res != "working":
                try:
                    import lab_api as _lab
                    _lab.step_done()
                except Exception:
                    pass
            if res == "continue":
                continue
            return res

        return "stuck"
    finally:
        self._adv_deadline = None


# ---- 相位剧本（steps.PHASE_STEPS 的 handler 字段指过来）----
# 每段都返回："continue" = 推进了一步继续循环；其它字符串 = 该谁出手了（pending 值）

# 🔴 2026-10-07 第三期：`_announce_night()` **已删除**。
#    老实现（第二期）是"入夜时把整夜的开眼句 + 闭眼句一次性塞进 timeline"，
#    它结构上做不到定稿要求的"闭眼句在**窗口结束的同时**念"——闭眼句必须等到
#    窗口真的走完那一刻才产生。现在整夜播报由 `_night_clock()` 增量补播，
#    防重标记 `night["night_announced"]` 随之退休（改由 `_nn_round` 管）。


def _advance_night(self) -> str:
    g = self.game
    # ① 节点时钟：按窗口增量补播。有产出就交回循环（下一拍接着算 pending）。
    if _night_clock(self):
        self._flush_log()
        return "continue"
    # ② 夜**结算的前提**：步骤做完 **且** 节点时钟走完。
    #    只判前者的话，先落定的一步（如 AI 秒刀）会把后面几个角色的窗口截掉
    #    —— 那正是定稿要防的"读秒可跳过、玩家能加速夜间进程"。
    if self._night_done():
        if _night_clock_done(self):
            g.resolve_night()
            self._flush_log()
            return "continue"
        return "working"                 # 固定节拍：等窗口走完（前端继续轮询）
    _pump_night_pre(self)
    _pump_witch_pre(self)
    r = self._run_one_ai_night()
    if r is True:
        self._flush_log()
        return "continue"
    if r == "wait":
        # AI 决策还在后台跑（预跑没到收割点）⇒ 报 working，让前端继续轮询。
        return "working"
    # ★ 2026-10-28：狼刀窗口拉长之后会出现「**没人该动，纯粹在等窗口**」的时刻
    #   （全 AI 狼队：点子都点完了，但要等 30~90 秒的窗口才闭眼结算）。
    #   这时若仍报 `human_night`，调用方会以为"该递手机了"而立刻返回 ⇒
    #   墙上时间不再流逝（drive 只在 working 时才睡），窗口就永远走不完。
    #   报 working 才是实话：服务端还在推进，只是没有人类待办。
    if not any(_human_blocking(g, p) for p in g.players
               if p.kind == "human" and p.alive):
        return "working"
    return "human_night"


def _human_blocking(g, p) -> bool:
    """这位人类是不是**真的卡住了流程**（决定要不要报 human_night）。

    🔴 2026-10-28（狼刀整窗可改的连带修正）：按用户定的「这期间可任意修改刀口，
    以窗口结束时的选择为准」，`kill` 会在**整个窗口**里一直挂在待办列表上 ——
    它是"随时可改"，不是"还没交"。

    可 `_advance_night` 的老判据只看"待办列表空不空"，于是人类狼一睁眼 pending
    就整窗停在 `human_night`：调用方把它读成"该递手机了"⇒ 立刻返回 ⇒ 墙上时间
    不再流逝（`drive()` 只在 `working` 时才在锁外 sleep）⇒ 30/60/90 秒的窗口
    永远走不完，局面卡死在第一夜。

    所以这里把"只剩 kill/confirm、且这位狼**已经交过点子**"判为**不阻塞**：
    面板照旧开着（前端靠 `/pending_seats` 决定画不画，不看 pending），
    但消费者知道服务端还在推进，该等窗口就等窗口。

    ⚠️ 只判 kill/confirm —— 女巫的 heal/poison/pass、预言家的 investigate
    都是"交了就消失"的一次性待办，它们留在列表里就是**真待办**，不能放行。
    """
    acts = g.actions_for(p.seat)
    if not acts:
        return False
    # ★ 2026-10-29（窗口内可改的必然代价）：点了之后 `actions_for` 仍然挂着这个动作
    #   （否则面板当场消失、改都没法改），但那**不等于他在卡流程** ——
    #   不把这条判出来的话，pending 会整窗停在 human_night，`drive()` 就不再
    #   在锁外 sleep，墙钟不流逝 ⇒ 15/90 秒的窗口永远走不完，局面卡死在第一夜。
    n = g.night
    if set(acts) <= {"kill", "confirm"}:
        if p.seat in (n.get("wolf_points") or {}):   # -1（空刀）也算交过了
            return False
    if set(acts) <= {"guard"} and n.get("guard_asked"):
        return False
    if set(acts) <= {"heal", "pass"} and n.get("heal_picked"):
        return False
    if set(acts) <= {"poison", "pass"} and n.get("poison_picked"):
        return False
    # ★ 2026-10-29：预言家同理 —— `investigate` 现在也是"窗口内随时可改"的，
    #   点完人待办不会消失。判据看**有没有交过**（investigate is not None），
    #   不是看确认过没有：点了没确认的这段也算"已经出手"，窗口该照走。
    #   （只看 seer_confirmed 的话，人一点号码 pending 就整窗停在 human_night，
    #    泵线程会空转到窗口结束 —— 守卫/女巫那两条没有这个洞，口径必须一致。）
    if set(acts) <= {"investigate", "confirm"} and n.get("investigate") is not None:
        return False
    return True


def _advance_shoot(self) -> str:
    """猎人开枪。它会从两处进来：夜里死讯之后（DAWN 之前）、投票结算之后。"""
    g = self.game
    if g.shooter is not None and not g.night.get("ann_hunter_open"):
        g.night["ann_hunter_open"] = True
        # stage 标记：同 _announce_night（P1 夜间单一时间源）
        self.timeline.append({"t": "sys", "s": "猎人请睁眼，请选择你要带走的目标",
                              "voice": JUDGE_VOICE, "delay": NIGHT_GAP,
                              "stage": "hunter"})
    if self._run_one_ai_shoot():
        self._flush_log()
        return "continue"
    return "human_shoot"


def _advance_exile(self) -> str:
    """投票结算后的收尾：被投出的猎人开完枪（shoot_from=EXILE）⇒ 进下一夜。

    🔴 2026-10-05 之前这里不存在：EXILE 是"瞬时态"（resolve_vote 之后
    _advance_vote 直接 next_night）。猎人开枪相位引入后，开完枪回到 EXILE
    必须有人接住 —— 否则 auto_advance 报"没有配剧本的相位"然后 stuck。

    ★ 2026-10-28（用户定）：白天被投出的、以及被这个猎人带走的人都要留遗言 ——
    所以进夜之前先过一遍遗言相位。
    """
    g = self.game
    _lw = g.pending_lastwords()
    if _lw:
        g.open_lastwords(_lw, "night")
        self._flush_log()
        return "continue"
    g.next_night()
    self._flush_log()
    return "continue"


def _advance_dawn(self) -> str:
    g = self.game
    # ★ 遗言（2026-10-28 用户定）：第一夜死的人，死讯播报之后**先留遗言**，
    #   再开始白天的轮流发言。判据全在 `engine.pending_lastwords()` 里
    #   （死因记录上已经按规则算好了"该不该留"）。
    _lw = g.pending_lastwords()
    if _lw:
        g.open_lastwords(_lw, "discuss")
        self._flush_log()
        return "continue"
    g.open_discuss()
    self._flush_log()
    return "continue"


# ================================================================ 发言时限（2026-10-28 用户定）
#
# 用户逐字：「限制每人发言时间最长为120秒，在还剩30秒时法官喊"还剩30秒"，
#   倒计时120秒结束后直接进入下一个人发言。（仍保留可以提前结束发言）」
# 遗言同构（同一上限、同一句提醒）。
#
# 为什么放在驱动器而不是引擎：引擎是规则（谁能说、说完轮到谁），
# 而"说了多久"是时间，不是规则 —— 引擎里没有任何墙钟，也不该有
# （它要能在一秒内把整局重算一遍给回放/评测用）。
#
# 状态放在 Session 上（`_sp_state`），键 = (种类, 轮次, 加时阶段, 座位)：
#   · 同一轮里同一个人可能说两次（平票加时那一轮），所以键里必须有加时阶段；
#   · 换人/换轮 ⇒ 键一变 ⇒ 时钟自动重起，不需要额外的"重置"调用点。

def _sp_key(self, kind: str, seat: int) -> tuple:
    g = self.game
    return (kind, int(g.round), int(getattr(g, "runoff_stage", 0) or 0), int(seat))


def _sp_state(self, kind: str, seat: int) -> Dict:
    """这一位这一次发言的计时状态（换人/换轮自动重起）。

    ★ 2026-10-30（用户实机：「夜间还没结束、其他玩家还没发言，圆盘就显示
      4 号正在发言中，倒计时还在减」）：
      `at` 以前取 `time.monotonic()` —— 也就是**驱动推进到 DISCUSS 的那一拍**。
      可服务端推进**不等播报**：那一拍法官还在念「天亮了」「昨夜 X 出局」
      「4号请发言」，于是 120 秒在一句都还没念的时候就开始扣，
      前端（拿到 `current_speaker` 就画大圆）看起来就是"夜里有人正在发言"。
      现在 `at` = 「N 号请发言」**念完**的时刻（与夜间面板同一个估算器）；
      估算不出来（无播报 / 老数据）才退回"立刻开始" —— 宁可早，不可锁麦。
    """
    key = _sp_key(self, kind, seat)
    st = getattr(self, "_sp_state", None)
    if not st or st.get("key") != key:
        _ready = _speech_ready_mono(self, kind, seat)
        st = {"key": key, "at": _ready if _ready is not None else time.monotonic(),
              "warned": False}
        self._sp_state = st
    return st


def _speech_started(self, kind: str, seat: int) -> bool:
    """这一位的话**现在能不能开口**（=「N 号请发言」那句念完了没有）。

    与 `_speech_left` 一样**只读**：`now_state()` 每拍都调它，
    绝不能顺手把时钟建起来 —— 那正是"起点被轮询拖歪"的老坑。
    """
    st = getattr(self, "_sp_state", None)
    if st and st.get("key") == _sp_key(self, kind, seat):
        return time.monotonic() >= st["at"]
    _ready = _speech_ready_mono(self, kind, seat)
    return True if _ready is None else time.monotonic() >= _ready


def _speech_left(self, kind: str, seat: int) -> float:
    """还剩多少秒（**只读**，不会顺手把时钟建起来）。

    只读很重要：`now_state()` 每拍都会被调用，如果它顺手建时钟，
    "发言人刚开始说话"的时刻就被轮询拖歪了（而且 /public 一密集就永远不到点）。
    """
    st = getattr(self, "_sp_state", None)
    if not st or st.get("key") != _sp_key(self, kind, seat):
        return speech_io.speech_window()
    # 夹住上限：`at` 可能取在**未来**（「N号请发言」还没念完）——
    # 那时算出的是 120+，前端读秒一上来就"超窗"了。念完之前一律显示满窗。
    return min(speech_io.speech_window(),
               speech_io.speech_window() - (time.monotonic() - st["at"]))


def _speech_guard(self, kind: str, seat: int, end_fn) -> str:
    """人话发言的看门狗：到 30 秒提醒一句，到 120 秒**替他结束**。

    返回 "continue" = 这一拍有产出（刚提醒过 / 刚替他结束），调用方回到循环；
    返回 "human_speak" = 继续等他（前端照常显示发言屏，仍可提前结束）。
    """
    st = _sp_state(self, kind, seat)
    left = speech_io.speech_window() - (time.monotonic() - st["at"])
    if left <= 0:
        # 到点：**直接进下一个人**（用户定）。提前结束的通道照旧有效
        # （玩家自己点一下关麦 / 点「过」就走 act 的 speak 分支）。
        end_fn()
        self._sp_state = None
        self._flush_log()
        return "continue"
    if (not st["warned"] and speech_io.speech_has_last_call()
            and left <= speech_io.speech_last_call()):
        st["warned"] = True
        # ⚠️ 与夜间「还剩15秒」同一条纪律：这句**故意不带 node** ——
        #    它只是播报，不是门控；带上 node 会让前端的播报门把它当成本段的
        #    "开眼句"重新计时（夜间那次的教训，见 _night_clock 的注释）。
        self.timeline.append({"t": "sys", "s": speech_io.SPEECH_LAST_CALL_SAY,
                              "voice": JUDGE_VOICE, "delay": 0,
                              "tick": "speech_30", "at": time.time()})
        return "continue"
    return "human_speak"


def _advance_lastword(self) -> str:
    """遗言相位（2026-10-28 用户定）。

    · 第一夜死的人 / 白天被投出的人 / 被同命猎人带走的人 —— 依次留遗言
    · 遗言与发言**同构**：同一套 120 秒上限、同一句「还剩30秒」
    · 说完回 `lastword_back` 说的那个地方（接着白天发言 / 接着进黑夜）
    """
    g = self.game
    spk = g.current_lastword()
    if spk is None:
        back = g.end_lastwords()
        if back == "discuss":
            g.open_discuss()               # 遗言说完 ⇒ 从锚点开始轮流发言
        else:
            g.next_night()                 # 遗言说完 ⇒ 进黑夜
        self._flush_log()
        return "continue"
    if g.player(spk).kind == "ai":
        # ★ AI 的遗言也要有「N号请留遗言」开场：
        #   人类分支靠 `_announce_lastword` 播，AI 分支若漏了，时间线上就会出现
        #   一条"没有开场白"的 say（`speech_io._announce_speaker` 只在 DISCUSS
        #   相位播「请发言」，遗言相位直接 return）。幂等由 `_announced` 保证。
        self._announce_lastword(spk)
        self._ai_speak(spk, lastword=True)
        g.end_lastword()
        return "continue"
    self._announce_lastword(spk)
    return _speech_guard(self, "lastword", spk, g.end_lastword)


def _advance_discuss(self) -> str:
    g = self.game
    spk = g.current_speaker()
    if spk is None:
        g.open_vote()
        self._flush_log()
        return "continue"
    if g.player(spk).kind == "ai":
        self._ai_speak(spk)
        g.end_speech()
        return "continue"
    self._announce_speaker(spk)           # 🔴 轮到人类发言：先报「N号请发言」
    # ★ 2026-10-28（用户定）：每人发言最长 120 秒，剩 30 秒法官提醒，
    #   到点**直接进下一个人**（提前结束照旧 —— 点一下关麦 / 「过」）。
    return _speech_guard(self, "speak", spk, g.end_speech)


def _advance_vote(self) -> str:
    g = self.game
    if self._all_voted():
        g.resolve_vote()
        self._flush_log()
        # 🔴 2026-10-05 实局（死猎人第二晚开枪）：resolve_vote 后可能是 SHOOT
        #    （猎人被投出，等他开枪）。原来只挡 ENDED ⇒ next_night 把枪跳过、
        #    shooter 残留 ⇒ 下一晚结算时死人开枪。现在交回循环：
        #    下一拍 dispatch 到 _advance_shoot，开完枪回 EXILE 由 _advance_exile 进夜。
        if g.phase is Phase.SHOOT:
            return "continue"
        # ★ 平票加时（2026-10-28 用户定）：resolve_vote 在第一轮平票时把相位切回
        #   DISCUSS（= 平票者再发一轮言）。这里必须**放手让循环走发言**，
        #   不能像原来那样一律 next_night —— 那等于把加时整段跳过。
        if g.phase is Phase.DISCUSS:
            return "continue"
        # ★ 遗言（2026-10-28 用户定）：白天被投出的人（以及被这个猎人带走的人）
        #   要先留遗言，再进黑夜。交给 `_advance_lastword` 走完，
        #   它结束时会调 next_night。
        if g.phase is Phase.EXILE:
            _lw = g.pending_lastwords()
            if _lw:
                g.open_lastwords(_lw, "night")
                self._flush_log()
                return "continue"
        if g.phase is not Phase.ENDED:
            g.next_night()
            self._flush_log()
        return "continue"
    _pump_vote_pre(self)
    if _grace_fill_votes(self):
        return "continue"          # 兜底补齐了票 ⇒ 下一拍就归票（同一次推进里）
    r = self._run_one_ai_vote()
    if r is True:
        return "continue"
    if r == "wait":
        # AI 票还在后台跑；只要还有人类要投，就先把手机给人（别显示"AI 正在想"）
        humans_left = any(p.kind == "human" and p.alive and g.can_vote(p.seat)
                          and p.seat not in g.votes for p in g.players)
        return "human_vote" if humans_left else "working"
    return "human_vote"


def _pump_vote_pre(self) -> None:
    """进入一轮投票时，把 AI 的投票决策一次性发到后台（用户定：最后发言结束即触发）。

    换轮（`g.round` 变）就换一个新池并把兜底计时清零。

    ★ 2026-10-28 平票加时：加时投票**不换轮**（同一天），但它是另一次投票 ——
      决策输入完全不同（候选被收窄到平票者、自己可能没有投票权）。
      所以池子的键必须带上 `runoff_stage`，否则会直接收割第一轮的预跑结果：
      表现是"加时投票里 AI 投了一个不在候选里的号"，引擎拒绝、整局卡住。
    """
    g = self.game
    _key = (g.round, int(getattr(g, "runoff_stage", 0) or 0))
    if getattr(self, "_pre_vote_key", None) != _key:
        self._pre_vote_key = _key
        pre = getattr(self, "_pre", None)
        if pre is not None:
            pre.discard_all()
        self._pre = PrePool()
        self._vote_grace_at = None
    order = g.vote_order or [p.seat for p in g.alive]
    for s in order:
        p = g.player(s)
        if p.kind != "ai" or not p.alive or not g.can_vote(s):
            continue
        if s in g.votes:
            continue
        # 预跑上限借狼刀那一档（20s）：投票本身**没有**窗口/闭眼句（定稿只给了
        # 5s 弃票兜底），这个数只用来决定"多少次轮询后放弃这次预跑"。
        _pre_kick(self, s, "vote", night_script.WIN_WOLF)


def _grace_fill_votes(self) -> bool:
    """AI 投票 5 秒弃票兜底（2026-10-07 用户定稿）。返回 True = 这一拍补了票。

    定稿：「最后一个人类投票完成后，检查所有 AI 玩家是否投票，若有没投票的，
    等待 5 秒，若 5 秒后该 AI 玩家还未决策，直接视为弃票」。
    计时从"所有该投的人类都投完"那一刻起算；任何一次还有人类没投就清零。
    """
    g = self.game
    humans_left = any(p.kind == "human" and p.alive and g.can_vote(p.seat)
                      and p.seat not in g.votes for p in g.players)
    if humans_left:
        self._vote_grace_at = None
        return False
    now = time.monotonic()
    # 🔴 2026-10-12（用户实机·重大：唯一人类死后，每轮投票全体 AI 弃票）：
    #    定稿的"5 秒兜底"语义是「**最后一个人类投完**后再等 5 秒」—— 场上没有
    #    人类时这个前置根本不成立，而预跑的 AI 票（真 LLM 每张 3~8 秒）远没到
    #    收割点 ⇒ 5 秒一到全体记弃票（战报里连续三轮全员弃票就是这个）。
    #    修：没有活着且有投票权的人类时，兜底从投票相位开始给足**预跑窗口**
    #    （WIN_WOLF，与 _pump_vote_pre 的预跑上限同源），AI 票来得及落地。
    _wait = night_script.AI_VOTE_GRACE
    if not any(p.kind == "human" and p.alive and g.can_vote(p.seat)
               for p in g.players):
        _wait = max(_wait, night_script.WIN_WOLF)
    if getattr(self, "_vote_grace_at", None) is None:
        self._vote_grace_at = now
        return False
    if now - self._vote_grace_at < _wait:
        return False
    filled = 0
    for p in g.players:
        if (p.kind == "ai" and p.alive and g.can_vote(p.seat)
                and p.seat not in g.votes):
            g.cast_vote(p.seat, -1)                 # 弃票（-1）
            filled += 1
    self._vote_grace_at = None
    if filled:
        print(f"[vote] 5s 兜底：{filled} 个 AI 未决策 ⇒ 记弃票", flush=True)
    return bool(filled)


# ---- 夜晚（**剧本驱动**：顺序与完成条件都在 steps.py 那张表里）----

def _night_done(self) -> bool:
    """
    夜晚该做的都做了没有（H1）。

    ⚠️ 判据**完全来自** `steps.NIGHT_STEPS`：以前这里是一串手写 if
    （狼 / 预言家 / 女巫 / 守卫各一条），**加一个角色就要再加一条** ——
    三神当年就是这么加进来的。现在加步骤 = 加一行数据，
    而且"场上没狼时这一步天然算做完"这类兜底也写在数据里
    （那条兜底治的是一个真卡死：最后一狼被毒死后，夜晚再也结束不了）。
    """
    return steps.night_done(self.game)


def _run_one_ai_night(self):
    """
    替 AI 做掉**第一个还没做完**的夜间步骤；

    返回值（2026-10-07 第二期扩成三态）：
      · True   = 推进了一步（驱动器 continue）
      · "wait" = 有 AI 决策还在**后台预跑**里跑（驱动器报 working，继续轮询）
      · False  = 该等人了（驱动器报 human_night）

    ⚠️ 有个**容易写错**的地方：某一步只能等人类时，必须**继续看后面的步骤** ——
    预言家验人和狼刀互不依赖，不该因为"人类狼还没点"就跟着一起等
    （老代码靠分支 fall-through 正好是这个语义，换成表之后必须显式保住）。
    女巫那一步不一样：它的 `run` 自带"刀口已定"的前置门，靠表里的**顺序**保证。

    ⚠️ 人类优先：**只要还有人类待办，就一律报 human_night**，
    绝不报 "wait" —— 否则前端会显示"AI 正在想"，而实际上该递手机了。
    """
    g = self.game
    human_pending = any(p.kind == "human" and g.actions_for(p.seat)
                        for p in g.players)
    waited = False
    for step in steps.NIGHT_STEPS:
        if step.done(g):
            continue
        r = getattr(self, step.run)()
        if r is True:
            return True
        if r == "wait":
            waited = True
    if human_pending:
        return False
    return "wait" if waited else False


def _all_voted(self) -> bool:
    """
    归票判据：**所有"有投票权"的活人**都投过了。

    ⚠️ 不能用 alive 代替 can_vote：翻牌的白痴活着但永远投不了，
    照旧写法（活人都在 votes 里）会**永远等不到他** ⇒ 归票死锁（D5）。
    """
    g = self.game
    return all(p.seat in g.votes for p in g.alive if g.can_vote(p.seat))


def ensure_night_summaries(self) -> None:
    """天黑投票后，把「今晚之前」的每一天压成逐日摘要（2026-10-02 用户定）。

    为什么在夜里做：压缩是每段一次模型调用，白天做会让发言干等；
    夜里大家本来就在等狼/神行动，过渡期最不心疼。摘要按**天**缓存，
    只压一次；注入时带「第 N 天」标注。用主 LLM（flash 档）压缩，
    不要求配 SUMMARY_MODEL。失败跳过（下一天晚上还会补试）。
    """
    g = self.game
    if g.phase.value != "night":
        return
    chat = None
    for ag in self.agents.values():
        cf = getattr(ag, "chat_fn", None)
        if cf is not None:
            chat = cf
            break
    if chat is None:                       # 全 RuleAgent（mock/评测）：没有真模型
        return
    for d in range(1, g.round):            # 本轮之外的天都算「已结束」
        if d in self.day_summaries:
            continue
        lines = [f"{int(rec['seat']) + 1}号：{rec['text']}"
                 for rec in g.speeches if int(rec.get("round", 0)) == d]
        if not lines:
            continue
        try:
            prompt = ("你是狼人杀对局的记录员。把下面第 " + str(d) + " 天的公开发言"
                      "压成一段不超过 120 字的摘要，必须保留：谁跳了什么身份、"
                      "谁点了谁/保了谁、票投给了谁、关键矛盾。只回摘要正文。\n\n"
                      + "\n".join(lines))
            self.day_summaries[d] = str(chat(
                [{"role": "user", "content": prompt}])).strip()[:400]
        except Exception as e:                     # noqa: BLE001
            print(f"[summary] 第{d}天摘要失败（下晚再补）：{e}", flush=True)


def ensure_day_plans(self) -> None:
    """天亮那一刻（discuss 刚开始、还没轮到任何人发言），给每个 AI 预盘今天的战术。

    用户 2026-10-02 定：发言前的内心活动会造成发言延迟 ⇒ 挪到「夜晚所有
    行动结束的那一刻」集中做。预案是**私有**的，发言/投票时注入；
    局势随时会变 ⇒ 它只是参考，照不照打由模型自己判断。
    失败的座位预案留空 —— 发言链路自己会现想（老路径），一局不能卡。
    """
    g = self.game
    if g.phase.value != "discuss" or self._planned_round == g.round:
        return
    self._planned_round = g.round
    for p in g.players:
        if p.kind != "ai" or not p.alive:
            continue
        ag = self.agents.get(p.seat)
        if ag is None or getattr(ag, "chat_fn", None) is None:
            continue          # RuleAgent / 评测桩：没有真模型，预案无意义
        try:
            v = g.get_view_for(p.seat)
            v["my_tactics"] = self.tactics.get(p.seat) or []
            v["day_summaries"] = dict(self.day_summaries)
            raw = ag._ask(ag._build(v, self._round_speech(g), "plan"))
            _t = extract_control(raw or "").get("thought") or ""
            self.day_plans[p.seat] = str(_t).strip()[:400]
        except Exception as e:                         # noqa: BLE001
            print(f"[plan] {p.display} 预盘失败（发言时现想）：{e}", flush=True)
            self.day_plans[p.seat] = ""

