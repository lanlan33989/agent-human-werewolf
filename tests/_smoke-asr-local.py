# -*- coding: utf-8 -*-
"""
真实本地 ASR 冒烟测试（可选）。

与 _smoke-talk.py 的分工：
  _smoke-talk.py      测接口层，强制 mock 环境，随时可跑
  本文件               测真实识别质量，需要本地模型（.env 的 ASR_MODEL_PATH 或已下好的缓存）

用法：
    ./.venv/Scripts/python.exe tests/_smoke-asr-local.py

它回答的问题：本地模型到底能不能用、中文和狼人杀黑话识别得怎么样、CPU 上多慢。
"""
import json
import os
import sys
import time
import wave

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/

# 这一层要真跑，覆盖掉可能存在的 mock 配置
os.environ["MOCK"] = "0"
os.environ["MOCK_ASR"] = "0"

import server  # noqa: E402

# (音频文件名, 原文, 必须命中的关键词)
CASES = [
    ("_test_zh1.wav", "我是三号玩家，昨晚我没有看到什么特别的。", ["玩家", "没"]),
    ("_test_zh2.wav", "我觉得五号有问题，他的发言前后矛盾，今天先把票归给他。", ["问题", "矛盾", "票"]),
    ("_test_zh3.wav", "金水是四号，查杀六号，我建议今天先出六号。", ["金水", "查杀"]),
    ("_test_zh4.wav", "昨晚是平安夜，女巫可能用了解药，大家先不要急着站边。", ["平安夜", "女巫", "解药"]),
]

print("\n=== 真实本地 ASR 冒烟测试 ===\n")
print("实现:", type(server.ASR).__name__)
print("模型:", getattr(server.ASR, "model_path", "") or getattr(server.ASR, "model_size", "-"))
print()

if os.path.exists("_test_zh1.wav"):
    with wave.open("_test_zh1.wav", "rb") as w:
        print(f"测试音频: {w.getframerate()} Hz / {w.getnchannels()} ch / "
              f"{w.getsampwidth() * 8} bit")
print()

if not hasattr(server.ASR, "transcribe"):
    print("ASR 未配置，跳过")
    sys.exit(1)

# 夹具缺失 = 测试失败，**不是跳过**。
# 原来这里是 `print(SKIP) + continue`，于是「把夹具删掉」表现为
# 「测试通过，但静默少跑 4 条」—— 正是 §5.3#1 要防的那种退化。
# 这 4 个 wav 是本测试唯一的夹具（`_probe_farfield_sim.py` 也在用），不能删。
_missing = [f for f, _o, _k in CASES if not os.path.exists(f)]
if _missing:
    print("FAIL  夹具缺失：" + "、".join(_missing))
    print("      它们是 _test_zh1~4.wav，本测试的夹具，不能删；")
    print("      少了就直接失败，而不是静默少跑几条。")
    sys.exit(1)

passed = failed = 0
for fname, origin, keywords in CASES:
    with open(fname, "rb") as f:
        data = f.read()
    t0 = time.perf_counter()
    try:
        text = server.ASR.transcribe(data, "audio/wav", 16000)
    except Exception as e:  # noqa: BLE001
        print(f"FAIL  {fname}  异常 {type(e).__name__}: {e}")
        failed += 1
        continue
    ms = (time.perf_counter() - t0) * 1000

    hit = [k for k in keywords if k in text]
    ok = len(hit) == len(keywords)
    print(("PASS  " if ok else "FAIL  ") + fname + f"  ({ms:.0f} ms)")
    print(f"      原文: {origin}")
    print(f"      转写: {text}")
    if not ok:
        print(f"      未命中关键词: {[k for k in keywords if k not in text]}")
    print()
    if ok:
        passed += 1
    else:
        failed += 1

print("=" * 52)
print("ALL GREEN (" + str(passed) + " passed)" if failed == 0
      else str(failed) + " FAILED / " + str(passed) + " passed")
print()
print("提醒：合成语音比真人清晰。这里通过只说明「模型可用」，")
print("      「远场可用」必须用真人真机测。")
sys.exit(1 if failed else 0)
