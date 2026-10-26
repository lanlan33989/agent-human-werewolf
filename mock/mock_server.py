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
        return self._send({"error": "mock 没配这个路由：" + path}, 404)

    def do_POST(self) -> None:                               # noqa: N802
        path = self.path.split("?")[0]
        if self.delay:
            import time
            time.sleep(self.delay)
        if path == f"/api/game/{GID}/act":
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
