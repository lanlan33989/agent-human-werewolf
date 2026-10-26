# -*- coding: utf-8 -*-
"""播报写入：法官「N号请发言」、时间线/日志写入、本轮发言摘取。

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


# ================================================================ 夜间法官播报（2026-10-05 用户定）
# 面杀仪式：每夜法官按「守卫 → 狼人 → 女巫 → 预言家」的主流睁眼顺序喊流程
#（3DM 2019 / 公众号 2017 / 豆丁 2024 多源一致）。播报只是仪式、与逻辑执行顺序
#（steps 表的 wolf→seer→witch→guard，女巫要看刀口）**解耦**。
# 规则（用户定）：角色**在板子上**（无论死活）就终身播报 —— 死了也照喊，
# 否则"今晚没喊守卫"本身就是死讯泄漏；只有**开局就没有**的板子才不播。
# 播报里**绝不带任何私有信息**（刀口/验人结果/药）—— 公开喊的只有流程。
#
# 🔴 2026-10-07 第二期：整夜播报的唯一真相搬到 **night_script.NIGHT_NODES**
#    （逐节点 3 秒间隔 / 面板 / 收割点 / 预言家"他是这个"节点都在那张表里）。
#    这里原先的 NIGHT_ANNOUNCES / _NIGHT_ROLE 两张表已删除 —— 留两份表
#    必然漂移（改一处忘另一处，正是本项目栽过多次的坑）。
#
# 每步"闭眼"之后的留白秒数（用户定：逐节点 3 秒）。.env 里 NIGHT_GAP=数字 可调，
# 只影响猎人开枪那两句（`_flush_log` 里的补喊）；夜流程走 night_script.GAP。
NIGHT_GAP = float(os.environ.get("NIGHT_GAP", "3"))  # 2026-10-07 用户定：3 秒（原 5）

import time
from agents import JUDGE_VOICE, voice_for_personality
from engine import Phase
from typing import Any, Dict, List, Optional


def _announce_speaker(self, seat: int) -> None:
    """发言回合开始时，法官先播一句「N号请发言」（2026-10-02 用户定）。

    · 用户原话：「x号（该玩家自己说）」改为「x号请发言（法官说）」——
      旧行为是发言文本自带「N号：」前缀、由 AI 音色念出来，听感就是
      "玩家自己报名字"；现在报名字这件事归法官。
    · ⚠️ 只进 timeline（播报/字幕用），**不进 public_log** —— public_log
      会喂给 AI（history_text 从它来），塞满「N号请发言」是纯噪音。
    · 两个触发点（2026-10-02 教训：挂在 drive() 开头**不可靠** —— 快代理
      一局能在一次 drive 里跑完，而开局时 phase 还是夜晚 ⇒ 永远不触发；
      实测就是这样）：① `_say`（AI 发言的必经点，公告必然排在发言前）
      ② `_advance_discuss` 轮到人类时（轮次开始就报，当开场提示）。
      同一个（轮次，座位）只报一次。
    """
    g = self.game
    if g.phase is not Phase.DISCUSS:
        return
    key = (g.round, seat)
    if key in self._announced:
        return
    self._announced.add(key)
    self.timeline.append({"t": "sys", "s": f"{seat + 1}号请发言",
                          "voice": JUDGE_VOICE, "ts": time.time()})


# ---------------------------------------------------------- 时间线

def _flush_log(self) -> None:
    g = self.game
    # 猎人开枪的"请闭眼"收尾：幂等（开过枪/放弃后 shooter 清空，此处补喊）。
    # 放在 _flush_log 是因为人类开枪走 act()，不经过 _advance_shoot 的 AI 分支。
    if (g.night.get("ann_hunter_open") and g.shooter is None
            and not g.night.get("ann_hunter_close")):
        g.night["ann_hunter_close"] = True
        self.timeline.append({"t": "sys", "s": "猎人请闭眼", "voice": JUDGE_VOICE,
                              "delay": NIGHT_GAP})
    while self._log_ptr < len(g.public_log):
        # 法官（主持人）的播报用**法官音色**：字正腔圆、权威感最强。
        # 之前只能靠语速区分"法官 / 玩家"，现在就听得出是哪一类了。
        self.timeline.append({"t": "sys", "s": g.public_log[self._log_ptr],
                              "voice": JUDGE_VOICE, "ts": time.time()})
        self._log_ptr += 1


def _round_speech(self, g) -> List[str]:
    """只取**本轮**的公开发言（2026-10-02 用户定：本轮原文全量，
    前几轮走逐日摘要 —— 见 ensure_night_summaries）。"""
    return [f"{int(rec['seat']) + 1}号：{rec['text']}"
            for rec in g.speeches if int(rec.get("round", 0)) == g.round]


def _say(self, text: str, seat: Optional[int] = None,
         spoken: Optional[str] = None,
         card: Optional[Dict[str, Any]] = None) -> None:
    if seat is not None:
        # 🔴 法官先报「N号请发言」（必在发言前 —— 公告逻辑见该方法 docstring）
        self._announce_speaker(seat)
    self.speech.append(text)
    # 音色（人格的听觉那一半）：**由服务端决定** —— 只有这里知道这个座位的人格。
    # 前端只把 voice 原样转给 /api/tts，别在两边各维护一份映射（必然漂）。
    # 人类座位没有人格 ⇒ voice 给空，前端用默认音色。
    _ag = self.agents.get(seat) if seat is not None else None
    self.timeline.append({"t": "say", "s": text, "seat": seat,
                          "voice": voice_for_personality(
                              getattr(_ag, "personality", None)) if _ag else "",
                          "ts": time.time()})
    if seat is not None:
        # 引擎也留一份（按座位存）。这样 AI 能从自己的视图里看到
        # 「我自己说过什么」—— 这就是「显式记忆」的来源（见 agents.private_block）。
        # `card` = 局势卡（G4）：引擎会把它当公开信息，经 view["stances"] 给全桌。
        self.game.log_speech(seat, spoken if spoken is not None else text, card=card)

