# -*- coding: utf-8 -*-
"""
配置台（`/admin` + `/api/admin/*`）—— 用户要的「随时改 key（ASR / LLM / 音色）
+ 看得到所有 prompt 并修改」。

## 为什么单独一个文件

`server.py` 加完这些会到 1800+ 行，而 §2.1.4 锁着的拆分裂线是 1600 行
（超了 `_smoke-map.py` 就红）。这个文件的职责也确实独立：**运行时配置怎么读写**
vs「语音链路怎么走」，不是一回事。

## 依赖方向：**注入，不 import server**

`_smoke-map.py` 会查「依赖图无环」（下层不许反过来依赖上层）。这个模块**不能**
`import server` —— 那会成 `admin_api <-> server` 的环（第一次就是这么写的，
当场被自检拦下来了，拦得对）。所以三件事全靠 `register()` 注入：

    snapshot_fn() → 当前装配状态（哪个实现、是不是 mock）
    reload_fn()   → 按当前 env 重新装配三层
    page_fn()     → 取 /admin 那个页面的响应（server 的 _page，带 NO_STORE）

本模块**只**依赖 `os/json` + `fastapi` + 惰性 `agents`（读改 prompt 模板）。

## 三条安全底线（不是可选项，改之前先读完）

1. **密钥只进不出**：GET 只回指纹（`sk-…12ab`）。
   和「key 只能待在服务端」是同一条原则 —— 面板不该成为例外。
2. **默认只有本机能用**：来源 127.0.0.1 / ::1 免口令；其它来源必须带
   `X-Admin-Token` 且等于 env 里的 `ADMIN_TOKEN`。**没设 ADMIN_TOKEN ⇒ 外部一律拒**
   （不搞"默认密码" —— 那等于把改 key 的权限挂到公网上）。
3. **只改白名单键**：`ADMIN_ENV_KEYS` 之外一律 400，值还要过枚举/类型校验。
"""
import importlib
import json
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

# ---------------------------------------------------------------- 白名单与配置
# 允许在面板上改的 env 键（不在这里的键 POST 一律 400）
ADMIN_ENV_KEYS = (
    "ASR", "ASR_MODEL", "ASR_MODEL_PATH", "ASR_API_KEY", "DASHSCOPE_ASR_MODEL",
    "TTS", "DASHSCOPE_TTS_VOICE", "DASHSCOPE_TTS_MODEL", "DASHSCOPE_TTS_INSTRUCT",
    "SAPI_VOICE", "SAPI_RATE",
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_MODELS",
    "DASHSCOPE_API_KEY", "DASHSCOPE_BASE_URL", "DASHSCOPE_TIMEOUT",
    "SUMMARY_MODEL", "SUMMARY_BASE_URL", "SUMMARY_API_KEY",
    "VOLC_ASR_APPID", "VOLC_ASR_TOKEN", "VOLC_ASR_CLUSTER",
    "VOLC_TTS_APPID", "VOLC_TTS_TOKEN", "VOLC_TTS_VOICE",
    "MOCK", "MOCK_ASR", "MOCK_LLM", "MOCK_TTS",
)
# 密钥类：**只进不出**（GET 只给指纹）
ADMIN_SECRET_KEYS = tuple(k for k in ADMIN_ENV_KEYS if ("KEY" in k or "TOKEN" in k))

