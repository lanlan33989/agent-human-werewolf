# -*- coding: utf-8 -*-
"""
配置台冒烟测试（2026-10-01 加，当天扩过一次）。

它回答五个问题：
  1. **密钥会不会被回传**？（最重要：GET /api/admin/config 里**不许**出现任何真实密钥的值）
  2. 非本机来源**没口令**时是不是真的拒了？
  3. 白名单之外 / 非法值是不是 400？
  4. 改完之后：**热换真的换了吗**、**prompt 真的立刻生效吗**（改完必须能在
     真正拼出来的 prompt 里看到，而不是只改了属性）。
  5. ★ **完整性守门**（2026-10-01 用户指出"并不是所有 prompt，职业、赌注、性格
     等等你都没加"之后加的）：扫代码里所有**承载 prompt 的模块级常量**，
     每一个都必须**要么可改、要么在只读清单里写明原因** —— 谁也不许再悄悄漏掉。

⚠️ 测试**不留副作用**：改过的 env / prompt / `_runtime.json` 全部深拷贝复原。
退出码：0 = 全过；1 = 有失败；2 = 没跑成（环境不具备，例如 import 就失败）。
"""
import sys as _sys
try:                      # 中文 Windows 控制台默认 GBK：测试名里的 ⇒ / emoji 会让整条脚本中途崩
    _sys.stdout.reconfigure(errors="replace")   # 保留控制台原编码（中文照常显示），只把编不了的字符换成 ?
except Exception:         # noqa: BLE001  （老 Python / 被替换过的 stdout）
    pass
import ast
import copy
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根目录
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/

os.environ["MOCK"] = "0"
os.environ["MOCK_ASR"] = "0"
os.environ["MOCK_LLM"] = "0"
os.environ["MOCK_TTS"] = "0"

try:
    import admin_api  # noqa: E402
except Exception as e:  # noqa: BLE001
    print("导入失败（这是**没跑成**，不是通过）：", type(e).__name__, e)
    sys.exit(2)

# 🔴 先清残留，**再** import server：server 启动时会应用 `_runtime.json` 的 prompt
#    覆盖，所以必须在它启动前把毒清掉 —— 否则"中毒后的第一遍"仍然红一次
#    （实测 2026-10-01：清文件放在 import server 之后时，boot 已把标记应用进内存，
#      快照带着毒，复原检查永远差一口气）。
# ⚠️ `admin_api.STATE_PATH` 此刻还是 None（要等 server 启动时的 apply_runtime_state
#    才装配），所以这里按 admin_api 文件自己的位置推算 —— 两种跑法（直接跑 / 地图跑批）
#    的 cwd 都是 h5，但靠 __file__ 推算最稳。
from pathlib import Path  # noqa: E402

# ⚠️ 状态文件在**仓库根**（server.py 的 apply_runtime_state(ROOT)），不是 backend/。
# 2026-10-31 之前这里少了一层 parent：读不到真正的 _runtime.json ⇒ STATE_BEFORE=None，
# 收尾时反而把仓库根那份删掉（用户已填的 key 被测试抹掉）。
STATE = (Path(admin_api.__file__).resolve().parent.parent
         / getattr(admin_api, "STATE_NAME", "_runtime.json"))
STATE_BEFORE = STATE.read_text(encoding="utf-8") if STATE.exists() else None
_TEST_PROMPT_KEYS = ("agents:_STYLE_COMMON", "agents:LEVELS.认真.stake",
                     "agents:PERSONALITIES.bold.brief", "agents:VOICE_BY_PERSONALITY")
if STATE_BEFORE is not None:
    try:
        _st = json.loads(STATE_BEFORE)
        _pr = _st.get("prompts") or {}
        _drop = [k for k in _TEST_PROMPT_KEYS if k in _pr]
        if _drop:
            for k in _drop:
                _pr.pop(k)
            STATE_BEFORE = json.dumps(_st, ensure_ascii=False, indent=1)
            STATE.write_text(STATE_BEFORE, encoding="utf-8")
            print("  （清掉了上次残留在 _runtime.json 里的 " + str(len(_drop))
                  + " 个测试标记 —— 上次运行中途被杀留下的）")
    except Exception as e:  # noqa: BLE001
        print("  （清理残留标记失败，继续：" + type(e).__name__ + "）")

