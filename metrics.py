"""
测量层（2026-09-17 加，批四）：把一局对局变成**可比较的数字**。

为什么要单独一个文件：这是我们相对那几个开源项目唯一真正缺的东西
（它们有 evaluation 模块，我们只有"看起来还行"）。而且它是**纯消费方** ——
没有任何模块依赖它，所以它坏了不会影响一局游戏打不打得完。

⚠️ 两条边界（很关键，改这个文件时别破）：
  1. **不 import engine**：只用鸭子类型读 game 的属性。
     这样 engine 可以随时 import 它（"本轮谁被质疑最多"要进 AI 的视图），不会成环。
  2. **不进 prompt 的隔离体系**：它是分析工具，读的是全知状态（含所有人的身份）。
     所以它的输出**只能给复盘/指标/榜单看，绝不能塞进任何 AI 的 prompt**。
     唯一例外是 suspicion_counts —— 它的输入只有公开发言，所以人人相同（L0）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

# ================================================================ 说话里的"质疑"

# 只从**公开发言**里算：它等价于面杀里"你听得出来大家都在怀疑谁"。
# 用途：补"不亮票"那个信息缺口 —— 投票进行中 AI 看不到任何票，
# 但看得到大家嘴上在怀疑谁，才有依据判断"我这一票会不会显得孤立"。
_SUSPECT = re.compile(r"(怀疑|有问题|像狼|是狼|狼人|查杀|出他|出掉|投他|投给|票他|要投|可能是|不对劲)")
# "3 号是好人"这种要排除：明夸的人不是被质疑的对象。
# 只看座位号**紧跟**的 8 个字符，窗口不能大 —— 窗口一大，
# "我怀疑 3 号，2 号是好人"会把 3 号也误判成"被夸"。
_CLEAR_AFTER = re.compile(r"^[\s，,。：:、]{0,2}(是|为|肯定|应该|算)?\s*(好人|金水|不是狼|不像狼|我信)")
_SEAT = re.compile(r"(\d{1,2})\s*号")


def _accused_in(text: str, live: set, me: int) -> List[int]:
    """
    **一条**发言里被负面指向的座位（按出现顺序、去重）。

    口径唯一，两个调用方共用：
      · `suspicion_counts`（全轮统计，进 AI 视图）
      · `accuse_targets`（单条发言，决定「谁先回应」）
    分成两份实现迟早会漂 —— 一边改了另一边没改，表现是"视图里的怀疑方向
    和按发言顺序算出来的不一样"，而那种不一致在桌上没人看得出来。
    """
    if not text or not _SUSPECT.search(text):
        return []
    order, hit = [], set()
    for m in _SEAT.finditer(text):
        tgt = int(m.group(1)) - 1
        if tgt not in live or tgt == me or tgt in hit:
            continue
        if _CLEAR_AFTER.match(text[m.end():m.end() + 8]):
            continue
        hit.add(tgt)
        order.append(tgt)
    return order


def accuse_targets(text: str, seats: Iterable[int], me: int,
                   card: Optional[Dict[str, Any]] = None) -> List[int]:
    """
    这条发言"负面指向"了谁 —— 挑战式发言顺序用它决定**谁先回应**。

    优先级：**局势卡 > 关键词**。
      · 卡上的 `accuse` 是发言人**自己给的**（AI 走这条），最准、顺序也由他给
      · 关键词是从文本里猜的（人类没有卡，只能走这条）
    卡里的座位**照样要再校验一遍**（活着 / 不是自己）：卡本身过了
    `agents.sanitize_card`，但那是"能不能广播"的检查，这里是"能不能拿它插队"的检查，
    两件事 —— 多校验一次的成本是零，漏一次的成本是"死人被叫起来发言"。

    返回 0-based 座位，**按发言里出现的顺序**（"y 和 z 依次回应"里那个"依次"）。
    """
    live = set(int(s) for s in seats)
    if me not in live:
        return []
    acc = (card or {}).get("accuse")
    if acc is not None:
        raw = acc if isinstance(acc, (list, tuple, set)) else [acc]
        out, seen = [], set()
        for x in raw:
            # 🔴 卡里的座位号是**引擎内部约定：0-based** —— 它是 agents.sanitize_card
            #    的产物，那边已经 -1 过了，**这里不能再减**。
            #    我第一版在这里又减了一次，后果很隐蔽：卡上"2 号、5 号"会被读成
            #    "1 号、4 号"，而且第一个人常常正好是发言人自己 ⇒ 被当成"点自己"丢掉。
            #    （这也是"两处约定只能有一处负责转换"的典型翻车：转换做了两遍。）
            try:
                tgt = int(x)
            except (TypeError, ValueError):
                continue
            if tgt in live and tgt != me and tgt not in seen:
                seen.add(tgt)
                out.append(tgt)
        if out:
            return out
    return _accused_in(text, live, me)


def suspicion_counts(speeches: Optional[Sequence[Dict[str, Any]]],
                     seats: Iterable[int]) -> Dict[int, int]:
    """
    统计"每个座位被点名质疑了几次"（只用公开发言）。

    · 同一条发言里反复点名同一个座位，只算 1 次（防止刷关键词刷出高分）
    · 只统计**还活着**的座位（死人不在讨论范围内）
    · 自己说的"我"不算（_SEAT 只认"N 号"这种写法）
    · 「3 号是好人」不算质疑

    诚实边界：这是**关键词级**判断，和 leak_check 同一档 ——
    反话（"3 号绝对不是好人"）、隐喻、不带数字的指代都认不出来。
    它的价值是给 AI 一个粗糙但真实的"舆论方向"，不是精确的语义理解。
    """
    live = set(int(s) for s in seats)
    counts: Dict[int, int] = {s: 0 for s in sorted(live)}
    for sp in speeches or []:
        who = int(sp.get("seat", -1))
        for t in _accused_in(str(sp.get("text") or ""), live, who):
            counts[t] += 1
    return counts


def suspicion_line(counts: Dict[int, int], limit: int = 3) -> str:
    """
    把统计变成给 AI 看的一句话（人话，不是数字表）。

    没有一个人被质疑时返回空串 —— 那就说明"这轮还没人挑头"，
    这句话本身也是信息，但把它写成"本轮无人被质疑"会让 AI 以为必须回应它。
    """
    hot = [(s, c) for s, c in sorted(counts.items()) if c > 0]
    if not hot:
        return ""
    hot.sort(key=lambda kv: (-kv[1], kv[0]))
    parts = [f"{s + 1} 号（{c} 人）" for s, c in hot[:limit]]
    return "本轮被质疑最多的：" + "、".join(parts)


# ================================================================ 六维指标


def _pct(a: float, b: float) -> Optional[float]:
    """比率。分母为 0 时返回 None（**不是 0** —— 0 会被误读成"表现很差"）。"""
    return round(a / b, 3) if b else None


def _avg(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 2) if xs else None


def scoreboard_rounds(game) -> int:
    """这一局一共打了多少轮（轮 = 夜 + 昼 一回）。"""
    return max(int(getattr(game, "round", 0)), 0)


def _seat_rows(game) -> List[Dict[str, Any]]:
    return [{"seat": p.seat, "kind": p.kind, "role": p.role.value,
             "role_label": p.role.label, "is_wolf": bool(p.role.is_wolf),
             "alive": bool(p.alive)}
            for p in game.players]


def summarize(game, level: str = "", speech_quality: Optional[float] = None) -> Dict[str, Any]:
    """
    一局的六维指标 + 人机对比。

    ⚠️ 它读的是**全知状态**（含所有人身份）。给复盘/指标/榜单用，**不许进 prompt**。
    """
    rows = _seat_rows(game)
    winner = getattr(game, "winner", None)
    rounds = scoreboard_rounds(game)
    deaths: List[Dict[str, Any]] = list(getattr(game, "deaths", []) or [])
    nights: List[Dict[str, Any]] = list(getattr(game, "night_records", []) or [])
    votes: List[Dict[int, int]] = list(getattr(game, "vote_history", []) or [])
    by_seat = {r["seat"]: r for r in rows}

    # ---- ① 胜负与阵营 ----
    outcome = {
        "winner": winner,
        "ended": winner in ("wolf", "good"),
        "rounds": rounds,
        "level": level or None,
        "tie_votes": int(getattr(game, "tie_count", 0) or 0),
    }

    # ---- ② 存活 ----
    # 死在第几轮：没死的算"活到最后一轮"（这样平均值才有意义，不能拿 0 去拉低）
    died_round = {}
    for d in deaths:
        r = d.get("round")
        if r is not None:
            died_round.setdefault(int(d["seat"]), int(r))
    surv = []
    for r in rows:
        surv.append({**r, "died_round": died_round.get(r["seat"]),
                     "rounds_survived": died_round.get(r["seat"], rounds)})
    survival = {
        "overall": _avg([float(x["rounds_survived"]) for x in surv]),
        "by_seat": surv,
        "by_kind": {k: _avg([float(x["rounds_survived"]) for x in surv if x["kind"] == k])
                    for k in ("human", "ai")},
        "by_role": {},
        "first_death_round": min(died_round.values()) if died_round else None,
    }
    for role in sorted({r["role"] for r in rows}):
        survival["by_role"][role] = _avg(
            [float(x["rounds_survived"]) for x in surv if x["role"] == role])

    # ---- ③ 投票 ----
    good_votes = good_hit = 0        # 好人投出的票 / 其中投中狼的
    wolf_votes = wolf_misvote = 0    # 狼投出的票 / 其中投了好人的（伪装性）
    abstain = self_vote = 0
    cross = {"human_to_ai": 0, "ai_to_human": 0, "human_to_human": 0, "ai_to_ai": 0}
    for h in votes:
        for voter, target in h.items():
            v = by_seat.get(int(voter))
            if v is None:
                continue
            if target in (None, -1):
                abstain += 1
                continue
            target = int(target)
            if target == v["seat"]:
                self_vote += 1
                continue
            t = by_seat.get(target)
            if t is None:
                continue
            cross[f"{v['kind']}_to_{t['kind']}"] += 1
            if v["is_wolf"]:
                wolf_votes += 1
                if not t["is_wolf"]:
                    wolf_misvote += 1
            else:
                good_votes += 1
                if t["is_wolf"]:
                    good_hit += 1
    votes_dim = {
        "rounds_with_votes": len(votes),
        "total_votes": sum(len(h) for h in votes),
        "good_accuracy": _pct(good_hit, good_votes),      # 好人投中狼的比例
        "wolf_misvote_rate": _pct(wolf_misvote, wolf_votes),  # 狼把票投给好人的比例
        "abstain_rate": _pct(abstain, sum(len(h) for h in votes)),
        "self_vote_rate": _pct(self_vote, sum(len(h) for h in votes)),
        "cross_kind": cross,
        "by_round": [{"round": i + 1,
                      "votes": {int(k): int(v) for k, v in h.items()}}
                     for i, h in enumerate(votes)],
    }

    # ---- ④ 技能收益 ----
    seer_checks = seer_hits = 0
    for p in game.players:
        if p.role.value == "seer":
            inv = getattr(game.private.get(p.seat), "investigations", {}) or {}
            seer_checks += len(inv)
            seer_hits += sum(1 for is_wolf in inv.values() if is_wolf)
    heal_used = heal_good = poison_used = poison_hit_wolf = poison_hit_good = 0
    for n in nights:
        if n.get("saved_by_heal"):
            heal_used += 1
            t = by_seat.get(int(n["kill"]))
            if t and not t["is_wolf"]:
                heal_good += 1
        if n.get("saved_by_guard"):
            t = by_seat.get(int(n["kill"]))
            if t and not t["is_wolf"]:
                heal_good += 0        # 守卫挡刀单独算，不混进女巫
        pt = n.get("poison")
        if pt is not None and int(pt) != -1:
            poison_used += 1
            t = by_seat.get(int(pt))
            if t and t["is_wolf"]:
                poison_hit_wolf += 1
            elif t:
                poison_hit_good += 1
    guard_blocks = sum(1 for n in nights if n.get("saved_by_guard"))
    hunter_shots = [d for d in deaths if d.get("cause") == "shoot"]
    hunter_hit_wolf = sum(1 for d in hunter_shots
                          if (by_seat.get(int(d["seat"])) or {}).get("is_wolf"))
    skills = {
        "seer_checks": seer_checks,
        "seer_found_wolves": seer_hits,
        "seer_hit_rate": _pct(seer_hits, seer_checks),
        "witch_heals": heal_used,
        "witch_heals_on_good": heal_good,
        "witch_poisons": poison_used,
        "witch_poison_hit_wolf": poison_hit_wolf,
        "witch_poison_hit_good": poison_hit_good,
        "guard_blocks": guard_blocks,
        "guard_pierced": sum(1 for n in nights if n.get("pierced")),
        "hunter_shots": len(hunter_shots),
        "hunter_hit_wolf": hunter_hit_wolf,
    }

    # ---- ⑤ 发言 ----
    speeches = list(getattr(game, "speeches", []) or [])
    per_seat: Dict[int, int] = {}
    for s in speeches:
        per_seat[int(s.get("seat", -1))] = per_seat.get(int(s.get("seat", -1)), 0) + 1
    lengths = [len(str(s.get("text") or "")) for s in speeches]
    speech = {
        "count": len(speeches),
        "per_seat": per_seat,
        "avg_length": _avg([float(x) for x in lengths]),
        # ⚠️ 「发言质量」要 LLM 打分，**本轮没做**（不能拿"字数"冒充质量）。
        #    接线位置就在这个参数：跑评测时把打分传进来即可。
        "quality": speech_quality,
        "quality_note": None if speech_quality is not None else "待接 LLM 打分",
    }

    # ---- ⑥ 节奏 ----
    night_deaths = sum(1 for d in deaths if d.get("cause") in ("kill", "poison"))
    day_deaths = sum(1 for d in deaths if d.get("cause") in ("vote", "shoot"))
    tempo = {
        "rounds": rounds,
        "nights": len(nights),
        "peaceful_nights": sum(1 for n in nights if n.get("peaceful")),
        "night_deaths": night_deaths,
        "day_deaths": day_deaths,
        "deaths_per_round": round(len(deaths) / rounds, 2) if rounds else None,
        "tie_votes": outcome["tie_votes"],
    }

    # ---- ⑦ 人机对比（用户点名要的那一栏）----
    # 定义：**每个座位**"我这边赢了没有" = (赢家是狼) == (我是不是狼)。
    # 不能写成"人类 vs AI 谁赢" —— 人和 AI 会分在同一边（混玩），那样会算成双赢。
    wins = {"human": [], "ai": []}
    for r in rows:
        if winner not in ("wolf", "good"):
            continue
        won = (winner == "wolf") == r["is_wolf"]
        wins.setdefault(r["kind"], []).append(won)
    exiled = [int(d["seat"]) for d in deaths if d.get("cause") == "vote"]
    human_ai = {
        "seat_win_rate": {k: _pct(sum(1 for x in v if x), len(v)) for k, v in wins.items()},
        "human_exiled": sum(1 for s in exiled
                            if (by_seat.get(s) or {}).get("kind") == "human"),
        "ai_exiled": sum(1 for s in exiled
                         if (by_seat.get(s) or {}).get("kind") == "ai"),
        "exiled_total": len(exiled),
        "human_exiled_rate": _pct(
            sum(1 for s in exiled if (by_seat.get(s) or {}).get("kind") == "human"),
            len(exiled)),
        # AI 有没有在"欺负"真人：AI 把票投给人的次数 / AI 投出的总票数
        "ai_votes_on_human_rate": _pct(cross["ai_to_human"],
                                       cross["ai_to_human"] + cross["ai_to_ai"]),
    }

    # ---- 展示用：六个维度的名字（前端/报告按这个顺序渲染）----
    dims = [
        {"key": "outcome", "label": "① 胜负与阵营", "value": outcome},
        {"key": "survival", "label": "② 存活", "value": survival},
        {"key": "votes", "label": "③ 投票", "value": votes_dim},
        {"key": "skills", "label": "④ 技能收益", "value": skills},
        {"key": "speech", "label": "⑤ 发言", "value": speech},
        {"key": "tempo", "label": "⑥ 节奏", "value": tempo},
        {"key": "human_ai", "label": "人机对比（用户点名要的）", "value": human_ai},
    ]
    return {"level": level or None, "winner": winner, "dims": dims,
            "outcome": outcome, "survival": survival, "votes": votes_dim,
            "skills": skills, "speech": speech, "tempo": tempo, "human_ai": human_ai}


# ================================================================ 跨局聚合（榜单 v0）

def aggregate(boards: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """
    把若干局的 scoreboard 汇成一张榜。**按档分组** ——
    不分档的胜率没有意义：不同档的 AI 根本不是一回事（赌注、说话要求都不同）。

    诚实边界：只统计**内存里还活着**的那些局（探针阶段不做持久化）。
    真正的榜单要等落盘（批次 5/6 的结构化日志）。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for b in boards:
        lv = b.get("level") or "（未标档）"
        g = out.setdefault(lv, {"games": 0, "wolf_wins": 0, "good_wins": 0,
                                "human_win_seats": [0, 0], "ai_win_seats": [0, 0],
                                "rounds": [], "human_exiled": 0, "ai_exiled": 0})
        g["games"] += 1
        w = b.get("winner")
        if w == "wolf":
            g["wolf_wins"] += 1
        elif w == "good":
            g["good_wins"] += 1
        g["rounds"].append(b["outcome"]["rounds"])
        for k, i in (("human", 0), ("ai", 1)):
            rate = (b["human_ai"]["seat_win_rate"] or {}).get(k)
            n = len([r for r in b["survival"]["by_seat"] if r["kind"] == k])
            if rate is not None and n:
                g["human_win_seats" if k == "human" else "ai_win_seats"][0] += round(rate * n)
                g["human_win_seats" if k == "human" else "ai_win_seats"][1] += n
        g["human_exiled"] += b["human_ai"]["human_exiled"]
        g["ai_exiled"] += b["human_ai"]["ai_exiled"]
    for lv, g in out.items():
        g["wolf_win_rate"] = _pct(g["wolf_wins"], g["games"])
        g["avg_rounds"] = _avg([float(x) for x in g["rounds"]])
        g["human_seat_win_rate"] = _pct(*g["human_win_seats"])
        g["ai_seat_win_rate"] = _pct(*g["ai_win_seats"])
        g.pop("rounds", None)
    return {"by_level": out}
