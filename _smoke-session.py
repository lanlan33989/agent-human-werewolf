# -*- coding: utf-8 -*-
"""
D6 回归：真人局前夜的可用性收尾

这一轮补的全是"明天真在桌上跑会出事"的东西：

  1. 一场（series）统计：开始时间 / 已打几局 / 多久 —— 顺带把 M26 的数据采集做进去
  2. 断线恢复：手机锁屏或刷新后能接回上一局；服务重启丢了要说清原因
  3. 屏幕常亮：讨论到一半自动黑屏就没人接得下去
  4. 夜间暗屏：光泄露是手机形态**新引入**的通道（闭眼能透过眼睑感知）
  5. 合规提醒：连开 4 局 ≈ 2h40m，**必然触发**"连续使用超 2 小时需提醒"
  6. 再来一局：不回到首屏、不重新选板子
  7. 每局上下文隔离：一场 4 局是 4 个独立对局，不能带上一局的记忆

跑法：
    ./.venv/Scripts/python.exe _smoke-session.py
"""
from __future__ import annotations

import time as _time  # noqa: E402  （夜间节拍是墙上时间，测试要等它）

import sys
from pathlib import Path

BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

# 2026-10-07 第三期：夜间窗口是真·墙上时间 —— 不缩短的话**一夜四段 = 65 秒**，
# 这个短跑文件会从几十秒涨到几分钟。生产/真机恒为 1.0；窗口的**真实秒数**
# 由 `_smoke-night-tempo.py` 单独验收（那个才是验收标准）。
# ⚠️ 必须在**任何 Session 建出来之前**设好（本文件的用例从 line ~80 就开始跑）。
import time as _time  # noqa: E402
import night_script  # noqa: E402

night_script.set_win_scale(0.02)

from _testboards import SEER4  # noqa: E402  （4 人局带预言家：现算不出来，只能显式给）

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if detail and not ok else ""))


def play(sess, max_steps: int = 80) -> None:
    """把一局推到结束（或没有人类动作可做）。"""
    _idle_from = None
    for _ in range(max_steps):
        if sess.game.phase.value == "ended":
            return
        need = []
        for p in sess.game.players:
            if p.kind == "human" and p.alive:
                a = sess.game.actions_for(p.seat)
                if a:
                    need.append((p.seat, a))
        if not need:
            # 🔴 2026-10-07 第三期：夜间节点时钟走**墙上时间**（读秒不可跳过）——
            # 人类动作都交完了也可能还在走节拍、或 AI 还在后台跑，得再推一会儿。
            # 连续 3 秒推不出新东西才算真的到头（老实现是"没人待办就直接 return"）。
            _idle_from = _idle_from or _time.monotonic()
            sess.drive(0.3)
            if _time.monotonic() - _idle_from > 3.0:
                return
            continue
        _idle_from = None
        seat, acts = need[0]
        v = sess.game.get_view_for(seat)
        others = [q["seat"] for q in v["players"] if q["alive"] and q["seat"] != seat]
        if "speak" in acts:
            sess.act(seat, "speak", None, f"{seat + 1} 号过")
        elif "kill" in acts:
            sess.act(seat, "kill", others[0], None)
        elif "investigate" in acts:
            sess.act(seat, "investigate", others[0], None)
        elif "vote" in acts:
            sess.act(seat, "vote", others[0] if others else -1, None)
        elif set(acts) & {"heal", "poison"}:
            sess.act(seat, "pass", None, None)
        else:
            return


print("=" * 74)
print("① 服务端：板子键 + 每局上下文隔离")
import game_api  # noqa: E402

a = game_api.Session(humans=2, ais=2, seed=11, board=SEER4)
check("public_state 带 board_key（界面「再来一局」要用；现在它就是板子自己的 key）",
      a.public_state().get("board_key") == SEER4.key,
      str(a.public_state().get("board_key")))

play(a)   # 打一会儿，攒出日志与发言
a_speech = list(a.speech)
a_len = len(a.public_state()["timeline"])
check("打了一会儿之后，A 局确实产生了发言与日志", len(a_speech) > 0 and a_len > 3,
      f"speech={len(a_speech)} timeline={a_len}")

