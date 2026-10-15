# -*- coding: utf-8 -*-
"""
对局 API 回归（D4）。

验证的是「浏览器能不能玩完一局」，以及「接口层有没有漏私有信息」。

强制 mock 环境（这一层不涉及 ASR / LLM 调用）。

用法（在 h5 目录下）：
    ./.venv/Scripts/python.exe _smoke-game-api.py
"""
import os
import sys

os.environ["MOCK"] = "1"
os.environ["MOCK_ASR"] = "1"
os.environ["MOCK_LLM"] = "1"
os.environ["MOCK_TTS"] = "1"

sys.path.insert(0, ".")

import server  # noqa: E402

# 2026-10-07 第三期：夜间窗口是真·墙上时间 —— 不缩短的话**一夜四段 = 65 秒**，
# 这个短跑文件会从几十秒涨到几分钟。生产/真机恒为 1.0；窗口的**真实秒数**
# 由 _smoke-night-tempo.py 单独验收（那个才是验收标准）。
import night_script  # noqa: E402
night_script.set_win_scale(0.02)
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)

results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, str(detail)))


# ================================================================ 板子：只有"按人数现算"一条路

r = client.get("/api/game/boards")
check("★ 旧预设端点已删（404）—— 留着它就是留了一条绕过现算规则的旁路",
      r.status_code == 404, str(r.status_code))

r = client.post("/api/game/new", json={"board": "classic6"})
check("★ 请求里塞 board 不再能换板子（字段已删，退回按人数现算）",
      r.status_code == 200 and "dyn" in r.json()["public"]["board_key"],
      str(r.json().get("public", {}).get("board_key")))

# ⚠️ 这条原来测的是"给 board=mini4 + humans=99 ⇒ 越界"。
# 现在没有 board 了，人数由 humans+ais 现算 ⇒ "humans=99" 不再是越界（那就是一局 99 人的牌）。
# 真正越界的形态变成了：**总人数不足 4**（以及负数，见文件后面那条）。
r = client.post("/api/game/new", json={"humans": 1, "ais": 1})
check("总人数不足 4 返回 400", r.status_code == 400, str(r.status_code))

# ================================================================ 座次编排（2026-09-30 用户定，10-01 落地）
from game_api import default_layout  # noqa: E402

check("★ 默认座次：人类首位 + 人类之间均匀隔 AI（3人6AI → 1/4/7 号是人，间隔全 2）",
      default_layout(3, 6) == ["human", "ai", "ai", "human", "ai", "ai",
                               "human", "ai", "ai"],
      str(default_layout(3, 6)))
check("★ 默认座次：2人2AI → 1/3 号是人（旧的 [人,人,AI,AI] 已废）",
      default_layout(2, 2) == ["human", "ai", "human", "ai"],
      str(default_layout(2, 2)))
check("★ AI 数不够隔时退回「人类全排前位」（物理上唯一可行）",
      default_layout(3, 1) == ["human"] * 3 + ["ai"], str(default_layout(3, 1)))

r = client.post("/api/game/new", json={"humans": 3, "ais": 6})
check("★ 开局不传 layout ⇒ 后端按默认均匀排（human_seats 0 基 = [0,3,6]，即 1/4/7 号）",
      r.status_code == 200 and r.json()["human_seats"] == [0, 3, 6],
      str(r.json().get("human_seats")))

r = client.post("/api/game/new",
                json={"humans": 2, "ais": 2,
                      "layout": ["ai", "human", "human", "ai"]})
check("★ 拖拽编排：layout 照单全收（human_seats 0 基 = [1,2]，即 2/3 号）",
      r.status_code == 200 and r.json()["human_seats"] == [1, 2],
      str(r.json().get("human_seats")))

r = client.post("/api/game/new",
                json={"humans": 2, "ais": 2,
                      "layout": ["human", "human", "human", "ai"]})
check("★ layout 的人类数与 humans 对不上 ⇒ 400（不许静默改排法）",
      r.status_code == 400, str(r.status_code))

# ================================================================ 开局

r = client.post("/api/game/new", json={"humans": 2, "ais": 2, "seed": 11})
check("开局返回 200", r.status_code == 200, str(r.status_code))
data = r.json()
gid = data["gid"]
humans = data["human_seats"]

check("返回 4 个座位", len(data["seats"]) == 4, str(len(data["seats"])))
check("真人数正确", len(humans) == 2, str(humans))
check("初始 pending 是需要人类输入的状态",
      data["public"]["pending"] in ("human_night", "human_speak", "human_vote", "ended"),
      data["public"]["pending"])

# ================================================================ 视图隔离

r = client.get(f"/api/game/{gid}/seat/0")
check("取座位视图返回 200", r.status_code == 200, str(r.status_code))

r = client.get(f"/api/game/{gid}/seat/99")
check("座位越界返回 400", r.status_code == 400, str(r.status_code))

# 换一个有多狼的板子，专门验「狼队组内共享」在 HTTP 层也成立
r2 = client.post("/api/game/new", json={"humans": 3, "ais": 3, "seed": 5})
gid2 = r2.json()["gid"]
pub2 = client.get(f"/api/game/{gid2}/public").json()
check("公共状态里不含 team_mates", "team_mates" not in str(pub2.keys()))

leaked = False
for s in [p["seat"] for p in pub2["players"]]:
    v = client.get(f"/api/game/{gid2}/seat/{s}").json()
    # 公共接口不该出现私有字段名带值的组合
    if "team_mates" in v and v["team_mates"]:
        # 只有狼才该有队友；这里无法判断 s 是不是狼，故只记录
        pass
check("公共状态字段里没有 investigations / potions",
      "investigations" not in pub2 and "potions" not in pub2,
      str(sorted(pub2.keys())))

# ================================================================ 不能操作 AI 座位

ai_seat = next(p["seat"] for p in data["seats"] if p["kind"] == "ai")
r = client.post(f"/api/game/{gid}/act",
                json={"seat": ai_seat, "action": "pass"})
check("不能手动操作 AI 座位（返回 400）", r.status_code == 400, str(r.status_code))

# ================================================================ 自动走完一局


def auto_play(gid: str, human_seats, max_rounds: int = 120):
    """一个"机器人真人"：每次看到自己能做什么就做什么。"""
    for _ in range(max_rounds):
        pub = client.get(f"/api/game/{gid}/public").json()
        if pub["phase"] == "ended":
            return pub

        pending = pub["pending"]

        if pending == "human_speak":
            spk = pub["current_speaker"]
            r = client.post(f"/api/game/{gid}/act",
                            json={"seat": spk, "action": "speak", "text": "我先过。"})
            if r.status_code != 200:
                return {"error": r.text, "phase": pub["phase"]}
            continue

        if pending == "human_vote":
            for seat in human_seats:
                v = client.get(f"/api/game/{gid}/seat/{seat}").json()
                if "vote" in v.get("can_act", []):
                    others = [p["seat"] for p in v["players"]
                              if p["alive"] and p["seat"] != seat]
                    if others:
                        client.post(f"/api/game/{gid}/act",
                                    json={"seat": seat, "action": "vote",
                                          "target": others[0]})
            continue

        if pending == "human_night":
            acted = False
            for seat in human_seats:
                v = client.get(f"/api/game/{gid}/seat/{seat}").json()
                acts = v.get("can_act", [])
                others = [p["seat"] for p in v["players"]
                          if p["alive"] and p["seat"] != seat]
                # ⚠️ 狼队协商是**两步**：先点人（kill），再确定（confirm）。
                #    只点不确定 ⇒ 刀口永远定不下来 ⇒ 这一夜推不下去。
                #    判"点过没有"要靠视图里的 wolf_points（不能只看 can_act：
                #    未定案前 kill 一直在 can_act 里，会一直点下去）。
                _pts = v.get("wolf_points") or {}
                _pointed = any(str(k) == str(seat) and _pts[k] is not None for k in _pts)
                if "kill" in acts and others and not _pointed:
                    client.post(f"/api/game/{gid}/act",
                                json={"seat": seat, "action": "kill",
                                      "target": others[0]})
                    acted = True
                    break
                if "confirm" in acts and _pointed:
                    client.post(f"/api/game/{gid}/act",
                                json={"seat": seat, "action": "confirm"})
                    acted = True
                    break
                for a in ("guard", "investigate"):
                    if a in acts and others:
                        pool = [s for s in others if s != v.get("guard_last")]
                        client.post(f"/api/game/{gid}/act",
                                    json={"seat": seat, "action": a,
                                          "target": (pool or others)[0]})
                        acted = True
                        break
                if acted:
                    break
                if "heal" in acts and v.get("night_victim") is not None:
                    client.post(f"/api/game/{gid}/act",
                                json={"seat": seat, "action": "heal",
                                      "target": v["night_victim"]})
                    acted = True
                    break
                if "heal" in acts or "poison" in acts:
                    client.post(f"/api/game/{gid}/act",
                                json={"seat": seat, "action": "pass"})
                    acted = True
                    break
            if not acted:
                # 人类没有夜间动作可做（例如村民），推一下让 AI 继续
                for seat in human_seats:
                    client.post(f"/api/game/{gid}/act",
                                json={"seat": seat, "action": "pass"})
            continue

    return client.get(f"/api/game/{gid}/public").json()


