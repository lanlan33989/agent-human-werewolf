# -*- coding: utf-8 -*-
"""
D5 回归：语音接进对局（本地 TTS + 共享录音模块 + 半双工）

覆盖本轮新增：
  1. 本地 SAPI TTS 能真的合成出中文语音（不是蜂鸣）
  2. /api/tts 端点按速率参数合成，返回 audio/wav
  3. /rec.js 被正确伺服（两个页面共用一份录音实现，不许再各写一份）
  4. 前端半双工门控存在：AI 说话时不开始录音，播报期间靠自适应阈值捕捉插嘴
  5. 探针页不再复制录音/编码逻辑（防漂移）

跑法：
    ./.venv/Scripts/python.exe tests/_smoke-voice.py
"""
from __future__ import annotations

import io
import re
import sys
import wave
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []
SKIP = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if detail and not ok else ""))


def wav_info(data: bytes):
    with wave.open(io.BytesIO(data), "rb") as w:
        return {
            "rate": w.getframerate(),
            "ch": w.getnchannels(),
            "bits": w.getsampwidth() * 8,
            "seconds": round(w.getnframes() / w.getframerate(), 2),
        }


print("=" * 74)
print("① 本地 TTS（SAPI）")
import server  # noqa: E402

print(f"  装配：ASR={type(server.ASR).__name__}  LLM={type(server.LLM).__name__}  "
      f"TTS={type(server.TTS).__name__}")

if type(server.TTS).__name__ == "SapiTts":
    sentence = "请 3 号玩家发言。"
    data = server.TTS.synth(sentence)
    info = wav_info(data)
    check("SAPI 合成的是一段有效 WAV", len(data) > 8000, f"{len(data)} bytes")
    check("时长与这句话相称（0.5–6 秒）", 0.5 <= info["seconds"] <= 6.0, str(info))
    check("是单声道 16bit", info["ch"] == 1 and info["bits"] == 16, str(info))
    print(f"     实际：{info['seconds']}s / {info['rate']}Hz / {len(data)} bytes")

    # 不是蜂鸣：同一句话两次合成的波形应当一致，而不同句子应当不同
    a = server.TTS.synth("天黑请闭眼。")
    b = server.TTS.synth("天黑请闭眼。")
    c = server.TTS.synth("金水是四号，查杀六号。")
    check("同样的话合成结果稳定（同一引擎）", a == b, "两次不一致")
    check("不同的话合成结果不同（不是固定蜂鸣）", len(a) != len(c) or a != c)
    check("语速参数生效（rate 越大越短）",
          len(server.TTS.synth("请 3 号玩家发言。", rate=-4)) >
          len(server.TTS.synth("请 3 号玩家发言。", rate=6)))
elif isinstance(server.TTS, server.MockTts):
    SKIP.append("TTS 是 mock（MOCK_TTS=1）⇒ 没有真实合成可验，跳过")
else:
    # 云端 TTS（如 QwenTts）：**能验的和不能验的不一样**，所以另起一段而不是跳过。
    # ⚠️ 下面两条**故意不验**，因为它们对云端本来就不成立（套上去就是"用错的尺子量东西"）：
    #   · 「同一句话两次合成结果一致」—— 云端每次生成一个新文件，不保证逐字节相同
    #   · 「rate 越大越短」—— 云端这个参数有没有用取决于模型（qwen3-tts-flash 不接 rate）
    sentence = "请 3 号玩家发言。"
    data = server.TTS.synth(sentence)
    info = wav_info(data)
    check(f"云端 TTS（{type(server.TTS).__name__}）合成的是有效 WAV",
          len(data) > 8000, f"{len(data)} bytes")
    check("时长与这句话相称（0.5–6 秒）", 0.5 <= info["seconds"] <= 6.0, str(info))
    check("是单声道 16bit", info["ch"] == 1 and info["bits"] == 16, str(info))
    print(f"     实际：{info['seconds']}s / {info['rate']}Hz / {len(data)} bytes")
    c2 = server.TTS.synth("金水是四号，查杀六号。")
    check("不同的话合成结果不同（不是固定音频）", data != c2,
          f"两句长度 {len(data)} / {len(c2)}")
    # 音色：**多音色是这次接入的主要目的**，所以这里必须有断言（换个音色得真的不一样）
    v1 = server.TTS.synth(sentence, voice="Neil")
    v2 = server.TTS.synth(sentence, voice="Moon")
    check("★ 指定不同音色 ⇒ 合成结果不同（多音色真的生效，而不是被忽略）",
          v1 != v2 and len(v1) > 8000 and len(v2) > 8000,
          f"Neil {len(v1)} 字节 / Moon {len(v2)} 字节")

print("=" * 74)
print("② /api/tts 与 /rec.js 端点")
from fastapi.testclient import TestClient  # noqa: E402

c = TestClient(server.app)

r = c.get("/rec.js")
check("/rec.js 返回 JS", r.status_code == 200 and "javascript" in r.headers.get("content-type", ""),
      f"{r.status_code} {r.headers.get('content-type')}")
check("/rec.js 内容含 Rec 模块", "window.Rec" in r.text and "getUserMedia" in r.text)

r = c.get("/api/tts", params={"text": "我是三号玩家", "rate": 3})
check("/api/tts 返回 audio/wav", r.status_code == 200 and r.headers["content-type"] == "audio/wav",
      f"{r.status_code} {r.headers.get('content-type')}")