b = game_api.Session(humans=2, ais=2, seed=99)
b_tl = b.public_state()["timeline"]
b_speech = list(b.speech)
check("B 局（同一场里的下一局）起手没有 A 局的发言", not b_speech, str(b_speech[:2]))
# 🔴 2026-10-05：夜间法官播报上线后，开局日志多了夜流程条目（狼/女巫/预言家
#    睁闭眼），写死的条数上限过期。断言语义不变（B 局不继承 A 局的内容），
#    改成：B 局日志里**不含 A 局的任何一条**（按文本精确比对）。
_a_log_texts = {x["s"] for x in a.public_state()["timeline"]}
check("B 局的日志只有自己开局那几条（不继承上一局）",
      len([x for x in b_tl if x["s"] in _a_log_texts
                      and not any(w in x["s"] for w in (
                          "游戏开始", "天黑请闭眼", "请睁眼", "请闭眼", "天亮了"))]) == 0)

# 只比较「发言」—— 开局那两条模板句（游戏开始 / 第 1 夜）本来就该一样，
# 第一版断言把模板句当成"共享记忆"，是断言写错了。
a_says = [x["s"] for x in a.public_state()["timeline"] if x["t"] == "say"]
b_says = [x["s"] for x in b_tl if x["t"] == "say"]
shared = [x for x in b_says if x in a_says]
check("两局之间没有共享的发言", not shared, str(shared[:2]))
check("B 局从第 1 轮重新开始", b.public_state()["round"] == 1)

# 私有信息不继承：拿一局「全部是真人」的新局 —— 没人自动验人，
# 所以它的验人记录必须是空的；而上一条 A 局已经积累过私有信息。
a_has_private = any(v.investigations for v in a.game.private.values())
check("A 局确实积累过私有信息（验人记录）", a_has_private,
      "A 局没有任何验人记录，这条对照没意义")
# 4 人全真人。⚠️ 必须显式给板子：按人数现算的 4 人局神职是**女巫**，
# 拿不到预言家，而这条断言要看"预言家的验人记录是空的"。
b2 = game_api.Session(humans=4, ais=0, seed=5, board=SEER4)
seer2 = next(p for p in b2.game.players if p.role.value == "seer")
check("新的一局从干净的私有信息开始（不继承上一局）",
      b2.seat_state(seer2.seat).get("investigations") == {},
      str(b2.seat_state(seer2.seat).get("investigations")))
check("两局的私有信息是不同的对象（不是同一份被复用）",
      b2.game.private is not a.game.private)

check("板子键按人数现算（6 人局 → dyn6；界面「再来一局」要用它）",
      game_api.Session(humans=3, ais=3, seed=1).public_state()["board_key"] == "dyn6",
      game_api.Session(humans=3, ais=3, seed=1).public_state()["board_key"])

print("=" * 74)
print("② 服务端：不存在的局要能给客户端一个可判断的错误")
from fastapi.testclient import TestClient  # noqa: E402
import server  # noqa: E402

c = TestClient(server.app)
r = c.get("/api/game/deadbeef/public")
check("未知局号返回 404（前端据此提示「上一局已失效」）", r.status_code == 404,
      f"{r.status_code}")

print("=" * 74)
print("③ 前端：真人局可用性（静态检查）")
# 2026-10-06：game.html（/game 旧界面）已删除，/play 是唯一对局界面。
# 下列断言原守 game.html，逐条甄别（详见 HANDOFF §10.7 2026-10-06）：
#   改指向 p2（play.html + tableui.js 拼接扫描）：场次统计 / 断线恢复 /
#     屏幕常亮 / 2 小时合规提醒；
#   删除（功能被后续决策取代或形态不同）：
#     bumpSeries 局数计数（一场局数由 metrics.py 统计）、
#     nightops 夜间暗屏 4 条（2026-10-05 用户定取消所有半透明遮罩，机制整体移除）、
#     「再来一局」直接同板子重开（2026-10-01 用户定改为回封面再按开始）、
#     LOCAL_LINES 换局清零（新局走整页重建，形态不同）。
p2 = ((BASE / "play.html").read_text(encoding="utf-8")
      + (BASE / "tableui.js").read_text(encoding="utf-8")
      # 2026-10-07：播报队列 + 夜间门控拆去了 speech_ui.js ⇒ 清单跟着走（判据未改）
      + (BASE / "speech_ui.js").read_text(encoding="utf-8"))

check("① 一场统计：显示开始时间/时长（renderSeries，跨局累计）",
      'id="series"' in p2 and "本场 " in p2 and "function seriesSince" in p2)
