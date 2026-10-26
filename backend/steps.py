"""
阶段剧本（H1）：把"这一步谁该做什么"从 if-else 里搬进一张**数据表**。

MetaGPT 的做法是给每一步一张卡片 {内容, 发给谁, 谁可发言}。我们的驱动器
（`game_api.auto_advance`）原来是一长串 `if g.phase is ...:`，**每加一个角色
就要在里面再挂一支** —— F2 加三神时就是这么加进去的（狼刀 / 验人 / 女巫 / 守卫
各一支，外加各自"做完了没有"的判断）。

这张表把"夜晚还有哪些步骤没做完"变成**可枚举的数据**：
  · 每行 = 一个夜间步骤
  · `done(game)` 判断这一步做完没有（**只读状态，不改状态**）
  · `run` 是驱动器上的方法名（字符串，避免本模块反向 import game_api）
    返回 True = 它推进了一步；False = 该人类出手了（驱动器据此报 human_night）

驱动器只做一件事：**从上往下找第一个没做完的步骤**，然后要么替 AI 做掉、
要么把 pending 报成"等人"。它不再认识任何角色。

⚠️ 边界（很重要）：这张表只描述**顺序和完成条件**，不描述规则。
   判例（女巫只能救被刀的人、守卫不能连守…）全在 engine 里。
   所以改这张表**改不动任何判例** —— 这也是它能安全重构的前提。

⚠️ 顺序就是语义：女巫必须在狼刀定下来之后（她要看着刀口决定救不救），
   所以 wolf 那一行必须在 witch 前面。加新步骤时别插错位置。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Tuple


@dataclass(frozen=True)
class NightStep:
    key: str
    label: str                       # 给人看的名字（战报/诊断会用到）
    done: Callable[[Any], bool]      # (game) -> 这一步做完没有
    run: str                         # 驱动器上的方法名


def _wolves_alive(g) -> list:
    return [p for p in g.alive if p.role.is_wolf]


NIGHT_STEPS: Tuple[NightStep, ...] = (
    NightStep(
        key="wolf",
        label="狼队刀人",
        # 场上没狼 ⇒ 这一步天然"做完"（否则会永远等不到刀口，整个夜晚卡死）
        done=lambda g: (not _wolves_alive(g)) or bool(g.night.get("kill_settled")),
        run="_step_ai_wolf",
    ),
    NightStep(
        key="seer",
        label="预言家验人",
        done=lambda g: (not any(p.role.value == "seer" for p in g.alive))
                       or g.night.get("investigate") is not None,
        run="_step_ai_seer",
    ),
    NightStep(
        key="witch",
        label="女巫用药",
        # 必须在刀口定下来之后（她要看着刀口决定救不救）——顺序在表里保证
        done=lambda g: (not any(p.role.value == "witch" for p in g.alive))
                       or bool(g.night.get("witch_asked")),
        run="_step_ai_witch",
    ),
    NightStep(
        key="guard",
        label="守卫守人",
        done=lambda g: (not any(p.role.value == "guard" for p in g.alive))
                       or bool(g.night.get("guard_asked")),
        run="_step_ai_guard",
    ),
)


def night_step_names() -> Tuple[str, ...]:
    """步骤顺序（诊断/测试用：能一眼看出顺序，不用去读驱动器）。"""
    return tuple(s.key for s in NIGHT_STEPS)


# ================================================================ 相位剧本（白天也表驱动）
#
# 上面那张表只描述**夜晚内部的四步**；这张表描述**相位之间**怎么走
# （夜晚 → 开枪 → 天亮 → 发言 → 投票 → 下一夜）。
#
# 为什么要有这一层：`auto_advance` 原来是一串 `if g.phase is Phase.XXX:` ——
# 加一个相位就要在循环里再插一段。现在循环只剩一句"这个相位交给谁"，
# 每段逻辑各自是一个方法（可单独测、也可以单独替换）。
#
# ⚠️ `handler` 是驱动器上的方法名（字符串，避免本模块反向 import game_api）。
#    运行时驱动器要能取到它 —— 取不到就是拼错了，那会表现成"卡在这个相位"，
#    所以驱动器在取不到时**明确报错**而不是静默转圈。
@dataclass(frozen=True)
class PhaseStep:
    phase_key: str
    label: str
    handler: str


PHASE_STEPS: Tuple[PhaseStep, ...] = (
    PhaseStep("night", "夜晚（狼刀/验人/用药/守人）", "_advance_night"),
    PhaseStep("shoot", "猎人开枪", "_advance_shoot"),
    PhaseStep("dawn", "天亮", "_advance_dawn"),
    PhaseStep("discuss", "白天发言", "_advance_discuss"),
    PhaseStep("vote", "投票", "_advance_vote"),
    # 🔴 2026-10-05：EXILE 不再是"瞬时态" —— 被投出的猎人开完枪会回到这里
    #（shoot_from=EXILE），这一拍的任务就是进下一夜。没有它，auto_advance
    # 在 EXILE 相位会报"没有配剧本的相位"然后 stuck。
    PhaseStep("exile", "投票结算后（猎人开完枪）→ 下一夜", "_advance_exile"),
)


def handler_for(phase) -> "str | None":
    """这个相位交给驱动器的哪个方法。认不出返回 None（调用方要明确报错）。"""
    key = getattr(phase, "value", None) or str(phase)
    for s in PHASE_STEPS:
        if s.phase_key == key:
            return s.handler
    return None


# ⚠️ 2026-09-25 删掉了 phase_names()：全仓没人调用（只有定义本身）。
#    要按相位名遍历的话，PHASE_STEPS 就在上面，直接用它即可。


def first_unfinished(game) -> NightStep:
    """
    第一个没做完的夜间步骤。全做完时返回最后一行（调用方在此之前会先判"全做完")。
    """
    for s in NIGHT_STEPS:
        if not s.done(game):
            return s
    return NIGHT_STEPS[-1]


def night_done(game) -> bool:
    """
    夜晚该做的都做了没有（驱动器"夜里推不动了"的唯一判据）。

    🔴 **场上没有狼 ⇒ 整夜直接算完**（这是必须保留的一条兜底，不是优化）：
    没有狼就没有刀口，`kill` 永远是 None；而**女巫那一步要求"刀口已定"**
    （她要看着刀口决定救不救）⇒ 她这一步永远做不完 ⇒ 夜晚再也结束不了，
    表现成"游戏不动了"，而且此时**没人有任何待办**（狼死光了、女巫在等刀口）。

    这条兜底原来写在 `game_api._night_done` 的第一行（带注释），
    我把它搬进表时漏了 —— 12 人局回归当场抓到（第 6 轮、狼全死光、卡在 human_night）。
    """
    if not _wolves_alive(game):
        return True
    return all(s.done(game) for s in NIGHT_STEPS)
