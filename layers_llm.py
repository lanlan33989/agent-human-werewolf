# -*- coding: utf-8 -*-
"""LLM 层实现（Mock / OpenAI 兼容 / 慢速 Mock）。

2026-10-06 P4a：自 server.py 机械搬移（原样保留，不改任何行为）——
server.py 只留装配（build_pipeline/lifespan）与传输（路由/WS）。
"""
import json
import os
import time
import httpx

# 提示词实验室：所有模型调用都必经本文件，落盘放在这里最省事也最全。
# 关不掉就设 LAB_LOG=0；任何异常都被 lab_api 内部吞掉，不会影响对局。
try:
    import lab_api as _lab
except Exception:      # 没有这个模块（或被删）就当没这回事
    _lab = None


class Llm:
    def chat(self, user_text: str) -> str:
        """探针页用：一句话进、一句话出（自带 SYSTEM）。"""
        raise NotImplementedError

    def chat_messages(self, messages: list[dict], timeout: float | None = None) -> str:
        """
        对局用：接受**完整的 messages 列表**（system + user 都由调用方给）。

        为什么必须多这一档：`agents.build_messages()` 已经组装好了带私有块
        与硬事实的 prompt。如果把它硬塞进 `chat(user_text)`，那层自带的 SYSTEM
        会跟它对冲 —— 身份和作用域都会串（"你是三号玩家"和"你是其中一名玩家"）。
        """
        raise NotImplementedError


class MockLlm(Llm):
    def chat(self, user_text: str) -> str:
        return "（mock）收到，我是四号。你说的我记下了，先过。"

    def chat_messages(self, messages: list[dict], timeout: float | None = None) -> str:
        return "（mock）我先过，听你们的。"