check("② 断线恢复：把局号存进 localStorage 并在启动时接回",
      "localStorage.setItem('tt.gid'" in p2 and "async function boot" in p2)
check("② 恢复失败要说明原因，不是只说「读取失败」",
      "这一局没了" in p2)
check("③ 屏幕常亮：申请 Wake Lock", "wakeLock" in p2)
check("③ 切回前台要重新申请常亮（锁屏返回后会失效）",
      "visibilitychange" in p2 and "WAKE === null" in p2)
check("③ 不支持常亮时明确提示手动关锁屏",
      "浏览器不支持常亮" in p2)

check("⑤ 超 2 小时主动提醒（合规要求）",
      "mins >= 120" in p2 and "checkLongUse" in p2)
check("⑤ 提醒写进日志（LOCAL_LINES）+ 常驻提示面板",
      "LOCAL_LINES.push" in p2 and 'id="longUse"' in p2)
check("⑤ 本地提醒台词会进日志", "concat(LOCAL_LINES)" in p2)
check("⑤ 说明依据的法规名", "人工智能拟人化互动服务管理暂行办法" in p2)
check("⑤ 每小时最多提醒一次（不刷屏）", "mins - WREMINDED >= 60" in p2)

print("=" * 74)
print("★ D1 回归：pending 过期时，轮询仍必须把局面推下去")


def play_until_vote(sess, max_steps: int = 200) -> bool:
    """推到「投票阶段、AI 都投完了」——只差人类那一票。"""
    for _ in range(max_steps):
        g = sess.game
        if g.phase.value == "vote":
            return True
        need = [(p.seat, g.actions_for(p.seat)) for p in g.players
                if p.kind == "human" and p.alive and g.actions_for(p.seat)]
        if not need:
            sess.drive(0.05)
            continue
        seat, acts = need[0]
        v = g.get_view_for(seat)
        others = [q["seat"] for q in v["players"] if q["alive"] and q["seat"] != seat]
        if "speak" in acts:
            sess.act(seat, "speak", None, "过")
        elif "kill" in acts and others:
            sess.act(seat, "kill", others[0], None)
        elif "investigate" in acts and others:
            sess.act(seat, "investigate", others[0], None)
        else:
            sess.act(seat, "pass", None, None)
    return False


# 背景（用户实测报的「投票后归票卡住、归票没出」）：
#   act() 里 cast_vote() 成功、紧接着 auto_advance() 抛错 ⇒ /act 返回 400，
#   而 pending 就停在 "human_vote"。可票其实已经齐了 —— 旧实现在这里被
#   `not self.pending.startswith("human_")` 挡住 ⇒ 永远不归票、不写 timeline。
s1 = game_api.Session(humans=1, ais=3, seed=7)
reached = play_until_vote(s1)
check("① 能推到「只差人类那一票」的投票阶段（先把前提立住）",
      reached, f"phase={s1.game.phase.value}")

# 手工伪造「cast_vote 成功、推进失败」的残留状态：票补齐 + pending 留在过期值
for seat in [p.seat for p in s1.game.alive if p.seat not in s1.game.votes]:
    others = [q.seat for q in s1.game.alive if q.seat != seat]
    if others:
        s1.game.cast_vote(seat, others[0])
s1.pending = "human_vote"
check("② 构造成功：票已齐，但 pending 还停在过期的 human_vote",
      s1._all_voted() and s1.pending == "human_vote",
      f"all_voted={s1._all_voted()} pending={s1.pending}")

before = len([x for x in s1.game.public_log if x.startswith("投票结果")])
s1.drive(0.5)
after = len([x for x in s1.game.public_log if x.startswith("投票结果")])
check("★ 轮询一次就归票（旧实现会永远卡在 human_vote 上）", after > before,
      f"「投票结果」{before}→{after}，pending={s1.pending}")

# 反向断言：放宽闸门之后，drive() 仍然**不能**替人类出手
# 🔴 2026-10-05：不再写死 seed=3 —— 4 人局人类可能第一夜就被刀死、局直接 ended，
#    fixture 立不住（纯随机，与被测代码无关）。跨 seed 找一个人类还活着的局，
#    被测断言（drive 不替人类出手）语义不变。
s2 = None
seat2 = None
for _seed in range(3, 40):
    cand = game_api.Session(humans=1, ais=3, seed=_seed)
    for _ in range(240):      # 2026-10-07：没 arm 的夜是 1.5× 窗口 ⇒ 步数预算同步放大
        cand.drive(0.05)
        need2 = [p.seat for p in cand.game.players
                 if p.kind == "human" and p.alive and cand.game.actions_for(p.seat)]
        if need2:
            s2, seat2 = cand, need2[0]
            break
    if s2 is not None and not cand.game.winner:
        break
    s2, seat2 = None, None