import server  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import agents  # noqa: E402
import summarize  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  OK   " + name)
    else:
        FAIL += 1
        print("  FAIL " + name + ("  <- " + extra if extra else ""))


class Req:
    """伪造一个请求：只用到 .client.host 与 .headers 两处。"""

    def __init__(self, host: str, token: str = "") -> None:
        self.client = types.SimpleNamespace(host=host)
        self.headers = {"X-Admin-Token": token} if token else {}


STATE = admin_api.STATE_PATH   # 清理已在 import server 之前做掉（见文件头）
check("★ 测试推算的状态文件 == server 真正使用的文件（防止再误删仓库根的 _runtime.json）",
      STATE == (Path(admin_api.__file__).resolve().parent.parent / admin_api.STATE_NAME),
      f"{STATE} vs {admin_api.STATE_PATH}")

# ---- 要复原的东西（深拷贝：下面会**原地改**这些表） ----
_env_before = {k: os.environ.get(k) for k in admin_api.ADMIN_ENV_KEYS}
_SNAP_AGENTS = ("TASKS", "LEVELS", "PERSONALITIES", "VOICE_BY_PERSONALITY", "SYSTEM_TEMPLATE",
                "_STYLE_COMMON", "_STYLE_TAME", "_STYLE_GORE", "ANTI_INJECTION", "ANTI_BOMB",
                "JUDGE_VOICE", "DEFAULT_VOICE")
_agents_before = {k: copy.deepcopy(getattr(agents, k, None)) for k in _SNAP_AGENTS}
_summarize_before = summarize.PROMPT
_style_out_before = agents.style_rules("娱乐")
_token_before = os.environ.get("ADMIN_TOKEN", "")

client = TestClient(server.app)

print("\n=== 配置台冒烟测试 ===\n")

# ---------------------------------------------------------------- 1. 密钥只进不出
r = client.get("/api/admin/config")
check("GET /api/admin/config 能拿到（本机免口令）", r.status_code == 200,
      str(r.status_code) + " " + r.text[:120])
body = r.text
j = r.json() if r.status_code == 200 else {}

secrets = list(j.get("secrets") or [])
check("响应里声明了哪些是密钥类字段（面板据此只显示指纹）", len(secrets) >= 3, str(secrets))

# ★ 核心：真实密钥的值**一个都不许出现在响应里**
leaked = [k for k in secrets
          if len((os.environ.get(k) or "").strip()) >= 8 and (os.environ.get(k) or "").strip() in body]
check("★ 真实密钥的值**没有**出现在响应里（只回指纹）", not leaked,
      "泄露了：" + ", ".join(leaked))
check("指纹函数不吐完整值（长值给 `头3…尾4`）",
      admin_api.fingerprint("sk-abcdefghijklmnop").endswith("mnop")
      and "…" in admin_api.fingerprint("sk-abcdefghijklmnop"))
check("指纹函数对短值全打星号", admin_api.fingerprint("abc") == "***")
check("空值给空串（面板上显示为空）", admin_api.fingerprint("") == "")

# ---------------------------------------------------------------- 2. 口令 / 来源
check("本机来源免口令", admin_api._allowed(Req("127.0.0.1")) is True)
check("非本机 + 服务端没设口令 => 拒（不搞默认密码）",
      admin_api._allowed(Req("8.8.8.8")) is False)
os.environ["ADMIN_TOKEN"] = "test-token-12345"
check("非本机 + 口令不符 => 拒", admin_api._allowed(Req("8.8.8.8", "wrong")) is False)
check("非本机 + 口令正确 => 放行",
      admin_api._allowed(Req("8.8.8.8", "test-token-12345")) is True)
r403 = admin_api._guard(Req("8.8.8.8"))
check("被拒时返回 403（而不是悄悄放过去）",
      r403 is not None and r403.status_code == 403)

# ---------------------------------------------------------------- 3. 白名单 / 校验
check("改 env 白名单之外的键 => 400",
      client.post("/api/admin/config", json={"env": {"PATH": "/tmp"}}).status_code == 400)
