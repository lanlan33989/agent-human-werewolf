# -*- coding: utf-8 -*-
"""
离线批量评测（G1 的"评测吞吐"那一半）。

⚠️ 先说清它**不是**什么：它不是局内并发。局内能并发的只有"狼刀 + 预言家验人"
   那一次调用，收益可以忽略；白天发言必须串行（后发言者要引用前者）。
   真正需要并发的是**离线跑几十上百局**：出六维指标、出榜单、验证平衡改动 ——
   串行跑 50 局要几小时，并发之后是几十分钟。

为什么是 HTTP 驱动、而不是在进程内直接 new Session：
  · 进程内要先装配 LLM（`import server` 会顺带预热 whisper），而且会和
    正在跑的那个服务抢同一个 provider 的限流；
  · HTTP 跑的才是**真实那条路**（队列、超时、重试都在里面），测出来的数才算数。

⚠️ 诚实边界：并发靠线程 + 服务端自己的并发；**provider 的 RPM 限制是硬墙**。
   把 --parallel 开到 8 只会换来一堆 429 和更慢的墙钟。默认 2。

用法：
    ./.venv/Scripts/python.exe eval_batch.py --games 10 --ais 6 --level 认真
    ./.venv/Scripts/python.exe eval_batch.py --games 6 --ais 9 --level 硬核 --parallel 3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import metrics


# ================================================================ 客户端

class HttpClient:
    """对着一个**正在跑的服务**说话（默认本机 8000）。"""

    def __init__(self, base: str = "http://127.0.0.1:8000", timeout: float = 60.0) -> None:
        self.base = base.rstrip("/") + "/api/game"
        self.timeout = timeout

    def _req(self, path: str, body: Optional[dict] = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"},
            method="POST" if data is not None else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as f:
            return json.loads(f.read().decode())

    def new(self, **body: Any) -> dict:
        return self._req("/new", body)

    def public(self, gid: str) -> dict:
        return self._req(f"/{gid}/public")

    def pending(self, gid: str) -> dict:
        return self._req(f"/{gid}/pending_seats")

    def seat(self, gid: str, seat: int) -> dict:
        return self._req(f"/{gid}/seat/{seat}")

    def act(self, gid: str, body: dict) -> dict:
        return self._req(f"/{gid}/act", body)

    def scoreboard(self, gid: str) -> dict:
        return self._req(f"/{gid}/scoreboard")


# ================================================================ 跑一局

def next_action(acts: List[str], view: Dict[str, Any], seat: int,
                others: List[int]) -> Optional[dict]:
    """
    给人类座位挑一个动作（评测用的"机器人真人"）。

    ⚠️ 狼队协商是**两步**（点人 → 确定），而且"点过没有"必须从视图里读
       `wolf_points` —— 只看 can_act 的话，未定案前 kill 一直在里面，会一直点下去。
    返回 None 表示这个座位此刻不该动（交给调用方去推服务端）。
    """
    pts = view.get("wolf_points") or {}
    pointed = pts.get(seat) is not None or str(seat) in pts

    def has(k: str) -> bool:
        return k in acts

    if has("speak"):
        return {"seat": seat, "action": "speak", "text": "我先过。"}
    if has("confirm") and pointed:
        return {"seat": seat, "action": "confirm"}
    for k in ("vote", "kill", "investigate", "guard"):
        if has(k) and others:
            pool = [s for s in others if s != view.get("guard_last")]
            return {"seat": seat, "action": k, "target": (pool or others)[0]}
    if has("heal") and view.get("night_victim") is not None:
        return {"seat": seat, "action": "heal", "target": view["night_victim"]}
    if has("shoot") and others:
        return {"seat": seat, "action": "shoot", "target": others[0]}
    return {"seat": seat, "action": "pass"}


def play_one(client, gid: str, human_seats: List[int], *,
             max_steps: int = 800, sleep: float = 0.5) -> Dict[str, Any]:
    """把一局推到结束（人类座位由机器人代打；全 AI 局则只是轮询）。"""
    pub: Dict[str, Any] = {}
    for _ in range(max_steps):
        pub = client.public(gid)
        if pub.get("phase") == "ended":
            return pub
        pend = client.pending(gid)
        seats = pend.get("seats") or []
        if not seats:
            # 没人在等：服务端还在替 AI 干活（模型调用要时间）⇒ 等一下再问
            time.sleep(sleep)
            continue
        s = seats[0]
        seat = int(s["seat"])
        if human_seats and seat not in human_seats:
            # 服务端说这个座位有活，但它不是"人类"（理论上不该发生）——
            # 记下来比死循环强
            return {"phase": pub.get("phase"), "anomaly": f"座位 {seat} 不在人类名单里"}
        view = client.seat(gid, seat)
        others = [p["seat"] for p in view.get("players") or []
                  if p.get("alive") and p.get("seat") != seat]
        body = next_action(list(s.get("actions") or []), view, seat, others)
        if body is None:
            time.sleep(sleep)
            continue
        try:
            client.act(gid, body)
        except urllib.error.HTTPError as e:
            # 400 通常是"这一步已经不用做了"（比如别人先做了）——不是致命，
            # 但要看得到：连续很多次说明有真问题
            if e.code >= 500:
                raise
            time.sleep(sleep)
    return pub


# ================================================================ 批量

def run_batch(client, *, games: int = 4, humans: int = 0, ais: int = 6,
              level: str = "认真", parallel: int = 2, seed_base: int = 1000,
              on_done=None) -> Dict[str, Any]:
    """
    跑 N 局，返回 {boards, failures, plan}。

    · `boards` 每局一份 scoreboard（含 gid）
    · `failures` 没打完/报错的局（**不抛异常**：评测要的是"最后有多少局可用"，
      中途崩掉会让前面跑完的那些白费）
    · `parallel` 是**同时跑几局**（不是局内并发）
    """
    plan = {"games": games, "humans": humans, "ais": ais, "level": level,
            "parallel": parallel, "seed_base": seed_base}

    def one(i: int) -> Dict[str, Any]:
        try:
            d = client.new(humans=humans, ais=ais, level=level, seed=seed_base + i)
            gid = d["gid"]
            hseats = list(d.get("human_seats") or [])
            final = play_one(client, gid, hseats)
            if final.get("phase") != "ended":
                return {"gid": gid, "error": f"没打完（phase={final.get('phase')}）"}
            sb = client.scoreboard(gid)
            sb["gid"] = gid
            if on_done:
                on_done(sb)
            return sb
        except Exception as e:                  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    boards: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(parallel))) as ex:
        for r in ex.map(one, range(int(games))):
            (failures if r.get("error") else boards).append(r)
    return {"boards": boards, "failures": failures, "plan": plan,
            "aggregate": metrics.aggregate(boards)}


def _pct(v: Any) -> str:
    return "—" if v is None else f"{round(float(v) * 100)}%"


def markdown_report(res: Dict[str, Any]) -> str:
    """评测报告（Markdown）：给人看的那一份。"""
    plan, agg = res["plan"], res.get("aggregate") or {}
    boards, fails = res["boards"], res["failures"]
    L: List[str] = []
    L.append("# 离线批量评测报告")
    L.append("")
    L.append(f"计划：{plan['games']} 局 · 真人 {plan['humans']} + AI {plan['ais']} · "
             f"档位 {plan['level']} · 并发 {plan['parallel']}")
    L.append("")
    L.append(f"**可用 {len(boards)} 局 / 失败 {len(fails)} 局**")
    L.append("")
    L.append("## 按档聚合")
    L.append("")
    L.append("| 档位 | 局数 | 狼胜率 | 平均轮数 | 真人座位胜率 | AI 座位胜率 | 真人被投出 | AI 被投出 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for lv, g in sorted((agg.get("by_level") or {}).items()):
        L.append(f"| {lv} | {g['games']} | {_pct(g.get('wolf_win_rate'))} | "
                 f"{g.get('avg_rounds')} | {_pct(g.get('human_seat_win_rate'))} | "
                 f"{_pct(g.get('ai_seat_win_rate'))} | {g.get('human_exiled')} | "
                 f"{g.get('ai_exiled')} |")
    L.append("")
    L.append("## 逐局")
    L.append("")
    L.append("| 局号 | 赢家 | 轮数 | 好人投中狼 | 狼投好人 | 平安夜 | 技能（验/救/毒/挡/枪） |")
    L.append("|---|---|---|---|---|---|---|")
    for b in boards:
        v = b.get("votes") or {}
        sk = b.get("skills") or {}
        t = b.get("tempo") or {}
        L.append(f"| `{b.get('gid')}` | {b.get('winner')} | {(b.get('outcome') or {}).get('rounds')} | "
                 f"{_pct(v.get('good_accuracy'))} | {_pct(v.get('wolf_misvote_rate'))} | "
                 f"{t.get('peaceful_nights')} | "
                 f"{sk.get('seer_checks')}/{sk.get('witch_heals')}/{sk.get('witch_poisons')}/"
                 f"{sk.get('guard_blocks')}/{sk.get('hunter_shots')} |")
    L.append("")
    if fails:
        L.append("## 失败/没打完的局（别忽略：它们会系统性偏掉胜率）")
        L.append("")
        for f in fails:
            L.append(f"- `{f.get('gid', '?')}`：{f.get('error')}")
        L.append("")
    L.append("---")
    L.append("")
    L.append("**怎么读这份报告**：")
    L.append("- 胜率必须**按档看**：不同档的 AI 不是一回事（赌注、说话要求、best-of 都不同）。")
    L.append("- 「真人座位胜率 vs AI 座位胜率」是逐座位算的（混玩时人和 AI 常在同一阵营，")
    L.append("  写成「人 vs AI 谁赢」会算成双赢）。")
    L.append("- 失败的那几局会**系统性偏掉**胜率（往往是某一方优势时卡住），所以必须一起看。")
    L.append("- 并发受 provider RPM 限制：429 变多就把 --parallel 调小。")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    # Windows 控制台默认是 GBK，报告里有「·」「——」这些字符，
    # 撞上编不了的字符就是 UnicodeEncodeError（**实测把服务搞挂过一次**）。
    # 这里自保一下：编不出来就替换，而不是崩。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
    except Exception:                           # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="离线批量评测")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--games", type=int, default=4)
    ap.add_argument("--humans", type=int, default=0)
    ap.add_argument("--ais", type=int, default=6)
    ap.add_argument("--level", default="认真")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--seed-base", type=int, default=1000)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)

    client = HttpClient(a.base)
    t0 = time.time()
    print(f"开始：{a.games} 局（真人 {a.humans} + AI {a.ais}，档位 {a.level}，"
          f"并发 {a.parallel}）", flush=True)

    def _done(sb):
        print(f"  完成 {sb.get('gid')}：{sb.get('winner')} "
              f"{(sb.get('outcome') or {}).get('rounds')} 轮", flush=True)

    res = run_batch(client, games=a.games, humans=a.humans, ais=a.ais,
                    level=a.level, parallel=a.parallel, seed_base=a.seed_base,
                    on_done=_done)
    md = markdown_report(res)
    print("\n" + md)
    print(f"\n墙钟：{time.time() - t0:.0f} 秒")
    out = a.out or f"eval-{time.strftime('%Y%m%d-%H%M%S')}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"报告已写入：{out}")
    return 0 if res["boards"] else 1


if __name__ == "__main__":
    sys.exit(main())
