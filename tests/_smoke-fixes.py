# -*- coding: utf-8 -*-
"""
本轮修复的回归测试（2026-09-11 第二次会话）

覆盖四个真实反馈：
  1. 女巫用毒会报「未知动作：poison」—— 引擎里根本没有毒药动作
  2. 人类女巫会被整个跳过 —— 因为「狼刀已定 + 验人已定」被判成夜晚结束
  3. 发言与引擎播报的时间线错乱 —— 界面把两者分成两段拼接
  4. 「打字框打不了字」—— 轮询每隔 2.5 秒用 innerHTML 重建输入框

跑法：
    ./.venv/Scripts/python.exe tests/_smoke-fixes.py
"""
from __future__ import annotations

import time as _time  # noqa: E402  （夜间节拍是墙上时间，测试要等它）

import io
import math
import os
import re
import struct
import sys
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))  # 仓库根目录入 path：保证 python tests/xxx.py 可直接跑
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/

from engine import make_board  # noqa: E402
from _testboards import SEER4  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if detail and not ok else ""))


def wav_bytes(seconds: float, freq: float, rate: int = 16000, amp: int = 9000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            v = 0 if freq == 0 else int(amp * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def wav_speechlike(seconds: float = 2.4, rate: int = 16000) -> bytes:
    """
    像人说话的测试信号：音节包络（约 4Hz 起伏）+ 前后留静音。
    用它是因为「持续音」在物理上不像语音 —— 帧能量没有起伏，
    信噪比估计会失效，那是设计上的正常行为，不是 bug。
    """
    import random
    rnd = random.Random(0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        n = int(rate * seconds)
        for i in range(n):
            t = i / rate
            if t < 0.4 or t > seconds - 0.4:
                env = 0.0                       # 前后静音
            else:
                env = max(0.0, math.sin(2 * math.pi * 4.0 * t)) ** 1.5
            base = math.sin(2 * math.pi * 220 * t) + 0.5 * math.sin(2 * math.pi * 660 * t)
            noise = rnd.uniform(-0.002, 0.002)
            v = int(9000 * (env * base / 1.5 + noise))
            frames += struct.pack("<h", max(-32768, min(32767, v)))
        w.writeframes(bytes(frames))
    return buf.getvalue()


print("=" * 74)
print("① 引擎：女巫毒药")
from engine import Game, Phase, Role  # noqa: E402

g = Game(make_board(3, 3), ["human"] * 6, seed=1)
g.start()
witch_seat = next(p.seat for p in g.players if p.role is Role.WITCH)
wolf_seat = next(p.seat for p in g.players if p.role.is_wolf)
# ⚠️ 2026-09-15 改：规则变成「一人一条命」之后，**刀和毒必须打在两个人身上**。
# 原来两个都打同一个人 —— 那种打法在「2 条命」时才能看出"2 点伤害"；
# 在 1 条命下狼刀本身就已经让人出局，**毒药有没有生效根本测不出来**（断言会假绿）。
knifed = next(p.seat for p in g.players
              if not p.role.is_wolf and p.seat != witch_seat)
poisoned = next(p.seat for p in g.players
                if not p.role.is_wolf and p.seat not in (witch_seat, knifed))

g.wolf_agree_kill(knifed)
g.night_poison(witch_seat, poisoned)
g.resolve_night()
check("狼刀与毒药各杀一人（两条独立结算，不是叠加伤害）",
      not g.player(knifed).alive and not g.player(poisoned).alive,
      f"被刀者存活={g.player(knifed).alive} 被毒者存活={g.player(poisoned).alive}")
# 🔴 2026-10-07 第七起（用户定）：**毒杀与刀杀同一个说法**，公共播报里**不许出现死因**。
#    这条断言原来写的正是反过来的事（"日志里要有『毒』字"）⇒ 必须按新规则重写。
#    现在它锁两件事：毒死的人**照样**进公开日志（统一措辞）＋**全篇一个『毒』字都没有**。
check("毒药致死写入公开日志，但**不带死因**（用户定：毒杀与刀杀一个说法）",
      any("出局" in line for line in g.public_log)
      and not any("毒" in line for line in g.public_log),
      str(g.public_log))
check("用毒后女巫的药被消耗", not g.private[witch_seat].potions["poison"])

g2 = Game(make_board(3, 3), ["human"] * 6, seed=2)
g2.start()
w2 = next(p.seat for p in g2.players if p.role is Role.WITCH)
wolf2 = next(p.seat for p in g2.players if p.role.is_wolf)
v2 = next(p.seat for p in g2.players if p.seat != w2 and p.seat != wolf2)
g2.wolf_agree_kill(v2)
g2.night_poison(w2, v2)
try:
    g2.night_poison(w2, v2)
    check("同一夜不能重复用毒", False, "第二次没有报错")
except PermissionError:
    check("同一夜不能重复用毒", True)

print("=" * 74)
print("② 引擎：女巫必须在狼刀之后才睁眼（2026-09-15 修正）")
# ⚠️ 这一段原来断言的是「女巫一开局就能 heal/poison/pass」。
# 那条断言**准确描述了当时的实现，但实现本身的规则是错的**：
# 狼还没刀人，女巫拿不到死者是谁，她只能"过"掉；而 witch_asked 一旦写死，
# 这一夜再也不会问她 ⇒ 她永远救不了人（实测 bar4 seed=5 复现）。
# 所以这里把狼刀按真实规则补上再断言她的动作。
# 这不是"为了让测试变绿而改断言" —— 是**断言锁住了错误规则**，改成锁住正确规则。
# 实测依据：修之前 bar4 seed=5 的 night 是 {kill: None, witch_asked: True}。
g3 = Game(make_board(3, 3), ["human"] * 6, seed=3)
g3.start()
w3 = next(p.seat for p in g3.players if p.role is Role.WITCH)
wolf3 = next(p.seat for p in g3.players if p.role.is_wolf)

check("狼刀未定时，女巫不睁眼（否则她永远救不了人）",
      g3.actions_for(w3) == [], str(g3.actions_for(w3)))
try:
    g3.night_decline(w3)
    check("狼刀未定时，女巫不能提前表态「不用药」", False, "没有报错")
except RuntimeError:
    check("狼刀未定时，女巫不能提前表态「不用药」", True)

victim3 = next(p.seat for p in g3.players if not p.role.is_wolf and p.seat != w3)
g3.wolf_agree_kill(victim3)
acts = g3.actions_for(w3)
check("狼刀定了之后，女巫动作含 heal/poison/pass",
      set(acts) == {"heal", "poison", "pass"}, str(acts))
check("女巫醒来时能看到今晚被刀的是谁（这是她救人唯一的依据）",
      g3.get_view_for(w3).get("night_victim") == victim3,
      str(g3.get_view_for(w3).get("night_victim")))

# 解药只能救被刀的那个：点错人会白耗一瓶（药被消耗、被刀的人照样死）
other3 = next(p.seat for p in g3.players
              if p.seat != w3 and p.seat != victim3 and p.alive)
try:
    g3.night_heal(w3, other3)
    check("解药只能救今晚被刀的人（救别人应当被拒）", False, "没有报错")
except ValueError:
    check("解药只能救今晚被刀的人（救别人应当被拒）", True)
check("被拒之后解药还在（不能白耗掉）", g3.private[w3].potions["heal"] is True)

g3.night_decline(w3)
# ⚠️ 2026-10-07：旧语义暂时保留（表态『不用药』⇒ 动作列表清空）。
#    "用了/不用解药，毒药那一问都要照常走"（用户定）**尚未落地** ——
#    两次尝试都把夜搞卡了（详见 §10.7 第九起）；正确改法 = 节点感知
#    （`night_decline(actor, stage=...)` + 驱动器在毒药窗口收口时以 stage="poison" 调用）。
check("表态不用药后，动作列表清空", g3.actions_for(w3) == [],
      str(g3.actions_for(w3)))
check("night_victim 只对女巫暴露",
      "night_victim" in g3.get_view_for(w3) and
      all("night_victim" not in g3.get_view_for(p.seat)
          for p in g3.players if p.role is not Role.WITCH))

print("=" * 74)
print("③ 对局 API：人类女巫不会被跳过 + 时间线")
import game_api  # noqa: E402

# 2026-10-07 第三期：夜间窗口是真·墙上时间 —— 不缩短的话**一夜四段 = 65 秒**，
# 这个短跑文件会从几十秒涨到几分钟。生产/真机恒为 1.0；窗口的**真实秒数**
# 由 _smoke-night-tempo.py 单独验收（那个才是验收标准）。
import night_script  # noqa: E402
night_script.set_win_scale(0.02)
# （旧预设 BOARDS 已删：这里原本 import 它是为了取板子属性，迁移后不再需要）

def _pump_until(sess, pred, limit=30.0):
    """推进到 pred() 成立（★ 2026-10-29：夜间是**真窗口**，必须让墙上时间走）。

    以前这些用例直接 `sess.auto_advance()` 一次就能推到女巫 —— 那是"执行顺序
    wolf→seer→witch"且"一问即定"的老语义。现在顺序改成跟播报一致
    （守卫→狼→女巫→预言家），而且每个窗口要真走满，所以必须循环推进。
    """
    t0 = _time.time()
    while _time.time() - t0 < limit:
        sess.drive(0.05)
        try:
            if pred():
                return True
        except Exception:            # noqa: BLE001
            pass
    return False


# 造一局「女巫是人类」的场景：直接构造，再手动把 AI 的狼刀/验人做掉
found = False
for seed in range(200):
    s = game_api.Session(humans=6, ais=0, seed=seed)
    gs = s.game
    wi = next((p for p in gs.players if p.role is Role.WITCH and p.kind == "human"), None)
    if wi is None:
        continue
    # 全部做成 AI 行为，看夜晚会不会停在女巫这里
    if gs.phase is not Phase.NIGHT:
        continue
    # 把狼刀与验人填掉
    wolf = next((p for p in gs.alive if p.role.is_wolf), None)
    seer = next((p for p in gs.alive if p.role is Role.SEER), None)
    if wolf:
        tgt = next(p.seat for p in gs.good_alive())
        gs.wolf_agree_kill(tgt)
    if seer:
        tgt2 = next(p.seat for p in gs.alive if p.seat != seer.seat)
        gs.night_investigate(seer.seat, tgt2)
    # ★ 2026-10-29：顺序改成跟播报一致（守卫→狼→女巫→预言家）+ 窗口要真走完 ⇒
    #   推进到**女巫自己的窗口**才该看到她，一次 auto_advance 推不到。
    _pump_until(s, lambda: "heal" in gs.actions_for(wi.seat), 25)
    pend = s.pending
    check("人类女巫在「狼刀+验人都已定」后仍被问到",
          pend == "human_night" and "heal" in gs.actions_for(wi.seat),
          f"pend={pend} acts={gs.actions_for(wi.seat)}")
    found = True
    break
if not found:
    check("人类女巫在「狼刀+验人都已定」后仍被问到", False, "200 个种子里没造出该场景")

s4 = game_api.Session(humans=2, ais=2, seed=7)


def play(sess, max_steps: int = 80) -> None:
    """把一局推到结束（或推到没有人类动作可做），模拟界面会做的事。"""
    _idle_from = None
    for _ in range(max_steps):
        if sess.game.phase.value == "ended":
            return
        # ★ 2026-10-28：已点过刀口的人狼不再重复提交（kill 整窗都挂着，见 settle_wolf_window）
        _wpt = sess.game.night.get("wolf_points") or {}
        need = [(p.seat, sess.game.actions_for(p.seat))
                for p in sess.game.players
                if p.kind == "human" and p.alive and sess.game.actions_for(p.seat)
                and not (p.seat in _wpt
                         and set(sess.game.actions_for(p.seat)) <= {"kill", "confirm"})]
        if not need:
            # 🔴 2026-10-07 第三期：夜里是**墙上时间**的固定节拍（读秒不可跳过），
            # 人类动作交完也可能还在走节拍 ⇒ 不能"没人待办就 return"。
            _idle_from = _idle_from or _time.monotonic()
            sess.drive(0.3)
            if _time.monotonic() - _idle_from > 3.0:
                return
            continue
        _idle_from = None
        seat, acts = need[0]
        v = sess.game.get_view_for(seat)
        others = [p["seat"] for p in v["players"] if p["alive"] and p["seat"] != seat]
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


play(s4)
tl = s4.public_state()["timeline"]
check("时间线含两类条目（引擎播报 + 玩家发言）",
      any(x["t"] == "sys" for x in tl) and any(x["t"] == "say" for x in tl),
      str([x["t"] for x in tl][:6]))
check("走完一段后至少出现 1 条玩家发言",
      sum(1 for x in tl if x["t"] == "say") >= 1,
      f"say={sum(1 for x in tl if x['t'] == 'say')}")
# 关键：某轮「开始投票」必须出现在该轮最后一条发言之后
last_vote = max((i for i, x in enumerate(tl) if "开始投票" in x["s"]), default=-1)
last_say = max((i for i, x in enumerate(tl) if x["t"] == "say"), default=-1)
check("「开始投票」排在当轮最后一条发言之后", last_vote > last_say,
      f"vote@{last_vote} say@{last_say}")

print("=" * 74)
print("④ 拾音指标（server.analyze_input）")
import server  # noqa: E402

# ⚠️ `import server` 有副作用：它会把 game_api.AI_AGENT_FACTORY 指向真 LLM。
# 于是这个文件后半段**所有** Session 行为测试都会去打真模型 —— 每次 AI 动作 2~3 秒、
# 要花钱、还不稳定（实测：并发用例因此 1.8 秒里一条发言都跑不出来，测试形同虚设）。
# 这些测试验的是驱动器与规则，不需要真模型，统一退回规则发言器。
game_api.AI_AGENT_FACTORY = None

sil = server.analyze_input(wav_bytes(2.0, 0))
check("静音被识别为几乎没收到声音",
      sil.get("rms_dbfs", 0) < -55 and "没收到" in sil.get("verdict", ""),
      str(sil))
steady = server.analyze_input(wav_bytes(2.0, 440))
check("持续音被识别为「不像人说话」而不是误判成正常",
      "不像人说话" in steady.get("verdict", ""), str(steady))
speech = server.analyze_input(wav_speechlike())
check("有音节起伏的信号被判定为拾音正常",
      speech.get("verdict") == "拾音正常",
      f"snr={speech.get('snr_db')} verdict={speech.get('verdict')}")
check("指标含响度/信噪比/爆音三项",
      all(k in speech for k in ("rms_dbfs", "snr_db", "clipping_pct")), str(speech))

print("=" * 74)
print("⑤ 前端静态检查（防同类 bug 回潮）")
# 2026-10-06：game.html（/game 旧界面）已删除，/play 是唯一对局界面。
# 下列断言原守 game.html，逐条甄别（详见 HANDOFF §10.7 2026-10-06）：
#   改指向 play.html：timeline 渲染、「不用解药」入口；
#   删除（防线已由 p2 等价断言 + 真浏览器走查覆盖）：
#     握持继续轮询 / 不重建面板（p2 的加密轮询 + WHEEL.pick 断言）、
#     DRAFT（大圆录音直接提交，无输入框草稿场景）、日志折叠（#logbox 常显）、
#     pickActions（轮盘取代按钮列表，女巫改为分步问句）。
p2 = ((BASE / "frontend" / "play.html").read_text(encoding="utf-8")
      + (BASE / "frontend" / "tableui.js").read_text(encoding="utf-8")
      # 2026-10-07：播报队列 + 夜间门控拆去了 speech_ui.js ⇒ 清单跟着走（判据未改）
      + (BASE / "frontend" / "speech_ui.js").read_text(encoding="utf-8"))
# 🔴 2026-10-23：探针页 index.html 已删（用户：非游戏画面），相关判据随之退役
server_py = (BASE / "backend" / "server.py").read_text(encoding="utf-8")

check("play.html：渲染的是服务端 timeline（不自己拼接历史）",
      "PUBLIC.timeline" in p2)
check("play.html：女巫有「不用解药」入口（✕ 大按钮）",
      'id="wheelNo"' in p2)
check("server.py：返回 input_audio 与 asr_mocked",
      '"input_audio": input_audio' in server_py and '"asr_mocked": asr_mocked' in server_py)

print("=" * 74)
print("⑥ 入口与报错路径（run_https / start.py 的调用关系）")
# 锁定的是「谁负责给出友好报错」这个事实。本项目文档曾经把它写错过一次：
#   · run_https.py 自己也有 main()，里面有 except ImportError 的友好提示
#   · 但 start.py（唯一推荐入口）是**直调** run_https.make_cert()，绕过了它
# 所以「缺 cryptography 会友好报错」只对 `python run_https.py` 成立，
# 对推荐入口 `python start.py` 不成立。下面两条各测一侧。
import builtins  # noqa: E402

run_https_src = (BASE / "backend" / "run_https.py").read_text(encoding="utf-8")
start_src = (BASE / "start.py").read_text(encoding="utf-8")

check("run_https.py 是双用途：可被 import，也有 __main__ 独立入口",
      'if __name__ == "__main__":' in run_https_src and "def make_cert(" in run_https_src)
check("start.py 直调 make_cert（因此拿不到 run_https.main 的友好报错）",
      "run_https.make_cert(" in start_src)

_real_import = builtins.__import__


def _block_crypto(name, *a, **k):
    if name.split(".")[0] == "cryptography":
        raise ImportError("simulated: cryptography 未安装")
    return _real_import(name, *a, **k)


import run_https  # noqa: E402

builtins.__import__ = _block_crypto
try:
    try:
        run_https.make_cert([])
        _direct_raised = False
    except ImportError:
        _direct_raised = True
finally:
    builtins.__import__ = _real_import
check("cryptography 缺失时 make_cert 直接抛 ImportError（直调无保护）", _direct_raised)

_argv = sys.argv
builtins.__import__ = _block_crypto
try:
    sys.argv = ["run_https.py", "--force-cert"]
    _rc = run_https.main()
finally:
    builtins.__import__ = _real_import
    sys.argv = _argv
check("cryptography 缺失时 run_https.main() 捕获并返回 1（友好报错）", _rc == 1,
      f"rc={_rc}")

print("=" * 74)
print("⑦ 夜间秩序：AI 女巫必须救得了人 + 人类狼必须刀得了人（2026-09-15 修）")
# 这两个都是「板子规则」的实现 bug。共同点：**只在多人板子里出现**，
# mini4（1 狼 1 预 2 民）测不出来 —— 所以它们能活到用户手上。

# ① 人类狼 + AI 女巫。修之前：AI 女巫在狼刀之前就被问，victim 还是 None，
#    她只能 decline，而 witch_asked 被写死 ⇒ 这一夜再也不会问她 ⇒ 永远救不了人。
#    实测复现：bar4 seed=5，night = {kill: None, witch_asked: True}
_found7 = False
for seed in range(300):
    s7 = game_api.Session(humans=1, ais=3, seed=seed)
    g7 = s7.game
    wolf7 = next((p for p in g7.players if p.role.is_wolf), None)
    witch7 = next((p for p in g7.players if p.role is Role.WITCH), None)
    if not (wolf7 and witch7 and wolf7.kind == "human" and witch7.kind == "ai"):
        continue
    check("人狼+AI女巫：狼没刀之前，AI 女巫不许被问（不许提前放弃）",
          g7.night.get("kill") is None and not g7.night.get("witch_asked"),
          f"seed={seed} kill={g7.night.get('kill')} witch_asked={g7.night.get('witch_asked')}")
    victim7 = g7.good_alive()[0].seat
    # ★ 2026-10-29：等价于「人类狼已经刀完了」—— 用 wolf_agree_kill 而不是直接写
    #   night["kill"]，因为现在刀口在**窗口关闭时**按点子结算，手写的值会被覆盖成空刀。
    g7.wolf_agree_kill(victim7)
    s7.rng.random = lambda: 0.0      # 逼 AI 女巫一定救人
    _pump_until(s7, lambda: g7.night.get("heal") is not None
                or g7.phase is not Phase.NIGHT, 25)
    check("AI 女巫在狼刀之后真的能救人（被刀的人没出局）",
          g7.player(victim7).alive
          and g7.player(victim7).lives == make_board(2, 2).lives
          and g7.night.get("heal") == victim7,
          f"alive={g7.player(victim7).alive} lives={g7.player(victim7).lives} "
          f"heal={g7.night.get('heal')}")
    _found7 = True
    break
if not _found7:
    check("人狼+AI女巫：狼没刀之前，AI 女巫不许被问", False, "300 个种子里没造出该场景")

# ② 双狼局（人狼 + AI 狼）。修之前：AI 狼抢先定刀，人类狼的 actions_for() 直接变空
#    ⇒ 他拿不到手机，这一夜等于完全没参与。
#    实测复现：classic6 seed=0，人类狼 3 号能做 = []，刀已被 4 号 AI 定成 5 号
_found8 = False
for seed in range(300):
    s8 = game_api.Session(humans=3, ais=3, seed=seed)
    if s8.pending != "human_night":
        continue
    g8 = s8.game
    wolves8 = [p for p in g8.players if p.role.is_wolf]
    if not (len(wolves8) == 2 and any(p.kind == "human" for p in wolves8)):
        continue
    hw8 = next(p for p in wolves8 if p.kind == "human")
    check("双狼局：人类狼拿得到「刀谁」（AI 狼不许抢先定刀）",
          "kill" in g8.actions_for(hw8.seat),
          f"seed={seed} 人类狼={hw8.seat + 1} 能做={g8.actions_for(hw8.seat)}")
    _found8 = True
    break
if not _found8:
    check("双狼局：人类狼拿得到「刀谁」", False, "300 个种子里没造出该场景")

print("=" * 74)
print("⑧ AI 女巫的用药走模型（2026-09-17 改）")
# 改之前：`self.rng.random() < 0.6` 一刀切决定救不救，
# 而**毒药在 AI 这条路径上从来没被调用过**（night_poison 全仓唯一调用点是人类那条）
# ⇒ AI 女巫 = "60% 概率救被刀者 + 从不毒人"。
# 现在：解药问是/否、毒药问座位号（且允许说"不毒"），都用剧本 Agent 来钉住。
from agents import ScriptedAgent  # noqa: E402

_found_w1 = False
for seed in range(400):
    s_w1 = game_api.Session(humans=1, ais=3, seed=seed)
    g_w1 = s_w1.game
    w1 = next((p for p in g_w1.players if p.role is Role.WITCH), None)
    wolf_w1 = next((p for p in g_w1.players if p.role.is_wolf), None)
    # ⚠️ 前提必须是「**人类**狼 + AI 女巫」：人狼还没刀 ⇒ 夜晚悬在那里、女巫还没被问过。
    #    如果狼和女巫都是 AI，Session 构造时那一轮 auto_advance 就已经把整夜跑完了
    #    （狼刀 + 女巫用药都发生过），之后我再改 night 毫无意义 ——
    #    第一版就是这么写的，断言于是随机地过 / 不过。
    if not (w1 and wolf_w1 and w1.kind == "ai" and wolf_w1.kind == "human"):
        continue
    if g_w1.night.get("kill") is not None or g_w1.night.get("witch_asked"):
        continue
    victim_w1 = next(p.seat for p in g_w1.alive if p.seat != w1.seat and not p.role.is_wolf)
    g_w1.wolf_agree_kill(victim_w1)
    # 掷骰子这一路会说"救"—— 断言没走它，就等于证明决策真的来自模型
    s_w1.rng.random = lambda: 0.0
    s_w1.agents[w1.seat] = ScriptedAgent(w1.seat, ["不救"])
    _pump_until(s_w1, lambda: g_w1.night.get("heal_picked")
                or g_w1.phase is not Phase.NIGHT, 25)
    check("★ AI 女巫说「不救」就真的不救（走模型，不再掷骰子）",
          g_w1.night.get("heal") is None
          # ★ 2026-10-29：判据从 witch_asked 改成 heal_picked —— 两问两个窗口，
          #   解药那一问的"不救"只算**这一问**表态，witch_asked 要到毒药窗口关闭。
          and g_w1.night.get("heal_picked") is True
          and g_w1.private[w1.seat].potions["heal"] is True,
          f"heal={g_w1.night.get('heal')} picked={g_w1.night.get('heal_picked')} "
          f"药还在={g_w1.private[w1.seat].potions['heal']}")
    _found_w1 = True
    break
if not _found_w1:
    check("AI 女巫说「不救」就真的不救", False, "400 个种子里没造出「人狼 + AI 女巫」")

_found_w2 = False
for seed in range(400):
    s_w2 = game_api.Session(humans=1, ais=3, seed=seed)
    g_w2 = s_w2.game
    w2 = next((p for p in g_w2.players if p.role is Role.WITCH), None)
    wolf_w2 = next((p for p in g_w2.players if p.role.is_wolf), None)
    if not (w2 and wolf_w2 and w2.kind == "ai" and wolf_w2.kind == "human"):
        continue
    if g_w2.night.get("kill") is not None or g_w2.night.get("witch_asked"):
        continue
    victim_w2 = next(p.seat for p in g_w2.alive if p.seat != w2.seat and not p.role.is_wolf)
    poison_w2 = next(p.seat for p in g_w2.alive
                     if p.seat != w2.seat and p.seat != victim_w2)
    g_w2.wolf_agree_kill(victim_w2)
    g_w2.private[w2.seat].potions["heal"] = False      # 解药已用 ⇒ 只剩毒药可考虑
    s_w2.agents[w2.seat] = ScriptedAgent(w2.seat, [str(poison_w2 + 1)])
    # ★ 2026-10-29：等**毒药窗口关闭**（扣药发生在这一刻），不是"一出结果就断言"。
    _pump_until(s_w2, lambda: g_w2.night.get("poison_settled")
                or g_w2.phase is not Phase.NIGHT, 25)
    check("★ AI 女巫给出座位号就真的用毒（改之前 AI 永远不毒人）",
          g_w2.night.get("poison") == poison_w2
          and g_w2.private[w2.seat].potions["poison"] is False,
          f"poison={g_w2.night.get('poison')} 期望={poison_w2}")

    s_w3 = game_api.Session(humans=1, ais=3, seed=seed)
    g_w3 = s_w3.game
    w3 = next((p for p in g_w3.players if p.role is Role.WITCH), None)
    g_w3.night["kill"] = victim_w2
    g_w3.private[w3.seat].potions["heal"] = False
    s_w3.agents[w3.seat] = ScriptedAgent(w3.seat, ["不毒"])
    s_w3.auto_advance()
    check("★ AI 女巫说「不毒」就不用毒（没有这个出口 ⇒ 只能盲毒）",
          g_w3.night.get("poison") is None
          and g_w3.private[w3.seat].potions["poison"] is True,
          f"poison={g_w3.night.get('poison')} 药还在={g_w3.private[w3.seat].potions['poison']}")
    _found_w2 = True
    break
if not _found_w2:
    check("AI 女巫用毒/不毒", False, "400 个种子里没造出「人狼 + AI 女巫」")

print("=" * 74)
print("⑨ 投票：允许投自己 + AI 能主动弃票（2026-09-17）")

g_sv = Game(SEER4, kinds=["human"] * 4, seed=1)
g_sv.start()
g_sv.night["kill"] = None
g_sv.resolve_night()
g_sv.open_discuss()
g_sv.open_vote()
_self_ok = True
try:
    g_sv.cast_vote(0, 0)
except Exception as _e:                        # noqa: BLE001
    _self_ok = False
check("★ 允许投自己（标准局里自投≈弃权的一种表达）",
      _self_ok and g_sv.votes.get(0) == 0, f"votes={g_sv.votes}")
g_sv.cast_vote(1, 0)
g_sv.cast_vote(2, 0)
g_sv.cast_vote(3, -1)
check("自投会被正常计票（和面杀里举手投自己一致）",
      g_sv.tally_text() == "1，2，3→1；4 弃票" or g_sv.tally_text() == "1，2，3→1",
      g_sv.tally_text())

# AI 的投票决策函数：三种输入 → 三种结果
s_vote = game_api.Session(humans=1, ais=3, seed=2)
_seat_ai = next(p.seat for p in s_vote.game.players if p.kind == "ai")
_others_v = [p.seat for p in s_vote.game.alive if p.seat != _seat_ai]

s_vote.agents[_seat_ai] = ScriptedAgent(_seat_ai, ["弃票"])
check("★ AI 说「弃票」→ 决策返回 -1（改之前 AI 从不弃票）",
      s_vote._pick_ai_vote(_seat_ai) == -1, str(s_vote._pick_ai_vote(_seat_ai)))

s_vote.agents[_seat_ai] = ScriptedAgent(_seat_ai, [str(_others_v[0] + 1)])
check("AI 给出座位号 → 正常投他（弃票功能没有把正常投票吃掉）",
      s_vote._pick_ai_vote(_seat_ai) == _others_v[0], str(_others_v[0]))

s_vote.agents[_seat_ai] = ScriptedAgent(_seat_ai, ["我想想"])
_guess = s_vote._pick_ai_vote(_seat_ai)
check("AI 没答上来（解析失败）→ 随机兜底一个合法目标，**不许变成弃票**",
      _guess in _others_v, f"got={_guess} 合法={_others_v}")

print("=" * 74)
print("⑩ 并发回归：/public 与 /act 同时推进（AI 连说两次 / 投票提前开）")
# 根因：act() 以前**没有上锁**，而 /public 的 drive() 上了锁。FastAPI 把两个同步端点
# 都丢进线程池 ⇒ 它们真的会同时跑，于是：
#   · 两个线程读到同一个 current_speaker ⇒ 同一个 AI 说两遍
#   · speak_cursor 被各加一次 ⇒ 跳过最后一个发言人、投票提前开始
#   · 一句话的 _say 被挤到"发言结束，开始投票"之后 ⇒ 时间线顺序错乱
import threading  # noqa: E402
import time as _time  # noqa: E402


def _race_round(seed, seconds=3.5):
    """
    一边不停轮询（drive），一边替人类做动作（act）—— 复现并发推进。

    两个要点，缺一个就测不出来：
      · 用 **6 人局**：4 人局常常第一夜就把真人杀死，发言总数太少（实测 8 局只 8 条）
      · 把「模型思考时间」放大到 50ms：mock 是瞬时的，竞态窗口小到撞不上
    """
    s = game_api.Session(humans=1, ais=5, seed=seed)
    # 把「模型思考时间」放大到 50ms。注意要包 speak / act_on_target，
    # 不能包 _ask —— 规则发言器（RuleAgent）根本不走 _ask，包了等于没包。
    for _seat, _ag in s.agents.items():
        _os, _ot = _ag.speak, _ag.act_on_target

        def _slow_speak(view, history, _f=_os):
            _time.sleep(0.05)
            return _f(view, history)

        def _slow_act(view, history, kind, _f=_ot):
            _time.sleep(0.05)
            return _f(view, history, kind)

        _ag.speak = _slow_speak
        _ag.act_on_target = _slow_act

    stop = {"v": False}
    errs = []

    def poller():
        while not stop["v"]:
            try:
                s.drive(0.02)
            except Exception as e:              # noqa: BLE001
                errs.append(f"drive: {type(e).__name__}: {e}")

    th = threading.Thread(target=poller, daemon=True)
    th.start()
    t0 = _time.time()
    while _time.time() - t0 < seconds:
        g = s.game
        if g.phase.value == "ended":
            break
        _wpt = g.night.get("wolf_points") or {}
        need = [(p.seat, g.actions_for(p.seat)) for p in g.players
                if p.kind == "human" and p.alive and g.actions_for(p.seat)
                and not (p.seat in _wpt
                         and set(g.actions_for(p.seat)) <= {"kill", "confirm"})]
        if not need:
            _time.sleep(0.003)
            continue
        seat, acts = need[0]
        v = g.get_view_for(seat)
        others = [q["seat"] for q in v["players"] if q["alive"] and q["seat"] != seat]
        try:
            if "speak" in acts:
                s.act(seat, "speak", None, "过")
            elif "kill" in acts and others:
                s.act(seat, "kill", others[0], None)
            elif "investigate" in acts and others:
                s.act(seat, "investigate", others[0], None)
            elif "vote" in acts and others:
                s.act(seat, "vote", others[0], None)
            else:
                s.act(seat, "pass", None, None)
        except Exception as e:                  # noqa: BLE001
            errs.append(f"act: {type(e).__name__}: {e}")
    stop["v"] = True
    th.join(timeout=5)
    return s, errs


def _round_problems(tl):
    """每一轮里，**所有存活者都必须恰好发过一次言**（跳过最后一个发言人会被抓到）。

    ★ 2026-10-28（遗言规则）：出局者的遗言也是一条 `say`，而它发生在
    "发言结束，开始投票。" / "N 号 被投出并出局。" 之后 ——
    判"这一轮谁没说话"时**必须把它排掉**，否则同一个人会被算成说了两次。
    """
    alive, speakers, out = None, [], []
    runoff = False
    vote_open = False
    for x in tl:
        s = x["s"]
        if x["t"] == "sys":
            m = re.search(r"(\d+)\s*人局", s)
            if m:
                alive = set(range(int(m.group(1))))
            m = re.match(r"昨夜\s*(\d+)\s*号\s*出局", s)
            if m and alive is not None:
                alive.discard(int(m.group(1)) - 1)
            # ⚠️ 毒杀的措辞已并入上面那条统一格式（"昨夜 N 号 出局。"）⇒ 这里只剩投票出局
            for pat in (r"(\d+)\s*号\s*被投出并出局",):
                m = re.search(pat, s)
                if m and alive is not None:
                    alive.discard(int(m.group(1)) - 1)
            # ★ 2026-10-28 平票加时：平票者的"最后一轮陈述"只有平票的人说话，
            #   拿"全员都发过言"去卡它必然误报 ⇒ 加时那一轮整体跳过。
            if "进入加时" in s:
                runoff = True
            if "天亮了" in s or ("第" in s and "夜" in s):
                vote_open = False          # 新一轮开始：讨论重新算
            if "开始投票" in s and alive is not None:
                if runoff:                  # 加时后的重投：不参与"全员发言"判据
                    runoff = False
                    speakers = []
                    vote_open = True
                    continue
                _miss = sorted(a + 1 for a in (alive - set(speakers)))
                _dup = len(speakers) != len(set(speakers))
                if _miss or _dup:
                    out.append(f"缺发言={_miss} 有重复={_dup}（本轮共 {len(speakers)} 条）")
                speakers = []
                vote_open = True
        else:
            if vote_open:
                continue                   # 投票之后还开口的 = 遗言（规则允许，不算讨论发言）
            m = re.match(r"^(\d+)\s*号", s)
            if m:
                speakers.append(int(m.group(1)) - 1)
    return out


_race_errs, _race_probs, _race_says, _race_order = [], [], 0, []
for _seed in range(3):
    _s, _errs = _race_round(_seed)
    _race_errs += [f"seed={_seed} {e}" for e in _errs]
    _tl = _s.public_state()["timeline"]
    print(f"    [race] seed={_seed} phase={_s.game.phase.value} pending={_s.pending} "
          f"tl={len(_tl)} say={len([x for x in _tl if x['t'] == 'say'])} errs={len(_errs)} "
          f"agent={type(list(_s.agents.values())[0]).__name__}")
    _race_says += len([x for x in _tl if x["t"] == "say"])
    _race_probs += [f"seed={_seed} {p}" for p in _round_problems(_tl)]
    _vopen = False
    for _x in _tl:
        if _x["t"] == "sys":
            if "开始投票" in _x["s"]:
                _vopen = True
            # ★ 2026-10-28 平票加时：加时那一轮"平票者再各说一轮"发生在「开始投票」之后，
            #   它是**规则允许的**发言 —— 关掉窗口，别把最后的陈述判成"被挤到投票之后"。
            if "进入加时" in _x["s"]:
                _vopen = False
            # ★ 2026-10-28 遗言：出局者的遗言同样发生在「开始投票」之后（被投出之后），
            #   它也是规则允许的。判据用"有死讯"而不是"有遗言" —— 死讯一定先播。
            if "出局" in _x["s"]:
                _vopen = False
            if "天亮了" in _x["s"] or ("第" in _x["s"] and "夜" in _x["s"]):
                _vopen = False
            continue
        if _vopen and re.match(r"^([0-9]+)\s*号", _x["s"]):
            _race_order.append(f"seed={_seed} {_x['s'][:24]}")

check("前提：并发这 3 局确实产生了发言（对照有意义）", _race_says > 9,
      f"says={_race_says}")   # 3 局 × 至少 3 句；不是精确值，是"确实在说话"的存在性前提
# ★ 这条是这次修复的关键证据：把 act() 变回"不上锁"，同样 3 局会稳定报
#   「RuntimeError: 当前阶段是 night/vote，不允许该操作」2~4 次/局（实测过）。
check("★ 并发推进下不许抛异常（不上锁时每局 2~4 次「当前阶段是…不允许该操作」）",
      not _race_errs, str(_race_errs[:3]))
check("★ 并发推进下：每一轮所有存活者都恰好发过一次言（不跳最后一个发言人）",
      not _race_probs, str(_race_probs[:2]))
check("★ 并发推进下：没有发言被挤到「发言结束，开始投票」之后",
      not _race_order, str(_race_order[:2]))

print("=" * 74)
print("⑪ /act 必须有预算：真人说完一句话，不许把后面所有 AI 的发言都跑完才返回")
check("★ 归票判据用 can_vote（不是 alive）—— 否则翻牌白痴会让归票永远等下去（D5）",
      # 2026-10-06 P2：_all_voted 随驱动器搬到 night_driver.py
      "g.can_vote(p.seat)" in (BASE / "backend" / "night_driver.py").read_text(encoding="utf-8"))
# 机制：act() 里以前是**无预算**的 auto_advance() ⇒ 6 人局 4 个 AI × 每次 2.3 秒 ≈ 9 秒，
# 而前端是拿到响应才开始念第一句 ⇒ 体感就是「我说完，等好久 AI 才开口」。
# 这里把每个 AI 发言放慢到 0.6 秒，然后看「这一次请求里跑了几个 AI 发言」。
_ab_found = False
for _ab_seed in range(6):
    s_ab = game_api.Session(humans=1, ais=5, seed=_ab_seed)
    for _seat, _ag in s_ab.agents.items():
        _os = _ag.speak

        def _sp(view, history, _f=_os):
            _time.sleep(0.6)
            return _f(view, history)

        _ag.speak = _sp
    # 🔴 2026-10-07 第三期：夜里有**固定节拍**（读秒不可跳过）⇒ 推的预算放宽到 ~12 秒，
    # 否则"人类还没轮到发言"会被误判成"造不出样本"。
    for _i in range(120):
        if s_ab.pending == "human_speak":
            break
        s_ab.drive(0.1)
    if s_ab.pending != "human_speak":
        continue                      # 真人在这一局里可能已经出局，换一个种子
    _say0 = len([x for x in s_ab.public_state()["timeline"] if x["t"] == "say"])
    _t0 = _time.time()
    s_ab.act(0, "speak", None, "过")
    _dt = _time.time() - _t0
    _say1 = len([x for x in s_ab.public_state()["timeline"] if x["t"] == "say"])
    _made = _say1 - _say0 - 1         # 减掉真人自己那一条
    check("★ 一次 /act 只推一两个动作就返回（有预算）", _made <= 2,
          f"这次请求里跑了 {_made} 个 AI 发言、用了 {_dt:.1f}s（无预算时会跑完所有剩下的人）")
    _ab_found = True
    break
if not _ab_found:
    check("一次 /act 只推一两个动作就返回（有预算）", False, "6 个种子里没造出「轮到真人发言」")

print("=" * 74)
print("⑫ 12 人局（含守卫/猎人）能从开局跑到分出胜负")
# 12 人局 = 4 狼 + 女巫/预言家/猎人/守卫 + 4 民，会把新加的两条路径都走到：
# 守卫每晚的 guard 动作、以及猎人出局后的 SHOOT 相位。
# 只在引擎层测判例是不够的 —— 相位机接不上照样会卡死在一局里。
errs12 = []
s12 = game_api.Session(humans=1, ais=11, seed=3)
reached_end = False
# ★ 2026-10-28：狼刀改成"整窗可改、到点才结算"之后，人狼点过的 `kill` 会一直挂在
#   待办里；不跳过的话机器人只会反复提交同一个点子、从不 drive ⇒ 墙上时间不走 ⇒
#   整局卡在第一夜（实测轮次=1、kill_settled=False）。
#   同时把循环次数放宽：窗口按人数定死（12 人局 90 秒，本文件缩到 0.02 倍 =
#   1.8 秒/节点），旧的 500 拍够不到一局打完。
for _i in range(3000):
    g = s12.game
    if g.phase.value == "ended":
        reached_end = True
        break
    _wpt12 = g.night.get("wolf_points") or {}
    need = [(p.seat, g.actions_for(p.seat)) for p in g.players
            if p.kind == "human" and g.actions_for(p.seat)
            and not (p.seat in _wpt12
                     and set(g.actions_for(p.seat)) <= {"kill", "confirm"})]
    if not need:
        s12.drive(0.05)
        continue
    seat, acts = need[0]
    v = g.get_view_for(seat)
    others = [q["seat"] for q in v["players"] if q["alive"] and q["seat"] != seat]
    try:
        _pts = v.get("wolf_points") or {}
        _pointed = _pts.get(seat) is not None
        if "speak" in acts:
            s12.act(seat, "speak", None, "过")
        elif "confirm" in acts and _pointed:
            s12.act(seat, "confirm", None, None)       # 狼队协商第二步
        elif "shoot" in acts:
            s12.act(seat, "pass", None, None)          # 真人猎人选择不开枪
        elif "guard" in acts:
            last = g.private[seat].guard_last
            pool = [q["seat"] for q in v["players"] if q["alive"] and q["seat"] != last]
            s12.act(seat, "guard", pool[0] if pool else None, None)
        elif "kill" in acts and others:
            s12.act(seat, "kill", others[0], None)
        elif "investigate" in acts and others:
            s12.act(seat, "investigate", others[0], None)
        elif "vote" in acts and others:
            s12.act(seat, "vote", others[0], None)
        else:
            s12.act(seat, "pass", None, None)
    except Exception as e:                              # noqa: BLE001
        errs12.append(f"{type(e).__name__}: {e}")

_night_keys = ("kill", "kill_settled", "wolf_points", "wolf_confirm", "wolf_steps",
               "investigate", "witch_asked", "guard_asked")
check("★ 12 人局跑得完（含守卫每晚动作 + 猎人开枪相位）", reached_end,
      f"phase={s12.game.phase.value} pending={s12.pending} errs={errs12[:2]} "
      f"night={ {k: v for k, v in s12.game.night.items() if k in _night_keys} } "
      f"轮次={s12.game.round} "
      f"待办={[(p.seat, p.kind, s12.game.actions_for(p.seat)) for p in s12.game.players if s12.game.actions_for(p.seat)][:4]}")
check("12 人局真的配上了守卫与猎人",
      all(any(p.role.value == _r for p in s12.game.players)
          for _r in ("guard", "hunter")),
      str(sorted({p.role.value for p in s12.game.players})))
check("整局没有抛异常（新相位没接错）", not errs12, str(errs12[:3]))
# 死掉的猎人也要能被"叫到"：pending_seats 在 SHOOT 相位不能把他当死人跳过，
# 否则前端永远收不到"该把手机交给 X 号" ⇒ 卡死在 human_shoot。
from engine import Board as _Board, Role as _R  # noqa: E402

# ⚠️ 不再往 engine.BOARDS 里塞测试板（那个字典已经删了）：板子对象直接传给 Session。
_t_hunter_board = _Board(
    key="_t_hunter", label="测试板：猎人/狼/2 民",
    roles=[_R.HUNTER, _R.WOLF, _R.VILLAGER, _R.VILLAGER], lives=1)
_found_h = False
try:
    for _sd in range(30):
        _s = game_api.Session(humans=1, board=_t_hunter_board, seed=_sd)
        _g = _s.game
        if _g.players[0].role is not _R.HUNTER:
            continue                                  # 换种子，直到人类正好是猎人
        if _g.phase.value == "ended":
            continue
        if _g.phase.value != "night":
            _g._open_night()                          # 测试要的是 SHOOT 相位的过滤，不是规则
        _g.night["kill"] = 0                          # 人类猎人被刀
        _g.night["guard_asked"] = True
        _g.night["witch_asked"] = True
        _g.night["investigate"] = -1
        _g.resolve_night()
        game_api.SESSIONS[_s.id] = _s
        _need = game_api.pending_seats(_s.id)["seats"]
        check("★ 刚死的猎人仍会出现在「该谁操作」里（SHOOT 相位，否则前端永远不叫他）",
              _g.phase.value == "shoot" and _g.shooter == 0
              and any(x["seat"] == 0 and "shoot" in x["actions"] for x in _need),
              f"phase={_g.phase.value} shooter={_g.shooter} seats={_need}")
        game_api.SESSIONS.pop(_s.id, None)
        _found_h = True
        break
finally:
    game_api.SESSIONS.pop(_s.id, None)      # 测完清掉这局（板子对象本来就是我自己的，不用回收）
if not _found_h:
    check("刚死的猎人仍会出现在「该谁操作」里", False, "30 个种子里人类都没拿到猎人")
check("分出胜负了", s12.game.winner in ("wolf", "good"), str(s12.game.winner))

print("=" * 74)
print("⑬ 狼队夜间协商（人类狼 + AI 狼）端到端")
# 老实现的硬性让位：**只要场上有任何人类狼，AI 狼就完全不动** ⇒ 人类狼独裁；
# 两只狼白天各投各的、一只跟大势一只咬好人 ⇒ 立刻暴露（用户实测报的"狼人投票太蠢"）。
# 现在两只狼都参与：先各自点 → 再各自确定；AI 默认**跟随**人狼那一票。
found13 = False
for _sd in range(60):
    _s = game_api.Session(humans=1, ais=5, seed=_sd)          # 6 人局 = 2 狼
    _g = _s.game
    _wh = next((p for p in _g.players if p.role.is_wolf and p.kind == "human"), None)
    _wa = next((p for p in _g.players if p.role.is_wolf and p.kind == "ai"), None)
    if _wh is None or _wa is None:
        continue
    for _i in range(30):                                     # 等到"夜里 + AI 狼已点"
        if _g.phase.value == "night" and _g.night.get("wolf_points"):
            break
        _s.drive(0.05)
    if "kill" not in _g.actions_for(_wh.seat):
        continue
    check("★ 有人类狼时 AI 狼不再被挡在场外（老实现里人类狼独裁、AI 狼闭嘴）",
          bool(_g.night.get("wolf_points")), str(_g.night.get("wolf_points")))
    _vw = _g.get_view_for(_wh.seat)
    check("★ 人类狼看得到队友此刻点了谁（面杀里交换的那个眼神）",
          len(_vw.get("wolf_points") or {}) == 2, str(_vw.get("wolf_points")))
    _good13 = next(p.seat for p in _g.alive if not p.role.is_wolf)
    _s.act(_wh.seat, "kill", _good13, None)
    for _i in range(30):
        if _g.night.get("kill_settled"):
            break
        if "confirm" in _g.actions_for(_wh.seat):
            _s.act(_wh.seat, "confirm", None, None)           # 真人按「确定」
            continue
        _s.drive(0.05)
    check("★ 人狼点完 + AI 跟随 + 双方确定 ⇒ 定案，且刀的是人狼点的那个",
          _g.night.get("kill") == _good13 and _g.night.get("kill_settled"),
          f"kill={_g.night.get('kill')} 期望={_good13} "
          f"settled={_g.night.get('kill_settled')}")
    check("★ 协商过程一个字都没进公共播报",
          not any("狼队" in x for x in _g.public_log), str(_g.public_log[-3:]))
    found13 = True
    break
if not found13:
    check("狼队协商端到端（人类狼 + AI 狼）", False, "60 个种子里没造出该场景")

print("=" * 74)
print("⑭ 发言/遗言 120 秒上限 + 剩 30 秒提醒（2026-10-28 用户定）")
# 用户原话：「限制每人发言时间最长为120秒，在还剩30秒时法官喊"还剩30秒"，
#           倒计时120秒结束后直接进入下一个人发言。（仍保留可以提前结束发言）」
# **两条硬约束**：
#   ① 120/30 只有一份来源（`speech_io`），且必须走 `night_script._scaled()`——
#      否则测试缩了夜窗、发言却还是 120 秒真时间 ⇒ 出现"半缩"状态，整局卡死。
#   ② 到点由**服务端**收尾：前端倒计时归零不许自己提交（前端提交 = 两台手机各判各的）。
import speech_io  # noqa: E402
from agents import JUDGE_VOICE as _ag_voice  # noqa: E402

check("★ 发言上限是 120 秒、剩 30 秒提醒（数字写死在这一处）",
      (speech_io.SPEECH_MAX, speech_io.SPEECH_LAST_CALL) == (120, 30),
      f"{speech_io.SPEECH_MAX}/{speech_io.SPEECH_LAST_CALL}")

_saved_sp = night_script.win_scale()
try:
    night_script.set_win_scale(1.0)           # 本文件全局缩到 0.02，这里先还原成真机口径
    check("★ 不缩窗时两个时限就是原值（_scaled(1.0) 不许改数）",
          (speech_io.speech_window(), speech_io.speech_last_call()) == (120.0, 30.0),
          f"{speech_io.speech_window()}/{speech_io.speech_last_call()}")
    night_script.set_win_scale(0.01)          # 120s → 1.2s；提醒点在 0.9s
    check("★ 两个时限都走同一把缩窗尺子（缩夜窗必然同缩发言，不许只缩一半）",
          abs(speech_io.speech_window() - 1.2) < 1e-6
          and abs(speech_io.speech_last_call() - 0.3) < 1e-6,
          f"{speech_io.speech_window()}/{speech_io.speech_last_call()}")
    check("★ 「还剩30秒」用**原始秒数**判定（缩到 1.2 秒的窗口里照样该喊）",
          speech_io.speech_has_last_call() is True)

    # ---- 端到端：人类发言到点 ⇒ 服务端自动收尾、直接进下一位 ----
    _sp_hit = None
    for _sd in range(30):
        _ss = game_api.Session(humans=1, ais=3, seed=_sd)
        _hs = next(p.seat for p in _ss.game.players if p.kind == "human")
        _tq = _time.time()
        while _time.time() - _tq < 20.0:              # 一夜四段（缩窗后约 4 秒）
            _pd = _ss.drive(0.05)
            _ns = _ss.now_state()
            if _ns.get("kind") == "speak" and _ns.get("seat") == _hs:
                _sp_hit = (_ss, _hs, _ns)
                break
            if _pd == "ended" or _ss.game.winner:
                break
            _time.sleep(0.01)
        if _sp_hit:
            break
    if _sp_hit is None:
        check("人类发言 120 秒到点自动进下一位", False, "30 个种子里没轮到人类发言")
    else:
        _ss, _hs, _ns0 = _sp_hit
        check("★ 轮到人类发言时 `now_state` 把剩余秒数直接下发（前端只做减法、不自己掐表）",
              0 < float(_ns0.get("until_left") or 0) <= 1.2,
              f"until_left={_ns0.get('until_left')}")
        _t0 = _time.time()
        _auto = False
        # ⚠️ 用**墙上时间**做上界，不用"循环多少次"：`drive()` 在等人类时
        #    立刻返回（不 sleep），几百次循环可能在 0.3 秒内跑完 —— 那样测的是
        #    "循环跑得快不快"，不是"到点收不收尾"（第一版就这么假绿/假红过）。
        while _time.time() - _t0 < 8.0:
            _ss.drive(0.05)
            _n1 = _ss.now_state()
            if not (_n1.get("kind") == "speak" and _n1.get("seat") == _hs):
                _auto = True
                break
            _time.sleep(0.02)
        _el = _time.time() - _t0
        check("★ 人类**一句话没说**也不提前结束 ⇒ 到点服务端自动收尾、直接进下一位",
              _auto, f"{_el:.2f}s 后仍停在这个人身上")
        check("★ 收尾发生在窗口到点的时刻（≈1.2s），不是前端提交、也不是拖到下一拍很久",
              _auto and 1.0 <= _el <= 3.0, f"{_el:.2f}s")
        _c30 = [x for x in _ss.timeline if str(x.get("s", "")) == "还剩30秒"]
        check("★ 剩 30 秒法官喊「还剩30秒」—— 而且只喊一次（不刷屏）",
              len(_c30) == 1, f"{len(_c30)} 次")
        check("★ 提醒是法官音色（不是玩家音色）",
              bool(_c30) and _c30[0].get("voice") == _ag_voice,
              str(_c30[:1])[:90])

    # ---- 遗言走同一套计时（同窗长、同提醒、同自动收尾）----
    _sl = game_api.Session(humans=1, ais=8, seed=5)   # 9 人局：死一个不会立刻判胜负
    _gl = _sl.game
    _hl = next(p.seat for p in _gl.players if p.kind == "human")
    _gl.player(_hl).alive = False                     # 造一个"刚出局、该留遗言"的人
    _gl.deaths.append({"seat": _hl, "round": 1, "cause": "kill", "lastword": True})
    _gl.phase = Phase.DAWN                           # 遗言只能从「刚死」进来（DAWN/EXILE）
    _gl.open_lastwords([_hl], "discuss")
    _sl.drive(0.05)
    _nl = _sl.now_state()
    check("★ 遗言是**发言同类型**：同样是 `kind=speak`，同样带剩余秒数",
          _nl.get("kind") == "speak" and _nl.get("seat") == _hl
          and 0 < float(_nl.get("until_left") or 0) <= 1.2,
          str(_nl)[:110])
    check("★ 遗言轮到谁，`public_state().lastword_seat` 就指谁（前端画面板的判据）",
          _sl.public_state().get("lastword_seat") == _hl,
          str(_sl.public_state().get("lastword_seat")))
    _t1 = _time.time()
    _lw_auto = False
    while _time.time() - _t1 < 8.0:                   # 同上：按墙上时间等，不按次数
        _sl.drive(0.05)
        if _sl.public_state().get("lastword_seat") != _hl:
            _lw_auto = True
            break
        _time.sleep(0.02)
    _el1 = _time.time() - _t1
    check("★ 遗言不说也到点自动收尾（同一条看门狗，不是另写一套）",
          _lw_auto and 1.0 <= _el1 <= 3.0, f"{_el1:.2f}s")
    # 判据看**时间线**，不看"此刻的 phase"：全 RuleAgent 的一局在一次 drive()
    # 里就能把整个白天跑完 —— 等这里返回时早就进了下一夜，"phase 是不是 discuss"
    # 测的是运气。真正要锁的是"遗言之后确实接回了白天发言"。
    _tl = [str(x.get("s", "")) for x in _sl.timeline]
    # 认**这一位人类自己**那句开场白：一局里后面还会有人出局、还有别的遗言，
    # 取"最后一条请留遗言"会取到别人身上（第一版就这么误判过）。
    _i_lw = max([i for i, s in enumerate(_tl)
                 if s == f"{_hl + 1}号请留遗言"] or [-1])
    # ⚠️ 2026-10-29：这里原来还要求遗言之后出现「天亮了…」—— 那条断言跟着
    #   「天亮句挪进 resolve_night（先报天亮、再报死讯）」一起失效了：
    #   本篇是**手工**从 DAWN 直接开遗言、没走 resolve_night，自然没有天亮句。
    #   天亮与死讯的先后已由 ⑯③ 单独钉住，这里只锁"接回白天发言"。
    check("★ 遗言说完接回白天发言（不跳过发言直接进黑夜）",
          _i_lw >= 0
          and any(s.endswith("请发言") for s in _tl[_i_lw:]),
          str(_tl[_i_lw:][:4]))
finally:
    night_script.set_win_scale(_saved_sp)

# ================================================================ ⑮ 2026-10-29
# 「当前发言玩家高亮要落在**号码小圆**上」+「大圆写 ×号正在发言中」+
# 「法官念数字不许数字与汉字之间留空白」+「终局弹框要在播报之后且一定会弹」。
# 这几条都是**前端行为**，Python 这边没有 DOM —— 所以钉的是"实现有没有在那儿"，
# 语义靠无头浏览器截图验收（见 _handoff/probe_*）。判据写成"必须出现的字符串"，
# 有人把这些删了/改回去了，这里就会红。
print("=" * 74)
print("⑮ 前端三处修复（高亮 / 大圆文案 / 播报去空格 / 终局弹框）")
print("=" * 74)

_FE = BASE / "frontend"
_play = (_FE / "play.html").read_text(encoding="utf-8")
_tui = (_FE / "tableui.js").read_text(encoding="utf-8")
_jv = (_FE / "judge_voice.js").read_text(encoding="utf-8")
_sui = (_FE / "speech_ui.js").read_text(encoding="utf-8")

check("★ 号码小圆的高亮样式真的存在（.wnum.speaking）",
      ".wnum.speaking{" in _play.replace(" ", ""))
check("★ 高亮是加在 .wnum 上（不是被 #p4 藏掉的 .seat）",
      "paintNumberGlow" in _tui and "querySelectorAll('.wnum')" in _tui)
check("★ 大圆发言文案 =「N号正在发言中」",
      "'号正在发言中'" in _tui)
check("★ 发言态要藏掉「法官」标题（否则一句话两个『谁在说』）",
      "#p4 .tcenter.talking .t{ display:none; }" in _play)
check("★ 法官播报去掉「数字 + 空白 + 汉字」的间隔（3 号 → 3号）",
      r"([\u4e00-\u9fa5\d])\s+(?=[\u4e00-\u9fa5\d])" in _jv)
check("★ 终局弹框有『队列空闲』兜底（TTS 卡住也要弹）",
      "!SPEAKING" in _play and "1500" in _play)
check("★ 终局弹框有『这句是 20 秒前的』兜底（刷新/接回旧局照弹）",
      "_lineAt" in _play and "> 20" in _play)
check("★ 播报队列一放完就判一次终局（念完才弹，不等下一拍）",
      "renderEndPop" in _sui)

# ================================================================ ⑯ 2026-10-29
# 用户四条（原话）：
#   1. 执行顺序与播报顺序改为一致
#   2. 守卫/女巫窗口点击后不退出、时间结束前均可改、以结束时的选择为准；
#      预言家增加确认键，点人后再确认才报结果，同样时间结束才退窗
#   3. 夜间窗口与播报严格对齐；天亮时先播「天亮了」再播死讯
#   4. 白天投票完成后等法官念完投票结果与出局信息，若未结束则喊「天黑请闭眼」，
#      喊完才进夜间页面
print("=" * 74)
print("⑯ 夜间顺序统一 / 窗口内可改 / 预言家确认 / 面板等「天黑请闭眼」")
print("=" * 74)

import steps as _steps  # noqa: E402
import night_driver as _ndrv  # noqa: E402
import types as _types  # noqa: E402

_ord = _steps.night_step_names()
check("★ ① 夜间执行顺序 = 守卫→狼→女巫→预言家（与播报逐字一致）",
      tuple(_ord) == ("guard", "wolf", "witch", "seer"), str(_ord))

_nights = night_script.build({"guard", "wolf", "witch", "seer"}, 1)
_first = {}
for _nd0 in _nights:
    _k = {"witch_heal": "witch", "witch_poison": "witch"}.get(_nd0["id"], _nd0["id"])
    _first.setdefault(_k, _nd0["id"])
_bcast = [k for k in ("guard", "wolf", "witch", "seer") if k in _first]
check("★ ① 播报顺序也是 守卫→狼→女巫→预言家（同一张表推出来的）",
      _bcast == list(_ord), str(_bcast))

# ---- ②-1 守卫：点了不退出，以关窗时的选择为准 --------------------------
from engine import Board as _B16, Game as _G16, Role as _R16  # noqa: E402

_b16 = _B16(key="_t_win", label="测试板：守卫/女巫/预言家/狼/2 民",
            roles=[_R16.GUARD, _R16.WITCH, _R16.SEER, _R16.WOLF,
                   _R16.VILLAGER, _R16.VILLAGER], lives=1)
_g16 = _G16(_b16, ["human"] * 6, seed=3)
_g16.start()
_guard = next(p for p in _g16.players if p.role is _R16.GUARD)
_a, _b = [p.seat for p in _g16.players if p.seat != _guard.seat][:2]
_g16.night_guard(_guard.seat, _a)
_acts1 = _g16.actions_for(_guard.seat)
check("★ ② 守卫点完人后**窗口不退出**（待办里仍有 guard）",
      "guard" in _acts1 and not _g16.night.get("guard_settled"),
      f"acts={_acts1} settled={_g16.night.get('guard_settled')}")
_g16.night_guard(_guard.seat, _b)
check("★ ② 窗口内可以改守护对象（以最后一次为准）",
      _g16.night.get("guard") == _b, f"guard={_g16.night.get('guard')} 期望 {_b}")
_g16.settle_guard_window()
check("★ ② 关窗后才定案（guard_settled + 待办消失）",
      _g16.night.get("guard_settled") and "guard" not in _g16.actions_for(_guard.seat),
      f"acts={_g16.actions_for(_guard.seat)}")

# ---- ②-2 女巫解药：关窗才扣药 -----------------------------------------
_g16b = _G16(_b16, ["human"] * 6, seed=3)
_g16b.start()
_wit = next(p for p in _g16b.players if p.role is _R16.WITCH)
_wolf0 = next(p for p in _g16b.players if p.role.is_wolf)
_vic = next(p.seat for p in _g16b.good_alive() if p.seat != _wit.seat)
_g16b.wolf_agree_kill(_vic)
# ⚠️ 必须显式给 stage：`stage is None` 是"老调用方（引擎单测/回放/脚本）"的兼容路，
#   那条路是**立刻扣药**的老语义。真机上 stage 由节点时钟写入（`_nn_open`）。
_g16b.night["stage"] = "witch_heal"
_g16b.night_heal(_wit.seat, _vic)
check("★ ② 女巫救人后窗口不退出、**药还没扣**（改主意还来得及）",
      "heal" in _g16b.actions_for(_wit.seat)
      and _g16b.private[_wit.seat].potions["heal"] is True
      and _g16b.night.get("heal_picked") is True,
      f"acts={_g16b.actions_for(_wit.seat)} potion={_g16b.private[_wit.seat].potions['heal']}")
_g16b.settle_witch_heal()
check("★ ② 关窗那一刻才扣解药（且只扣一次）",
      _g16b.private[_wit.seat].potions["heal"] is False
      and _g16b.night.get("heal") == _vic,
      f"potion={_g16b.private[_wit.seat].potions['heal']}")
_g16b.night["heal"] = None
_g16b.settle_witch_heal()          # 幂等：别再扣第二次（本来就没药了）
check("★ ② settle_witch_heal 幂等（重复调用不改变已定案的选择）",
      _g16b.night.get("heal") is None, str(_g16b.night.get("heal")))

# ---- ②-3 预言家：点人 → 确认 → 才报结果 --------------------------------
_g16c = _G16(_b16, ["human"] * 6, seed=3)
_g16c.start()
_seer = next(p for p in _g16c.players if p.role is _R16.SEER)
_t = next(p.seat for p in _g16c.players if p.seat != _seer.seat)
_g16c.night_investigate(_seer.seat, _t)
_seer_acts = _g16c.actions_for(_seer.seat)
check("★ ② 预言家点完人后**还没报结果**，且面板上多出「确认」",
      "investigate" in _seer_acts and "confirm" in _seer_acts
      and not _g16c.night.get("seer_confirmed"),
      f"acts={_seer_acts}")
try:
    _g16c.night["investigate"] = None
    _g16c.night_seer_confirm(_seer.seat)
    _raised = False
except RuntimeError:
    _raised = True
check("★ ② 没点人就不能确认（否则会「报出」一个不存在的验人结果）", _raised)
_g16c.night_investigate(_seer.seat, _t)
_res = _g16c.night_seer_confirm(_seer.seat)
check("★ ② 确认这一刻才拿到结果（= 是否狼人）",
      isinstance(_res, bool) and _g16c.night.get("seer_confirmed") is True
      and _res == _g16c.player(_t).role.is_wolf, f"res={_res}")

# ---- ③ 天亮 → 死讯（顺序）--------------------------------------------
_g16d = _G16(_b16, ["human"] * 6, seed=3)
_g16d.start()
_v3 = next(p.seat for p in _g16d.good_alive())
_g16d.wolf_agree_kill(_v3)
_g16d.settle_wolf_window()
_g16d.settle_guard_window()
_g16d.settle_witch_heal()
_g16d.settle_witch_poison()
_g16d.settle_seer_window()
_g16d.resolve_night()
_i_light = next((i for i, s in enumerate(_g16d.public_log) if "天亮了" in s), -1)
_i_next = _g16d.public_log[_i_light + 1] if _i_light >= 0 else ""
check("★ ③ 天亮时法官**先**说「天亮了」，下一句才是死讯（昨夜…出局/平安夜）",
      _i_light >= 0 and ("出局" in _i_next or "倒牌" in _i_next or "平安夜" in _i_next),
      f"light@{_i_light} next={_i_next!r}")

# ---- ④ 面板起算点必须排到「天黑请闭眼」之后 -----------------------------
# 现场（用户实测的那一段）：投票结果 + 出局信息刚进队列，引擎紧接着就开了夜。
# 这三句都是**法官播报**（t == "sys"、没有 node 字段），客户端要串行念完。
# 老口径只累加带 node 的夜间剧本句 ⇒ 起算点从"天黑请闭眼"那一刻开始算，
# 白天那几句根本没计入 ⇒ 面板在白天播报还没念完时就弹（这正是用户第 4 条）。
_scale_saved4 = night_script.win_scale()
night_script.set_win_scale(0.5)          # 放大到能明显区分两种口径
try:
    _t0 = 1_700_000_000.0
    _GAP = night_script.GAP
    _fake = _types.SimpleNamespace(timeline=[
        {"t": "sys", "s": "投票结果：2号、3号、4号投给5号；5号投给2号；6号弃票。",
         "voice": "Neil", "ts": _t0},
        {"t": "sys", "s": "5号 被投出并出局。", "voice": "Neil", "ts": _t0 + 0.01},
        # 玩家长发言（t == "say"）：几百字，绝不许进串行累加，否则起算点被推到一分钟外
        {"t": "say", "s": "我" * 400, "seat": 1, "voice": "", "ts": _t0 + 0.02},
        {"t": "sys", "s": "第 2 夜，天黑请闭眼。", "voice": "Neil",
         "delay": _GAP, "ts": _t0 + 0.03},
        {"t": "sys", "s": "守卫请睁眼，请选择今晚你要守护的人", "voice": "Neil",
         "delay": _GAP, "node": "guard", "at": _t0 + 0.04},
    ])
    _est = _ndrv._nn_ready_mono(_fake, "guard")
    # `_nn_ready_mono` 回的是 **monotonic**，而时间线里的 `ts/at` 是墙上时间 ——
    # 换算回墙上再比，不然比出来是 -17 亿秒（第一次写就是这么错的）。
    _est_wall = (_est - _time.monotonic() + _time.time()) if _est is not None else None
    _sc4 = night_script.win_scale()
    _dur = lambda s: _sc4 * (0.5 + len(s) / 3.4)   # 与实现同源的口径（含缩放）
    _s_vote = "投票结果：2号、3号、4号投给5号；5号投给2号；6号弃票。"
    _s_dead = "5号 被投出并出局。"
    _span_day = _dur(_s_vote) + _dur(_s_dead)    # 白天两句加起来（scale 0.5 ⇒ ≈6.2s）
    _d = (_est_wall or 0.0) - _t0
    check("★ ④ 白天「投票结果 / 出局信息」也算进播报时长（面板不许插队）",
          _est is not None and _d >= _span_day,
          f"est 只比开夜晚 {_d:.2f}s，白天两句就要念 {_span_day:.2f}s")
    check("★ ④ 起算点也排在「天黑请闭眼」念完之后",
          _est is not None and _d > _span_day + 3.0,
          f"est-t0={_d:.2f}s（+3s = GAP 留白）")
    check("★ ④ 玩家长发言（t=say）**不参与**累加（否则一夜的面板会被推后一分钟）",
          _est is not None and _d < 20,
          f"est-t0={_d:.2f}s")
finally:
    night_script.set_win_scale(_scale_saved4)

# ---- ④ 前端：确认键 / 关窗留面板 -------------------------------------
check("★ ② 前端有「确认验人」这颗键（点人之后才出现）",
      "确认验人" in _tui)
check("★ ② 前端把 confirm 当独立动作提交（不是选中即报结果）",
      "'seer_confirm'" in _tui or "seer_confirm" in _tui)
check("★ ② 后端把 seer_confirm 映射成 night_seer_confirm + 播「他是这个」",
      "night_seer_confirm" in (BASE / "backend" / "game_api.py").read_text(encoding="utf-8"))
check("★ ③ 前端认得 `now.closing`（闭眼句还在念时窗口不许先收）",
      "NOW.closing" in _tui)
check("★ ③ 后端把 `closing` 下发给前端（now_state 的 none 分支）",
      '"closing"' in (BASE / "backend" / "game_api.py").read_text(encoding="utf-8"))
check("★ ③ 节点推进要等闭眼句念完（_nn_closing 闸）",
      "_nn_closing" in (BASE / "backend" / "night_driver.py").read_text(encoding="utf-8"))

# ================================================================ ⑰ 2026-10-30
# 用户实机：「夜间还没结束、其他玩家还没发言时，圆盘就显示了 4 号（真人）
#            正在发言中，且倒计时在减少」
# 根因两层：
#   ① 服务端：发言的 120 秒从「驱动推进到 DISCUSS 那一拍」起算，而推进**不等播报**
#      ⇒ 法官还在念「天亮了…昨夜 X 出局…4号请发言」，读秒已经在掉。
#   ② 前端：大圆只看 `current_speaker` 就画「X号正在发言中」，没有播报门
#      （`_gateOk` 对 speak 用的是 voicedUp()，本机音频队列暂时为空时会放行）。
# 修法：`now.started` 下发（语义与夜间 wheel 完全一致）+ 计时起点 = 那句念完。
print("=" * 74)
print("⑰ 发言必须等「N号请发言」念完（夜间播报里不许抢先显示/读秒）")
print("=" * 74)

from engine import Phase as _Ph17  # noqa: E402

_scale17 = night_script.win_scale()
night_script.set_win_scale(1.0)    # 量"还没念完"必须用真实时长（缩窗后只剩 0.04s）
try:
    _s17 = game_api.Session(humans=6, ais=0, seed=3)
    _g17 = _s17.game
    _g17.phase = _Ph17.DISCUSS
    _g17.speaking_order = [p.seat for p in _g17.alive]
    _g17.speak_cursor = 0
    _spk17 = _g17.current_speaker()
    speech_io._announce_speaker(_s17, _spk17)   # 法官刚把「N号请发言」排进队列
    _t17 = _time.monotonic()
    _st17 = _ndrv._sp_state(_s17, "speak", _spk17)
    _n17 = _s17.now_state()
    _say17 = f"{_spk17 + 1}号请发言"
    _dur17 = 0.5 + len(_say17) / 3.4
    check("★ ⑰ 计时起点 =「N号请发言」念完那一刻（不是驱动推进那一拍）",
          _st17["at"] - _t17 >= _dur17 * 0.8,
          f"at-now={_st17['at'] - _t17:.2f}s 该句≈{_dur17:.2f}s")
    check("★ ⑰ 这句没念完 ⇒ now.started=False",
          _n17.get("kind") == "speak" and _n17.get("started") is False,
          str(_n17)[:100])
    check("★ ⑰ 没念完时读秒是满窗（不许已经在掉）",
          abs((_n17.get("until_left") or 0) - speech_io.SPEECH_MAX) < 0.01,
          f"until_left={_n17.get('until_left')}")
    _time.sleep(max(0.0, _st17["at"] - _time.monotonic()) + 0.05)
    _n17b = _s17.now_state()
    check("★ ⑰ 念完之后 started=True，读秒才从满窗开始走",
          _n17b.get("started") is True
          and (_n17b.get("until_left") or 0) <= speech_io.SPEECH_MAX,
          f"started={_n17b.get('started')} left={_n17b.get('until_left')}")
finally:
    night_script.set_win_scale(_scale17)

# 前端：大圆 / 号码高亮 / 读秒 / 面板四处都必须认这个 started
check("★ ⑰ 前端有 `speakPending`（started=false 时大圆退回法官）",
      "function speakPending" in _tui and "speakPending()" in _tui)
check("★ ⑰ 大圆文案与号码高亮都走 speakingSeat()（一处挡住、两处生效）",
      "if (speakPending()) return null;" in _tui)
check("★ ⑰ 面板/发言屏的闸把 speak 也纳入 started（不再只靠 voicedUp）",
      "NOW.kind === 'speak'" in _tui)
check("★ ⑰ started=false 时不显示读秒",
      "NOW.kind === 'speak' && NOW.started === false" in _tui)
check("★ ⑰ 后端把 started 下发给发言屏（now_state 的 speak 分支）",
      "_speech_started" in (BASE / "backend" / "game_api.py").read_text(encoding="utf-8"))

# ================================================================ ⑱ 2026-10-30
# 用户定：「狼队夜间刀口从原来的平票改为**以真人刀口为准**」
#   真人选 5 号、其余 AI 都选 6 号 ⇒ 刀 5 号；真人选空刀 ⇒ 当夜空刀；
#   真人整窗没表态 ⇒ 才按 AI 刀口走原来的多数决（平票随机、空刀不计数）。
print("=" * 74)
print("⑱ 狼刀：真人表态即定刀（AI 票不再计入）")

_b18 = _B16(key="_t_wolf18", label="测试板：3 狼 + 预言家 + 2 民",
            roles=[_R16.WOLF, _R16.WOLF, _R16.WOLF,
                   _R16.SEER, _R16.VILLAGER, _R16.VILLAGER], lives=1)


def _wolf18(seed: int, human_idx=None):
    """造一局 3 狼 3 好；human_idx 指定把第几只狼改成真人。"""
    g = _G16(_b18, ["ai"] * 6, seed=seed)
    g.start()
    ws = [p.seat for p in g.alive if p.role.is_wolf]
    if human_idx is not None:
        g.player(ws[human_idx]).kind = "human"
    goods = [p.seat for p in g.alive if not p.role.is_wolf]
    return g, ws, goods


# ---- ⑱-1 真人一票压过 AI 两票 -----------------------------------------
_g18a, _w18a, _d18a = _wolf18(71, human_idx=1)
_g18a.night_kill(_w18a[0], _d18a[1])
_g18a.night_kill(_w18a[1], _d18a[0])       # 真人（第 2 只狼）点 d0
_g18a.night_kill(_w18a[2], _d18a[1])
_g18a.settle_wolf_window()
check("★ ⑱ 真人 1 票 vs AI 2 票 ⇒ **听真人的**（刀 d0，不是 d1）",
      _g18a.night.get("kill") == _d18a[0]
      and _g18a.night.get("wolf_decided_by") == "human",
      f"kill={_g18a.night.get('kill')} 期望={_d18a[0]} by={_g18a.night.get('wolf_decided_by')}")

# ---- ⑱-2 真人点空刀 ⇒ 当夜空刀 ----------------------------------------
_g18b, _w18b, _d18b = _wolf18(72, human_idx=1)
_g18b.night_kill(_w18b[0], _d18b[0])
_g18b.night_kill(_w18b[1], -1)             # 真人：空刀
_g18b.night_kill(_w18b[2], _d18b[0])
_g18b.settle_wolf_window()
check("★ ⑱ 真人点空刀 ⇒ 当夜空刀（AI 两张刀票全部作废）",
      _g18b.night.get("kill") == -1
      and _g18b.night.get("kill_settled")
      and _g18b.night.get("wolf_decided_by") == "human",
      f"kill={_g18b.night.get('kill')} by={_g18b.night.get('wolf_decided_by')}")

# ---- ⑱-3 真人未表态 ⇒ 回落 AI 多数决 ----------------------------------
_g18c, _w18c, _d18c = _wolf18(73, human_idx=1)
_g18c.night_kill(_w18c[0], _d18c[0])
_g18c.night_kill(_w18c[2], _d18c[0])       # 真人 _w18c[1] 一次没点
_g18c.settle_wolf_window()
check("★ ⑱ 真人没表态 ⇒ 按 AI 刀口多数决（不能被当成「真人投了空刀」）",
      _g18c.night.get("kill") == _d18c[0]
      and _g18c.night.get("wolf_decided_by") == "ai",
      f"kill={_g18c.night.get('kill')} by={_g18c.night.get('wolf_decided_by')}")

# ---- ⑱-4 真人撤回点刀 ⇒ 又变回"未表态" --------------------------------
_g18d, _w18d, _d18d = _wolf18(74, human_idx=0)
_g18d.night_kill(_w18d[0], _d18d[0])       # 真人点 d0（此处会盖过 AI 的票）
_g18d.night_clear(_w18d[0])                # 再点同一个号码 = 撤销
_g18d.night_kill(_w18d[1], _d18d[1])
_g18d.night_kill(_w18d[2], _d18d[1])
_g18d.settle_wolf_window()
check("★ ⑱ 真人撤回后就是「没表态」⇒ 回到 AI 多数决（d1）",
      _g18d.night.get("kill") == _d18d[1]
      and _g18d.night.get("wolf_decided_by") == "ai",
      f"kill={_g18d.night.get('kill')} by={_g18d.night.get('wolf_decided_by')}")

# ---- ⑱-5 两个真人意见一致 ⇒ 就是那个；不一致 ⇒ 真人内部多数 -------------
_g18e, _w18e, _d18e = _wolf18(75, human_idx=None)
_g18e.player(_w18e[0]).kind = "human"
_g18e.player(_w18e[2]).kind = "human"
_g18e.night_kill(_w18e[0], _d18e[0])
_g18e.night_kill(_w18e[1], _d18e[1])
_g18e.night_kill(_w18e[2], _d18e[0])
_g18e.settle_wolf_window()
check("★ ⑱ 两个真人同点 d0 ⇒ 刀 d0（AI 那一票 d1 不算）",
      _g18e.night.get("kill") == _d18e[0], f"kill={_g18e.night.get('kill')}")

_g18f, _w18f, _d18f = _wolf18(76, human_idx=None)
_g18f.player(_w18f[0]).kind = "human"
_g18f.player(_w18f[1]).kind = "human"
_g18f.night_kill(_w18f[0], _d18f[0])
_g18f.night_kill(_w18f[1], _d18f[1])       # 真人 1:1 分歧，AI 那一票不再决定
_g18f.night_kill(_w18f[2], _d18f[0])
_g18f.settle_wolf_window()
check("★ ⑱ 两个真人 1:1 ⇒ 在**真人**的两个对象里随机（AI 那一票不参与）",
      _g18f.night.get("kill") in (_d18f[0], _d18f[1])
      and _g18f.night.get("wolf_decided_by") == "human",
      f"kill={_g18f.night.get('kill')} by={_g18f.night.get('wolf_decided_by')}")

# ---- ⑱-6 到点兜底**不给真人狼**注入刀（没点 = 没表态）-----------------
_inj_notes18 = []


class _FakeWolfG18:
    night = {"wolf_points": {}}

    def player(self, s):
        return _types.SimpleNamespace(kind="human")

    def note(self, t):
        _inj_notes18.append(t)

    players: list = []


_fs18 = _types.SimpleNamespace(
    game=_FakeWolfG18(),
    _act_locked=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不该给真人狼注入刀口 —— 那等于伪造真人意志")))
_ndrv._inject_default(_fs18, 2, ["kill", "confirm"], "wolf")
check("★ ⑱ 到点兜底不给真人狼注入 follow_kill（没点就是没表态，留给 AI 多数决）",
      bool(_inj_notes18) and "没表态" in (_inj_notes18[-1] if _inj_notes18 else ""),
      str(_inj_notes18))

# 反面：AI 狼的兜底照旧（跟随已点过的队友），别把功能一并删了
_inj_calls18 = []


class _FakeAiG18:
    night = {"wolf_points": {0: 3}}
    players = [_types.SimpleNamespace(
        seat=0, role=_types.SimpleNamespace(value="wolf"))]

    def player(self, s):
        return _types.SimpleNamespace(kind="ai")

    def actions_for(self, s):
        return []

    def note(self, t):
        pass


_fs18b = _types.SimpleNamespace(
    game=_FakeAiG18(),
    _act_locked=lambda seat, act, tgt, x: _inj_calls18.append((seat, act, tgt)))
_ndrv._inject_default(_fs18b, 5, ["kill", "confirm"], "wolf")
check("★ ⑱ AI 狼的到点兜底照旧（跟随座位序第一只点过刀的狼）",
      (5, "kill", 3) in _inj_calls18, str(_inj_calls18))

# ---- ⑱-7 源码级：判据必须是「是不是真人」，不能退回"先手/座位序" ------
_eng18 = (BASE / "backend" / "engine.py").read_text(encoding="utf-8")
check("★ ⑱ 结算里先挑真人票（kind != 'ai' + 点过）",
      'human_picks = [pts[s] for s in wolves' in _eng18
      and 'self.player(s).kind != "ai"' in _eng18)
check("★ ⑱ 真人的空刀也进 human_picks（不像 AI 票那样被 `t < 0` 过滤）",
      "if self.player(s).kind != \"ai\" and pts.get(s) is not None" in _eng18)
check("★ ⑱ 定刀依据留痕（wolf_decided_by: human / ai / none）",
      _eng18.count('"wolf_decided_by"') >= 3)
check("★ ⑱ 真人狼整窗没点时，driver 不注入刀口（只写一条内部备注）",
      "整窗没表态，刀口交给 AI 多数决" in
      (BASE / "backend" / "night_driver.py").read_text(encoding="utf-8"))

print("=" * 74)
total = len(PASS) + len(FAIL)
print(f"结果：{len(PASS)}/{total} 通过")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
