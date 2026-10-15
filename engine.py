# -*- coding: utf-8 -*-
"""
狼人杀规则引擎（D2）

三条设计原则：

1. 引擎与界面分离
   所有「某人此刻该看到什么」都由 get_view_for(seat) 回答，
   而不是让渲染层去读全局状态。这是单机 / 多机切换的唯一前提，
   也是 v7 原型那笔技术债（renderScreen 耦合）的正确修法。

2. 私有信息隔离做在结构上，不做在提示词里
   四层：
     L0 全局公开   -> self.public_log，所有人都能看到
     L1 组内共享   -> 狼队互知队友（这是 autodungeon 那套两层模型缺的一层）
     L2 个体私有   -> 预言家验人记录、女巫用药，只有本人能看
     L3 元信息     -> 引擎内部状态，任何人（包括 AI）都不可见
   本模块不负责输出侧检查，那是调用方的事。

3. 引擎只做确定性逻辑，不含任何 LLM 调用
   AI 决策在别的模块里，引擎不 import 任何模型 SDK。

用法：
    from engine import Game, make_board
    g = Game(make_board(2, 2), kinds=["human", "ai", "human", "ai"])
    g.start()
    ...
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# 测量层。**方向必须是 engine → metrics 单向**：
# metrics 只用鸭子类型读 game，不 import 本模块，所以不会成环。
# 它被引擎用到的有两处，都在"从公开发言里读信息"这条线上（同样是 L0 派生）：
#   · suspicion_counts —— 本轮谁被质疑最多（要进 AI 的视图）
#   · accuse_targets   —— **这一条**发言指向了谁（挑战式发言顺序用它决定谁先回应）
from metrics import accuse_targets, suspicion_counts


# ================================================================ 基础类型

class Role(str, Enum):
    WOLF = "wolf"
    VILLAGER = "villager"
    SEER = "seer"
    WITCH = "witch"
    # 2026-09-17 用户定：三神**全加**（猎人 / 守卫 / 白痴）。
    # 出场顺序见 GOD_ORDER —— 神职按"狼数"从前往后取。
    HUNTER = "hunter"
    GUARD = "guard"
    IDIOT = "idiot"

    @property
    def label(self) -> str:
        return {
            Role.WOLF: "狼人",
            Role.VILLAGER: "村民",
            Role.SEER: "预言家",
            Role.WITCH: "女巫",
            Role.HUNTER: "猎人",
            Role.GUARD: "守卫",
            Role.IDIOT: "白痴",
        }[self]

    @property
    def is_wolf(self) -> bool:
        return self is Role.WOLF

    @property
    def is_god(self) -> bool:
        """神职（含三神）。注意胜负只分「狼 / 好」，神与民同属好人。"""
        return self in (Role.SEER, Role.WITCH, Role.HUNTER, Role.GUARD, Role.IDIOT)


class Phase(str, Enum):
    SETUP = "setup"
    NIGHT = "night"          # 夜间行动
    DAWN = "dawn"            # 夜晚结算 + 播报
    DISCUSS = "discuss"      # 白天轮流发言
    VOTE = "vote"            # 投票
    EXILE = "exile"          # 投票结算
    # 2026-09-17 新增：猎人开枪。
    # 它要插在**两处**（夜间死讯公布之后 / 投票结算之后），所以必须是一个独立相位，
    # 而不是夹在别处的一个 if —— 否则判胜会抢在开枪之前（见 resolve_vote 的说明）。
    SHOOT = "shoot"
    ENDED = "ended"


@dataclass
class Player:
    seat: int
    kind: str                 # "human" | "ai"
    role: Role
    lives: int = 2
    alive: bool = True
    name: str = ""

    @property
    def display(self) -> str:
        return self.name or f"{self.seat} 号"


@dataclass
class Board:
    key: str
    label: str
    roles: List[Role]
    # 每人生命数。**2026-09-15 用户决定：一人一条命**（原来的「每人 2 条命」已去掉）。
    lives: int = 1
    note: str = ""


@dataclass
class Rules:
    """
    可配规则开关（G9）。默认值全部取**标准局判例**，改一个只影响这一局。

    为什么要有它：这些分歧点各版本做法不同（网杀 vs 面杀），写死的话
    "换个玩法"就得改代码；做成开关后，前端/测试都能直接指定。
    """
    # 猎人被女巫毒死能不能开枪 —— 网杀标准是**不能**，面杀有变体
    hunter_can_shoot_on_witch_poison: bool = False
    # 守卫能不能守自己（多数版本允许）
    guard_can_self_guard: bool = True
    # 守卫能不能连续两晚守同一个人（标准：不能）
    guard_can_repeat: bool = False
    # 平票怎么处理：no_exile（标准：无人出局）/ random（随机出一个）
    tie_vote_rule: str = "no_exile"
    # 发言方式（2026-09-18 用户定）：
    #   rotation  = 依次轮转（现设，默认 —— 不传这个开关的老调用方行为一字不变）
    #   challenge = 「被点名的人先回应」：X 的发言里对 Y（或 Y、Z）有负面指向
    #               ⇒ Y（Z）先回应，之后 X 再回应，然后**照常**轮到 X 的下一位。
    # 为什么要有它：纯轮转时，最后几个发言的人握着"本轮已经没人能回嘴"的位置，
    # 带节奏的成本极低（他点什么、没人当场反驳，第二天大家只记得他说过）。
    # 让被点名的人**当场**开口，是把"带节奏"重新变回一件有代价的事。
    speech_mode: str = "rotation"


# 规则开关的**单一真相表**（G9 的"可配"那一半）。
# 前端按它渲染控件、接口按它校验 —— 两边都不许自己写死键名和选项，
# 否则加一个开关就要改三处，而"漏改一处"的表现是那一项静默不生效。
RULE_SWITCHES: List[Dict[str, Any]] = [
    {
        "key": "hunter_can_shoot_on_witch_poison",
        "label": "猎人被毒杀也能开枪",
        "type": "bool",
        "default": False,
        "note": "网杀标准：不能（被毒死不算是被狼杀的，枪哑火）；面杀有变体",
    },
    {
        "key": "guard_can_self_guard",
        "label": "守卫可以守自己",
        "type": "bool",
        "default": True,
        "note": "多数版本允许；禁掉之后守卫的选人要更谨慎",
    },
    {
        "key": "guard_can_repeat",
        "label": "守卫可以连守同一人",
        "type": "bool",
        "default": False,
        "note": "标准：不能连守（否则守着一个人就无敌）",
    },
    # 🔴 2026-10-12（用户：平票怎么处理/发言方式两个选项**彻底删除**）：
    #    两个开关从表里摘掉 ⇒ 前端不再展示、接口不再可设；
    #    行为固定为 `no_exile`（无人出局）+ `rotation`（依次轮转）。
    #    `challenge`/`random` 的休眠机制留在引擎里未拆（拆它要动 4 个 smoke 文件），
    #    已不可达 —— 后续清理见 HANDOFF。
]


# ================================================================ 板子怎么定
# 2026-09-17 用户定（**换掉了原来的 30% / 双神阈值**）：
#   · 狼数 = floor(总人数 × 34%)，至少 1 只
#   · **神职数 = 狼数**（按 GOD_ORDER 从前往后取，最多 5 个 —— 我们总共只有 5 个神）
#   · 其余全是民
#
# 这套就是**标准局比例**：12 人局 4 狼 4 神 4 民、9 人局 3 狼 3 神 3 民。
# floor(0.34n) 在 3 的倍数上精确还原标准配置，而且顺手消掉了旧规则那条断崖
# （旧规则狼数在 6→7 人处从 1 跳到 2：6 人局 1 狼是死局、7 人局立刻偏狼）。
WOLF_RATIO = 0.34
# 神职启用顺序（用户定）：女巫 → 预言家 → 猎人 → 守卫 → 白痴。
# ⚠️ 白痴排在最后 ⇒ **只有 15 人局（5 狼 5 神 5 民）才出场**。这是用户定的顺序，
#    不要为了让白痴早登场而擅自调它（想让 12 人局出白痴，得先改这个顺序）。
GOD_ORDER: List[Role] = [Role.WITCH, Role.SEER, Role.HUNTER, Role.GUARD, Role.IDIOT]
_GOD_SHORT = {Role.WITCH: "巫", Role.SEER: "预", Role.HUNTER: "猎",
              Role.GUARD: "守", Role.IDIOT: "白"}


def wolf_count(total: int) -> int:
    """狼数 = floor(总人数 × 34%)，至少 1 只（0 只狼没法玩）。"""
    return max(1, int(total * WOLF_RATIO))


def god_list(wolves: int) -> List[Role]:
    """本局的神职名单：按启用顺序取前 N 个（N = 狼数，最多 5 个）。"""
    return list(GOD_ORDER[:wolves])


def make_board(n_humans: int, n_ai: int) -> Board:
    """
    按人数现算一副板子 —— **这是现在的主线**，界面上不再选固定板子。

    校验放在这里而不是界面：界面上少一个 if，就等于少一条能绕过规则的路。
    """
    total = n_humans + n_ai
    # ⚠️ 允许 n_humans == 0：**全 AI 局**是离线评测要用的形态
    # （批七的批量跑胜率：没有人坐在桌边，也没有"人轮"）。
    # 界面不会走到这里 —— play.html 自己有"至少 1 个真人"的规则。
    # 注意这里管的是"人数别越界"，不是"这一局有没有人能玩"。
    if n_humans < 0:
        raise ValueError("真人数不能是负数")
    if total < 4:
        raise ValueError("总人数至少 4 人")

    wolves = wolf_count(total)
    gods = god_list(wolves)
    if wolves + len(gods) >= total:
        raise ValueError(
            f"{total} 人局按规则要 {wolves} 狼 + {len(gods)} 神，剩下的位置不够放村民")
    # 狼一开局就凑够 w >= g 的话，游戏还没开始就判狼胜
    if wolves * 2 >= total:
        raise ValueError(f"{total} 人局配 {wolves} 只狼，一开局就满足狼胜条件，人数不够")

    roles: List[Role] = [Role.WOLF] * wolves + gods
    roles += [Role.VILLAGER] * (total - len(roles))
    god_text = " ".join(f"1 {_GOD_SHORT[g]}" for g in gods)
    return Board(
        key=f"dyn{total}",
        label=f"{total} 人 · {wolves} 狼 {god_text} {total - wolves - len(gods)} 民",
        roles=roles,
        lives=1,
        note=f"{n_humans} 人类 + {n_ai} AI，按「狼 = 34%、神数 = 狼数」自动生成。",
    )


# ================================================================ 板子：只有现算这一条路
#
# ⚠️ 旧的"预设板子"（mini4 / bar4 / classic6）**已经删掉**（2026-09-17 迁移）。
#    删的理由不是"不好看"，而是：留着预设名就等于**留了一条绕过现算规则的旁路** ——
#    改配比（狼=34%、神=狼）之后，预设板子还是老配比，而它照样能开出来。
#
#    迁移时注意过一个坑：**"角色多重集相同"不等于"座位相同"**。
#    `make_board(3,3)` 与旧 classic6 的角色构成完全一样（2 狼 + 女巫 + 预言家 + 2 民），
#    但列表**顺序**不同（make_board 按 GOD_ORDER，女巫在前）⇒ 同一个 seed 洗出来的
#    座位分配也不同。所以凡是"按座位"写的断言都重新核过（见 _smoke-engine 里 L2 那几条）。
#
#    唯一的例外：**4 人局带预言家**现算不出来（4 人局的神职是女巫），
#    而测预言家行为需要它 ⇒ 那副板子搬到了测试专用的 `_testboards.SEER4`。


# ================================================================ 私有信息

@dataclass
class PrivateInfo:
    """L1 组内共享 + L2 个体私有。刻意与公共日志分开存放。"""
    team_mates: List[int] = field(default_factory=list)        # 仅狼
    investigations: Dict[int, bool] = field(default_factory=dict)  # 验人：seat -> 是否狼
    potions: Dict[str, bool] = field(default_factory=lambda: {"heal": True, "poison": True})
    # 守卫：上一晚守了谁。标准规则**不可连续两晚守同一人**，所以必须记住。
    # 只他本人可见（L2）。
    guard_last: Optional[int] = None
    # 白痴：翻过牌没有。翻过 ⇒ 不死，但**失去投票权**（标准判例）。
    flipped: bool = False


# ================================================================ 引擎

class Game:
    """一局狼人杀。纯确定性，可完全在控制台里跑完。"""

    def __init__(self, board: Board, kinds: List[str], seed: Optional[int] = None,
                 names: Optional[Dict[int, str]] = None,
                 rules: Optional["Rules"] = None) -> None:
        if len(kinds) != len(board.roles):
            raise ValueError(f"人数不符：板子需要 {len(board.roles)} 人，收到 {len(kinds)} 人")

        self.board = board
        self.rules = rules or Rules()
        self.rng = random.Random(seed)
        self.phase = Phase.SETUP
        self.round = 0
        self.winner: Optional[str] = None          # "wolf" | "good"
        self.public_log: List[str] = []
        # 内部备注（**永不外发**）：给排查和将来的结构化日志用。
        # 为什么必须有它：狼队协商的分歧、"按谁的意见定案"这类话**绝不能进公共日志** ——
        # 那等于当众宣布"狼队刚才商量过、3 号的意见赢了"，3 号立刻就是狼。
        # 和 resolve_night 那条"平安夜不能暴露被刀者"是同一类泄漏。
        self.debug_log: List[str] = []
        self.private: Dict[int, PrivateInfo] = {}
        self.night: Dict[str, Any] = {}
        self.votes: Dict[int, int] = {}
        self.speaking_order: List[int] = []
        self.speak_cursor = 0
        # 挑战式发言顺序（Rules.speech_mode == "challenge"）的记账。
        # 三个闸门，缺一个都会变成"这一轮永远说不完"（见 _queue_challenge）：
        #   _discussion_slots 哪些**光标位**是讨论位（回应 / 再回应）——
        #                     讨论位上的发言**不再触发**新挑战（判据必须是"这一句"，
        #                     不是"这个人"：被点的人回应时反点对方，同样不能插队）
        #   _responded        已经作为「回应者」插过队的座位 —— 同一座位一轮只插一次
        #   _inserted         插队次数，硬上限 = 存活人数
        self._discussion_slots: set = set()
        self._responded: set = set()
        self._inserted = 0
        # 「这一句是**回应**，不是自由发言」：按**光标位置**存，不按座位存 ——
        # 一轮里同一个座位会说两次（回应 + 被回应后的再回应），按座位存会把两句糊在一起。
        # 它只给这个人自己看（进他的视图），不进公共播报。
        self._slot_reason: Dict[int, str] = {}
        # 投票顺序：与发言同一套轮转（用户定的规则：都从前一晚被刀的人开始顺时针）
        self.vote_order: List[int] = []
        # 上一轮**最后一个发言的人**。只用于一件事：平安夜的中性起点
        # （见 _neutral_anchor —— 平安夜不能拿"被刀的人"当起点，那会泄漏夜里发生了什么）。
        # 在 open_vote() 里落定：那一刻本轮发言才真正结束。
        self.last_speaker: Optional[int] = None
        # 猎人开枪：谁还没开枪（None = 没人等着开枪）。shoot_from 记住"这一枪从哪来"：
        # 夜间死讯之后 → 开完继续白天；投票出局之后 → 开完进下一夜。
        self.shooter: Optional[int] = None
        self.shoot_from: Optional[Phase] = None
        # 死因分层（G11）：[{"seat": 3, "cause": "kill"|"poison"|"vote"|"shoot"}]
        # 猎人判例（被毒能不能开枪）、六维指标、复盘战报都要用它 ——
        # 不能再靠"看他死没死"去猜死因。
        # ⚠️ 每条死因**必须带 round**（2026-09-17 补）：没有它就算不出"活了几轮"，
        #    六维里的存活维度、战报里的时间线全都要靠它。
        self.deaths: List[Dict[str, Any]] = []
        # 逐夜记录（见 resolve_night 末尾）：女巫/守卫/预言家的每次动作 + 结果。
        self.night_records: List[Dict[str, Any]] = []
        # 本轮无人出局的次数（平票、或者全员弃票）—— 节奏维度要用。
        self.tie_count: int = 0
        # 怀疑走向缓存（按 轮次 + 发言条数 作键）。
        # 为什么要缓存：get_view_for 会被轮询频繁调用（每个待办座位一次），
        # 而重新解析全部发言没必要 —— 但**必须带键**，否则发言变了还返回旧值。
        self._susp_cache: Dict[int, int] = {}
        self._susp_key: Optional[tuple] = None
        # 历史：让 AI 能回答「我上一轮说过什么」「我投给了谁」。
        # ⚠️ 存它们**不是**为了给 AI 一段对话历史（那会把不该记的也带进去），
        #    而是为了按 §4.5 的规矩，通过 get_view_for(自己的座位) 显式喂给它。
        self.vote_history: List[Dict[int, int]] = []     # 每轮结算后的票型快照
        self.speeches: List[Dict[str, Any]] = []         # {"round", "seat", "text"}

        roles = list(board.roles)
        self.rng.shuffle(roles)
        self.players: List[Player] = []
        for i, (role, kind) in enumerate(zip(roles, kinds)):
            name = (names or {}).get(i)
            self.players.append(Player(seat=i, kind=kind, role=role,
                                       lives=board.lives, name=name or f"{i + 1} 号"))

        # L1：狼队互知队友
        wolves = [p.seat for p in self.players if p.role.is_wolf]
        for p in self.players:
            self.private[p.seat] = PrivateInfo()
            if p.role.is_wolf:
                self.private[p.seat].team_mates = [w for w in wolves if w != p.seat]

    # ------------------------------------------------------------ 查询

    @property
    def alive(self) -> List[Player]:
        return [p for p in self.players if p.alive]

    def wolves_alive(self) -> List[Player]:
        return [p for p in self.alive if p.role.is_wolf]

    def good_alive(self) -> List[Player]:
        return [p for p in self.alive if not p.role.is_wolf]

    def player(self, seat: int) -> Player:
        return self.players[seat]

    def note(self, text: str) -> None:
        """
        内部备注：**只进 debug_log，永不进公共播报**。

        用于那些"说出来就泄漏"的事实（狼队协商的分歧与定案依据、将来的指标中间量）。
        想公开就用 log()，想留痕就用 note() —— 两个别混。
        """
        self.debug_log.append(text)

    def log(self, text: str) -> None:
        """只写公共信息。任何私有内容都不许进这里。"""
        self.public_log.append(text)

    def log_speech(self, seat: int, text: str,
                   card: Optional[Dict[str, Any]] = None) -> None:
        """
        记一句玩家发言。给 get_view_for 的「我说过什么」用（只回给他本人）。

        `card` 是局势卡（G4）：这一句发言的要点（怀疑谁/站边谁/自称什么）。
        ⚠️ 它**算公开信息**（是"他刚说的那句话的要点"，见 agents.sanitize_card 的四条过滤），
           所以会经 view["stances"] 给全桌 —— 这和"投票进行中看不到票"不冲突：
           它不是票，是他自己声明的立场。
        """
        rec: Dict[str, Any] = {"round": self.round, "seat": seat, "text": text}
        if card:
            rec["card"] = dict(card)
        self.speeches.append(rec)

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self.phase is not Phase.SETUP:
            raise RuntimeError("已经开始了")
        self.phase = Phase.NIGHT
        self.round = 1
        self.log(f"游戏开始，{len(self.players)} 人局，每人 {self.board.lives} 条命。")
        self._open_night()

    def _open_night(self) -> None:
        self.phase = Phase.NIGHT
        # witch_asked 是必需的，不是可选优化：
        # 女巫「用不用药」是她的选择，如果只用「heal/poison 是否已设」来判断夜晚结束，
        # 人类女巫会被驱动器整个跳过 —— 她永远拿不到手机。
        # guard / guard_asked 与 witch_asked 同理：守卫"守谁"是他的选择，
        # 不显式标记「问过了」，人类守卫会被整个跳过、夜晚也结束不了。
        # wolf_* 是**狼队组内（L1）**的协商状态，绝不能进公共播报：
        #   wolf_points  {座位: 目标}   每只狼此刻点的谁（-1 = 空刀提议，含自刀）
        #   wolf_confirm [座位]          已经点过"确定"的狼（改点会把自己从里面移除）
        #   wolf_order   [座位]          首次确认的时间序 —— 分歧时用**先落地的那个**
        #   kill_settled 最终刀口是否已定案（多狼局：全员确认才算定）
        self.night = {"kill": None, "investigate": None, "heal": None,
                      "poison": None, "witch_asked": False,
                      "guard": None, "guard_asked": False,
                      "wolf_points": {}, "wolf_confirm": [], "wolf_order": [],
                      "wolf_followed": [], "kill_settled": False, "wolf_steps": 0}
        self.log(f"第 {self.round} 夜，天黑请闭眼。")

    # ---- 夜间动作 ----

    def night_kill(self, actor: int, target: int) -> None:
        """
        狼人**点刀**（2026-09-17 改：从"一只狼定刀"变成"狼队组内协商"）。

        · target >= 0 → 点这个座位；**允许自刀**（target == actor，面杀本来就有）
        · target == -1 → 提议空刀
        · 改点会把自己从"已确认"里移除（改了就得重新确认）
        · **单狼局：点即定案** —— 保持旧行为，也让夜晚能直接往下走
        · 多狼局：等全员确认后由 _settle_wolf_kill() 定案

        为什么要有这套：以前只有一只狼能定刀，其余狼（含 AI 狼）连"队友点了谁"
        都不知道。白天投票时狼各投各的、一只跟大势一只咬好人 ⇒ 立刻暴露。
        夜里能互相看到按钮，等价于面杀时狼睁眼交换的那个眼神 —— **不是新增交流通道**。
        """
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if not p.role.is_wolf:
            raise PermissionError(f"{p.display} 不是狼人，不能刀人")
        if target != -1:
            self._require_alive(target)      # 允许自刀 ⇒ 不再排除 target == actor
        pts = self.night.setdefault("wolf_points", {})
        prev = pts.get(actor, None)
        changed = actor not in pts or prev != target
        pts[actor] = target
        conf = self.night.setdefault("wolf_confirm", [])
        order = self.night.setdefault("wolf_order", [])
        # ⚠️ **只有真的改了目标才撤销确认**（幂等）。
        # 老写法是"只要调用过就把确认删掉" —— 后果：玩家重按同一个座位、
        # 或者前端重发一次同样的请求，他就会莫名其妙失去确认 ⇒ 刀口永远定不下来
        # ⇒ 这一夜卡死（实测被测试机器人复现：两只人狼互相干等到 500 步）。
        if changed and actor in conf:
            conf.remove(actor)
        if actor not in order:
            order.append(actor)              # 「先手」= 第一个提议的人（不是第一个确认的）
        if changed:
            # 空转（点回同一个点）不算一步 —— 否则"步数上限兜底"会被重按刷爆，
            # 变成"玩家多按几下就把刀口强行定了"。
            self.night["wolf_steps"] = self.night.get("wolf_steps", 0) + 1
        wolves = [q.seat for q in self.alive if q.role.is_wolf]
        if len(wolves) <= 1:
            self.night["kill"] = target
            self.night["kill_settled"] = True
            return
        self._settle_wolf_kill()

    def wolf_agree_kill(self, target: int) -> None:
        """
        狼队**已达成一致**，一步把刀口定下来。

        为什么需要它：`night_kill` 现在是"协商的一步"（多狼局只记点子、要全员确认才定案），
        而测试机器人、演示脚本、以及"狼队已经商量好"的调用方需要的是一步到位。
        没有这个入口，每个调用点都得自己模拟"点 + 确定"两步 —— 那样测试会掩盖真实流程。

        它仍然校验目标活着（-1 = 空刀），并把所有活狼的点子都记成同一个（保持状态自洽）。
        """
        self._require_phase(Phase.NIGHT)
        if target != -1:
            self._require_alive(target)
        wolves = [q.seat for q in self.alive if q.role.is_wolf]
        pts = self.night.setdefault("wolf_points", {})
        conf = self.night.setdefault("wolf_confirm", [])
        for s in wolves:
            pts[s] = target
            if s not in conf:
                conf.append(s)
            order = self.night.setdefault("wolf_order", [])
            if s not in order:
                order.append(s)
        self.night["kill"] = target
        self.night["kill_settled"] = True

    def night_confirm(self, actor: int) -> None:
        """
        狼人**确认**自己当前的点（多狼局定案的最后一步）。

        首次确认时把座位压进 wolf_order —— 这就是"先落地"的顺序，
        分歧时用它决定刀口（用户定的规则：两个人狼分别点了不同好人，就刀第一个被投的）。
        """
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if not p.role.is_wolf:
            raise PermissionError(f"{p.display} 不是狼人，不能确认刀口")
        if self.night.get("kill_settled"):
            return
        if actor not in (self.night.get("wolf_points") or {}):
            raise RuntimeError("还没点人，不能确认")
        conf = self.night.setdefault("wolf_confirm", [])
        if actor not in conf:
            conf.append(actor)
        self.night["wolf_steps"] = self.night.get("wolf_steps", 0) + 1
        self._settle_wolf_kill()

    def _settle_wolf_kill(self) -> None:
        """
        全员确认 ⇒ 定案。规则（用户定，照面杀）：
          · 点子一致 → 就刀那个
          · 有分歧   → 刀**第一个落地的人**点的那个（时间序，不是座位号）
        """
        wolves = [q.seat for q in self.alive if q.role.is_wolf]
        conf = self.night.get("wolf_confirm") or []
        if not wolves or not all(s in conf for s in wolves):
            return
        pts = self.night.get("wolf_points") or {}
        targets = [pts.get(s) for s in wolves]
        if len(set(targets)) == 1:
            final = targets[0]
        else:
            # 分歧时的优先级（用户定，照面杀）：
            #   ① **人狼们自己一致** → 听人狼的（"平票就刀人狼票的人"）
            #   ② 否则 → 听**第一个提议**的那个（"两个人狼分别票两个好人，就刀第一个被投的"）
            humans = [q.seat for q in self.alive if q.role.is_wolf and q.kind == "human"]
            human_targets = {pts.get(s) for s in humans}
            if humans and len(human_targets) == 1:
                final = human_targets.pop()
                # ⚠️ note 而不是 log：这句话进公共播报就等于宣布"狼队商量过、
                #    人狼的意见赢了" ⇒ 人狼立刻暴露。它只该出现在内部备注里。
                self.note("狼队刀口有分歧，按人狼那一票定。")
            else:
                first = next((s for s in (self.night.get("wolf_order") or []) if s in wolves),
                             wolves[0])
                final = pts.get(first)
                self.note(f"狼队刀口有分歧，按先提议的 {first + 1} 号的意见。")
        self.night["kill"] = -1 if final is None else final
        self.night["kill_settled"] = True

    def wolf_force_settle(self) -> None:
        """
        兜底：协商步数超上限时强行定案（取先落地的意见）。

        纯 AI 狼队理论上会自己在有限步内收敛（跟随 = 点队友的点），
        但"固执"的人格将来会让它反复改主意 —— 必须有这条兜底，
        否则一夜能无限来回，表现就是"卡在夜里不动"。
        """
        pts = self.night.get("wolf_points") or {}
        order = self.night.get("wolf_order") or []
        wolves = [q.seat for q in self.alive if q.role.is_wolf]
        first = next((s for s in order if s in wolves), None)
        if first is None:
            first = next((s for s in pts if s in wolves), None)
        if first is None:
            self.night["kill"] = -1
        else:
            self.note(f"狼队协商超时，按先落地的 {first + 1} 号的意见。")
            self.night["kill"] = pts.get(first)
        self.night["kill_settled"] = True

    def night_investigate(self, actor: int, target: int) -> Optional[bool]:
        """预言家验人。返回结果，同时写入自己的 L2 私有记录。"""
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if p.role is not Role.SEER:
            raise PermissionError(f"{p.display} 不是预言家")
        self._require_alive(target)
        result = self.player(target).role.is_wolf
        self.night["investigate"] = target
        self.private[actor].investigations[target] = result
        return result

    def night_skip_investigate(self, actor: int) -> None:
        """预言家今晚不验（2026-10-06 倒计时超时的默认结果，用户定：神职只给 15 秒）。
        investigate=-1 只是「结束掉预言家这一分支」的标记：不进任何人的记忆，
        天亮汇总里显示为没验。引擎原有 actions_for 条件（investigate is None）
        对 -1 不再成立，夜晚可以正常往下走。"""
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if p.role is not Role.SEER:
            raise PermissionError(f"{p.display} 不是预言家")
        if self.night.get("investigate") is not None:
            return
        self.night["investigate"] = -1

    def night_heal(self, actor: int, target: int) -> None:
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if p.role is not Role.WITCH:
            raise PermissionError(f"{p.display} 不是女巫")
        # 解药只能救**今晚被狼刀的那个人**。不校验的后果：点错人 ⇒ 解药被消耗、
        # 被刀的人照样死（resolve_night 是拿 heal 和 kill 比对的），也就是白耗一瓶。
        killed = self.knife_target()
        if killed is None:
            # 🔴 2026-10-07：空刀也要走到这一屏（界面照常、**解药按钮置灰**），
            #    所以拒绝文案要说清"没人被刀"，不能再说"狼还没刀人"。
            #    这条是**第二道防线**：界面置灰不是唯一防线（伪造请求也救不了人）。
            if self.night.get("kill_settled"):
                raise ValueError("今晚无人被刀，解药无处可用")
            raise RuntimeError("狼人还没刀人，女巫不能睁眼")
        if target != killed:
            raise ValueError("解药只能救今晚被刀的人")
        if not self.private[actor].potions["heal"]:
            raise PermissionError("解药已经用过了")
        self.night["heal"] = target
        # 🔴 2026-10-07（用户实机："你要使用毒药吗？面板没出现"）：
        #    `witch_asked` 是"女巫这一夜问完了吗"的开关（夜晚结束要用它），
        #    但它**同时**被当成"还能不能问毒药"的判据 ⇒ 用了/放弃了**解药**，
        #    就顺带把**毒药那一问**也掐死了（`_actions_available` 不再给她 poison）。
        #    用户早就定了"要它照常走"（这话写在 `night_driver._inject_default` 那段注释里）。
        #    所以：**毒药还在手上就先别关这一夜** —— `witch_asked` 留到毒药窗口收口
        #    （驱动器到点注入 pass）或毒药真被用掉时才置位。
        # ⚠️ 2026-10-07 更正：无条件置位（夜能不能收尾看它）—— 同 `night_decline`。
        self.night["witch_asked"] = True
        self.private[actor].potions["heal"] = False

    def night_poison(self, actor: int, target: int) -> None:
        """
        女巫用毒。

        这个方法是补上的：此前 `_actions_available` 会向界面宣告「女巫可以毒人」，
        但引擎里根本没有对应的动作，人类女巫一旦点毒药就会拿到
        `未知动作：poison` —— 是一个真的会让人玩不下去的漏洞。
        """
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if p.role is not Role.WITCH:
            raise PermissionError(f"{p.display} 不是女巫")
        # 🔴 2026-10-07：判据同 night_decline —— "刀已定案"就允许（空刀也算定案）。
        #    空刀只是**解药无处可用**，毒药照常能下（定稿：那一问照走）。
        if not self.night.get("kill_settled") and self.knife_target() is None:
            raise RuntimeError("狼人还没刀人，女巫不能睁眼")
        if not self.private[actor].potions["poison"]:
            raise PermissionError("毒药已经用过了")
        self._require_alive(target)
        self.night["poison"] = target
        self.night["witch_asked"] = True
        self.private[actor].potions["poison"] = False

    def night_decline(self, actor: int) -> None:
        """女巫决定本轮不用药。必须是显式动作，否则夜晚永远结束不了。"""
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if p.role is not Role.WITCH:
            raise PermissionError(f"{p.display} 不是女巫")
        # 同样要等狼刀定了才允许表态 —— 否则「提前放弃」会把 witch_asked 写死，
        # 这一夜再也不会问她（见 _actions_available 里那段）。
        # 🔴 2026-10-07：判据改成"刀已定案"（空刀也是定案）—— 否则空刀那一夜
        #   女巫既不能用药也不能放弃，witch_asked 永远置不上，夜晚结束不了。
        if not self.night.get("kill_settled") and self.knife_target() is None:
            raise RuntimeError("狼人还没刀人，女巫不能睁眼")
        # ⚠️ 2026-10-07 更正：无条件置位（夜能不能收尾看它）。
        #    "用了/不用解药，毒药那一问都要照常走"（用户定）的正确改法是**节点感知**
        #    （毒药窗口的收口由驱动器以 `stage="poison"` 调用），前两版都把夜搞卡了
        #    （实测），已回退 —— 见 §10.7 第九起。
        self.night["witch_asked"] = True

    def night_clear(self, actor: int) -> None:
        """撤回**本窗口**刚做的那个动作（2026-10-07 定稿交互：轮盘"再点同一个号码 = 取消"）。

        定稿逐字（守卫段，其余同构）："轮盘按某人号码即视为守护，不需要再点击其他按钮，
        再次点击原选中号码可取消，点击其他号码也可切过去"。

        撤回的是**还没被结算的临时决定**，所以：
          · 守卫 → guard 清空、guard_asked 归假（窗口内还能再选；不撤就等于空守）
          · 狼   → 撤我的点 + 撤我的"已确认"，kill_settled 归假（再点即重新定刀）
          · 预言家 → investigate 清空（可再验）。⚠️ **已经看过的那一眼收不回来**：
            私有记忆 `investigations` 保留 —— 面杀也一样，你睁眼看到了就是看到了。
          · 女巫 → 药**还回来**（撤回≠用掉），witch_asked 归假
          · 投票（VOTE 相位）→ 撤掉我那一票
        没得撤时抛 PermissionError（前端只在"刚点过的那个号码"上发它）。
        """
        if self.phase is Phase.VOTE:
            if actor not in self.votes:
                raise PermissionError(f"{self.player(actor).display} 这一轮还没投票")
            self.votes.pop(actor, None)
            return
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        n = self.night
        if p.role is Role.GUARD:
            n["guard"] = None
            n["guard_asked"] = False
            return
        if p.role.is_wolf:
            pts = n.get("wolf_points") or {}
            pts.pop(actor, None)
            n["wolf_points"] = pts
            n["wolf_confirm"] = [s for s in (n.get("wolf_confirm") or []) if s != actor]
            n["kill"] = None
            n["kill_settled"] = False
            return
        if p.role is Role.SEER:
            n["investigate"] = None
            return
        if p.role is Role.WITCH:
            if n.get("heal") is not None:
                n["heal"] = None
                self.private[actor].potions["heal"] = True
            if n.get("poison") is not None:
                n["poison"] = None
                self.private[actor].potions["poison"] = True
            n["witch_asked"] = False
            return
        raise PermissionError(f"{p.display} 没有可撤回的夜间动作")

    def night_guard(self, actor: int, target: int) -> None:
        """
        守卫守人（2026-09-17 新增，标准判例照抄）。

        · **可以守自己**（多数版本允许，见 Rules.guard_can_self_guard）——所以不排除 self
        · **不可连续两晚守同一人**（Rules.guard_can_repeat 默认 False），
          所以必须记住 private[seat].guard_last
        · **同守同救 = 该人仍死**（"奶穿"）——在 resolve_night 里结算，不在这里
        · 守卫当晚自己死掉，他这一晚的守护**仍然生效**：守护记在 night["guard"] 上、
          结算时才用，与人死活无关
        """
        self._require_phase(Phase.NIGHT)
        p = self.player(actor)
        if p.role is not Role.GUARD:
            raise PermissionError(f"{p.display} 不是守卫，不能守人")
        if target == -1:
            # 空守（2026-10-06 用户定：神职操作只给 15 秒，超时=自动放弃 ——
            # 守卫的"放弃"就是不守任何人。只置 guard_asked 让夜晚能结束。
            self.night["guard"] = None
            self.night["guard_asked"] = True
            return
        self._require_alive(target)
        if target == actor and not self.rules.guard_can_self_guard:
            raise ValueError("本局规则不允许守卫守自己")
        if not self.rules.guard_can_repeat and self.private[actor].guard_last == target:
            raise ValueError("不能连续两晚守同一个人")
        self.night["guard"] = target
        self.night["guard_asked"] = True
        self.private[actor].guard_last = target

    def night_shoot(self, actor: int, target: int) -> None:
        """
        猎人开枪（2026-09-17 新增）。target = -1 表示放弃开枪。

        两个关键点：
          · 只能在 Phase.SHOOT 里开，且必须是本局记下的那个 shooter —— 别的座位
            在别的时候调用一律拒绝（"谁能开枪"是规则，不能靠界面自觉）。
          · **开完枪才判胜负**。否则会出现"已经宣布狼人胜利了，猎人手里还拿着枪"。
            所以这里 damage → 记死因 → 再 check_winner。
        """
        if self.phase is not Phase.SHOOT:
            raise RuntimeError(f"当前阶段是 {self.phase.value}，不允许开枪")
        if self.shooter != actor:
            raise PermissionError(f"{self.player(actor).display} 现在不能开枪")
        if target != -1:
            self._require_alive(target)
            self._damage(target, 1)
            self.deaths.append({"seat": target, "round": self.round, "cause": "shoot"})
            tgt = self.player(target)
            self.log(f"{tgt.display} 被猎人带走，出局。" if not tgt.alive
                     else f"{tgt.display} 被猎人带走，还剩 1 条命。")
        else:
            self.log(f"{self.player(actor).display}（猎人）选择不开枪。")
        back_to = self.shoot_from or Phase.DAWN
        self.shooter = None
        self.shoot_from = None
        self.phase = back_to
        self.winner = self.check_winner()
        if self.winner:
            self.phase = Phase.ENDED
            self.log("狼人胜利。" if self.winner == "wolf" else "好人胜利。")

    # ---- 夜晚结算 ----

    def resolve_night(self) -> List[str]:
        """
        结算夜晚。返回本夜公开播报。

        结束相位是 DAWN，**除非有猎人要开枪** —— 那时进 SHOOT（开完枪自动回 DAWN）。
        这一处是 D6 的一半：判胜必须等开枪之后（见 night_shoot / resolve_vote）。
        """
        self._require_phase(Phase.NIGHT)
        events: List[str] = []

        target = self.knife_target()          # 空刀(-1) ⇒ None（不许流进 _damage）
        guarded = self.night.get("guard")
        by_heal = target is not None and self.night.get("heal") == target
        by_guard = target is not None and guarded == target
        # ★ 同守同救 = 该人仍死（"奶穿"，标准判例）：
        #   解药与守护同时命中同一人时互相抵消 —— 这不是 bug，是规则。
        pierced = by_heal and by_guard
        saved = (by_heal or by_guard) and not pierced

        # 🔴 2026-10-07 第七起（用户定）：**毒杀与刀杀同一个说法**，而且
        #    "**只要有人死就不是平安夜**"。旧实现是"先按狼刀播报（没人死就说平安夜）,
        #    再把毒杀单独写一句『另外，X 被毒杀出局』" ⇒ 两处错：
        #      ① 公共播报里**点出了死因**（毒/刀），这是夜里动作细节；
        #      ② 狼空刀 + 女巫毒死人时，会先喊"平安夜。"再喊有人出局 —— 自相矛盾。
        #    现在：两种死因先各自结算成"死人"，**最后统一播报**（去掉死因、去掉"另外"）。
        # ⚠️ 空刀 / 被女巫救 / 被守卫守住仍然**一字不差**（三种成因都只体现为"没人死"）
        #    —— 分开写会让全桌反推出"狼今晚到底刀了没有"（被刀的人也不该知道自己被刀过）。
        #    起点规则见 `_neutral_anchor`。
        events = []
        _died: List[str] = []          # 今夜**出局**的人（按死因结算顺序：先刀后毒）
        _hurt: List[str] = []          # 掉了一条命但还没出局（多命局才有）
        if target is None or saved:
            pass                       # 刀没落定（空刀 / 被救 / 被守）—— 一个字都不说
        else:
            self._damage(target, 1)
            self.deaths.append({"seat": target, "round": self.round, "cause": "kill"})
            (_died if self.player(target).alive is False else _hurt).append(
                self.player(target).display)

        # 女巫用毒：与狼刀独立结算。注意顺序 —— 被救活的人仍可能被毒死，
        # 这是标准规则，也避免"一个动作覆盖另一个动作"。
        poison_target = self.night.get("poison")
        if poison_target is not None and poison_target != -1:
            self._damage(poison_target, 1)
            self.deaths.append({"seat": poison_target, "round": self.round, "cause": "poison"})
            (_died if self.player(poison_target).alive is False else _hurt).append(
                self.player(poison_target).display)

        if _died:
            events.extend(f"昨夜 {n} 出局。" for n in _died)
        elif _hurt:
            events.extend(f"昨夜 {n} 倒牌，还剩 1 条命。" for n in _hurt)
        else:
            events.append("平安夜。")
        for e in events:
            self.log(e)

        # 逐夜记录（六维指标 / 战报 / 复盘都要它）：
        # 只记"这一夜到底发生了什么"，不带任何身份 → 它不参与隔离，也不进公共播报。
        # 有了它才算得出「女巫救得对不对」「守卫挡了几刀」「预言家验了几个人」，
        # 否则这些只能靠 public_log 反推（那是解析日志的脆弱办法）。
        self.night_records.append({
            "round": self.round,
            "kill": target,
            "heal": self.night.get("heal"),
            "poison": self.night.get("poison"),
            "guard": guarded,
            "investigate": self.night.get("investigate"),
            "saved_by_heal": bool(by_heal and not by_guard and target is not None),
            "saved_by_guard": bool(by_guard and not by_heal and target is not None),
            "pierced": bool(pierced),        # 同守同救 = 奶穿（仍死）
            "peaceful": bool(target is None or saved),
        })

        # 猎人判例：今夜死掉的人里如果有猎人，他要开枪 —— 但**被毒死默认不能开枪**
        # （网杀标准，Rules.hunter_can_shoot_on_witch_poison 可改）。
        self._open_shoot_if_hunter(back_to=Phase.DAWN)

        # ★ 判胜（2026-09-17 修）：**夜里也会分出胜负，以前这里不判**。
        #   两个后果都是真 bug：
        #     · 最后一狼被女巫毒死 → 游戏不结束，好人还白票一整天
        #     · 狼夜里杀到 w >= g → 也不结束，好人还能在"已经输了"的局面里票一个人
        #   现在：有人要开枪就先进 SHOOT（判胜交给 night_shoot），否则当场判。
        if self.shooter is not None:
            self.phase = Phase.SHOOT
            return events
        self.winner = self.check_winner()
        if self.winner:
            self.phase = Phase.ENDED
            self.log("狼人胜利。" if self.winner == "wolf" else "好人胜利。")
            return events
        self.phase = Phase.DAWN
        return events

    def _open_shoot_if_hunter(self, back_to: 'Phase' = None) -> None:
        """检查"今晚有没有猎人出局、该不该开枪"，该开就把 shooter 记上。

        back_to = 开完枪回到哪个相位（night_shoot 的 shoot_from）：
          · 夜里死的（resolve_night 调）→ DAWN（开完继续白天）
          · 被投出的（resolve_vote 调）→ EXILE（开完进下一夜 —— 338 行注释的
            语义；🔴 2026-10-05 实局：原来硬编码 DAWN，被投出的猎人开完枪会
            错误地再来一轮白天）。由调用方声明。"""
        # 🔴 2026-10-02：必须**只看本轮**的 deaths —— 以前遍历全部历史，
        #    死掉的猎人被票出/被刀开过一次枪之后，每一晚的结算都会把他
    #    重新任命成 shooter ⇒ 死人反复开枪（实测单测复现）。
        for d in self.deaths:
            if int(d.get("round", -1)) != self.round:
                continue
            seat = int(d["seat"])
            if not self.player(seat).alive and self.player(seat).role is Role.HUNTER:
                if d["cause"] == "poison" and not self.rules.hunter_can_shoot_on_witch_poison:
                    self.log(f"{self.player(seat).display}（猎人）被毒杀，无法开枪。")
                    continue
                if self.shooter is None:
                    self.shooter = seat
                    self.shoot_from = back_to or Phase.DAWN

    def _damage(self, seat: int, amount: int) -> None:
        p = self.player(seat)
        p.lives -= amount
        if p.lives <= 0:
            p.lives = 0
            p.alive = False

    # ---- 白天 ----

    def _neutral_anchor(self) -> int:
        """
        平安夜专用的**中性起点**：上一轮最后发言者的下一位。

        为什么平安夜不能用"被刀的人"当起点：起点会写进公共播报
        （旧文案：「天亮了，从昨晚被刀的 3 号开始依次发言。」），
        而 public_log 是**公开**的、还会进 AI 的 prompt（agents.py 会逐条包进
        <主持人> 标签）⇒ 全桌都能反推出「3 号是目标、而且有人救了他」。
        于是三件本该保密的事一起漏了：**狼有没有刀人 / 守卫守了谁 / 女巫救了谁**。

        取"上一轮最后发言者的下一位"而不是固定 1 号：
        固定 1 号的话，「每晚都从 1 号开始」本身就成了一个可观测的异常（= 平安夜）。
        这个起点只由**公开的上一轮顺序**决定，所以对"昨晚发生了什么"零信息量。
        """
        last = self.last_speaker
        if last is None:
            return 0                      # 第 1 晚就平安：没有上一轮，从 1 号开始
        return (last + 1) % len(self.players)

    def _night_anchor(self) -> int:
        """
        本夜轮转该用谁当起点 —— **发言和投票必须用同一个**（否则投票顺序会把
        发言顺序藏起来的那件事又说一遍）。

        · 被刀的人**确实出局了** → 用他。他的死本来就是公开播报，
          起点落在他身上不带任何新信息（也和真人面杀的"从他起往下数"一致）。
        · 其它一切情况（空刀 / 被女巫救 / 被守卫守住）→ **中性起点**。
          这三者在外部表现上必须**完全一致**，否则玩家和 AI 都能反推。
        """
        kill = self.night.get("kill")
        if kill is not None and kill >= 0 and not self.player(kill).alive:
            return kill
        return self._neutral_anchor()

    def _rotation_from(self, anchor: Optional[int]) -> List[int]:
        """
        存活座位按「从 anchor 开始顺时针」排。

        anchor 由 `_night_anchor()` 给（发言与投票共用）。三种情况都要兜住：
          · anchor 还活着   → 就从他自己开始
          · anchor 已经出局 → 从**他顺时针方向的第一个活人**开始
          · anchor 是 None/-1 → 从 1 号开始（只有测试或异常路径会传这个）
        """
        seats = [p.seat for p in self.alive]
        if not seats:
            return []
        if anchor is None or anchor < 0:
            idx = 0
        else:
            later = [s for s in seats if s >= anchor]
            idx = seats.index(later[0]) if later else 0
        return seats[idx:] + seats[:idx]

    def open_discuss(self, start_seat: Optional[int] = None) -> List[int]:
        self._require_phase(Phase.DAWN)
        self.phase = Phase.DISCUSS
        anchor = self._night_anchor() if start_seat is None else start_seat
        seats = self._rotation_from(anchor)
        if not seats:
            raise RuntimeError("场上没有活人，无法开始发言")
        self.speaking_order = seats
        self.speak_cursor = 0
        self._discussion_slots = set()   # 新一轮 ⇒ 三个闸门清零（都是"这一轮"的范围）
        self._responded = set()
        self._inserted = 0
        self._slot_reason = {}
        # ⚠️ 播报只说**起点是谁**，不能说他「为什么」成为起点。
        #    以前写的是「从昨晚被刀的 3 号开始依次发言。」—— 在平安夜
        #    （被女巫救 / 被守卫守住 / 空刀）时，这一句就把"谁是目标、有人救了他"
        #    公开了（见 _neutral_anchor）。现在两种情况的文案完全一致，
        #    外部看不出区别。
        self.log(f"天亮了，请睁眼。从 {seats[0] + 1} 号开始依次发言。")
        return seats

    def current_speaker(self) -> Optional[int]:
        if self.phase is not Phase.DISCUSS:
            return None
        if self.speak_cursor >= len(self.speaking_order):
            return None
        return self.speaking_order[self.speak_cursor]

    def end_speech(self) -> Optional[int]:
        """
        当前发言人结束，返回下一位（None 表示发言完毕）。

        挑战式（Rules.speech_mode == "challenge"）下会在这里**插队**（见 _queue_challenge）。
        注意它不是"另一套发言顺序"：**轮转本身没动** —— 起点、方向、以及投票顺序
        （vote_order 走的是同一套轮转）全都和以前一样，插队只是把讨论位就地塞进队列。
        所以关掉这个开关 = 一字不差地回到旧行为。
        """
        spk = self.current_speaker()
        if spk is None:
            return None
        _slot = self.speak_cursor          # 这一句所在的**光标位**（讨论位判定要用它）
        self.speak_cursor += 1
        if self.rules.speech_mode == "challenge":
            rec = self.speeches[-1] if self.speeches else None
            self._queue_challenge(spk, rec, _slot)
        return self.current_speaker()

    def _queue_challenge(self, speaker: int, rec: Optional[Dict[str, Any]],
                         slot_index: int) -> bool:
        """
        「被点名的人先回应」——主持人（引擎）拿**刚刚这一句发言**做判定。

        规则（2026-09-18 用户定，照面杀）：
          · X 的话里对 Y（或 Y、Z）有负面指向 ⇒ 顺序变成
            […, X, **Y, Z, X**, 原轮转的下一位, …]
            （Y、Z 依次回应，然后 X 再回应，接着照常轮到 X 的下一位）
          · **只认第一次发言**：Y 回应时反驳 X 不算新一轮挑战
            （否则两人能互相点一辈子 —— 这是最关键的那道闸）
          · 同一个座位一轮只作为回应者插一次
          · 插队总数上限 = 存活人数（病态输入的兜底；正常局碰不到）
          · 只认**活着且不是他自己**的座位（死人不在讨论范围内）

        判定来源：**局势卡 > 关键词**（metrics.accuse_targets）。
        卡是发言人自己给的（AI 走这条），关键词是从文本里猜的（人类没有卡）。
        两者都只用**公开发言** ⇒ 这套机制不引入任何新信息通道（和 L0 派生同一档）。
        """
        if rec is None or int(rec.get("seat", -1)) != speaker:
            # 不变量：刚记下的那句必须是这个座位说的。不成立就**什么都不做** ——
            # 宁可少插一次队，也不能拿别人的话去给这个座位排顺序。
            return False
        if slot_index in self._discussion_slots:
            # 闸门①：**这一句本身就是讨论位**（被点的人回应 / 挑战者再回应）⇒ 不再插队。
            # 这条不能用"这个人是不是已经说过话"来判断 —— 我第一版就是这么写的，
            # 结果被点的人的回应照样能触发新一轮插队（实测 7 → 9 个字位）⇒ 两人互相点，
            # 这一轮永远说不完。判据必须是"**这一句**在不在讨论位里"，不是"这个人"。
            return False
        live = [p.seat for p in self.alive]
        if self._inserted >= len(live):
            return False               # 闸门②：插队总次数上限
        targets = accuse_targets(str(rec.get("text") or ""), live, speaker,
                                 card=rec.get("card"))
        responders = [t for t in targets if t not in self._responded]
        if not responders:
            return False
        self._responded.update(responders)   # 闸门③：同一座位一轮只插一次
        self._inserted += 1
        # 插在**当前光标处**：此刻 speak_cursor 已经指向"下一位该发言的人"，
        # 所以这一手等价于"下一位改成 Y（Z），之后再回到 X"。
        _start = self.speak_cursor
        self.speaking_order[_start:_start] = responders + [speaker]
        # 记下这段是**讨论位**：里面的每一句都不再触发新挑战（闸门①的判据）。
        self._discussion_slots.update(range(_start, _start + len(responders) + 1))
        _names = "、".join(f"{t + 1} 号" for t in responders)
        # 这句话进**公共播报**没问题：被点名这件事是当众说的，没有新信息。
        self.log(f"{speaker + 1} 号的话指向了 {_names}，{_names}先回应，"
                 f"之后 {speaker + 1} 号再回应。")
        # 给这次回应的两个人各一句"为什么轮到你"（只进他自己的视图，不进公共播报）
        for _i, _t in enumerate(responders):
            self._slot_reason[self.speak_cursor + _i] = (
                f"你被 {speaker + 1} 号点名了，现在回应。")
        self._slot_reason[self.speak_cursor + len(responders)] = (
            f"{_names}已经回应过了，现在轮到你再说一句。")
        return True

    def open_vote(self) -> None:
        if self.phase is not Phase.DISCUSS:
            raise RuntimeError("还没到投票阶段")
        self.phase = Phase.VOTE
        self.votes = {}
        # 本轮发言到此结束 ⇒ 记下"最后发言的人"，给下一个平安夜当中性起点用。
        self.last_speaker = self.speaking_order[-1] if self.speaking_order else None
        # 投票顺序与发言**必须同一套轮转**（用户定的规则）。
        # ⚠️ 这里不能直接拿 self.night["kill"] 当锚点：平安夜时那等于把发言顺序
        #    刚藏起来的事情又写进 vote_order（同一个泄漏，只是换了个字段）。
        #    所以两者都走 _night_anchor()。
        # 它决定「手机依次递给谁」，也决定 AI 的投票先后 —— 靠后的人要能听到前面的。
        self.vote_order = self._rotation_from(self._night_anchor())
        self.log("发言结束，开始投票。")

    def cast_vote(self, voter: int, target: int) -> None:
        """
        投票。target = -1 表示弃票。

        ⚠️ 2026-09-17 起**允许投自己**（用户定）：标准局里自投≈弃权的一种表达，
        引擎没必要拦。`tally_text` 与 `resolve_vote` 会照常把他算进那一组，
        和面杀里"举手投自己"的表现一致。
        """
        self._require_phase(Phase.VOTE)
        self._require_alive(voter)
        if target != -1:
            self._require_alive(target)
        self.votes[voter] = target

    def tally_text(self) -> str:
        """
        本轮票型的**可公布文本**。格式与 demo/index.html 的 `tallyText()` 完全一致：

            投给同一个人的归成一组 —— 组内座位号升序、用「，」分隔，形如 `1，2→3`；
            组与组用「；」分隔，票数多的组在前，同票时座位号小的在前。

        全是弃票（或还没人投）时返回「无人投票」—— 与 demo 的措辞也一致。
        只读 self.votes、不写任何状态，所以随时可以调用。
        """
        groups: Dict[int, List[int]] = {}
        for voter, target in self.votes.items():
            if target == -1:            # 弃票不进分组，口径与 resolve_vote 一致
                continue
            groups.setdefault(target, []).append(voter)
        if not groups:
            return "无人投票"
        order = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        return "；".join(
            "，".join(str(v + 1) for v in sorted(voters)) + "→" + str(target + 1)
            # 见下面 tally_speech 的说明：这个紧凑写法只给 AI / demo 用
            for target, voters in order
        )

    def knife_target(self) -> Optional[int]:
        """今晚真正被刀的座位；**空刀返回 None**。

        🔴 2026-10-05 真 bug：空刀存的是 -1，而 `resolve_night` 只用
           `kill is None` 判"没刀" ⇒ -1 一路流到 `self._damage(-1, 1)`，
           而 Python 的 -1 索引 = **最后一位玩家** —— 表现就是"狼空刀、
           女巫没毒，却有人死了"。
           凡是**读**今晚刀口的地方一律走这个方法，不要再直接拿
           `night["kill"]` 比 None（写的地方照旧：None / -1 / 座位号）。"""
        k = self.night.get("kill")
        if k is None or int(k) < 0:
            return None
        return int(k)

    def tally_speech(self) -> str:
        """
        票型的**口语版**，专供**语音播报**（2026-10-05 用户报："票型实时念出来，
        人类听着费力"）。

        `tally_text()` 那个紧凑写法（`1，2，3→4；5→2`）是给**眼睛**看的：念出来
        是一串数字+箭头，桌上一句都抓不住。播报要的是"谁得几票、谁投的、谁出去"：

            3 号 2 票（1 号、4 号投的）；5 号 1 票（2 号投的）；弃票 1 票（6 号）
            3 号票最多，被投出。

        只读 self.votes，不写状态。
        """
        groups: Dict[int, List[int]] = {}
        abstain: List[int] = []
        for voter, target in self.votes.items():
            if target == -1:
                abstain.append(voter)
                continue
            groups.setdefault(target, []).append(voter)
        if not groups and not abstain:
            return "无人投票"
        parts = []
        for target, voters in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            parts.append(f"{target + 1} 号 {len(voters)} 票（"
                         + "、".join(str(v + 1) + " 号" for v in sorted(voters)) + "投的）")
        if abstain:
            parts.append("弃票 " + str(len(abstain)) + " 票（"
                         + "、".join(str(v + 1) + " 号" for v in sorted(abstain)) + "）")
        # ⚠️ 全是弃票时 groups 为空 —— 那时没有"票最多"可说（这一句必须判空，
        #    否则 IndexError 直接把结算炸掉，实测就是这样红的）
        if groups:
            # 🔴 2026-10-12（用户：平票时先说"x号票最多"再说平票，这句是错的）：
            #    "票最多"只在**唯一最高票**时才有意义；平票（多人并列最高）不说。
            top = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0]
            top_n = len(top[1])
            tied = len([g for g in groups.values() if len(g) == top_n]) > 1
            out = "；".join(parts) + "。"
            return out if tied else out + f"{top[0] + 1} 号票最多。"
        return "；".join(parts) + "。全部弃票。"

    def resolve_vote(self) -> Optional[int]:
        """结算投票，返回被放逐的座位（None = 平票或全弃票）。"""
        self._require_phase(Phase.VOTE)
        tally: Dict[int, int] = {}
        for t in self.votes.values():
            if t == -1:
                continue
            tally[t] = tally.get(t, 0) + 1

        exiled: Optional[int] = None
        if tally:
            top = max(tally.values())
            leaders = [s for s, v in tally.items() if v == top]
            if len(leaders) == 1:
                exiled = leaders[0]
            elif self.rules.tie_vote_rule == "random":
                # 可配规则（G9）：平票随机出一个（默认是"无人出局"，见 Rules）
                exiled = self.rng.choice(sorted(leaders))

        # 存档这一轮的票型。用途有两个，都在"结算之后"：
        #   · AI 能从自己的视图里看到「我投给了谁」（get_view_for 的 my_votes）
        #   · 完整票型进 public_log，AI 下一轮能看到 —— **投票进行中看不到**
        #     （public_log 是这一行才写入的，进行中它里面根本没有本轮票型）
        self.vote_history.append(dict(self.votes))

        # 票型先播报，再播报结果 —— 顺序与 demo/index.html 一致
        # （「投票结果：…」在前，「x号玩家投票出局」在后）。
        # 它进的是 public_log，所以界面上的日志、时间线、以及语音播报都会自动带上，
        # 前端不需要再单独画一块「投票板」。
        # 播报用**口语版**（人类靠耳朵抓信息）；紧凑版留给 AI/界面（tally_text）
        self.log("投票结果：" + self.tally_speech())

        if exiled is None:
            # 平票 / 全员弃票都算"这一轮没出人"。节奏维度要看它 ——
            # 平票率高说明好人不敢投（或狼在故意打散），是个真实的平衡信号。
            self.tie_count += 1
            self.log("投票平票，本轮无人出局。")
        elif self.player(exiled).role is Role.IDIOT:
            # ★ 白痴判例（标准）：
            #   · 第一次被投出 → **翻牌不死**，但从此**失去投票权**
            #   · 已经翻过牌的白痴 → 不能再被投出（多数版本）
            me = self.private[exiled]
            if not me.flipped:
                me.flipped = True
                self.log(f"{self.player(exiled).display} 是白痴，翻牌不死，但从此不能投票。")
            else:
                self.log(f"{self.player(exiled).display} 已翻过牌，本轮不再出局。")
        else:
            self._damage(exiled, 1)
            self.deaths.append({"seat": exiled, "round": self.round, "cause": "vote"})
            p = self.player(exiled)
            if p.alive:
                self.log(f"{p.display} 被投出，还剩 1 条命。")
            else:
                self.log(f"{p.display} 被投出并出局。")

        # ★ D6：猎人被投出也要能开枪，而**判胜必须等这一枪之后**。
        #   以前这里立刻 check_winner() 并置 ENDED ⇒ 会出现"已经宣布狼人胜利，
        #   猎人手里还拿着枪"。现在改成：要开枪就先进 SHOOT，判胜交给 night_shoot。
        self.shooter = None
        self.shoot_from = None
        # 🔴 2026-10-05：back_to=EXILE —— 被投出的猎人开完枪**进下一夜**
        #    （原来硬编码 DAWN ⇒ 开完枪错误地再来一轮白天）。
        self._open_shoot_if_hunter(back_to=Phase.EXILE)
        if self.shooter is not None:
            self.phase = Phase.SHOOT
            return exiled

        self.phase = Phase.EXILE
        self.winner = self.check_winner()
        if self.winner:
            self.phase = Phase.ENDED
            self.log("狼人胜利。" if self.winner == "wolf" else "好人胜利。")
        return exiled

    def can_vote(self, seat: int) -> bool:
        """
        这个人现在还有没有投票权。

        白痴翻牌后**失去投票权**（标准判例）。它必须是一个显式判据，因为归票的判据是
        "所有活人都投过了"：翻牌的白痴永远投不了 —— 照旧写法（活人都在 votes 里）
        会**永远等不到他** ⇒ 归票死锁（D5）。所以 _all_voted / _actions_available /
        前端三处都必须用它，不能再用 alive 代替。
        """
        p = self.player(seat)
        if not p.alive:
            return False
        if p.role is Role.IDIOT and self.private[seat].flipped:
            return False
        return True

    def next_night(self) -> None:
        if self.phase is Phase.ENDED:
            raise RuntimeError("已经结束了")
        if self.phase is Phase.SHOOT:
            # 🔴 2026-10-05 实局（用户报：死猎人第二晚开枪打死预言家）：
            #    调用方在 SHOOT 相位直接 next_night ⇒ shooter 残留 ⇒ 下一晚结算时
            #    resolve_night 见 shooter 非空又进 SHOOT ⇒ **死人开枪**。
            #    这里明确拒绝：开枪必须先落定（开或弃），谁跳谁炸 —— 不许静默。
            raise RuntimeError("猎人还没开枪（SHOOT 相位），不能进下一夜")
        self.round += 1
        self._open_night()

    # ------------------------------------------------------------ 胜负

    def check_winner(self) -> Optional[str]:
        w = len(self.wolves_alive())
        g = len(self.good_alive())
        if w == 0:
            return "good"
        if w >= g:
            return "wolf"
        return None

    # ------------------------------------------------------------ 视图

    def stances_latest(self) -> Dict[int, Dict[str, Any]]:
        """
        每个座位**最近一次**声明的立场（局势卡，G4，公开信息）。

        取最近一次而不是全部：立场是会变的（昨天怀疑 2 号、今天站边 2 号），
        桌上的人记住的也是"他现在怎么想"。
        只算**还活着**的座位：死人最后一次声明不该继续影响判断。

        为什么它算公开信息：卡片内容是"他刚说的那句话的要点"，由他自己给出，
        并且过了 agents.sanitize_card 的四道过滤（踩泄露词、指死人、自称狼都会丢）。
        """
        live = {p.seat for p in self.alive}
        out: Dict[int, Dict[str, Any]] = {}
        for rec in self.speeches:
            s = int(rec.get("seat", -1))
            if s in live and rec.get("card"):
                out[s] = dict(rec["card"])       # 后面的覆盖前面的 ⇒ 留下最近一次
        return out

    def board_fact(self) -> str:
        """本局配置的一句话事实（公开信息）：总人数 / 狼数 / 各神职有无 / 村民数。

        用户 2026-10-02 报：4 人局（无守卫）的 AI 说「可能有守卫」——
        此前模型只能靠猜配置。面杀里发牌前大家都会确认板子，所以这是公开层。"""
        n = len(self.players)
        wolves = sum(1 for p in self.players if p.role.is_wolf)
        gods, villagers = [], 0
        for p in self.players:
            r = p.role
            if r.is_wolf:
                continue
            if r is Role.VILLAGER:
                villagers += 1
            else:
                gods.append(r.label)
        god_txt = "、".join(sorted(gods)) if gods else "无"
        return (f"共 {n} 人 = 狼人 {wolves} 名；神职：{god_txt}；村民 {villagers} 名。"
                f"没有列出的神职本局不存在（例如没写守卫就没有守卫，别猜）。")

    def vote_patterns(self) -> List[Dict[str, Any]]:
        """历次白天投票的**票型**（带天标注），给 AI 复盘用（2026-10-02 用户加）。

        数据来自 `vote_history` —— 它在**结算时**才 append（`open_vote` 的收尾），
        所以「正在投的今天」天然不在里面：白天投票中谁投了谁是暗的，
        纪律由数据结构保证（不需要任何 if）。
        返回 [{"round": 天数(1起), "votes": {投票人0基: 目标0基}}]，目标 -1 = 弃票。
        """
        return [{"round": i + 1, "votes": dict(h)}
                for i, h in enumerate(self.vote_history)]

    def speech_progress(self) -> Dict[str, Any]:
        """今天白天的发言进度（公开信息）：顺序、谁已说、此刻轮到谁。

        用户 2026-10-02 实局报：4 号（狼）说「1 号刚才的逻辑我没太跟上，想再
        听听」—— 而 1 号**根本还没发言**（顺序 3→4→1）。模型看不到进度，
        就会把「还没发生的话」当成「刚才的话」来评。发言顺序与进度全桌可见
        （面杀里谁说过谁没说一目了然）⇒ 放公开层。
        非讨论阶段返回 {}（夜晚/投票没有这个概念）。"""
        if self.phase is not Phase.DISCUSS:
            return {}
        spoken = sorted({int(s["seat"]) for s in self.speeches
                         if int(s.get("round", 0)) == self.round})
        return {"order": list(self.speaking_order), "spoken": spoken,
                "current": self.current_speaker()}

    def stances_all(self) -> List[Dict[str, Any]]:
        """全部历史局势卡，**带轮次**（2026-10-02 用户定：无限叠加）。

        与 stances_latest 的区别：那边只留每人最近一张（给界面的「现状」），
        这边每张卡连同它出现的**轮次**一起交出去 —— 模型能看出「这是几轮前
        的旧立场」，陈旧性可见（另一窗口实测的 bug：第 1 轮的旧卡到第 4 轮
        还被当成当前立场）。同一座位同一轮发言多次时，只留该轮最后一张。
        只算活人（口径与 stances_latest 一致）。
        """
        live = {p.seat for p in self.alive}
        out: List[Dict[str, Any]] = []
        seen: Dict[tuple, int] = {}
        for rec in self.speeches:
            s = int(rec.get("seat", -1))
            if s not in live or not rec.get("card"):
                continue
            r = int(rec.get("round", 0))
            card = dict(rec["card"])
            card["round"] = r
            key = (r, s)
            if key in seen:
                out[seen[key]] = {"seat": s, "round": r, "card": card}
            else:
                seen[key] = len(out)
                out.append({"seat": s, "round": r, "card": card})
        return out

    def suspicion_this_round(self) -> Dict[int, int]:
        """
        「每个座位被点名质疑了几次」——**只从公开发言算**，所以人人相同。

        取的是**最近一轮有发言的轮次**，不是 self.round：
        夜里（round 已经加过 1）狼队要看的正是白天刚说过的话，
        按 self.round 过滤会得到空集 —— 那这个功能在夜里就废了（而夜里正是狼最需要它的时候）。

        缓存键 = (那一轮, 发言总条数)：又有人说话了就重算。
        ⚠️ 不能用 id(self.speeches) 当键：列表是原地追加的，id 不变 ⇒ 缓存永远不失效。
        """
        last_round = self.speeches[-1].get("round") if self.speeches else None
        key = (last_round, len(self.speeches))
        if self._susp_key != key:
            recent = [s for s in self.speeches if s.get("round") == last_round]
            self._susp_cache = suspicion_counts(recent, [p.seat for p in self.alive])
            self._susp_key = key
        return dict(self._susp_cache)

    def get_view_for(self, seat: int) -> Dict[str, Any]:
        """
        返回该座位此刻应当看到的一切。这是单机 / 多机共用的唯一入口。

        刻意不是 render()：多机时每台手机各调一次 get_view_for(自己的座位号)，
        引擎一行都不用改（见《多机vs单机》§6）。
        """
        p = self.player(seat)
        priv = self.private.get(seat, PrivateInfo())

        view: Dict[str, Any] = {
            "seat": seat,
            "display": p.display,
            "role": p.role.value,
            "role_label": p.role.label,
            "lives": p.lives,
            "alive": p.alive,
            "phase": self.phase.value,
            "round": self.round,
            "board": self.board.label,
            "public_log": list(self.public_log),
            "players": [
                {"seat": q.seat, "display": q.display, "lives": q.lives, "alive": q.alive,
                 "kind": q.kind,
                 # 白痴翻牌是**公开事件**（桌上看得见），所以这里可以给所有人；
                 # 前端据此把"投票"按钮去掉。
                 "flipped": self.private[q.seat].flipped}
                for q in self.players
            ],
            "can_act": self._actions_available(seat),
            # 规则开关（G9）也是**公开信息**：全桌都知道本局按什么玩法打。
            # 🔴 2026-10-02 补：之前视图不带规则，而打法文本/TASKS 里写死了
            # 「守卫不能连守两晚」—— 用户把「守卫可以连守同一人」打开后，
            # AI 照旧以为不行（用户实测指出）。现在把开关原样带给视图，
            # prompt 里那一行跟着开关走。
            "rules": {
                "hunter_can_shoot_on_witch_poison":
                    bool(self.rules.hunter_can_shoot_on_witch_poison),
                "guard_can_self_guard": bool(self.rules.guard_can_self_guard),
                "guard_can_repeat": bool(self.rules.guard_can_repeat),
                "tie_vote_rule": self.rules.tie_vote_rule,
                "speech_mode": self.rules.speech_mode,
            },
        }

        # L1 组内共享：只有狼看得到队友
        if p.role.is_wolf:
            view["team_mates"] = list(priv.team_mates)
            # 狼队此刻的协商状态（2026-09-17 加）：**只有狼的视图里有这一段**。
            # 这是"面杀里狼睁眼互相看一眼"的等价物 —— 信息全部来自狼自己的动作，
            # 不是新开的交流通道（白天照旧完全不交流）。
            pts = self.night.get("wolf_points") or {}
            view["wolf_points"] = {s: pts.get(s) for s in sorted({seat, *priv.team_mates})
                                   if self.player(s).alive}
            view["wolf_confirmed"] = list(self.night.get("wolf_confirm") or [])
            view["kill_settled"] = bool(self.night.get("kill_settled"))

        # L2 个体私有：只有本人看得到「我自己做过什么」。
        # 存在的理由：AI 每轮现拼 prompt、不带对话历史，所以必须**显式**把
        # 「我上一轮说过什么」「我投给了谁」喂给它 —— 否则它会前后矛盾，
        # 也不会去验自己说过要验的人（用户实测报过这个问题）。
        # ⚠️ 只放**自己**的；别的座位一条都拿不到（这是 L2，不是 L0）。
        view["my_speeches"] = [{"round": s["round"], "text": s["text"]}
                               for s in self.speeches if s["seat"] == seat][-4:]
        view["my_votes"] = [{"round": i + 1, "target": h.get(seat)}
                            for i, h in enumerate(self.vote_history)]
        # 本局配置（2026-10-02 用户加）：多少人、多少狼、各神职有无。
        # 面杀里配置是**公开**的 ⇒ 放引擎视图（人人相同）。没有它的后果（实测）：
        # 4 人局没有守卫，AI 发言却说「可能有守卫」—— 配置靠猜，猜就一定有错的。
        view["board_fact"] = self.board_fact()
        # 前几天的投票票型（2026-10-02 用户加）：**公开**信息（暗投结算后公开），
        # 所以放在引擎视图 —— 发言/投票/刀人/技能全部走 get_view_for，一处覆盖。
        # 「正在投的今天」不在 vote_history（结算时才 append）⇒ 天然不暴露。
        view["vote_patterns"] = self.vote_patterns()
        # 今天的发言进度（公开）：谁说过/谁没说/现在轮到谁 —— 防「评论还没发生的发言」
        view["speech_progress"] = self.speech_progress()

        # 「你为什么现在开口」——挑战式发言顺序（speech_mode == "challenge"）下，
        # 一轮里会出现两种**非轮转**的发言：被点名的人回应、以及被回应后再说一句。
        # 不告诉 AI 这件事，它会以为又轮到自己自由发言（重复上一句、或者接着说下去）。
        # ⚠️ 只给**正在发言的那个人**（别的座位拿到它是噪音，而且这是 L2 私有上下文）。
        if self.phase is Phase.DISCUSS and self.current_speaker() == seat:
            _why = self._slot_reason.get(self.speak_cursor)
            if _why:
                view["speech_reason"] = _why

        # L0 派生：本轮"谁被质疑最多"。
        # ⚠️ 它的输入**只有公开发言**，所以：人人相同、不违反暗投、
        #    和"投票进行中看不到票型"那条铁律不冲突（这不是票型，是舆论方向）。
        # 它补的是"不亮票"留下的那个信息缺口：AI 投出的那一刻看不到任何票，
        # 但看得到大家嘴上在怀疑谁 —— 才有可能判断"我这一票会不会显得孤立"。
        view["suspicion"] = self.suspicion_this_round()

        # L0：全桌**自己声明的**立场（局势卡，G4）。
        # 和 suspicion 一样是"人人相同"的公开派生信息，但精度高得多：
        # suspicion 是从自由文本里猜，这里是本人给的（人类没有卡，只能靠关键词那一行）。
        view["stances"] = self.stances_latest()
        view["stances_history"] = self.stances_all()

        # L2 个体私有：只有本人看得到自己的身份记录
        if p.role is Role.SEER:
            view["investigations"] = dict(priv.investigations)
        if p.role is Role.WITCH:
            view["potions"] = dict(priv.potions)
            # 女巫必须知道今晚谁被刀才能决定救不救 —— 这正是「法官私下告知」的信息。
            # 只在夜间暴露，且只给她一个人（L2）。
            if self.phase is Phase.NIGHT:
                victim = self.night.get("kill")
                view["night_victim"] = victim if victim not in (None, -1) else None

        return view

    def actions_for(self, seat: int) -> List[str]:
        """公共接口：该座位此刻能做什么。给驱动器与界面用。"""
        return self._actions_available(seat)

    def _actions_available(self, seat: int) -> List[str]:
        """返回该座位**此刻还能做**的动作（已经做过的就不再列出）。"""
        # ⚠️ 开枪这一支**必须放在"死人不给动作"之前**：要开枪的猎人刚刚死了。
        if self.phase is Phase.SHOOT:
            # 给 "pass" 这个出口：猎人可以选择**不开枪**（前端会把它显示成「不开枪」按钮）
            return ["shoot", "pass"] if self.shooter == seat else []
        p = self.player(seat)
        if not p.alive:
            return []

        if self.phase is Phase.NIGHT:
            acts = []
            if p.role.is_wolf and not self.night.get("kill_settled"):
                acts.append("kill")
                # 点过之后还要"确定"。多狼局里不确认 = 没表态，刀口定不下来；
                # 单狼局点即定案（kill_settled 立刻为真），走不到这一支。
                if seat not in (self.night.get("wolf_confirm") or []):
                    acts.append("confirm")
            if p.role is Role.SEER and self.night.get("investigate") is None:
                acts.append("investigate")
            # 守卫：每晚都要守一个人（标准规则里没有"今晚不守"这个选项）。
            # guard_asked 与 witch_asked 同理：不显式标记"问过了"，
            # 人类守卫会被整个跳过、夜晚也结束不了。
            if p.role is Role.GUARD and not self.night.get("guard_asked"):
                acts.append("guard")
            # 女巫必须**在狼人刀完之后**才睁眼。这是规则，也是她在结构上唯一
            # 「事后救人」的方式 —— 狼没刀之前，她根本拿不到死者是谁。
            # 少这道门会出两种事故，都实测复现过：
            #   · AI 女巫在狼刀之前就被问，此时 night_victim 还是 None，她只能"过"掉，
            #     而 witch_asked 被写死 ⇒ 这一夜她再也不会被问，**永远救不了人**
            #     （bar4 seed=5：kill=None 但 witch_asked=True）
            #   · 人类女巫拿到手机时夜里还没死人，界面让她"随便救谁"，白耗一瓶解药
            # 🔴 2026-10-07（用户定："用了/不用解药，毒药那一问都要照常走"）：
            #    **第二窗**：解药那一问表过态之后（witch_asked 已置位），只要毒药还在、
            #    还没用，就在毒药它自己的窗口里列出来。收尾由 `night_decline(stage=
            #    "poison")` 负责（毒药窗口到点注入）—— 不会再卡夜。
            if p.role is Role.WITCH and not self.night.get("witch_asked"):
                # 🔴 2026-10-07：判据从"有刀口"改成"**刀已定案**" —— 空刀也是一种
                #   定案（kill_settled=True、kill=-1）。定稿要求"女巫没药也要走满
                #   「你要使用毒药吗？」那 15 秒"，空刀同理；只认刀口的话，空刀这一夜
                #   女巫永远拿不到手机、witch_asked 永远置不上 ⇒ 夜晚结束不了（死锁）。
                if self.night.get("kill_settled") or self.knife_target() is not None:
                    # 一晚只能用一瓶药（标准规则），所以给出选择而不是全部列出。
                    # 同时必须提供「不用药」，否则这一夜永远结束不了。
                    # ⚠️ 空刀（没人被刀）时 **heal 照样列出来** —— 用户定：
                    #    "狼空刀，人类女巫照常走解药播报和面板流程，只是解药按钮置灰"。
                    #    所以这一屏由**前端置灰**、引擎在 night_heal 里兜底拒绝；
                    #    不列出来的话那一屏整个消失（那是我第一版做窄了）。
                    if self.private[seat].potions["heal"]:
                        acts.append("heal")
                    if self.private[seat].potions["poison"]:
                        acts.append("poison")
                    acts.append("pass")
            return acts

        if self.phase is Phase.DISCUSS and self.current_speaker() == seat:
            return ["speak"]

        # ⚠️ 用 can_vote 而不是 alive：翻牌的白痴活着但没有投票权（标准判例）。
        #    写成 alive 的后果不只是"多给一个按钮"，而是归票判据会永远等不到他（D5）。
        if self.phase is Phase.VOTE and seat not in self.votes and self.can_vote(seat):
            return ["vote"]

        return []

    # ------------------------------------------------------------ 约束

    def _require_phase(self, *allowed: Phase) -> None:
        if self.phase not in allowed:
            raise RuntimeError(f"当前阶段是 {self.phase.value}，不允许该操作")

    def _require_alive(self, seat: int) -> None:
        if not self.player(seat).alive:
            raise ValueError(f"{self.player(seat).display} 已经出局")


# ================================================================ 便捷构造

def new_game(humans: int = 2, ais: int = 2,
             seed: Optional[int] = None) -> Game:
    """
    按人数造一局（默认 2 真人 + 2 AI）。

    ⚠️ 以前这个函数收的是"预设名 + kinds"，名字还叫 new_game —— 迁移时改成了**人数**：
    收预设名就等于让测试和界面走两条不同的开局路径，那正是我们要消掉的东西。
    """
    board = make_board(int(humans), int(ais))
    n = len(board.roles)
    h = min(max(int(humans), 0), n)
    return Game(board, ["human"] * h + ["ai"] * (n - h), seed=seed)
