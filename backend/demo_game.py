# -*- coding: utf-8 -*-
"""
把引擎和 AI 玩家层跑给人看。

用法（在 h5 目录下）：
    ./.venv/Scripts/python.exe demo_game.py

三幕：
  一、同一时刻，三个人看到的东西不一样（四层隔离）
  二、跑完一整局（公共日志）
  三、prompt 层隔离（狼与村民的 system prompt 对比）

注意：这里的 AI 用确定性假策略，只为验证编排链路通不通，不代表任何真实棋力。
"""
import random

from agents import build_messages, private_block
from engine import Game, Phase, Role, new_game
from engine import make_board  # noqa: E402

LINE = "=" * 68


def title(s: str) -> None:
    print("\n" + LINE)
    print(s)
    print(LINE)


# ================================================================ 第一幕

g = Game(make_board(3, 3), kinds=["human", "human", "human", "ai", "ai", "ai"], seed=7)
g.start()

wolves = [p.seat for p in g.players if p.role.is_wolf]
seer = next(p.seat for p in g.players if p.role is Role.SEER)
villager = next(p.seat for p in g.players if p.role is Role.VILLAGER)
g.night_investigate(seer, wolves[0])

title("第一幕 · 同一个瞬间，三个人眼里的世界")

for tag, seat in [("狼", wolves[0]), ("预言家", seer), ("村民", villager)]:
    v = g.get_view_for(seat)
    print(f"\n[{tag} · {seat + 1} 号玩家]  身份={v['role_label']}  命={v['lives']}")
    print("  能看到：", "、".join(sorted(v.keys())))
    print("  私有信息块：", private_block(v).replace("\n", " ⏎ ") or "（空）")

print("\n注意 L3 元信息：视图里没有任何字段能让他知道「谁是 AI、谁是真人」以外的引擎内部状态。")


# ================================================================ 第二幕

title("第二幕 · 跑完一整局")


def bot_act(g: Game, seat: int, rng: random.Random) -> None:
    view = g.get_view_for(seat)
    acts = view.get("can_act", [])
    others = [p.seat for p in g.alive if p.seat != seat]
    good = [p.seat for p in g.good_alive()]
    if "kill" in acts and good:
        g.wolf_agree_kill(rng.choice(good))
    if "investigate" in acts and others:
        r = g.night_investigate(seat, rng.choice(others))
    if "vote" in acts and others:
        g.cast_vote(seat, rng.choice(others))


def run(humans: int, ais: int, seed: int, max_steps: int = 80):
    g = new_game(humans, ais, seed=seed)
    g.start()
    steps = 0
    while g.phase is not Phase.ENDED and steps < max_steps:
        steps += 1
        if g.phase is Phase.NIGHT:
            for p in list(g.alive):
                bot_act(g, p.seat, g.rng)
            g.resolve_night()
        elif g.phase is Phase.DAWN:
            g.open_discuss()
        elif g.phase is Phase.DISCUSS:
            if g.end_speech() is None:
                g.open_vote()
        elif g.phase is Phase.VOTE:
            for p in list(g.alive):
                bot_act(g, p.seat, g.rng)
            for p in list(g.alive):
                if p.seat not in g.votes:
                    g.cast_vote(p.seat, -1)
            g.resolve_vote()
            if g.phase is Phase.SHOOT:      # 猎人被投出：bot 放弃开枪
                g.night_shoot(g.shooter, -1)
            if g.phase is not Phase.ENDED:
                g.next_night()
    return g


game = run(2, 2, seed=42)      # 旧 mini4 = 4 人局 ⇒ 2 真人 + 2 AI
print(f"\n板子：{game.board.label}　每人 {game.board.lives} 条命")
print(f"座位：", "、".join(f"{p.seat + 1}号({p.kind})" for p in game.players))
print("\n--- 公共日志（所有人都能看到的全部内容）---")
for i, line in enumerate(game.public_log, 1):
    print(f"  {i:>2}. {line}")
print(f"\n结果：{'狼人胜利' if game.winner == 'wolf' else '好人胜利'}")
print(f"共 {game.round} 轮，{len(game.public_log)} 条公开记录。")


# ================================================================ 第三幕

title("第三幕 · prompt 层的隔离")


def show_prompt(tag: str, seat: int) -> None:
    v = g.get_view_for(seat)
    msgs = build_messages(v, ["2 号：我先过。", "5 号：我觉得 3 号有问题。"], "发言")
    print(f"\n[{tag} · {seat + 1} 号] 的 system prompt")
    print("-" * 60)
    print(msgs[0]["content"].rstrip())
    print("-" * 60)


villager2 = next(p.seat for p in g.players
                 if p.role is Role.VILLAGER and p.seat != villager)
show_prompt("狼", wolves[0])
show_prompt("村民", villager2)

print("\n要看的差别：狼的 prompt 里有队友信息与伪装要求；村民的 prompt 里")
print("完全没有私有块 —— 这不是靠模型自觉，而是进 prompt 之前就不存在。")
print("\n" + LINE)