# ---------------------------------------------------------------- prompt 全量清单
# 🔴 2026-10-01 修：**第一版只放了 4 个字符串**（SYSTEM_TEMPLATE / _STYLE_COMMON /
#    ANTI_INJECTION / ANTI_BOMB），用户当场指出「并不是所有 prompt，职业、赌注、性格
#    等等你都没加」。他说得对 —— 真正喂给模型的东西还有一大摞：
#      · `TASKS`（8 句动作话）        · `LEVELS[*].stake`（四档**赌注**措辞）
#      · `PERSONALITIES[*].label/brief`（九种**性格**） · `VOICE_BY_PERSONALITY`（音色）
#      · `summarize.PROMPT`（上下文压缩） · `JUDGE_VOICE / DEFAULT_VOICE`
#    ⇒ 改成**清单驱动**，而且表里的条目**动态展开**：
#       表里加一条（比如新增一个性格），面板自动多一项，**不需要改这个文件**。
#       这正是上一版漏掉东西的根因（写死的 4 个名字，谁加了新 prompt 都不会被发现）。
#
# 三种条目：
#   STATIC  —— 模块级字符串（改它用 setattr）
#   TABLE   —— 表里的叶子（改它**原地改字典** ⇒ 下一局立刻生效，不用重启也不用 reload）
#   JSON    —— 整张 dict[str,str] 表（用来**加/删条目**，要过 JSON 校验）
PROMPT_STATIC = (
    # （模块, 属性, 分组, 说明）
    ("agents", "SYSTEM_TEMPLATE", "系统与风格",
     "主模板。{seat}/{role_label}/{personality}/{stake}/{experiences}/{anti_injection}/"
     "{anti_bomb}/{style_rules}/{private} 是占位符，**别删**"),
    ("agents", "_STYLE_COMMON", "系统与风格",
     "说话要求（公共部分）。{style} 会被替换成血腥/温和那行"),
    ("agents", "_STYLE_TAME", "系统与风格", "说话要求·非血腥档"),
    ("agents", "_STYLE_GORE", "系统与风格", "说话要求·血腥档（档位 gore=true 时用）"),
    ("agents", "ANTI_INJECTION", "安全与防注入", "防伪造「主持人消息」"),
    ("agents", "ANTI_BOMB", "安全与防注入", "防逻辑炸弹（塞数学题占时间）"),
    ("agents", "WOLF_TERMS", "术语与打法",
     "桌上通用的短说法（金水/查杀/对跳/归票/分票/抗推/倒钩…）；不含警长系 —— 我们板子没有警长"),
    ("agents", "FACT_ONE_ROLE", "系统与风格",
     "硬事实：每人一个固定身份（防「不止一张牌」这类幻觉，写进主持人事实区）"),
    ("agents", "FACT_ONE_ROUND", "系统与风格",
     "硬事实：白天每人只有一次发言（被点名回应除外），说完立刻投票 —— 防「想再听听」"),
    ("agents", "SPEECH_PROGRESS_HEAD", "系统与风格",
     "发言进度段标题（✓已说/●正在说/○还没说 —— 防「评论还没发生的发言」）"),
    ("agents", "THOUGHT_SPEC", "战术备忘（thought）",
     "thought 的**范式**（主=我的战术，副=盘面，末句收口）。不给字数上限 —— "
     "用户定：用范式约束顺序而不是字数锁（实测：thought 一放开会写到 3000~10000 "
     "tokens ⇒ 24~50s 甚至超时走兜底，靠范式收住）"),
    ("summarize", "PROMPT", "摘要压缩", "公开发言超 15 条时的压缩提示词（没配 SUMMARY_MODEL 就不走它）"),
    ("agents", "JUDGE_VOICE", "音色", "法官音色（**只有云端 TTS 生效**，本地 SAPI 忽略）"),
    ("agents", "DEFAULT_VOICE", "音色", "没配人格的 AI 座位兜底音色"),
    ("agents", "EXPERIENCE_HEAD", "身份 / 职业（私有信息）",
     "跨局经验块的标题（G3，只有开了经验的档位才会出现这段）"),
    ("agents", "VOTE_PATTERN_HEAD", "投票票型",
     "行动前注入的票型段标题（含『拿票型对账言行』的指引；2026-10-02 加）"),
    # 🔴 2026-10-26 提示词契约收尾：build_messages 里剩余的段落标题/收尾
    #    从 f-string 内联抽成模块级常量（逐字原样，行为零变化），登记到这里。
    #    段落里的数据（发言记录/票型/立场卡）由引擎填，不在契约范围。
    ("agents", "HEAD_BOARD_FACT", "段落标题（user 消息）", "本局配置段标题"),
    ("agents", "HEAD_PUBLIC_LOG", "段落标题（user 消息）", "主持人播报段标题"),
    ("agents", "HEAD_HISTORY", "段落标题（user 消息）", "公开发言记录段标题"),
    ("agents", "HEAD_STANCES_HIST", "段落标题（user 消息）", "立场卡·全量历史段标题"),
    ("agents", "HEAD_STANCES", "段落标题（user 消息）", "立场卡·兜底段标题"),
    ("agents", "TAIL_STANCES", "段落标题（user 消息）", "立场卡·兜底段收尾（声明出处提醒）"),
    ("agents", "HEAD_MY_TACTICS", "段落标题（user 消息）", "私有战术备忘段标题"),
    ("agents", "HEAD_MY_PLAN", "段落标题（user 消息）", "夜末预案段标题"),
    ("agents", "HEAD_WHY_TURN", "段落标题（user 消息）", "「为什么轮到你」段标题（挑战式发言）"),
    ("agents", "TAIL_WHY_TURN", "段落标题（user 消息）", "「为什么轮到你」段收尾（回应提醒）"),
    ("agents", "HEAD_WOLF_TERMS", "段落标题（user 消息）", "桌上通用说法段标题"),
    ("agents", "HEAD_PLAYBOOK", "段落标题（user 消息）", "身份常规打法段标题"),
    ("agents", "HEAD_TASK", "段落标题（user 消息）", "任务段标题"),
    ("agents", "SPEAK_RULES_REMINDER", "任务文案（speak）",
     "发言任务末尾的【全场规则提醒】整段；{round} 是轮数占位符，**别删**"),
)

