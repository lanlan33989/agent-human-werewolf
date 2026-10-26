# -*- coding: utf-8 -*-
"""
前后端分离一致性检查（2026-10-27）。

为什么要有它：分离不是"把 js 挪进 frontend/ 就完事"。真实代价是**四类漂移**：
  ① 后端 `_page()/_asset()` 引用的静态文件实际不存在（浏览器 404、页面少一块）；
  ② 前端 fetch 的接口后端没有（点了没反应，控制台 404）；
  ③ 后端有路由但前端没人用（白给的面，联调时才发现字段对不上）；
  ④ `docs/openapi.json` 没跟着后端重生成（新人照它对接 = 对着过期契约写代码）。
这四类平时都不报错，只在联调那天一起爆炸 —— 所以写成脚本，**谁改谁跑**。

用法（仓库根 h5/ 下）：
    ./.venv/Scripts/python.exe tests/_check-split-consistency.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
FRONTEND = BASE / "frontend"
BACKEND = BASE / "backend"

sys.path.insert(0, str(BACKEND))

FAILS: list[str] = []
WARNS: list[str] = []


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def bad(msg: str) -> None:
    FAILS.append(msg)
    print(f"  [NG] {msg}")


def warn(msg: str) -> None:
    WARNS.append(msg)
    print(f"  [!!] {msg}")


# ---------------------------------------------------------------- ① 静态资源
def check_static_files() -> set[str]:
    print("\n① 后端路由引用的静态文件是否真的存在（缺一个 = 页面少一块）")
    refs: dict[str, list[str]] = {}
    for p in BACKEND.rglob("*.py"):
        for m in re.finditer(r'_(?:page|asset)\(\s*["\']([^"\']+)["\']', p.read_text(encoding="utf-8")):
            refs.setdefault(m.group(1), []).append(p.name)
    used = set()
    for name, owners in sorted(refs.items()):
        used.add(name)
        if (FRONTEND / name).exists():
            ok(f"{name:22s} ← {','.join(owners)}")
        else:
            bad(f"{name} 不存在于 frontend/（被 {','.join(owners)} 引用）")
    return used


# ---------------------------------------------------------------- ② / ③ 接口
def frontend_api_paths() -> set[str]:
    """前端代码里出现过的所有 /api/... 路径（拼接的变量统一归一化成 {param}）。"""
    out: set[str] = set()
    for p in list(FRONTEND.rglob("*.js")) + list(FRONTEND.rglob("*.html")):
        text = p.read_text(encoding="utf-8")
        # 先把 `'...' + GID + '...'` 这类拼接粘成一整串，再抽到 path 里
        text = re.sub(r"['\`\"]\s*\+\s*[A-Za-z_$][\w$]*\s*\+\s*['\`\"]", "{param}", text)
        text = re.sub(r"\$\{[^}]+\}", "{param}", text)
        for m in re.finditer(r'["\'`](/api/[^"\'`]*)["\'`]', text):
            s = m.group(1).split("?")[0].strip()
            if s.count("{param}") and s.endswith("/"):
                pass
            out.add(s)
    return out


def check_api_paths() -> None:
    print("\n② 前端调的接口，后端有没有；③ 后端有的接口，前端有没有用")
    try:
        from server import app  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        warn(f"导入 backend/server.py 失败（依赖没装？{type(e).__name__}: {e}）—— 跳过 ②③，请先在 .venv 里装 backend/requirements.txt")
        return

    # ⚠️ 走 app.openapi() 而不是 app.routes：新版 FastAPI/Starlette 会把
    #    router 包成 _IncludedRouter / mount，自己遍历会漏掉一多半路由。
    routes = set(app.openapi().get("paths", {}).keys())
    fe = frontend_api_paths()

    def _match(p: str) -> bool:
        if p in routes:
            return True
        for r in routes:                      # 把 {param} 当通配：逐段比
            pr, rr = p.strip("/").split("/"), r.strip("/").split("/")
            if len(pr) != len(rr):
                continue
            if all(a == b or b.startswith("{") for a, b in zip(pr, rr)):
                return True
        return False

    missing = sorted(p for p in fe if not _match(p) and not _match(p + "{param}"))
    if missing:
        for p in missing:
            bad(f"前端调用了后端没有的接口：{p}")
    else:
        ok(f"前端引用的 {len(fe)} 条接口路径，后端全部有对应路由")

    unused = sorted(r for r in routes if r.startswith("/api") and not _match(r.replace("{gid}", "{param}").replace("{seat}", "{param}")))
    # 后台管理/实验室/评测类接口，前端不一定用，只提示不算错
    if unused:
        warn("后端有但前端没用到的接口（正常的话请忽略，多出来的请确认是不是漏接）："
             + ", ".join(unused[:12]) + (" …" if len(unused) > 12 else ""))

    # ------------------------------------------------------------ ④ openapi
    print("\n④ docs/openapi.json 与后端现有路由是否一致")
    op = BASE / "docs" / "openapi.json"
    if not op.exists():
        warn("docs/openapi.json 不存在")
        return
    doc = set(json.loads(op.read_text(encoding="utf-8")).get("paths", {}).keys())
    gone = sorted(doc - routes)
    fresh = sorted(routes - doc)
    if gone:
        bad("openapi.json 里有、但代码里已经没有的路由：" + ", ".join(gone[:10]))
    if fresh:
        bad("代码里有、但 openapi.json 没登记的路由：" + ", ".join(fresh[:10]))
    if not gone and not fresh:
        ok(f"openapi.json 与后端路由完全一致（{len(doc)} 条）")


# ---------------------------------------------------------------- ⑤ 目录归属
def check_ownership() -> None:
    print("\n⑤ 目录归属：别把后端的包袱留给前端，反之亦然")
    for sub, owner in (("frontend", "前端"), ("backend", "后端")):
        root = BASE / sub
        stray_py = [str(p.relative_to(BASE)) for p in root.rglob("*.py")] if sub == "frontend" else []
        stray_js = [str(p.relative_to(BASE)) for p in root.rglob("*.js")] if sub == "backend" else []
        # frontend/mock 是**前端**自己的假数据服务，允许 .py；backend 里不应有前端 js
        allowed = lambda f: "frontend/mock" in f.replace("\\", "/")  # noqa: E731
        real_py = [f for f in stray_py if not allowed(f)]
        if real_py:
            warn(f"{owner}目录里出现了 Python 文件（应在 backend/）：" + ", ".join(real_py[:6]))
        if sub == "frontend":
            ok("frontend/ 下的 .py 只有 mock/（假数据服务，归前端管）")
        if stray_js and sub == "backend":
            warn("backend/ 里有 .js 文件（应在 frontend/）：" + ", ".join(stray_js[:6]))

    root_junk = [p.name for p in BASE.glob("*")
                 if p.is_file() and p.name not in
                 {"README.md", "LICENSE", ".gitignore", ".env.example", "start.py", "cert.pem", "key.pem", ".env"}
                 and not p.name.startswith(("_", "."))]
    if root_junk:
        warn("仓库根还有这些散文件，确认是不是忘了归档：" + ", ".join(root_junk))
    else:
        ok("仓库根只剩 README / LICENSE / .gitignore / .env.example / start.py（+ 运行时证书）")


# ---------------------------------------------------------------- ⑥ mock 可跑
def check_mock_paths() -> None:
    print("\n⑥ 前端 mock 服务的静态根目录还对不对")
    ms = FRONTEND / "mock" / "mock_server.py"
    if not ms.exists():
        bad("frontend/mock/mock_server.py 不见了（前端迭代全靠它）")
        return
    src = ms.read_text(encoding="utf-8")
    if 'os.path.dirname(HERE)' in src and '"frontend"' in src:
        bad("mock_server.py 还在按旧的 h5/mock/ 位置算 frontend 路径")
    else:
        ok("mock_server.py 的路径已按 frontend/mock/ 更新")
    ok(f"mock fixtures 数量：{len(list((FRONTEND / 'mock' / 'fixtures').glob('*.json')))}")


def main() -> int:
    print("===== 前后端分离一致性检查 =====")
    used = check_static_files()
    check_api_paths()
    check_ownership()
    check_mock_paths()

    print("\n===== 结果 =====")
    print(f"失败 {len(FAILS)} 项，提醒 {len(WARNS)} 项")
    if FAILS:
        print("\n必须修：")
        for f in FAILS:
            print("  - " + f)
    if WARNS:
        print("\n提醒（不一定错，但请过一眼）：")
        for w in WARNS:
            print("  - " + w)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