check("非法 ASR 通道值 => 400",
      client.post("/api/admin/config", json={"env": {"ASR": "gpt-9"}}).status_code == 400)
check("数字类字段给了非数字 => 400",
      client.post("/api/admin/config", json={"env": {"SAPI_RATE": "很快"}}).status_code == 400)
check("改不在清单里的 prompt 键 => 400",
      client.post("/api/admin/prompts",
                  json={"prompts": {"NOT_A_PROMPT": "x"}}).status_code == 400)

# ---------------------------------------------------------------- 4. 改值 / 落盘 / 热换
old_rate = os.environ.get("SAPI_RATE")
r = client.post("/api/admin/config", json={"env": {"SAPI_RATE": "7"}})
check("改一个合法值 => 200", r.status_code == 200, r.text[:120])
check("改了之后 os.environ 立刻变（reload 才读到它）", os.environ.get("SAPI_RATE") == "7")
saved = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
check("改动落盘到 _runtime.json（重启后仍然生效）",
      (saved.get("env") or {}).get("SAPI_RATE") == "7")

old_asr = server.ASR
r = client.post("/api/admin/reload")
check("POST /api/admin/reload => 200", r.status_code == 200, r.text[:160])
check("★ 重新装配后 ASR 是**新对象**（真的换掉了，不是原样返回）", server.ASR is not old_asr)
check("reload 回的值和实际持有的一致",
      r.json().get("live", {}).get("asr") == type(server.ASR).__name__)

# ---------------------------------------------------------------- 5. prompt 全量清单
d = client.get("/api/admin/prompts").json()
keys = {it["key"] for gp in (d.get("groups") or []) for it in gp["items"]}
need = {"agents:SYSTEM_TEMPLATE", "agents:_STYLE_COMMON", "agents:_STYLE_TAME",
        "agents:_STYLE_GORE", "agents:ANTI_INJECTION", "agents:ANTI_BOMB",
        "summarize:PROMPT", "agents:JUDGE_VOICE", "agents:DEFAULT_VOICE",
        "agents:EXPERIENCE_HEAD",
        "agents:LEVELS.娱乐.stake", "agents:LEVELS.地狱.stake", "agents:LEVELS.认真.desc",
        "agents:PERSONALITIES.bold.brief", "agents:PERSONALITIES.aggressive.label",
        "agents:VOICE_BY_PERSONALITY.bold", "agents:TASKS.speak", "agents:TASKS.vote",
        # 2026-10-01：用户点名的「职业」—— 身份/职业私有信息的措辞
        "agents:PRIVATE_LINES.mates_some", "agents:PRIVATE_LINES.wolf_disguise",
        "agents:PRIVATE_LINES.inv_some", "agents:PRIVATE_LINES.potion_head",
        "agents:PRIVATE_LINES.guard_last", "agents:PRIVATE_LINES.flipped",
        "agents:PRIVATE_LINES.night_victim", "agents:PRIVATE_LINES.my_speeches",
        "agents:PRIVATE_LINES.my_votes", "agents:PRIVATE_LINES.title"}
check("★ 全量清单：系统模板 / 风格 / 防注入 / 动作句 / 档位赌注 / 性格 / 音色 / 摘要 / **职业私有** 一个都不少",
      need <= keys, "缺：" + ", ".join(sorted(need - keys)))
check("清单规模合理（>20 项，含动态展开的表）", len(keys) > 20, "只有 " + str(len(keys)) + " 项")
check("只读区列出了**故意不给改**的常量并写了原因",
      len(d.get("readonly") or []) >= 3
      and all(r0.get("why") for r0 in d["readonly"]), json.dumps(d.get("readonly"))[:160])
# ★ 2026-10-01：这里曾经**静默少一项** —— `agents.LEAK_PATTERNS` 里是 `re.Pattern`，
#   `json.dumps` 抛错后被 `except: pass` 吞掉，面板上就凭空少了一行。
#   断言条数必须**等于源码里写的那份**（属性项 + 函数内字面量项）：少一项就红。
check("★ 只读区**不许静默少项**（条数 == 源码里登记的条数）",
      len(d.get("readonly") or []) == len(admin_api.PROMPT_READONLY)
      + len(admin_api.PROMPT_FUNC_LITERALS),
      "HTTP " + str(len(d.get("readonly") or [])) + " vs 源码 "
      + str(len(admin_api.PROMPT_READONLY) + len(admin_api.PROMPT_FUNC_LITERALS)))