# （模块, 表名, 分组, 说明, 叶子名元组（None = 表本身就是 dict[str,str]））
PROMPT_TABLES = (
    ("agents", "TASKS", "动作句", "每个动作对 AI 说的那句话", None),
    ("agents", "VOICE_BY_PERSONALITY", "音色", "人格 → 音色名（云端 TTS 用）", None),
    ("agents", "LEVELS", "档位 / 赌注", "stake=写进 prompt 的赌注措辞；desc=界面上的档位说明",
     ("stake", "desc")),
    ("agents", "PERSONALITIES", "性格",
     "label=性格名（也会进 prompt）；brief=性格描述（写进 prompt 的那句）",
     ("label", "brief")),
    # 2026-10-01：用户指出「**职业**你也没加」—— 说的就是这一张。
    # 那些句子（狼队友是谁、你必须伪装、狼队此刻的点、你验过谁、你的药、你守过谁、
    # 你翻过牌、今晚谁被刀、你说过什么/投过谁）原来**硬编码在 private_block() 里**，
    # 面板看不见、守门也扫不到（守门只扫模块级）。已抽成这张表。
    ("agents", "PRIVATE_LINES", "身份 / 职业（私有信息）",
     "**这个身份知道什么**的措辞（狼队友 / 伪装要求 / 狼队的刀 / 验人结果 / 药 / 守人 / "
     "翻牌 / 今晚谁被刀 / 我自己说过与投过）。带 {占位符} 的是模板，改占位符名字要小心",
     None),
)
# 可以整张 JSON 编辑的表（用来**加/删条目**）：必须 dict[str,str]
PROMPT_JSON_TABLES = ("TASKS", "VOICE_BY_PERSONALITY")
# 相关但**故意不给改**的（面板里只读显示 + 写明原因）——
# 少了这一块，用户看到的就是"你还有东西没放出来"，而分不清是漏了还是有意
PROMPT_READONLY = (
    ("agents", "LEAK_PATTERNS", "泄露检测正则：它是**守卫**，可改等于把守卫拆了"),
    ("agents", "DEFAULT_LEVEL", "默认档位名（不传档位时的兜底，改它会影响所有调用方与测试）"),
    ("agents", "_CONTROL_KEYS", "解析模型输出用的控制键，不是 prompt"),
    ("agents", "_THOUGHT_HEAD", "thought 段的甄别正则片段（2026-10-05）：它是防泄守卫的一半，放开可改等于允许把闸门改松"),
    ("agents", "_CLAIM_ALLOWED", "允许的身份宣称表，属于规则不是措辞"),
    ("agents", "PERSONALITY_KEYS", "从 PERSONALITIES 自动派生，改上面那张表即可"),
    ("engine", "_GOD_SHORT", "神职简称（界面显示用）"),
    ("report", "_CAUSE_CN", "死因中文（战报用）"),
)

