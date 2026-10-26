# 提示词实验室（工作台）—— 详细说明

> GitHub 代码：
> - 页面：https://github.com/lanlan33989/agent-human-werewolf/blob/main/frontend/promptlab.html
> - 后端 API：https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/lab_api.py
> - 配置台/契约 API：https://github.com/lanlan33989/agent-human-werewolf/blob/main/backend/admin_api.py
> 访问：服务起起来后开 `http://<主机>:8000/lab`（必须同源 —— 页面要直连契约与步骤 API，无 CORS 问题）

## 一句话

给**提示词负责人**用的迭代驾驶舱：左边管"用哪家的模型跑"，中间**直接改线上提示词**（和主产品是同一份，保存即生效），右边看真对局直播、把任何一步拉出来回退重跑、对比改前改后。

## 三列结构

### 第①列 · AI 席位（试跑用）
- 每个席位 = 一套 `baseUrl + 模型名 + apiKey + temperature + 超时/重试/thinking`
- **对照席位B**（勾 `ref=true`）只试跑对比，**不进对局**
- **推席位**：点「推送到对局」→ `POST /api/lab/agents-config` → 服务端 `lab/agents_runtime.json`（持久）→ **下一局对局的 LLM 通道全部走第①列**（key/model/baseUrl 按座位循环分配）——服务端 .env 的 key 退化为兜底
- ⚠️ 主产品 `/play` 封面改的 LLM key 在席位覆盖存在时**不用于对局**（页面会提示）；ASR/TTS key 不受席位影响，永远走服务端配置

### 第②列 · 线上提示词契约（核心）
- 「载入线上契约」：`GET /api/admin/prompts` 拉全部可改条目（**动态展开**——代码里加了新常量这里自动多一项，还有只读守卫区）
- 「保存到线上」：`POST /api/admin/prompts` → 原地改 `agents.py`/`summarize.py` 模块属性 + 落盘 `_runtime.json`（重启仍生效）
- **与主产品同一份，没有副本**：主产品对局的 prompt 组装（`agents.build_messages`）每次模型调用实时读这些模块属性
- ⚠️ 生效语义：**下一次模型调用即生效（含进行中的局）**，不是"下一局"——想只影响新局，在没有对局进行时保存
- 下方"离线沙盒"与线上**不同源**（页面已标注）：只是草稿试验，定稿必须抄进上方契约保存

### 第③列 · 直播 / 回放 / 快照
- 直播：纯 AI 局实时滚动（含**上帝视角**：身份徽标、每晚谁刀了谁——旁观者可看、AI 不知道）
- 快照：每相位步自动存（内存 80 步环形缓冲）；**回退**到任何一步 → 改第②列契约 → **重跑这一步**（规则等价复现：rng 在快照里，LLM 输出天然非确定）
- 单步放行：暂停状态下"再走一步"（`lab/control.json` 暂停门）
- 单步重跑还可整体替换 prompt（一次性消费、120s TTL）

## 对局存档（给度量用）

每局结束自动写 `runs/<日期>_<局号>_v<git短哈希>/`：

```
meta.json        种子/版本/座位表/模型配置（复现锚点）
trace.jsonl      一次模型调用一行：visible_info 全文 + 渲染后 prompt 全文 + raw_output 原样
timeline.jsonl   公开动作流（发言/播报/死亡，含 round）
result.json      胜负/票型/死亡/夜记录/metrics
```

五个度量指标（发言合规率/投票有据率/自相矛盾率/信息引用率/越权率）**只看这些文件就能人工算**，
口径表与脱敏样例：https://github.com/lanlan33989/agent-human-werewolf/tree/main/runs-sample
（`runs/` 不进 git；追加写、进程锁、runs 不可写时对局照常）

## 常用工作流（提示词负责人每天用的循环）

1. 导入一局真实对局（`/api/lab/export` 的 lab-export JSON 或旧 steps.jsonl）
2. 载入某一步 → 快照 → 回退
3. 第②列改契约 → 单步重跑 → 第③列对比改前改后输出
4. 满意 → 保存到线上（这就是主产品在用的了）→ 开一局新的跑整局验证
5. 每局自动落 `runs/`，把 git 短哈希记下来 —— 两版对比就是两个哈希目录的对比

## 后端支撑（lab_api.py 关键点）

- 快照 = 驱动器每步一个落点（与暂停门/单步同粒度）；回退语义是**规则等价**不是逐字复现
- prompt 覆盖（单步重跑）一次性消费、120s TTL，防污染后续调用
- 步骤落盘 `lab/steps.jsonl`：`{round, phase, seat, role, model, ms, raw, prompt:{system,user}}` —— 回放页的数据源
- 守门测试：`tests/_smoke-lab-api.py`（17 项）、`tests/_smoke-admin.py`（45 项，含"每个提示词常量必须登记契约"的守门）
