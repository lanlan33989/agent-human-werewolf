# -*- coding: utf-8 -*-
"""
前端联调假服务（2026-09-26 前后端分离配套）：把 mock/fixtures/ 里的真实响应
按路由返回，前端不碰后端也能把夜间面板/轮盘/倒计时全部开发完。

用法（仓库根 h5/ 下）：
    python mock/mock_server.py            # 默认 :5501
    python mock/mock_server.py --port 5501 --delay 0.8

路由约定（fixtures 文件名即路由）：
    /api/health            → health.json
    /api/game/rules        → rules.json
    /api/game/settings     → settings.json
    POST /api/game/new     → new_game.json（gid 固定 "MOCKGID"）
    GET  /api/game/MOCKGID/public → public.json
    POST /api/game/MOCKGID/act    → {"ok": true}（自己按 action 换返回）

想演练异常序列（夜转昼 / started 翻转 / until_left 回退）：直接改 fixtures，
或在 MOCK_SEQUENCE 里排几套场景轮播 —— 不许为这个去连真后端。
"""
import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GID = "MOCKGID"

ROUTES = {
    "/api/health": "health.json",
    "/api/game/rules": "rules.json",
    "/api/game/settings": "settings.json",
    "/api/game/new": "new_game.json",
    f"/api/game/{GID}/public": "public.json",
}


def _load(name: str):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


# 静态页面（2026-10-27）：同一端口直接吐 frontend/ 的页面与资源 ——
# 黄子凡不需要再开第二个服务：:5501/play.html 就是"页面+假数据"全流程。
_FRONTEND = os.path.join(os.path.dirname(HERE), "frontend")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
          ".css": "text/css", ".png": "image/png", ".ico": "image/x-icon"}


def _static(path: str):
    name = "play.html" if path in ("/", "/play") else path.lstrip("/")
    fp = os.path.normpath(os.path.join(_FRONTEND, name))
    if not fp.startswith(_FRONTEND) or not os.path.isfile(fp):
        return None
    ext = os.path.splitext(fp)[1]
    with open(fp, "rb") as f:
        return _TYPES.get(ext, "application/octet-stream"), f.read()


class H(BaseHTTPRequestHandler):
    delay = 0.0

    def _send(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")   # 前端任意 dev 端口
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                                # noqa: N802
        path = self.path.split("?")[0]
        if self.delay:
            import time
            time.sleep(self.delay)
        for route, name in ROUTES.items():
            if path == route:
                data = _load(name)
                if path.endswith("/new"):
                    data["gid"] = GID
                return self._send(data)
        # 身份牌（2026-10-27）：按座位轮换角色 —— 翻 4 张牌能看 4 种身份样式
        m = re.match(r"^/api/game/" + GID + r"/seat/(\d+)$", path)
        if m:
            n = int(m.group(1))
            roles = ["wolf", "seer", "guard", "witch", "villager"]
            role = roles[n % len(roles)]
            return self._send({
                "seat": n, "role": role,
                "alive": True, "round": 0, "phase": "SETUP",
                "speech": [], "timeline": [],
                "pending": f"human_look:{n}",
                "is_my_turn_to_speak": False, "is_my_turn_to_vote": False,
                "players": [{"seat": i, "display": f"{i + 1}号", "kind": "human" if i == n else "ai", "alive": True}
                            for i in range(4)],
            })
        st = _static(path)
        if st is not None:
            mime, body = st
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        return self._send({"error": "mock 没配这个路由：" + path}, 404)

    def do_POST(self) -> None:                               # noqa: N802
        path = self.path.split("?")[0]
        if self.delay:
            import time
            time.sleep(self.delay)
        if path == "/api/game/new":
            # 🔴 2026-10-27 修复：开局是 POST，之前只配了 GET 侧 → 点"开始一局"404
            data = _load("new_game.json")
            data["gid"] = GID
            return self._send(data)
        if path == f"/api/game/{GID}/act" or path.endswith("/arm") \
                or path.endswith("/tick"):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            return self._send({"ok": True, "echo": req})
        return self._send({"error": "mock 没配这个路由：" + path}, 404)

    def log_message(self, *a) -> None:                       # noqa: N802
        print("[mock]", self.path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5501)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="每个响应延迟秒数（模拟网络/后端慢）")
    args = ap.parse_args()
    H.delay = args.delay
    print(f"[mock] 假服务 http://127.0.0.1:{args.port}  （fixtures: {os.path.join(HERE, 'fixtures')}）")
    ThreadingHTTPServer(("127.0.0.1", args.port), H).serve_forever()
