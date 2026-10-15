# -*- coding: utf-8 -*-
"""_smoke-night-live.py —— 「假页面」夜间整局回归（2026-10-07 第三期）

================================================================
为什么要有它（这是本期唯一"防复发"的东西，不是普通单测）
================================================================
这一周四起真机事故，形状全一样 —— **单测全绿，只有"页面按协议跑一整夜"才炸**：

  · 狼人/预言家面板不弹         ← 前端拿音频回放游标当门控
  · 神职都没读秒                ← 倒计时被串成 "arm → 面板 → 音频"
  · 夜里还没结束就能开麦        ← 发言模式只判 actions、不判相位
  · 闭眼句抢在倒计时前念出来    ← 播报与窗口两套节拍

真浏览器走查（`_smoke-play.js`）要 5~20 分钟、要真麦克风、还只能在有音频输入的
机器上跑。这个 harness **不开浏览器、不开 HTTP**：直接用 `Session` +
与页面**同一套协议**（读 `now` → arm → act）把整夜跑完，断言 6 条不变量。
实测定位上面每一类事故都在秒级。

不变量（这就是"复杂状态机到底该怎么推"的可执行答案）：
  I1 任一时刻 `now.kind` 合法；且 **speak 只可能出现在 DISCUSS**
  I2 `kind=='wheel'` ⇒ `now.seat` **一定**在 pending_seats 里，且属于**当前节点**
  I3 夜间节点：open 句 / close 句都在时间线里，close 比 open 晚 **≥ 窗口**
     （定稿："闭眼句必须在倒计时归零之后才念出来" —— 这条是可量化的）
  I4 `kind=='wheel'` ⇒ `until` 有值（倒计时**一定存在**）；窗口内单调递减
  I5 轮到谁，`now.seat` 就只能是**这一个**（不许同一拍给两个人两套指令）
  I6 整夜跑完 ⇒ 进 DISCUSS；每个节点都被走到（不跳段）
"""

import os
import sys
import time