check("③ 能找到「有人类待办」的状态（先把前提立住）", seat2 is not None,
      "40 个 seed 里人类全都开局即死（概率极低，检查引擎是不是改坏了）")
if seat2 is not None:
    acts_before = list(s2.game.actions_for(seat2))
    kill_before = s2.game.night.get("kill")
    speech_before = len(s2.speech)
    votes_before = dict(s2.game.votes)
    for _ in range(5):
        s2.drive(0.05)
    check("★ drive() 连续轮询也不会替人类出手（动作/刀口/发言/票 都不许变）",
          s2.game.actions_for(seat2) == acts_before
          and s2.game.night.get("kill") == kill_before
          and len(s2.speech) == speech_before
          and s2.game.votes == votes_before,
          f"actions={s2.game.actions_for(seat2)} votes={s2.game.votes}")

print("=" * 74)
print("★ D2 回归：对局页（play.html）的轮询必须能自愈")

# 2026-10-06：g2 原守 game.html 的轮询自愈四条 —— 页面已删，甄别：
#   轮询自愈改指向 p2（tick 的失败重试）；其余三条的防线
#   已由 p2 的 sigOf(mine)/WHEEL.pick/voicedUp 等价断言覆盖，删除不降级。
check("④ 读取失败后仍要排下一次轮询（旧实现一次网络抖动就永久冻住）",
      "setTimeout(tick, 1500" in p2)

# ★ 两页都要按「座位 + 动作」判断手上这屏是否过期。
# 只看座位会漏掉这一类：同一个人从「投票」变成「用解药」，座位没变 ⇒ 屏幕上还是旧按钮
# ⇒ 玩家一点就是 400「当前阶段是 night，不允许该操作」（实测截图）。
# 2026-10-02：play.html 拆出了 tableui.js（大屏 UI 状态机 + 对局驱动：
# sigOf/voicedUp/tick/openHandoff/renderHandoff/startSay… 都搬了过去）。
# 这些检查是**源码扫描**，所以把两个文件拼起来扫 —— 拆分不该让防线失明。
p2 = ((BASE / "play.html").read_text(encoding="utf-8")
      + (BASE / "tableui.js").read_text(encoding="utf-8")
      # 2026-10-07：播报队列 + 夜间门控拆去了 speech_ui.js ⇒ 清单跟着走（判据未改）
      + (BASE / "speech_ui.js").read_text(encoding="utf-8"))
# ⚠️ 2026-10-07：交接层（交接认领 / 轮盘操作屏）全部在 tableui.js 里；
#    play.html 里的「请把手机交给 N 号」是**身份牌**那一页的文案，是另一回事。
#    下面那条"认领步骤已删"的断言原来扫的是 p2（两文件拼接）⇒ 被身份牌文案
#    误伤，**基线就是红的**（`_smoke-map.py` 只比对"通过数"、不查退出码，所以一直没报）。
#    断言写错了，不是实现错了 ⇒ 改成只扫 tableui.js（判据强度不变）。
pui = (BASE / "tableui.js").read_text(encoding="utf-8")
check("★ play.html：手上这屏的判据包含「动作」而不只是座位",
      "function sigOf" in p2 and "sigOf(mine) === HANDOFF_SIG" in p2
      # sigOf 本身必须仍然把「动作」算进签名
      and "(need.actions || []).slice().sort().join(',')" in p2)
check("★ play.html：这一步被拒后，重新取视图把按钮换成现在能做的动作",
      "这一步没被接受" in p2
      and "MYVIEW = await api('/api/game/' + GID + '/seat/' + seatNo)" in p2
      # 2026-10-05：renderHandoff 卡片被 renderWheelAct 轮盘取代，行为不变
      and "renderWheelAct(seatNo, MYVIEW)" in p2)
check("★ play.html：上一句没念完之前不许弹下一屏（投票弹窗抢先过的情况）",
      "function voicedUp" in p2 and "voicedUp()" in p2 and "!VOICE_ON || PAUSED" in p2)
