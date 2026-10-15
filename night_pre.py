# -*- coding: utf-8 -*-
"""夜间/投票 AI 决策**预跑池**（2026-10-07 用户定稿第二期）。

为什么要它：AI 的一次决策 = 一次 LLM 调用（实测 2~3 秒，慢的十几秒）。
以前驱动器是**同步**跑的 —— 狼刀、验人、用药、守人逐个等一次，
一夜就是"串行好几秒"，法官播报也被拖着。

用户定稿（根目录《夜间推进-用户定稿.md》）：**天黑即后台并发预跑**
（守卫/狼/预言家互不依赖，按安全顺序同时起跑），到**收割点**取结果：
  · 狼刀节点开始收 [守卫, 狼]；预言家节点结束收 [女巫, 预言家]
  · 到点还没算完 ⇒ **结果直接丢弃、默认放弃**（用户定：LLM 不缓存）

这一层只做两件事，且**只做计算、绝不碰引擎状态**：
  1. `PrePool.submit()` —— 在后台线程里跑一个纯计算（调用方给的闭包）；
  2. `PrePool.take()`   —— 到收割点时**非阻塞**地取：
        · 有结果   ⇒ 返回它（调用方负责写引擎）
        · 还在跑   ⇒ 返回 WAIT（驱动器据此报 "working"，继续轮询，不干等）
        · 过点没算完 ⇒ 返回 GIVEUP（调用方按"默认放弃"处理，结果丢弃）
        · 压根没预跑 ⇒ 返回 MISS（本地规则 Agent：同步算就等于预跑）

⚠️ 隔离：提交时调用方已经把**快照**取好（`get_view_for` 的返回值、`speech` 的拷贝）；
   预跑线程不读引擎可变状态、不写引擎 —— 这就是用户点名的
   「把 agents 的决策计算与引擎写入解耦」。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# take() 的三种"不是结果"的返回（用**同一性**判断，别用 ==）
WAIT = "__night_pre_wait__"        # 后台还在算 ⇒ 调用方返回 "wait"
GIVEUP = "__night_pre_giveup__"    # 过了收割点 ⇒ 丢弃，按"默认放弃"处理
MISS = "__night_pre_miss__"        # 没预跑过 ⇒ 调用方照旧同步算


class PrePool:
    """一"相位"一个池（每夜一个、每轮投票一个）。线程全部 daemon。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done: Dict[Tuple[int, str], Any] = {}      # key -> 结果（或异常对象）
        self._pending: Dict[Tuple[int, str], float] = {} # key -> 截止时刻
        self._gaveup: set = set()
        self._threads: list = []

    # ---------------------------------------------------------------- 提交
    def submit(self, seat: int, kind: str, fn: Callable[[], Any],
               window: float) -> bool:
        """后台跑 fn()；`window` 秒后还没跑完 ⇒ 该决策作废（结果丢弃）。

        同一个 (座位, 动作) 只会提交一次（重复提交返回 False）。
        """
        key = (int(seat), str(kind))
        with self._lock:
            if key in self._done or key in self._pending or key in self._gaveup:
                return False
            self._pending[key] = time.monotonic() + float(window)
        t = threading.Thread(target=self._run, args=(key, fn), daemon=True,
                             name="nightpre-%d-%s" % (key[0] + 1, key[1]))
        self._threads.append(t)
        t.start()
        return True

    def _run(self, key: Tuple[int, str], fn: Callable[[], Any]) -> None:
        try:
            val: Any = fn()
        except BaseException as e:                       # noqa: BLE001
            val = e                                     # 失败也"有结果"（调用方退化）
        with self._lock:
            self._pending.pop(key, None)
            if key in self._gaveup:
                return                                   # 过点了 ⇒ 结果直接丢弃
            self._done[key] = val

    # ---------------------------------------------------------------- 收割
    def take(self, seat: int, kind: str, timeout: Optional[float] = None) -> Any:
        """取一个决策（不干等）。

        · `timeout=None` ⇒ 等到它有结果、或到它**自己的截止时刻**为止；
        · `timeout=秒数` ⇒ 最多再等这么久（且不超过它自己的截止时刻）——
          这是给驱动器的：**本次推进还剩多少预算**，用完就返回 WAIT，
          让上层报 "working"、下一次轮询接着等。
        """
        key = (int(seat), str(kind))
        with self._lock:
            if key in self._done:
                return self._done.pop(key)
            if key in self._gaveup:
                return GIVEUP
            dl = self._pending.get(key)
            if dl is None:
                return MISS                              # 没预跑（本地 Agent / 已收割）
        t_end = dl
        if timeout is not None:
            t_end = min(t_end, time.monotonic() + max(0.0, float(timeout)))
        while True:
            with self._lock:
                if key in self._done:
                    return self._done.pop(key)
                if key in self._gaveup:
                    return GIVEUP
                if key not in self._pending:
                    return MISS
                now = time.monotonic()
                if now >= dl:                            # 过了收割点 ⇒ 丢弃
                    self._pending.pop(key, None)
                    self._gaveup.add(key)
                    return GIVEUP
            if time.monotonic() >= t_end:
                return WAIT
            time.sleep(0.02)

    def discard_all(self) -> None:
        """相位结束：在飞的一律作废（它们的结果不再被接收）。"""
        with self._lock:
            self._gaveup.update(self._pending)
            self._pending.clear()
            self._done.clear()

    def in_flight(self) -> int:
        with self._lock:
            return len(self._pending)
