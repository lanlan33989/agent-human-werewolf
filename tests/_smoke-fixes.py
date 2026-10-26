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
    pend = s.auto_advance()
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
        need = [(p.seat, sess.game.actions_for(p.seat))
                for p in sess.game.players
                if p.kind == "human" and p.alive and sess.game.actions_for(p.seat)]
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
p2 = ((BASE / "play.html").read_text(encoding="utf-8")
      + (BASE / "tableui.js").read_text(encoding="utf-8")
      # 2026-10-07：播报队列 + 夜间门控拆去了 speech_ui.js ⇒ 清单跟着走（判据未改）
      + (BASE / "speech_ui.js").read_text(encoding="utf-8"))
# 🔴 2026-10-23：探针页 index.html 已删（用户：非游戏画面），相关判据随之退役
server_py = (BASE / "server.py").read_text(encoding="utf-8")

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

run_https_src = (BASE / "run_https.py").read_text(encoding="utf-8")
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
    g7.night["kill"] = victim7        # 等价于「人类狼已经刀完了」
    s7.rng.random = lambda: 0.0      # 逼 AI 女巫一定救人
    s7.auto_advance()
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
    g_w1.night["kill"] = victim_w1
    # 掷骰子这一路会说"救"—— 断言没走它，就等于证明决策真的来自模型
    s_w1.rng.random = lambda: 0.0
    s_w1.agents[w1.seat] = ScriptedAgent(w1.seat, ["不救"])
    s_w1.auto_advance()
    check("★ AI 女巫说「不救」就真的不救（走模型，不再掷骰子）",
          g_w1.night.get("heal") is None
          and g_w1.night.get("witch_asked") is True
          and g_w1.private[w1.seat].potions["heal"] is True,
          f"heal={g_w1.night.get('heal')} asked={g_w1.night.get('witch_asked')} "
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
    g_w2.night["kill"] = victim_w2
    g_w2.private[w2.seat].potions["heal"] = False      # 解药已用 ⇒ 只剩毒药可考虑
    s_w2.agents[w2.seat] = ScriptedAgent(w2.seat, [str(poison_w2 + 1)])
    s_w2.auto_advance()
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


def _race_round(seed, seconds=1.8):
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
        need = [(p.seat, g.actions_for(p.seat)) for p in g.players
                if p.kind == "human" and p.alive and g.actions_for(p.seat)]
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
    """每一轮里，**所有存活者都必须恰好发过一次言**（跳过最后一个发言人会被抓到）。"""
    alive, speakers, out = None, [], []
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
            if "开始投票" in s and alive is not None:
                _miss = sorted(a + 1 for a in (alive - set(speakers)))
                _dup = len(speakers) != len(set(speakers))
                if _miss or _dup:
                    out.append(f"缺发言={_miss} 有重复={_dup}（本轮共 {len(speakers)} 条）")
                speakers = []
        else:
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
      "g.can_vote(p.seat)" in (BASE / "night_driver.py").read_text(encoding="utf-8"))
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
for _i in range(500):
    g = s12.game
    if g.phase.value == "ended":
        reached_end = True
        break
    need = [(p.seat, g.actions_for(p.seat)) for p in g.players
            if p.kind == "human" and g.actions_for(p.seat)]
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
total = len(PASS) + len(FAIL)
print(f"结果：{len(PASS)}/{total} 通过")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