check("★ play.html：夜间操作屏必须等播报念到该角色的「请睁眼」才放行"
      "（2026-10-05 实局：女巫面板抢在狼人阶段之前弹出 —— 状态机与播报两套路径；"
      "2026-10-06 P1：改对齐服务端 timeline 的 stage 标记 + pending_seats 的 role，"
      "前端不再用正则猜播报文本 —— 改播报文案不会再弄坏门控）",
      "function nightReady" in p2 and "nightReady(top)" in p2
      # ⚠️ 2026-10-07：判据字符串原来写的是 `.stage === need.role`，而实现是
      #    nightStageIdx 里先取 `role = need.role` 再比 `tl[j].stage === role`
      #    —— 语义完全一样，只是写法不同 ⇒ 断言永远不成立（基线就红，
      #    `_smoke-map.py` 只比对通过数所以没报）。改断言（不是改实现）。
      and ".stage === role" in p2
      # 旧的白名单/正则表必须真的删掉，否则门控形同虚设
      and "_NIGHT_ACTS" not in p2 and "_NIGHT_OPEN" not in p2)
# 🔴 2026-10-05 走查挂死的那两条，各自单独写一条断言（改一处要红两个不同的名字）
check("★ tableui：狼队共屏多选 + 批量提交（2026-10-06 晚用户定：不区分哪只狼点的）",
      "function renderWolfPack" in p2 and "wheelConfirmWolfPack" in p2 and "WHEEL.batch" in p2)
check("★ tableui：「交给 X 号」认领步骤已删 —— 播报放出即视为拿到手机",
      # ⚠️ 2026-10-07：原判据 "交给 " / "点圆心认领" 现在出现在**注释**里
      #    （"这一步已删"的说明、以及 WHEEL 文档串），源码扫描必然误伤。
      #    换成不会被注释误伤的判据：旧的认领元素 id（hoTake）已删干净，
      #    并且 openHandoff 直接 wheelTake() 进操作屏。
      "wheelTake();" in pui and "hoTake" not in pui)

check("★ play.html：已经有操作屏时按**本人那一条**比对，且在选中未确认时不重建"
      "（原来对 ps.seats[0] 比对 ⇒ 夜里多人同时 pending 时顺序一变就整屏重载"
      " ⇒ 用户刚点中的号码被冲掉，永远提交不出去）",
      "var mine = (ps.seats || [])" in p2 and "sigOf(mine)" in p2
      and "WHEEL.pick != null" in p2)
check("★ tableui：夜间待办**按当前节点挑人**（不是恒取 seats[0]）—— 真机事故回归闸："
      "恒取 seats[0] 会让\"最小座位属于后面的节点\"时全员卡住（只有最后一段的预言家能动）",
      "PUBLIC.night_node" in pui and "_readyTop" in pui
      and "x.node === _curNode" in pui
      and "var top = (ps.seats && ps.seats.length) ? ps.seats[0] : null;" not in pui)
check("★ tableui：轮盘**点号码即生效**（定稿：「不需要再点击其他按钮」）；"
      "再点同一个号码 = 提交 clear 撤回；**圆心不再承担确认**",
      "submit(seatNo, act, target, null)" in pui
      and "submit(seatNo, 'clear', null, null)" in pui
      and "点一个号码就是决定" in pui
      # 老的两步式（点号码选中 → 点圆心提交）必须**真的删掉**，否则等于两套并存
      and "submit(seatNo, WHEEL.act, WHEEL.pick, null)" not in pui)
check("★ play.html：多狼局仍保留「就这个，确定」兜底入口（引擎挂着 confirm 时才出现）",
      "'hoConfirm'" in p2 and "就这个，确定" in p2
      # 自己还没指过就给确定按钮 = 空动作（engine.night_confirm 要求已在 wolf_points 里）
      and "pointed" in p2 and "indexOf('confirm') >= 0 && pointed" in p2)
check("★ play.html：AI 干活时加密轮询（否则每个动作之间白等 1.2 秒）",
      "PUBLIC.pending === 'working') ? 200" in p2)
check("★ tableui：狼**空刀**时解药那一屏照常出、按钮**置灰不可点**"
      "（2026-10-07 用户定：照常走解药播报和面板流程，只是解药按钮置灰）",
      "无人被刀" in pui and "_yesBtn.disabled = true" in pui
      and "healBlocked" in pui and "healBlocked" in p2
      and "#wheelYes.off" in (BASE / "play.html").read_text(encoding="utf-8"))