final = auto_play(gid, humans)
check("mini4 一局能自动走完",
      final.get("phase") == "ended", f"phase={final.get('phase')} {final.get('error','')}")
check("结束后有胜负", final.get("winner") in ("wolf", "good"), str(final.get("winner")))
check("有公开状态播报", len(final.get("public_log", [])) > 3,
      str(len(final.get("public_log", []))))
check("有玩家发言记录", len(final.get("speech", [])) > 0,
      str(final.get("speech", [])[:2]))

# ================================================================ 多局连跑

ok_runs = 0
for seed in range(1, 11):
    r = client.post("/api/game/new", json={"humans": 2, "ais": 2, "seed": seed})
    if r.status_code != 200:
        continue
    d = r.json()
    f = auto_play(d["gid"], d["human_seats"])
    if f.get("phase") == "ended":
        ok_runs += 1
check("mini4 连跑 10 局全部正常结束", ok_runs == 10, f"{ok_runs}/10")

ok_runs6 = 0
for seed in range(1, 11):
    r = client.post("/api/game/new", json={"humans": 3, "ais": 3, "seed": seed})
    if r.status_code != 200:
        continue
    d = r.json()
    f = auto_play(d["gid"], d["human_seats"])
    if f.get("phase") == "ended":
        ok_runs6 += 1
check("classic6 连跑 10 局全部正常结束", ok_runs6 == 10, f"{ok_runs6}/10")

# ================================================================ 404

r = client.get("/api/game/nonexistent/public")
check("不存在的局返回 404", r.status_code == 404, str(r.status_code))

# ================================================================ 投票时序（单机形态的投票独立性）
# 单机是「交接式」的：手机要递到投票人手里。如果 /public 在投票还没结束时就
# 把已投的票吐出来，那手机递过去的一瞬间投票独立性就没了 ——
# 看得到别人的票就能跟随或反制。而 AI 的票是在 auto_advance 内部自动投的，
# 是最容易漏掉的一条路径，所以下面特意单独断言它。


def drive_to_human_vote(gid: str, human_seats, max_steps: int = 200):
    """把一局推到「还有人没投票」的状态，途中自动处理发言与夜间。"""
    for _ in range(max_steps):
        pub = client.get(f"/api/game/{gid}/public").json()
        if pub["phase"] == "ended" or pub["pending"] == "human_vote":
            return pub
        if pub["pending"] == "human_speak":
            client.post(f"/api/game/{gid}/act",
                        json={"seat": pub["current_speaker"], "action": "speak",
                              "text": "我先过。"})
            continue
        if pub["pending"] == "human_night":
            acted = False
            for seat in human_seats:
                v = client.get(f"/api/game/{gid}/seat/{seat}").json()
                acts = v.get("can_act", [])
                for a in ("kill", "investigate", "heal", "poison"):
                    if a in acts:
                        if a == "heal":
                            # 解药只能救今晚被刀的那一个（引擎会拒别人），
                            # 所以这里必须用视图里的 night_victim，不能随便挑一个
                            tgt = v.get("night_victim")
                            if tgt is None:
                                continue
                        else:
                            others = [p["seat"] for p in v["players"]
                                      if p["alive"] and p["seat"] != seat]
                            tgt = others[0] if others else -1
                        client.post(f"/api/game/{gid}/act",
                                    json={"seat": seat, "action": a, "target": tgt})
                        acted = True
                        break
                if acted:
                    break
            if not acted:
                for seat in human_seats:
                    client.post(f"/api/game/{gid}/act",
                                json={"seat": seat, "action": "pass"})
            continue
        break
    return client.get(f"/api/game/{gid}/public").json()


# 采样点选「pending == human_vote」这一刻，而不是"投完一票之后"。
# 那一刻 AI 已经全投完、人类一个都还没投 ⇒ **必然**是"投票进行中"。
#
# ⚠️ 原来这条用的是"投一票之后再采样"，它隐含了一个前提：
#    「投完之后还有另一个人类没投」。2026-09-15 改成「一人一条命」之后这个前提不成立 ——
#    夜里被刀的人真的出局，4 人局可能只剩 1 个人类，他一投就立刻进结算，
#    采样点会落到下一个夜晚（断言假红：phase=night）。
# 同时要求那一刻**至少有一个 AI 已经投完**，否则这条断言会空转通过。
gidv = pubv = seatsv = humansv = None
for _seed in range(40):
    _rv = client.post("/api/game/new",
                      json={"humans": 2, "ais": 2, "seed": _seed}).json()
    _pub = drive_to_human_vote(_rv["gid"], _rv["human_seats"])
    if _pub.get("pending") == "human_vote" and len(_pub.get("voted_seats") or []) >= 1:
        gidv, pubv = _rv["gid"], _pub
        seatsv, humansv = _rv["seats"], _rv["human_seats"]
        break

check("能造出「AI 已投完、人类还没投」的采样点（造不出，下面几条会空转通过）",
      pubv is not None, "40 个种子里没造出来")

if pubv is not None:
    voted = sorted(pubv.get("voted_seats") or [])
    alive_ai = sorted(p["seat"] for p in pubv["players"]
                      if p["alive"] and p["kind"] == "ai")
    check("投票进行中，public 不返回票型（防交接时被看到）",
          pubv.get("votes") == {},
          f"phase={pubv.get('phase')} votes={pubv.get('votes')}")
    check("投票进行中，public 只说明「谁投完了」（正好是投完的那些 AI）",
          voted == alive_ai, f"voted_seats={voted} 期望={alive_ai}")
    check("上一条不是「还没人投票」造成的假通过（确实已经有人投了）",
          len(voted) >= 1 and pubv.get("votes") == {},
          f"已投 {len(voted)} 人 / votes={pubv.get('votes')}")

    voter = next((s for s in humansv
                  if "vote" in client.get(f"/api/game/{gidv}/seat/{s}")
                  .json().get("can_act", [])), humansv[0])
    view_mid = client.get(f"/api/game/{gidv}/seat/{voter}").json()
    check("座位视图（走 get_view_for）里始终没有票字段",
          "votes" not in view_mid and "voted_seats" not in view_mid,
          str([k for k in view_mid if "vote" in k.lower()]))

    # 反过来也要断言：票型**必须**在投票结束后公布。
    # 只写"进行中不给"是半条断言 —— 它允许实现把票型永久藏起来，那样投票板就没数据了。
    # 而 exile 在 auto_advance 里是瞬时态，所以公布点不能只认 exile。
    alive_at_vote = sorted(p["seat"] for p in pubv["players"] if p["alive"])
    for seat in humansv:
        v = client.get(f"/api/game/{gidv}/seat/{seat}").json()
        if "vote" in v.get("can_act", []):
            others = [p["seat"] for p in v["players"]
                      if p["alive"] and p["seat"] != seat]
            client.post(f"/api/game/{gidv}/act",
                        json={"seat": seat, "action": "vote",
                              "target": others[0] if others else -1})
    after = client.get(f"/api/game/{gidv}/public").json()
    check("投票结束后公布本轮完整票型（只认 exile 会永远公布不了）",
          after.get("phase") != "vote"
          and set(after.get("votes") or {}) == {str(s) for s in alive_at_vote},
          f"phase={after.get('phase')} votes={after.get('votes')} 期望键={alive_at_vote}")

