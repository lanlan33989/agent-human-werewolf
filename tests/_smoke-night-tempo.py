# -*- coding: utf-8 -*-
"""夜间「窗口节拍」验收（2026-10-07 第三期）—— **真实秒数**，故意不缩放窗口。

用户定的验收标准（逐字）：

    真人听感上，每个角色的「请闭眼」必须在倒计时归零之后才念出来；
    守卫 / 狼刀 / 女巫 / 预言家四段逐一过。

所以这个文件**不调** `night_script.set_win_scale`（别的 `_smoke-*` 都把窗口缩到
0.02 倍以求短跑；**窗口的行为**在这里、用真秒数验收）。代价：跑一次 ~1.5 分钟。

判据：timeline 上每行都带 `at`（那一行**产生**的时刻，墙上时间；第三期加的）。
    · 闭眼句 at − 开眼句 at ≥ 该段的窗口（定稿：闭眼句与窗口结束**同时**）
    · 有人类的那一段：闭眼句 at − arm at ≥ 窗口（arm = 前端「念完 +2 秒」的通知）

用法（在 h5 目录下）：
    ./.venv/Scripts/python.exe tests/_smoke-night-tempo.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) or ".")

os.environ.setdefault("MOCK", "1")
os.environ.setdefault("MOCK_ASR", "1")
os.environ.setdefault("MOCK_LLM", "1")
os.environ.setdefault("MOCK_TTS", "1")

import server                # noqa: E402,F401  （进 mock 层，避免真模型/真 TTS）
import game_api as _ga       # noqa: E402
import night_driver          # noqa: E402
import night_script          # noqa: E402
from engine import Board, Phase, Role  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  OK   " if ok else "  FAIL ") + name
          + (f"  <- {detail}" if detail and not ok else ""))


BOARD12 = Board(
    key="tempo12",
    label="12 人 · 4 狼 1 巫 1 预 1 猎 1 守 4 民",
    roles=[Role.WOLF, Role.WOLF, Role.WOLF, Role.WOLF,
           Role.WITCH, Role.SEER, Role.HUNTER, Role.GUARD,
           Role.VILLAGER, Role.VILLAGER, Role.VILLAGER, Role.VILLAGER],
    lives=1,
    note="窗口节拍验收专用：一副里凑齐 守卫/狼刀/女巫/预言家 四段。",
)

OPEN_GUARD = "守卫请睁眼，请选择今晚你要守护的人"
OPEN_WOLF = "狼人请睁眼，请选择今晚你们要击杀的人"


def at_of(tl, text):
    """某句在 timeline 上的**产生时刻**（没出现返回 None）。

    ⚠️ 两种来源：节点时钟补播的行带 `at`（第三期加的），引擎自己的公共播报
    （天黑/天亮）只带 `ts` —— 都是墙上时间，取到哪个都算。
    """
    for x in reversed(tl):
        if x.get("s") == text:
            return x.get("at") or x.get("ts")
    return None


def first_night(tl):
    """只取**第一夜**那段（到「天亮了」为止）—— mock 的 AI 太快，
    一次 drive 可能已经跨过整个白天进了第二夜，不切掉会把第二夜的守卫句算进来。"""
    out = []
    for x in tl:
        out.append(x)
        if str(x.get("s", "")).startswith("天亮了"):
            break
    return out


def until_dawn(sess, cap_s: float = 150.0) -> None:
    """推到「天亮了」那句出现（= 第一夜走完）。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < cap_s:
        if any(str(x.get("s", "")).startswith("天亮了") for x in sess.timeline):
            return
        if sess.game.winner:
            return
        sess.drive(0.5)


print("=" * 74)
print("① 窗口秒数只有一个来源（夜间剧本表）")
print("=" * 74)

_src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "night_driver.py"), encoding="utf-8").read()
check("★ 驱动器里**没有**第二份窗口表（NIGHT_ACT_WINDOW 已删；两份必然漂移）",
      "NIGHT_ACT_WINDOW =" not in _src)
