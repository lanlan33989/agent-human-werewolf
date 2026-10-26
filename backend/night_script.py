# -*- coding: utf-8 -*-
"""夜间剧本表（2026-10-07 用户定稿）：播报为节拍器的夜推进。

设计要点（用户逐条拍板，勿改）：
  · 夜 = 固定节点流水线：节点 = 播报句 + 倒计时窗口 + 收割点；**闭眼句在窗口后**。
  · 窗口固定（神职 15s / 狼刀 20s），**玩家不可加速、读秒不可跳过**。
  · 节点间隔（定稿逐字）：
      【播报】XX请睁眼 →（念完 +2 秒）开始倒计时 →（窗口结束的**同时**）面板消失
      + **同时【播报】XX请闭眼** → 播报最后一个字之后等待 3 秒 → 下一个节点的开眼句。
    所以本表的间隔分两个方向：
      `gap`       = 这句**开眼句**之后的留白 —— 窗口结束后紧接着的那句**闭眼句**是
                    "同时"念的 ⇒ 默认 **0**（不是 3！第三期改的，见 §10.7）。
      `close_gap` = **闭眼句**之后的留白 ⇒ 默认 `GAP`(=3)，下一个节点才开。
      `open`/`dawn` 两个节点由引擎自己的公共播报承担（`SKIP_IN_ANNOUNCE`），
      它们那一行的留白由引擎发给 timeline 的 `delay`（= `speech_io.NIGHT_GAP`）负责。
  · 节点只按**初始板子**过滤（板子公开，去掉没有的角色节点不泄露）；
    **绝不因"药已用 / 角色已死"跳过节点** —— 固定节拍本身防泄露
    （女巫没药也要走满"你要使用毒药吗？"的 15 秒）。
  · AI 决策：节点开始即后台异步发起；到收割点未完成 = 默认放弃并打断，
    **结果直接丢弃**（用户定：LLM 不缓存）。收割点写在节点上、**已被驱动器消费**：
      · `harvest_start`：狼刀节点**开始**时收 [守卫, 狼]；
      · `harvest_end`  ：预言家节点**结束**时收 [女巫, 预言家]。
  · 人类：操作面板与倒计时严格绑定 —— 倒计时归零面板即消失，以消失时状态为准：
      守卫 = 轮盘单选可切可取消；狼刀 = 单选可切可取消 + 空刀（AI 狼无脑跟随，
      不触发 LLM；最终刀口作为私有信息传所有 AI 狼；预留 AI 二次思考接口）；
      女巫 = 解药大按钮 toggle + 毒药轮盘（**两个窗口**：解药 15s → 毒药 15s，
      中间那句"你要使用毒药吗？"是**同时**念的，没有 3 秒留白）；
      预言家 = 轮盘。未操作默认：守卫没守 / 女巫没用 / 预言家没验 / 狼跟随 AI
      （全空则空刀）。
  · 投票：最后发言结束即触发 AI 并发投票；人类轮流上机，轮盘 + 「OK / 弃票」
    （点击即锁定并切到下一人类）；AI 未投者等 5s（`AI_VOTE_GRACE`），仍没投视为弃票。
    ⚠️ 投票**没有**夜间窗口/闭眼句（定稿只给了 5s 兜底）。
  · 遗言（被票出/被枪/夜刀）：**接口占位，先不做**（见 on_executed 空钩子）。

接线（2026-10-07 第二期「异步化」+ 第三期「窗口节拍」）：
  · 节点时钟：`night_driver._night_clock()` 按本表**增量**补播 ——
    节点开始播 open 句、窗口结束播 close 句（不再入夜时一次发完）。
  · 窗口秒数**只认这张表**（`window_of` / `window_for_acts`）；
    `night_driver.NIGHT_ACT_WINDOW` 已删除（两份数字必然漂移）。
  · 前端门控认**节点 id**（timeline 条目与 pending_seats 都带 `node`），
    不再靠"念到第几句"（`nightStageIdx` 那套降级为老局兜底）。
  · 测试钩子：`set_win_scale(x)` 按比例缩短窗口（`_smoke-*` 用；生产恒 1.0）。
"""
import os
from typing import Dict, List, Optional

# ---------------------------------------------------------------- 时长（秒，用户定）
WIN_GUARD = 15          # 守卫请睁眼
WIN_WOLF = 20           # 狼人请睁眼（狼队共用一个窗口）
WIN_GOD = 15            # 女巫两问 / 预言家 / 猎人开枪
AI_VOTE_GRACE = 5       # AI 投票兜底
GAP = 3                 # 闭眼句之后、下一个开眼句之前的留白（用户定：3 秒）

# 测试钩子（**只在测试里调**）：0.05 ⇒ 15s→0.75s。生产恒 1.0。
# 为什么要有它：窗口是真·墙上时间，不缩短短跑不进 CI（一夜四段 = 65 秒）。
_WIN_SCALE: List[float] = [float(os.environ.get("NIGHT_WIN_SCALE", "1"))]


