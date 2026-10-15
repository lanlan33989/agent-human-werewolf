# -*- coding: utf-8 -*-
"""代码地图自检 —— 让「地图过期」变成一条**会红的测试**。

为什么需要它（这是这个项目最贵的一条教训）：
    **老的文档比没有文档更坏。** 本项目积累过 20 份文档、其中 15 条结论被推翻，
    而"照着作废的图纸施工"是真实发生过的事故。光靠"记得去更新文档"没有用 ——
    所以要治的是**机制**：凡地图里能机器判定的事实，都在这里断言化。

它核对的七件事（对着 `HANDOFF-开发交接单.md` 的 §2.1 和 §10）：
    1. 文件体量（§2.1.4 那张表）== 磁盘实际行数，且没有文件超过阈值
    2. `engine.py` 只 import 标准库（§4.1 契约，2026-09-25 之前**没有任何断言**在锁它）
    3. 依赖方向：engine 不许反向依赖上层；agents 不许 import fastapi/httpx
    4. `get_view_for` 仍是**唯一**取信息出口（§10.3 那条唯一安全边界）
    5. 活代码里不许再有「照 `CURRENT §x` 办事」的引用（那份文档 09-25 已退役）
    6. 断言数（§10.4 那张表）== 各 smoke **实际跑出来**的数   ← 重：会跑一遍所有 smoke
    7. 端点集合 == 运行中服务的 `/openapi.json`（源码装饰器 vs 实际注册）  ← 需要服务在跑

退出码（和 `_smoke-play.js` 同一套约定，三个必须分清）：
    0 = 跑了，全过
    1 = 跑了，有失败
    2 = **没跑成** —— 第 6/7 条因环境不具备而没跑（且静态部分没有失败）。
        ⚠️ 不许把 2 当成通过：那是本项目栽过两次的"静默降级"。

用法：
    .venv\\Scripts\\python.exe _smoke-map.py
    .venv\\Scripts\\python.exe _smoke-map.py --fast      # 跳过第 6 条（不跑 smoke）
"""
from __future__ import annotations
import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))  # 仓库根目录入 path：保证 python tests/xxx.py 可直接跑

import argparse
import ast
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent    # .../tabletop-ai/h5
ROOT = BASE.parent                              # .../tabletop-ai
MAP = ROOT / "HANDOFF-开发交接单.md"
RETIRED = ROOT / "狼人杀旧版本参照.md"            # 已退役，仅作追溯

# 单文件行数上限（§2.1.4）：超了就该拆 —— 不是洁癖，是"AI 一次读不完一份文件"。
# 单文件行数上限（§2.1.4）。2026-10-01 由 1600 提到 1700：
#   那天把配置台（约 320 行）**拆去了 admin_api.py**，server.py 只多出"热换 + 注入"
#   那三十来行胶水（1593 -> 1623）—— 该拆的已经拆了，剩下的都是它自己的职责。
# 状态机抽成了 tableui.js，play.html 回到 983 行）：用户定的大屏改版（单一大圆 / 大圆发言模式 /
#   票型大字）只能落在 play.html —— 这些是**页面级**覆盖（demo 与 table.css/table.js
#   是共享文件，不能动）。**欠着的拆分计划**：把对局页的驱动脚本（大圆状态机 /
#   票型展示 / 播报队列）抽成 `play.js`（像 table.js 那样页面间复用与隔离），
#   抽完 play.html 应回到 ~1100 行。在那之前，别再往它身上加整块功能。
#   ⚠️ 这条不是"超了就抬"的橡皮筋：再往上加之前先想清楚是加还是拆。
LINE_LIMIT = 1900

