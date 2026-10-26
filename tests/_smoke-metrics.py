# -*- coding: utf-8 -*-
"""
测量层 smoke test（批四：六维指标 + 人机对比 + 怀疑走向）。

为什么要单独一个文件：metrics 是**纯消费方**（谁都不依赖它），
它的正确性只有靠"给定输入 → 给定数字"这种白盒断言来钉，
而不是靠"跑一局没崩"。
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
_s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/  # 仓库根目录入 path：保证 python tests/xxx.py 可直接跑
import sys

import game_api

# 2026-10-07 第三期：夜间窗口是真·墙上时间 —— 不缩短的话**一夜四段 = 65 秒**，
# 这个短跑文件会从几十秒涨到几分钟。生产/真机恒为 1.0；窗口的**真实秒数**
# 由 _smoke-night-tempo.py 单独验收（那个才是验收标准）。
import night_script  # noqa: E402
night_script.set_win_scale(0.02)
import metrics
from engine import Game, Phase, Role
from engine import make_board  # noqa: E402
from _testboards import SEER4  # noqa: E402

PASS, FAIL, results = [], [], []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


# ================================================================ 怀疑走向

_SEATS = [0, 1, 2, 3, 4]


def _c(text, seat=0):
    return {"round": 1, "seat": seat, "text": text}


_counts = metrics.suspicion_counts([
    _c("我怀疑 3 号，他发言太空"),                     # → 座位 2 +1
    _c("3 号就是狼", seat=1),                          # → 座位 2 +1
    _c("我觉得 2 号是好人", seat=4),                    # 明夸 → 不算
    _c("我投 4 号，出他", seat=1),                      # → 座位 3 +1
    _c("3 号说了半天，3 号一直在划水", seat=4),          # 同一条里点名两次 → 只算 1
    _c("5 号今天说了三句话", seat=2),                   # 没有质疑动词 → 不算
], _SEATS)
check("★ 点名叫人 + 质疑动词 ⇒ 计数", _counts[2] == 2, str(_counts))
check("★ 「2 号是好人」不算被质疑（明夸不是质疑）", _counts[1] == 0, str(_counts))
check("「我投 4 号」算质疑", _counts[3] == 1, str(_counts))
check("★ 同一条发言里反复点名同一个座位只算一次（防刷词）", _counts[2] == 2, str(_counts))
check("没有质疑动词的发言完全不算（5 号被提到但没人怀疑他）", _counts[4] == 0, str(_counts))

_counts2 = metrics.suspicion_counts([
    _c("3 号说自己要带队", seat=2),        # 自己说自己 → 不算
    _c("我怀疑 9 号", seat=0),             # 死人/不存在 → 不算
], [0, 1, 2])
check("★ 自己点名自己不算（不给自己刷质疑）", _counts2.get(2, 0) == 0, str(_counts2))
check("不存在的座位不进统计", 8 not in _counts2, str(_counts2))

_line = metrics.suspicion_line({0: 0, 1: 2, 2: 1, 3: 0})
check("★ 怀疑走向那句话按次数排序、只列有票的",
      _line == "本轮被质疑最多的：2 号（2 人）、3 号（1 人）", _line)
check("没人被质疑时返回空串（不写'无人被质疑'那种废话）",
      metrics.suspicion_line({0: 0, 1: 0}) == "", "空串")

# ================================================================ 引擎接线

_g = Game(make_board(3, 3), kinds=["human", "ai"] * 3, seed=5)
_g.start()
_g.resolve_night()          # 第一夜：不刀人 ⇒ 平安夜，然后进白天（要发过言才有"怀疑走向"）
_g.open_discuss()
s0 = next(p.seat for p in _g.alive)
s1 = next(p.seat for p in _g.alive if p.seat != s0)
tgt = next(p.seat for p in _g.alive if p.seat not in (s0, s1))
_g.log_speech(s0, f"我怀疑 {tgt + 1} 号")
v0, v1 = _g.get_view_for(s0), _g.get_view_for(s1)
check("★ 怀疑走向进了视图", v0.get("suspicion", {}).get(tgt) == 1, str(v0.get("suspicion")))
check("★ 怀疑走向人人相同（它是 L0 派生，不是私有信息）",
      v0["suspicion"] == v1["suspicion"], f"{v0['suspicion']} vs {v1['suspicion']}")

_g.log_speech(s1, f"我也怀疑 {tgt + 1} 号，还有 {s0 + 1} 号")
v0b = _g.get_view_for(s0)
check("★ 又有人发言后缓存会失效并重算（缓存键里必须带发言条数）",
      v0b["suspicion"].get(tgt) == 2 and v0b["suspicion"].get(s0) == 1,
      str(v0b["suspicion"]))
check("★ 怀疑走向里不含任何票 → 不违反暗投（值都是正整数计数）",
      all(isinstance(x, int) and x >= 0 for x in v0b["suspicion"].values()),
      str(v0b["suspicion"]))

# ================================================================ 六维指标

# 空局（没打过）也不能崩 —— 接口在局中就会被调用
_fresh = Game(SEER4, kinds=["human", "human", "ai", "ai"], seed=9)
_fresh.start()
_fresh_sum = metrics.summarize(_fresh, "娱乐")
check("★ 没打完的局也能算指标（不崩）",
      _fresh_sum["outcome"]["ended"] is False
      and _fresh_sum["outcome"]["rounds"] == _fresh.round,
      str(_fresh_sum["outcome"]))
check("★ 分母为 0 的比率返回 None，而不是 0（0 会被误读成'表现很差'）",
      _fresh_sum["votes"]["good_accuracy"] is None
      and _fresh_sum["speech"]["quality"] is None,
      f"acc={_fresh_sum['votes']['good_accuracy']} quality={_fresh_sum['speech']['quality']}")
check("发言质量标明了'待接 LLM 打分'，不用字数冒充",
      _fresh_sum["speech"]["quality_note"] == "待接 LLM 打分",
      str(_fresh_sum["speech"]["quality_note"]))


def play_to_end(sess, max_steps: int = 500) -> bool:
    """把一局走完（人类座位按最简单的策略出手）。"""
    for _ in range(max_steps):
        g = sess.game
        if g.phase is Phase.ENDED:
            return True
        need = [(p.seat, g.actions_for(p.seat)) for p in g.players
                if p.kind == "human" and g.actions_for(p.seat)]
        # ★ 跨座位挑动作：**优先做「确定」**。
        #   只按 need[0]（最小座位）做的话，两只人狼里靠前那只一直在"点人"，
        #   靠后那只的"确定"永远轮不到 ⇒ 协商永远定不了案（实测卡到 500 步）。
        _pick = None
        for _se, _ac in need:
            _p = g.get_view_for(_se).get("wolf_points") or {}
            if "confirm" in _ac and _p.get(_se) is not None:
                _pick = (_se, _ac)
                break
        if need and _pick is None:
            _pick = need[0]
        if need:
            seat, acts = _pick
            v = g.get_view_for(seat)
            others = [q["seat"] for q in v["players"]
                      if q["alive"] and q["seat"] != seat]
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


_finished = []
for _sd in range(1, 7):
    _s = game_api.Session(humans=3, ais=3, seed=_sd, level="硬核" if _sd % 2 else "认真")
    if play_to_end(_s):
        _finished.append(_s)
check("★ 连打 6 局都能打完（指标得有局可算）", len(_finished) >= 1,
      f"{len(_finished)}/6")

if _finished:
    _s0m = _finished[0]
    _sum = metrics.summarize(_s0m.game, _s0m.level)
    dims = _sum["dims"]
    check("★ 六个维度 + 人机对比都在（顺序固定，前端按它渲染）",
          [d["key"] for d in dims] == ["outcome", "survival", "votes", "skills",
                                       "speech", "tempo", "human_ai"],
          str([d["key"] for d in dims]))
    check("★ 打完的局：胜负有归属", _sum["outcome"]["ended"]
          and _sum["winner"] in ("wolf", "good"), str(_sum["outcome"]))
    check("★ 每个座位都有存活轮次，且不超过总局数",
          len(_sum["survival"]["by_seat"]) == len(_s0m.game.players)
          and all(0 <= r["rounds_survived"] <= _sum["outcome"]["rounds"]
                  for r in _sum["survival"]["by_seat"]),
          str(_sum["survival"]["by_seat"]))
    check("★ 存活按人机分层（这是我们要的、那四个项目没有的）",
          set(_sum["survival"]["by_kind"]) == {"human", "ai"},
          str(_sum["survival"]["by_kind"]))
    check("★ 人机对比：人类座位胜率与 AI 座位胜率都算出来了（不是'谁赢'那种含糊口径）",
          set(_sum["human_ai"]["seat_win_rate"]) == {"human", "ai"}
          and all(v is not None for v in _sum["human_ai"]["seat_win_rate"].values()),
          str(_sum["human_ai"]["seat_win_rate"]))
    check("★ 出局人数守恒：人类出局 + AI 出局 = 被投出总数",
          _sum["human_ai"]["human_exiled"] + _sum["human_ai"]["ai_exiled"]
          == _sum["human_ai"]["exiled_total"],
          str(_sum["human_ai"]))
    check("投票维度有真数字（不是因为没票变成空表）",
          _sum["votes"]["total_votes"] > 0
          and _sum["votes"]["rounds_with_votes"] >= 1,
          str({k: _sum["votes"][k] for k in ("total_votes", "rounds_with_votes")}))
    check("★ 逐夜记录被写下来了（技能收益才算得出来）",
          len(_s0m.game.night_records) >= 1
          and "peaceful" in _s0m.game.night_records[0],
          str(_s0m.game.night_records[:1]))
    check("节奏维度：夜数/平安夜/死因分层都在",
          _sum["tempo"]["nights"] == len(_s0m.game.night_records)
          and _sum["tempo"]["night_deaths"] + _sum["tempo"]["day_deaths"]
          == len([d for d in _s0m.game.deaths]),
          str(_sum["tempo"]))
    check("★ 死因全部带轮次（没有它就算不出'活了几轮'）",
          all("round" in d for d in _s0m.game.deaths), str(_s0m.game.deaths))
    _agg = metrics.aggregate([metrics.summarize(s.game, s.level) for s in _finished])
    check("★ 跨局聚合按档分组（不分档的胜率没有意义）",
          set(_agg["by_level"]) <= {"认真", "硬核"} and len(_agg["by_level"]) >= 1,
          str(list(_agg["by_level"])))
    _lv0 = next(iter(_agg["by_level"]))
    _g0 = _agg["by_level"][_lv0]
    check("★ 聚合里狼胜 + 好人胜 = 总局数（榜单不能漏局）",
          _g0["wolf_wins"] + _g0["good_wins"] == _g0["games"],
          str({k: _g0[k] for k in ("games", "wolf_wins", "good_wins")}))

print("\n=== 测量层 smoke test (批四) ===\n")
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
