# frontend/ —— 主产品前端（桌面中央手机 H5）

> GitHub：https://github.com/lanlan33989/agent-human-werewolf/tree/main/frontend
> 接口契约（先读这份再动代码）：https://github.com/lanlan33989/agent-human-werewolf/blob/main/docs/contract.md
> 工作台（提示词实验室）前端也在本目录：`promptlab.html`，详见 `docs/lab-README.md`

## 一句话

一个竖屏单页：手机放桌子中央，真人抬头就能看到椭圆桌上的 AI 玩家发言/投票/出局，
需要操作时（发言/投票/用技能）**伸手就能按到**。没有框架、没有构建——纯 HTML/JS/CSS，
浏览器直接跑，改完刷新即生效。

## 文件地图

| 文件 | 职责 |
|---|---|
| `play.html` | 对局页（根路径直落）：封面（人数/档位/性格/规则开关/key 填写）→ 身份牌 → 牌桌。单文件状态机，页面切换全靠 `.page.on` |
| `tableui.js` | 牌桌渲染状态机：大圆（桌面主视觉）/ 座位小圆 / 行动光标 / 票型大字 / AI 表情挂点（**表情动画加在这**） |
| `speech_ui.js` | 播报队列 + 夜间门控：谁的话什么时候念出来、夜间面板什么时候允许弹（**弹面板只听服务端 `now.started`**，不许前端自己提前） |
| `rec.js` | 录音共享模块（按住说话 → 16k WAV → `/api/asr`）；采样率换算只在这里做一次 |
| `table.js` / `table.css` | 椭圆排座/座位特效/样式（从 demo 提取，未动） |
| `promptlab.html` | 提示词实验室（三列：席位 / 线上契约 / 直播回放）—— 见 `docs/lab-README.md` |
| `admin.html` | 配置台（key / 通道 / 音色 / prompt，服务端 `/api/admin/*` 渲染） |
| `board.html` | 榜单与战报（只读，随便刷新） |

## 数据流（唯一强耦合点）

前端**每 1~1.5s 轮询 `GET /api/game/{gid}/public`** 驱动一切：

- `timeline[]` → 播报队列（speech_ui.js 排队念）
- `now` → 夜间面板/轮盘/倒计时（`started/until_left/panel_line` 语义见契约）
- `pending` → 谁该操作（`human_say` / `human_vote` / `working`=AI 在算）
- 人类操作 → `POST /api/game/{gid}/act`（action 全枚举见契约）

**前端只渲染，不做裁决**：倒计时到 0 不自己判超时、面板不自己弹 —— 全听服务端。

## 联调方式（不碰后端也能干活）

```powershell
# 仓库根下：起假服务（实机抓的真实响应）
python mock/mock_server.py --port 5501
# 前端用 Live Server 开 frontend/play.html（:5500），请求指向 :5501 或真后端 :8000
```

- 后端已开 CORS 白名单（`backend/server.py` 的 CORSMiddleware，新端口去那里加）
- 想演练异常序列（夜转昼 / `started` 翻转 / `until_left` 回退）：直接改 `mock/fixtures/*.json`
- 语音链路最后两天再接真（`VOICE_ON` / 云 TTS），开发期静音

## 已知坑（都踩过，别再踩）

1. **新增 js/css/html 必须在 `backend/server.py` 补一条路由**——本站静态资源是一文件一路由（不是 StaticFiles 挂目录），不补路由浏览器 404、页面静默少一块；
2. **静态资源一律禁缓存**（server 端 `no-store`）：改了没生效先怀疑缓存；
3. 手机端麦克风必须 **HTTPS**（`start.py` 会自动生成自签证书并双协议启动）；
4. 刷新/重开**永远落回封面**（"继续上一局"按钮会自己出现）——这是 2026-10-01 用户定的，不许改回自动进局。

## 本冲刺（10/27–11/7）前端任务落点

- **好看 + 大按钮 + AI 表情**：改 `tableui.js`（表情挂点）与 `table.css`；验收标准是汇报评委看着舒服、真人玩家不用凑近就能按到
- 11/2 起冻结新界面功能，之后只配合联调（状态同步、表情字段接线 —— 协议变化走 `docs/contract.md` 规矩）