# 🔴 2026-10-01 补：**函数内部**硬编码的、同样进 prompt 的句子。
#    它们是用户那句「并非所有 prompt」的最后一层 —— 我的第一版守门**只扫模块级常量**，
#    扫不到函数体内 ⇒ 这批句子既看不见、也没人拦。
#    这里按"句子开头"登记 + **写明为什么只读**；守门（_smoke-admin.py）会扫函数体，
#    任何 ≥30 字的字符串如果**不**以这些前缀开头，就报红（逼你抽成表或写清原因）。
PROMPT_FUNC_LITERALS = (
    ("（也可以只回一行 JSON：",
     "控制协议的格式样例：必须与解析器 `_CONTROL_JSON` / `_CONTROL_KEYS` 严丝合缝，"
     "改了提示却没改解析器 ⇒ 模型按新格式回、解析器认不出（**只读是故意的**）"),
    ("（这是回应，不是新一轮自由发言",
     "回应式发言的补充说明（写在 `build_messages` 的 f-string 里）：要改得动函数结构，"
     "属于「改了不如不改」的那一类"),
    ("不(救|用|毒|要|需要|必)",
     "否定词正则（解析模型回答用），**不是 prompt**"),
    ("### 你昨晚天亮前写下的战术预案",
     "夜末预案的**注入标题**（build_messages 里随 view[\"my_plan\"] 动态生成）⇒ 只读"),
    ("hunter_can_shoot_on_witch_poison",
     "视图规则开关的**字典键名**（build_messages 里取开关用的 key），不是 prompt 文本"),
    ("猎人被毒杀",
     "本局规则行（build_messages 里随 view[\"rules\"] 开关**动态生成**）："
     "三个开关怎么开它就怎么拼，抽成常量反而没有意义 ⇒ 只读"),
    ("不毒|不用|不救|不投",
     "否定词正则（解析模型回答用），**不是 prompt**"),
    ("格式：输出里带了 <thought> 私有思维块",
     "sanitize_speech 的**格式问题记录**（回给日志/前端看，不进模型 prompt）⇒ 只读"),
    ("格式：出现 thought 私有盘算",
     "同上：sanitize_speech 的格式问题记录，**不进模型 prompt** ⇒ 只读"),
)

# 这几个必须是数字
NUMERIC_KEYS = ("SAPI_RATE", "DASHSCOPE_TIMEOUT", "DASHSCOPE_TTS_TIMEOUT",
                "DASHSCOPE_TTS_RETRIES")
ASR_KINDS = ("qwen", "faster-whisper", "faster_whisper", "whisper", "local",
             "doubao", "mock")
TTS_KINDS = ("qwen", "dashscope", "bailian", "aliyun", "sapi", "local", "windows",
             "doubao", "mock")

STATE_NAME = "_runtime.json"
STATE_PATH = None            # 由 apply_runtime_state(base) 设定
_reload_fn = None            # 下面三个由 register() 注入
_snapshot_fn = None
_page_fn = None


