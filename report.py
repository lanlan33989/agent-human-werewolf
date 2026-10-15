"""
战报（G6）：把一局变成一份**能给人看的** Markdown。

和另外两份产物的分工（别混，三个东西解决三件事）：
  · `metrics.py` —— 出**数字**（六维、人机对比），给指标/榜单/评测用
  · `Session.replay()` —— 出**结构化 JSON**，给机器读、给战报当输入
  · 本模块 —— 出**叙事**（时间线重建 + 关键转折点），给人读

⚠️ 战报是**局末产物**：它揭示所有人的身份。
   接口层必须拦住局中调用（game_api 里返回 409），否则等于把答案塞给还在打的人。

⚠️ 方向：本模块只 import metrics（可选），不 import engine/game_api。
   它读的是"会话对象"（鸭子类型：s.game / s.level / s.id / s.replay()）。
"""
from __future__ import annotations

from typing import Any, Dict, List

_CAUSE_CN = {"kill": "被狼刀", "poison": "被毒", "vote": "被投出", "shoot": "被枪带走"}


def _who(seat: Any) -> str:
    try:
        return f"{int(seat) + 1} 号"
    except (TypeError, ValueError):
        return "—"


def _target(seat: Any) -> str:
    """刀口/目标：-1 = 空刀，None = 没做。"""
    if seat is None:
        return "—"
    try:
        s = int(seat)
    except (TypeError, ValueError):
        return "—"
    return "空刀" if s == -1 else f"{s + 1} 号"


def _vote_line(votes: Dict[Any, Any]) -> str:
    """票型："2，4→1；5→3"（按目标分组，票多的在前）。"""
    groups: Dict[int, List[int]] = {}
    for voter, t in (votes or {}).items():
        if t in (None, -1):
            groups.setdefault(-1, []).append(int(voter))
            continue
        groups.setdefault(int(t), []).append(int(voter))
    parts = []
    for tgt, vs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        head = "弃票" if tgt == -1 else _who(tgt)
        parts.append("，".join(_who(v) for v in sorted(vs)) + "→" + head)
    return "；".join(parts) if parts else "（无有效票）"


def turning_points(rp: Dict[str, Any], limit: int = 6) -> List[str]:
    """
    关键转折点：**规则式识别**（不调模型），每条都能对回原始数据。

    为什么不用模型总结：转折点必须**可核对**（玩家会拿它跟自己的记忆对）。
    模型写的好看但可能编，而这份东西是要贴在桌上让人复盘的。
    """
    seats = {int(s["seat"]): s for s in rp.get("seats") or []}
    rounds = int(rp.get("rounds") or 0)
    out: List[tuple] = []

    for n in (rp.get("nights") or []):
        r = int(n.get("round") or 0)
        if n.get("pierced"):
            out.append((r, f"第 {r} 轮：**同守同救**（守卫与女巫救了同一个人）—— 按规则该人仍然出局。"))
        elif n.get("saved_by_heal") or n.get("saved_by_guard"):
            by = "女巫的解药" if n.get("saved_by_heal") else "守卫的守护"
            out.append((r, f"第 {r} 轮：**平安夜**（{by}挡住了这一刀）。"
                           f"桌上没人知道是谁挡的 —— 这正是平安夜该有的样子。"))
        elif n.get("peaceful"):
            out.append((r, f"第 {r} 轮：**平安夜**（也可能是狼空刀，公告里看不出区别）。"))
        pt = n.get("poison")
        if pt is not None and int(pt) != -1:
            t = seats.get(int(pt)) or {}
            if t.get("role") == "wolf":
                out.append((r, f"第 {r} 轮：女巫**毒到了狼**（{_who(pt)}）。"))
            else:
                out.append((r, f"第 {r} 轮：女巫**毒到了自己人**（{_who(pt)} 是{t.get('role_label') or '好人'}）。"))

    for v in (rp.get("votes") or []):
        r = int(v.get("round") or 0)
        ex = v.get("exiled")
        if ex is None:
            out.append((r, f"第 {r} 轮：投票**平票**，本轮无人出局。"))
        else:
            t = seats.get(int(ex)) or {}
            if t.get("role") == "wolf":
                out.append((r, f"第 {r} 轮：全场第一次**投出狼**（{_who(ex)}）。"))
            elif t.get("role") in ("seer", "witch", "hunter", "guard", "idiot"):
                out.append((r, f"第 {r} 轮：**投出了神职**（{_who(ex)} 是{t.get('role_label')}）。"))
            else:
                out.append((r, f"第 {r} 轮：投出了好人（{_who(ex)} 是{t.get('role_label')}）。"))

    for d in (rp.get("deaths") or []):
        if d.get("cause") == "shoot":
            t = seats.get(int(d["seat"])) or {}
            tag = "狼" if (t.get("role") == "wolf") else (t.get("role_label") or "好人")
            out.append((int(d.get("round") or 0),
                        f"第 {d.get('round')} 轮：猎人开枪带走了 {_who(d['seat'])}（{tag}）。"))

    w = rp.get("winner")
    if w in ("wolf", "good"):
        out.append((rounds + 1, "**终局**：" + ("狼人胜利。" if w == "wolf" else "好人胜利。")))
    out.sort(key=lambda x: x[0])
    # 同类转折挤在一轮时只留前几条，避免战报变成播报流水账
    seen, uniq = set(), []
    for r, t in out:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq[:limit]