check("★ play.html：狼夜里也要加密轮询（实时盘子要看队友的点/锁定，700ms 一轮）",
      "PUBLIC.phase === 'night') ? 700 : 1200" in p2)
# 2026-10-06：以下三条原守 game.html（voicedUp 同款 / 2.5s 快档 / 动作变换屏），
# 防线已由上面 p2 的 voicedUp、加密轮询、sigOf(mine) 断言覆盖，随页面删除退役。

print("=" * 74)
print("★ 四档氛围：档位必须真的落到每个 AI 的 Agent 实例上")

_s_lv = game_api.Session(humans=2, ais=2, seed=7, level="硬核")
check("★ 档位打到了会话上，也打到了每个 AI 座位上（最容易漏的一环）",
      _s_lv.level == "硬核"
      and all(getattr(ag, "level", None) == "硬核" for ag in _s_lv.agents.values())
      and len(_s_lv.agents) > 0,
      f"session={_s_lv.level} agents={ {k: getattr(v, 'level', None) for k, v in _s_lv.agents.items()} }")

_s_lv0 = game_api.Session(humans=2, ais=2, seed=7)
check("不传档位时落到默认档（旧调用方行为不变）",
      _s_lv0.level == game_api.DEFAULT_LEVEL
      and all(getattr(ag, "level", None) == game_api.DEFAULT_LEVEL
              for ag in _s_lv0.agents.values()),
      str(_s_lv0.level))

_bad = False
try:
    game_api.Session(humans=2, ais=2, seed=7, level="地狱模式")
except ValueError:
    _bad = True
check("★ 会话层同样拒绝未知档位（不静默退回）", _bad)

print("=" * 74)
print("★ 规则开关（G9）：设置要真的打进新开的局，且**不追溯**改已在打的局")

_old_set = dict(game_api.SETTINGS)
try:
    game_api.SETTINGS.update({"tie_vote_rule": "random",
                              "guard_can_repeat": True,
                              "hunter_can_shoot_on_witch_poison": True})
    _s6 = game_api.Session(humans=3, ais=3, seed=11)
    check("★ 新开的局拿到设置的规则（三个开关全生效）",
          _s6.game.rules.tie_vote_rule == "random"
          and _s6.game.rules.guard_can_repeat is True
          and _s6.game.rules.hunter_can_shoot_on_witch_poison is True,
          str(_s6.game.rules))
    game_api.SETTINGS.update({"tie_vote_rule": "no_exile"})
    check("★ 不追溯改已在打的那一局（中途改规则 = 让全桌的判断作废）",
          _s6.game.rules.tie_vote_rule == "random", _s6.game.rules.tie_vote_rule)
    _s7 = game_api.Session(humans=3, ais=3, seed=11)
    check("★ 改完设置后**新开**的那一局才拿到新值",
          _s7.game.rules.tie_vote_rule == "no_exile", _s7.game.rules.tie_vote_rule)
    # （Rules 是每局一个 —— Board 上没有 rules 字段，所以这里查的是"两局不是同一个对象"，
    #   这比"板子有没有被污染"更贴近真正会出事的形态：万一以后有人把 rules 提成共享单例，
    #   一次设置就会串味到所有局、包含别人的对局。）
    check("★ 每局拿到的 Rules 是独立对象（不是共享单例）—— 否则开关会串到别的局",
          _s7.game.rules is not _s6.game.rules, "")

    # 真机对照：**同一手牌、只改规则 ⇒ 结果必须不同**。
    # 只断言"存进去了"是不够的 —— 那证明不了引擎真的读了它（读漏了照样能存）。
    def _tie_result(sess):
        """造一个必平票的局面：前两人互投、其余人弃票 ⇒ 看规则怎么判。"""
        g = sess.game
        g.resolve_night()              # 第一夜不刀人 ⇒ 平安夜 ⇒ 进白天
        g.open_discuss()
        while g.current_speaker() is not None:
            g.end_speech()
        g.open_vote()
        a, b = [p.seat for p in g.alive][:2]
        g.cast_vote(a, b)
        g.cast_vote(b, a)
        for p in g.alive:
            if p.seat not in (a, b):
                g.cast_vote(p.seat, -1)    # 其他人弃票 ⇒ 只剩这两票，必平
        return g.resolve_vote()

    _old6 = dict(game_api.SETTINGS)
    try:
        game_api.SETTINGS.update({"tie_vote_rule": "no_exile"})
        _r_std = _tie_result(game_api.Session(humans=3, ais=3, seed=21))
        game_api.SETTINGS.update({"tie_vote_rule": "random"})
        _r_rnd = _tie_result(game_api.Session(humans=3, ais=3, seed=21))
    finally:
        game_api.SETTINGS.clear()
        game_api.SETTINGS.update(_old6)
    check("★ 规则真的改变结果：平票时 no_exile 无人出局 / random 出一个",
          _r_std is None and _r_rnd is not None,
          f"标准={_r_std} 随机={_r_rnd}")