os.environ.setdefault("MOCK", "1")
os.environ.setdefault("MOCK_ASR", "1")
os.environ.setdefault("MOCK_LLM", "1")
os.environ.setdefault("MOCK_TTS", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import game_api as _ga            # noqa: E402
import night_script               # noqa: E402

_ok = 0
_bad = 0


def check(name, cond, detail=""):
    global _ok, _bad
    if cond:
        _ok += 1
        print(f"  OK  {name}")
    else:
        _bad += 1
        print(f"FAIL  {name}" + (f"    [{detail}]" if detail else ""))


KINDS = ("wheel", "shoot", "speak", "vote", "none", "over")


def _legal_act(s, need):
    """假页面：从待办里挑一个合法动作做掉（模拟真人点号码 + 圆心）。"""
    g = s.game
    acts = need.get("actions") or []
    seat = need["seat"]
    others = [q.seat for q in g.alive if q.seat != seat]
    try:
        if "speak" in acts:
            s.act(seat, "speak", None, "（过）")
        elif "vote" in acts:
            s.act(seat, "vote", others[0] if others else -1, None)
        elif "kill" in acts:
            s.act(seat, "kill", others[0] if others else -1, None)
            if "confirm" in g.actions_for(seat):
                s.act(seat, "confirm", None, None)
        elif "investigate" in acts:
            s.act(seat, "investigate", others[0] if others else -1, None)
        elif "heal" in acts:
            s.act(seat, "heal", g.knife_target(), None)
        elif "shoot" in acts:
            s.act(seat, "shoot", others[0] if others else -1, None)
        elif "poison" in acts or "pass" in acts or "noop" in acts:
            # 不毒人；`noop` 是占位待办（女巫没药那一屏）—— 什么都不做，等窗口走满
            if "pass" in acts:
                s.act(seat, "pass", None, None)
        return True
    except Exception as e:                                    # noqa: BLE001
        print(f"      （假页面动作失败 seat={seat} acts={acts}：{e}）")
        return False


def run_night(seed=7, humans=4, ais=5, scale=0.12, verbose=False, skip_heal=False,
              no_arm=False):
    """跑一整夜，返回统计。假页面按 now 行事（和真页面同一套判断）。

    `skip_heal=True`：故意**不答**女巫解药那一问（让那个窗口到点自动收口）——
    用来验"到点没操作不会把后面的毒药问掐死"（第四起·续的真机 bug）。
    `no_arm=True`：**从不 arm**（模拟"面板没出来 / 前端没通知"）—— 用来验
    "人类照样拿得到一整窗"（第六起：人狼面板没出现 ⇒ 窗口先跑完 ⇒ 注入 AI 跟随）。
    """
    night_script.set_win_scale(scale)
    s = _ga.Session(humans=humans, ais=ais, seed=seed)
    stat = {"kinds": set(), "seats_bad": [], "until_missing": 0, "multi_seat": [],
            "speak_in_night": [], "never_acted": [], "seats_acted": set(),
            "early_until": [], "poison_saw": [], "started_nodes": {},
            "plan_bad": [], "plan_n": 0,
            "no_arm": no_arm}
    stat["human_witch"] = any(p.kind == "human" and p.role.value == "witch"
                              for p in s.game.players)
    last_until = None
    t0 = time.time()
    while time.time() - t0 < 180:
        s.drive(0.15)                      # 「轮询 = 推进器」，和页面完全一样
        now = s.now_state()
        # ---- I10：夜表下发（2026-10-08「alarm 架构」②）：夜里必须有整张表，
        #      且当前节点必在表上（前端按表渲染的权威来源）
        if s.game.phase is _ga.Phase.NIGHT:
            _plan = s._night_plan()
            if not _plan:
                stat["plan_bad"].append("empty")
            else:
                stat["plan_n"] = len(_plan)
                if now.get("node") and now.get("node") not in {
                        x["id"] for x in _plan}:
                    stat["plan_bad"].append(now.get("node"))
        kind = now.get("kind")
        stat["kinds"].add(kind)
        # ---- I1：kind 合法；speak 不许出现在夜里
        if kind == "speak" and s.game.phase is not _ga.Phase.DISCUSS:
            stat["speak_in_night"].append((s.game.phase.value, now.get("seat")))
        # ---- I2：wheel 的 seat 必须真在待办里，且属于当前节点
        if kind in ("wheel", "shoot"):
            seats = [x["seat"] for x in _ga._pending_seats_of(s)["seats"]]
            if now.get("seat") not in seats:
                stat["seats_bad"].append((now.get("seat"), seats))
            # ---- I4：**arm 之后**倒计时一定存在（第二起事故：恒为 None）
            if now.get("started") and not now.get("until"):
                stat["until_missing"] += 1
            # ---- I7：**arm 之前**倒计时必须是 None（第四起事故：节点一开始就读秒）
            if (not now.get("started")) and now.get("until") is not None:
                stat["early_until"].append((now.get("node"), round(now["until"], 1)))
        # ---- I5：同一拍只给一个人一套指令
        if kind in ("wheel", "speak", "vote", "shoot") and now.get("seat") is None:
            stat["multi_seat"].append(kind)
        # ---- I4 续：until 在窗口内单调递减（只在"窗口已起算"时比：
        #      arm 那一下会把窗口整体推到 arm+W，那不是漂移，是定稿口径）
        u = now.get("until")
        if u and last_until and now.get("seat") is not None and now.get("started"):
            if last_until[0] == now.get("seat") and u > last_until[1] + 0.001:
                stat["until_missing"] += 1          # 同一人窗口内倒着涨 = 漂移
        last_until = (now.get("seat"), u) if (u and now.get("started")) else None
        # ---- 假页面照 now 行动：**先 arm 再操作**
        #      真页面的 arm 发生在"法官念完那句 + 2 秒"；这个 harness 没有音频
        #      ⇒ 等价于页面"语音关掉"那一档 ⇒ 立即 arm（走的是同一个 `arm_window`）。
        # ⚠️ 判据要**只看 node、不看 kind**：人类一操作完那个节点就没待办了
        # （kind 变 none），但窗口还在走 —— 只在 wheel 时取样会永远看不到 started。
        if now.get("node"):
            stat["started_nodes"][now["node"]] = (
                stat["started_nodes"].get(now["node"], False)
                or bool(now.get("started")))
        if now.get("node") == "witch_poison":
            # 毒药窗口这一拍：服务端给的是谁、有没有面板（第四起·续的现场取证点）
            stat["poison_saw"].append((kind, now.get("seat")))
        if kind in ("wheel", "shoot") and now.get("need"):
            if skip_heal and now.get("node") == "witch_heal":
                pass                      # 故意不答解药那一问，等窗口到点
            elif not no_arm:
                s.arm_window(now.get("node") or "")
        if (kind in ("wheel", "shoot", "speak", "vote") and now.get("need")
                and not (skip_heal and now.get("node") == "witch_heal")):
            seat = now["seat"]
            if seat not in stat["seats_acted"]:
                stat["seats_acted"].add(seat)
            if verbose:
                print(f"      [{kind}] seat={seat} node={now.get('node')} "
                      f"acts={now['need'].get('actions')}")
            _legal_act(s, now["need"])
        if s.game.phase is _ga.Phase.DISCUSS or s.game.winner:
            break
        time.sleep(0.02)
    stat["phase"] = s.game.phase.value
    stat["session"] = s
    return stat


def segments(s):
    """量「每个睁眼句 → 它之后第一个闭眼句」的间隔（= 定稿的验收标准）。

    ⚠️ 不能按 `node` 字段配对：闭眼句带的 node id **不一定**等于睁眼句
    （实测「女巫请闭眼」带 `witch_poison`、「预言家请闭眼」带 `seer_close`）——
    所以这里按**时间线顺序**配：每个 open 之后遇到的第一个「请闭眼」就是它那一段的收口。
    预言家那段的两个窗口（`seer` 睁眼 → `seer_result`"他是这个"→`seer_close` 闭眼）
    因此被正确地量成**一整段**。
    """
    tl = [x for x in s.timeline if x.get("node") and x.get("at")]
    rows, opened = [], None
    for x in tl:
        txt = x.get("s") or ""
        if ("请睁眼" in txt) or txt.startswith("你要使用"):
            # 上一段还没等到「请闭眼」就来了下一段的睁眼句 ⇒ 说明上一段的窗口
            # 就是在这句**之前**结束的（女巫两问共用末尾那一句「女巫请闭眼」，
            # 解药段必须按"毒药问开始"收口，否则它的 15 秒会被整段吞掉）。
            if opened:
                n0, at0, w0 = opened
                rows.append((n0, round(x["at"] - at0, 2), round(w0, 2)))
            opened = (x.get("node"), x["at"], night_script.window_of(x.get("node")))
        elif "请闭眼" in txt and opened:
            nid, at0, win = opened
            rows.append((nid, round(x["at"] - at0, 2), round(win, 2)))
            opened = None
    return rows


def _seed_with_human_role(role, humans, ais, upto=60):
    """找一个"人类拿到了这个角色"的种子（真机那两局的形状才有意义）。"""
    for sd in range(1, upto):
        s = _ga.Session(humans=humans, ais=ais, seed=sd)
        if any(p.kind == "human" and p.role.value == role for p in s.game.players):
            return sd
    return None


def report(title, st):
    s, rows = st["session"], segments(st["session"])
    print(f"\n-- {title}：板子 {s.game.board.label}；"
          f"kind 出现过 {sorted(st['kinds'])}")
    print(f"   （v4：定稿点名的四段逐一量 open→close；本跑窗口缩短 ×{night_script.win_scale()}）")
    for nid, g, w in rows:
        flag = "✅" if g + 0.15 >= w else "❌ 抢跑了"
        print(f"     {nid:<14} {g:>6.2f}s   窗口≥{w:>5.2f}s  {flag}")
    check(f"{title} I1 每个 kind 都合法", st["kinds"] <= set(KINDS), str(sorted(st["kinds"])))
    check(f"{title} I1 ★ 夜里**从不**出现 speak（面板/开麦不许在夜间冒出来）",
          not st["speak_in_night"], str(st["speak_in_night"][:3]))
    check(f"{title} I2 ★ kind=wheel 的 seat 一定在该拍待办里", not st["seats_bad"],
          str(st["seats_bad"][:3]))
    check(f"{title} I4 ★ **arm 之后**倒计时一定存在（第二起事故：恒为 None）",
          st["until_missing"] == 0, f"缺/漂 {st['until_missing']} 次")
    if st.get("no_arm"):
        # 🔴 I9 只在"从不 arm"这一档判，而且是**时序判据**：
        #    **面板没出来 / 前端没通知时，服务端必须自己把窗口起起来**
        #    （第六起：兜底 30 秒 > 20 秒狼刀窗口 ⇒ 节点按 open+W 先跑完 ⇒
        #     到点注入默认（狼跟随 AI）⇒ 人类狼一次都没动、AI 的刀成了定案）。
        #    现在兜底 = 半个窗口内必起算 ⇒ 节点跨度应 ≥ **1.5 × 窗口**
        #    （正常档是 1.0×：窗口一开就 arm）。
        #    ⚠️ 不用"采到 started 的那几拍"来判：假页面一 arm/一操作，待办当场消失，
        #    那几拍根本采不到（服务端本身是对的，已用 debug 单独证过）。
        _short = [(n, g, w) for n, g, w in rows if g + 0.25 < w * 1.5]
        check(f"{title} I9 ★ **从不 arm**时每个节点都自己起了窗口"
              f"（节点跨度 ≥ 1.5×窗口；正常档是 1.0×）", not _short, str(_short[:4]))
    if st.get("human_witch"):
        check(f"{title} I8 人类女巫在毒药窗口**一定有面板**"
              f"（第四起·续：解药答过/到点都不许掐死毒药那一问）",
              any(k == "wheel" for k, _s in st["poison_saw"]), str(st["poison_saw"][:4]))
    check(f"{title} I7 ★ **没念完（没 arm）之前倒计时必须是 None**"
          f"（第四起事故：'节点开始过早 / 没按约定的播完起算'的回归闸）",
          not st["early_until"], str(st["early_until"][:3]))
    check(f"{title} I10 ★ 夜里下发**整张夜表**（alarm ②）且当前节点必在表上",
          not st.get("plan_bad"),
          str(st.get("plan_bad", [])[:3]) + f"（表长 {st.get('plan_n', 0)}）")
    check(f"{title} I5 同一拍只给一个座位一套指令",
          not st["multi_seat"], str(st["multi_seat"][:3]))
    check(f"{title} I3 ★ 每个「请闭眼」都在**倒计时归零之后**才念出来",
          bool(rows) and all(g + 0.15 >= w for _n, g, w in rows),
          str([r for r in rows if r[1] + 0.15 < r[2]]))
    return rows


def main():
    print("=" * 66)
    print("_smoke-night-live.py —— 假页面夜间整局（协议级回归）")
    print("=" * 66)

    if "--real" in sys.argv:
        # 定稿的验收标准是**真人听感**，所以给一个按生产窗口值（15/20 秒）跑的档：
        # 一夜约 70 秒。数组断言与快跑档完全一样，只是数字是"见到的时间"。
        print("（--real：窗口 15/20 秒原样，一夜约 70 秒 —— 这才是定稿的验收跑）")
        report("真窗口 12 人（5 人 7 AI）",
               run_night(seed=11, humans=5, ais=7, scale=1.0))
        print("\n" + "=" * 66)
        print(f"结果：{_ok}/{_ok + _bad} 通过" + ("  ✅ ALL GREEN" if not _bad else ""))
        print("=" * 66)
        return 0 if _bad == 0 else 1

    st9 = run_night(seed=7, humans=4, ais=5)          # 9 人：无守卫
    rows9 = report("一局 9 人（4 人 5 AI）", st9)
    check("I6 整夜跑完 ⇒ 进 DISCUSS 或分出胜负",
          st9["phase"] in ("discuss", "ended"), st9["phase"])
    check("I6 女巫/预言家段都走到了",
          {"witch_heal", "witch_poison", "seer"} <= {r[0] for r in rows9},
          str(sorted(r[0] for r in rows9)))

    # 「从不 arm」档（第六起事故的形状）：面板没出来 / 前端没通知时，
    # 人类**照样必须拿到一整窗** —— 服务端 arm 失联兜底（8 秒）负责这件事。
    st_na = run_night(seed=7, humans=4, ais=5, no_arm=True)
    report("不 arm 档（模拟「面板没出来」）", st_na)

    st12 = run_night(seed=11, humans=5, ais=7)        # 12 人：**有守卫**（定稿点名的四段齐全）
    rows12 = report("一局 12 人（5 人 7 AI）", st12)
    check("I6 ★ 四个角色段逐一经过：守卫 / 狼刀 / 女巫（两问）/ 预言家",
          {"guard", "wolf", "witch_heal", "witch_poison", "seer"}
          <= {r[0] for r in rows12}, str(sorted(r[0] for r in rows12)))

    # ---- I8：解药那一问到点**没操作**，毒药那一问必须照样有面板
    #      （真机第四起·续：`_inject_default` 在解药窗口到点注入 pass ⇒ witch_asked
    #       被置位 ⇒ 毒药那一问消失 ⇒ 用户看到"你要使用毒药吗？面板没出现"）
    sd_w = _seed_with_human_role("witch", 4, 5)
    if sd_w is None:
        check("I8 找到'人类女巫'的种子（真机那局的形状）", False, "60 个种子里没有")
    else:
        stw = run_night(seed=sd_w, humans=4, ais=5, skip_heal=True)
        saw = stw["poison_saw"]
        print(f"\n-- 人类女巫那一局（seed={sd_w}）毒药窗口的 now：{saw[:6]}")
        check("I8 ★ 解药那一问**到点没操作** ⇒ 毒药那一问的面板照样出现"
              "（第四起·续的锁：`pass` 不许把毒药问掐死）",
              any(k == "wheel" for k, _s in saw), str(saw[:6]))

    print("\n" + "=" * 66)
    print(f"结果：{_ok}/{_ok + _bad} 通过" + ("  ✅ ALL GREEN" if not _bad else ""))
    print("=" * 66)
    night_script.set_win_scale(1.0)
    return 0 if _bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
