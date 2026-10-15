# -*- coding: utf-8 -*-
"""
一键启动 P0-talk（同时开 http 8000 与 https 8443）。

为什么需要它 —— 之前真实发生过的事故：
  先起过一个 8000 的服务（那时 .env 里 ASR 还是 mock），
  后来写了 .env 切换成真 ASR 又起了一个 8443 的服务。
  结果手机走 HTTPS 连到的仍是**旧进程**，页面一直显示「（mock）我是三号玩家…」，
  近场远场都"识别不出来"。用户排查了很久。

  根因不是配置错，是**同一份代码跑着两套配置的进程**。
  所以这个脚本做三件事：
    1. 启动前先自检：8000 / 8443 上现在跑着什么，是 mock 还是真 ASR
    2. 若占用者是**本项目**的旧进程，自动停掉；若是别人的程序，拒绝并说明
    3. 只起一个进程，同时监听 http 与 https，两边配置必然一致

用法：
    ./.venv/Scripts/python.exe start.py              # 检查 + 启动
    ./.venv/Scripts/python.exe start.py --doctor     # 只看现状，不启动
    ./.venv/Scripts/python.exe start.py --port-http 8000 --port-https 8443
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent


# ------------------------------------------------------------------ 端口检查

def _run(cmd: list[str]) -> str:
    """
    跑一个 Windows 命令并取回 stdout。

    注意：不能用 text=True —— 中文 Windows 的 netstat / wmic 输出是 GBK，
    按 UTF-8 解会在后台线程抛 UnicodeDecodeError（第一次实现就踩了）。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return ""
    raw = r.stdout or b""
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _listeners() -> list[tuple[int, int]]:
    """返回 [(port, pid)]，来自 netstat -ano。"""
    out: list[tuple[int, int]] = []
    for line in _run(["netstat", "-ano"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            addr = parts[1]
            if ":" not in addr:
                continue
            try:
                port = int(addr.rsplit(":", 1)[1])
                pid = int(parts[4])
            except ValueError:
                continue
            out.append((port, pid))
    return out


def _cmdline(pid: int) -> str:
    """
    取进程命令行，仅用于显示。

    两条路都试：新版 Windows 已移除 wmic（返回空），所以还要靠 PowerShell 兜底。
    真正决定「要不要停掉它」的不是命令行，而是 /api/health 的返回结构 ——
    见 report_and_clean。
    """
    txt = " ".join(_run(
        ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"]
    ).split())
    if txt and "CommandLine" != txt.strip():
        return txt
    ps = ("Get-CimInstance Win32_Process -Filter 'ProcessId=%d' "
          "| Select-Object -ExpandProperty CommandLine" % pid)
    return " ".join(_run(["powershell", "-NoProfile", "-Command", ps]).split())


def _probe(port: int, scheme: str) -> dict | None:
    """问一下这个端口上的服务，它的各层是真还是假。"""
    import ssl

    url = f"{scheme}://127.0.0.1:{port}/api/health"
    ctx = ssl._create_unverified_context() if scheme == "https" else None
    try:
        with urllib.request.urlopen(url, timeout=4, context=ctx) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def _is_ours(h: dict | None) -> bool:
    """
    判断某个端口上跑的是不是我们的服务。

    判据不是进程名，而是 /api/health 的返回结构 —— 只有我们这套服务会这样返回。
    这比查命令行可靠得多（wmic 在新 Windows 上已经没了）。

    必须同时认两种版本，否则恰好会把「旧版服务」认成别人的：
      · 新版：{mock, layers:{asr,llm,tts}, asr, llm, tts}
      · 旧版（分层开关加进来之前）：{mock, asr, llm, tts}   ← 就是这么漏掉的
    """
    if not h:
        return False
    keys = set(h)
    if "layers" in keys and isinstance(h.get("layers"), dict):
        return True
    return {"mock", "asr", "llm", "tts"} <= keys


def report_and_clean(ports: list[tuple[int, str]], force: bool) -> int:
    """
    ports = [(port, scheme)]。返回「确实挡住了启动、且我没处理」的端口数。

    第一版这里有个计数 bug：`blocked` 同时承担了「有几件事要处理」和
    「有几件事处理不了」两个含义，而 force 分支里对自己的服务走的是 `continue`
    且不加计数 —— 于是 force=True 时 blocked 恒为 0，
    提前 return，**该杀的旧进程一个都没杀**，8443 于是绑定失败。
    现在把「要杀的」单独记在 to_kill 里。
    """
    listeners = dict(_listeners())
    blocked = 0
    to_kill: list[tuple[int, int]] = []

    for port, scheme in ports:
        pid = listeners.get(port)
        if pid is None:
            print(f"  [{port}] 空闲")
            continue

        h = _probe(port, scheme)
        if _is_ours(h):
            layer = (h or {}).get("layers", {})
            asr = layer.get("asr") or {"impl": (h or {}).get("asr"), "mocked": None}
            state = "模拟(mock)" if asr.get("mocked") else (
                "真实" if asr.get("mocked") is False else "状态未知（旧版服务）")
            print(f"  [{port}] pid {pid}｜本项目的服务｜ASR = {asr.get('impl')}（{state}）")
            # 关键：这个旧进程的配置可能已经变了（我们就是这样被坑的）
            if force:
                print("        └─ 会先停掉它：同一份代码跑着两套配置，手机可能连到旧的那套")
                to_kill.append((port, pid))
            else:
                print("        └─ 需要停掉它（本次为只读模式，未动）")
                blocked += 1
            continue

        cmd = _cmdline(pid)
        print(f"  [{port}] pid {pid}｜已被占用，但不是本服务")
        if cmd:
            print(f"           {cmd[:110]}")
        print("        └─ 不会动它。换个端口（--port-http 8010 --port-https 8453）或手动关掉它")
        blocked += 1

    for port, pid in to_kill:
        _run(["taskkill", "/F", "/PID", str(pid)])
        print(f"  已停止 {port} 上的旧进程 pid {pid}")

    return blocked


# ------------------------------------------------------------------ 启动

def local_ips() -> list[str]:
    ips: set[str] = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:  # noqa: BLE001
        pass
    return sorted(i for i in ips if not i.startswith("127."))


async def _serve_one(server, label: str) -> None:
    """
    包一层，防止一个端口失败把另一个拖死。

    uvicorn 在绑定失败时会 `sys.exit(3)`（SystemExit），而 SystemExit 会穿过
    asyncio.gather 把整个事件循环带走 —— 实测里表现就是：
    8443 被旧进程占着 → 8443 绑定失败 → 连已经跑起来的 8000 一起挂了。
    """
    try:
        await server.serve()
    except SystemExit as e:
        print(f"[start] {label} 启动失败（端口可能被占用）：{e}", flush=True)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[start] {label} 异常退出：{type(e).__name__}: {e}", flush=True)


async def serve_both(port_http: int, port_https: int) -> None:
    import uvicorn

    from server import app  # 在这里 import：确保 .env 已被加载

    configs = [
        (uvicorn.Config(app, host="0.0.0.0", port=port_http, log_level="info"),
         f"http:{port_http}"),
        (uvicorn.Config(app, host="0.0.0.0", port=port_https, log_level="info",
                        ssl_certfile=str(BASE / "cert.pem"),
                        ssl_keyfile=str(BASE / "key.pem")),
         f"https:{port_https}"),
    ]
    servers = [uvicorn.Server(c) for c, _ in configs]
    # 只让一个 server 装信号处理，避免重复安装
    for s in servers[1:]:
        s.install_signal_handlers = lambda: None  # type: ignore[assignment]

    await asyncio.gather(*(_serve_one(s, lbl)
                           for s, (_c, lbl) in zip(servers, configs)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-http", type=int, default=8000)
    ap.add_argument("--port-https", type=int, default=8443)
    ap.add_argument("--doctor", action="store_true", help="只检查现状，不启动")
    ap.add_argument("--no-clean", action="store_true", help="不自动停掉本项目的旧进程")
    args = ap.parse_args()

    ports = [(args.port_http, "http"), (args.port_https, "https")]
    # 让 /api/net 知道 https 用的是哪个端口（诊断面板要把手机地址显示出来）。
    # 用 setdefault：万一外面显式设过了，以外面为准。
    os.environ.setdefault("HTTPS_PORT", str(args.port_https))

    print("启动前自检：")
    # --doctor 是只读的：绝不能顺手把别人的进程停掉
    left = report_and_clean(ports, force=(not args.no_clean) and not args.doctor)

    if args.doctor:
        print("\n（--doctor 模式，不启动）")
        return 0

    if left:
        print("\n❌ 有端口被别的程序占用，已中止。")
        print("   你可以：换个端口（--port-http 8010 --port-https 8453），")
        print("   或先关掉占用者再重试。")
        return 1

    # 证书：缺了就生成
    if not (BASE / "cert.pem").exists() or not (BASE / "key.pem").exists():
        import run_https
        ips = local_ips()
        run_https.make_cert(ips)
        print("已生成自签名证书（SAN 含 localhost / 127.0.0.1 / 本机 IP）")

    from server import ASR, LLM, TTS  # noqa: E402  仅用于打印配置

    def desc(o) -> str:
        return f"{type(o).__name__}{'（模拟）' if type(o).__name__.startswith('Mock') else ''}"

    print("\n本次装配：")
    print(f"  ASR = {desc(ASR)}")
    print(f"  LLM = {desc(LLM)}")
    print(f"  TTS = {desc(TTS)}")

    print("\n打开地址：")
    print(f"  本机（麦克风可用）：http://localhost:{args.port_http}")
    for ip in local_ips():
        print(f"  手机（远场测试走这条）：https://{ip}:{args.port_https}")
    print("  手机提示证书不受信任 → 选「继续访问」。\n")

    try:
        asyncio.run(serve_both(args.port_http, args.port_https))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