# 项目内依赖图（**2026-09-25 实测**，不是拍的）。
#
# ⚠️ 为什么要把图钉死：**新边长出来就是一次架构变更**。这里多一条边 ⇒ 检查红 ⇒
#    逼你有意识地改这里（而不是让它悄悄长歪）。
# ⚠️ 第一版这里是我"拍"的（写成 UPPER_LAYERS = engine 不许 import metrics 等），
#    结果**当场误报**：engine 真的 `from metrics import ...` —— `metrics` 是**叶子模块**
#    （L0 派生，只吃公开发言，零依赖），engine 依赖它是允许的。
#    而退役文档里那句「engine.py 纯标准库」是**不准确的**（它对第三方成立，对项目内模块不成立）
#    —— 这正是"永不参照"要拦下的东西：旧文档的一句含糊话会被下一代照抄。
INNER_GRAPH = {
    # 叶子（零项目内依赖、零第三方）：纯函数 / 常量，谁都可以用
    "metrics": set(), "steps": set(), "summarize": set(),
    # 2026-10-07 夜间剧本化第二期：剧本表 + AI 决策预跑池（都是叶子）
    "night_script": set(), "night_pre": set(),
    # 引擎层（零第三方）
    "engine": {"metrics"},
    "agents": {"metrics"},
    # 工具层
    "experience": {"agents"},
    "report": {"experience"},
    "eval_batch": {"metrics"},
    "demo_game": {"agents", "engine"},
    # 接口层（从这里开始才许碰第三方）
    # 2026-10-06 P2：Session 拆出三模块（game_api 保留薄委托）
    "speech_io": {"agents", "engine"},
    # 2026-10-07：ai_behaviors 取预跑结果（night_pre）；驱动器按 night_script 生成播报
    "ai_behaviors": {"agents", "engine", "night_pre"},
    "night_driver": {"agents", "engine", "experience", "night_pre",
                     "night_script", "speech_io", "steps"},
    # 2026-10-07 第三期：game_api 直接用 night_script 算 pending_seats 的 node/seconds_left
    "game_api": {"agents", "ai_behaviors", "engine", "experience",
                 "metrics", "night_driver", "night_script", "report", "speech_io",
                 "steps"},
    # 2026-10-06 P4a：三层实现拆出（server 保留转口 import）
    "layers_asr": set(),
    "layers_llm": set(),
    "layers_tts": set(),
    "server": {"admin_api", "agents", "game_api", "layers_asr",
               "layers_llm", "layers_tts", "summarize"},
    # 配置台（2026-10-01）：**单向**。server 装配时 import 它、并在末尾
    # `admin_api.register(app, reload_fn=…, snapshot_fn=…, page_fn=…)` 注入三件事；
    # admin_api **不** import server（第一次写成双向边，当场被这条无环检查拦下）。
    # ⚠️ 它是 **set()**：prompt 清单改成了"按名字动态 import"（`importlib.import_module`，
    #    名字来自清单表 `agents:LEVELS.娱乐.stake` 这类键），所以**静态 import 一个都没有** ——
    #    依赖是有的，但这条检查看的是静态边，看不见动态的那部分（2026-10-01 被它抓出来后
    #    改成如实声明"没有静态边"）。要给 admin_api 加静态依赖时，记得同时改这一行。
    "admin_api": set(),
    "start": {"run_https", "server"},
    "run_https": set(),
}

# 只有这几个模块许碰第三方；**其余一律零第三方**（engine/agents 是引擎层，这条最要紧）
THIRD_PARTY_OK = {
    "game_api": {"fastapi", "pydantic"},
    "layers_asr": {"faster_whisper", "httpx", "numpy"},
    "layers_llm": {"httpx"},
    "layers_tts": {"comtypes", "httpx"},
    "server": {"comtypes", "fastapi", "faster_whisper", "httpx", "numpy", "websockets"},
    "admin_api": {"fastapi"},
    "start": {"uvicorn"},
    "run_https": {"cryptography", "uvicorn"},
}

PASS: list[str] = []
FAIL: list[str] = []
BLOCKED: list[str] = []          # 环境不具备 ⇒ "没跑成"，既不算过也不算失败


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  {} {}".format("OK  " if ok else "FAIL", name)
          + ("  <- " + str(detail) if (detail and not ok) else ""))


def note(s: str) -> None:
    print("       " + s)


