# -*- coding: utf-8 -*-
"""
对局 API（D4）

把 engine（规则）与 agents（AI 玩家）接到 HTTP 上，让浏览器能跑完一局。

三条设计约束：

1. 单设备形态
   一部手机 = 法官 + N 个 AI + 唯一麦克风。保密靠「交接」：
   夜间逐个把手机交到需要行动的人手里，操作完再交回中央。

2. 每个座位只能拿到 get_view_for(seat) 的结果
   服务端不做任何额外裁剪，也不提供"上帝视角"接口 ——
   否则界面层迟早会不小心把私有信息画出来。

3. 发言与状态播报分开放
   engine 的 public_log 只放引擎的状态播报（天亮、出局、投票结果）；
   玩家发言单独放在 speech 里。这样 public_log 那条「不含身份词」的
   保证不会被 AI 的发言破坏（预言家报身份是正常玩法）。
"""
from __future__ import annotations

import os
import random
import re
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import experience
import metrics
import report as report_mod
import steps
from agents import (DEFAULT_LEVEL, DEFAULT_VOICE, JUDGE_VOICE, LEVELS, PERSONALITIES,
                    Decision, RuleAgent, assign_personalities, extract_control, level_spec,
                    voice_for_personality)

# 规则开关的全局默认（G9）：**只影响新开的局**（已在打的那局规则不能变 ——
# 中途改规则会让桌上的人按旧规则做的判断全部作废）。
# 单一真相表在 engine.RULE_SWITCHES（前端按它渲染、这里按它校验）。
SETTINGS: Dict[str, Any] = {}