# ================================================================ AI 玩家用哪个 Agent
# 「接真 LLM」的关键不是填 .env，而是这个工厂：game_api 默认给的是规则发言器。
# 所以两条都要断言 —— 没注入时行为必须与"没接模型"完全一致；
# 注入后必须真的换掉。只测一条都会漏。

import game_api as _ga  # noqa: E402

_saved_factory = _ga.AI_AGENT_FACTORY
try:
    _ga.AI_AGENT_FACTORY = None
    _s0 = _ga.Session(humans=2, ais=2, seed=1)
    _names0 = {type(x).__name__ for x in _s0.agents.values()}
    check("没注入工厂时 AI 用规则发言器（不接模型的行为不变）",
          _names0 == {"RuleAgent"}, str(_names0))

    class _MarkerAgent:
        """
        标记用的假 Agent：能构造、能应答就够（这个测试只验工厂注入）。

        ⚠️ `act_on_target` 是 2026-09-15 才必须实现的：在那之前，投票/刀人/验人
        根本不走 Agent（驱动器直接 `rng.choice`），所以只实现 `speak` 也能过。
        现在决策也走 Agent 了，缺这个方法会直接在构造 Session 时就报错。
        """

        def __init__(self, seat, rng):
            from agents import Decision
            self.seat = seat
            self._Decision = Decision

        def speak(self, view, history):
            return self._Decision(text="（标记 Agent 的发言）")

        def act_on_target(self, view, history, kind):
            others = [p["seat"] for p in view["players"]
                      if p["alive"] and p["seat"] != self.seat]
            return self._Decision(target=others[0] if others else None)

    _ga.AI_AGENT_FACTORY = lambda seat, rng: _MarkerAgent(seat, rng)
    _s1 = _ga.Session(humans=2, ais=2, seed=1)
    _names1 = {type(x).__name__ for x in _s1.agents.values()}
    check("注入工厂后 AI 座位换成注入的 Agent（接模型走的就是这条路）",
          _names1 == {"_MarkerAgent"}, str(_names1))
finally:
    _ga.AI_AGENT_FACTORY = _saved_factory

# ================================================================ 按人数现算板子（2026-09-15）
# 用户改的组局方式：界面不再给固定板子，只给「几个真人 + 几个 AI」。
# 这条走的是界面真正调的那条路（不带 board、只带人数）。
_dyn = client.post("/api/game/new", json={"humans": 2, "ais": 4})
check("只给人数（不给 board）就能开局", _dyn.status_code == 200, _dyn.text[:150])
_d = _dyn.json() if _dyn.status_code == 200 else {}
check("按规则现算：6 人局 = 2 狼 1 巫 1 预 2 民（狼 = floor(6×0.34)，神数 = 狼数）",
      _d.get("board") == "6 人 · 2 狼 1 巫 1 预 2 民", str(_d.get("board")))
check("返回的座位数 = 真人 + AI", len(_d.get("seats") or []) == 6, str(len(_d.get("seats") or [])))
check("真人数正确（人类座位只有 2 个）",
      len(_d.get("human_seats") or []) == 2, str(_d.get("human_seats")))

_r0h = client.post("/api/game/new", json={"humans": 0, "ais": 4})
check("★ 0 个真人 ⇒ 200（全 AI 局，离线评测用；界面自己有'至少 1 个真人'的规则）",
      _r0h.status_code == 200 and not (_r0h.json().get("human_seats") or []),
      str(_r0h.status_code))
_r400h = client.post("/api/game/new", json={"humans": -1, "ais": 5})
check("负数人数 → 400（这才是'不能悄悄开一局'）", _r400h.status_code == 400, str(_r400h.status_code))

_rules = client.get("/api/game/rules")
check("组局规则端点返回 200", _rules.status_code == 200, str(_rules.status_code))
_rj = _rules.json() if _rules.status_code == 200 else {}
check("规则里带了 34% 与神职启用顺序（神数 = 狼数）",
      _rj.get("wolf_ratio") == 0.34
      and _rj.get("god_order") == ["witch", "seer", "hunter", "guard", "idiot"],
      str(_rj)[:170])
check("preview 里 9 人局标了 3 神（女巫+预言家+猎人）",
      len((_rj.get("preview") or {}).get("9", {}).get("gods", [])) == 3,
      str((_rj.get("preview") or {}).get("9")))

# ================================================================ AI 决策走 Agent（2026-09-15 修）
# 用户实测报过「AI 的投票跟场上发言毫无关系、刀人像是顺时针」。
# 根因：投票 / 刀人 / 验人**根本没调模型**，驱动器直接 rng.choice，只有发言走了模型。
# 这条把「决策必须经过 Agent」钉住 —— 以后谁把它改回 rng，这里会红。
_decision_calls = []


class _SpyAgent:
    """把驱动器交给它的每一次决策记下来。"""

    def __init__(self, seat, rng):
        from agents import Decision
        self.seat = seat
        self._D = Decision

    def speak(self, view, history):
        _decision_calls.append((self.seat, "speak"))
        return self._D(text="（spy 的发言）")

    def act_on_target(self, view, history, kind):
        _decision_calls.append((self.seat, kind))
        targets = [p["seat"] for p in view["players"]
                   if p["alive"] and p["seat"] != self.seat]
        return self._D(target=targets[0] if targets else None)

    # ⚠️ 决策入口不止 act_on_target：2026-09-17 起
    #   狼刀（negotiation）/ 投票 / 守卫 / 猎人开枪 / 女巫用药都走 decide_target / decide_yes。
    #   替身没实现它们时，驱动器会**静默**退化成兜底 —— 这条断言就是这么红的
    #   （表现为"AI 的决策没经过 Agent"，其实是替身缺方法）。
    def decide_target(self, view, history, kind):
        _decision_calls.append((self.seat, kind))
        targets = [p["seat"] for p in view["players"]
                   if p["alive"] and p["seat"] != self.seat]
        return self._D(target=targets[0] if targets else None)

    def decide_yes(self, view, history, kind):
        _decision_calls.append((self.seat, kind))
        return self._D(yes=False)


_saved_spy = _ga.AI_AGENT_FACTORY
try:
    _ga.AI_AGENT_FACTORY = lambda seat, rng: _SpyAgent(seat, rng)
    _spy = _ga.Session(humans=2, ais=2, seed=7)
    _gsp = _spy.game
    for _ in range(80):
        _pend = _spy.pending
        if _pend in ("ended", "human_vote"):
            break                       # 到投票就等于 AI 已经投过了
        if _pend == "human_speak":
            _spy.act(_gsp.current_speaker(), "speak", None, "我先过")
        elif _pend == "human_night":
            _actor = next((q for q in _gsp.players if q.kind == "human" and q.alive
                           and _gsp.actions_for(q.seat)), None)
            if _actor is None:
                break
            _acts = _gsp.actions_for(_actor.seat)
            _vw = _gsp.get_view_for(_actor.seat)
            _tgt = (_vw.get("night_victim") if _acts[0] == "heal"
                    else next((q.seat for q in _gsp.alive if q.seat != _actor.seat), -1))
            # 比整份 night 而不只是 kill/witch_asked —— 只看那两个的话，
            # 人类预言家验完人会被误判成"没推进"而提前退出（漏掉验人键）。
            _before = dict(_gsp.night)
            _spy.act(_actor.seat, _acts[0], _tgt, None)
            if _gsp.night == _before:
                break                   # 这一步真的没推进，别在这儿空转
        elif _pend == "working":
            # 🔴 2026-10-07 第三期：夜间节点时钟走**墙上时间**（读秒不可跳过）——
            # 这时 pending 就是 working，**不是**"没事可干"，得接着推。
            _spy.drive(0.3)
        else:
            break
