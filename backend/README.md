# backend/ —— 主产品后端（FastAPI 对局服务 + 语音 + 实验室 API）

> GitHub：https://github.com/lanlan33989/agent-human-werewolf/tree/main/backend
> 接口契约（前端同学看这份）：https://github.com/lanlan33989/agent-human-werewolf/blob/main/docs/contract.md

## 一句话

把"一桌狼人杀"搬进一台手机：**确定性规则引擎**管发牌/昼夜/结算/信息隔离（AI 想作弊都拿不到别人的底牌），**LLM 玩家**只在"该说哪句话、该投谁"的决策点出场，**夜间驱动器**把一夜的多个模型调用编排成不冷场的节拍。

## 文件地图（每个文件干什么）

| 文件 | 职责 |
|---|---|
| `server.py` | **总装**：FastAPI app + 页面路由（`/play` `/lab` `/admin` `/board`）+ 语音上传/播报/WS ASR 中继 + CORS（联调白名单）+ LLM/ASR/TTS 通道装配（`.env` 驱动，MOCK 可全假） |
| `game_api.py` | 对局 HTTP API：开局/轮询/操作/回放/战报；`Session` 是一局的宿主（rng、agents、timeline、锁） |
| `engine.py` | **规则引擎（纯确定性）**：发牌、夜刀/解/毒/守/验、投票结算、胜负判定、`view`（每个座位能看到什么）—— 四层信息隔离在这层做死 |
| `agents.py` | AI 玩家：`LlmAgent`（真模型）/`RuleAgent`（模板兜底）/`ScriptedAgent`（测试）；**prompt 组装唯一入口** `build_messages`（契约常量全在这） |
| `night_driver.py` | 夜间流程驱动：剧本表节拍、收割预跑结果、猎人开枪、终局分支 |
| `night_pre.py` | 后台预跑池：天黑即并发预跑 AI 决策（守卫→狼→预言家），收割点在锁内写引擎 |
| `night_script.py` | 夜间剧本表：每个节点的台词/时长/收束条件（**改夜环节奏只动这张表**） |
| `speech_io.py` | 发言/播报进出 timeline 的合并流（`_flush_log` 游标搬运 public_log） |
| `steps.py` | 推进步进器（drive 的预算与节拍） |
| `ai_behaviors.py` | AI 决策与预跑收割的粘合（等待/超时丢弃/默认放弃） |
| `layers_llm.py` | LLM 适配（OpenAI 兼容）：重试/超时策略、usage 打点、温度/thinking 配置 |
| `layers_asr.py` / `layers_tts.py` | 云 ASR（qwen3-asr-flash）/ 云 TTS（qwen-tts）适配；TTS 缺 key 回落浏览器本地合成 |
| `lab_api.py` | **工作台 API**（详见 `docs/lab-README.md`）：快照/回退/重跑/暂停门/步骤落盘/席位覆盖/导出 |
| `admin_api.py` | **配置台 + 提示词契约 API**：key 白名单改动（密钥只进不出）、prompt 可改清单（动态展开 + 守门）、`_runtime.json` 持久化 |
| `run_archive.py` | 对局自动存档：每局 `runs/<时间>_<局号>_v<git短哈希>/`（meta/trace/timeline/result，详见根 README 的度量一节） |
| `experience.py` / `metrics.py` / `report.py` / `summarize.py` | 跨局经验库 / 对局指标 / 战报 markdown / 长上下文压缩 |
| `run_https.py` | 自签名证书生成（手机麦克风必须 HTTPS） |
| `demo_game.py` / `eval_batch.py` | 本地对局演示 / 批量评测 |

## 启动

```powershell
# 仓库根 h5/ 下（推荐，自动清进程/自检/双协议 http+https）
.venv\Scripts\python.exe start.py
# 或最小启动
.venv\Scripts\python.exe -m uvicorn server:app --app-dir backend --host 0.0.0.0 --port 8000
```

- `.env` 放**仓库根**（模板 `.env.example`）：`LLM_API_KEY`/`LLM_MODEL`/`LLM_TEMPERATURE`/`LLM_THINKING`、`DASHSCOPE_API_KEY`（ASR/TTS）、`MOCK=1`（全假通道，联调/测试不花钱）
- 全部 key 也可以在 `/admin` 配置台改（写 `_runtime.json`，优先于 .env，重启仍生效）

## 必须知道的四个机制

1. **信息隔离在引擎层**：`engine.view(seat)` 只给该座位该知道的。任何"让 AI 更聪明"的改动都不许破坏它 —— `tests/_smoke-agents.py`（129 项）守着。
2. **prompt 组装唯一入口**：`agents.build_messages`。所有提示词都是**契约常量**（模块级，登记在 `admin_api.PROMPT_STATIC`），改 prompt 走 `/lab` 第②列或 `/admin`，**下一次模型调用即生效**（含进行中的局）。守门测试会抓"加了 prompt 没登记"：`tests/_smoke-admin.py`。
3. **夜间预跑 + 时钟预算**：AI 夜间决策天黑就并发预跑，收割点在预算内等；超预算丢弃走默认。⇒ 同 seed 的**逐字**复现不可保证，规则层（胜负/票型）可复现 —— 存档复现对比按 `docs/存档与度量口径.md` 的口径（温度 0 + 真模型比"高度接近"）。
4. **print 不许用 GBK 编不了的字符**（如 ⇒）：start.py 拉起的进程没有 PYTHONIOENCODING=utf-8，一个特殊字符整个服务起不来（踩过实测坑）。

## 测试（都在 `h5/tests/`，`python tests/<脚本>` 直接跑）

| 脚本 | 盖什么 | 现状 |
|---|---|---|
| `_smoke-agents.py` | 信息隔离 129 项 | 全绿 |
| `_smoke-admin.py` | 配置台/契约/守门 45 项 | 全绿 |
| `_smoke-lab-api.py` | 实验室快照/回退/导出 17 项 | 全绿 |
| `_smoke-runs.py` | 存档四件套/只读不炸局/复跑 28 项 | 全绿 |
| `_smoke-night-live.py` | 协议级跑一整夜 29 项 | 28/29（I9 为存量失败，联调周核对） |
| `_smoke-game-api.py` | 对局 API 全量 | 5 个失败为历史遗留（layout 宽容化、未实现的 speech_mode 开关） |

⚠️ `_smoke-game-api.py` 部分段会走**真模型**（烧钱、耗时数分钟），日常跑前面四个就够。