def _valid_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    校验规则开关。**不认识的键直接报错**，不静默忽略 ——
    静默忽略的表现是"前端传了个拼错的键、看起来设置成功了、实际没生效"。
    """
    out: Dict[str, Any] = {}
    spec = {s["key"]: s for s in RULE_SWITCHES}
    for k, v in (raw or {}).items():
        if k not in spec:
            raise ValueError(f"未知规则开关：{k}（可选：{'、'.join(spec)}）")
        item = spec[k]
        if item["type"] == "bool":
            if not isinstance(v, bool):
                raise ValueError(f"{k} 必须是布尔值")
            out[k] = v
        elif item["type"] == "choice":
            allowed = [o["value"] for o in item["options"]]
            if v not in allowed:
                raise ValueError(f"{k} 只能是 {'/'.join(allowed)}")
            out[k] = v
    return out

# G2 摘要器：**由装配层注入**（server.py 接便宜模型时把它设成真函数）。
# 默认 None ⇒ build_messages 退回硬截断（= 旧行为）。
# 为什么放在这里而不是 hardcode 在 agents 里：agents 不 import 任何 SDK，
# 模型通道属于装配层的事（见 agents 文件头的第二条原则）。
SUMMARIZER = None
from engine import (GOD_ORDER, RULE_SWITCHES, WOLF_RATIO, Board, Game, Phase, Role,
                    make_board, wolf_count)
# 留白挂在「睁眼」条目上（2026-10-05 实局修正：那是给玩家操作/思考的时间；
# 闭眼紧跟着念 —— 第一版把留白挂在闭眼后，人类狼的整段操作时间被仪式播报吃掉）


router = APIRouter(prefix="/api/game", tags=["game"])

SESSIONS: Dict[str, "Session"] = {}
MAX_SESSIONS = 20

# 单个请求里替 AI 干活的时间预算（秒）。
# 调大 ⇒ 一次请求干完更多、但页面等更久；调小 ⇒ 开局快、但要多轮询几次。
INIT_BUDGET = 1.5      # 构造（POST /new）时
POLL_BUDGET = 1.2      # 轮询（GET /public）时
# 人类动作（POST /act）之后的推进预算。
# ⚠️ 这里以前是**没有预算**的 `auto_advance()`：意味着「真人说完一句话，这一次请求
#    要把后面所有 AI 的发言全部跑完才返回」。6 人局 4 个 AI × 每次 2.3 秒 ≈ 9 秒，
#    而前端是**拿到响应才开始播报** ⇒ 玩家感受就是「我说完，等好久 AI 才开口」。
#    现在和轮询用同一档预算：这一请求只把**第一句**推出来（deadline 在循环顶部检查，
#    所以至少会完整跑完当前这一个动作），剩下的交给 /public 继续推。
ACT_BUDGET = 1.2

# 天亮预盘（2026-10-02 加）：天亮那一刻给每个 AI 现算一份战术预案，存进 view["my_plan"]。
# 🔴 2026-10-04 用户定：**默认停用**。理由：speak 前的 thought 已经承担"梳理战术"
#    这个角色（thought 范式化后更是如此），而预盘要在天亮那一刻**串行**跑 N 个
#    LLM 调用 —— 实测每个 5~15s，N 个人就是半分钟到一分钟的纯等待。
#    ⚠️ 接口全部保留（ensure_day_plans / view["my_plan"] / TASKS["plan"]），
#    把这里改成 True（或环境变量 DAY_PLANS=1）就能原样做回来。
ENABLE_DAY_PLANS = os.environ.get("DAY_PLANS", "0") == "1"

# 谁给 AI 座位造 Agent。默认 None = 规则发言器，也就是「没接模型」时的行为。
# `server.py` 在装配时把它换成真 LLM 版本。
# 为什么要用这种注入而不是直接 `from server import LLM`：
# server 已经 import 了 game_api，反向 import 会成环。
AI_AGENT_FACTORY = None


def _new_agent(seat: int, rng, level: str = DEFAULT_LEVEL):
    """
    造一个 AI 座位。

    ⚠️ 档位是**构造完打在实例上**的，不是构造参数：工厂签名 `(seat, rng)` 被
    server.py 的 lambda 和一批测试替身用着，改签名会把它们全弄坏。
    Agent 基类里 level 有默认值，所以这一步只是"覆盖"，不会改变谁的存在性。
    """
    ag = RuleAgent(seat, rng) if AI_AGENT_FACTORY is None else AI_AGENT_FACTORY(seat, rng)
    ag.level = level
    return ag


# ================================================================ 请求体

class NewGameReq(BaseModel):
    # ⚠️ 没有 `board` 字段了（旧预设迁移）：板子一律**按人数现算**。
    #    留着一个"换副板子"的入口，等于留了一条绕过 34%/神=狼 规则的旁路。
    humans: Optional[int] = None
    ais: Optional[int] = None
    seed: Optional[int] = None
    # 四档氛围：娱乐 / 认真 / 硬核 / 地狱（缺省 = agents.DEFAULT_LEVEL）
    level: Optional[str] = None
    # 人格（H2）：""/"off" = 不配（缺省）；"random" = 每个 AI 座位随机一种；
    # 也可以是某个具体人格的 key（整桌 AI 同一种）。
    personality: Optional[str] = None
    # 座次编排（2026-10-01）：前端拖拽出来的 human/ai 数组（按座位 1..n 顺序）。
    # 不给 → 后端用 default_layout（人类首位 + 均匀隔 AI）。
    layout: Optional[List[str]] = None


class ActionReq(BaseModel):
    seat: int
    action: str = "pass"
    target: Optional[int] = None
    text: Optional[str] = None


class ReviewReq(BaseModel):
    """复核一条经验候选（G3 离线评审入库）。"""
    id: int
    approve: bool = True


class SettingsReq(BaseModel):
    """规则开关（G9 可配那一半）。**只影响新开的局**，不动已在打的对局。"""
    settings: Dict[str, Any] = {}


# ================================================================ 一局

def default_layout(humans: int, ais: int) -> List[str]:
    """默认座次（2026-10-01 用户定）：**人类坐 1 号位先发言**，
    其余人类均匀隔开 —— 把 ais 个 AI 分进 H 个「人类之后的间隔」（含末位人类
    绕回 1 号的间隔），先每人 base = ais // H 个，余数从左到右一轮各补一个，
    保证人类之间的 AI 数量**尽量一致**。
    例：3 人 6 AI → 人AA 人AA 人AA（人类在 1/4/7 号，间隔全是 2）；
        2 人 2 AI → 人A 人A（1/3 号是人）。
    AI 数不够隔（ais < H）时退回"人类全排前位"——这是物理上唯一可行的排法。"""
    if humans <= 0:
        return ["ai"] * ais
    if ais < humans:                       # 隔不开：退回旧行为（人类全排前位）
        return ["human"] * humans + ["ai"] * ais
    base, extra = divmod(ais, humans)
    out: List[str] = []
    for g in range(humans):
        out.append("human")
        out.extend(["ai"] * (base + (1 if g < extra else 0)))
    return out


# 2026-10-06 P2：驱动器/AI 行为/播报写入拆到三个模块（Session 保留薄委托）
import speech_io
import ai_behaviors
import night_driver
import night_script          # 2026-10-07 第三期：窗口/节点都在那张表里

class Session:
    def __init__(self, board: Optional[Board] = None, humans: Optional[int] = None,
                 seed: Optional[int] = None, ais: Optional[int] = None,
                 level: Optional[str] = None,
                 personality: Optional[str] = None,
                 layout: Optional[List[str]] = None) -> None:
        """
        两种开局方式：
          · 给了 `board`（**Board 对象**）→ 用这副板子。**只给测试用** ——
            因为有些组合新规则现算不出来：比如"4 人局带预言家"
            （按 34%/神=狼 现算，4 人局的神职是**女巫**）。
          · 不给 → **按人数现算**（humans + ais）。**这是生产路径**，界面走这条。
            旧那套"预设名"（mini4/bar4/classic6）已经删掉了：它们只是历史包袱，
            留着就等于有第二条不经过现算规则的路。

        `level` = 四档氛围（娱乐/认真/硬核/地狱，见 agents.LEVELS）。
        ⚠️ 认不出就**抛**（/new 转 400），不静默退回默认档 —— 静默的话，
        "前端传错一个字符串"会表现成"玩家选了地狱、拿到的是娱乐局"，桌上没人看得出来。
        """
        level_spec(level)                 # 校验；不合法直接抛
        self.level = level or DEFAULT_LEVEL
        if board is not None:
            n = len(board.roles)
            if humans is None:
                humans = (n + 1) // 2
        else:
            if humans is None and ais is None:
                humans, ais = 2, 2           # 什么都没给：按 4 人局（2 人 + 2 AI）
            humans = int(humans or 0)
            ais = int(ais or 0)
            board = make_board(humans, ais)  # 全部校验都在里面，界面不重复实现一遍
            n = len(board.roles)

        # ⚠️ 允许 humans=0：**全 AI 局**是离线评测要用的形态（批七的批量跑胜率），
        # 里面根本没有"人轮"，驱动照常推进。
        # 界面不会出现这种局 —— 界面自己有"至少 1 个真人"的规则（play.html），
        # 而这条后端规则只管"人数别越界"。
        if not 0 <= humans <= n:
            raise ValueError(f"真人数必须在 0–{n} 之间（0 = 全 AI 局，评测用）")

        self.id = uuid.uuid4().hex[:8]
        self.board_key = board.key
        # 最近一次被访问的时间。淘汰顺序按它算，不按创建顺序（见 new_game 里那段）。
        self.last_used = time.monotonic()
        # 🔴 2026-10-01：座次（谁坐哪个位置）。
        #   · layout 给了 → 照单全收（前端拖拽编排的落点）。只校验长度与数量，
        #     **不强制**"人类之间必须隔 AI"——拖出来的排法是用户自己选的。
        #   · 没给 → default_layout：人类首位 + 人类之间均匀隔 AI。
        #     旧的 ["human"]*h + ["ai"]*… 把真人全排前几位，2 个真人坐 1、2 号
        #     直接挨着 —— 用户点名要改（人类与人类之间必有至少 1 个 AI）。
        if layout is not None:
            kinds = list(layout)
            # 2026-10-26 宽容化：长度/取值仍强校验（坏数据没救），但「人类数对不上」
            # 不再 400 —— 那是前端残留旧座次（比如上局 2 真人 2 AI、这局 0 真人 4 AI）
            # 的典型症状，直接忽略编排退回默认座次更符合直觉。
            if len(kinds) != n or any(k not in ("human", "ai") for k in kinds):
                raise ValueError("layout 必须是长度为 " + str(n)
                                 + " 的 human/ai 数组")
            if kinds.count("human") != humans:
                print("[new] layout 人类数(" + str(kinds.count("human"))
                      + ")与 humans(" + str(humans) + ")不一致 —— 忽略编排，用默认座次",
                      flush=True)
                kinds = default_layout(humans, n - humans)
        else:
            kinds = default_layout(humans, n - humans)
        self.game = Game(board, kinds, seed=seed)
        # 规则开关（G9）：**在开局这一刻**把全局设置打进这一局的规则里。
        # 用 replace 生成新对象、而不是原地改 `game.rules` 的字段：
        # Rules 目前是每局一个（Board 上没有 rules），但"原地改"这种写法一旦
        # 将来有人把它提成共享对象，就会变成"一次设置串味到所有局（含别人的对局）"，
        # 那是最难查的一类 bug —— 所以在写法上就把它堵死。
        if SETTINGS:
            self.game.rules = replace(self.game.rules, **_valid_settings(SETTINGS))
        self.rng = random.Random(seed if seed is not None else random.randrange(10 ** 6))
        # 每个 AI 座位：造 Agent + 打档位 + 注入 G2 摘要器 + 注入 G3 跨局经验。
        # ⚠️ 经验是**按角色**给的：经验里写的是"我当狼时…"，给好人座位是噪音。
        #   for_prompt 里还会再查一次"这一档开不开经验"（硬核/地狱才开）——
        #   单一真相在那一处，这里不重复判断。
        # H2 人格：分配**只看座位，不看角色**（见 agents.assign_personalities 的入参）——
        # 只要分配与角色无关，人格就不构成身份信号。
        #   "" / "off"    ⇒ 不配（缺省，旧行为不变）
        #   "random"      ⇒ 按种子随机给每个 AI 座位一种
        #   具体人格 key   ⇒ 整桌 AI 同一种（"全部激进"这种玩法）
        self.personality_mode = (personality or "").strip() or "off"
        _ai_seats = [p.seat for p in self.game.players if p.kind == "ai"]
        if self.personality_mode == "off":
            _persons = {s: None for s in _ai_seats}
        elif self.personality_mode == "random":
            _persons = assign_personalities(
                seed if seed is not None else self.rng.randrange(10 ** 6), _ai_seats)
        else:
            _persons = {s: self.personality_mode for s in _ai_seats}

        self.agents = {}
        # 私有战术存档（2026-10-02）：seat → 最近 3 条「发言/投票的 thought」。
        # 用户要求：心理活动要存下来，AI 下一轮自己能拿到。
        self.tactics: Dict[int, List[str]] = {}
        # 夜末预案：seat → 天亮那一刻写下的今日战术（私有）。
        self.day_plans: Dict[int, str] = {}
        # 逐日摘要（2026-10-02）：day → 摘要。天黑投票后生成（防白天延迟），
        # 注入时带天标注。用户定：本轮原文全量，前几轮只给摘要。
        self.day_summaries: Dict[int, str] = {}
        self._planned_round: Optional[int] = None
        for _p in self.game.players:
            if _p.kind != "ai":
                continue
            _ag = _new_agent(_p.seat, self.rng, self.level)
            _ag.experiences = experience.BANK.for_prompt(self.level, _p.role.value)
            _ag.summarize = SUMMARIZER
            _ag.personality = _persons.get(_p.seat)
            # H5：只有"高推理档"（硬核/地狱）开 best-of-N，而且只在投票这一处
            # （best_of_kinds 在 agents 里写死 = {"vote"}）。
            # 成本 ×2 只落在投票上；发言每次都调用，那里选优是另一个量级的钱。
            if LEVELS.get(self.level, {}).get("reasoning") == "high":
                _ag.best_of = 2
            self.agents[_p.seat] = _ag
        # H3 多厂商混编：哪个座位由哪个模型服务（由装配层打在每个 Agent 上）。
        # 记下来是为了让榜单能回答"哪个模型当狼最难被看穿"——
        # 不记的话，混编跑出来的数据就没法归因。
        self.models = {s: getattr(a, "model", None) for s, a in self.agents.items()}
        # 局末收割经验候选：只做一次（见 _harvest）
        self._harvested = False
        self.speech: List[str] = []
        # timeline = 引擎播报与玩家发言**按真实先后**合并成一条流。
        # 之前前端把 public_log 全排在前面、speech 全排在后面，于是
        # 「发言结束，开始投票」会显示在某人的发言上方，读起来像 AI 没说话。
        self.timeline: List[Dict[str, str]] = []
        self._log_ptr = 0
        # 「N号请发言」播报去重：drive() 会被轮询反复调，同一个（轮次，座位）只报一次
        self._announced: set = set()
        self.steps = 0
        # 并发轮询会同时推同一个局，跑出重复回合 —— 推的时候上锁
        self.lock = threading.Lock()
        self.game.start()
        self._flush_log()
        # ⚠️ 构造时**只推一小段**（deadline 见下）。
        # 理由：AI 的决策现在要调模型（一句 ~2.3s）。13 人局光"第一轮 AI 发言"
        # 就有十几个调用，全塞进这个 POST 会让它跑几十秒 —— 页面看起来就是
        # 「连开都开不了」（实测：{真人 1, AI 12} 的 POST 15 秒都不返回）。
        # 剩下的交给 /public 轮询推进（drive()），桌面自己长出来。
        self.pending = self.auto_advance(deadline=time.monotonic() + INIT_BUDGET)
        # 人类夜间待办的起算时刻（倒计时，2026-10-06）—— 任何成功提交清零
        self.human_wait_since = None
        # pending 首现时刻（前端失联 30s 未 arm 时的强制起算兜底）
        self.human_wait_pending_since = None
        # AI 决策后台预跑池（2026-10-07 第二期，见 night_pre.PrePool）：
        #   _pre            当前相位的池（每夜 / 每轮投票各换一个新池）
        #   _pre_round      夜池属于哪一夜（换夜即换池）
        #   _pre_vote_round 投票池属于哪一轮
        #   _vote_grace_at  「所有该投的人类都投完了」的时刻（5s 弃票兜底计时）
        self._pre = None
        self._pre_round = None
        self._pre_vote_round = None
        self._vote_grace_at = None

    def _harvest(self):        return night_driver._harvest(self)
    def drive(self, budget=1.5):        return night_driver.drive(self, budget)
    def _announce_speaker(self, seat):        return speech_io._announce_speaker(self, seat)
    def _flush_log(self):        return speech_io._flush_log(self)
    def _round_speech(self, g):        return speech_io._round_speech(self, g)
    def _say(self, text, seat=None, spoken=None, card=None):        return speech_io._say(self, text, seat, spoken, card)

    # ---------------------------------------------------------- 视图

    def now_state(self) -> Dict[str, Any]:
        """「现在该谁做什么」——**服务端唯一权威**（2026-10-07 第三期，用户定）。

        为什么要有它（四天四起真机事故的结账）：
        「面板要不要弹 / 要不要读秒 / 能不能开麦」这三件事原来全在前端**猜**
        （猜节点 id、猜音频回放游标 SPOKEN、猜 arm 有没有到）。每一起事故都是
        "两个猜法的结论不一致"，而每修一次就再加一个条件 ⇒ 下一轮换个组合再炸。
        现在服务端每拍直接给**一句话**，前端只渲染它 —— 前端不再存在"要不要显示"。

        kind:
          wheel  夜里轮盘（need = 要渲染的那条待办，含 seconds_left）
          shoot  猎人开枪（同 wheel 形状）
          speak  轮到你发言
          vote   轮到你投票
          none   等（AI 在算 / 窗口在走但这节没人类要点 / 收尾播报）
          over   这局结束
        `until` = **窗口结束的墙上时刻**（epoch 秒）：倒计时前端只做减法，
        不再各自算一遍（消除漂移）。
        """
        g = self.game
        if g.winner:
            return {"kind": "over", "seat": None, "node": None, "until": None,
                    "need": None, "why": "本局结束"}
        seats = _pending_seats_of(self).get("seats") or []

        if g.phase is Phase.SHOOT:
            pick = next((x for x in seats if "shoot" in (x.get("actions") or [])), None)
            if pick is None:
                return {"kind": "none", "seat": None, "node": None, "until": None,
                        "need": None, "why": "等猎人开枪"}
            return {"kind": "shoot", "seat": pick["seat"], "node": None,
                    "until": None, "need": pick, "why": ""}

        if g.phase is Phase.NIGHT:
            cur = night_driver._nn_cur(self)
            if not cur:
                return {"kind": "none", "seat": None, "node": None, "until": None,
                        "need": None, "why": "夜间收尾（天亮播报中）"}
            # 🔴 判据是 `_nn_humans_of(cur)`（= 本节点的动作落在谁头上），**不是**
            #    `item.node == cur`：女巫两问是她一个人的两个节点，她的待办 `node`
            #    一直是 witch_heal，用后者会在毒药窗口把她的面板撤掉。
            humans = night_driver._nn_humans_of(self, cur)
            pick = next((x for x in seats if x["seat"] in humans), None)
            if pick is None:
                # 也报 `started`（2026-10-07）：人类**一操作完**这个节点就没待办了
                # （`kind` 变 none），但窗口还在走 —— 不报的话，"窗口到底起没起"
                # 在外部（harness / 排查）就观察不到。见 `_smoke-night-live.py` 的 I9。
                return {"kind": "none", "seat": None, "node": cur, "until": None,
                        "need": None, "why": "这一节没有人类要点",
                        "started": getattr(self, "human_wait_since", None) is not None}
            # 🔴 2026-10-07 第四起（用户实机："倒计时节点开始过早，没按我一开始约定的来"）：
            #    **定稿口径 = 倒计时从"法官念完该角色那句 + 2 秒"起算**
            #    （`/arm` 的说明写的就是这个，2026-10-06 用户定）。
            #    上一轮我为了修"倒计时恒为 None"，把 `until` 改成一进节点就下发 ⇒
            #    法官还在念「狼人请睁眼…」，秒数就已经在跑了 ⇒ 正是用户说的"过早"。
            #    现在：**没 arm（= 那句还没念完）就不下发 `until`** —— 前端因此不显示秒数。
            #    ⚠️ 这一条**不会**再把窗口憋死：面板已经不依赖音频了（上一轮修的），
            #    所以前端一定会把 arm 送来（语音关掉 / 队列卡住时它也会兜底 arm）。
            armed = getattr(self, "human_wait_since", None) is not None
            left = max(0.0, night_driver._nn_deadline(self, cur) - time.monotonic())
            # 🔴 2026-10-07 第十期（兑现"四时钟"账的音频半边）：
            #    `panel_line` = **时间线里本段那句的下标**（服务端算好下发）——
            #    前端"面板在该句念完之后开"就不再需要 nightNodeIdx/nightStageIdx
            #    这类索引猜测（那正是面板时机反复出问题的源头）。
            #    `lines_due` = 本段的播报行 + 各自 `not_before`（= at + delay，
            #    即"这句最早什么时候该开口"）—— 音频按它播，落后太多的直接跳过。
            tl = self.timeline or []
            panel_line = next((i for i, x in enumerate(tl)
                               if x.get("node") == cur), -1)
            lines_due = [{"i": i, "s": (x.get("s") or "")[:60],
                          "not_before": round((x.get("at") or 0)
                                              + (x.get("delay") or 0), 2)}
                         for i, x in enumerate(tl) if x.get("node") == cur]
            return {"kind": "wheel", "seat": pick["seat"], "node": cur,
                    "until": (time.time() + left) if armed else None,
                    "started": armed, "need": pick, "why": "",
                    "panel_line": panel_line, "lines_due": lines_due}

        if g.phase is Phase.DISCUSS:
            sp = g.current_speaker()
            pick = next((x for x in seats if x["seat"] == sp
                         and "speak" in (x.get("actions") or [])), None)
            if pick is None:
                return {"kind": "none", "seat": None, "node": None, "until": None,
                        "need": None, "why": "AI 发言中 / 等切换"}
            return {"kind": "speak", "seat": sp, "node": None, "until": None,
                    "need": pick, "why": ""}

        if g.phase is Phase.VOTE:
            pick = next((x for x in seats if "vote" in (x.get("actions") or [])), None)
            if pick is None:
                return {"kind": "none", "seat": None, "node": None, "until": None,
                        "need": None, "why": "等 AI 投票"}
            return {"kind": "vote", "seat": pick["seat"], "node": None, "until": None,
                    "need": pick, "why": ""}

        return {"kind": "none", "seat": None, "node": None, "until": None,
                "need": None, "why": f"phase={g.phase.value}"}

    def _night_plan(self) -> List[Dict[str, Any]]:
        """②整张夜表下发（2026-10-08 用户定「alarm 架构」）：**客户端按表渲染**。

        只含**公有信息**：节点顺序 / 播报文案 / 窗口秒数 / 留白 —— 全桌一致，
        板子过滤本来就近公开（见 night_script.build），不泄露任何角色私密。
        同一夜缓存一份（表在夜里不会变）。前端用途：夜间进度（第几段/共几段）、
        节点顺序的权威来源 —— 客户端从此不需要自己维护节点清单。
        """
        g = self.game
        if g.phase is not Phase.NIGHT:
            return []
        roles = {p.role.value for p in g.players}
        key = (g.round, tuple(sorted(roles)))
        if getattr(self, "_plan_key", None) != key:
            self._plan_key = key
            self._plan_cache = [
                {"id": nd["id"], "say": nd["say"], "win": nd.get("win") or 0,
                 "gap": nd.get("gap") or 0, "close": nd.get("close"),
                 "close_gap": nd.get("close_gap") or 0}
                for nd in night_script.build(roles, g.round)]
        return self._plan_cache

    def arm_window(self, node: str = "") -> Dict[str, Any]:
        """前端：**法官念完该角色「请睁眼」那句 + 2 秒后**调用 —— **倒计时此刻才开始**
        （2026-10-06 晚用户定：15/20 秒从**播完**起算，不是从 pending 出现起算）。

        幂等：已 arm 就忽略。仅夜间 / 猎人相位生效。

        🔴 2026-10-07：带了 `node`（节点 id）。节点时钟**只认当前节点**的 arm ——
        否则会有这一类竞态：一个节点刚结束、下一个节点刚开，而上一屏的 arm 还在路上
        ⇒ 新节点的窗口被提前/错位起算（女巫两问是**同一个人的两个节点**，最容易踩）。
        `node` 为空（老页面 / 猎人开枪 —— 它不在剧本节点里）时按旧行为放行。

        （原 `/arm` 路由的实现，抽成 Session 方法：路由与「假页面」harness 共用
        同一条路径 —— 否则 harness 验的就是另一套 arm 语义。）
        """
        if self.game.phase is Phase.NIGHT or self.game.phase is Phase.SHOOT:
            cur = night_driver._nn_cur(self)
            if node and cur and node != cur:
                return {"ok": False, "why": "节点已变（arm 过期）", "node": cur}
            if getattr(self, "human_wait_since", None) is None:
                self.human_wait_since = time.monotonic()
        return {"ok": True, "node": node or None}

    def public_state(self) -> Dict[str, Any]:
        g = self.game
        return {
            "gid": self.id,
            "board": g.board.label,
            # 板子的键也返回：界面「再来一局」要用它开同配置的新局，
            # 不能拿中文 label 反查。
            "board_key": self.board_key,
            "phase": g.phase.value,
            "round": g.round,
            "winner": g.winner,
            # night_node（2026-10-07 第三期）：当前夜间节点 id（读秒节拍进行到哪一段）。
            # 前端用它把 "AI 正在想…" 这类文案换成人话，也方便现场排查节拍。
            "night_node": night_driver._nn_cur(self),
            # ②整张夜表（2026-10-08「alarm 架构」）：客户端按表渲染，不再自己猜节点清单
            "night_plan": self._night_plan(),
            # now（2026-10-07 第三期）：**「现在该谁做什么」的唯一权威**。
            # 前端所有"要不要显示面板 / 要不要读秒 / 能不能开麦"的判断都改成读它，
            # 不再自己拿 night_node + 音频游标 + arm 状态去猜（见 now_state 的注释）。
            "now": self.now_state(),
            "public_log": list(g.public_log),
            "speech": list(self.speech),
            "timeline": list(self.timeline),
            "players": [
                {"seat": p.seat, "display": p.display, "lives": p.lives,
                 "alive": p.alive, "kind": p.kind}
                for p in g.players
            ],
            "speaking_order": list(g.speaking_order),
            "current_speaker": g.current_speaker(),
            # 投票时序（单机形态的关键）：手机是「交接」给投票人的，
            # 所以投票**进行中**绝不能给出票型 —— 递过去的一瞬间就破坏了投票独立性
            # （看得到别人的票就能跟随或反制）。
            #   · voted_seats：只说「谁投完了」，不含目标。界面用它显示进度是安全的。
            #   · votes：票型。**投票一结束就公布** —— 这正是「投票板」要的数据，
            #     它和真人面杀里举手投票是公开的这一点一致。
            # ⚠️ 别把它改成只认 Phase.EXILE：实测 1117 个采样点里 EXILE 一次都没被观察到
            #    （auto_advance 在 resolve_vote() 之后立刻 next_night()，EXILE 是瞬时态），
            #    写成只认 EXILE 等于**永远不公布票型**。
            # 下一轮 open_vote() 会清空 g.votes，所以「进行中」天然拿不到上一轮的票。
            "voted_seats": sorted(g.votes.keys()) if g.phase is Phase.VOTE else [],
            "votes": {} if g.phase is Phase.VOTE else dict(g.votes),
            "pending": self.pending,
            # 这一局是哪一档（娱乐/认真/硬核/地狱）。界面显示用；将来的战报和
            # 六维指标也必须按档分组 —— 不分档的胜率没有意义（不同档的 AI 不是一回事）。
            "level": self.level,
            # 这一局的发言方式（rotation / challenge）。界面得看得出来 ——
            # "小讨论"局里发言顺序会跳来跳去，不知道规则的人会以为是 bug。
            "speech_mode": g.rules.speech_mode,
            # 「本轮谁被质疑最多」：从**公开发言**里数出来的（L0 派生，人人相同）。
            # 它不是票型 —— 暗投阶段照样不泄露任何人的票。
            # 放进公共状态是为了：① 界面能显示舆论方向 ② 排查"AI 为什么这么投"
            # 时有据可查（这是本轮新增的、最容易被怀疑"是不是透视了"的那一项）。
            "suspicion": g.suspicion_this_round(),
        }

    def replay(self) -> Dict[str, Any]:
        """
        **结构化对局日志**（G5）：一局的全部事实，一份 JSON。

        为什么要有它：现在票型只是 public_log 里的一行文本（"投票结果：1，2→3"），
        界面、模型、评测都得自己去解析那行字 —— 播报措辞一改，三处一起崩。
        存成字段之后，战报（G6）、榜单（G7）、离线评测（批七）读的是**同一份**。

        🔴 **只允许局末调用**（接口层拦住）：它是**全知**的（含所有人的身份），
        局中给出去等于把身份告诉还在打的人，"断线恢复不泄密"那条会一起破。
        """
        g = self.game
        deaths = list(getattr(g, "deaths", []) or [])
        bye = {int(d["seat"]): d for d in deaths if d.get("seat") is not None}
        exiled_by_round = {int(d["round"]): int(d["seat"]) for d in deaths
                           if d.get("cause") == "vote" and d.get("round") is not None}
        return {
            "gid": self.id,
            "board": g.board.label,
            "level": self.level,
            "ended": g.winner in ("wolf", "good"),
            "winner": g.winner,
            "rounds": g.round,
            "seats": [
                {"seat": p.seat, "display": p.display, "kind": p.kind,
                 "role": p.role.value, "role_label": p.role.label,
                 # 人格与模型（H2/H3）：复盘要能看到"这局是谁在说话、由哪个模型服务"，
                 # 否则混编跑出来的数据没法归因。
                 "personality": getattr(self.agents.get(p.seat), "personality", None),
                 "model": getattr(self.agents.get(p.seat), "model", None),
                 "alive": p.alive, "lives": p.lives,
                 "died_round": (bye.get(p.seat) or {}).get("round"),
                 "died_cause": (bye.get(p.seat) or {}).get("cause")}
                for p in g.players
            ],
            "timeline": list(self.timeline),
            "public_log": list(g.public_log),
            "speeches": list(getattr(g, "speeches", []) or []),
            "votes": [
                {"round": i + 1,
                 "votes": {int(k): int(v) for k, v in h.items()},
                 "exiled": exiled_by_round.get(i + 1)}
                for i, h in enumerate(getattr(g, "vote_history", []) or [])
            ],
            "deaths": deaths,
            "nights": list(getattr(g, "night_records", []) or []),
            "metrics": metrics.summarize(g, self.level),
        }

    def seat_state(self, seat: int) -> Dict[str, Any]:
        if not 0 <= seat < len(self.game.players):
            raise ValueError("座位号越界")
        v = self.game.get_view_for(seat)
        v["speech"] = list(self.speech)
        v["timeline"] = list(self.timeline)
        v["pending"] = self.pending
        v["is_my_turn_to_speak"] = (self.game.current_speaker() == seat)
        v["is_my_turn_to_vote"] = (
            self.game.phase is Phase.VOTE and seat not in self.game.votes
            and self.game.player(seat).alive
        )
        return v
    def auto_advance(self, max_steps=300, deadline=None):        return night_driver.auto_advance(self, max_steps, deadline)
    def _announce_night(self):        return night_driver._announce_night(self)
    def _advance_night(self):        return night_driver._advance_night(self)
    def _advance_shoot(self):        return night_driver._advance_shoot(self)
    def _advance_exile(self):        return night_driver._advance_exile(self)
    def _advance_dawn(self):        return night_driver._advance_dawn(self)
    def _advance_discuss(self):        return night_driver._advance_discuss(self)
    def _advance_vote(self):        return night_driver._advance_vote(self)
    def _night_done(self):        return night_driver._night_done(self)
    def _run_one_ai_night(self):        return night_driver._run_one_ai_night(self)
    def _step_ai_wolf(self):        return ai_behaviors._step_ai_wolf(self)
    def _step_ai_seer(self):        return ai_behaviors._step_ai_seer(self)
    def _step_ai_witch(self):        return ai_behaviors._step_ai_witch(self)
    def _step_ai_guard(self):        return ai_behaviors._step_ai_guard(self)
    def _ai_wolf_propose(self, seat):        return ai_behaviors._ai_wolf_propose(self, seat)
    def _ai_wolf_maybe_follow(self, seat):        return ai_behaviors._ai_wolf_maybe_follow(self, seat)
    def _ai_guard_act(self, seat):        return ai_behaviors._ai_guard_act(self, seat)
    def _ai_witch_act(self, seat):        return ai_behaviors._ai_witch_act(self, seat)
    def _ai_speak(self, seat):        return ai_behaviors._ai_speak(self, seat)
    def _all_voted(self):        return night_driver._all_voted(self)
    def _run_one_ai_shoot(self):        return ai_behaviors._run_one_ai_shoot(self)
    def _pick_ai_target(self, seat, kind):        return ai_behaviors._pick_ai_target(self, seat, kind)
    def _fallback_target(self, seat, kind):        return ai_behaviors._fallback_target(self, seat, kind)
    def _run_one_ai_vote(self):        return ai_behaviors._run_one_ai_vote(self)
    def _pick_ai_vote(self, seat):        return ai_behaviors._pick_ai_vote(self, seat)
    def ensure_night_summaries(self):        return night_driver.ensure_night_summaries(self)
    def ensure_day_plans(self):        return night_driver.ensure_day_plans(self)

    # ---------------------------------------------------------- 人类动作

    def act(self, seat: int, action: str, target: Optional[int],
            text: Optional[str]) -> str:
        """
        ⚠️ **整段都在锁里**（2026-09-17 修）。

        这里是两个真 bug 的根因：`/public` 会推进状态机（drive），而 `/act` 也会
        （下面最后那行 auto_advance）。FastAPI 把这两个同步端点都丢进线程池，
        所以它们**真的会同时跑** —— 于是出现：

          · 同一个 AI 连说两次：两个线程都读到 current_speaker() == 同一个座位，
            都调了一次 _ai_speak ⇒ 那个 AI 说了两遍
          · 投票在倒数第二个人之后就开了：一句话的 _say 被挤到
            "发言结束，开始投票" 之后（时间线顺序错乱）
          · 机械上更糟的情况：speak_cursor 被两个线程各加一次 ⇒ **整段跳过一个发言人**

        以前只有 drive() 上锁，act() 没有 —— 那是这次两个症状的来源。
        锁是可重入以外不可嵌套的：这里只调用 auto_advance（它自己不上锁），不会死锁。
        """
        with self.lock:
            pending = self._act_locked(seat, action, target, text)
            # 成功提交 ⇒ 当前待办消失，倒计时作废（换人/换问重新起算）
            self.human_wait_since = None
            self.human_wait_pending_since = None
            return pending

    def _act_locked(self, seat: int, action: str, target: Optional[int],
                    text: Optional[str]) -> str:
        g = self.game
        if not 0 <= seat < len(g.players):
            raise ValueError("座位号越界")
        p = g.player(seat)
        if p.kind != "human":
            raise ValueError(f"{p.display} 是 AI，不能手动操作")

        if action == "speak":
            if g.current_speaker() != seat:
                raise ValueError("还没轮到你发言")
            body = (text or "").strip() or "（过）"
            self._say(f"{p.display}：{body}", seat, body)
            g.end_speech()

        elif action == "vote":
            if target is None:
                raise ValueError("投票需要 target")
            g.cast_vote(seat, target)

        elif action == "kill":
            if target is None:
                raise ValueError("刀人需要 target")
            # 多狼局里这是「点一个人」（提议），不是定案；单狼局点即定案。
            # target = -1 表示空刀提议，target == seat 表示自刀 —— 两者都合法（面杀本来就有）。
            g.night_kill(seat, target)

        elif action == "confirm":
            # 狼队协商的第二步：确认自己当前的点。全员（含人类狼）都确认后由引擎定案。
            # 它必须是一个**显式动作**：不确认就等于没表态，刀口定不下来、夜晚结束不了。
            g.night_confirm(seat)

        elif action == "investigate":
            if target is None:
                raise ValueError("验人需要 target")
            g.night_investigate(seat, target)

        elif action == "heal":
            if target is None:
                raise ValueError("用解药需要 target")
            g.night_heal(seat, target)

        elif action == "poison":
            if target is None:
                raise ValueError("用毒药需要 target")
            g.night_poison(seat, target)

        elif action == "guard":
            if target is None:
                raise ValueError("守人需要 target")
            g.night_guard(seat, target)

        elif action == "clear":
            # 🔴 2026-10-07（定稿交互）：轮盘**再点同一个号码 = 取消**。
            # 撤回的是"本窗口还没结算的临时决定"（守卫取消守护 / 狼撤点+撤确认 /
            # 预言家不验了（已看过的那一眼收不回）/ 女巫把药还回来 / 投票撤票）——
            # 详见 engine.night_clear。
            g.night_clear(seat)

        elif action == "shoot":
            # target 为 None = 放弃开枪（猎人自己选择不开）
            g.night_shoot(seat, target if target is not None else -1)

        elif action == "pass":
            # 三个用途：① 女巫决定本轮不用药（必须显式记录，否则夜晚结束不了）
            #           ② 猎人选择不开枪（必须显式记录，否则会卡在 SHOOT 相位）
            #           ③ 其他情况下的空动作，引擎不做任何事
            if g.phase is Phase.NIGHT and p.role is Role.WITCH:
                g.night_decline(seat)
            elif g.phase is Phase.NIGHT and p.role is Role.SEER:
                # 2026-10-06：预言家也有"放弃"出口（倒计时超时的注入通道，
                # 人类自己想不验也走这里）—— 否则验人必须验、夜永远结束不了。
                g.night_skip_investigate(seat)
            elif (g.phase is Phase.NIGHT and p.role is Role.GUARD
                  and not g.night.get("guard_asked")):
                g.night_guard(seat, -1)   # 空守（2026-10-06 引擎新增）
            elif g.phase is Phase.SHOOT:
                g.night_shoot(seat, -1)

        else:
            raise ValueError(f"未知动作：{action}")

        self._flush_log()
        _t0 = time.monotonic()
        self.ensure_night_summaries()         # 🔴 天黑投票后：前几天压成逐日摘要
        # 🔴 天亮预盘：默认停用（见 ENABLE_DAY_PLANS 的说明），接口保留
        if ENABLE_DAY_PLANS:
            self.ensure_day_plans()
        try:
            # ⚠️ **必须有预算**（见 ACT_BUDGET 的说明）：
            # 无预算时这一次请求会把后面所有 AI 的发言跑完（6 人局 ≈ 9 秒）才返回，
            # 前端又是拿到响应才开念 ⇒ 玩家感受就是"我说完等好久 AI 才开口"。
            self.pending = self.auto_advance(deadline=_t0 + ACT_BUDGET)
        except Exception:
            # 人类这一步**已经落库**了，但后续推进失败（引擎拒绝下一步 / 内部异常）。
            # 绝不能把 pending 留在过期值上 —— 那正是「卡住」的成因。
            # 置为 "working"：轮询会继续用 drive() 推进，而 drive 的闸门按真值判断。
            self.pending = "working"
            raise
        finally:
            # ★ 这一步**可能刚好把局打完** ⇒ 经验候选必须在这里收割。
            # 放在 finally 里（异常也不漏），而且必须在推进**之后**：
            # 写成推进前调用的话，"人类这一票就是终局票"这种情况下
            # 调用的那一刻 winner 还是 None ⇒ 经验静默丢掉（实测就是这么漏的）。
            self._harvest()
        _dt = time.monotonic() - _t0
        if _dt > 4.0:
            # 迟到的原因基本只有一个：真的在等模型。留一条日志，免得下次又靠猜。
            print(f"[slow] /act 推进用了 {_dt:.1f}s（pending={self.pending}）", flush=True)
        return self.pending


# ================================================================ 端点

def _get(gid: str) -> Session:
    s = SESSIONS.get(gid)
    if s is None:
        raise HTTPException(status_code=404, detail=f"没有这一局：{gid}")
    s.last_used = time.monotonic()      # 被访问过就算"活跃"，淘汰时别先挑它
    return s


# ⚠️ `/boards` 端点已删（旧预设迁移）：板子不再有"预设名"这个概念，
#    配置一律走人数（`/rules` 里的 preview 给出"这个人数会配成几狼几神几民"）。


def _preview(total: int) -> Dict[str, Any]:
    """N 人局会配成什么样。人数不够时给出**原因**，不让界面自己猜。"""
    try:
        b = make_board(1, total - 1)     # 人类/AI 怎么分不影响角色配置
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": True, "label": b.label, "wolves": wolf_count(total),
            "gods": [r.value for r in b.roles if r.is_god],
            "villagers": sum(1 for r in b.roles if r is Role.VILLAGER),
            "roles": [r.value for r in b.roles]}


@router.get("/rules")
def rules() -> Dict[str, Any]:
    """
    自动组局的规则。界面用它把「你选的 N 人 = 几狼几神几民」直接显示给玩家。

    放在服务端而不是前端写死：规则只有一份，改的时候两边不会漂移。
    """
    return {
        "wolf_ratio": WOLF_RATIO,
        # 神职启用顺序（女巫→预言家→猎人→守卫→白痴）；**神数 = 狼数**
        "god_order": [r.value for r in GOD_ORDER],
        # 四档氛围：**前端只读这里的键**，不自己写死档位名
        # （否则加一档就要改前端，而"忘了改前端"的表现是这一档永远选不到）。
        "levels": [{"key": k, "desc": v["desc"]} for k, v in LEVELS.items()],
        "default_level": DEFAULT_LEVEL,
        # 人格（H2）：**前端只读这张表**（和档位按钮同一个理由）。
        # 额外给两个特殊值：不配 / 随机分配 —— 界面上它们是按钮，不是人格。
        "personalities": [{"key": k, "label": v["label"], "brief": v["brief"]}
                          for k, v in PERSONALITIES.items()],
        "personality_specials": [{"key": "off", "label": "不配人格"},
                                 {"key": "random", "label": "随机分配"}],
        # 覆盖到界面输入框允许的最大值（真人 12 + AI 12），两边范围必须一致 ——
        # 否则玩家选了个界面允许、但 preview 没覆盖的人数，**按钮会静默不生效**。
        "preview": {str(n): _preview(n) for n in range(4, 25)},
    }


@router.get("/{gid}/scoreboard")
def scoreboard(gid: str) -> Dict[str, Any]:
    """
    这一局的**六维指标 + 人机对比**（测量层，批四）。

    ⚠️ 它读的是**全知状态**（含所有人的身份），所以只给复盘/指标/榜单看 ——
       **绝不能**把它塞进任何 AI 的 prompt（那等于把 L3 交给 AI）。
    还没打完的局也能查（就是"截至此刻"的快照），`outcome.ended` 说明它完没完。
    """
    s = _get(gid)
    return metrics.summarize(s.game, s.level)


@router.get("/stats")
def stats() -> Dict[str, Any]:
    """
    跨局聚合（榜单 v0）。**按档分组** —— 不分档的胜率没有意义：
    不同档的 AI 根本不是一回事（赌注措辞、说话要求都不同）。

    诚实边界：只统计**内存里还在**的局（探针阶段不做持久化）。
    真正的榜单要等结构化日志落盘（批次 5/6）。
    """
    boards = [metrics.summarize(s.game, s.level)
              for s in SESSIONS.values() if s.game.winner in ("wolf", "good")]
    return metrics.aggregate(boards)


@router.get("/settings")
def settings_get() -> Dict[str, Any]:
    """
    规则开关（G9 的"可配"那一半）：**整张表 + 当前值**。

    为什么返回整张表而不是只返回值：前端不该自己写死"有哪几个开关、
    哪个是布尔哪个是单选" —— 那样加一个开关要改两处，漏改的表现是那一项永远调不到。
    """
    return {"switches": RULE_SWITCHES, "current": dict(SETTINGS),
            "note": "只影响新开的局；已在打的那一局规则不会变（中途改规则会让桌上的人白判断）"}


@router.post("/settings")
def settings_set(req: SettingsReq) -> Dict[str, Any]:
    """设置规则开关。未知键或非法值一律 400，**不静默忽略**。"""
    try:
        clean = _valid_settings(req.settings)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    SETTINGS.update(clean)
    return settings_get()


@router.get("/games")
def games() -> Dict[str, Any]:
    """
    已结束的对局清单（榜单页要它）。

    **只列已结束的**：局中那几局的身份还没公开，列出来会让人点进战报
    看到不该看的东西（战报接口本身也会拦，这是第二道）。
    """
    out = []
    for s in SESSIONS.values():
        if s.game.winner not in ("wolf", "good"):
            continue
        out.append({"gid": s.id, "board": s.game.board.label, "level": s.level,
                    "winner": s.game.winner, "rounds": s.game.round,
                    "report": f"/api/game/{s.id}/report.md",
                    "replay": f"/api/game/{s.id}/replay"})
    out.sort(key=lambda x: x["gid"], reverse=True)      # 新的在前
    return {"games": out, "persistent": False, "note": "进程内存；重启即清空"}


@router.get("/{gid}/report.md")
def report_md(gid: str) -> PlainTextResponse:
    """
    本局战报（G6，Markdown）。**和复盘日志同理：只在局末给** ——
    战报会揭示所有人的身份，局中给出去等于把答案塞给还在打的人。
    """
    s = _get(gid)
    if s.game.winner not in ("wolf", "good"):
        raise HTTPException(status_code=409,
                            detail="对局还没结束：战报会揭示所有人的身份，局中不能取")
    try:
        md = report_mod.markdown(s)
    except Exception as e:                       # noqa: BLE001
        # 战报是收尾动作，它崩了不该让玩家看到 500 —— 给一份最小可读的降级版
        md = (f"# 本局战报（生成降级）\n\n这一局的战报生成失败：{type(e).__name__}: {e}\n\n"
              f"结构化日志仍然可用：`/api/game/{gid}/replay`\n")
    return PlainTextResponse(md, media_type="text/markdown; charset=utf-8",
                             headers={"Cache-Control": "no-store"})


@router.get("/{gid}/replay")
def replay(gid: str) -> Dict[str, Any]:
    """
    结构化对局日志（G5）。**只在局末给** —— 它是全知视角（含所有人的身份），
    局中返回等于把身份透给还在打的人。
    """
    s = _get(gid)
    if s.game.winner not in ("wolf", "good"):
        raise HTTPException(
            status_code=409,
            detail="对局还没结束：复盘日志含所有人的身份，局中不能取")
    return s.replay()


@router.get("/experience")
def experience_list() -> Dict[str, Any]:
    """
    经验候选 + 已入库清单（G3）。

    ⚠️ **候选 ≠ 生效**：只有复核入库（approve）的才会进下一局的 prompt，
    而且只在该档开启（硬核/地狱）时、且只给对应角色的座位。
    """
    return experience.BANK.snapshot()


@router.post("/experience/review")
def experience_review(req: ReviewReq) -> Dict[str, Any]:
    """复核一条候选：approve=true 入库，false 丢弃。"""
    it = experience.BANK.review(req.id, req.approve)
    if it is None:
        raise HTTPException(status_code=404, detail=f"没有这条候选：{req.id}")
    return {"ok": True, "item": it, "bank": experience.BANK.snapshot()}


@router.post("/new")
def new_game(req: NewGameReq) -> Dict[str, Any]:
    try:
        s = Session(humans=req.humans, seed=req.seed, ais=req.ais,
                    level=req.level, personality=req.personality,
                    layout=req.layout)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    # 🔴 2026-10-26：暂停门状态是跨局持久的（lab/control.json）—— 上一局回退后
    # 忘了解暂停，新局开局就被门拦住"对局不动"（用户踩过）。新局 = 新意图，
    # 开局即自动解除暂停；回退/单步的暂停语义只属于当前那一局。
    try:
        import lab_api as _lab
        _lab.set_paused(False)
    except Exception:                                    # noqa: BLE001
        pass

    # 只保留最近若干局，避免内存无限增长（探针阶段不做持久化）。
    # ⚠️ 按**最近使用**淘汰，不是按创建顺序 —— 按创建顺序会把「人家正在打的那一局」挤掉，
    # 那个页面随即开始拿 404，而它的轮询循环原本是静默 return 的 ⇒ 界面冻住。
    # 实测：日志里出现过 /api/game/{gid}/public 404。
    if len(SESSIONS) >= MAX_SESSIONS:
        stalest = min(SESSIONS.items(), key=lambda kv: kv[1].last_used)[0]
        SESSIONS.pop(stalest, None)
    SESSIONS[s.id] = s

    return {
        "gid": s.id,
        "board": s.game.board.label,
        "seats": [
            {"seat": p.seat, "display": p.display, "kind": p.kind}
            for p in s.game.players
        ],
        "human_seats": [p.seat for p in s.game.players if p.kind == "human"],
        "public": s.public_state(),
    }


@router.post("/{gid}/arm")
def arm_countdown(gid: str, node: str = ""):
    """前端：法官念完该角色「请睁眼」那句 + 2 秒后调用 —— **倒计时此刻才开始**
    （2026-10-06 晚用户定：15/20 秒从播完起算，不是从 pending 出现起算）。
    幂等：已 arm 就忽略。仅夜间 / 猎人相位生效。

    🔴 2026-10-07 第三期：带了 `node`（节点 id）。节点时钟**只认当前节点**的 arm ——
    否则会有这一类竞态：一个节点刚结束、下一个节点刚开，而上一屏的 arm 还在路上
    ⇒ 新节点的窗口被提前/错位起算（女巫两问是**同一个人的两个节点**，最容易踩）。
    `node` 为空（老页面 / 猎人开枪——它不在剧本节点里）时按旧行为放行。
    """
    return _get(gid).arm_window(node)


def _pending_seats_of(s) -> Dict[str, Any]:
    """谁现在需要操作手机？（原 @router 实现，抽成模块函数）

    只返回「哪个座位、要做什么动作」，**不含任何私有信息** ——
    这样界面层就不必为了知道该叫谁而先去拉所有人的私有视图。

    ⚠️ 抽出来的原因（2026-10-07 第三期）：服务端 `now_state()` 与「假页面」
    回归 harness（`_smoke-night-live.py`）都要用它，而它们都不该去开 HTTP。
    """
    g = s.game
    # 投票阶段按 vote_order 排（用户定的规则：从前一晚被刀的人开始顺时针），
    # 其他阶段按座位号 —— 界面据此决定"手机先递给谁"。
    order = g.vote_order if g.phase is Phase.VOTE else [p.seat for p in g.players]
    # 当前夜间节点（2026-10-07 第三期）：前端门控 / 倒计时都认它。
    _cur = night_driver._nn_cur(s) if g.phase is Phase.NIGHT else None
    need = []
    for seat in order:
        p = g.player(seat)
        if p.kind != "human":
            continue
        # ⚠️ 不能一律跳过死人：**要开枪的猎人刚刚死了**（SHOOT 相位里他还有一个动作要做）。
        #    一律跳过 ⇒ 前端永远收不到"该把手机交给 X 号" ⇒ 卡死在 human_shoot。
        if not p.alive and g.phase is not Phase.SHOOT:
            continue
        acts = g.actions_for(p.seat)
        # 2026-10-07 剧本节拍：女巫的窗口不因"药已用"消失（防泄露）——
        # 夜间女巫没有可做动作时给 noop 占位待办，前端照常走满窗口。
        if (not acts and g.phase is Phase.NIGHT and p.role.value == "witch"
                and p.alive):
            acts = ["noop"]
        if acts:
            # role（2026-10-06 P1）：前端夜间门控用 —— 对齐 timeline 条目上
            # 的 stage 标记（都是服务端单一真相，两边不再各猜一份）。
            # 值就是 engine.Role 的 value（'wolf'/'witch'/'seer'/'guard'/'hunter'…）。
            item = {"seat": p.seat, "display": p.display, "actions": acts,
                    "role": p.role.value}
            # node（2026-10-07 第三期）：前端夜间门控认**节点 id** ——
            # 「该你动 == 时间线里带这个 node 的那一句已经被播到」（见 tableui.nightReady）。
            if g.phase is Phase.NIGHT:
                nid = night_script.node_for_acts(acts)
                if nid is None and acts == ["noop"] and p.role.value == "witch":
                    # 女巫的占位待办（还没到她的窗口 / 已没药）——归她**当前**那个窗口，
                    # 否则面板在守卫/狼刀那两段就抢出来了。
                    nid = ("witch_poison" if g.night.get("witch_asked")
                           else "witch_heal")
                item["node"] = nid
            # 倒计时（2026-10-06 用户定）：夜间待办下发剩余秒数，前端只渲染。
            # 🔴 2026-10-07 第三期：秒数改由**节点时钟**算（窗口只认 night_script；
            #    `NIGHT_ACT_WINDOW` 已删）。只有"该待办就是当前节点"才下发 ——
            #    后面节点的待办不该已经在读秒。
            if _cur and item.get("node") == _cur:
                # ⚠️ 2026-10-07 第四起（用户："倒计时节点开始过早"）：这里**必须**要求
                #    `human_wait_since`（= 前端 arm 过 = **法官已念完那句 + 2 秒**）——
                #    定稿的口径就是"15/20 秒从**播完**起算，不是从 pending 出现起算"。
                #    上一轮我把这个条件摘掉（因为那时 arm 永远不来：面板被音频卡死），
                #    结果秒数跟着节点一起开始跑 ⇒ 用户听到的就是"面板还没念到就已在读秒"。
                #    正确的解法是**修面板**（面板已经改成服务端驱动了），不是把起算点提前。
                if getattr(s, "human_wait_since", None) is not None:
                    left = night_driver._nn_deadline(s, _cur) - time.monotonic()
                    item["seconds_left"] = round(max(0.0, left), 1)
            need.append(item)
    return {"pending": s.pending, "seats": need}


@router.get("/{gid}/pending_seats")
def pending_seats(gid: str) -> Dict[str, Any]:
    return _pending_seats_of(_get(gid))


@router.get("/{gid}/public")
def public(gid: str) -> Dict[str, Any]:
    """
    ⚠️ 这是**唯一会推进状态机的 GET** —— 它会替 AI 干活（最多 POLL_BUDGET 秒）。

    为什么必须这样：AI 的决策要调模型，大人数局一次跑不完一个回合。
    如果只有 POST /act 才推进，那 13 人局会有几十秒完全没反应。
    让它在这里推，开局就能立刻返回、桌面自己长出来。
    （`pending == "working"` 就表示"还在推进"，前端照常继续轮询即可。）
    """
    s = _get(gid)
    s.drive(POLL_BUDGET)
    return s.public_state()


@router.get("/{gid}/seat/{seat}")
def seat_view(gid: str, seat: int) -> Dict[str, Any]:
    try:
        return _get(gid).seat_state(seat)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/{gid}/act")
def act(gid: str, req: ActionReq) -> Dict[str, Any]:
    s = _get(gid)
    try:
        pending = s.act(req.seat, req.action, req.target, req.text)
    except (ValueError, PermissionError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}") from e
    return {"ok": True, "pending": pending, "public": s.public_state()}
