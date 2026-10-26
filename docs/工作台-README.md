# 工作台 —— 提示词实验室 / 配置台 / 榜单与战报

> **三份并列的上手文档**（动哪个看哪个）：
> [`backend/README.md`](../backend/README.md)（后端）·
> [`frontend/README.md`](../frontend/README.md)（前端）·
> **本文件（工作台）**

> GitHub 入口总表：[docs/入口链接.md](https://github.com/lanlan33989/agent-human-werewolf/blob/main/docs/%E5%85%A5%E5%8F%A3%E9%93%BE%E6%8E%A5.md)

## 一句话

**工作台 = 不改产品代码就能改 AI 的地方**：在浏览器里直接改线上提示词、用真实对局回放逐步对比、把满意的版本保存（主产品同一份，立刻生效）；旁边的配置台管 key / 通道 / 音色，榜单与战报看每局结果。
它是给**提示词负责人**和**度量负责人**用的驾驶舱，不是给玩家用的。

## 开起来（两步）

```bash
.venv\Scripts\python.exe start.py          # 起服务（http:8000 / https:8443）
```

| 打开 | 页面 | 干什么 |
|---|---|---|
| <http://localhost:8000/lab> | 提示词实验室 | 改提示词、试跑、回退、对比、保存上线 |
| <http://localhost:8000/admin> | 配置台 | key / ASR·LLM·TTS 通道 / 音色 / 契约项开关 |
| <http://localhost:8000/board> | 榜单与战报 | 每局胜负、轮数、`战报` / `日志` 两个出口 |

⚠️ 必须**同源访问**（和 `/play` 同一个端口）：这三个页要直连 `/api/admin/*`、`/api/lab/*`，
跨域的话 Configure CORS 是白名单制的，新端口要去 `backend/server.py` 加。

## 代码地图（改东西改哪）

| 你要改的东西 | 源码 |
|---|---|
| 实验室三列 UI | [frontend/promptlab.html](https://github.com/lanlan33989/agent-human-werewolf/blob/main/frontend/promptlab.html) |
| 配置台 UI | [frontend/admin.html](https://github.com/lanlan33989/agent-human-werewolf/blob/main/frontend/admin.html) |
| 榜单与战报 UI | [frontend/board.html](https://github.com/lanlan33989/agent-human-werewolf/blob/main/frontend/board.html) |
| 实验室后端（快照/回退/重跑/步骤/席位/导出） | [backend/lab_api.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/lab_api.py) |
| 配置台后端（config / reload / prompts / play-keys） | [backend/admin_api.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/admin_api.py) |
| 步骤落盘 | [backend/steps.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/steps.py) |
| 跨局经验（局末总结 → 离线评审入库） | [backend/experience.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/experience.py) |
| 每局指标聚合 / 榜单数据 | [backend/metrics.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/metrics.py) |
| 战报正文（Markdown） | [backend/report.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/report.py) |
| 对局自动存档 | [backend/run_archive.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/run_archive.py) |
| 批量评测 | [backend/eval_batch.py](https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/eval_batch.py) |

> 页面路由在服务端是**一文件一条**（见 `backend/server.py` 的 `/lab` `/admin` `/board`）；
> 新增/拆分 js 不要忘了补那条路由（前台也因此踩过一次 404）。

## 三块地方，各管什么

### ① `/lab` 提示词实验室（三列）

#### 第①列 · AI 席位（试跑用）
- 每个席位 = 一套 `baseUrl + 模型名 + apiKey + temperature + 超时/重试/thinking`
- **对照席位B**（勾 `ref=true`）只试跑对比，**不进对局**
- **推席位**：点「推送到对局」→ `POST /api/lab/agents-config` → 服务端 `lab/agents_runtime.json`（持久）→ **下一局对局的 LLM 通道全部走第①列**（key/model/baseUrl 按座位循环分配）——服务端 .env 的 key 退化为兜底
- ⚠️ 主产品 `/play` 封面改的 LLM key 在席位覆盖存在时**不用于对局**（页面会提示）；ASR/TTS key 不受席位影响，永远走服务端配置

#### 第②列 · 线上提示词契约（核心）
- 「载入线上契约」：`GET /api/admin/prompts` 拉全部可改条目（**动态展开**——代码里加了新常量这里自动多一项，还有只读守卫区）
- 「保存到线上」：`POST /api/admin/prompts` → 原地改 `agents.py`/`summarize.py` 模块属性 + 落盘 `_runtime.json`（重启仍生效）
- **与主产品同一份，没有副本**：主产品对局的 prompt 组装（`agents.build_messages`）每次模型调用实时读这些模块属性
- ⚠️ 生效语义：**下一次模型调用即生效（含进行中的局）**，不是"下一局"——想只影响新局，在没有对局进行时保存
- 下方"离线沙盒"与线上**不同源**（页面已标注）：只是草稿试验，定稿必须抄进上方契约保存

#### 第③列 · 直播 / 回放 / 快照
- 直播：纯 AI 局实时滚动（含**上帝视角**：身份徽标、每晚谁刀了谁——旁观者可看、AI 不知道）
- 快照：每相位步自动存（内存 80 步环形缓冲）；**回退**到任何一步 → 改第②列契约 → **重跑这一步**（规则等价复现：rng 在快照里，LLM 输出天然非确定）
- 单步放行：暂停状态下"再走一步"（`lab/control.json` 暂停门）
- 单步重跑还可整体替换 prompt（一次性消费、120s TTL）

### ② `/admin` 配置台
`/api/admin/config`（读写 key 与通道）+ `/api/admin/reload`（热重载）+ `/api/admin/prompts`（契约）+ `/api/admin/play-keys`（封面那三个 key）。
**face 一句话**：这里改的东西影响的是"链路用哪家的服务"，实验室第②列改的是"AI 说什么话"。

### ③ `/board` 榜单与战报
只读页（随便刷新，不会弄坏任何对局）：`/api/game/stats`（按档聚合的胜率/平均轮数）+ `/api/game/games`（已结束清单 + 每局的 `战报` / `日志` 链接）。
战报正文是服务端吐的 **Markdown 纯文本**（`text/markdown`）—— 现在还没有排版，要把它做成有样式的战报页 = 新增界面，得走 [docs/前端界面清单.md §3](https://github.com/lanlan33989/agent-human-werewolf/blob/main/docs/%E5%89%8D%E7%AB%AF%E7%95%8C%E9%9D%A2%E6%B8%85%E5%8D%95.md) 的规矩。

## 谁用哪块

| 角色 | 用哪块 | 每天干什么 |
|---|---|---|
| **提示词负责人**（宁晰琳） | `/lab` 第②列为主，第③列对比 | 改契约 → 单步重跑对比 → 保存线上 → 开整局验证 |
| **度量负责人**（杨家彦） | `/board` + `runs/` 存档 | 按 [docs/存档与度量口径.md](https://github.com/lanlan33989/agent-human-werewolf/blob/main/docs/%E5%AD%98%E6%A1%A3%E4%B8%8E%E5%BA%A6%E9%87%8F%E5%8F%A3%E5%BE%84.md) 的五个指标算数、做前后两版对比 |
| **提示词 ↔ 后端接口对齐**（郑锦川） | 只在加/改契约常量时介入 | 代码里加常量 → 实验室自动多一项（守门测试会查"每个常量必须登记契约"） |
| 暂时没人需要 | `/admin` 配置台 | 换 key / 换通道时才动 |

## 每天的工作循环（提示词负责人）

1. 导入一局真实对局（`/api/lab/export` 的 lab-export JSON 或旧 steps.jsonl）
2. 载入某一步 → 快照 → 回退
3. 第②列改契约 → 单步重跑 → 第③列对比改前改后输出
4. 满意 → 保存到线上（这就是主产品在用的了）→ 开一局新的跑整局验证
5. 每局自动落 `runs/`，把 git 短哈希记下来 —— 两版对比就是两个哈希目录的对比

## 数据落在哪（度量用）

每局结束自动写 `runs/<日期>_<局号>_v<git短哈希>/`：

```
meta.json        种子/版本/座位表/模型配置（复现锚点）
trace.jsonl      一次模型调用一行：visible_info 全文 + 渲染后 prompt 全文 + raw_output 原样
timeline.jsonl   公开动作流（发言/播报/死亡，含 round）
result.json      胜负/票型/死亡/夜记录/metrics
```

五个度量指标（发言合规率 / 投票有据率 / 自相矛盾率 / 信息引用率 / 越权率）**只看这些文件就能人工算**，
口径与字段说明：[docs/存档与度量口径.md](https://github.com/lanlan33989/agent-human-werewolf/blob/main/docs/%E5%AD%98%E6%A1%A3%E4%B8%8E%E5%BA%A6%E9%87%8F%E5%8F%A3%E5%BE%84.md)
（`runs/` 不进 git；追加写、进程锁、runs 不可写时对局照常）

## 后端支撑（lab_api.py 关键点）

- 快照 = 驱动器每步一个落点（与暂停门/单步同粒度）；回退语义是**规则等价**不是逐字复现
- prompt 覆盖（单步重跑）一次性消费、120s TTL，防污染后续调用
- 步骤落盘 `lab/steps.jsonl`：`{round, phase, seat, role, model, ms, raw, prompt:{system,user}}` —— 回放页的数据源
- 守门测试：`tests/_smoke-lab-api.py`（17 项）、`tests/_smoke-admin.py`（45 项，含"每个提示词常量必须登记契约"的守门）

## 已知坑

1. **保存即生效，而且是下一次模型调用就生效** —— 有对局在跑时改契约会当场影响它；要只影响新局，请等没人玩时保存。
2. **推了席位之后，`/play` 封面填的 LLM key 就不管用了**（页面会提示）；这是设计如此，不是 bug（ASR/TTS 仍走服务端配置）。
3. 实验室的"离线沙盒"跟线上**不是同一份**，草稿定稿必须抄进第②列保存，否则等于白改。
4. 这些页面要**同源**访问（`http://<同一个主机>:8000/...`），别用别的端口的静态服务器开 —— 跨域部分接口不会通。
5. 新增 js/css/html 必须在 `backend/server.py` 补一条路由（一文件一路由），不补就是 404。