def set_win_scale(x: float) -> None:
    """测试钩子：按比例缩短窗口（`_smoke-*` 在 import server 之前调）。"""
    _WIN_SCALE[0] = float(x)


def win_scale() -> float:
    return _WIN_SCALE[0]


def _scaled(sec: Optional[float]) -> float:
    if not sec:
        return 0.0
    return float(sec) * _WIN_SCALE[0]


# 这两个节点的播报**不由节点时钟发**：
# "天黑请闭眼" 由引擎 _open_night 的公共播报承担，"天亮" 由 open_discuss 承担。
SKIP_IN_ANNOUNCE = ("open", "dawn")
# 第三期之后**没有**"延后补播"的节点了 —— 预言家结果/闭眼就是两个普通节点，
# 由节点时钟在窗口结束时按顺序发。旧名字保留（空元组）防老 import 炸。
DEFERRED: tuple = ()

# 节点 → 前端夜间门控用的 stage（= engine.Role 的 value）。
# ⚠️ 女巫两问：`witch_heal` 那一条带 stage="witch"（面板在她那句念到之后开），
#    `witch_poison` 也带 stage="witch"（同一个角色的第二个窗口）。
STAGE: Dict[str, str] = {
    "guard": "guard", "wolf": "wolf",
    "witch_heal": "witch", "witch_poison": "witch",
    "seer": "seer", "seer_result": "seer_result", "seer_close": "seer_close",
    "open": "open", "dawn": "dawn",
}

# 动作 → 节点（**窗口/门控都按节点查**，绝不再立第二份数字或第二份映射）。
# "confirm" 不是独立动作（它是狼刀的锁定键），"pass" 是放弃 —— 都不带节点。
ACT_NODE: Dict[str, str] = {
    "guard": "guard",
    "kill": "wolf",
    "heal": "witch_heal",
    "poison": "witch_poison",
    "investigate": "seer",
}


def stage_of(node_id: str) -> Optional[str]:
    return STAGE.get(node_id)


def node(say: str, nid: str, win: int = 0, gap: int = 0,
         close: Optional[str] = None, close_gap: int = GAP,
         panel: Optional[str] = None, run: tuple = (),
         harvest_start: tuple = (), harvest_end: tuple = (),
         roles: tuple = (),
         default: Optional[str] = None, accept_noop: bool = False) -> Dict:
    """剧本节点。

    · `win`       = 倒计时窗口（秒，原始值；查表时按 `_WIN_SCALE` 缩放）
    · `gap`       = 开眼句之后的留白（默认 0 = "窗口结束的同时念闭眼句"）
    · `close`/`close_gap` = 闭眼句 / 它之后的留白（默认 GAP）
    · `roles`     = 该节点涉及的角色键（空 = 无条件保留）
    · `default`   = 🔴 2026-10-08 表化（用户定的「alarm 架构」①）：到点没人操作
                    注入什么。'pass'=放弃；'follow_kill'=狼刀跟随已点过的狼；
                    **None=什么都不注入**（女巫解药：注入 pass 会把毒药那一问
                    掐死 —— `witch_asked` 一置位就再也不会问了，用户要它照常走）。
                    驱动器不再按 acts 猜（第四起事故的案发地就在那串分支里）。
    · `accept_noop` = noop 占位待办（女巫"药已用"防泄露）算不算本节点的人类。
    """
    return dict(id=nid, say=say, win=win, gap=gap, close=close,
                close_gap=close_gap, panel=panel, run=run,
                harvest_start=harvest_start, harvest_end=harvest_end,
                roles=roles, default=default, accept_noop=accept_noop)