def blocked(name: str, why: str) -> None:
    BLOCKED.append(name)
    print("  NOT-RUN {}".format(name))
    note("=> 这一条**没有发生**（不是通过）：" + why)


def section(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("-" * 74)


# ------------------------------------------------------------------ 读地图

def map_text() -> str:
    if not MAP.exists():
        print("找不到地图：{}".format(MAP))
        sys.exit(2)
    return MAP.read_text(encoding="utf-8")


def map_section(text: str, head: str) -> str:
    """取某个 #### 小节（到下一个 #### 或 ## 为止）。"""
    i = text.find(head)
    if i < 0:
        return ""
    j = len(text)
    for pat in ("\n#### ", "\n### ", "\n## "):
        k = text.find(pat, i + len(head))
        if k >= 0:
            j = min(j, k)
    return text[i:j]


def map_size_table(text: str) -> dict[str, int]:
    """§2.1.4 的「文件 | 行」表 -> {相对路径: 行数}。"""
    sec = map_section(text, "#### 2.1.4")
    out: dict[str, int] = {}
    for name, num in re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|js|css|html))`\s*\|\s*(\d+)", sec):
        out[name] = int(num)
    return out


def map_assert_table(text: str) -> dict[str, int]:
    """§10.4 的「测试文件 | 断言」表 -> {文件名: 断言数}。"""
    sec = map_section(text, "### 10.4")
    out: dict[str, int] = {}
    for row in sec.split("\n"):
        if not row.startswith("|") or "---" in row:
            continue
        cells = [c.strip() for c in row.strip("|").split("|")]
        if len(cells) < 2:
            continue
        names = re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|js))`", cells[0])
        nums = re.findall(r"(\d+)", cells[1])
        if names and len(names) == len(nums):
            for n, v in zip(names, nums):
                out[n] = int(v)
    return out


# ------------------------------------------------------------------ 1 体量

def check_sizes(text: str) -> None:
    section("1. 文件体量 == 地图 §2.1.4（阈值 {} 行）".format(LINE_LIMIT))
    want = map_size_table(text)
    if not want:
        check("地图 §2.1.4 能解析出文件与行数", False, "正则没匹配到，表被改过格式？")
        return
    check("地图 §2.1.4 解析出 {} 个文件".format(len(want)), len(want) >= 10)
    for name, lines in sorted(want.items()):
        p = (ROOT / name) if "/" in name else (BASE / name)
        if not p.exists():
            check("{} 存在".format(name), False, "地图里有、磁盘上没有")
            continue
        real = p.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        check("{} {} 行（地图写的）".format(name.ljust(18), lines), real == lines,
              "实际 {} 行 -> 地图该改成 {}".format(real, real))
        check("{} 未超 {} 行".format(name.ljust(18), LINE_LIMIT), real <= LINE_LIMIT,
              "{} 行，该拆了（或说明为什么不拆）".format(real))


# ------------------------------------------------------------------ 2/3 依赖

def imports_of(p: Path) -> tuple[set[str], set[str]]:
    """返回 (项目内模块, 第三方模块)。标准库两边都不算。"""
    mods = {q.stem for q in BASE.glob("*.py") if not q.stem.startswith("_")}
    std = set(sys.stdlib_module_names)
    tree = ast.parse(p.read_text(encoding="utf-8"))
    inner: set[str] = set()
    ext: set[str] = set()
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names = [node.module]
        for full in names:
            top = full.split(".")[0]
            if top in mods:
                inner.add(top)
            elif top not in std:
                ext.add(top)
    return inner, ext


