# -*- coding: utf-8 -*-
"""
测试专用板子（旧预设迁移的落点）。

为什么还留一副：
`make_board(n)` 是**按人数现算**的，所以"4 人局"永远是 `1 狼 + 女巫 + 2 民`
（神职启用顺序里女巫排第一）。而旧预设 `mini4` 是 `1 狼 + **预言家** + 2 民` ——
**新规则下拿不到这个组合**。

所以它不是"还没迁"，而是"现算不出来"：测预言家相关行为（验人、报身份、信息利用）
需要一个 4 人局带预言家的板子。把这副板子留在测试里，生产路径（`engine.py`）
就彻底没有"预设"这个概念了 —— 这正是 05c 那条迁移要的结果。

⚠️ 只给测试用。生产代码不要 import 本模块。
"""
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
_s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/  # 仓库根目录入 path：保证 python tests/xxx.py 可直接跑
from engine import Board, Role

# 等价于旧的 mini4：1 狼 + 1 预言家 + 2 民。
SEER4 = Board(
    key="seer4",
    label="4 人 · 1 狼 1 预言家 2 民",
    roles=[Role.WOLF, Role.SEER, Role.VILLAGER, Role.VILLAGER],
    lives=1,
    note="测试专用：新规则下 4 人局给女巫，拿不到预言家，所以单独留一副。",
)
