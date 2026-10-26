# runs/ 对局存档 —— 格式说明与脱敏样例

每跑完一局，`run_archive.py` 自动在 `runs/` 下生成一个目录（**runs/ 不进 git**，
本目录 `runs-sample/` 是提交进仓库的脱敏样例，给交接人看格式用）：

```
runs/20261026-191445_ab12cd34_vbf008d0/
├── meta.json        开局即写：局号 / 种子 / 版本短哈希 / 座位表 / 模型配置
├── trace.jsonl      一次模型调用一行（prompt 与 raw_output 全文）
├── timeline.jsonl   局末写全：公开动作流（发言 / 播报 / 投票 / 出局）
└── result.json      局末写：胜负 / 票型 / 死亡 / 夜间记录 / metrics / 存档错误
```

目录名 `<日期时间>_<局号>_v<git短哈希>` —— **版本短哈希在目录名里**，
两版对比时按它分组，不会混。

## 五个指标怎么算（只看文件，不看代码、不重跑）

| 指标 | 口径 | 用哪些字段 |
|---|---|---|
| 发言合规率 | `action=speak` 的行里，raw_output 能被正常解析上台（不含整段 JSON / 空串）的比例 | `trace.jsonl: action + raw_output` |
| 投票有据率 | `action=vote` 的行里，raw_output 给了具体理由（引用了具体座位号 / 事件）的比例 | `trace.jsonl: action + raw_output` |
| 自相矛盾率 | 同一座位前后发言里，对同一事实（自称身份 / 对某号的怀疑）表述冲突的比例 | `trace.jsonl: seat + round + raw_output` 按座位分组人工比对 |
| 信息引用率 | 发言中引用了 `visible_info` 里真实存在的信息（票型 / 死亡 / 昨夜事件）的比例 | `trace.jsonl: raw_output + visible_info` |
| 越权率 | 发言试图行使自己角色没有的技能（狼人说验人、民说毒人等）的比例 | `trace.jsonl: raw_output + visible_info.role` |

## trace.jsonl 一行（脱敏样例）

```json
{"ts": "2026-10-26T19:14:50", "round": 1, "phase": "day", "seat": 1, "role": "seer", "action": "speak", "legal_targets": [1, 2, 3], "visible_info": {"seat": 0, "role": "seer", "round": 1, "phase": "day", "players": ["…全文，9~15 个座位的存活/状态…"], "public_log": ["…全文…"], "rules": "…", "my_speeches": "…", "investigations": "…"}, "prompt_system": "…实际渲染后的 system 全文（SYSTEM_TEMPLATE.format 之后的）…", "prompt_user": "…实际渲染后的 user 全文…", "few_shot_ids": null, "model": "deepseek-flash", "model_version": null, "temperature": 0.8, "seed": 424242, "thinking": "0", "max_tokens": null, "raw_output": "…模型原样输出，一个字符都不改（含尾随空格、thought、情绪标记）…", "parsed": null, "postprocess": [], "latency": {"first_token_ms": null, "total_ms": 2340}, "tokens": {"prompt": 1024, "completion": 87}, "engine_verdict": null, "reject_reason": null}
```

## 字段说明与诚实边界

- `visible_info` / `raw_output`：**全文**，不截断不摘要（越权率、引用率没有原文判不了）。
- `prompt_system` / `prompt_user`：**实际渲染后**的文本（模板 .format 之后的），
  回答"这次到底喂了哪些字"。
- `few_shot_ids`：当前代码**没有 few-shot 机制**，如实记 null；
  将来上了 few-shot，同一字段补 id，原文另存 `prompts/few_shot_<版本>.json`。
- `parsed` / `postprocess` / `engine_verdict`：当前记 null / []。
  模型说了什么 → 桌上最终采纳了什么，用 `raw_output` + `timeline.jsonl` 人工对齐
  （votes / deaths 在 result.json 里有终局真相）。
- `latency.first_token_ms`：恒为 null —— 调用是非流式的，没有首 token 时刻。
- `model_version` / `max_tokens`：当前没有独立版本号、payload 未设 max_tokens，如实 null。
- `engine_verdict` 只在两种情况非 null：`"error"`（这次调用失败，`reject_reason`
  带异常）——解析成功与否看 raw_output 自己判。
- 调用失败的行：`raw_output=""`、`engine_verdict="error"`、`reject_reason` 带异常摘要。

## 归因约定

- 归因假设"同一时刻只有一局在推进"（与实验室 lab_api._ctx 的既有约定一致）；
  trace 行的 `seed` 就是本局种子，可用它核对归属。
- `timeline.jsonl` 的 `round` 从 sys 播报的「第X夜 / 第X天」推进补齐，
  首条播报之前的行记 null。

## 复现对比（验收标准 2）

`meta.json.seed` + `LLM_TEMPERATURE=0` 重跑同一局：rng 与规则层完全复现，
LLM 在温度 0 下 raw_output 应高度接近。若不一致，查 top_p / 并发顺序 /
时间戳是否进了 prompt。开局 API：`POST /api/game/new` 带 `seed` 字段。

## 工程保证（验收标准 3）

存档全链路 try/except：`runs/` 只读或不可写时，错误只攒进内存、局末写进
`result.json`（若最终那次也写失败就只剩服务端日志），**对局照常跑完**。