def check_deps() -> None:
    section("2. 引擎层零第三方（§4.1 契约：engine / agents）")
    std = set(sys.stdlib_module_names)
    check("stdlib 名单可用（用它判定第三方）", len(std) > 100)
    for name in ("engine.py", "agents.py", "metrics.py"):
        inner, ext = imports_of(BASE / name)
        check("{} 零第三方".format(name.ljust(12)), not ext,
              "它 import 了：" + ", ".join(sorted(ext)))
        note("{} -> 项目内 {}｜第三方 {}".format(
            name, ",".join(sorted(inner)) or "(无)", ",".join(sorted(ext)) or "(无)"))

    section("3. 项目内依赖图 == 2026-09-25 实测的那份（§2.1.1）")
    seen: dict[str, tuple[set[str], set[str]]] = {}
    for name in sorted(INNER_GRAPH):
        p = BASE / "{}.py".format(name)
        if not p.exists():
            check("{} 存在".format(name), False, "地图/依赖图里有、磁盘上没有")
            continue
        seen[name] = imports_of(p)
    for name in sorted(INNER_GRAPH):
        if name not in seen:
            continue
        inner, ext = seen[name]
        want = INNER_GRAPH[name]
        grew = sorted(inner - want)
        shrank = sorted(want - inner)
        check("{} 的项目内依赖没变".format(name.ljust(12)), not grew and not shrank,
              "新增边 {}｜少了边 {}（真改了就把 INNER_GRAPH 一起改）".format(grew, shrank))
        over = sorted(ext - THIRD_PARTY_OK.get(name, set()))
        check("{} 的第三方在允许清单内".format(name.ljust(12)), not over,
              "多出来：" + ", ".join(over))

    def find_cycle() -> list[str]:
        state: dict[str, int] = {}

        def dfs(n: str, stack: list[str]) -> list[str]:
            state[n] = 1
            for m in sorted(INNER_GRAPH.get(n, set())):
                if state.get(m) == 1:
                    return stack + [n, m]
                if state.get(m, 0) == 0:
                    r = dfs(m, stack + [n])
                    if r:
                        return r
            state[n] = 2
            return []

        for n in sorted(INNER_GRAPH):
            if state.get(n, 0) == 0:
                r = dfs(n, [])
                if r:
                    return r
        return []

    cyc = find_cycle()
    check("依赖图无环（下层不许反过来依赖上层）", not cyc, " -> ".join(cyc))


# ------------------------------------------------------------------ 4 唯一出口

def check_view_exit(text: str) -> None:
    section("4. get_view_for 仍是唯一取信息出口（§10.3）")
    owners = []
    for p in sorted(BASE.glob("*.py")):
        if re.search(r"^\s*def\s+get_view_for\b", p.read_text(encoding="utf-8"), re.M):
            owners.append(p.name)
    check("get_view_for 只在一处定义", owners == ["engine.py"], "定义在：" + ", ".join(owners))

    # "第二个出口"的典型样子：另写一个函数把所有人的私有信息一次性拿出来。
    # ⚠️ **不要拿 `god` 当关键词** —— 狼人杀里 god = **神职**，`is_god` / `god_list`
    #    都是领域词。2026-09-25 第一版就是这么误报的（同一次里我"拍"的 UPPER_LAYERS 也误报一回）。
    sus = []
    for p in sorted(BASE.glob("*.py")):
        s = p.read_text(encoding="utf-8")
        for m in re.finditer(r"^\s*(?:async\s+)?def\s+(\w*(?:all_?view|view_all|debug_view|"
                             r"reveal|omniscient|dump_all)\w*)\s*\(", s, re.M | re.I):
            sus.append("{}.{}".format(p.name, m.group(1)))
    check("没有出现'第二个取信息出口'式的新函数名", not sus, "可疑：" + ", ".join(sus))
    # 比查函数名更硬的一条：HTTP 层要座位视图时必须**调用** engine 那个函数，不许自己拼
    ga = (BASE / "game_api.py").read_text(encoding="utf-8")
    check("game_api 取座位视图走的是 get_view_for", "get_view_for(" in ga,
          "game_api.py 里没有调用 get_view_for —— 它是不是自己拼了一份视图？")

    section("5. 活代码里不许再'照退役文档办事'（2026-09-25 退役）")
    hits = []
    for pat in ("*.py", "*.js", "*.html"):
        for p in list(BASE.glob(pat)) + list((ROOT / "demo").glob(pat)):
            if p.name.startswith("_serve_docs") or p.name == "_smoke-map.py":
                # _serve_docs：文档预览服务 —— 它的注释里**必须**提旧名字（说明它退役了、
                #             以及"权威列表"里为什么把它拿掉）
                # _smoke-map.py：本文件 —— 它的注释里**必须**写清自己在拦什么
                continue
            s = p.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"CURRENT\s*§", s):
                hits.append("{}@{}".format(p.name, s[:m.start()].count("\n") + 1))
    check("没有 'CURRENT §x' 这种按退役文档办事的引用", not hits,
          "命中：" + ", ".join(hits[:5]))
    check("退役文件仍在（改名了，没删）", RETIRED.exists(), str(RETIRED))
    check("活文档里也没有 'CURRENT §x' 引用",
          not re.search(r"CURRENT\s*§", text.replace(
              map_section(text, "## 10."), "")),
          "除 §10 的说明外，HANDOFF 里还有引用")


