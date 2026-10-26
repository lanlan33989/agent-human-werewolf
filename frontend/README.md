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

## 迭代方式（2026-10-27 定稿：**完全不需要后端**）

```powershell
# 1) 仓库根下，起假数据服务（只要标准库 python，不用建 venv、不用装依赖）
python frontend/mock/mock_server.py            # :5501

# 2) 面板画廊：不开后端、不跑对局，一页看全部界面
#    http://127.0.0.1:5501/dev_panels.html

# 3) 单场景练交互（点号码 → 提交 → 下一屏）
#    http://127.0.0.1:5501/play.html?demo=night_wolf
#    场景名见 frontend/demo_scenes.js
```

三条路的差别：

| 地址 | 数据 | 用来干什么 |
|---|---|---|
| `:5501/dev_panels.html` | 内置场景 | **改样式**：每格都是真 play.html，改完刷新全部场景一起变 |
| `:5501/play.html?demo=<场景>` | 内置场景 | **练单屏交互**：可以点、可以提交（提交后按 next 链自动进下一场景） |
| `:5501/play.html` | mock 假数据 | **跑完整流程**：开局 → 夜间 → 白天 → 终局，全部假数据 |

### 加一个新界面（三步，不用改 gallery）

1. 在 [`demo_scenes.js`](demo_scenes.js) 里加一项 `{title, note, public}`（`public` 照真实 `/public`
   的字段抄，样例在 `mock/fixtures/public.json`）；
2. 刷新画廊 —— 格子**自动生成**；没放进 `dev_panels.html` 的 `GROUPS` 也没关系，
   底部会弹红条提醒你归类；
3. 需要新接口/新字段 ⇒ 先在 [`docs/contract.md`](../docs/contract.md) 登记，再让后端同步到 mock。

> 判断标准：demo 场景走的是**真渲染管线**（`resumeInto → enterTable → tick → renderNightDial`），
> 所以画廊里长什么样，线上就是什么样；反过来，如果某屏只在 demo 里出现而真机没有，
> 那是数据抄错了，不是画面对了。

## 以前的做法（已废弃，别再用）

- ~~前端用 Live Server 开 :5500 再跨端口打 :5501~~ —— mock 服务现在**同端口吐页面**，
  一个 `:5501` 全解决，不再需要第二个服务、也不再需要加 CORS 白名单。
- mock 服务已经从仓库根的 `mock/` 挪到 `frontend/mock/`（归前端管），路径别再按旧位置写。

## 已知坑（都踩过，别再踩）

1. **新增 js/css/html 必须在 `backend/server.py` 补一条路由**——本站静态资源是一文件一路由（不是 StaticFiles 挂目录），不补路由浏览器 404、页面静默少一块（`judge_voice.js` 就这么失联过一次，2026-10-27 修）；
2. **demo 场景的数据必须抄真值**：`phase` 结束是小写 `ended`、`winner` 是 `wolf`/`good` —— 写成大写或整句，局末那排出口（本局战报/导出复盘）会**悄悄不出现**；
3. `renderWheelAct` 里夜间与投票已被统一短路到**拨号盘**（`renderNightDial`），白天发言才是大圆；女巫 ✓✕ 大圆、狼队共屏那两段现在是死代码，别照着它们设计；
4. **静态资源一律禁缓存**（server 端 `no-store`）：改了没生效先怀疑缓存；
5. 手机端麦克风必须 **HTTPS**（`start.py` 会自动生成自签证书并双协议启动）；
6. 刷新/重开**永远落回封面**（"继续上一局"按钮会自己出现）——这是 2026-10-01 用户定的，不许改回自动进局。

## 本冲刺（10/27–11/7）前端任务落点

- **好看 + 大按钮 + AI 表情**：改 `tableui.js`（表情挂点）与 `table.css`；验收标准是汇报评委看着舒服、真人玩家不用凑近就能按到
- 11/2 起冻结新界面功能，之后只配合联调（状态同步、表情字段接线 —— 协议变化走 `docs/contract.md` 规矩）