finally:
    _ga.AI_AGENT_FACTORY = _saved_spy

_spy_kinds = sorted({k for _s, k in _decision_calls})
check("★ AI 的决策（刀人/验人/投票）真的经过 Agent —— 不是驱动器随机挑的",
      bool({"kill", "investigate", "vote"} & set(_spy_kinds)), str(_spy_kinds))
check("★ AI 发言走的是同一个 Agent 入口", "speak" in _spy_kinds, str(_spy_kinds))

# ================================================================ 输出

# ================================================================ 会话淘汰策略（2026-09-16 修）
# 实测事故：**正在打的那一局被挤掉** ⇒ 页面开始拿 404 ⇒ 而页面轮询当时是静默 return 的
# ⇒ 界面永久冻住、一句提示都没有（用户报的"卡住"就是这个）。
# 所以淘汰必须按「最近使用」，不能按「创建顺序」。
_ga.SESSIONS.clear()
_gids = [client.post("/api/game/new",
                     json={"humans": 2, "ais": 2}).json()["gid"]
         for _ in range(_ga.MAX_SESSIONS)]
_keep = _gids[0]                                  # 最老的那一局
check("正好装满时，最老的一局还在（先把前提立住）",
      client.get(f"/api/game/{_keep}/public").status_code == 200, _keep)
client.get(f"/api/game/{_keep}/public")            # 摸一下：它现在算"最近使用"
client.post("/api/game/new", json={"humans": 2, "ais": 2})   # 这一步会触发淘汰
check("★ 淘汰挑的是最久没被访问的局 —— 刚摸过的那一局必须还在",
      client.get(f"/api/game/{_keep}/public").status_code == 200,
      "刚被访问过的局被挤掉了 ⇒ 那个页面会开始 404 ⇒ 界面冻住")
check("确实淘汰了别的局（不是压根没淘汰）",
      len([g for g in _gids if g not in _ga.SESSIONS]) >= 1,
      f"还在的 = {len(_ga.SESSIONS)}")

print("\n=== 对局 API smoke test (D4) ===\n")

# ================================================================ 四档氛围（2026-09-17）
_r_lvr = client.get("/api/game/rules")
_rj_lvr = _r_lvr.json() if _r_lvr.status_code == 200 else {}
_lv_keys = [x["key"] for x in (_rj_lvr.get("levels") or [])]
check("★ /api/game/rules 返回四档 + 默认档（前端靠它生成按钮，不写死档位名）",
      _lv_keys == ["娱乐", "认真", "硬核", "地狱"] and bool(_rj_lvr.get("default_level")),
      str(_lv_keys))

_r_hell = client.post("/api/game/new", json={"humans": 2, "ais": 2, "level": "地狱"})
check("★ 能带档位开局", _r_hell.status_code == 200, _r_hell.text[:120])
if _r_hell.status_code == 200:
    _pub_hell = client.get(f"/api/game/{_r_hell.json()['gid']}/public").json()
    check("★ 公共状态里带着这一局的档位（将来战报/六维指标必须按档分组）",
          _pub_hell.get("level") == "地狱", str(_pub_hell.get("level")))

_r_badlv = client.post("/api/game/new",
                       json={"humans": 2, "ais": 2, "level": "地狱模式"})
check("★ 未知档位返回 400（不静默退回默认档）",
      _r_badlv.status_code == 400, _r_badlv.text[:120])

# ================================================================ 测量层接口（批四）
# ⚠️ 不能用文件开头那个 gid：上面的"会话淘汰"测试清过 SESSIONS，它已经 404 了。
_r_m = client.post("/api/game/new", json={"humans": 2, "ais": 2, "level": "认真"})
_gid_m = _r_m.json()["gid"]

_r_sc = client.get(f"/api/game/{_gid_m}/scoreboard")
check("★ 记分板接口可用（局中也能查，就是「截至此刻」的快照）",
      _r_sc.status_code == 200, _r_sc.text[:120])
if _r_sc.status_code == 200:
    _sb = _r_sc.json()
    check("★ 记分板：六个维度 + 人机对比，顺序固定",
          [d["key"] for d in _sb["dims"]] == ["outcome", "survival", "votes", "skills",
                                              "speech", "tempo", "human_ai"],
          str([d["key"] for d in _sb["dims"]]))
    check("★ 记分板带着这一局的档位（不分档的胜率没有意义）",
          _sb["level"] == "认真", str(_sb["level"]))
    check("记分板里给得出人机对比的两个数",
          set(_sb["human_ai"]["seat_win_rate"]) == {"human", "ai"},
          str(_sb["human_ai"]["seat_win_rate"]))

_r_st = client.get("/api/game/stats")
check("★ 跨局聚合（榜单 v0）可用，且按档分组",
      _r_st.status_code == 200 and "by_level" in _r_st.json(),
      _r_st.text[:120])

_pub_susp = client.get(f"/api/game/{_gid_m}/public").json()
check("★ 公共状态里带「怀疑走向」（L0 派生，人人相同、不泄露任何票）",
      isinstance(_pub_susp.get("suspicion"), dict)
      and all(isinstance(v, int) for v in _pub_susp["suspicion"].values()),
      str(_pub_susp.get("suspicion"))[:90])

# ================================================================ 结构化日志 + 经验（批五）
import re as _re5  # noqa: E402

_r_a = client.post("/api/game/new", json={"humans": 2, "ais": 2})
_gid_a = _r_a.json()["gid"]
_rp_mid = client.get(f"/api/game/{_gid_a}/replay")
check("★★ 局中取复盘日志被拒（409）—— 它是全知视角，含所有人的身份",
      _rp_mid.status_code == 409, str(_rp_mid.status_code))

_fin_a = auto_play(_gid_a, _r_a.json()["human_seats"])
check("（前提）这一局打完了", _fin_a.get("phase") == "ended", str(_fin_a.get("phase")))

_rp = client.get(f"/api/game/{_gid_a}/replay")
check("★ 局末可以取结构化对局日志", _rp.status_code == 200, _rp.text[:120])
if _rp.status_code == 200:
    _rpj = _rp.json()
    check("★ 日志里座位/时间线/票型/死因/逐夜/指标都在（一份 JSON 顶三处解析）",
          all(k in _rpj for k in ("seats", "timeline", "votes", "deaths",
                                  "nights", "metrics"))
          and all("role" in s for s in _rpj["seats"]),
          str(sorted(_rpj.keys())))
    check("★ 票型是结构化字段（不再是 public_log 里那一行文本）",
          isinstance(_rpj["votes"], list)
          and (not _rpj["votes"] or isinstance(_rpj["votes"][0]["votes"], dict)),
          str(_rpj["votes"])[:110])

_r_exp = client.get("/api/game/experience")
check("★ 经验接口可用，且自曝「不持久化」",
      _r_exp.status_code == 200 and _r_exp.json()["persistent"] is False,
      _r_exp.text[:110])
_bank5 = _r_exp.json()
check("★ 打完一局后收割到了候选（离线评审队列）",
      len(_bank5["pending"]) > 0, f"{len(_bank5['pending'])} 条")
check("★ 候选里不含座位号（脱敏：下一局座位会重排）",
      not any(_re5.search(r"\d+\s*号", x["text"]) for x in _bank5["pending"]), "")
if _bank5["pending"]:
    _cid5 = _bank5["pending"][0]["id"]
    _rv5 = client.post("/api/game/experience/review", json={"id": _cid5, "approve": True})
    check("★ 复核入库（approve）成功",
          _rv5.status_code == 200 and len(_rv5.json()["bank"]["approved"]) >= 1,
          _rv5.text[:110])
    check("复核不存在的候选 → 404，不是静默成功",
          client.post("/api/game/experience/review",
                      json={"id": 999999}).status_code == 404, "")