def markdown(sess) -> str:
    """
    生成战报正文（Markdown）。

    ⚠️ 整段用 try 兜住：战报**绝不能因为一个字段缺失就把整局报错**
       （它已经是局末的收尾动作，报错只会让玩家看到 500）。
    """
    rp = sess.replay()
    m = rp.get("metrics") or {}
    out = m.get("outcome") or {}
    seats = list(rp.get("seats") or [])
    humans = sum(1 for s in seats if s.get("kind") == "human")
    ai_n = len(seats) - humans
    win_cn = {"wolf": "狼人胜利", "good": "好人胜利"}.get(rp.get("winner"), "未结束")

    L: List[str] = []
    L.append(f"# 本局战报 · {rp.get('board') or ''}"
             + (f" · {rp.get('level')}档" if rp.get("level") else ""))
    L.append("")
    L.append(f"**{win_cn}** ｜ 共 {out.get('rounds', rp.get('rounds'))} 轮 ｜ "
             f"{humans} 名真人 + {ai_n} 名 AI ｜ 局号 `{rp.get('gid')}`")
    L.append("")

    L.append("## 关键转折")
    tps = turning_points(rp)
    if tps:
        for t in tps:
            L.append(f"- {t}")
    else:
        L.append("- （这一局没有明显的转折点）")
    L.append("")

    L.append("## 身份公开（局末解密）")
    L.append("")
    L.append("| 座位 | 谁 | 身份 | 结果 |")
    L.append("|---|---|---|---|")
    for s in seats:
        res = ("存活" if s.get("alive")
               else f"第 {s.get('died_round')} 轮{_CAUSE_CN.get(s.get('died_cause'), '出局')}")
        L.append(f"| {_who(s['seat'])} | {'真人' if s.get('kind') == 'human' else 'AI'} | "
                 f"{s.get('role_label')} | {res} |")
    L.append("")

    L.append("## 逐轮回顾")
    speeches = rp.get("speeches") or []
    votes = rp.get("votes") or []
    nights = rp.get("nights") or []
    for r in range(1, int(rp.get("rounds") or 0) + 1):
        L.append(f"### 第 {r} 轮")
        n = next((x for x in nights if int(x.get("round") or 0) == r), None)
        if n:
            if n.get("peaceful"):
                L.append("- **夜**：平安夜。")
            else:
                L.append(f"- **夜**：{_target(n.get('kill'))}倒下。")
            extra = []
            # 🔴 2026-10-12（用户：白天和白天之间要能看到夜里的行动）：
            #    狼刀原来不进"夜间动作"行 —— 平安夜/无人夜里谁被刀完全看不出来。
            if n.get("kill") not in (None, -1):
                extra.append(f"狼刀 {_target(n.get('kill'))}")
            if n.get("heal") not in (None, -1):
                extra.append(f"女巫救 {_target(n.get('heal'))}")
            if n.get("guard") not in (None, -1):
                extra.append(f"守卫守 {_target(n.get('guard'))}")
            if n.get("investigate") not in (None, -1):
                extra.append(f"预言家验 {_target(n.get('investigate'))}")
            if n.get("poison") not in (None, -1):
                extra.append(f"女巫毒 {_target(n.get('poison'))}")
            if extra:
                L.append(f"  - 夜间动作：{'；'.join(extra)}")
        sp = [x for x in speeches if int(x.get("round") or 0) == r]
        if sp:
            L.append("- **言**：")
            for x in sp:
                s = next((q for q in seats if int(q["seat"]) == int(x["seat"])), {})
                L.append(f"  - {_who(x['seat'])}（{s.get('role_label') or '?'}）：{x.get('text') or ''}")
        v = next((x for x in votes if int(x.get("round") or 0) == r), None)
        if v:
            L.append(f"- **票**：{_vote_line(v.get('votes') or {})}")
            if v.get("exiled") is not None:
                L.append(f"  - ⇒ {_who(v['exiled'])} 被投出")
            else:
                L.append("  - ⇒ 平票，无人出局")
        L.append("")

    L.append("## 六维指标")
    L.append("")
    L.append("| 维度 | 数值 |")
    L.append("|---|---|")
    for d in (m.get("dims") or []):
        L.append(f"| {d['label']} | {_dim_brief(d['key'], d['value'], m)} |")
    L.append("")

    L.append("## 人机对比")
    ha = m.get("human_ai") or {}
    sr = ha.get("seat_win_rate") or {}
    L.append(f"- 真人座位胜率：{_pct(sr.get('human'))}　AI 座位胜率：{_pct(sr.get('ai'))}")
    L.append(f"- 被投出的人里：真人 {ha.get('human_exiled', 0)} 人、AI {ha.get('ai_exiled', 0)} 人"
             f"（真人被投出率 {_pct(ha.get('human_exiled_rate'))}）")
    L.append(f"- AI 把票投给真人的比例：{_pct(ha.get('ai_votes_on_human_rate'))}")
    L.append("")

    L.append("## 本局教训（待复核，不会自动生效）")
    cands: List[Dict[str, Any]] = []
    try:
        import experience                                    # noqa: PLC0415
        cands = [x for x in experience.BANK.pending if x.get("gid") == rp.get("gid")]
    except Exception:                                        # noqa: BLE001
        cands = []
    if cands:
        for c in cands[:5]:
            L.append(f"- （{c.get('role')}）{c.get('text')}　`{c.get('why')}`")
        L.append("")
        L.append("> 这些是**候选**：要在接口上复核入库，且只在硬核/地狱档生效。"
                 "它们是脱敏过的（不含座位号），因为下一局座位会重排。")
    else:
        L.append("- （这一局没有产出候选，或者已经被复核掉了）")
    L.append("")
    L.append("---")
    L.append("*战报由本地引擎生成，局末解密；不含任何场外信息。*")
    return "\n".join(L)


