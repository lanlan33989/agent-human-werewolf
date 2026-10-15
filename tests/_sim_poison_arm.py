# -*- coding: utf-8 -*-
"""复现 bug2 v2：真实窗口（15/20s）+ 修好的音频队列模型。
每句：start = max(now, 上一句完 + delay)，finish = start + TTS。
SPOKE_DONE(t) = 正在念的行 {idx, at:0}；念完 → at=finish。"""
import os, sys, time
os.environ['MOCK'] = '1'; os.environ['MOCK_ASR'] = '1'
os.environ['MOCK_LLM'] = '1'; os.environ['MOCK_TTS'] = '1'
sys.path.insert(0, '.')
import game_api as _ga, night_script

night_script.set_win_scale(1.0)          # 真窗口 15/20 秒
TTS, POLL = 2.0, 0.5

def nightNodeIdx(nid, tl):
    if not nid:
        return -1
    for j, x in enumerate(tl):
        if x.get('node') == nid:
            return j
    return -1

def spoke_state(t, fins, started):
    """started = 已开念的最大行号；fins[i] = 行 i 的预计念完时刻。"""
    if started < 0:
        return None
    at = fins[started] if started < len(fins) else 0
    return {'idx': started, 'at': at if at <= t else 0}

def linePlayed(node, NOW, tl, sd):
    jn = -1
    if NOW and isinstance(NOW.get('panel_line'), int) and NOW['panel_line'] >= 0:
        jn = NOW['panel_line']
    elif node:
        jn = nightNodeIdx(node, tl)
    if jn < 0:
        return False, jn, 'jn<0'
    if not VOICE_ON:
        return True, jn, 'voice-off'
    if sd and sd['idx'] >= jn and sd['at'] > 0:
        return True, jn, 'spoken'
    last = (sd and sd['at']) or 0
    ok = bool(last and time.time() - last > 20)
    return ok, jn, 'bypass20' if ok else 'waiting(idx=%s at=%s)' % (sd and sd['idx'], sd and round(sd['at'] or 0, 1))

VOICE_ON = True
ARMED_SIG = None
fins, started = [], -1
play_head = 0.0

def arm_key(need, now, rnd):
    """tableui._armKey 的移植（2026-10-12 版：带 now.node）。"""
    n = (now and now.get('node')) or (need and need.get('node')) or ''
    return '%s:%s:%s' % (need.get('seat'), n, rnd)

seed = None
for k in range(1, 60):
    s = _ga.Session(humans=4, ais=5, seed=k)
    if any(p.kind == 'human' and p.role.value == 'witch' for p in s.game.players):
        seed = k
        break
print('human-witch seed =', seed, '（真窗口 15/20s，一夜约 70s）')
t0 = time.time()
while time.time() - t0 < 100:
    s.drive(0.1)
    pub = s.public_state()
    now = pub.get('now') or {}
    tl = pub.get('timeline') or []
    while started + 1 < len(tl):
        started += 1
        ln = tl[started]
        gap = ln.get('delay') or 0
        st = max(time.time(), play_head + (gap if started else 0))
        fin = st + TTS
        fins.append(fin)
        play_head = fin
    sd = spoke_state(time.time(), fins, started)
    NOW = now or None
    top = NOW.get('need') if NOW else None
    if NOW:
        _gateOk = (NOW.get('kind') == 'none'
                   or (NOW.get('kind') in ('wheel', 'shoot')
                       and linePlayed(NOW.get('node'), NOW, tl, sd)[0]))
    else:
        _gateOk = True
    armed_now, why = False, ''
    if (top and pub.get('phase') == 'night'
            and ARMED_SIG != arm_key(top, NOW, pub.get('round'))):
        _armNode = (NOW and NOW.get('node')) or top.get('node')
        done, _jn, why = linePlayed(_armNode, NOW, tl, sd) if NOW else (True, -1, '')
        if done:
            ARMED_SIG = arm_key(top, NOW, pub.get('round'))
            s.arm_window(_armNode or '')
            armed_now = True
    if NOW and NOW.get('node') == 'witch_poison':
        print('t=%5.1f started=%-5s gate=%-5s arm=%-1s %-9s ARMED=%s spoke.idx=%s/%s'
              % (time.time() - t0, NOW.get('started'), _gateOk,
                 'Y' if armed_now else '-', why, ARMED_SIG,
                 sd and sd['idx'], NOW.get('panel_line')))
    if pub.get('phase') != 'night' and time.time() - t0 > 5:
        print('夜结束 →', pub.get('phase'))
        break
    time.sleep(POLL)