class OpenAICompatLlm(Llm):
    """
    OpenAI 兼容端点 —— 协议完整可用。
    火山方舟：LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
    DeepSeek：LLM_BASE_URL=https://api.deepseek.com/v1
    """

    SYSTEM = (
        "你在和几个朋友面对面玩狼人杀，你是其中一名玩家。"
        "说话要像真人聊天，口语化、简短，不超过 25 个字。"
        "不要解释规则，不要自称 AI。"
    )

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 model: str | None = None, timeout: float | None = None,
                 turn_timeout: float | None = None,
                 temperature: float | None = None,
                 thinking: str | None = None) -> None:
        """
        参数优先、环境变量兜底。

        为什么要能传参：**同一个类要装成两个通道** —— 对局通道（贵、快）
        和摘要通道（便宜、可以慢一点，见 H4）。用环境变量分身会导致
        "改摘要模型把对局模型一起改了"这种事故。
        temperature/thinking（2026-10-26）：实验室席位接管对局后要按席位生效；
        不传的旧调用零变化（走 env 兜底，默认同旧值）。
        """
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.temperature = float(temperature if temperature is not None
                                 else os.environ.get("LLM_TEMPERATURE", "0.8"))
        self.thinking = str(thinking if thinking is not None
                            else os.environ.get("LLM_THINKING", "0"))
        # 探针页沿用一个宽预算（与改动前一致，不动它的行为）。
        # 对局路径单独一个更短的预算：轮到某个 AI 说话时，桌上没人能等 60 秒，
        # 超时就退化成一句本地套话（见 agents.LlmAgent）。
        self.timeout = float(timeout if timeout is not None
                             else os.environ.get("LLM_TIMEOUT", "60"))
        # 对局路径的超时预算。定 50 秒是因为**实测过**：中科大那家单次调用
        # 10.3 / 42.6 / 11.0 秒（2026-09-15）—— 比它文档里写的 0.2~2s 慢一个量级。
        # 设小了会把「慢但成功」的调用掐掉，反而更糟（详见 chat_messages 里的超时分支）。
        self.turn_timeout = float(turn_timeout if turn_timeout is not None
                                  else os.environ.get("LLM_TURN_TIMEOUT", "50"))

    def chat(self, user_text: str) -> str:
        return self.chat_messages([
            {"role": "system", "content": self.SYSTEM},
            {"role": "user", "content": user_text},
        ])

    # 这些状态要先退避重试，而不是直接失败：
    #   429 = 平台限流（中科大那家是 20 请求/分钟）
    #   5xx = 平台侧瞬时故障（重试即可，不是配置错）
    RETRY_STATUS = (429, 500, 502, 503, 504)

    def chat_messages(self, messages: list[dict], timeout: float | None = None) -> str:
        if not (self.base_url and self.api_key and self.model):
            raise RuntimeError("未配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
        payload = {"model": self.model, "messages": messages,
                   "temperature": self.temperature}
        # 🔴 2026-10-05 用户定：**默认关掉模型自带思考**（thinking）。
        #    实测：问"1+1"，默认要烧 42 个 reasoning token 才吐一个"2"；
        #    关掉后 completion 从 44 掉到 1。而实局里一次投票的 reasoning 高达
        #    8494 tokens ⇒ 43~50s —— "慢"的大头就在这里（不是我们的 thought：
        #    thought 是它输出的正文，算普通 completion tokens）。
        #    ⚠️ 我们的 thought 范式已经承担"战术梳理"这个角色，模型再内部长考一遍
        #    是重复劳动。想 A/B 质量就设 LLM_THINKING=1 把它开回来。
        if self.thinking != "1":
            payload["thinking"] = {"type": "disabled"}
        attempts = max(1, int(os.environ.get("LLM_RETRIES", "3")))
        last: Exception | None = None
        # 🔴 2026-10-04 用户定：给每次调用打点（耗时/重试/输入输出规模）——
        #    实测"两位 AI 说话间隔中位 47s"，先把单次决策的耗时拆开再谈优化。
        _t0 = time.monotonic()
        _in_chars = sum(len(str(m.get("content") or "")) for m in messages)

        for i in range(attempts):
            try:
                with httpx.Client(timeout=self.timeout if timeout is None else timeout) as c:
                    r = c.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
                if r.status_code in self.RETRY_STATUS:
                    raise httpx.HTTPStatusError(
                        f"HTTP {r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                body = r.json()
                msg = body["choices"][0]["message"]
                # 思考型模型把内容写在 reasoning_content 里，content 可能是 null
                out = (msg.get("content") or msg.get("reasoning_content") or "").strip()
                _u = body.get("usage") or {}
                print("[llm-timing] "
                      + "耗时=" + str(round(time.monotonic() - _t0, 1)) + "s"
                      + " 第" + str(i + 1) + "次尝试"
                      + " 输入" + str(_in_chars) + "字"
                      + " 输出" + str(len(out)) + "字"
                      + " 输出=" + repr(out[:36]), flush=True)
                # 2026-10-05 用户要：完整 usage + 缓存命中/未命中单独列出
                print("[llm-usage] full=" + json.dumps(_u, ensure_ascii=False), flush=True)
                print("[llm-usage] prompt_tokens=" + str(_u.get("prompt_tokens"))
                      + " prompt_cache_hit_tokens="
                      + str(_u.get("prompt_cache_hit_tokens"))
                      + " prompt_cache_miss_tokens="
                      + str(_u.get("prompt_cache_miss_tokens")), flush=True)
                if _lab:
                    _lab.log_call(model=self.model, messages=messages, raw=out,
                                  ms=int((time.monotonic() - _t0) * 1000), ok=True)
                try:
                    import run_archive as _ra
                    _ra.note_model(self.model, self.temperature, self.thinking,
                                   int((time.monotonic() - _t0) * 1000), _u)
                except Exception:                            # noqa: BLE001
                    pass
                return out
            except httpx.TimeoutException as e:
                # ⚠️ 超时**不走重试**：它已经把整段预算烧光了，再重试只会让桌上多等一轮。
                # 实测中科大那家单次调用 10~43 秒 —— 掐断重试比老实等完更糟。
                print("[llm-timing] 超时 耗时=" + str(round(time.monotonic() - _t0, 1))
                      + "s 输入" + str(_in_chars) + "字", flush=True)
                if _lab:
                    _lab.log_call(model=self.model, messages=messages, raw="",
                                  ms=int((time.monotonic() - _t0) * 1000), ok=False,
                                  err="timeout")
                try:
                    import run_archive as _ra
                    _ra.note_model(self.model, self.temperature, self.thinking,
                                   int((time.monotonic() - _t0) * 1000), None)
                except Exception:                            # noqa: BLE001
                    pass
                raise RuntimeError(f"LLM 超时：{e}") from e
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as e:
                last = e
                print("[llm-timing] 第" + str(i + 1) + "次尝试失败（"
                      + str(round(time.monotonic() - _t0, 1)) + "s）："
                      + type(e).__name__ + " " + str(e)[:80], flush=True)
                if i + 1 < attempts:
                    time.sleep(min(8.0, 0.8 * (2 ** i)))     # 0.8s / 1.6s / 3.2s

        if _lab:
            _lab.log_call(model=self.model, messages=messages, raw="",
                          ms=int((time.monotonic() - _t0) * 1000), ok=False,
                          err=f"{type(last).__name__}: {last}"[:200])
        try:
            import run_archive as _ra
            _ra.note_model(self.model, self.temperature, self.thinking,
                           int((time.monotonic() - _t0) * 1000), None)
        except Exception:                                # noqa: BLE001
            pass
        raise RuntimeError(f"LLM 调用失败（已重试 {attempts} 次）：{last}")


class MockLlmSlow(MockLlm):
    """用于测量「延迟遮盖」的假想值，暂未启用。"""
