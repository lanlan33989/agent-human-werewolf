# -*- coding: utf-8 -*-
"""
战报 smoke test（批六 · G6）。

战报是**给人读**的产物，所以这里断言的不是"数字对不对"（那是 metrics 的事），
而是"**能不能读**"：小节在不在、每轮有没有被重建出来、身份表行数对不对、
而且它**绝不能因为某个字段缺失就整篇报错**（局末崩掉 = 玩家看到 500）。
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))  # 仓库根目录入 path：保证 python tests/xxx.py 可直接跑
import re
import sys

import game_api

# 2026-10-07 第三期：夜间窗口是真·墙上时间 —— 不缩短的话**一夜四段 = 65 秒**，
# 这个短跑文件会从几十秒涨到几分钟。生产/真机恒为 1.0；窗口的**真实秒数**
# 由 _smoke-night-tempo.py 单独验收（那个才是验收标准）。
import night_script  # noqa: E402
night_script.set_win_scale(0.02)
import report
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


# 局中就能生成（模块本身不是闸门 —— 拦在接口层，见 game_api 的 409）。
# 这条同时保证"接口万一漏拦也不会崩"。
_mid = game_api.Session(humans=3, ais=3, seed=5, level="认真")
_md_mid = report.markdown(_mid)
check("★ 模块本身对没打完的局也不崩（闸门在接口层，不是在这里）",
      "# 本局战报" in _md_mid and "未结束" in _md_mid, _md_mid[:60])

_done = None
for _sd in range(1, 12):
    _s = game_api.Session(humans=3, ais=3, seed=_sd, level="硬核")
    if play_to_end(_s):
        _done = _s
        break
check("★ 能打出一局用于生成战报", _done is not None, "")

if _done is not None:
    md = report.markdown(_done)
    rp = _done.replay()
    check("★ 标题带上板子/档位/局号",
          "# 本局战报" in md and (_done.game.board.label in md) and _done.id in md, md[:70])
    check("★ 六个小节齐全（能给人读，不是一堆数字）",
          all(k in md for k in ("## 关键转折", "## 身份公开（局末解密）", "## 逐轮回顾",
                                "## 六维指标", "## 人机对比", "## 本局教训")), "")
    check("★ 身份表行数 = 座位数（一个都不漏）",
          len([x for x in md.splitlines() if x.startswith("| ") and "号 |" in x])
          == len(rp["seats"]), str(len(rp["seats"])))
    check("★ 逐轮回顾覆盖到最后一轮",
          f"### 第 {int(rp['rounds'])} 轮" in md, f"rounds={rp['rounds']}")
    check("★ 关键转折非空，且每条都带轮次（可核对 —— 玩家会拿它跟记忆对）",
          len(report.turning_points(rp)) > 0
          and all(re.search(r"第 \d+ 轮", t) or "终局" in t
                  for t in report.turning_points(rp)),
          str(report.turning_points(rp)[:2]))
    check("★ 票型是结构化的（不是从 public_log 那行文本里抠的）",
          "**票**" in md and ("→" in md), "")
    check("★ 战报结尾标明'局末解密、不含场外信息'",
          "局末解密" in md and "不含任何场外信息" in md, "")
    check("没有任何未替换的占位符（None/— 堆在正文里说明字段没接上）",
          "None" not in md, "")

print("\n=== 战报 smoke test (批六 · G6) ===\n")
pass_n = fail_n = 0
for name, ok, detail in results:
    print(("PASS" if ok else "FAIL") + "  " + name.ljust(44)
          + ("  " + detail if detail else ""))
    if ok:
        pass_n += 1
    else:
        fail_n += 1
print("\n" + ("ALL GREEN (" + str(pass_n) + " passed)" if fail_n == 0
              else str(fail_n) + " FAILED / " + str(pass_n) + " passed"))
sys.exit(1 if fail_n else 0)