# ================================================================ 批六：规则开关 / 榜单 / 战报
import game_api as _ga6  # noqa: E402

_set0 = client.get("/api/game/settings")
check("★ 规则开关接口返回**整张表** + 当前值（前端按表渲染，不写死键名/选项）",
      _set0.status_code == 200
      and isinstance(_set0.json().get("switches"), list)
      and len(_set0.json()["switches"]) >= 3
      and all(("key" in s and "type" in s and "label" in s) for s in _set0.json()["switches"]),
      str(_set0.json().get("current")))

_bad_set = client.post("/api/game/settings", json={"settings": {"no_such_rule": True}})
check("★ 未知规则开关 → 400（不静默忽略：静默的表现是「设置成功但没生效」）",
      _bad_set.status_code == 400, _bad_set.text[:110])
_bad_val = client.post("/api/game/settings", json={"settings": {"tie_vote_rule": "随便"}})
check("★ 选项类开关的非法值 → 400", _bad_val.status_code == 400, _bad_val.text[:110])
_ok_set = client.post("/api/game/settings", json={"settings": {"guard_can_repeat": True}})
check("★ 合法设置能存下并回显",
      _ok_set.status_code == 200
      and _ok_set.json()["current"].get("guard_can_repeat") is True, _ok_set.text[:110])
_ga6.SETTINGS.clear()          # 复位：本文件后面还有用例，别把开关留着影响它们

_g6 = client.get("/api/game/games").json()
check("★ 对局清单只列已结束的（还在打的那局不在里面）",
      bool(_g6["games"]) and all(g["winner"] in ("wolf", "good") for g in _g6["games"]),
      str([g["gid"] for g in _g6["games"]][:3]))
check("★ 已结束的那局在清单里，且带战报链接",
      any(g["gid"] == _gid_a and g["report"].endswith("report.md")
          for g in _g6["games"]), str([g["gid"] for g in _g6["games"]][:3]))

_rp_mid6 = client.get(f"/api/game/{_gid_m}/report.md")
check("★★ 局中取战报被拒（409）—— 战报会解密所有人的身份",
      _rp_mid6.status_code == 409, str(_rp_mid6.status_code))
_md6 = client.get(f"/api/game/{_gid_a}/report.md")
check("★ 局末能取战报（Markdown）",
      _md6.status_code == 200 and "# 本局战报" in _md6.text, _md6.text[:90])
check("★ 战报含全部小节（转折/身份/逐轮/指标/人机/教训）",
      all(k in _md6.text for k in ("## 关键转折", "## 身份公开（局末解密）", "## 逐轮回顾",
                                  "## 六维指标", "## 人机对比", "## 本局教训")),
      "")

_bd6 = client.get("/board")
check("★ 榜单页可访问（纯展示页，不碰对局状态）",
      _bd6.status_code == 200 and "榜单" in _bd6.text, str(_bd6.status_code))
check("★ 榜单页里引用了两个数据源（stats + games）",
      "/api/game/stats" in _bd6.text and "/api/game/games" in _bd6.text, "")

# ================================================================ 发言方式的开关（2026-09-18）
# 挑战式发言顺序（被点名的人先回应）走的是**既有那条规则开关通路** ——
# 加一条表项，前端下拉自动就有了（前端不写死键名，见 play.html 的 renderSwitches）。
_r_set = client.get("/api/game/settings")
_sw = {s["key"]: s for s in _r_set.json().get("switches", [])}
check("★ 规则开关表里有「发言方式」，是二选一（前端自动渲染成下拉，不用改前端）",
      "speech_mode" in _sw and _sw["speech_mode"]["type"] == "choice"
      and [o["value"] for o in _sw["speech_mode"]["options"]] == ["rotation", "challenge"],
      str(_sw.get("speech_mode"))[:140])

_r_bad_mode = client.post("/api/game/settings", json={"settings": {"speech_mode": "随便"}})
check("★ 非法的发言方式被拒（不静默退回默认 —— 静默的话玩家选了小讨论却拿到普通局）",
      _r_bad_mode.status_code == 400, _r_bad_mode.text[:120])

_r_ok_mode = client.post("/api/game/settings", json={"settings": {"speech_mode": "challenge"}})
check("★ 能设置成「被点名的人先回应」", _r_ok_mode.status_code == 200, _r_ok_mode.text[:120])
_r_sp = client.post("/api/game/new", json={"humans": 2, "ais": 2})
_gid_sp = _r_sp.json()["gid"] if _r_sp.status_code == 200 else None
if _gid_sp:
    check("★ 新开的局真的带着这个发言方式（不是设置存了、开局没用上）",
          client.get(f"/api/game/{_gid_sp}/public").json().get("speech_mode") == "challenge",
          str(client.get(f"/api/game/{_gid_sp}/public").json().get("speech_mode")))
client.post("/api/game/settings", json={"settings": {"speech_mode": "rotation"}})   # 复原

# ================================================================ 手机入口（2026-09-19）
# 手机上要用**局域网 https** 打开：`http://<局域网IP>` 不是安全上下文，
# 浏览器会把麦克风接口直接收走（连授权弹窗都不给，看着就像页面坏了）。
# 所以这个接口必须给 https 地址，且给出证书提示（自签证书会警告）。
_r_net = client.get("/api/net")
_n = _r_net.json()
check("★ /api/net 给出手机可用的入口，且必须是 **https**（http 的局域网地址拿不到麦克风）",
      _r_net.status_code == 200 and _n.get("phones")
      and all(p.startswith("https://") for p in _n["phones"]), str(_n)[:150])
check("……且入口直接落到对局页 /play（省得用户自己拼路径）",
      all("/play" in p for p in (_n.get("phones") or [])), str(_n.get("phones")))
check("……提示里要写清「证书是自签的、要点继续访问」（否则用户以为页面坏了）",
      "证书" in str(_n.get("note", "")), str(_n.get("note"))[:80])

# ---- 票型 / 播报的 check 都写在计数块之后了（2026-10-02）----
# ⚠️ 计数与汇总块已挪到文件最末尾 —— 它原来在这里，而后来往文件尾部追加的
#    check 全部落在了它后面：不计数、失败也不影响退出码（等于白跑）。
#    教训写在这：**再往后追加 check 时，计数块永远要在它们后面。**

# 票型注入走**引擎视图**（2026-10-02）：任何视图消费者都拿得到，
# 不需要发言/投票/预案各路径重复装饰（第一版就写重复了，被这里拦下来）。
check("★ 票型字段在引擎视图上（与 vote_patterns() 同源）",
      _gsp.get_view_for(next(p.seat for p in _gsp.alive)).get("vote_patterns")
      == _gsp.vote_patterns(), "-")

# ================================================================ 法官播报「N号请发言」（2026-10-02 用户定）
# 「x号（该玩家自己说）」改为「x号请发言（法官说）」：报名字归法官。
# 三条硬约束：①每回合恰好一次 ②用法官音色 ③**不进 public_log**
#（public_log 会喂给 AI —— history_text 从它来，塞满这句是纯噪音）。
import agents as _ag_ann                                    # noqa: PLC0415

_sann = _ga.Session(humans=0, ais=4, seed=23)
for _ in range(40):
    # 🔴 2026-10-07 第三期：夜里是**固定节拍**（读秒不可跳过）——推的判据改成
    # "攒够两条「请发言」"，而不是"相位变成 DISCUSS"（一次 drive 可能直接跨过去）。
    _pd = _sann.drive(0.2)
    if len([x for x in _sann.timeline if x["s"].endswith("请发言")]) >= 2:
        break
    if _pd == "ended":
        break
_ann = [x for x in _sann.timeline if x["s"].endswith("请发言")]
check("★ 法官播报「N号请发言」：至少两回合、每条都带法官音色",
      len(_ann) >= 2 and all(x["voice"] == _ag_ann.JUDGE_VOICE for x in _ann),
      str([x["s"] for x in _ann][:6]))
