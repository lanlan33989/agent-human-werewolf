# -*- coding: utf-8 -*-
"""
批七 smoke test：摘要通道（G2+H4）· best-of-N（H5）· 离线批量评测（G1 的评测吞吐）。

三块各自的性质不同，所以断言也分三类：
  · 摘要器：**成本**（缓存必须命中）+ **降级**（模型挂了不能崩）
  · best-of-N：**该选哪个候选**（规则打分）+ **只在一处用**（kind 白名单）
  · 批量评测：**全 AI 局能跑通** + 失败被记录而不是抛异常
"""
import sys

import game_api
import metrics
from agents import Agent, LlmAgent
from eval_batch import markdown_report, run_batch
from summarize import make_summarizer

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


# ================================================================ 摘要器（G2 + H4）

_calls = {"n": 0, "seen": []}


def _fake_chat(msgs, timeout=None):
    _calls["n"] += 1
    _calls["seen"].append(msgs)
    return "（摘要：2 号怀疑 3 号，4 号保了 3 号。）"


_sm = make_summarizer(_fake_chat)
_older = [f"{i + 1} 号：第 {i} 句" for i in range(19)]

_r1 = _sm(_older)
_r2 = _sm(_older)
check("★ 摘要器能出结果", "摘要" in _r1, _r1[:40])
check("★ 同一批内容第二次调用**命中缓存**（没有缓存 = 每次 agent 调用都重复花钱）",
      _r1 == _r2 and _calls["n"] == 1, f"模型调用 {_calls['n']} 次")
_sm(_older + ["20 号：我又想了想"])
check("★ 发言增加了 ⇒ 缓存失效、重新摘要（否则 AI 会拿着过期摘要做判断）",
      _calls["n"] == 2, f"模型调用 {_calls['n']} 次")
check("★ 摘要器的入参只有公开发言那几条（结构上就传不进私有信息）",
      len(_calls["seen"][0][1]["content"].splitlines()) == 19,
      str(_calls["seen"][0][1]["content"][:40]))
check("摘要提示词里写死了'只写出现过的事实、不要下判断'",
      "只写这段话里出现过的事实" in _calls["seen"][0][0]["content"], "")

_fail = make_summarizer(lambda msgs, t=None: (_ for _ in ()).throw(RuntimeError("挂了")))
check("★ 模型挂掉时返回空串（调用方会退回硬截断，绝不能让摘要搞崩整局）",
      _fail(_older) == "" and _fail.failures == 1, f"failures={_fail.failures}")

# 和 build_messages 串起来：>15 条时摘要进 prompt、最新 5 条留原文
import agents as _ag7  # noqa: E402

_v7 = {"seat": 0, "role_label": "村民", "public_log": [], "players": [
    {"seat": 0, "alive": True}, {"seat": 1, "alive": True}]}
_txt = _ag7._history_text(_older, _sm)
check("★ 摘要真的进了发言那段文本（不是只做了个能算的函数）",
      "摘要" in _txt and "第 18 句" in _txt and "第 10 句" not in _txt,
      _txt.replace("\n", " ")[:110])

# ================================================================ best-of-N（H5）

# 用**真实视图**，不手搓：手搓的视图会随 prompt 需要的字段变化而失效
# （第一次跑就踩了：hard_facts 要 view["phase"]，手搓的没有 ⇒ KeyError）
_ag7_game = game_api.Session(humans=2, ais=2, seed=5, level="硬核").game
_view7 = _ag7_game.get_view_for(
    next(p.seat for p in _ag7_game.players if p.kind == "ai"))


class _ScriptAgent(LlmAgent):
    """按脚本逐次返回的 Agent（用来验"选哪一份"）。"""

    def __init__(self, seat, script):
        super().__init__(seat, chat_fn=None)
        self.script = list(script)
        self.n = 0

    def _ask(self, msgs):
        out = self.script[self.n % len(self.script)]
        self.n += 1
        return out


# ⚠️ '{"target": 3}'（0 基 2 号）：必须**不是第一个 AI 自己** ——
#    座次默认改成「人类首位 + 均匀隔 AI」后（2026-10-01），2 人 2 AI 局的
#    第一个 AI 从 0 基 2 挪到了 0 基 1，原来脚本里的 {"target": 2} 恰好
#    变成了"投自己" ⇒ 解析不出 ⇒ 两份候选全灭（target=None，实测红过）。
#    seat 布局再改时，这里的数字要跟着对一遍。
_a7 = _ScriptAgent(0, ["我先想想，好像谁都不太对", '{"target": 3}'])
_a7.best_of = 2
_d7 = _a7.decide_target(_view7, [], "vote")
check("★ best-of-N：两次候选都发了", _a7.n == 2, f"调用 {_a7.n} 次")
check("★ best-of-N 选中**能解析出合法目标**的那份（解析不出的那半被规则打分淘汰）",
      _d7.target == 2, f"target={_d7.target}")   # {"target": 3} → 0 基 2