# ================================================================
# ★ 完整性守门（用户那句「并不是所有 prompt…你都没加」的直接产物）
#   规则：`agents.py` / `summarize.py` 里**每个 ≥30 字的模块级字符串常量**、
#   以及**每个模块级字典**（prompt 表多半是字典），都必须
#   **要么出现在可改清单里，要么在只读清单里写明原因**。
#   以后谁加了新的 prompt 常量而忘了放出来，这条会当场红 —— 不会再靠人记得。
# ================================================================
def _module_level(path: str):
    """返回 (长字符串常量名集合, 字典常量名集合)。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    strs, dicts = set(), set()
    for node in tree.body:
        name = val = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name, val = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, val = node.target.id, node.value
        if not name:
            continue
        if isinstance(val, ast.Constant) and isinstance(val.value, str) and len(val.value) >= 30:
            strs.add(name)
        elif isinstance(val, (ast.Dict,)):
            dicts.add(name)
    return strs, dicts


_WAIVED_STR = {"LEAK_PATTERNS", "DEFAULT_LEVEL"}      # 非 prompt / 已在只读里写明
covered_attrs = {it[1] for it in admin_api.PROMPT_STATIC}
covered_attrs |= {row[1] for row in admin_api.PROMPT_READONLY}
covered_tables = {t[1] for t in admin_api.PROMPT_TABLES}
covered_tables |= {row[1] for row in admin_api.PROMPT_READONLY}

for mod_file, mod_name in (("agents.py", "agents"), ("summarize.py", "summarize")):
    # 2026-10-26 拆分：源码在 backend/ 下（check 名字仍用原文件名）
    strs, dicts = _module_level(os.path.join("backend", mod_file))
    miss_s = sorted(s for s in strs if s not in covered_attrs and s not in _WAIVED_STR)
    miss_d = sorted(x for x in dicts if x not in covered_tables)
    check("★ 守门：" + mod_file + " 里每个 ≥30 字的模块级文本常量都在清单里（或只读写了原因）",
          not miss_s, "漏了：" + ", ".join(miss_s))
    check("★ 守门：" + mod_file + " 里每个模块级字典都在清单里（或只读写了原因）",
          not miss_d, "漏了：" + ", ".join(miss_d))


def _func_literals(path: str):
    """**函数体内** ≥30 字的字符串（排除文档字符串）。

    为什么单独一层：2026-10-01 用户说「并非所有 prompt」时，我这里只扫了**模块级**常量，
    而 `private_block()` 里那些"你必须在白天伪装成好人"之类的句子全在**函数里** ⇒
    扫不到 = 面板上没有 = 没人拦得到。那一批已经抽成 `PRIVATE_LINES`；
    剩下的（控制协议样例等）必须登记在 `PROMPT_FUNC_LITERALS` 里并写清原因。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    ds = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) \
                and n.body:
            f0 = n.body[0]
            if isinstance(f0, ast.Expr) and isinstance(f0.value, ast.Constant) \
                    and isinstance(f0.value.value, str):
                ds.add(id(f0.value))
    out = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(n):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                    and id(sub) not in ds and len(sub.value) >= 30:
                out.append((n.name, sub.value))
    return out


_declared_lit = tuple(pre for pre, _ in admin_api.PROMPT_FUNC_LITERALS)
_editable_values = set()
for _it in admin_api._catalog():
    try:
        _v = admin_api._read_path([_it["module"]] + _it["path"])
    except Exception:                                        # noqa: BLE001
        _v = ""
    if isinstance(_v, str):
        _editable_values.add(_v)

for mod_file in ("agents.py", "summarize.py"):
    miss = []
    for fname, text in _func_literals(os.path.join("backend", mod_file)):
        if text.startswith(_declared_lit) or text in _editable_values:
            continue
        miss.append(fname + "：「" + text.replace("\n", " ")[:32] + "」")
    check("★ 守门：" + mod_file + " **函数体内**每个 ≥30 字的句子，要么已登记只读、要么已抽成可改项",
          not miss, "漏了：" + " / ".join(miss))

