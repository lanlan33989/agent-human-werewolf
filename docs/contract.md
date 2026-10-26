# 前后端接口契约（v1.1 · 2026-10-26 冻结，2026-10-27 小修）

> **v1.1（2026-10-27，纯加法不破坏）**：`GET /api/game/games` 的行里增加 `archive` 字段
> （留档目录名）—— 榜单页要给出"这一局存到哪儿了"。另：§0 的推荐开发形态改为
> `frontend/mock/mock_server.py` 单端口方案，并新增 §0.1 并行五步。

> **规矩**：所有协议字段以本文件为准。谁想加/改字段，先改本文 + commit，另一方跟进 ——
> 不许口头说"我这边加了个字段你适配一下"。每次破坏性改动把 `v` 加一并在此记录。
>
> 配套文件：
> - `docs/openapi.json` —— FastAPI 自动生成的全量端点描述（`python -c "import sys; sys.path.insert(0,'backend'); from server import app; import json; print(json.dumps(app.openapi()))" > docs/openapi.json` 重新生成）
> - `frontend/mock/fixtures/*.json` —— **实机抓的**真实响应样例，前端 mock 直接喂这些
> - `frontend/mock/mock_server.py` —— 50 行假服务，把 fixtures 当 API 返回

## 0. 基址与形态

- 生产形态：页面由后端同源服务（`http://<主机>:8000/play`），**无 CORS 问题**。
- **推荐的开发形态（2026-10-27 起）**：`python frontend/mock/mock_server.py`（`:5501`）
  —— 这个假服务**同端口吐页面**，一个端口给全套：`http://127.0.0.1:5501/dev_panels.html`
  （面板画廊）/ `.../play.html?demo=<场景>`（单场景）/ `.../play.html`（假数据全流程）。
  前端不需要装依赖、不需要起后端、不需要第二个静态服务器。
- 备用形态（要连真后端调才会用）：前端用 Live Server（`:5500`）↔ `http://localhost:8000` 的 API
  —— 后端已开 CORS 白名单（`server.py` 里 CORSMiddleware，要加端口改那里）。
- 语音链路开发期可全关：环境变量 `MOCK_TTS=1`（服务端不真合成）；前端自己静音即可。

## 0.1 这一周两边都要加东西时，怎么并行（2026-10-27 定，五步）

1. **契约先行**：后端要加接口/字段 ⇒ 先在**本文件**贴一份**真实返回样例 JSON**（不是文字描述），
   commit 并 @ 前端。加法字段不必升版本，破坏性改动才把顶部 `v` 加一。
2. **前端不等后端**：拿这份样例在 [`frontend/demo_scenes.js`](../frontend/demo_scenes.js)
   里加一个场景 —— 刷新 [`frontend/dev_panels.html`](../frontend/dev_panels.html)
   立刻能看能改样式（画廊**自动读场景文件生成格子**，不用改画廊）。
3. **mock 同步**：后端同时把样例塞进 `frontend/mock/`（fixtures 或路由），
   于是 `:5501/play.html` 的假数据全流程也能跑通同一屏，两条路看到的字段一致。
4. **联调只换数据源**：真后端就绪后，前端把 `?demo=` 去掉即可 —— **渲染分支一行都不用改**
   （demo 场景走的就是真实渲染管线：`resumeInto → enterTable → tick → renderNightDial`）。
5. **每周两次（周一 / 周四）**一起滚一遍画廊，只问一句："产品里有、画廊里没有的是哪一屏"。

> 完整的界面清单（每屏的触发数据 / 场景 key / 代码位置）与"加一个新界面三步"见
> [`docs/前端界面清单.md`](前端界面清单.md)；全员可点的链接总表见
> [`docs/入口链接.md`](入口链接.md)。
> 改完记得跑一次自检：`.venv/Scripts/python.exe tests/_check-split-consistency.py`。

## 1. 轮询协议（唯一强耦合点）：`GET /api/game/{gid}/public`

前端**每 1~1.5s 轮询**这一个接口驱动整个界面。完整真实样例：`frontend/mock/fixtures/public.json`。

顶层字段（实机样例抓取，2026-10-26）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `gid` | str | 局号 |
| `phase` | str | 引擎相位：`SETUP / NIGHT / DAWN / DAY / VOTE / SHOOT / ENDED` 等 |
| `pending` | str | "现在该谁做什么"：`working`（AI 在算）/ `human_say` / `human_vote` / `human_night:{node}` / `ended` / `stuck` … |
| `round` | int | 第几天 |
| `players` | array | 座位数组（seat/display/kind/alive） |
| `timeline` | array | **合并流**：引擎播报与玩家发言按真实先后排（见下） |
| `public_log` | array | 引擎播报原文（不含玩家发言） |
| `speech` / `current_speaker` / `speaking_order` | — | 发言模式与进度（轮到谁 / 谁说过） |
| `votes` / `voted_seats` | — | 当前投票的票与已投票座位（**投票进行中不含票型**，结算后才进 public_log） |
| `suspicion` | — | 疑点面板数据 |
| `night_node` / `night_plan` | — | 夜间剧本：当前节点与计划节点表（node/at/duration） |
| `now` | object | **当前面板状态机**（重点，见下） |
| `winner` | str|null | 终局胜负；非 null 即对局结束 |
| `board` / `board_key` / `level` / `speech_mode` | — | 板子/档位/发言方式 |

### `timeline[]` 每条的字段