check("★ 「请发言」**不进 public_log**（那是喂 AI 的，进去就是噪音）",
      not any("请发言" in s for s in _sann.game.public_log),
      str([s for s in _sann.game.public_log if "请发言" in s])[:80])
_ok_order = True
_last_ann = None
for x in _sann.timeline:
    if x["t"] == "sys" and x["s"].endswith("请发言"):
        _last_ann = x["s"]
    if x["t"] == "say":
        if _last_ann != str(x["seat"] + 1) + "号请发言":
            _ok_order = False
            break
check("★ 每条发言紧挨着的前一条就是该座位的「N号请发言」（顺序不能倒）",
      _ok_order, "")

if final.get("speech"):
    print("\n--- 最后一局的发言记录（前 6 条）---")
    for line in final["speech"][:6]:
        print("   ", line)
if final.get("public_log"):
    print("\n--- 最后一局的公共播报 ---")
    for line in final["public_log"]:
        print("   ", line)

# ================================================================ 夜间法官播报（2026-10-05 用户定）
_sn = _ga.Session(humans=0, ais=9, seed=7)
for _ in range(400):
    _pdn = _sn.drive(0.3)
    if _pdn == "ended" or _sn.game.phase is _ga.Phase.DISCUSS:
        break
_nseq = [x["s"] for x in _sn.timeline if x.get("t") == "sys"]
_norder = ["天黑请闭眼"]
for _k, _o, _c in (("guard", "守卫请睁眼", "守卫请闭眼"),
                   ("wolf", "狼人请睁眼", "狼人请闭眼"),
                   ("witch", "女巫请睁眼", "女巫请闭眼"),
                   ("seer", "预言家请睁眼", "预言家请闭眼")):
    _rv = {"guard": "guard", "wolf": "wolf", "witch": "witch", "seer": "seer"}[_k]
    if any(p.role.value == _rv for p in _sn.game.players):      # 在板（含死人）
        _norder += [_o, _c]
_norder.append("天亮了，请睁眼")
def _find_n(sub):
    return next((i for i, x in enumerate(_nseq) if sub in x), -1)
_npos = [_find_n(x) for x in _norder]
check("★ 夜间法官播报：完整流程按主流顺序（守卫→狼→女巫→预言家→天亮）",
      all(p >= 0 for p in _npos) and _npos == sorted(_npos),
      str(list(zip(_norder, _npos))))
_n1st = next((i for i, x in enumerate(_nseq) if "第 2 夜" in x), len(_nseq))
_n1st_seq = _nseq[:_n1st]
check("★ 夜间播报防重（**单夜窗口内**不重复喊；跨夜每夜重喊是对的）",
      len([x for x in _n1st_seq if "狼人请睁眼" in x]) == 1,
      str(len([x for x in _n1st_seq if "狼人请睁眼" in x])))
check("★ 夜间播报只含流程、绝无私有信息（刀口/验人/倒下都不进夜流程文案）",
      all(("倒下" not in x and "验的是" not in x) for x in _nseq
          if x in set(_norder)), "")
_s4 = _ga.Session(humans=0, ais=4, seed=5)
for _ in range(200):
    _pd4 = _s4.drive(0.3)
    if _pd4 == "ended" or _s4.game.phase is _ga.Phase.DISCUSS:
        break
_s4seq = [x["s"] for x in _s4.timeline if x.get("t") == "sys"]
check("★ 板上没有守卫（4 人局）⇒ 不播守卫流程，其余照播",
      not any("守卫请睁眼" in x for x in _s4seq)
      and any("狼人请睁眼" in x for x in _s4seq)
      and any("女巫请睁眼" in x for x in _s4seq)
      and not any("预言家请睁眼" in x for x in _s4seq),
      str(_s4seq[:9]))
# 12 人局（4 神，守卫出场）⇒ 守卫流程必须播（在板就终身播报）
_s12 = _ga.Session(humans=0, ais=12, seed=3)
for _ in range(300):
    _pd12 = _s12.drive(0.3)
    if _pd12 == "ended" or _s12.game.phase is _ga.Phase.DISCUSS:
        break
_s12seq = [x["s"] for x in _s12.timeline if x.get("t") == "sys"]
check("★ 12 人局守卫在板 ⇒ 守卫流程必播（在板角色死了也照喊）",
      any("守卫请睁眼" in x for x in _s12seq) and any("守卫请闭眼" in x for x in _s12seq),
      str(_s12seq[:9]))

# ================================================================ P1 夜间单一时间源（2026-10-06）
# timeline 条目带 stage、pending_seats 条目带 role —— 前端夜间门控对齐这两个
# 结构化字段，不再靠正则猜「请睁眼」文本（改播报文案不会再弄坏门控）。
_stg = {x.get("stage") for x in _sn.timeline
        if x.get("t") == "sys" and x.get("stage")}
_expect = {"guard", "wolf", "witch", "seer"} & {p.role.value for p in _sn.game.players}
check("★ P1：夜间「请睁眼」播报条目带 stage（= 角色键，服务端单一真相）",
      _expect <= _stg,
      f"expect={sorted(_expect)} got={sorted(_stg)}")
_spr = _ga.Session(humans=1, ais=4, seed=7)
_ga.SESSIONS[_spr.id] = _spr          # 直接构造的局要注册才能走 HTTP 路由
_role_seen = None
for _ in range(400):
    _spr.drive(0.3)
    if _spr.game.phase is _ga.Phase.ENDED:
        break
    _ppr = client.get(f"/api/game/{_spr.id}/pending_seats").json()
    _roles = [it.get("role") for it in _ppr["seats"]]
    if _roles:
        _role_seen = _roles
        break
check("★ P1：pending_seats 条目带 role（前端夜间门控的判据）",
      bool(_role_seen) and all(_role_seen) and _role_seen[0] in
      ("wolf", "witch", "seer", "guard", "hunter", "villager", "idiot"),
      f"roles={_role_seen}")

# ================================================================ 夜间剧本化第二期（2026-10-07）
# ② 节点/间隔：**开眼句 gap=0（"同时"）、闭眼句 delay=GAP(3)**，
#    且每一行都带 node（前端门控认节点 id）。
# ③ 预言家结果节点「他是这个」+ stage=seer_result，且排在睁眼之后、闭眼之前。
_sn2 = _ga.Session(humans=0, ais=9, seed=7)
for _ in range(600):
    # 判据用 `round > 1`（第一夜 + 第一个白天走完）而不是"相位是 DISCUSS"——
    # mock 的 AI 太快，一次 drive 可能从夜直接跨过 DISCUSS 到投票。
    _pdn2 = _sn2.drive(0.3)
    if _pdn2 == "ended" or _sn2.game.round > 1:
        break
_tl2 = [x for x in _sn2.timeline if x.get("t") == "sys"]
_night_lines = [x for x in _tl2 if x.get("node")]
check("★ 第三期：每个夜间节点行都带 node（前端门控认节点 id，不再猜第几句）",
      bool(_night_lines) and all(x.get("node") for x in _night_lines),
      str(sorted({x.get("node") for x in _night_lines})))
_open_nodes = ("guard", "wolf", "witch_heal", "witch_poison", "seer")
_opens = [x for x in _night_lines
          if x.get("node") in _open_nodes and "请闭眼" not in (x.get("s") or "")]
_closes = [x for x in _night_lines if "请闭眼" in (x.get("s") or "")]
check("★ 第三期：开眼句 gap=0（闭眼句与窗口结束**同时**念）、闭眼句 delay=GAP=3",
      len(_opens) >= 3 and all(x.get("delay") == 0 for x in _opens)
      and bool(_closes) and all(x.get("delay") == night_script.GAP for x in _closes),
      f"opens={[x.get('delay') for x in _opens]} closes={[x.get('delay') for x in _closes]}")