# ---------------------------------------------------------------- 运行时覆盖
def apply_runtime_state(base) -> dict:
    """把 `_runtime.json` 覆盖进 os.environ 与 prompt 模块属性。

    ⚠️ **必须在 server 的 `build_pipeline()` 之前调用** —— 否则装配出来的还是
    `.env` 那套，用户会看到"我改了、面板也认了，但一点变化都没有"。
    """
    global STATE_PATH
    STATE_PATH = base / STATE_NAME
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        print("[admin] _runtime.json 读不动（" + type(e).__name__ + ": " + str(e)
              + "），忽略", flush=True)
        return {}
    n = 0
    env = data.get("env") or {}
    for k, v in env.items():
        if k in ADMIN_ENV_KEYS and isinstance(v, (str, int, float)):
            os.environ[k] = str(v)
            n += 1
    prompts = data.get("prompts") or {}
    if prompts:
        for key, val in prompts.items():
            try:
                it = _find_item(key)
                if not it:
                    continue                                  # 旧版本写下的键：忽略
                _write_path([it["module"]] + it["path"],
                            json.loads(val) if it["kind"] == "json" else str(val))
                n += 1
            except Exception as e:                            # noqa: BLE001
                print("[admin] prompt 覆盖没应用（" + key + "：" + type(e).__name__ + "）",
                      flush=True)
    if n:
        print("[admin] 已应用 _runtime.json 的覆盖（优先于 .env）："
              + ", ".join(sorted(list(env.keys()) + list(prompts.keys()))), flush=True)
    return {"env": env, "prompts": prompts}


def _state() -> dict:
    if not STATE_PATH or not STATE_PATH.exists():
        return {"env": {}, "prompts": {}}
    try:
        d = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return {"env": {}, "prompts": {}}
    d.setdefault("env", {})
    d.setdefault("prompts", {})
    return d


