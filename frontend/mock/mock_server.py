# -*- coding: utf-8 -*-
"""
前端联调假服务（2026-09-26 前后端分离配套）：把 frontend/mock/fixtures/ 里的真实响应
按路由返回，前端不碰后端也能把夜间面板/轮盘/倒计时全部开发完。

用法（仓库根 h5/ 下）：
    python frontend/mock/mock_server.py            # 默认 :5501（仓库根 h5/ 下执行）
    python frontend/mock/mock_server.py --port 5501 --delay 0.8

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


# 战报样例：真后端 `/api/game/{gid}/report.md` 是 **Markdown 纯文本**
# （game_api.py:951 media_type="text/markdown"），所以这里同格式给。
_SAMPLE_REPORT = """# 本局战报（MOCK 样例）

- 板子：6 人 · 2 狼 1 巫 1 预 2 民
- 档位：认真 · 共 3 轮
- 结果：**好人胜利**

## 身份一览

| 座位 | 身份 | 结局 |
|---|---|---|
| 1 号 | 狼人（真人） | 第 3 轮被投出 |
| 2 号 | 预言家 | 存活 |
| 3 号 | 女巫 | 第 2 夜被刀 |
| 4 号 | 守卫 | 存活 |
| 5 号 | 狼人 | 第 2 轮被投出 |
| 6 号 | 村民 | 第 1 夜被刀 |

## 逐夜

- 第 1 夜：狼刀 6 号，女巫未救，守卫守 2 号。
- 第 2 夜：狼刀 3 号，女巫已出局，预言家验 1 号（查杀）。
- 第 3 轮：1 号被 4 票投出，狼队人数不足，好人获胜。

> 想改排版：这是服务端吐的一整份 Markdown，改样式要先确定"是做成 HTML 页
> 还是继续纯文本"，这条口子归后端 + 前端一起定（见 docs/界面清单.md）。
"""


def _load(name: str):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


# 静态页面（2026-10-27）：同一端口直接吐 frontend/ 的页面与资源 ——
# 前端同学不需要再开第二个服务：:5501/play.html 就是"页面+假数据"全流程。
# ⚠️ 2026-10-27 搬家：本服务从 h5/mock/ 挪到了 h5/frontend/mock/
#    （归到前端目录，免得被当作后端的一部分而没人管） ⇒ frontend/ 现在是
#    HERE 的上一级，仓库根再上一级。
_FRONTEND = os.path.dirname(HERE)
_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
          ".css": "text/css", ".png": "image/png", ".ico": "image/x-icon"}


def _static(path: str):
    name = "play.html" if path in ("/", "/play") else path.lstrip("/")
    # 封面/局末链过去用的是短路径（真后端 server.py 的路由名），这里照抄一份
    name = {"/board": "board.html", "/admin": "admin.html",
            "/lab": "promptlab.html", "/play": "play.html"}.get(path, name)
    if "/" in name:
        return None
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

    def _send_md(self, text: str, code: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
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
        # 静态空路由（2026-10-27）：页面每次加载都会 ping 的两个口 —— 不给就
        # 每个 iframe 刷一条 404 刷屏控制台。/api/net 是诊断面板的手机列表，
        # play-keys 是封面的 key 回填（mock 不存 key，回空串即可）。
        if path == "/api/net":
            return self._send({"phones": []})
        if path == "/api/admin/play-keys":
            return self._send({"asr": "", "llm": "", "tts": ""})
        # 榜单与战报页（2026-10-27）：board.html 只吃这两个接口 —— 给假数据，
        # 黄子凡调「榜单 / 战报列表」不用等后端。
        if path == "/api/game/stats":
            return self._send({"by_level": {
                "娱乐": {"games": 12, "wolf_wins": 5, "good_wins": 7,
                         "wolf_win_rate": "41.7%", "avg_rounds": 3.2,
                         "human_seat_win_rate": "50.0%", "ai_seat_win_rate": "50.0%",
                         "human_exiled": 4, "ai_exiled": 8},
                "认真": {"games": 6, "wolf_wins": 4, "good_wins": 2,
                         "wolf_win_rate": "66.7%", "avg_rounds": 4.5,
                         "human_seat_win_rate": "33.3%", "ai_seat_win_rate": "66.7%",
                         "human_exiled": 3, "ai_exiled": 3}}})
        if path == "/api/game/games":
            return self._send({"games": [
                {"gid": "MOCKGID", "board": "6 人 · 2 狼 1 巫 1 预 2 民",
                 "level": "认真", "winner": "good", "rounds": 3,
                 "report": "/api/game/MOCKGID/report.md",
                 "replay": "/api/game/MOCKGID/replay"},
                {"gid": "OLDGID1", "board": "9 人 · 3 狼 3 神 3 民",
                 "level": "娱乐", "winner": "wolf", "rounds": 4,
                 "report": "/api/game/OLDGID1/report.md",
                 "replay": "/api/game/OLDGID1/replay"}]})
        # 战报正文（真后端是 text/markdown 纯文本，这里同格式）—— 局末那一排
        # 出口点下去要真能打开东西，不然等于没这个界面。
        if path.endswith("/report.md"):
            return self._send_md(_SAMPLE_REPORT)
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