check("★ 选择过程留痕（复盘时能看到'选的是第几份、分多少'）",
      any("best-of" in x for x in (_d7.rejected or [])), str(_d7.rejected))

_a8 = _ScriptAgent(0, ["3 号吧"])
_a8.best_of = 5
_a8.best_of_kinds = {"vote"}
_a8.decide_target(_view7, [], "speak")      # 不在白名单里 ⇒ 不该走 best-of
check("★ 「只在一处用」被写进代码：白名单外的动作不会变成 N 倍成本",
      _a8.n == 1, f"调用 {_a8.n} 次")
_a8.decide_target(_view7, [], "vote")
check("……而白名单里的动作才走 N 份", _a8.n == 1 + 5, f"调用 {_a8.n} 次")

_a9 = _ScriptAgent(0, ["完全看不懂", "还是看不懂"])
_a9.best_of = 2
_d9 = _a9.decide_target(_view7, [], "vote")
check("★ N 份都解析不出来时**返回'没选出来'**，不偷偷随机挑一个（那会掩盖模型连续失败）",
      _d9.target is None, f"target={_d9.target}")
check("默认 best_of=1（不配置就不会多花钱）",
      Agent(0, chat_fn=None).best_of == 1, "")

# ================================================================ 离线批量评测（G1 评测吞吐）

class LocalClient:
    """
    把进程内的 Session 包装成 eval_batch 认识的客户端。

    为什么评测要能换客户端：`run_batch` 的逻辑（排队、记账、聚合、失败处理）
    才是要测的东西；挂着真 HTTP 跑步进 CI 是不现实的。
    生产用的是 `HttpClient`（跑真实那条路），测试用这个。
    """

    def __init__(self):
        self.n = 0

    def new(self, **body):
        s = game_api.Session(seed=body.get("seed"), humans=body.get("humans"),
                             ais=body.get("ais"), level=body.get("level"))
        self.n += 1
        game_api.SESSIONS[s.id] = s          # 注册进去，路由函数就能直接用
        return {"gid": s.id,
                "human_seats": [p.seat for p in s.game.players if p.kind == "human"]}

    def public(self, gid):
        return game_api.public(gid)

    def pending(self, gid):
        return game_api.pending_seats(gid)

    def seat(self, gid, seat):
        return game_api.SESSIONS[gid].seat_state(seat)

    def act(self, gid, body):
        return game_api.act(gid, game_api.ActionReq(**body))

    def scoreboard(self, gid):
        return game_api.scoreboard(gid)


_res7 = run_batch(LocalClient(), games=3, humans=0, ais=4, level="认真",
                  parallel=2, seed_base=777)
check("★ 全 AI 局能跑通（评测要的形态；界面不会出现）",
      len(_res7["boards"]) == 3, f"可用 {len(_res7['boards'])} 局，失败 {len(_res7['failures'])}")
check("★ 每局都带 gid 且都判出了胜负",
      all(b.get("gid") and b.get("winner") in ("wolf", "good") for b in _res7["boards"]),
      str([(b.get("gid"), b.get("winner")) for b in _res7["boards"]]))
check("★ 聚合按档分组，局数对得上",
      (_res7["aggregate"]["by_level"].get("认真") or {}).get("games") == 3,
      str(_res7["aggregate"]["by_level"]))
check("★ 报告含关键小节",
      all(k in (md7 := markdown_report(_res7)) for k in
          ("# 离线批量评测报告", "## 按档聚合", "## 逐局", "怎么读这份报告")), "")
check("★ 报告写明'胜率必须按档看'（不分档的胜率没有意义）",
      "按档看" in md7, "")


class _BoomClient(LocalClient):
    def new(self, **body):
        raise RuntimeError("服务没起")


_res_bad = run_batch(_BoomClient(), games=2, humans=0, ais=4, parallel=2)
check("★ 客户端全挂时**记录失败而不是抛异常**（中途崩掉会让跑完的局白费）",
      _res_bad["boards"] == [] and len(_res_bad["failures"]) == 2,
      str(_res_bad["failures"][:1]))
check("……而且报告里明确列出失败局（它们会系统性偏掉胜率）",
      "失败/没打完的局" in markdown_report(_res_bad), "")


def _human_names(ok, total=4):
    return f"{len(results)} 条断言（{ok}）" + str(total)


print("\n=== 批七 smoke test（摘要 · best-of · 批测）===\n")
pass_n = fail_n = 0
for name, ok, detail in results:
    print(("PASS" if ok else "FAIL") + "  " + name.ljust(52)
          + ("  " + detail if detail else ""))
    if ok:
        pass_n += 1
    else:
        fail_n += 1
print("\n" + ("ALL GREEN (" + str(pass_n) + " passed)" if fail_n == 0
              else str(fail_n) + " FAILED / " + str(pass_n) + " passed"))
sys.exit(1 if fail_n else 0)