```json
{"t": "sys", "s": "游戏开始，6 人局，每人 1 条命。", "voice": "Neil", "ts": 1793034399.25}
{"t": "say", "s": "我觉得 3 号有问题。", "seat": 2, "voice": "Ethan", "spoken": false}
```

- `t`：`sys`（法官播报）/ `say`（玩家发言）
- `s`：文本；`voice`：TTS 音色名；`ts`：写入时间戳
- `at`/`delay`/`node`/`stage`：夜间节点台词才有（定位到哪个夜的哪个节点、播报节拍）

### `now`（面板状态机）——夜间面板/轮盘/倒计时的唯一依据

```json
{"kind": "wheel", "seat": 3, "node": "witch_heal", "until": 1793034500.0,
 "need": "target", "why": "女巫：今晚 3 号被刀，要不要用解药？",
 "started": true, "until_left": 12.4, "panel_line": "……", "lines_due": [...]}
```

| 字段 | 说明 |
|---|---|
| `kind` | `none`（没有人类要点）/ `wheel`（轮盘选目标）/ `say`（该你发言）… |
| `seat` | 该操作的人类座位（不是你 ⇒ 面板不弹） |
| `node` | 夜节点名：`wolf / guard / seer / witch_heal / witch_poison / shoot …` |
| `until` / `until_left` | 窗口截止（绝对时间戳）/ 剩余秒数（**前端只渲染，不做裁决**） |
| `started` | 服务端窗口是否已正式开始 —— **面板弹出必须听它**，不许前端自己提前弹 |
| `panel_line` / `lines_due` | 面板提示文案与到期播报行 |
| `need` | 需要的输入形态：`target` / `yesno` / `text` |

⚠️ 最近一周的 bug 高发区（联调重点演练）：`started=false→true` 的面板弹出时机、
`until_left` 读秒回涨、`arm` 不再延长窗口（arm 只是"我看到了"，**不改变窗口**）、
多人抢答、夜转昼的播报队列排空。

## 2. `POST /api/game/{gid}/act` —— 人类操作（全枚举）

请求体：`{"action": "<枚举>", "target": <座位号|null>, "text": "<发言>"}`

| action | target | 说明 |
|---|---|---|
| `speak` | — | 发言（`text` 必填；轮不到你 400） |
| `vote` | 必填 | 投票 |
| `kill` | 必填 | 狼刀（多狼=提议，单狼=定案） |
| `confirm` | — | 狼队协商第二步：确认刀口（全员确认才定案） |
| `investigate` | 必填 | 预言家验人 |
| `heal` / `poison` | 必填 | 女巫用药 |
| `guard` | 必填 | 守卫守人 |
| `shoot` | 可 null | 猎人开枪（null=放弃） |
| `clear` | — | **再点同一个号码=取消**（撤回本窗口未结算的临时决定） |
| `pass` | — | 女巫明确不用药 / 猎人不开枪 / 空操作 |

成功返回 `{ok:true,...}` + 最新 `public`；参数/时机错误返回 400（`detail` 中文原因，可直接展示）。

## 3. 其他端点（前端会用到的）

| 端点 | 说明 | 样例 |
|---|---|---|
| `POST /api/game/new` | 开局。body：`{humans, ais, level?, personality?, seed?, layout?}`；**seed 传了就能复现** | `frontend/mock/fixtures/new_game.json` |
| `POST /api/game/{gid}/arm` | 面板 arm（播报念完的确认）；**不延长窗口** | |
| `GET /api/game/rules` / `settings` | 档位表 / 规则开关表（前端控件**由它生成**，不写死） | `rules.json` / `settings.json` |
| `GET /api/game/games` | 已结束清单（榜单页 `/board` 用）。行内含 `archive`=**留档编号**（`runs/<日期时间>_<局号>_v<git短哈希>`，2026-10-27 增） | |
| `GET /api/health` | 服务与各层真假 | `health.json` |
| `GET /api/tts?…`、`WS /ws/tts` | 云端 TTS 音频；`VOICE_ON=0` 时走浏览器本地合成（Web Speech） | |
| `POST /api/asr`、`WS /ws/asr` | 语音识别（16k WAV body / 流式） | |

全量 44 个端点见 `docs/openapi.json`。

## 4. 前端这周怎么干活（不碰后端）

1. `python frontend/mock/mock_server.py`（默认 :5501）——把 `fixtures/*.json` 按路由返回；
2. 夜间面板 / 轮盘 / 倒计时 / 播报队列全部对着假数据开发；用假 timeline 练**所有**状态：
   夜转昼、多人抢答、`started=false→true`、`until_left` 回退、`pending: working` 卡住 3 拍；
3. 要新状态？先在 `frontend/mock/fixtures/` 加样例 + 在本文件登记字段，再写代码；
4. 语音最后两天再接真链路（`/api/tts` 与 `/ws/asr`）。

## 5. 后端这周怎么干活（不碰前端）

1. 协议级自测：`python tests/_smoke-night-live.py`（不开浏览器，按协议跑一整夜，28 项）；
2. 改协议字段必须同步本文件并提交；破坏性改动把顶部 `v` 加一；
3. 对局存档（`runs/`，`run_archive.py`）与实验室（`/lab`）的端点变化也走同一规矩。

## 6. 版本记录

- **v1（2026-10-26）**：初版冻结。含夜间面板协议（`now.started/panel_line/lines_due`）、
  水泵节拍（服务端 est 推进，前端只渲染）、act 全枚举、CORS 联调白名单。
