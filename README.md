# 面杀 AI 玩家 · agent-human-werewolf

把 AI 玩家带上真实的狼人杀牌桌：一部手机平放桌面，AI 与真人玩家同场发言、投票、周旋 —— 用语音说话，用语音回答。

![status](https://img.shields.io/badge/status-alpha-orange) ![license](https://img.shields.io/badge/license-MIT-green) ![python](https://img.shields.io/badge/python-3.10+-blue)

## 这是什么

一个**桌面（面杀）狼人杀 AI 伙伴**，核心解决三件事：

1. **听得懂** —— 中文语音识别，兼容狼人杀黑话（金水 / 查杀 / 平安夜 / 悍跳……）
2. **玩得像** —— 完整的狼人杀规则引擎 + LLM 驱动的 AI 玩家（发言、投票、夜间行动）
3. **说得出** —— 语音合成播报，法官与各 AI 玩家可配置不同音色

全链路支持 **Mock 模式**：不需要任何 API key，就能先跑通"浏览器拾音 → 服务端 → 回传播放"的完整链路。

## 功能特性

### 游戏引擎（`engine.py`）

- 完整规则：狼人、村民、预言家、女巫、猎人、守卫、白痴
- **四层信息隔离**做在结构上，不靠提示词约束：
  - `L0` 全局公开（public_log）→ `L1` 组内共享（狼队互知）→ `L2` 个体私有（验人/用药记录）→ `L3` 引擎内部（对 AI 不可见）
- 引擎只做确定性逻辑，不含任何 LLM 调用；`get_view_for(seat)` 统一回答"某人此刻该看到什么"，单机/多机形态共用一套代码

### AI 玩家（`agents.py` / `ai_behaviors.py`）

- LLM 玩家与规则兜底（RuleAgent）自动切换：LLM 超时/限流时退化为本地套话，**不会卡死一局**
- 429 / 5xx 自动退避重试，超时预算分层（探针页宽预算、对局内短预算）

### 语音链路（可分层替换，`layers_asr.py` / `layers_llm.py` / `layers_tts.py`）

| 层 | 本地（零 key 离线） | 云端 | Mock |
|---|---|---|---|
| ASR | faster-whisper（small 模型即可识别黑话） | Qwen-ASR（OpenAI 兼容协议，含流式 `qwen3-asr-flash-realtime`）；火山引擎（骨架） | ✅ |
| LLM | — | 任何 OpenAI 兼容端点（DeepSeek / 火山方舟 / …） | ✅ |
| TTS | Windows SAPI | Qwen-TTS（48 音色，法官与每个 AI 人格可配不同音色）；火山引擎（骨架） | ✅ |

- 每层独立开关（`MOCK_ASR` / `MOCK_LLM` / `MOCK_TTS`），可以只验某一层
- 流式 ASR 优先（首字 ~0.5s），不可用时自动回落整段上传

### 麦克风与拾音诊断

- 浏览器麦克风需要 HTTPS 安全上下文：`run_https.py` 一键生成自签名证书（SAN 含本机 IP）
- 服务端返回**拾音指标**（`rms_dbfs` / `snr_db` / `clipping_pct` / `voice_ratio_pct`），把"麦克风没收到"和"模型不行"两种失败分开
- 页面内置近场/远场对比表，自动记录响度差

## 快速开始

```bash
git clone https://github.com/lanlan33989/agent-human-werewolf.git
cd agent-human-werewolf

python -m venv .venv
.venv\Scripts\pip install -r backend/requirements.txt   # Windows
# source .venv/bin/activate && pip install -r backend/requirements.txt  # Linux/macOS

# 一键启动（自检端口 → 清理旧进程 → 单进程同听 http:8000 / https:8443）
.venv\Scripts\python.exe start.py
```

打开 `http://localhost:8000`（本机）或手机连同一 WiFi 打开 `https://<本机IP>:8443`（自签名证书选"继续访问"即可）。

**Mock 模式零配置可跑**；接真实 API 时复制 `.env.example` 为 `.env` 填入 key，详见文件内注释。

### 诊断

```bash
.venv/Scripts/python.exe start.py --doctor            # 只看端口现状，不动任何东西
.venv/Scripts/python.exe tests/_smoke-game-api.py     # 接口回归（testclient，无需起服务）
.venv/Scripts/python.exe tests/_smoke-engine.py       # 引擎规则回归
```

冒烟测试均可在仓库根目录直接运行：`python tests/<脚本名>.py`。

## 谁该看哪份（按角色进，别从头读）

三份上手文档**并列**，动哪个看哪个：
[backend/README.md](backend/README.md)（后端）· [frontend/README.md](frontend/README.md)（前端）·
[docs/工作台-README.md](docs/%E5%B7%A5%E4%BD%9C%E5%8F%B0-README.md)（工作台：提示词实验室 / 配置台 / 榜单与战报）。

| 你是 | 第一件事 | 入口文档 |
|---|---|---|
| **前端负责人** | clone 完**不需要装任何环境、不需要起后端**，先把画廊跑起来看全部界面 | [frontend/README.md](frontend/README.md) · [docs/前端界面清单.md](docs/前端界面清单.md) |
| **后端负责人** | 建 venv → 一键启动 → 跑回归 | [backend/README.md](backend/README.md) · [docs/contract.md](docs/contract.md) · [docs/openapi.json](docs/openapi.json) |
| **调提示词 / 跑评测（工作台）** | 起服务后开 `/lab`（提示词实验室）、`/admin`（配置台）、`/board`（榜单与战报） | [docs/工作台-README.md](docs/%E5%B7%A5%E4%BD%9C%E5%8F%B0-README.md) · [docs/存档与度量口径.md](docs/存档与度量口径.md) |

一张联接清单（写排期时直接贴）：**[docs/入口链接.md](docs/入口链接.md)**。

## 页面一览

| 页面（线上 URL） | 源码 | 用途 |
|---|---|---|
| `/play` | [frontend/play.html](frontend/play.html) | 对局页（根路径直落此页）：AI 发言播报、按住说话、夜间面板、投票 |
| `/lab` | [frontend/promptlab.html](frontend/promptlab.html) | 提示词实验室：快照 / 回退 / 重跑 / 线上契约编辑 |
| `/admin` | [frontend/admin.html](frontend/admin.html) | 配置台：key / 通道 / 音色 / prompt |
| `/board` | [frontend/board.html](frontend/board.html) | 榜单与战报（只读，随便刷新） |
| `/dev_panels.html` | [frontend/dev_panels.html](frontend/dev_panels.html) | **前端面板画廊**：不开后端、不跑对局，一页看全部 UI 状态 |
| `/judge_voice.js` | [frontend/judge_voice.js](frontend/judge_voice.js) | 法官播报的本地合成通道（Web Speech） |

> ⚠️ 本站静态资源是**一个文件一条路由**（不是 StaticFiles 挂目录）：
> 前端**新增或拆分 js/html 时，必须同时在 `backend/server.py` 补一条路由**，
> 否则浏览器 404、页面静默少一块。补完请跑一次下面的自检。

## 前端怎么能完全不依赖后端迭代（本周的默认姿势）

```bash
# 1) 起假数据服务（只要标准库 python，不用装依赖）
python frontend/mock/mock_server.py            # :5501

# 2) 一页看全部界面（每一格都是真的 play.html）
#    http://127.0.0.1:5501/dev_panels.html

# 3) 想练交互流转（点号码 → 提交 → 下一屏）
#    http://127.0.0.1:5501/play.html?demo=night_wolf
```

- 每一格都是**真页面 + 真渲染管线**，只有数据来自 [frontend/demo_scenes.js](frontend/demo_scenes.js)
  的内置场景 ⇒ 改 `table.css` / `play.html` / `tableui.js` 后刷新画廊，所有场景一起变，**所见即所得**。
- 加一个新界面 = 在 `demo_scenes.js` 里加一项，画廊**自动生成格子**（忘归类会弹红条提醒）。
- 完整清单与"加页面的规矩"：[docs/前端界面清单.md](docs/前端界面清单.md)。

## 改完之后，跑这个自检（30 秒）

```bash
.venv/Scripts/python.exe tests/_check-split-consistency.py
```

它查六类分离漂移：后端路由引用的静态文件是否真在 `frontend/`、前端调的接口后端有没有、
后端有但前端没接的接口、`docs/openapi.json` 是否跟得上代码、目录归属、mock 服务路径。
**全绿才算改完。**（还没挂 CI，现在是手跑。）

## 项目结构（2026-10-26 前后端分离 · 2026-10-27 收拢）

```
├── start.py            # 一键启动器（自检/清进程/单进程双协议；py 模块在 backend/）
├── backend/            # ★ 后端：全部 Python
│   ├── requirements.txt#   服务端依赖清单（README 的建环境命令指向这里）
│   ├── server.py       #   FastAPI 服务端 + 页面路由 + 语音 + CORS（联调白名单）
│   ├── game_api.py     #   对局 HTTP API
│   ├── engine.py       #   狼人杀规则引擎（确定性逻辑，四层信息隔离）
│   ├── agents.py / ai_behaviors.py    # AI 玩家（LLM + 规则兜底）
│   ├── night_driver.py / night_pre.py / night_script.py   # 夜间流程
│   ├── layers_asr/llm/tts.py          # ASR / LLM / TTS 适配层（可插拔）
│   ├── lab_api.py / admin_api.py      # 提示词实验室 / 配置台
│   ├── run_archive.py  #   对局自动存档（runs/，不进 git）
│   └── metrics.py / experience.py / report.py / summarize.py
├── frontend/           # ★ 前端：页面与静态资源，前端同学只在这动手
│   ├── README.md       #   前端上手（画廊 / mock / 不碰后端的迭代方式）
│   ├── play.html / tableui.js / speech_ui.js / table.js / rec.js / table.css
│   ├── promptlab.html / admin.html / board.html / dev_panels.html
│   ├── judge_voice.js  #   法官本地合成（2026-10-27 从仓库根归位到这里）
│   ├── demo_scenes.js  #   画廊的全场景数据（加界面就加在这里）
│   └── mock/           #   前端联调假服务（fixtures 为实机抓的真实响应），归前端管
├── docs/               # contract.md（接口契约）+ openapi.json + 各角色入口 + 界面清单
├── tests/              # 冒烟测试与自检脚本（_smoke-*.py / _check-split-consistency.py）
└── lab/                # 实验室运行时产物（不进 git）
```

## 已知限制

- 手机外放 + 无 AEC 时，"随时插嘴"检测不可靠（自适应阈值方案已内置，戴耳机最稳）
- 本地 faster-whisper 为 CPU 推理，耗时 ~0.5×实时，仅用于拾音/黑话验证，不是产品延迟指标
- 火山引擎 ASR/TTS 为骨架实现（代码内 TODO 标注）

详细的设计取舍、远场测试方法论与踩坑记录见 [docs/README-P0.md](docs/README-P0.md)。

## 安全提示

`.env`、`*.pem`（证书私钥）、音频文件均已加入 `.gitignore`，**请勿提交或分享含真实 key 的配置**。

## License

[MIT](LICENSE)
