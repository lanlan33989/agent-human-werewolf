"""
跨局经验（G3）：局末生成局势总结 → **离线评审入库** → 下一局注入。

三条硬约束（都是明确要求的，不是我的偏好）：

1. **只走离线评审入库，不做自动进化。**
   生成的只是"候选"，必须复核后才进"已入库"，只有已入库的才会进 prompt。
   （开源那套 evolution 是"自动改 prompt 去涨胜率"，目标和我们相反，且有失控风险。）

2. **脱敏：候选里不许出现任何座位号。**
   下一局的座位是重排的 —— "3 号是狼"下周可能 3 号是好人，
   直接注入等于**教 AI 记住一个错误事实**（比不注入更糟）。
   所以只允许写"我自己的行为模式与教训"，且措辞上必须允许它不适用。

3. **按档开启**：只有 LEVELS[level]["experience"] 为真的档位（硬核/地狱）才注入。
   娱乐档注入跨局经验会让 AI 显得"太熟"，破坏轻松局的体感。

诚实边界：候选是**规则式**生成的（不调模型）——它是"我从结构化数据里能读出来的
统计结论"，不是"模型自己复盘的叙事"。等 H4（便宜的第二模型通道）接上后，
把 generate_candidates 换成"模型读战报写总结"即可，接口不用变。

⚠️ 方向：本模块**不 import engine / agents**（只用鸭子类型读 game 与传入的指标）。
   这样谁都能 import 它，不会成环。
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, List, Optional

_ids = itertools.count(1)


def _majority_target(votes: Dict[Any, Any]) -> Optional[int]:
    """这一轮票数最多的目标（没人投有效票时 None）。"""
    tally: Dict[int, int] = {}
    for t in (votes or {}).values():
        if t in (None, -1):
            continue
        tally[int(t)] = tally.get(int(t), 0) + 1
    if not tally:
        return None
    top = max(tally.values())
    if sum(1 for v in tally.values() if v == top) != 1:
        return None                       # 并列第一 ⇒ 这轮没有"大势所趋"
    return next(s for s, v in tally.items() if v == top)


def _my_vote_features(game, seat: int) -> Dict[str, Any]:
    """
    "我自己投票"的行为特征（**只看我自己的票** + 当轮票型的多数方向）。

    ⚠️ 这是脱敏的关键一步：我只统计"我"投得孤不孤立、有没有弃票，
       不去记录"谁投了谁"。所以生成的教训天然不含别人的身份。
    """
    lone = abstain = 0
    for h in (getattr(game, "vote_history", []) or []):
        mine = h.get(seat)
        if mine in (None, -1):
            abstain += 1
            continue
        maj = _majority_target(h)
        if maj is not None and int(mine) != maj:
            lone += 1
    return {"lone_votes": lone, "abstains": abstain,
            "rounds": len(getattr(game, "vote_history", []) or [])}


def generate_candidates(game, level: str, gid: str = "") -> List[Dict[str, Any]]:
    """
    从**已结束**的一局里为每个 AI 座位生成候选经验。

    产出是"观测 + 一条hedged 行动倾向"，并带上 `why`（触发它的数据）——
    复核的人需要能看出"这条是从哪来的"，否则评审就是走过场。
    """
    if getattr(game, "winner", None) not in ("wolf", "good"):
        return []
    out: List[Dict[str, Any]] = []
    rounds = max(int(getattr(game, "round", 0)), 1)
    deaths = {int(d["seat"]): d for d in (getattr(game, "deaths", []) or [])
              if d.get("seat") is not None}
    for p in game.players:
        if p.kind != "ai":
            continue
        me = _my_vote_features(game, p.seat)
        d = deaths.get(p.seat)
        survived = int(d.get("round", rounds)) if d else rounds
        role = p.role.value
        texts: List[tuple] = []

        if p.role.is_wolf:
            if survived < rounds:
                texts.append((
                    f"我当狼时在第 {survived} 轮就出局了（全场 {rounds} 轮）。"
                    f"别急着在第一天定调子，先让别人把话说满。",
                    "wolf_died_early"))
            if me["lone_votes"]:
                texts.append((
                    f"我当狼时投过 {me['lone_votes']} 次孤立票（那一票跟场上多数方向不一样）。"
                    f"投票前先看大家都在怀疑谁，单独走一次就够显眼了。",
                    "wolf_lone_votes"))
            if not texts:
                texts.append((
                    f"我当狼时活到了最后（{rounds} 轮）。少给主张、跟着场上的怀疑方向走，"
                    f"比我急着带节奏更稳。",
                    "wolf_survived"))
        else:
            hit = 0
            total = 0
            for h in (getattr(game, "vote_history", []) or []):
                t = h.get(p.seat)
                if t in (None, -1):
                    continue
                total += 1
                q = game.player(int(t))
                if q.role.is_wolf:
                    hit += 1
            if total and hit * 2 < total:
                texts.append((
                    f"我当好人时投了 {total} 票、只投中 {hit} 次狼人。"
                    f"我容易被说话多的人牵走 —— 先看谁在给逻辑，再决定跟不跟。",
                    "good_low_accuracy"))
            if me["abstains"]:
                texts.append((
                    f"我当好人时弃过 {me['abstains']} 次票。没人能确认我立场的时候，"
                    f"沉默看起来更像在躲，不如给一句理由再投。",
                    "good_abstain"))
            if not texts:
                texts.append((
                    f"我当好人时打完了全场（{rounds} 轮）。"
                    f"把票压在'谁给过能对上的逻辑'上，比压在'谁声音大'上稳。",
                    "good_survived"))

        for text, why in texts:
            out.append({"id": next(_ids), "gid": gid, "level": level, "role": role,
                        "text": text, "why": why, "status": "pending"})
    return out


class ExperienceBank:
    """
    经验库（**进程内存**，与 SESSIONS 一样的探针阶段做法：不做持久化）。

    持久化要等结构化日志落盘（G5 的下一站）。在那之前，重启服务 = 经验清空，
    这一点必须在接口里说清楚，否则会有人以为经验丢了是 bug。
    """

    def __init__(self) -> None:
        self.pending: List[Dict[str, Any]] = []
        self.approved: List[Dict[str, Any]] = []

    def add_candidates(self, items: List[Dict[str, Any]]) -> int:
        # 同一局重复收割时去重（driver 那边有"只收割一次"的锁，这里是第二道保险）
        seen = {(x["gid"], x["text"]) for x in self.pending + self.approved}
        n = 0
        for it in items or []:
            if (it.get("gid"), it.get("text")) in seen:
                continue
            self.pending.append(it)
            n += 1
        return n

    def review(self, cid: int, approve: bool) -> Optional[Dict[str, Any]]:
        """复核一条候选：approve=True 入库，False 丢弃。返回被处理的那条（找不到时 None）。"""
        for i, it in enumerate(self.pending):
            if int(it["id"]) == int(cid):
                self.pending.pop(i)
                if approve:
                    it["status"] = "approved"
                    self.approved.append(it)
                return it
        return None

    def for_prompt(self, level: str, role: str, limit: int = 3) -> List[str]:
        """
        给 prompt 用的经验：**该档开启才会非空** + 只给该角色的 + 只给已入库的。

        为什么按角色过滤：经验里写的是"我当狼时…"，把它给一个好人座位是噪音
        （模型会试图把狼的教训套到好人身上）。
        """
        from agents import LEVELS                       # 只在这里 import，避免顶部成环
        if not LEVELS.get(level, {}).get("experience"):
            return []
        items = [x for x in self.approved
                 if x["level"] == level and x["role"] in (role, "any")]
        return [x["text"] for x in items[-limit:]]

    def snapshot(self) -> Dict[str, Any]:
        return {
            "persistent": False,
            "note": "进程内存；重启服务即清空（要持久化得等结构化日志落盘）",
            "pending": self.pending,
            "approved": self.approved,
        }


# 全局实例：一局结束往这里投候选，开局从这里取已入库的。
BANK = ExperienceBank()