finally:
    game_api.SETTINGS.clear()
    game_api.SETTINGS.update(_old_set)

print("=" * 74)
print("★ 挑战式发言顺序（speech_mode）：端到端 + 整局跑不跑得完")
# 这个机制最大的风险不是"顺序对不对"，而是**一轮永远说不完**（插队失控）。
# 所以两件事一起验：① 真的插了队、手机真的递给了被点名的人 ② 整局能自己走到结束。
_keep = dict(game_api.SETTINGS)
game_api.SETTINGS = {**_keep, "speech_mode": "challenge"}
try:
    # ⚠️ 批八之后 Session 收的是 **Board 对象**（不是板子键字符串）：
    #    不传 board 就按人数现算（界面走的就是这条）。
    # ⚠️ 用 **3 真人 + 1 AI**：这样"第一天发言时还有另一个活人"是**确定**的
    #    （一夜最多死一个），那条"被点名的人下一个开口"才敢写成硬断言 ——
    #    否则它有一半的种子会因为"另一个人昨晚被刀了"而随机变红（测试自己的毛病）。
    _s_sp = game_api.Session(humans=3, ais=1, seed=23)
    _g = _s_sp.game
    check("★ 规则开关落到了这一局的 Rules 上（不是只在接口层记了一笔）",
          _g.rules.speech_mode == "challenge", _g.rules.speech_mode)
    check("★ /public 里带着发言方式（界面要能看出来，否则顺序跳来跳去会被当成 bug）",
          _s_sp.public_state().get("speech_mode") == "challenge",
          str(_s_sp.public_state().get("speech_mode")))

    # 一个循环干两件事：人类待办就替他做，没有待办就让 drive() 推 AI。
    # 第一次轮到人类发言时，让他**点名一个活人** —— 那一次就是端到端的判例。
    _did_accuse = False
    _queue_ok = None
    _chain_ok = None
    _end_ok = False
    for _ in range(800):      # 2026-10-07：没 arm 的夜是 1.5× 窗口 ⇒ 步数预算同步放大
        if _g.phase.value == "ended":
            _end_ok = True
            break
        _need = [p for p in _g.players
                 if p.kind == "human" and p.alive and _g.actions_for(p.seat)]
        if not _need:
            # ⚠️ 没有人类待办**不等于**结束：可能轮到 AI、也可能人类都出局了。
            #    我第一版在这里直接 return，把"该 AI 干活"当成"打完了"，
            #    于是误报成产品卡住（是**测试自己的毛病**，不是插队失控）。
            _s_sp.drive(0.05)
            continue
        _p = _need[0]
        _acts = _g.actions_for(_p.seat)
        _v = _g.get_view_for(_p.seat)
        _others = [q["seat"] for q in _v["players"] if q["alive"] and q["seat"] != _p.seat]
        if "speak" in _acts and not _did_accuse and _others:
            _did_accuse = True
            _y = _others[0]
            _s_sp.act(_p.seat, "speak", None, f"我怀疑 {_y + 1} 号，他像狼。")
            _pos = _g.speaking_order.index(_p.seat)
            _queue_ok = (_g.speaking_order[_pos + 1:_pos + 3] == [_y, _p.seat])
            # 被点名的是**人类** ⇒ 驱动器必须停在他那里（这就是"手机递给谁"）
            if _g.player(_y).kind == "human":
                _chain_ok = (_g.current_speaker() == _y)
            continue
        # ⚠️ 狼队协商是**两步**（先点人、再确定）：没点过就 confirm 会直接抛错。
        #    判"点过没有"要看视图里的 wolf_points —— can_act 里 kill 一直挂着，
        #    只看它就会一直点下去（批二踩过的同一个坑）。
        _pointed = (_v.get("wolf_points") or {}).get(_p.seat) is not None
        if "speak" in _acts:
            _s_sp.act(_p.seat, "speak", None, f"{_p.seat + 1} 号过")
        elif "kill" in _acts and not _pointed and _others:
            _s_sp.act(_p.seat, "kill", _others[0], None)
        elif "confirm" in _acts and _pointed:
            _s_sp.act(_p.seat, "confirm", None, None)
        elif "vote" in _acts:
            _s_sp.act(_p.seat, "vote", _others[0] if _others else -1, None)
        elif "kill" in _acts:
            _s_sp.act(_p.seat, "kill", _others[0], None)
        elif "investigate" in _acts:
            _s_sp.act(_p.seat, "investigate", _others[0], None)
        else:
            _s_sp.act(_p.seat, "pass", None, None)

    check("★ 人类玩家点名后**真的插了队**（act → 记发言 → 插队，整条链）",
          _did_accuse and _queue_ok is True,
          f"点名过={_did_accuse} 队列={_g.speaking_order}")
    check("★ 被点名的人**下一个**开口（驱动器把手机递给了他，不是别人）",
          _chain_ok is True, f"chain={_chain_ok} order={_g.speaking_order}")
    check("★ 挑战式发言顺序下**一局能打完**（插队失控的表现就是这里卡住）",
          _end_ok and _g.phase.value == "ended",
          f"phase={_g.phase.value} round={_g.round} 队列长度={len(_g.speaking_order)}")