# ---------------------------------------------------------------- 6. 改 prompt：立刻生效
MARK = "# SMOKE-ADMIN-MARK"
out_before = agents.style_rules("娱乐")
r = client.post("/api/admin/prompts", json={"prompts": {"agents:_STYLE_COMMON": out_before + "\n" + MARK}})
check("改一个 prompt 项 => 200", r.status_code == 200, r.text[:140])
check("改完立刻写进模块属性（不用重启）", MARK in agents._STYLE_COMMON)
check("★ 改了之后，**真正拼出来的规则**里带着它（不是改了没用）",
      MARK in agents.style_rules("娱乐"))
check("静态项的改动也落盘了",
      "agents:_STYLE_COMMON" in (json.loads(STATE.read_text(encoding="utf-8"))
                                 .get("prompts") or {}))

# 嵌套路径（档位赌注）—— 这是用户点名要的那类
lev_mark = "【赌注标记】"
r = client.post("/api/admin/prompts",
                json={"items": [{"key": "agents:LEVELS.认真.stake", "value": lev_mark}]})
check("改**表里的叶子**（档位赌注）=> 200", r.status_code == 200, r.text[:140])
check("★ 档位赌注改完立刻生效（原地改字典 ⇒ 不用重启、不用 reload）",
      agents.LEVELS["认真"]["stake"] == lev_mark)

# 性格
r = client.post("/api/admin/prompts",
                json={"items": [{"key": "agents:PERSONALITIES.bold.brief", "value": "【性格标记】"}]})
check("改**性格** => 200", r.status_code == 200, r.text[:140])
check("★ 性格改完，拼出来的性格块带着它",
      "【性格标记】" in agents._personality_block("bold"))

# 整张表 JSON（加/删条目）
r = client.post("/api/admin/prompts",
                json={"items": [{"key": "agents:VOICE_BY_PERSONALITY", "value": "不是JSON"}]})
check("整张表给了非法 JSON => 400", r.status_code == 400, r.text[:120])
r = client.post("/api/admin/prompts",
                json={"items": [{"key": "agents:VOICE_BY_PERSONALITY",
                                 "value": json.dumps({"bold": "Ryan", "cunning": "Vincent"}, ensure_ascii=False)}]})
check("整张表给合法 JSON => 200", r.status_code == 200, r.text[:140])
check("整张表替换后立刻生效", agents.VOICE_BY_PERSONALITY.get("cunning") == "Vincent")

# ---------------------------------------------------------------- 封面 LLM key
# ★ 2026-10-31（用户报："接入 LLM key 之后 AI 的发言还是兜底"）：
#   根因 = `MOCK_LLM=1` 时装配的是 MockLlm，填多少 key 都不会变成真模型。
#   口径：封面填了 LLM key 就是在要真模型 ⇒ 顺手关掉 MOCK_LLM 并持久化。
#   ⚠️ 用**临时状态文件**跑，绝不动仓库根的 `_runtime.json`（里面是真 key）。
print('\n★ 封面：填 LLM key 自动关闭 Mock（2026-10-31 用户报的“还是兜底”）')
import tempfile as _tempfile  # noqa: E402
import shutil as _shutil  # noqa: E402