check("★ 窗口表就是定稿的数字（守卫 15 / 狼刀 20 / 神职 15）",
      (night_script.WIN_GUARD, night_script.WIN_WOLF, night_script.WIN_GOD) == (15, 20, 15),
      f"{night_script.WIN_GUARD}/{night_script.WIN_WOLF}/{night_script.WIN_GOD}")
check("★ 窗口按**节点**查（动作 → 节点 → 窗口，一张表走到底）",
      (night_script.window_for_acts(["kill"]) == night_script.WIN_WOLF
       and night_script.window_for_acts(["guard"]) == night_script.WIN_GUARD
       and night_script.window_for_acts(["heal"]) == night_script.WIN_GOD
       and night_script.window_for_acts(["investigate"]) == night_script.WIN_GOD),
      f"kill={night_script.window_for_acts(['kill'])} "
      f"guard={night_script.window_for_acts(['guard'])}")

print()
print("=" * 74)
print("② 全 AI 12 人局：四段的「请闭眼」都在各自窗口之后（真秒数）")
print("=" * 74)

_s = _ga.Session(humans=0, ais=12, board=BOARD12, seed=7)
_t0 = time.monotonic()
until_dawn(_s)
_elapsed = time.monotonic() - _t0
_tl = first_night(_s.timeline)

_g_open, _g_close = at_of(_tl, OPEN_GUARD), at_of(_tl, "守卫请闭眼")
_w_open, _w_close = at_of(_tl, OPEN_WOLF), at_of(_tl, "狼人请闭眼")
_wh, _wp, _wc = (at_of(_tl, "女巫请睁眼，今晚死的是他，你要救吗？"),
                 at_of(_tl, "你要使用毒药吗？"), at_of(_tl, "女巫请闭眼"))
_wh_line = next((x for x in _tl if x.get("node") == "witch_heal"), {})
_s_open, _s_res = at_of(_tl, "预言家请睁眼，请选择今晚你要查验的人"), at_of(_tl, "他是这个")
_s_close = at_of(_tl, "预言家请闭眼")
_dawn = next((x.get("at") or x.get("ts") for x in _tl
              if str(x.get("s", "")).startswith("天亮了")), None)


def span(a, b):
    return None if (a is None or b is None) else (b - a)


check("★ 守卫段：闭眼句在**窗口（15s）之后**才念",
      (span(_g_open, _g_close) or -1) >= night_script.WIN_GUARD,
      f"open={_g_open} close={_g_close} span={span(_g_open, _g_close)}")
check("★ 狼刀段：闭眼句在**窗口（20s）之后**才念",
      (span(_w_open, _w_close) or -1) >= night_script.WIN_WOLF,
      f"span={span(_w_open, _w_close)}")
check("★ 女巫段：解药问 → 毒药问之间也走满 15s（定稿：两个窗口）；"
      "且解药那句**带**定稿的「今晚死的是他，你要救吗？」（不点名 ⇒ 不泄露是谁）",
      (span(_wh, _wp) or -1) >= night_script.WIN_GOD
      and "今晚死的是他" in str(_wh_line.get("s")),
      f"span={span(_wh, _wp)} line={_wh_line.get('s')!r}")
check("★ 女巫段：闭眼句在毒药窗口（15s）之后才念",
      (span(_wp, _wc) or -1) >= night_script.WIN_GOD,
      f"span={span(_wp, _wc)}")
check("★ 预言家段：验人窗口（15s）走满之后才念「他是这个」",
      (span(_s_open, _s_res) or -1) >= night_script.WIN_GOD,
      f"span={span(_s_open, _s_res)}")
_res_line = next((x for x in _tl if x.get("s") == "他是这个"), {})
check("★ 预言家段：「预言家请闭眼」排在「他是这个」之后、且隔 3 秒"
      "（服务端在结果句上留的 gap=GAP；前端按它等 3 秒再念闭眼句）",
      _s_res is not None and _s_close is not None and _s_res < _s_close
      and _res_line.get("delay") == night_script.GAP,
      f"res={_s_res} close={_s_close} delay={_res_line.get('delay')}")