def _pct(v: Any) -> str:
    return "—" if v is None else f"{round(float(v) * 100)}%"


def _dim_brief(key: str, val: Dict[str, Any], m: Dict[str, Any]) -> str:
    """把某个维度的核心数字压成一行（战报是给人扫的，不是给人算的）。"""
    try:
        if key == "outcome":
            return (f"{val.get('winner') or '未结束'}，{val.get('rounds')} 轮，"
                    f"平票 {val.get('tie_votes')} 次")
        if key == "survival":
            bk = val.get("by_kind") or {}
            return f"平均活 {val.get('overall')} 轮（真人 {bk.get('human')} / AI {bk.get('ai')}）"
        if key == "votes":
            return (f"好人投中狼 {_pct(val.get('good_accuracy'))}；"
                    f"狼投好人 {_pct(val.get('wolf_misvote_rate'))}；"
                    f"弃票 {_pct(val.get('abstain_rate'))}")
        if key == "skills":
            return (f"预言家验 {val.get('seer_checks')} 次（中狼 {val.get('seer_found_wolves')}）；"
                    f"女巫救 {val.get('witch_heals')} / 毒 {val.get('witch_poisons')}；"
                    f"守卫挡 {val.get('guard_blocks')}；猎人开枪 {val.get('hunter_shots')}")
        if key == "speech":
            return (f"{val.get('count')} 条，人均 {val.get('avg_length')} 字"
                    + ("（质量待接 LLM 打分）" if val.get("quality") is None else ""))
        if key == "tempo":
            return (f"{val.get('nights')} 夜（平安 {val.get('peaceful_nights')}）；"
                    f"夜里死 {val.get('night_deaths')} / 白天出 {val.get('day_deaths')}")
        if key == "human_ai":
            sr = val.get("seat_win_rate") or {}
            return f"真人 {_pct(sr.get('human'))} vs AI {_pct(sr.get('ai'))}"
    except Exception:                                        # noqa: BLE001
        pass
    return "—"
