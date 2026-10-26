# -*- coding: utf-8 -*-
"""lab_api 的自检：落盘 / 暂停门 / 单步额度 / 步骤查询。不依赖网络与模型。

为什么单独一个脚本：这个模块管的是"实验能不能回放/回退"，一旦坏了
看不出来（对局照跑，只是实验室没原料），所以要有一条能一键验证的线。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))          # 仓库根（lab_api.py 在那儿）
import lab_api  # noqa: E402

ok = []


def chk(name, cond, extra=""):
    ok.append((("PASS" if cond else "FAIL"), name, extra))
    print(("PASS " if cond else "FAIL ") + name + ("  " + str(extra) if extra else ""))


# 干净起步
lab_api.clear_steps()
lab_api.set_paused(False)

# 1) 落盘
lab_api.set_ctx(round=2, phase="DAY_VOTE", seat=3, role="狼人")
lab_api.log_call(model="m1", messages=[
    {"role": "system", "content": "SYS-A"},
    {"role": "user", "content": "USER-A"},
], raw='{"target": 4}', ms=1234, ok=True)
lab_api.log_call(model="m1", messages=[{"role": "user", "content": "U"}],
                 raw="", ms=900, ok=False, err="timeout")
rows = lab_api.read_steps()
chk("落盘两行", len(rows) == 2, len(rows))
chk("成功步 raw 正确", rows[0]["raw"] == '{"target": 4}', rows[0]["raw"])
chk("提示词快照有 system", rows[0]["prompt"]["system"] == "SYS-A")
chk("提示词快照有 user", rows[0]["prompt"]["user"] == "USER-A")
chk("上下文打上去了", rows[0]["round"] == 2 and rows[0]["phase"] == "DAY_VOTE")
chk("失败步标记兜底", rows[1]["fallback"] is True and rows[1]["err"] == "timeout")

# 2) 暂停门：默认不停
chk("默认不暂停", lab_api.gate() is False)

# 3) 暂停后停住
lab_api.set_paused(True)
chk("暂停后 gate=True", lab_api.gate() is True)
chk("暂停后仍然停住", lab_api.gate() is True)

# 4) 单步：额度在**步完成**时才扣（gate 只放行不扣）——
#    一步可能中途多次返回 working（预跑等待/夜钟补播），在 gate 扣会退化成半步
lab_api.request_step(1)
chk("单步有额度时放行", lab_api.gate() is False)
chk("working 半步不扣额度（仍放行）", lab_api.gate() is False)
chk("额度还没被 gate 吃掉", lab_api._read_control()["budget"] == 1)
lab_api.step_done()
chk("步完成后重新停住", lab_api.gate() is True)

# 5) 继续 → 额度清零、不再停
lab_api.set_paused(False)
chk("继续后不暂停", lab_api.gate() is False)
st = lab_api._read_control()
chk("继续后额度清零", st["budget"] == 0, st)

# 6) 关掉落盘后不再写
os.environ["LAB_LOG"] = "0"
n0 = len(lab_api.read_steps())
lab_api.log_call(model="m1", messages=[], raw="x", ms=1, ok=True)
chk("LAB_LOG=0 时不落盘", len(lab_api.read_steps()) == n0)
os.environ["LAB_LOG"] = "1"

# 7) 路由存在
paths = sorted({r.path for r in lab_api.router.routes})
chk("路由齐全", all(any(p.endswith(x) for p in paths)
                    for x in ["/status", "/pause", "/step", "/steps",
                              "/live", "/snapshots", "/rewind"]), paths)

lab_api.clear_steps()
bad = [x for x in ok if x[0] == "FAIL"]
print("\n通过 %d / %d，失败 %d" % (len(ok) - len(bad), len(ok), len(bad)))
sys.exit(1 if bad else 0)