# ------------------------------------------------------------------ 6 断言数

SUMMARY_PATS = [
    re.compile(r"ALL GREEN \((\d+) passed\)"),
    re.compile(r"结果：(\d+)/(\d+)\s*(?:通过|一致)"),
]


def run_one(path: Path, rel: str) -> tuple[int | None, str]:
    """跑一个 smoke，返回 (断言数 或 None, 说明)。

    ⚠️ `.js` 的必须用 **node** 跑。第一版一律用 `sys.executable`，
    于是 `demo/_smoke-demo.js` 被 Python 执行 ⇒ 报"没解析出断言数"
    ⇒ 自己造了一个假的 BLOCKED（**假红**也是要治的病：它和"假绿"一样会误导人）。
    """
    runner = sys.executable if path.suffix == ".py" else "node"
    try:
        r = subprocess.run([runner, path.name], cwd=str(path.parent),
                           capture_output=True, timeout=600)
    except FileNotFoundError:
        return None, "找不到 {}（没装 / 不在 PATH 上）".format(runner)
    except subprocess.TimeoutExpired:
        return None, "超时（>600s）"
    out = (r.stdout or b"").decode("utf-8", errors="replace")
    for pat in SUMMARY_PATS:
        m = pat.search(out)
        if m:
            return int(m.group(1)), "exit={}".format(r.returncode)
    if r.returncode == 2:
        return None, "exit 2（**没跑成**：环境不具备）"
    if r.returncode == 1:
        return None, ("exit 1（**它自己就是红的** ⇒ 断言数没法核对；"
                      "先把这个测试修绿，再回来看地图上的数字）")
    return None, "exit {}（没解析出断言数 —— 输出格式改了？）".format(r.returncode)


def check_counts(text: str, fast: bool) -> None:
    section("6. 断言数 == 地图 §10.4（重：会跑一遍所有 smoke）")
    want = map_assert_table(text)
    if not want:
        check("地图 §10.4 能解析出文件与断言数", False, "正则没匹配到")
        return
    check("地图 §10.4 解析出 {} 个测试文件".format(len(want)), len(want) >= 15)
    if fast:
        blocked("6. 断言数对账（--fast 跳过了）", "--fast")
        return

    total = 0
    for name, n in sorted(want.items()):
        p = (ROOT / name) if "/" in name else (BASE / name)
        if not p.exists():
            check("{} 存在".format(name), False)
            continue
        t0 = time.time()
        got, why = run_one(p, name)
        dt = time.time() - t0
        if got is None:
            blocked("{}（地图写 {}）".format(name, n), why)
            continue
        total += got
        check("{} {:>3} 条 == 地图".format(name.ljust(22), got), got == n,
              "地图写 {}、实际 {} -> 地图该改成 {}".format(n, got, got))
        note("{}  {:.1f}s".format(name, dt))
    note("本次逐个跑出来、且与地图一致的合计：{} 条".format(total))


# ------------------------------------------------------------------ 7 端点