def _write_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ---------------------------------------------------------------- 口令与指纹
def fingerprint(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return ""
    if len(s) <= 10:
        return "*" * len(s)
    return s[:3] + "…" + s[-4:]


def _is_local(request: Request) -> bool:
    try:
        host = (request.client.host or "") if request.client else ""
    except Exception:                                         # noqa: BLE001
        host = ""
    # testclient：fastapi 测试客户端的默认来源，测试要能免口令跑
    return host in ("127.0.0.1", "::1", "localhost", "testclient")


# ---------------------------------------------------------------- 主产品开始界面：三类 key
# 🔴 2026-10-26（用户要求）：/play 封面支持填写 ASR / LLM / TTS 的 key，
#    默认回填当前已配置的值。写入即热替换（os.environ + _runtime.json + reload），
#    进行中的对局不受影响，下一局生效。
#    ⚠️ 安全边界：明文**只回给本机请求**（服务可能开给局域网手机，key 不能广播）；
#    非本机只给指纹。这与配置台「密钥只进不出」的原则一致，只是范围放宽到本机页面。
PLAY_KEYS = (("asr", "ASR_API_KEY", "DASHSCOPE_API_KEY"),
             ("llm", "LLM_API_KEY", None),
             ("tts", "DASHSCOPE_API_KEY", None))


def api_play_keys_get(request: Request):
    local = _is_local(request)
    out = {}
    for name, env, fallback in PLAY_KEYS:
        v = os.environ.get(env, "")
        if not v and fallback:
            v = os.environ.get(fallback, "")
        out[name] = v if local else fingerprint(v)
    return out


async def api_play_keys_set(request: Request):
    try:
        body = await request.json()
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(status_code=400, detail="body 必须是 JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body 必须是对象")
    st = _state()
    changed = []
    for name, env, _fb in PLAY_KEYS:
        v = str(body.get(name) or "").strip()
        if not v or env not in ADMIN_ENV_KEYS:
            continue
        os.environ[env] = v
        st.setdefault("env", {})[env] = v
        changed.append(env)
    if not changed:
        return {"ok": True, "changed": [], "note": "没有要更新的 key"}
    _write_state(st)
    note = "进行中的对局不受影响，下一局生效"
    if _reload_fn:
        try:
            _reload_fn()
            note = "已热替换模型通道（进行中的对局不受影响，下一局生效）"
        except Exception as e:                               # noqa: BLE001
            note = "key 已保存，但热替换失败（重启服务后生效）：" + repr(e)
    print("[admin] 主产品封面更新 key：" + ", ".join(changed), flush=True)
    return {"ok": True, "changed": changed, "note": note}


def _allowed(request: Request) -> bool:
    if _is_local(request):
        return True
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not token:
        return False
    return request.headers.get("X-Admin-Token", "") == token


def _guard(request: Request) -> Optional[JSONResponse]:
    """不通过返回 403 响应；通过返回 None。"""
    if _allowed(request):
        return None
    return JSONResponse({"error": "需要管理员口令：在服务端 .env 里设 ADMIN_TOKEN，"
                                 "请求头带 X-Admin-Token"},
                        status_code=403)


# ---------------------------------------------------------------- prompt 读写
def _catalog() -> list:
    """按**当前代码现算**可编辑项。表里加一条（例如新增一个性格），这里自动多一项 ——
    不写死名字，所以不会再出现"加了新 prompt 但面板上根本没有"的情况。"""
    items = []
    for mod, attr, group, note in PROMPT_STATIC:
        items.append({"key": mod + ":" + attr, "module": mod, "path": [attr],
                      "group": group, "note": note, "kind": "str"})
    for mod, table, group, note, leaves in PROMPT_TABLES:
        try:
            d = getattr(importlib.import_module(mod), table)
        except Exception:                                    # noqa: BLE001
            continue
        if leaves is None:
            for k in d:
                items.append({"key": mod + ":" + table + "." + str(k), "module": mod,
                              "path": [table, k], "group": group, "note": note, "kind": "str"})
            if table in PROMPT_JSON_TABLES:
                items.append({"key": mod + ":" + table, "module": mod, "path": [table],
                              "group": group, "note": "整张表（JSON）—— 用来**加 / 删条目**",
                              "kind": "json"})
        else:
            for k, spec in d.items():
                for leaf in leaves:
                    if leaf in spec:
                        items.append({"key": mod + ":" + table + "." + str(k) + "." + leaf,
                                      "module": mod, "path": [table, k, leaf],
                                      "group": group, "note": note, "kind": "str"})
    return items


def _read_path(path: list):
    cur = getattr(importlib.import_module(path[0]), path[1])
    for p in path[2:]:
        cur = cur[p]
    return cur


def _write_path(path: list, value) -> None:
    """写回。顶层字符串用 setattr；表里的叶子**原地改字典** ——
    代码是在**调用时**读这些表的，所以原地改就对下一局生效（不必重启，也不必 reload）。"""
    mod = importlib.import_module(path[0])
    if len(path) == 2:
        setattr(mod, path[1], value)
        return
    cur = getattr(mod, path[1])
    for p in path[2:-1]:
        cur = cur[p]
    cur[path[-1]] = value


def _find_item(key: str):
    for it in _catalog():
        if it["key"] == key:
            return it
    return None


def _item_value(it: dict) -> str:
    v = _read_path([it["module"]] + it["path"])
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- 路由
def register(app: FastAPI, *, reload_fn, snapshot_fn, page_fn) -> None:
    """把配置台挂到 app 上（由 server.py 在文件末尾调用一次）。

    ⚠️ 三件事都靠**注入**，本模块不 import server（会成环，见文件头）。
    """
    global _reload_fn, _snapshot_fn, _page_fn
    _reload_fn, _snapshot_fn, _page_fn = reload_fn, snapshot_fn, page_fn

    # 主产品开始界面（/play 封面）的三类 key：本机明文回填 + 热替换
    @app.get("/api/admin/play-keys")
    def _pk_get(request: Request):
        return api_play_keys_get(request)

    @app.post("/api/admin/play-keys")
    async def _pk_set(request: Request):
        return await api_play_keys_set(request)

    @app.get("/api/admin/config")
    def admin_config(request: Request) -> JSONResponse:
        g = _guard(request)
        if g is not None:
            return g
        st = _state()
        env_view = {}
        for k in ADMIN_ENV_KEYS:
            v = os.environ.get(k, "")
            env_view[k] = fingerprint(v) if k in ADMIN_SECRET_KEYS else v
        return JSONResponse({
            "env": env_view,
            "secrets": list(ADMIN_SECRET_KEYS),
            "writable": list(ADMIN_ENV_KEYS),
            "numeric": list(NUMERIC_KEYS),
            "overridden": sorted(st["env"].keys()),   # 被 _runtime.json 覆盖的键
            "prompt_count": len(_catalog()),        # 可改的 prompt 项数（面板显示规模用）
            "state_file": STATE_NAME,
            "live": _snapshot_fn(),
            "local": _is_local(request),
            "has_token": bool(os.environ.get("ADMIN_TOKEN", "").strip()),
            "note": "密钥只回指纹（只进不出）；改完 key / 通道 / 音色要 "
                    "POST /api/admin/reload 才生效",
        })

    @app.post("/api/admin/config")
    async def admin_set_config(request: Request) -> JSONResponse:
        g = _guard(request)
        if g is not None:
            return g
        try:
            body = await request.json()
        except Exception:                                    # noqa: BLE001
            return JSONResponse({"error": "body 必须是 JSON"}, status_code=400)
        patch = (body or {}).get("env") or {}
        if not isinstance(patch, dict) or not patch:
            return JSONResponse({"error": "env 必须是非空对象"}, status_code=400)
        bad = sorted(k for k in patch if k not in ADMIN_ENV_KEYS)
        if bad:
            return JSONResponse({"error": "不许改这些键：" + ", ".join(bad[:6])},
                                status_code=400)
        for k, v in patch.items():
            s = str(v)
            if k == "ASR" and s.lower() not in ASR_KINDS:
                return JSONResponse({"error": "ASR 只能是 qwen / faster-whisper / "
                                             "doubao / mock"}, status_code=400)
            if k == "TTS" and s.lower() not in TTS_KINDS:
                return JSONResponse({"error": "TTS 只能是 qwen / sapi / doubao / mock"},
                                    status_code=400)
            if k in NUMERIC_KEYS:
                try:
                    float(s)
                except Exception:                            # noqa: BLE001
                    return JSONResponse({"error": k + " 必须是数字"}, status_code=400)
        st = _state()
        st["env"].update({k: str(v) for k, v in patch.items()})
        _write_state(st)
        for k, v in patch.items():
            os.environ[k] = str(v)                           # 立刻进环境（reload 用得到）
        print("[admin] 改了 " + ", ".join(sorted(patch.keys())) + "（已落盘 "
              + STATE_NAME + "；key / 通道 / 音色需 reload 才生效）", flush=True)
        return JSONResponse({"ok": True, "saved": sorted(patch.keys()),
                             "need_reload": True,
                             "hint": "POST /api/admin/reload 让它生效"})

    @app.get("/api/admin/prompts")
    def admin_prompts(request: Request) -> JSONResponse:
        g = _guard(request)
        if g is not None:
            return g
        groups, seen = [], {}
        for it in _catalog():
            name = it["group"]
            if name not in seen:
                seen[name] = {"name": name, "items": []}
                groups.append(seen[name])
            seen[name]["items"].append({"key": it["key"], "note": it["note"],
                                        "kind": it["kind"], "value": _item_value(it)})
        ro = []
        for mod, attr, why in PROMPT_READONLY:
            name = mod + "." + attr
            try:
                v = getattr(importlib.import_module(mod), attr)
            except Exception as e:                            # noqa: BLE001
                # ⚠️ **不许静默跳过**：读不到就写清"读不到"，否则面板上会少一项，
                #    而少的那一项看不出来（这正是"你还有东西没放出来"的来源）。
                ro.append({"name": name, "why": why,
                           "value": "（读不到：" + type(e).__name__ + "）"})
                continue
            try:
                text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            except Exception:                                 # noqa: BLE001
                # 例如 LEAK_PATTERNS 里是 re.Pattern：JSON 序列化不了，
                # 但**必须显示出来**（用 repr）—— 曾经这里 `except: pass`，
                # 结果那一项在面板上凭空消失（2026-10-01 实测发现）。
                text = repr(v)
            ro.append({"name": name, "why": why, "value": text[:300]})
        # 函数体内的句子（同样进 prompt，但故意只读）：把**实际扫到的**附在只读区末尾，
        # 这样面板上"看见的东西"与"守门扫到的东西"是同一份，不会一边多一边少。
        for pre, why in PROMPT_FUNC_LITERALS:
            ro.append({"name": "（函数内）" + pre[:16] + "…", "why": why, "value": ""})
        return JSONResponse({
            "groups": groups,
            "readonly": ro,
            "count": sum(len(gp["items"]) for gp in groups),
            "note": "改 prompt **立刻生效**（下一局开始用；进行中的局不受影响）。"
                    "表里的条目是**动态展开**的 —— 代码里加了新措辞，这里自动多一项。",
        })

    @app.post("/api/admin/prompts")
    async def admin_set_prompts(request: Request) -> JSONResponse:
        g = _guard(request)
        if g is not None:
            return g
        try:
            body = await request.json()
        except Exception:                                    # noqa: BLE001
            return JSONResponse({"error": "body 必须是 JSON"}, status_code=400)
        # 两种形状都收：{"items":[{"key","value"}]} 或 {"prompts":{key:value}}
        patch = (body or {}).get("items")
        if patch is None:
            pd = (body or {}).get("prompts")
            if isinstance(pd, dict) and pd:
                patch = [{"key": k, "value": v} for k, v in pd.items()]
        if not isinstance(patch, list) or not patch:
            return JSONResponse({"error": "items 必须是非空数组（或 prompts 是非空对象）"},
                                status_code=400)
        applied, saved = [], {}
        for raw in patch:
            key = str((raw or {}).get("key") or "")
            val = (raw or {}).get("value")
            it = _find_item(key)
            if not it:
                return JSONResponse({"error": "不在可改清单里：" + key[:70]},
                                    status_code=400)
            if it["kind"] == "json":
                if not isinstance(val, str):
                    return JSONResponse({"error": key + " 的值必须是 JSON 字符串"},
                                        status_code=400)
                try:
                    d = json.loads(val)
                except Exception:                            # noqa: BLE001
                    return JSONResponse({"error": key + " 不是合法 JSON"}, status_code=400)
                if not isinstance(d, dict) or any(not isinstance(v, str) for v in d.values()):
                    return JSONResponse({"error": key + " 必须是「字符串→字符串」的 JSON 对象"},
                                        status_code=400)
                _write_path([it["module"]] + it["path"], d)
                val = json.dumps(d, ensure_ascii=False)
            else:
                if not isinstance(val, str):
                    return JSONResponse({"error": key + " 的值必须是字符串"},
                                        status_code=400)
                _write_path([it["module"]] + it["path"], val)   # 立刻生效（下一局开始用）
            applied.append(key)
            saved[key] = val
        st = _state()
        st["prompts"].update(saved)
        _write_state(st)
        print("[admin] 改了 prompt " + str(len(applied)) + " 项：" + ", ".join(applied[:8])
              + (" …" if len(applied) > 8 else "") + "（下一局生效）", flush=True)
        return JSONResponse({"ok": True, "applied": applied, "note": "下一局开始时生效"})

    @app.post("/api/admin/reload")
    def admin_reload(request: Request) -> JSONResponse:
        g = _guard(request)
        if g is not None:
            return g
        try:
            live = _reload_fn()
        except Exception as e:                                # noqa: BLE001
            return JSONResponse({"error": "重新装配失败：" + type(e).__name__ + ": "
                                          + str(e)}, status_code=500)
        print("[admin] 重新装配完成：" + json.dumps(live, ensure_ascii=False), flush=True)
        return JSONResponse({"ok": True, "live": live})

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page():
        """配置台页面**本身不含任何密钥**：密钥只在服务端，页面通过带口令的 API 读写。"""
        return _page_fn()