check("★ 天亮了排在**所有**闭眼句之后",
      _dawn is not None and all(x is not None and _dawn > x
                                for x in (_g_close, _w_close, _wc, _s_close)),
      f"dawn={_dawn} closes={(_g_close, _w_close, _wc, _s_close)}")
check("★ 整夜总时长 ≥ 四段窗口之和（读秒不可跳过、玩家不能加速）",
      _elapsed >= (night_script.WIN_GUARD + night_script.WIN_WOLF
                   + 2 * night_script.WIN_GOD + night_script.WIN_GOD),
      f"实测 {_elapsed:.1f}s")

_seq = []
for _x in _tl:
    _n = _x.get("node")
    if _n and (not _seq or _seq[-1] != _n):
        _seq.append(_n)                      # 相邻去重：一句开眼 + 一句闭眼属同一个节点
check("★ 节点顺序 == 剧本表（守卫→狼→女巫×2→预言家→结果→闭眼，逐节点增量补播）",
      _seq == ["guard", "wolf", "witch_heal", "witch_poison", "seer", "seer_result",
               "seer_close"],
      str(_seq))

print()
print("=" * 74)
print("③ 有真人那一段：闭眼句在**倒计时归零之后**（arm 之后满窗口）")
print("=" * 74)


def find_human_session():
    """造一局"人类是守卫或狼"（前两段，测完不用等太久）。"""
    for seed in range(1, 40):
        sess = _ga.Session(humans=1, ais=11, board=BOARD12, seed=seed)
        hp = next((p for p in sess.game.players if p.kind == "human"), None)
        if hp is not None and hp.role in (Role.GUARD, Role.WOLF):
            return sess, hp
    return None, None


_s2, _hp = find_human_session()
if _s2 is None:
    check("★ 人类段：闭眼句在 arm + 窗口之后", False, "造不出「人类是守卫/狼」的局")
else:
    _win_text = OPEN_WOLF if _hp.role is Role.WOLF else OPEN_GUARD
    _cur = _hcur = None
    _t_arm = None
    _t1 = time.monotonic()
    while time.monotonic() - _t1 < 60:
        _cur = night_driver._nn_cur(_s2)
        _acts = _s2.game.actions_for(_hp.seat)
        if _cur and _acts and night_script.node_for_acts(_acts) == _cur:
            # 前端「该角色那句念完 + 2 秒」时会 POST /arm —— 它干的就是这一行。
            _t_arm = time.monotonic()
            _s2.human_wait_since = _t_arm
            _hcur = _cur
            break
        _s2.drive(0.3)
    _close_at = _close_txt = None
    _t2 = time.monotonic()
    _node = night_script._BY_ID.get(_hcur or "", {})
    _close_txt = _node.get("close")
    while _t_arm and time.monotonic() - _t2 < 60:
        _close_at = at_of(_s2.timeline, _close_txt)
        if _close_at is not None:
            break
        _s2.drive(0.3)
    _span = span(_t_arm, _close_at)
    check("★ 人类段（%s）：闭眼句在 **arm 之后满窗口** 才念（倒计时归零之后）"
          % ("狼刀" if _hp.role is Role.WOLF else "守卫"),
          _t_arm is not None and _span is not None
          and _span >= night_script.window_of(_hcur),
          f"cur={_hcur} arm={_t_arm} close={_close_at} span={_span} "
          f"win={night_script.window_of(_hcur or '')}")
    check("★ 人类段：没交出动作 ⇒ 到点按定稿注入默认（狼跟随/空刀；神职放弃）",
          _s2.game.night.get("kill") is not None
          or _s2.game.night.get("guard_asked") or _s2.game.night.get("investigate") is not None
          or _s2.game.night.get("witch_asked"),
          str({k: v for k, v in _s2.game.night.items() if k in
               ("kill", "guard_asked", "investigate", "witch_asked", "kill_settled")}))

print()
print("=" * 74)
print(f"结果：{len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
