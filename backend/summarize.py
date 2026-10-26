"""
G2 摘要器 + H4 便宜模型通道。

两条硬约束（改这个文件时别破）：

1. **入参只有公开发言**：签名就是 `(older: List[str]) -> str`，
   调用方（`agents._history_text`）传进来的本来就只有 `public_history`。
   哪天有人给它塞私有上下文，摘要就会把私有信息写进**全桌共享**的那段文本里 ——
   这是这个功能唯一的泄漏点，所以在签名上堵死。

2. **必须带缓存**：`_history_text` 在**每一次** agent 调用时都会跑（一局上百次）。
   没有缓存 = 每一句话都重新花钱摘要同一段内容。
   缓存键 = (更早的条数, 最后一条原文的前 120 字)：发言只往后追加，
   这两个值一起就能唯一定位"这一批内容"。

⚠️ 失败必须**降级成空串**（调用方会退回硬截断），绝不能让摘要挂掉整局。
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List, Optional

# 摘要提示词。三条要求都是踩过的坑：
#   · "只写这段话里出现过的事实" —— 不写这句，模型会顺手把"3 号很可疑"这种**判断**
#     写进摘要，而摘要会被全桌当真（等于替玩家推理）
#   · "不要列表" —— 列表会被当成结构化指令，模型会开始照着列表执行
#   · 明确说输入是**可能有点乱的口语**（ASR 转写），否则它会把错别字当线索
PROMPT = """你是狼人杀对局的记录员。下面是一局里**较早的一段公开发言**，
经过语音转写，可能有点乱：口语、口误、错别字、重复都可能有。

请压成不超过 4 句话的摘要，**三个要素，按这个顺序**写：
1. 谁在怀疑谁
2. 谁替谁说过话（保过谁）
3. 有没有人报身份、或者给出能被验证的逻辑

要求：
1. **只写这段话里出现过的事实**。不要推测、不要补充、不要下"谁最可疑"这类判断。
2. 直接写成一段话，不要用列表、不要分点、不要加标题。
3. 不要提"摘要""以上"这类元信息，就像在跟人转述刚才听到了什么。
4. **不用写投票票型**（谁投了谁）：票型会由主持人单独给全，写进来是重复，
   而且摘要里的票型容易被当成"他自己说的"来引用。"""


def make_summarizer(chat_fn: Callable[..., str], *,
                    timeout: Optional[float] = 20.0,
                    max_chars: int = 600) -> "HistorySummarizer":
    """按装配层给的 chat_fn 造一个摘要器（H4 的接入点）。"""
    return HistorySummarizer(chat_fn, timeout=timeout, max_chars=max_chars)


class HistorySummarizer:
    """
    把"更早的公开发言"压成一段话。可调用对象 —— 直接当 `build_messages(summarize=...)` 用。

    `calls` 会计数**真实的模型调用次数**（缓存命中不算）：成本核算和测试都要看它。
    """

    def __init__(self, chat_fn: Callable[..., str], *,
                 timeout: Optional[float] = 20.0, max_chars: int = 600) -> None:
        self.chat_fn = chat_fn
        self.timeout = timeout
        self.max_chars = max_chars
        self.cache: Dict[Any, str] = {}
        self.calls = 0          # 真调用次数（缓存命中不计）
        self.failures = 0
        # chat_fn 有的收 (messages, timeout)、有的只收 (messages)。
        # 用签名判断而不是 try/except TypeError —— 后者会把 chat_fn **内部**抛出的
        # TypeError 误判成"签名不匹配"（那是真 bug，会被吞掉）。
        try:
            self._takes_timeout = len(inspect.signature(chat_fn).parameters) >= 2
        except (TypeError, ValueError):
            self._takes_timeout = False

    def __call__(self, older: List[str]) -> str:
        hs = [str(x) for x in (older or []) if str(x).strip()]
        if not hs:
            return ""
        # ⚠️ 缓存键必须含**输入全文**，不能只看「条数 + 末尾 120 字」。
        #    这个摘要器是**进程级单例**（server.py 装配时建一次，注入每一局每个 Agent），
        #    而键里没有局号 ⇒ 只要两局「条数相同、末尾那条前 120 字相同」就会命中，
        #    把**上一局的摘要**当成本局的用 —— 那是跨局串味
        #    （2026-09-29 排查发现：结构缺陷是真的，只是自然复现概率低）。
        #    现在把全文纳入键：要撞上得两局的发言记录**逐字相同**，实际不可能。
        #    （更强的做法是每局新建一个摘要器实例，但那样 SUMARIZER 的累计统计会归零，
        #      先不动它；真出现内存/统计问题时再换。）
        key = (len(hs), hash("\x00".join(hs)))
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        text = self._ask(hs)
        if text:
            self.cache[key] = text
        return text

    # ------------------------------------------------------------ 内部

    def _ask(self, hs: List[str]) -> str:
        msgs = [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": "\n".join(hs)},
        ]
        try:
            self.calls += 1
            out = (self.chat_fn(msgs, self.timeout) if self._takes_timeout
                   else self.chat_fn(msgs))
        except Exception:                       # noqa: BLE001
            # 摘要挂了不能让整局卡住：返回空串 ⇒ 调用方退回硬截断
            self.failures += 1
            return ""
        text = str(out or "").strip()
        if len(text) > self.max_chars:
            text = text[:self.max_chars].rstrip() + "…"
        return text

    def stats(self) -> Dict[str, Any]:
        return {"calls": self.calls, "failures": self.failures,
                "cached": len(self.cache)}