if r.status_code == 200 and len(r.content) > 1000:
    info = wav_info(r.content)
    check("/api/tts 音频可解析", info["seconds"] > 0.3, str(info))
else:
    check("/api/tts 音频可解析", False, f"{len(r.content)} bytes")

r = c.get("/api/tts", params={"text": "   "})
check("空文本不报错、返回空音频", r.status_code == 200 and r.content == b"",
      f"{r.status_code} {len(r.content)}B")

print("=" * 74)
print("③ 前端：半双工与共享模块（静态检查）")
# 2026-10-06：game.html（/game 旧界面）已删除。原 game 断言逐条甄别
# （详见 HANDOFF §10.7 2026-10-06）：前四条改指向 p2（play.html+tableui.js）；
# 其余五条（插嘴采集/自适应阈值/串音提示/法官语速/播报开关）未移植到
# play.html 的大圆形态，随页面删除退役。
p2 = ((BASE / "play.html").read_text(encoding="utf-8")
      + (BASE / "tableui.js").read_text(encoding="utf-8")
      # 2026-10-07：播报队列 + 夜间门控拆去了 speech_ui.js（1900 行硬线，见 §2.1.4）
      # ⇒ 扫描的**文件清单跟着走**（判据一字未改、强度不变；同 §10.7 2026-10-02 那次）。
      + (BASE / "speech_ui.js").read_text(encoding="utf-8"))
# 🔴 2026-10-23：探针页 index.html 已删（用户：非游戏画面）—— 它的 5 条判据随之退役

# 🔴 2026-10-07（`tableui.js → speech_ui.js` 拆分时踩到的坑，值得单独上锁）：
#    本站静态资源是 server.py 里**一个文件一条路由**（不是 StaticFiles 挂目录）——
#    拆出 / 新增 js 时，极易只改 `play.html` 而忘了补路由 ⇒ 浏览器静默 404、
#    页面少一整块功能（那次差点让整块播报队列消失，且**所有 smoke 都不会红**）。
_srv = (BASE / "server.py").read_text(encoding="utf-8")
_refs = set(re.findall(r'<script src="/([^"]+\.js)"', p2))
check("★ 每个被 play.html 引用的 js 都有 server.py 路由（拆文件时最容易漏的一条）",
      bool(_refs) and all('"/%s"' % s in _srv for s in _refs),
      "被引用：" + str(sorted(_refs)))
check("play.html 引用了共享录音模块", '<script src="/rec.js"></script>' in p2)

# 录音上行只该跑需要的那一段：
#   对局界面只要转写（识别完直接提交），带 asrOnly 免得白跑一次 LLM + TTS
check("play.html 的录音只走 ASR（不白跑 LLM / TTS）",
      "Rec.transcribe(r.blob, { asrOnly: true })" in p2)

check("play.html：有麦克风入口（#hoSay，大圆发言模式复用）",
      "hoSay" in p2 and "startSay" in p2)
check("play.html：时间线播报有并发保护（半双工）",
      "if (SPEAKING || !VOICE_ON" in p2)

print("=" * 74)
print("④ 新对局页 /play（界面照 demo/index.html，数据走真引擎）")

r = c.get("/table.css")
check("/table.css 返回 CSS 且非空", r.status_code == 200
      and "text/css" in r.headers.get("content-type", "") and ".seat" in r.text,
      f"{r.status_code} {len(r.text)}B")
r = c.get("/table.js")
check("/table.js 返回 JS 且带排座算法",
      r.status_code == 200 and "function layout(" in r.text
      and "function equalChord(" in r.text,
      f"{r.status_code} {len(r.text)}B")
r = c.get("/play")
# 🔴 2026-10-23 修正陈旧判据：日志区已于 2026-10-12 在所有页面彻底删除
#    （play.html renderLog 注释：公有信息以法官播报为准），#logbox 元素早就不在了。
check("/play 返回 HTML 且照 demo 的牌桌结构", r.status_code == 200
      and 'id="p4"' in r.text and 'id="tablewrap"' in r.text,
      str(r.status_code))
check("play.html 引用了共享的样式 / 排座 / 录音模块",
      '/table.css' in r.text and '/table.js' in r.text and '/rec.js' in r.text)
check("play.html 的数据只来自引擎（没有把 demo 的剧本函数搬过来）",
      '/api/game/new' in r.text and 'function buildScript' not in r.text)

# ★ 防漂移：table.js 的排座算法必须与 demo/index.html 里那一段**逐字相同**。
# 否则「界面以 demo 为绝对参照」这句话就会随着时间默默失效。
demo_src = (BASE.parent / "demo" / "index.html").read_text(encoding="utf-8")
table_src = (BASE / "table.js").read_text(encoding="utf-8")


def _one_function(src, name):
    i = src.index("function " + name + "(")
    depth = 0
    j = i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[i:j + 1]


check("★ table.js 的 equalChord 与 demo 逐字一致（防两边慢慢漂开）",
      _one_function(table_src, "equalChord") == _one_function(demo_src, "equalChord"))
check("★ table.js 的 layout 与 demo 逐字一致",
      _one_function(table_src, "layout") == _one_function(demo_src, "layout"))

print("=" * 74)
total = len(PASS) + len(FAIL)
print(f"结果：{len(PASS)}/{total} 通过" + (f"（跳过 {len(SKIP)} 项）" if SKIP else ""))
for s in SKIP:
    print(f"  -- 跳过：{s}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过")