_exp_seq = [n["id"] for n in
            night_script.build({p.role.value for p in _sn2.game.players}, 1)
            if n["id"] not in night_script.SKIP_IN_ANNOUNCE]
_uniq = []
for _n in [x.get("node") for x in _night_lines]:
    if not _uniq or _uniq[-1] != _n:
        _uniq.append(_n)
check("★ 第三期：角色段按剧本表顺序出现（顺序 == night_script.build 的节点表）",
      _uniq[:len(_exp_seq)] == _exp_seq, f"seen={_uniq} want={_exp_seq}")
# 🔴 2026-10-07 真机事故的回归闸：每个节点的播报句**只许出现一次**。
#   当时 `_nn_close → _inject_default → _act_locked → auto_advance → _night_clock`
#   无限递归，每层解栈都补一句「狼人请闭眼」⇒ 实测 **124 遍** ⇒ 前端播报队列被灌满
#   ⇒ 门控（念到该角色那句才放行）永远轮不到 ⇒ 人类狼面板不弹、神职没读秒。
_n1: list = []                     # 只看**第一夜**（本用例会一路推到 round>1）
for _x in _tl2:
    _n1.append(_x)
    if str(_x.get("s", "")).startswith("天亮了"):
        break
_txt_n = {}
for _x in _n1:
    if not _x.get("node"):
        continue
    _txt_n[str(_x.get("s"))] = _txt_n.get(str(_x.get("s")), 0) + 1
_dup_txt = sorted([k for k, v in _txt_n.items() if v > 1])
check("★ 第三期：每个节点的播报句**只许出现一次**（防「收口被重复触发」再灌满队列）",
      not _dup_txt, str(_dup_txt[:3]))
_seer_i = next((i for i, x in enumerate(_tl2) if x.get("stage") == "seer"), -1)
_res_i = next((i for i, x in enumerate(_tl2)
               if x.get("stage") == "seer_result"), -1)
_close_i = next((i for i, x in enumerate(_tl2)
                 if "预言家请闭眼" in (x.get("s") or "")), -1)
check("★ 第二期：预言家结果节点「他是这个」带 stage=seer_result（前端据此出大标识）",
      _res_i >= 0 and _tl2[_res_i].get("s") == "他是这个", f"i={_res_i}")
check("★ 第二期：「他是这个」在预言家睁眼之后、闭眼之前（窗口结束时才补播）",
      _seer_i >= 0 and _seer_i < _res_i < _close_i,
      f"seer={_seer_i} result={_res_i} close={_close_i}")

# ④ AI 决策预跑池（night_pre.PrePool）：非阻塞收割 / 超时丢弃 / 没预跑就同步
import threading as _th  # noqa: E402
import time as _tm        # noqa: E402
import night_pre as _np   # noqa: E402

_pool = _np.PrePool()
_ev = _th.Event()
_pool.submit(0, "probe", lambda: (_ev.wait(5), "R")[1], 30)
check("★ 第二期：预跑没跑完 ⇒ take() 返回 WAIT（**不干等**，驱动器据此报 working）",
      _pool.take(0, "probe", 0) is _np.WAIT)
_ev.set()
check("★ 第二期：预跑跑完 ⇒ take() 取到结果（一次即取走）",
      _pool.take(0, "probe", 3) == "R")
_pool2 = _np.PrePool()
_pool2.submit(1, "slow", lambda: (_tm.sleep(0.6), "X")[1], 0.05)
_tm.sleep(0.25)
check("★ 第二期：过了收割点还没算完 ⇒ GIVEUP（结果直接丢弃、默认放弃）",
      _pool2.take(1, "slow") is _np.GIVEUP)
check("★ 第二期：压根没预跑过的 key ⇒ MISS（本地规则 Agent 照旧同步算）",
      _pool2.take(9, "nope") is _np.MISS)


# ⑤ 天黑即后台并发预跑（带真模型通道的替身 ⇒ 走预跑）；驱动器**不等它**
_pre_threads = []


class _SlowPreAgent(_SpyAgent):
    """带 chat_fn 的替身：_pre_kick 的判据就是 chat_fn 非空 ⇒ 决策走后台预跑。
    每次决策 sleep > INIT_BUDGET，好让"驱动器先报 working"这条可断言；
    并把**线程名**记下来 —— 名字带 `nightpre-` 就证明这次决策真是在预跑线程里算的。
    """

    def __init__(self, seat, rng):
        super().__init__(seat, rng)
        self.chat_fn = lambda msgs: ""      # 标记：这个座位有真模型通道

    def _slow(self, view, kind):
        _pre_threads.append((self.seat, kind, _th.current_thread().name))
        _tm.sleep(1.7)
        targets = [p["seat"] for p in view["players"]
                   if p["alive"] and p["seat"] != self.seat]
        return self._D(target=targets[0] if targets else None)

    def decide_target(self, view, history, kind):
        _decision_calls.append((self.seat, kind))
        return self._slow(view, kind)

    def act_on_target(self, view, history, kind):
        _decision_calls.append((self.seat, kind))
        return self._slow(view, kind)


_saved_pre = _ga.AI_AGENT_FACTORY
_saved_scale = night_script.win_scale()
try:
    # ⚠️ 这一段用**真实窗口**（scale=1）：定稿的收割点是"狼刀节点开始"= 入夜 15 秒后，
    #    而替身故意 sleep 1.7s —— 用缩短的窗口会把它的结果当"没算完"丢掉
    #    （那正是定稿的打断，见 `_smoke-night-tempo.py` 里那条断言）。这里要验的是
    #    "**收到了**预跑结果"。
    night_script.set_win_scale(1.0)
    _ga.AI_AGENT_FACTORY = lambda seat, rng: _SlowPreAgent(seat, rng)
    _s_pre = _ga.Session(humans=0, ais=9, seed=7)
    check("★ 第二期：天黑即预跑 —— 决策在后台跑，驱动器**不等满**（先报 working）",
          _s_pre.pending == "working", str(_s_pre.pending))
    _t_h = _tm.monotonic() + 15
    while _tm.monotonic() < _t_h:
        _s_pre.drive(0.3)
        if _s_pre.game.night.get("kill") is not None:
            break
finally:
    _ga.AI_AGENT_FACTORY = _saved_pre
    night_script.set_win_scale(_saved_scale)
check("★ 第二期：预跑结果被收割 —— 决策在**预跑线程**里算的，刀口真的落了地",
      any(name.startswith("nightpre-") for _s, _k, name in _pre_threads)
      and _s_pre.game.night.get("kill") is not None
      and _s_pre.game.night.get("kill") != -1,
      f"threads={sorted({n for _s, _k, n in _pre_threads})} "
      f"kill={_s_pre.game.night.get('kill')}")

# ⑥ AI 投票 5 秒弃票兜底（定稿：最后一个人类投完后等 5s，还没投 = 弃票）
_sg = _ga.Session(humans=1, ais=3, seed=11)
for _ in range(200):
    _g_sg = _sg.game
    if _g_sg.phase is _ga.Phase.VOTE and (_sg.pending == "human_vote"
                                          or _sg._all_voted()):
        break
    _need = [(p.seat, _g_sg.actions_for(p.seat)) for p in _g_sg.players
             if p.kind == "human" and p.alive and _g_sg.actions_for(p.seat)]
    if not _need:
        if _sg.drive(0.3) == "ended":
            break
        continue
    _s0, _a0 = _need[0]
    _o0 = [q.seat for q in _g_sg.alive if q.seat != _s0]
    if "speak" in _a0:
        _sg.act(_s0, "speak", None, "过")
    elif "kill" in _a0 and _o0:
        _sg.act(_s0, "kill", _o0[0], None)
    elif "investigate" in _a0 and _o0:
        _sg.act(_s0, "investigate", _o0[0], None)
    else:
        _sg.act(_s0, "pass", None, None)
