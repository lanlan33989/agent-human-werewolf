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
.venv\Scripts\pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # Linux/macOS

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

## 页面一览

| 页面 | 用途 |
|---|---|
| `frontend/play.html` | 对局页面（根路径直落此页）：AI 发言播报（半双工）、按住说话、确认后提交 |
| `board.html` | 榜单与战报 |
| `admin.html` | 配置台 |

## 项目结构（2026-10-26 前后端分离）

```
├── start.py            # 一键启动器（自检/清进程/单进程双协议；py 模块在 backend/）
├── backend/            # ★ 后端：全部 Python
│   ├── server.py       #   FastAPI 服务端 + 页面路由 + 语音 + CORS（联调白名单）
│   ├── game_api.py     #   对局 HTTP API
│   ├── engine.py       #   狼人杀规则引擎（确定性逻辑，四层信息隔离）
│   ├── agents.py / ai_behaviors.py    # AI 玩家（LLM + 规则兜底）
│   ├── night_driver.py / night_pre.py / night_script.py   # 夜间流程
│   ├── layers_asr/llm/tts.py          # ASR / LLM / TTS 适配层（可插拔）
│   ├── lab_api.py / admin_api.py      # 提示词实验室 / 配置台
│   ├── run_archive.py  #   对局自动存档（runs/）
│   └── metrics.py / experience.py / report.py / summarize.py
├── frontend/           # ★ 前端：页面与静态资源（play/promptlab/admin/board + js/css）
├── docs/               # contract.md（接口契约）+ openapi.json + 设计文档
├── mock/               # 前端联调假服务（fixtures 为实机抓的真实响应）
├── runs-sample/        # 对局存档格式说明与脱敏样例（真实存档在 runs/，不进 git）
├── tests/              # 冒烟测试与诊断脚本（tests/_smoke-*.py）
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