def source_endpoints() -> tuple[set[tuple[str, str]], set[str]]:
    """返回 (HTTP 端点, websocket 路径)。

    ⚠️ **两处都必须小心，两处都踩过**（都是"假红"—— 它和假绿一样有害，
       会让人去查一个不存在的问题）：
       ① 装饰器写的是 `@router.get("/experience")`，而
          `router = APIRouter(prefix="/api/game")` ⇒ 线上是 `/api/game/experience`。
          第一版没拼前缀 ⇒ 一次报出 5 个"声明了但没注册" + 5 个"多出来"。
       ② FastAPI 的 `openapi.json` **不含 websocket**（它描述的是 HTTP 契约）
          ⇒ ws 不能塞进 HTTP 集合去比对。
    """
    http: set[tuple[str, str]] = set()
    ws: set[str] = set()
    for p in (BASE / "game_api.py", BASE / "server.py", BASE / "admin_api.py"):
        s = p.read_text(encoding="utf-8")
        m = re.search(r'APIRouter\([^)]*?prefix\s*=\s*"([^"]*)"', s)
        prefix = m.group(1) if m else ""
        for m2 in re.finditer(r'@(app|router)\.(get|post|put|delete)\(\s*"([^"]+)"', s):
            head = prefix if m2.group(1) == "router" else ""
            http.add((m2.group(2).upper(), head + m2.group(3)))
        for m2 in re.finditer(r'@(app|router)\.websocket\(\s*"([^"]+)"', s):
            head = prefix if m2.group(1) == "router" else ""
            ws.add(head + m2.group(2))
    return http, ws


def live_endpoints() -> set[tuple[str, str]] | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/openapi.json", timeout=5) as r:
            spec = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    out: set[tuple[str, str]] = set()
    for path, ops in (spec.get("paths") or {}).items():
        for method in ops:
            if method.lower() in ("get", "post", "put", "delete", "websocket"):
                out.add((method.upper(), path))
    return out


def check_endpoints() -> None:
    section("7. 端点集合 == 运行中服务的 /openapi.json（§10.3）")
    src, ws = source_endpoints()
    check("源码里能解析出 HTTP 端点声明", len(src) >= 20, "只找到 {} 个".format(len(src)))
    note("源码装饰器：HTTP {} 个 ｜ websocket {} 个".format(len(src), len(ws)))
    if ws:
        note("websocket（openapi 看不到，只能靠源码 + 流式 smoke 验）："
             + ", ".join(sorted(ws)))
    live = live_endpoints()
    if live is None:
        blocked("7. 端点对账", "服务不在 127.0.0.1:8000 上（先跑 start.py）")
        return
    note("openapi 实际：{} 个".format(len(live)))
    only_src = sorted(src - live)
    only_live = sorted(live - src)
    check("源码声明的 HTTP 端点都真的注册了", not only_src,
          "声明了但没注册：" + str(only_src[:5]))
    check("线上没有源码里没有的 HTTP 端点", not only_live,
          "多出来：" + str(only_live[:5]))
    check("两个 websocket 路由都在源码里", {"/ws/asr", "/ws/tts"} <= ws,
          "现有：" + ", ".join(sorted(ws)))


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="跳过第 6 条（不跑 smoke）")
    args = ap.parse_args()

    print("代码地图自检  --  对着 {}".format(MAP.name))
    text = map_text()

    check_sizes(text)
    check_deps()
    check_view_exit(text)
    check_counts(text, args.fast)
    check_endpoints()

    print("\n" + "=" * 74)
    print("结果：{} 通过，{} 失败，{} 项没跑成".format(len(PASS), len(FAIL), len(BLOCKED)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    if BLOCKED:
        print("没跑成（**不是通过**）：")
        for b in BLOCKED:
            print("  - " + b)
    if FAIL:
        print("=> 地图与代码不符：去改**文档**，别改这个检查。")
    print("=" * 74)

    if FAIL:
        return 1
    if BLOCKED:
        return 2          # 没跑完 —— 绝不能算通过（本项目栽过两次的"静默降级"）
    return 0


if __name__ == "__main__":
    sys.exit(main())