_graced = False
_gdetail = f"phase={_sg.game.phase.value} pending={_sg.pending}"
if _sg.game.phase is _ga.Phase.VOTE:
    # 先让该投的人类投完（5s 兜底从**最后一个人类投完**起算）
    for _p in list(_sg.game.players):
        if (_p.kind == "human" and _p.alive and _sg.game.can_vote(_p.seat)
                and _p.seat not in _sg.game.votes):
            _o = next((q.seat for q in _sg.game.alive if q.seat != _p.seat), -1)
            _sg.game.cast_vote(_p.seat, _o)
    _aiv = next((p for p in _sg.game.players
                 if p.kind == "ai" and p.alive and _sg.game.can_vote(p.seat)), None)
    if _aiv is not None:
        _sg.game.votes.pop(_aiv.seat, None)          # 伪造"这个 AI 还没决策"
        _sg._vote_grace_at = _tm.monotonic() - 6     # 5s 兜底已过期
        _sg.drive(0.3)
        _vh = _sg.game.vote_history[-1] if _sg.game.vote_history else {}
        _graced = _vh.get(_aiv.seat) == -1
        _gdetail = f"seat={_aiv.seat} votes={_vh}"
check("★ 第二期：AI 投票 5s 兜底 —— 超时未决策直接记弃票（-1）", _graced, _gdetail)

# ⑦ 狼**空刀**：人类女巫照常走「解药问 + 毒药问」，只是解药**用不了**
#   （2026-10-07 用户定："狼空刀，人类女巫照常走解药播报和面板流程，只是解药按钮置灰"）
from engine import Board as _B7, Role as _R7  # noqa: E402

_BW4 = _B7(key="witch4", label="4 人 · 1 狼 1 女巫 2 民",
           roles=[_R7.WOLF, _R7.WITCH, _R7.VILLAGER, _R7.VILLAGER], lives=1,
           note="空刀用：现算不出「1 狼 + 女巫」的 4 人局。")
_sw = None
for _sd in range(1, 16):
    _t7 = _ga.Session(humans=1, ais=3, board=_BW4, seed=_sd)
    _h7 = next((p for p in _t7.game.players if p.kind == "human"), None)
    if _h7 is not None and _h7.role is _R7.WITCH:
        _sw = _t7
        break
check("（前提）造出「人类是女巫」的局（否则下面几条测不到东西）", _sw is not None)
if _sw is not None:
    _sw.game.night["kill"] = -1                 # 伪造"狼空刀"：刀已定案、但没有刀口
    _sw.game.night["kill_settled"] = True
    _acts7 = _sw.game.actions_for(_h7.seat)
    check("★ 空刀：女巫**照常**拿到解药问（面板照旧，只是按钮该置灰）",
          "heal" in _acts7 and "poison" in _acts7, str(_acts7))
    _rej7 = ""
    try:
        _sw.game.night_heal(_h7.seat, _h7.seat)
    except Exception as _e7:                    # noqa: BLE001
        _rej7 = type(_e7).__name__ + ":" + str(_e7)
    check("★ 空刀：解药真的用不了（引擎拒 —— 界面置灰不是唯一防线）",
          bool(_rej7) and _sw.game.night.get("witch_asked") in (None, False), _rej7)
    # 推到「毒药那问」：解药那一屏结束**不能**把 witch_asked 写死。
    # ⚠️ 必须**真的等时间**：机器人待办挂着时 drive() 会立刻返回（不消耗预算），
    #    节点时钟走的是墙上时间 ⇒ 纯循环推不动它。
    _t7b = _tm.monotonic()
    while _tm.monotonic() - _t7b < 5:
        if _ga.night_driver._nn_cur(_sw) == "witch_poison":
            break
        _sw.drive(0.1)
        _tm.sleep(0.05)
    check("★ 空刀：解药那一屏走完**不算放弃** —— 「你要使用毒药吗？」照常问",
          _ga.night_driver._nn_cur(_sw) == "witch_poison"
          and _sw.game.night.get("witch_asked") in (None, False)
          and "poison" in _sw.game.actions_for(_h7.seat),
          f"node={_ga.night_driver._nn_cur(_sw)} asked={_sw.game.night.get('witch_asked')} "
          f"acts={_sw.game.actions_for(_h7.seat)}")
    _poi7 = [q.seat for q in _sw.game.alive if q.seat != _h7.seat]
    _ok7 = False
    if _poi7:
        try:
            _sw.game.night_poison(_h7.seat, _poi7[0])
            _ok7 = _sw.game.night.get("poison") == _poi7[0]
        except Exception:                       # noqa: BLE001
            _ok7 = False
    check("★ 空刀：毒药**照常能下**（空刀只让解药无处可用，不是整晚停摆）", _ok7,
          f"poison={_sw.game.night.get('poison')}")

# ⑧ 撤回落盘动作（2026-10-07 定稿交互：轮盘"再点同一个号码 = 取消"）
#   引擎侧 `night_clear` 是那条交互的落点；这里逐角色验它"撤得掉、且不动别的"。
_sc8 = _ga.Session(humans=1, ais=3, board=_BW4, seed=11)   # 4 人局：1 狼 1 女巫 2 民
_w8 = next(p for p in _sc8.game.players if p.role.is_wolf)
_t8 = next(q.seat for q in _sc8.game.alive if q.seat != _w8.seat)
_sc8.game.night_kill(_w8.seat, _t8)
_ok8 = (_sc8.game.night.get("wolf_points") or {}).get(_w8.seat) == _t8
_sc8.game.night_clear(_w8.seat)
_ok8 = (bool(_ok8)
        and (_sc8.game.night.get("wolf_points") or {}).get(_w8.seat) is None
        and not _sc8.game.night.get("kill_settled"))
# 女巫：撤回 = **药还回来**（撤回 ≠ 用掉）
_wt8 = next((p for p in _sc8.game.players if p.role.value == "witch"), None)
if _wt8 is not None:
    _sc8.game.night["kill"] = _t8
    _sc8.game.night["kill_settled"] = True
    _sc8.game.night_poison(_wt8.seat, _t8)
    _used8 = not _sc8.game.private[_wt8.seat].potions["poison"]
    _sc8.game.night_clear(_wt8.seat)
    _ok8 = (bool(_ok8) and bool(_used8)
            and _sc8.game.private[_wt8.seat].potions["poison"]
            and _sc8.game.night.get("poison") is None
            and not _sc8.game.night.get("witch_asked"))
# 顺带把 **API 那一层** 也走一遍：`action=clear` 要认得（否则前端一取消就撞「未知动作」）。
# 真人可能是村民（撤无可撤 ⇒ 引擎报的是权限错，不是"未知动作"）——两种情况都算通过。
_s9 = _ga.Session(humans=1, ais=3, board=_BW4, seed=3)
_h9 = next(p for p in _s9.game.players if p.kind == "human")
_msg9 = ""
try:
    _s9.act(_h9.seat, "clear", None, None)
except Exception as _e9:                            # noqa: BLE001
    _msg9 = str(_e9)
_ok8 = bool(_ok8) and ("未知动作" not in _msg9)
check("★ 定稿交互：`night_clear` 撤得掉且不动别的（狼撤点+撤确认不再「已定案」；"
      "女巫撤药 = 药还回来、witch_asked 复位）；且 **API 认得 `clear`**",
      bool(_ok8), f"night={ {k: v for k, v in _sc8.game.night.items() if k in ('kill','kill_settled','poison','witch_asked','wolf_points')} }")

# ---- 计数与汇总：**必须放在所有 check() 之后** ----
# （check() 只是把结果塞进 results，真正的打印/计数在这里 ——
#   插到这段前面就等于没跑：不计数、失败也不会让退出码变红。）
pass_n = fail_n = 0
for name, ok, detail in results:
    print(("PASS" if ok else "FAIL") + "  " + name.ljust(34) + ("  " + detail if detail else ""))
    if ok:
        pass_n += 1
    else:
        fail_n += 1

print("\n" + ("ALL GREEN (" + str(pass_n) + " passed)" if fail_n == 0
              else str(fail_n) + " FAILED / " + str(pass_n) + " passed"))
sys.exit(1 if fail_n else 0)
