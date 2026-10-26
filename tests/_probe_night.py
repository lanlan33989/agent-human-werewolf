import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
_s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))), "backend"))  # 2026-10-26 拆分：py 在 backend/  # 仓库根目录入 path：保证 python tests/xxx.py 可直接跑
# -*- coding: utf-8 -*-
p = 'tableui.js'
s = open(p, encoding='utf-8', newline='').read()
old = """      else if (WHEEL.act === 'kill' || WHEEL.act === 'guard'
               || WHEEL.act === 'investigate' || WHEEL.act === 'poison'){
        if (WHEEL.pick != null){ submit(seatNo, WHEEL.act, WHEEL.pick, null); }
        else if (WHEEL.act === 'kill'){ submit(seatNo, 'kill', -1, null); }   /* 没点 = 空刀 */
        else { submit(seatNo, 'pass', null, null); }                          /* 没点 = 默认放弃 */
      }"""
nl = '\r\n' if '\r\n' in s[:3000] else '\n'
old = old.replace('\n', nl)
assert s.count(old) == 1
new = ("""      else if (WHEEL.act === 'kill' || WHEEL.act === 'guard'
               || WHEEL.act === 'investigate' || WHEEL.act === 'poison'){
        if (WHEEL.pick != null){ submit(seatNo, WHEEL.act, WHEEL.pick, null); }
        else if (WHEEL.act !== 'kill'){ submit(seatNo, 'pass', null, null); }
        /* 🔴 kill 没选中时**不提交** —— 交给服务端「跟随 AI 狼」的收割逻辑
           （2026-10-07 用户定：人狼没决策跟 AI；前端抢发空刀会绕过它）。 */
      }""").replace('\n', nl)
s = s.replace(old, new)
open(p, 'wb').write(s.encode('utf-8'))
print('kill follow-ai ok')