finally:
    game_api.SETTINGS = _keep      # 立刻复原：全局设置会影响后面所有局

print("=" * 74)
print("★ 音色（人格的听觉那一半）：时间线必须带上该由谁的声音念")
# 为什么要在会话层验：前端只是把 time.voice 原样转给 /api/tts，
# 所以"哪个座位该用什么音色"这件事**只能在服务端验**（前端没法测对错）。
_s_v = game_api.Session(humans=2, ais=2, seed=31, personality="random")
for _ in range(120):                     # 打几步，攒出法官播报 + AI 发言（2026-10-07：夜 1.5× ⇒ 预算放大）
    if _s_v.game.phase.value == "ended":
        break
    _need = [(p.seat, _s_v.game.actions_for(p.seat)) for p in _s_v.game.players
             if p.kind == "human" and p.alive and _s_v.game.actions_for(p.seat)]
    if not _need:
        _s_v.drive(0.05)
        continue
    _se, _ac = _need[0]
    _vv = _s_v.game.get_view_for(_se)
    _oth = [q["seat"] for q in _vv["players"] if q["alive"] and q["seat"] != _se]
    if "speak" in _ac:
        _s_v.act(_se, "speak", None, f"{_se + 1} 号过")
    elif "vote" in _ac:
        _s_v.act(_se, "vote", _oth[0] if _oth else -1, None)
    elif "kill" in _ac:
        _s_v.act(_se, "kill", _oth[0], None)
    elif set(_ac) & {"heal", "poison"}:
        _s_v.act(_se, "pass", None, None)
    else:
        break
_tl = _s_v.public_state()["timeline"]
_sys_lines = [x for x in _tl if x["t"] == "sys"]
_say_lines = [x for x in _tl if x["t"] == "say"]
check("★ 法官（主持人）的播报一律用**法官音色**",
      bool(_sys_lines) and all(x.get("voice") == game_api.JUDGE_VOICE for x in _sys_lines),
      str([(x.get("voice")) for x in _sys_lines[:3]]))
_ai_say = [x for x in _say_lines if x.get("seat") in _s_v.agents]
check("★ AI 发言的音色 = 它**人格**对应的音色（不是随机的、也不是所有人的都一样）",
      bool(_ai_say) and all(
          x.get("voice") == game_api.voice_for_personality(
              getattr(_s_v.agents[x["seat"]], "personality", None)) for x in _ai_say),
      str([(x.get("seat"), x.get("voice")) for x in _ai_say[:3]]))
check("★ AI 座位的音色来自那张表（没配人格时也不会空）",
      all(x.get("voice") in set(game_api.voice_for_personality(k)
                                for k in game_api.PERSONALITIES) | {game_api.DEFAULT_VOICE}
          for x in _ai_say),
      str(sorted({x.get("voice") for x in _ai_say})))

print("=" * 74)
total = len(PASS) + len(FAIL)
print(f"结果：{len(PASS)}/{total} 通过")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