_tmpdir = _tempfile.mkdtemp(prefix="admin_keys_")
_old_state_path = admin_api.STATE_PATH
_old_reload_fn = admin_api._reload_fn
_old_key = os.environ.get("LLM_API_KEY")
_old_mock = os.environ.get("MOCK_LLM")
try:
    admin_api.STATE_PATH = Path(_tmpdir) / "_runtime.json"
    admin_api._reload_fn = lambda: {"llm": "OpenAICompatLlm", "llm_mocked": False}
    os.environ["LLM_API_KEY"] = ""
    os.environ["MOCK_LLM"] = "1"
    _rk = client.post("/api/admin/play-keys", json={"llm": "sk-test-not-real"})
    check("填 LLM key => 200", _rk.status_code == 200, _rk.text[:140])
    _rj = _rk.json() if _rk.status_code == 200 else {}
    check("★ 填 LLM key 时 changed 里含 MOCK_LLM（自动关 Mock）",
          "MOCK_LLM" in (_rj.get("changed") or []), str(_rj)[:200])
    check("★ MOCK_LLM 真的被置 0（下一局不再装配 MockLlm）",
          os.environ.get("MOCK_LLM") == "0", os.environ.get("MOCK_LLM"))
    _st = json.loads(admin_api.STATE_PATH.read_text(encoding="utf-8"))
    check("★ _runtime.json 持久化了 MOCK_LLM=0（重启后仍然生效）",
          (_st.get("env") or {}).get("MOCK_LLM") == "0", str(_st.get("env"))[:160])
    _rg = client.get("/api/admin/play-keys").json()
    check("★ 封面能读到 LLM 的实际状态（llm_active / llm_mocked / llm_model）",
          "llm_active" in _rg and "llm_mocked" in _rg and "llm_model" in _rg,
          str(sorted(_rg))[:160])
    check("★ 封面能区分「配了 key」与「只有指纹」（llm_set）",
          _rg.get("llm_set") is True, str(_rg.get("llm_set")))

    # 🔴 2026-11-01（用户实机：真实 key 被 8 字符掩码覆盖 ⇒ AI 还是兜底）：
    #   非本机 GET 只回指纹（sk-…a506），页面原样提交时必须**拒绝写入**。
    _real = "sk-test-0123456789abcdef"
    os.environ["LLM_API_KEY"] = _real
    os.environ["MOCK_LLM"] = "0"
    _fp = admin_api.fingerprint(_real)
    check("指纹形如 sk-…cdef（模拟非本机回填）",
          "…" in _fp and _fp.endswith("cdef"), _fp)
    _rm = client.post("/api/admin/play-keys", json={"llm": _fp})
    _rmj = _rm.json() if _rm.status_code == 200 else {}
    check("★ 提交掩码不会覆盖真实 key",
          os.environ.get("LLM_API_KEY") == _real,
          "len=" + str(len(os.environ.get("LLM_API_KEY") or "")))
    check("★ 掩码提交返回明确提示（含「掩码」）",
          "掩码" in (_rmj.get("note") or ""), str(_rmj)[:200])
    check("★ 掩码提交不改 MOCK_LLM",
          os.environ.get("MOCK_LLM") == "0", os.environ.get("MOCK_LLM"))
    check("★ 全星号（短 key 的指纹）同样拒绝",
          admin_api._masked_key("***") and not admin_api._masked_key(_real))
    _html = open("frontend/play.html", encoding="utf-8").read()
    check("★ 前端：掩码不进输入框、不随「应用」提交（dataset.masked + keyValue）",
          "dataset.masked" in _html and "function keyValue" in _html
          and "已配置（本机不回显" in _html)
finally:
    admin_api.STATE_PATH = _old_state_path
    admin_api._reload_fn = _old_reload_fn
    for _k, _v in (("LLM_API_KEY", _old_key), ("MOCK_LLM", _old_mock)):
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    _shutil.rmtree(_tmpdir, ignore_errors=True)

# ---------------------------------------------------------------- 复原（不留副作用）
for k, v in _env_before.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v
for k, v in _agents_before.items():
    setattr(agents, k, v)
summarize.PROMPT = _summarize_before
os.environ["ADMIN_TOKEN"] = _token_before
if STATE_BEFORE is None:
    if STATE.exists():
        STATE.unlink()
else:
    STATE.write_text(STATE_BEFORE, encoding="utf-8")
server.reload_pipeline()

check("测试把改过的 env 复原了", os.environ.get("SAPI_RATE") == old_rate)
check("测试把改过的 prompt 复原了（含表里的嵌套项）",
      agents.LEVELS["认真"]["stake"] != lev_mark
      and agents.style_rules("娱乐") == _style_out_before
      and agents.PERSONALITIES["bold"]["brief"] != "【性格标记】")
check("测试把 _runtime.json 复原了（不留脏状态）",
      (STATE.read_text(encoding="utf-8") if STATE.exists() else None) == STATE_BEFORE)

print("\n" + "=" * 66)
if FAIL == 0:
    print("ALL GREEN (" + str(PASS) + " passed)")
    sys.exit(0)
print(str(FAIL) + " FAILED / " + str(PASS) + " passed")
sys.exit(1)
