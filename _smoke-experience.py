# -*- coding: utf-8 -*-
"""
跨局经验 smoke test（批五 · G3）。

这个文件里最重要的是**脱敏断言**：候选文本里**一个座位号都不许出现**。
理由很硬：下一局座位会重排，"3 号是狼"下周可能 3 号是好人，
直接注入等于教 AI 记住一个错误事实 —— 比不注入更糟。
"""
import re
import sys

import game_api

# 2026-10-07 第三期：夜间窗口是真·墙上时间 —— 不缩短的话**一夜四段 = 65 秒**，
# 这个短跑文件会从几十秒涨到几分钟。生产/真机恒为 1.0；窗口的**真实秒数**
# 由 _smoke-night-tempo.py 单独验收（那个才是验收标准）。
import night_script  # noqa: E402
night_script.set_win_scale(0.02)
import experience
from engine import Phase

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def play_to_end(sess, max_steps: int = 500) -> bool:
    for _ in range(max_steps):
        g = sess.game
        if g.phase is Phase.ENDED:
            return True
        need = [(p.seat, g.actions_for(p.seat)) for p in g.players
                if p.kind == "human" and g.actions_for(p.seat)]
        if need:
            pick = None
            for se, ac in need:
                pts = g.get_view_for(se).get("wolf_points") or {}
                if "confirm" in ac and pts.get(se) is not None:
                    pick = (se, ac)
                    break
            seat, acts = pick or need[0]
            v = g.get_view_for(seat)
            others = [q["seat"] for q in v["players"] if q["alive"] and q["seat"] != seat]
            pts = v.get("wolf_points") or {}
            pointed = pts.get(seat) is not None
            if "speak" in acts:
                sess.act(seat, "speak", None, "我先过。")
            elif "confirm" in acts and pointed:
                sess.act(seat, "confirm", None, None)
            elif "vote" in acts and others:
                sess.act(seat, "vote", others[0], None)
            elif "kill" in acts and others:
                sess.act(seat, "kill", others[0], None)
            elif "investigate" in acts and others:
                sess.act(seat, "investigate", others[0], None)
            elif "guard" in acts and others:
                sess.act(seat, "guard", others[0], None)
            elif "heal" in acts and v.get("night_victim") is not None:
                sess.act(seat, "heal", v["night_victim"], None)
            else:
                sess.act(seat, "pass", None, None)
            continue
        sess.drive(0.05)
    return False


# 没打完的局：不该产出任何候选
_mid = game_api.Session(humans=3, ais=3, seed=3, level="硬核")
check("★ 没打完的局不产出候选（不然会把半局当教训）",
      experience.generate_candidates(_mid.game, "硬核", "x") == [], "")

_done = None
for _sd in range(1, 12):
    _s = game_api.Session(humans=3, ais=3, seed=_sd, level="硬核")
    if play_to_end(_s):
        _done = _s
        break
check("★ 能打出一局用于收割经验", _done is not None, "")

if _done is not None:
    cands = experience.generate_candidates(_done.game, "硬核", _done.id)
    check("★ 局末为 AI 座位产出候选", len(cands) > 0, f"{len(cands)} 条")
    check("★ 候选默认是 pending（**候选 ≠ 生效**）",
          all(c["status"] == "pending" for c in cands), "")
    check("★ 每条候选都带 why（复核的人要能看出它从哪来，否则评审是走过场）",
          all(c.get("why") for c in cands), str([c.get("why") for c in cands]))

    # 🔴 脱敏：不许出现座位号（这是这个功能唯一会"教错事实"的地方）
    bad = [c["text"] for c in cands if re.search(r"\d+\s*号", c["text"])]
    check("★★ 候选里一个座位号都没有（下一局座位重排 ⇒ 带座位号的教训会教错事实）",
          not bad, str(bad)[:160])
    bad2 = [c["text"] for c in cands if "是狼" in c["text"] and "我" not in c["text"]]
    check("★★ 候选不写'别人是谁'（只写我自己的行为模式）", not bad2, str(bad2)[:120])

    bank = experience.ExperienceBank()
    bank.add_candidates(cands)
    check("★ 入库前，经验不会进 prompt（离线评审这条线必须真的拦得住）",
          bank.for_prompt("硬核", "wolf") == [], "")

    first = bank.pending[0]
    bank.review(first["id"], True)
    _pulled = bank.for_prompt(first["level"], first["role"])
    check("★ 复核入库后，同档同角色才取得到",
          first["text"] in _pulled, str(_pulled)[:120])
    check("★ 档位没开就不给（娱乐档不开跨局经验）",
          bank.for_prompt("娱乐", first["role"]) == [], "")
    _other = "villager" if first["role"] == "wolf" else "wolf"
    check("★ 角色不对不给（狼的教训给好人是噪音）",
          bank.for_prompt(first["level"], _other) == [], "")

    check("重复收割不产生重复候选（_harvest 有幂等锁，这里是第二道保险）",
          bank.add_candidates(cands) == 0, "")
    check("复核一条不存在的候选 → 返回 None（不是静默成功）",
          bank.review(999999, True) is None, "")
    _snap = bank.snapshot()
    check("接口自曝'不持久化'（否则会有人以为经验丢了是 bug）",
          _snap["persistent"] is False and "重启" in _snap["note"], "")

    # 会话级：_harvest 幂等 + 只收割一次
    _h = game_api.Session(humans=3, ais=3, seed=7, level="硬核")
    if play_to_end(_h):
        _b0 = len(experience.BANK.pending) + len(experience.BANK.approved)
        _h._harvest()
        _h._harvest()
        _b1 = len(experience.BANK.pending) + len(experience.BANK.approved)
        check("★ 会话层：连调两次 _harvest 只收割一次（幂等）",
              _b1 - _b0 <= len(_h.game.players), f"{_b0}→{_b1}")
        check("★ 收割的是候选，不是自动生效的经验（不做自动进化）",
              all(x["status"] == "pending" for x in experience.BANK.pending), "")

print("\n=== 跨局经验 smoke test (批五 · G3) ===\n")
pass_n = fail_n = 0
for name, ok, detail in results:
    print(("PASS" if ok else "FAIL") + "  " + name.ljust(46)
          + ("  " + detail if detail else ""))
    if ok:
        pass_n += 1
    else:
        fail_n += 1
print("\n" + ("ALL GREEN (" + str(pass_n) + " passed)" if fail_n == 0
              else str(fail_n) + " FAILED / " + str(pass_n) + " passed"))
sys.exit(1 if fail_n else 0)