# 夜剧本。build() 按板子过滤 roles；**顺序就是播报顺序**。
NIGHT_NODES: List[Dict] = [
    node("第{n}夜，天黑请闭眼", "open", gap=GAP),
    node("守卫请睁眼，请选择今晚你要守护的人", "guard", win=WIN_GUARD,
         panel="pick", run=("guard",), roles=("guard",), close="守卫请闭眼",
         default="pass"),
    node("狼人请睁眼，请选择今晚你们要击杀的人", "wolf", win=WIN_WOLF,
         panel="wolf", run=("wolf",), harvest_start=("guard", "wolf"),
         roles=("wolf",), close="狼人请闭眼", default="follow_kill"),
    # 女巫两问跨节点连续：解药 toggle + 毒药轮盘，面板不消失（一个会话两个窗口）。
    # 第二句是**同时**念的（gap=0）—— 定稿：「15秒倒计时结束的同时→…→同时
    # 【播报】你要使用毒药吗？」。
    # ⚠️ 解药那句**带"今晚死的是他，你要救吗？"**（定稿逐字："同时【播报】女巫请睁眼，
    #    今晚死的是他，你要救吗？"）。这句是**固定文案**（不点名）⇒ 全桌听见也不泄露是谁；
    #    "谁被刀"由人类女巫那屏的**抓痕**（`.victim`）承担；空刀则没有抓痕、按钮置灰。
    #    🔴 我上一版按"播报文案固定不变"把这段砍掉了 —— 那是**误读**（用户当天纠正）。
    node("女巫请睁眼，今晚死的是他，你要救吗？", "witch_heal", win=WIN_GOD,
         panel="witch_heal", run=("witch",), roles=("witch",),
         accept_noop=True),          # default=None：到点**绝不**注入（否则掐死毒药问）
    node("你要使用毒药吗？", "witch_poison", win=WIN_GOD,
         panel="witch_poison", roles=("witch",), close="女巫请闭眼",
         default="pass", accept_noop=True),
    node("预言家请睁眼，请选择今晚你要查验的人", "seer", win=WIN_GOD,
         panel="pick", run=("seer",), harvest_end=("witch", "seer"),
         roles=("seer",), close=None, default="pass"),
    # 预言家的收尾是**两个节点**（不是一个节点加两句）：
    #   窗口结束的同时念「他是这个」（gap=0），3 秒后念「预言家请闭眼」，
    #   再 3 秒后引擎念「天亮了…」—— 逐字对齐定稿。
    node("他是这个", "seer_result", win=0, gap=GAP, panel="seer_result",
         roles=("seer",)),
    # 🔴 2026-10-12（用户四人局实机：没有"预言家请睁眼"却有"预言家请闭眼"）：
    #    本节点原来**忘了挂 roles 过滤** —— seer/seer_result 都按板上有没有预言家
    #    过滤，唯独收尾这一句无条件保留 ⇒ 没有预言家的局，夜里凭空念一句
    #    "预言家请闭眼"。三段必须一起过滤。
    node("预言家请闭眼", "seer_close", win=0, gap=GAP, roles=("seer",)),
    node("天亮了，请所有玩家睁眼", "dawn", gap=0),
]

_BY_ID: Dict[str, Dict] = {nd["id"]: nd for nd in NIGHT_NODES}
_NODE_ACTS: Dict[str, tuple] = {}
for _act, _nid in ACT_NODE.items():
    _NODE_ACTS[_nid] = tuple(_NODE_ACTS.get(_nid, ())) + (_act,)


def build(roles_present: set, round_no: int) -> List[Dict]:
    """按本局板子生成实际节点表。roles_present = 引擎 Role.value 的集合。
    只按板子过滤（公开信息）；药/死状态不影响节点存在。"""
    out: List[Dict] = []
    for nd in NIGHT_NODES:
        if nd["roles"] and not set(nd["roles"]) & roles_present:
            continue
        d = dict(nd)
        d["say"] = d["say"].replace("{n}", str(round_no))
        out.append(d)
    return out


def acts_of(node_id: str) -> tuple:
    """这个节点涵盖哪些人类动作（空 = 纯播报节点，没有面板/窗口）。"""
    return _NODE_ACTS.get(node_id, ())


def default_of(node_id: Optional[str]) -> Optional[str]:
    """到点没人操作时注入什么（**表说了算**，2026-10-08 表化①）。
    语义见 `node()` 的 `default` 说明。查不到（猎人开枪等不在剧本里的）= 'pass'。"""
    nd = _BY_ID.get(node_id or "")
    return nd.get("default") if nd else "pass"


def accepts_noop(node_id: Optional[str]) -> bool:
    """noop 占位待办（女巫"药已用"防泄露）算不算这个节点的人类。"""
    nd = _BY_ID.get(node_id or "")
    return bool(nd and nd.get("accept_noop"))


def node_for_acts(acts) -> Optional[str]:
    """这组待办属于哪个节点（按剧本顺序取第一个命中的）。
    女巫：还没答解药 ⇒ `[heal,…]` ⇒ witch_heal；答完 ⇒ `[poison,…]` ⇒ witch_poison。
    认不出（`pass`/`shoot`/空）⇒ None。"""
    s = set(acts or ())
    if not s:
        return None
    for nd in NIGHT_NODES:
        if s & set(_NODE_ACTS.get(nd["id"], ())):
            return nd["id"]
    return None


def window_of(node_id: Optional[str]) -> float:
    """节点的倒计时窗口（秒，已按 `_WIN_SCALE` 缩放）。查不到 = 神职档 15s。"""
    nd = _BY_ID.get(node_id or "")
    if nd is None:
        return _scaled(WIN_GOD)
    return _scaled(nd.get("win"))


def window_for_acts(acts) -> float:
    """一组待办的窗口 —— 按它所属的节点查（**唯一的窗口来源**）。"""
    return window_of(node_for_acts(acts))


def window_for_act(act: Optional[str]) -> float:
    """单个动作的窗口（AI 预跑的上限用它）。"""
    return window_of(ACT_NODE.get(act or ""))


def on_executed(kind: str, ctx: dict) -> None:
    """遗言钩子（占位，2026-10-07 用户定：先不做）。
    kind ∈ {'voted_out', 'shot', 'night_killed'}；ctx 带座位/死因/轮次。
    以后做遗言时在这里接入播报与发言流程。"""
    return None
