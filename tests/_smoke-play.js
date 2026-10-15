/* ============================================================
   play.html 的「真的走一遍」回归 —— 开一个真 Chrome，真的点。

   为什么必须有这个（血泪）：
     · 漏绑一个按钮的 onclick   → 整局卡在身份牌那一步
     · 座位号差一位（1 号算成 -1）→ 服务端 400
     · 错误提示写在被浮层盖住的 #foot → 玩家什么都看不到，界面上按钮还在，
       点一次不动、点一次不动，表现出来就是「反复卡在同一步」
   这三件事，**打 API 测不出来、假 DOM 也测不出来** —— 只有真的点一遍才知道。
   前两个就是这么被用户点出来的。

   ⚠️ 这个脚本会连开几局，直到**真的覆盖到「夜间的特殊角色」**（验人 / 用药）。
   只跑一局的话，那个角色可能是 AI，断言就会假绿通过（§5.3 第 3 条要防的正是这个）。
   ⚠️ 2026-09-15：**不写死是"预言家"** —— 改组局规则后 4~8 人局只有女巫、没有预言家，
   写死预言家等于写了一条永远不可能通过的断言。

   前置条件：
     · 服务在 http://127.0.0.1:8000 上跑着（先跑 start.py）
     · 本机有 Chrome（自动查常见安装位置，或用 CHROME_PATH 指）

   退出码（三个必须分得清）：
     0 = 跑了，全过
     1 = 跑了，有失败
     2 = **没跑成**（服务不在 / 没 Chrome / Chrome 起不来）
   ⚠️ 原来写的是「缺任一个就 SKIP，不污染常规基线」，于是"没跑"和"跑过了"
      在退出码上完全一样（都是 0）—— 和 _smoke-asr-local.py 的夹具守卫
      是同一类退化（§5.3#1 要防的正是这个）。

   跑法：
     node _smoke-play.js
     node _smoke-play.js --shots     # 顺手把每一步截图存到 %TEMP%\tt-shots
   ============================================================ */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');

/* 收工要把这次起的 headless Chrome 关掉。
   不关的后果是实测过的：上一次积了 **36 个** 残留 Chrome，它们一直轮询 /public
   （日志里 36047 次），把服务端 SESSIONS 挤爆，**真人的页面随即开始拿 404 然后冻住**。
   所以"能跑通"包括"跑完不留垃圾"。 */
let chromePid = null;
function killChrome(){
  if (!chromePid) return;
  try {
    execFileSync('taskkill', ['/PID', String(chromePid), '/T', '/F'], { stdio: 'ignore' });
  } catch (e) { /* 已经没了就算了 */ }
  chromePid = null;
}
process.on('exit', killChrome);
process.on('SIGINT', () => { killChrome(); process.exit(130); });

const BASE = process.env.PLAY_BASE || 'http://127.0.0.1:8000';
const CDP_PORT = Number(process.env.CDP_PORT || 9333);
/* 找 Chrome：CHROME_PATH > 几个常见安装位置。
   原来只写死了本机那一个绝对路径 —— 换台机器就找不到，而且**静默 SKIP**。 */
const CHROME = (() => {
  const cands = [
    process.env.CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    path.join(process.env.LOCALAPPDATA || '', 'Google', 'Chrome', 'Application', 'chrome.exe'),
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
  ].filter(Boolean);
  for (const p of cands) { try { if (fs.existsSync(p)) return p; } catch (e) { /* 试下一个 */ } }
  return cands[1];   // 都没找到：保留原路径，交给后面的检查给出明确报错
})();
const SHOTS = process.argv.includes('--shots');
const MAX_GAMES = Number(process.env.PLAY_GAMES || 5);
/* 走查用的人数。默认 3 真人 + 1 AI（覆盖率理由见下面那段），
   但**可以用环境变量换成别的配置**，比如按页面的默认值 2+2 验一遍：
     $env:PLAY_HUMANS='2'; $env:PLAY_AI='2'; node _smoke-play.js */
const NH = Number(process.env.PLAY_HUMANS || 3);
const NA = Number(process.env.PLAY_AI || 1);
/* 视口尺寸。默认 430x900（手机）。**矮窗口要单独跑** ——
   "按钮被顶到屏幕外" 那类 bug 只有在窗口不够高时才出现：
     $env:PLAY_H='620'; node _smoke-play.js */
const WIN_W = Number(process.env.PLAY_W || 430);
const WIN_H = Number(process.env.PLAY_H || 900);
const SHOT_DIR = path.join(process.env.TEMP || '.', 'tt-shots');

const PASS = [], FAIL = [];
function check(name, ok, detail) {
  (ok ? PASS : FAIL).push(name);
  console.log(`  ${ok ? 'OK  ' : 'FAIL'} ${name}${(!ok && detail) ? '  <- ' + detail : ''}`);
}
const log = s => console.log('       ' + s);
const sleep = ms => new Promise(r => setTimeout(r, ms));

function get(url) {
  return new Promise((resolve, reject) => {
    const req = http.get(url, r => {
      let b = '';
      r.on('data', c => (b += c));
      r.on('end', () => resolve({ code: r.statusCode, body: b }));
    });
    req.on('error', reject);
    req.setTimeout(4000, () => req.destroy(new Error('timeout')));
  });
}

/* ---------------- CDP 最小客户端（零依赖，Node 自带 WebSocket） ---------------- */
let msgId = 0;
function makeSend(ws) {
  const pending = new Map();
  ws.addEventListener('message', ev => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.id && pending.has(m.id)) {
      const p = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) p.rej(new Error(JSON.stringify(m.error))); else p.res(m.result);
    }
  });
  return (method, params, sessionId) => new Promise((res, rej) => {
    const id = ++msgId;
    pending.set(id, { res, rej });
    const msg = { id, method, params: params || {} };
    if (sessionId) msg.sessionId = sessionId;
    ws.send(JSON.stringify(msg));
  });
}
function connect(url) {
  return new Promise((res, rej) => {
    const ws = new WebSocket(url);
    ws.addEventListener('open', () => res(ws));
    ws.addEventListener('error', () => rej(new Error('ws error')));
  });
}

(async () => {
  /* 1. 服务在不在 */
  try {
    const h = await get(BASE + '/api/health');
    if (h.code !== 200) throw new Error('HTTP ' + h.code);
    log('服务在：' + BASE);
  } catch (e) {
    console.log('没跑成：服务不在 ' + BASE + ' 上（先跑 start.py）');
    console.log('      ⇒ 这次走查**没有发生**，退出码 2（不是通过）。');
    process.exit(2);
  }

  /* 2. Chrome 在不在 */
  let ver = null;
  try { ver = JSON.parse((await get(`http://127.0.0.1:${CDP_PORT}/json/version`)).body); } catch (e) { /* 没起 */ }
  if (!ver) {
    if (!CHROME || !fs.existsSync(CHROME)) {
      console.log('没跑成：找不到 Chrome（用 CHROME_PATH 指一下）');
      console.log('      ⇒ 这次走查**没有发生**，退出码 2（不是通过）。');
      process.exit(2);
    }
    /* 🔴 2026-09-21 必须加下面两个开关，否则**脚本会静默挂死**（实测）：
       headless Chrome 里没有用户手势、也没放开自动播放策略 ⇒ AudioContext 恒为
       suspended，而 Rec.ensure() 第 71 行是 `await ctx.resume()` —— 那个 promise
       **永远不 resolve**（Chrome 的手势门槛），于是 CDP 的 evaluate 一直挂着：
       不报错、不退出、日志一行不涨。这正是 §9.4 那 8 条"从没跑过"的原因 ——
       谁去跑都卡在这里，拿不到红也拿不到绿。
       ⚠️ 代价要说清楚：放开自动播放策略之后，"解锁后必须是 running"那条
          （批十的第 3 条）**证据力被削弱** —— 政策放开后本来就会 running。
          它仍然能证明 unlock() 这条调用链没抛异常，但不再能证明"手机上手势解锁真的生效"。
          那一条最终仍需真机确认（见 §9.4 未验证清单）。 */
    const _cp = spawn(CHROME, ['--headless=new', `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${path.join(process.env.TEMP, 'tt-chrome')}`,
      '--no-first-run', '--no-default-browser-check', '--disable-gpu',
      '--autoplay-policy=no-user-gesture-required',   // 见上：不加就卡在 ctx.resume()
      '--use-fake-ui-for-media-stream',               // headless 里没有授权弹窗，直接放行
      /* ⚠️ 这里**故意不**用 `--use-file-for-fake-audio-capture`。
         2026-09-30 实测过：在本机这个 Chrome 上它**不喂声音** —— 录到的音频
         `peak = 0 / rms = 0.0`（整段全零），而界面表现是"没听清"，
         看起来像识别坏了，非常费时间（我在这上面绕了两轮）。
         现在改成"自己搭一条假麦克风"：见下面「真语音端到端」那块 ——
         解码 `_test_zh3.wav` → AudioBufferSourceNode → MediaStreamDestination
         → 替换 `getUserMedia`。那条路不依赖任何 Chrome 实现细节，
         而且**能断言喂进去没有**（录制结果里能量必须非零）。 */
      `--window-size=${WIN_W},${WIN_H}`, 'about:blank'],
      { detached: true, stdio: 'ignore' });
    chromePid = _cp.pid;            /* 记下来，退出时连子树一起收掉 */
    _cp.unref();
    for (let i = 0; i < 24 && !ver; i++) {
      await sleep(700);
      try { ver = JSON.parse((await get(`http://127.0.0.1:${CDP_PORT}/json/version`)).body); } catch (e) { /* 再等 */ }
    }
  }
  if (!ver) {
    console.log('没跑成：Chrome 起不来（可能被应用控制策略拦了）');
    console.log('      ⇒ 这次走查**没有发生**，退出码 2（不是通过）。');
    process.exit(2);
  }
  log('Chrome ' + ver.Browser);

  const ws = await connect(ver.webSocketDebuggerUrl);
  const send = makeSend(ws);
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Page.enable', {}, sessionId);
  await send('Runtime.enable', {}, sessionId);

  const errors = [];
  ws.addEventListener('message', ev => {
    let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.method === 'Runtime.exceptionThrown') {
      errors.push((m.params.exceptionDetails.exception || {}).description
        || m.params.exceptionDetails.text);
    }
    /* ⚠️ 2026-09-29：页面弹 alert / confirm 时，CDP 的 evaluate **一直不返回**
       —— JS 被模态框挡住了。表现就是"卡住"，最后被 ev2 的 90s 超时兜住（实测：
       卡在 hoDo 的「先点一个座位」上，因为那一步的前置条件没满足）。
       ⇒ 这里自动点掉它，并**记一笔警告**：弹框本身不是正常现象，它说明有一步
          没按预期走完（而不是"用户看到提示就好了"）。 */
    if (m.method === 'Page.javascriptDialogOpening') {
      log('!! 页面弹了 ' + m.params.type + '：' + String(m.params.message).slice(0, 70)
          + '（已自动点掉；这说明某一步的前置条件没满足）');
      send('Page.handleJavaScriptDialog', { accept: true }, sessionId).catch(() => {});
    }
  });

  /* ⚠️ evaluate 必须带超时（2026-09-21 加）。
     实测踩过：页面里只要有一个 await 永不 resolve（那次是 Rec.ensure() 里的
     ctx.resume()），CDP 的 evaluate 就一直挂着 —— 脚本**既不失败也不退出**，
     日志一行不涨。那比"失败"更糟：看不出是卡住了还是还在慢慢跑。
     现在超时直接抛，由最外层 catch 收成 exit 2，并打印是哪一句卡住的。 */
  const EVAL_TIMEOUT = Number(process.env.PLAY_EVAL_TIMEOUT || 90000);
  const ev2 = async expr => {
    const r = await Promise.race([
      send('Runtime.evaluate',
        { expression: expr, returnByValue: true, awaitPromise: true }, sessionId),
      new Promise((_, rej) => setTimeout(() => rej(new Error(
        'evaluate 超时 ' + Math.round(EVAL_TIMEOUT / 1000) + 's'
        + '（页面里可能有 await 永不 resolve）:: ' + expr.slice(0, 80))), EVAL_TIMEOUT)),
    ]);
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + ' :: ' + expr.slice(0, 60));
    return r.result.value;
  };

  /* 等页面把手里那个动作提交完（真模型 2~3 秒，慢的十几秒）。
     不等就会连点 —— 那会造出「假重复」，把走查脚本自己的节奏问题
     误报成页面 bug（2026-09-15 真模型下就是这么发生的）。 */
  const waitIdle = async ms => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      await sleep(300);
      if (!(await ev2('(typeof BUSY !== "undefined") ? !!BUSY : false'))) return true;
    }
    log('!! 等了 ' + Math.round(ms / 1000) + ' 秒，页面仍在 BUSY');
    return false;
  };
  let shotN = 0;
  const shot = async name => {
    if (!SHOTS) return;
    if (!fs.existsSync(SHOT_DIR)) fs.mkdirSync(SHOT_DIR, { recursive: true });
    const { data } = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
    const f = path.join(SHOT_DIR, `${String(++shotN).padStart(2, '0')}-${name}.png`);
    fs.writeFileSync(f, Buffer.from(data, 'base64'));
  };

  console.log('\n=== play.html 走一遍（真 Chrome） ===\n');
  /* ✅ 2026-10-01：**不再需要**"每次进页面前把 tt.gid 清掉"那段注入了。
     它当年（09-21）是为了防"页面一加载就自动把上一轮那局续上"，导致封面 #p0 被收起、
     「封面控件够得着」那条随机变红。现在页面**永远停在封面**（要接回上一局得用户自己
     按「继续上一局」），所以封面必然是干净的 —— 清锚点反而会挡住
     「刷新之后出现接回按钮」那条新断言（实测：就是它把锚点清了，按钮才不出现）。 */
  await send('Page.navigate', { url: BASE + '/play' }, sessionId);
  await sleep(2600);
  log('装配：' + (await ev2(`document.getElementById('health').textContent`)));
  await ev2('VOICE_ON = false');                    /* headless 没声卡，关掉播报省时间 */
  await shot('01-封面');

  /* ---------------- 封面：每个控件都必须**够得着** ----------------
     2026-09-19 用户手机报障：「真人/AI 人数、开始一局的按钮全没了（裁剪异常）」。
     原因不是没渲染，是被**裁掉**：`#phone` 是固定高度的 overflow:hidden 列，
     而 `#p0` 又是 `justify-content:center` ⇒ 内容一旦比屏幕高，**上下同时被切**。
     批八/批九往封面加了档位 / AI 性格 / 规则开关三组控件（规则开关由后端表生成，
     条目只会更多），正好把它顶超了。
     ⚠️ 这条断言**必须按矮屏跑才有意义**：`$env:PLAY_H='620'; node _smoke-play.js`
        （默认 430x900 装得下 ⇒ 永远测不出来，这也正是它漏到今天的原因。） */
  const reach = await ev2(`(async function(){
    var ids = ['nHuman','nAi','levelRow','personaRow','switchRow','btnNew','btnDiag0'];
    var p0 = document.getElementById('p0');
    var out = { vh: window.innerHeight, vw: window.innerWidth,
                overflowY: getComputedStyle(p0).overflowY,
                scrollH: p0.scrollHeight, clientH: p0.clientHeight, unreachable: [] };
    for (var i = 0; i < ids.length; i++) {
      var el = document.getElementById(ids[i]);
      if (!el) { out.unreachable.push(ids[i] + '(不存在)'); continue; }
      el.scrollIntoView({ block: 'nearest' });
      await new Promise(function(x){ setTimeout(x, 120); });
      var r = el.getBoundingClientRect();
      if (!(r.height > 0) || r.bottom < 1 || r.top > window.innerHeight - 1) {
        out.unreachable.push(ids[i] + '(' + Math.round(r.top) + '~' + Math.round(r.bottom) + ')');
      }
    }
    p0.scrollTop = 0;
    await new Promise(function(x){ setTimeout(x, 150); });
    return out;
  })()`);
  check('★ 封面上的每个控件都够得着（矮屏被裁掉 = 连一局都开不起来）',
    reach.unreachable.length === 0, JSON.stringify(reach).slice(0, 220));
  check('……封面能滚：内容高于屏幕时要滚得动，而不是被 overflow:hidden 裁掉',
    reach.scrollH <= reach.clientH + 2 || /auto|scroll/.test(String(reach.overflowY)),
    JSON.stringify(reach).slice(0, 220));

  /* 座次编排器（2026-10-01）：默认 = 真人首位 + 均匀隔 AI；改人数自动重排；
     点「恢复推荐排位」能还原 —— 前端算法必须与后端 default_layout 一致。 */
  const plan = await ev2(`(function(){
    document.getElementById('nHuman').value = 3;
    document.getElementById('nAi').value = 6;
    if (typeof renderRules === 'function') renderRules();
    var chips = document.querySelectorAll('#planRow .planChip');
    var seq = [];
    for (var i = 0; i < chips.length; i++) seq.push(chips[i].className.indexOf('ph') >= 0 ? 1 : 0);
    var afterDefault = null;
    var b = document.getElementById('planDefault');
    if (b){ b.click(); var c2 = document.querySelectorAll('#planRow .planChip');
            afterDefault = []; for (var j = 0; j < c2.length; j++) afterDefault.push(c2[j].className.indexOf('ph') >= 0 ? 1 : 0); }
    return { n: chips.length, seq: seq, afterDefault: afterDefault,
            hasBtn: !!b, planLen: (typeof PLAN !== 'undefined') ? PLAN.length : -1 };
  })()`);
  check('★ 座次编排器：改人数自动生成推荐排位（3真6AI → 人AA人AA人AA，9 个座位）',
    plan.n === 9 && plan.seq.join('') === '100100100' && plan.planLen === 9,
    JSON.stringify(plan).slice(0, 190));
  check('★ 「恢复推荐排位」能把顺序还原成推荐排位（拖乱之后有出路）',
    plan.hasBtn && plan.afterDefault && plan.afterDefault.join('') === '100100100',
    JSON.stringify(plan.afterDefault));

  const diagCover = await ev2(`(function(){
    var b = document.getElementById('btnDiag0');
    if (!b) return { no: true };
    b.click();
    var p = document.getElementById('diagPanel');
    var r = p.getBoundingClientRect();
    var out = { on: p.classList.contains('on'),
                inView: r.top >= -1 && r.bottom <= window.innerHeight + 1 && r.height > 100,
                text: (p.textContent || '').slice(0, 80) };
    var c = document.getElementById('diagClose');
    if (c) c.click();
    out.closed = !p.classList.contains('on');
    return out;
  })()`);
  check('★ 封面也能打开诊断面板（连一局都开不起来时，这是唯一的排查手段）',
    !!diagCover.on && !!diagCover.inView && !!diagCover.closed && !diagCover.no,
    JSON.stringify(diagCover).slice(0, 190));

  /* ---------------- 连开几局，直到覆盖到预言家验人 ---------------- */
  const trace = [];
  let step = 0, cards = 0, games = 0;
  let maxSame = 0, lastKey = '', sameKeyRuns = 0;
  let privClicks = 0, privSawResult = 0, endedGames = 0;
  /* 动作按钮"看不见"的次数。上一版把 #hoActs 塞进 overflow:auto 的容器里，
     投票按钮被挤出滚动区 ⇒ 用户看到"投票界面不出现"。
     ⚠️ **DOM 里存在 ≠ 看得见** —— 所以要量它们的矩形。 */
  let invisibleBtns = 0;
  /* 这一局的牌发了没、人类里有没有神职 —— 用来自动重开，避免覆盖率靠运气 */
  let cardsThisGame = 0, humanGotSpecial = false;
  let lastPhase = '';
  /* 🔴 2026-10-07：大圆发言的**识别失败计数**（见下面 s.spk 分支）。
     headless Chrome 在本机**没有真麦克风** ⇒ 录到的是静音 ⇒ 本地 whisper 返回空
     ⇒ 页面停在「没听清，直接打字后点『说这句』」不提交 ⇒ walker 在同一屏无限
     点下去（实测：一局里 261 次 /api/utter?only=asr、**0 次 /act**，20 分钟走不动）。
     这不是页面的 bug（真人说话就能提交），但会让走查在这一台机器上**永远跑不完一局**。
     所以：连续 2 次识别失败就点「过」把这一回合推过去（和真人按「过」等价），
     第一次先让它自己重试一遍 —— 免得把"识别真的坏了"这种回归一起盖掉。 */
  let spkFail = 0, spkSeat = 0;
  const badTargets = [];        /* 出现在「投给谁能点」列表里、但其实不该有的座位 */

  const state = () => ev2(`(function(){
    var h = document.getElementById('handoff');
    return {
      phase: (typeof PUBLIC !== 'undefined' && PUBLIC) ? PUBLIC.phase : '',
      p3: document.getElementById('p3').classList.contains('on'),
      p4: document.getElementById('p4').classList.contains('on'),
      hand: h.classList.contains('on'),
      /* 2026-10-05 轮盘改版：hoWho/hoTake/hoConfirm/hoDecline/hoGo 全部并入
         轮盘状态机 —— who 读大圆中心，take/cfm/decline 读 WHEEL.mode/act。 */
      who: (document.getElementById('bigWho')||{}).textContent || '',
      tip: (document.getElementById('hoTip')||{}).textContent || '',
      take: (typeof WHEEL !== 'undefined') && WHEEL.mode === 'handoff',
      /* 2026-10-05 轮盘改版下有两种形态：
         · 轮盘补充入口里的 #hoConfirm（多狼局指过刀之后的「就这个，确定」）
         · WHEEL.act==='confirm' 态（只剩确定这一步时，点圆心即确认） */
      cfm: !!document.getElementById('hoConfirm')
           || ((typeof WHEEL !== 'undefined') && WHEEL.mode === 'act' && WHEEL.act === 'confirm'),
      acts: Array.from(document.querySelectorAll('#hoActs button[data-act]'))
              .map(function(b){ return b.getAttribute('data-act') + ':' + b.getAttribute('data-target'); }),
      say: !!document.getElementById('fbGo'),
      decline: (typeof WHEEL !== 'undefined') && WHEEL.mode === 'act' && WHEEL.act === 'pass',
      /* 2026-10-05 轮盘改版：女巫解药是 ✓/✕ 两个大按钮（不在 #hoActs 里） */
      heal: (typeof WHEEL !== 'undefined') && WHEEL.mode === 'act' && WHEEL.act === 'heal',
      /* noop 占位屏（女巫"药已用/还没轮到她那一问"）：**没有任何可点按钮**，
         正确动作就是"等这一屏走完"。走查原来没有这条分支 ⇒ 5 条都走不通 ⇒
         误报「第 N 局卡住」（2026-10-07 实测）。 */
      noop: (typeof WHEEL !== 'undefined') && WHEEL.mode === 'act' && WHEEL.act === 'noop',
      back: !!document.getElementById('hoBack'),
      done: (typeof WHEEL !== 'undefined') && WHEEL.mode === 'act' && WHEEL.act === 'done',
      /* 狼队共屏多选（2026-10-06 晚）：多个人狼待办一块屏 */
      pack: (typeof WHEEL !== 'undefined') && WHEEL.mode === 'act' && WHEEL.act === 'kill' && WHEEL.allies && WHEEL.allies.length > 1,
      wact: (typeof WHEEL !== 'undefined') ? (WHEEL.mode + '/' + (WHEEL.act || '') + '/' + (((WHEEL.view || {}).can_act) || []).join(',')) : '',
      /* 2026-10-02：发言回合改走「大圆模式」（不再开交接卡片）——
         walker 必须认识这个状态，否则发言回合会空转到烧光步数预算。 */
      spk: (typeof SPK_MODE !== 'undefined') && !!SPK_MODE,
      hint: ((document.getElementById('hoErr')||{}).textContent || '') + ' | ' +
            ((document.getElementById('hoTip')||{}).textContent || '') + ' | ' +
            ((document.getElementById('foot')||{}).textContent || '')
    };
  })()`);

  await ev2(`document.getElementById('btnNew').click()`);
  await sleep(1600);

  /* ⚠️ step 是**跨所有局共享**的总预算（不是每局重置）。
     2026-09-15 决策改走模型之后一局明显变长，600 不够 —— 会出现
     "第一局还没打完预算就用光"，表现为随机的「结束了 0 局」。放宽到 2000。 */
  while (step++ < 2000) {
    const s = await state();

    /* 身份牌那一页 */
    if (s.p3) {
      await ev2(`document.getElementById('bigcard').click()`);
      await sleep(600);
      const face = (await ev2(`document.getElementById('bigback').textContent`)).replace(/\s+/g, ' ');
      log(`身份牌 ${cards + 1}：${(await ev2(`document.getElementById('cfgTop').textContent`))} -> ${face}`);
      cards++; cardsThisGame++;
      /* 记录「人类里有没有神职」。**看牌就知道这局能不能覆盖到目标**，
         不用把整局白打完 —— 原来靠连开 5 局赌运气，
         2 真人时神职只有 1/2 概率落在人类手里，约 3% 会随机变红。 */
      if (/女巫|预言家/.test(face)) humanGotSpecial = true;
      await ev2(`document.getElementById('btnNextCard').click()`);
      await sleep(600);
      continue;
    }

    /* 牌发完了、人类一个神职都没有 ⇒ 这局不可能覆盖到「夜间特殊角色」，
       直接重开。每局要几十秒，别白打。 */
    /* ⚠️ 不要在这里引用 HANDOFF —— 那是**页面里的变量**，Node 这边看不到
       （会直接报 HANDOFF is not defined）。
       cardsThisGame 只在本局发完牌后 > 0，重开时立刻清零 ⇒ 这个分支每局最多命中一次。 */
    if (cardsThisGame > 0 && !humanGotSpecial){
      log('--- 这局人类没拿到神职，直接重开（不白打一局）---');
      games++;
      cardsThisGame = 0; humanGotSpecial = false;
      /* 🔴 2026-10-07：重开必须**把"同一步重复"计数也清掉** ——
         否则上一局残留的 lastKey/sameKeyRuns 会带进新局，
         新局一开局就撞上同一个 key ⇒ 几步之内就报「第 N 局卡住」并 break
         （这就是"重开路径自身卡死"的一半原因；另一半见下面 s.pack 那段注释）。 */
      lastKey = ''; sameKeyRuns = 0; lastPhase = '';
      if (games >= MAX_GAMES) break;
      await ev2(`(function(){document.getElementById('nHuman').value=${NH};
                             document.getElementById('nAi').value=${NA};})()`);
      await ev2(`document.getElementById('btnNew').click()`);
      await sleep(1600);
      continue;
    }

    if (s.phase === 'ended') {
      endedGames++;
      /* 两个覆盖目标都要满足才收工：
         ① 走到过夜间特殊角色（否则"私有结论"那条是假绿）
         ② 至少有一局**真的打完**（否则"一局能真的点完"是假绿）
         只满足一个就退出，另一条会随机变红 —— 那是测试自己的毛病。 */
      if ((privClicks > 0 && endedGames > 0) || games >= MAX_GAMES) break;
      games++;
      cardsThisGame = 0; humanGotSpecial = false;      /* 新的一局，重置看牌状态 */
      log(`--- 第 ${games + 1} 局（还没同时覆盖到「夜间特殊角色」和「打完一局」，再开一局）---`);
      await sleep(800);
      /* 定人数：**3 真人 + 1 AI**，不用页面默认的 2+2。
         为什么：这个走查要覆盖「夜间的特殊角色拿到私有结论」，而角色是**随机分配**的 ——
         2 真人时女巫只有 1/2 概率落在真人手里，连开 5 局都轮不到的概率约 3%，
         也就是**测试会随机变红**（那是测试自己的毛病，不是产品的）。
         3 真人把单局覆盖率提到 3/4，5 局都轮不到降到 0.1%。
         留 1 个 AI，是为了 AI 那条路（发言 / 投票）仍然被走到。 */
      await ev2(`(function(){document.getElementById('nHuman').value=${NH};
                             document.getElementById('nAi').value=${NA};})()`);
      await ev2(`document.getElementById('btnNew').click()`);
      await sleep(1600);
      continue;
    }

    /* 2026-10-02：大圆发言回合 —— 点一下开始、再点一下结束（和真人一样的两下）。
       识别成功就提交了；没听清会自动弹回交接卡片（下一轮循环按卡片流程走）。 */
    if (s.spk) {
      const _spkSeatNow = Number((await ev2(`(typeof SPK_MODE!=='undefined')?SPK_MODE:0`)) || 0);
      if (_spkSeatNow !== spkSeat){ spkSeat = _spkSeatNow; spkFail = 0; }
      if (spkFail >= 2){
        /* 连着两次没识别出字 ⇒ 点「过」（等价于真人按住说话失败后按「过」）。
           这一步**不削弱任何断言**：本走查要测的夜间轮盘/私有结论/共屏都不在发言上，
           而"一局能真的点完"本来就只要求"这一回合能被推过去"。 */
        trace.push(`[${step}] 大圆发言 · 连续 ${spkFail} 次没识别出字 ⇒ 点「过」`);
        const _hasPass = await ev2(`!!document.getElementById('circlePass')`);
        if (_hasPass) await ev2(`document.getElementById('circlePass').click()`);
        await waitIdle(40000);
        spkFail = 0;
        continue;
      }
      trace.push(`[${step}] 大圆发言 · 两下`);
      await ev2(`document.getElementById('bigCircle').click()`);
      await sleep(1400);
      await ev2(`document.getElementById('bigCircle').click()`);
      await waitIdle(40000);
      const _st = await ev2(`(document.getElementById('hoStatus')||{}).textContent||''`);
      if (/没听清|太短|识别失败|识别通道|用不了麦克风|没有录到/.test(_st)) spkFail++;
      else spkFail = 0;
      continue;
    }

    if (!s.hand) { await sleep(600); continue; }

    const key = s.who + '|' + (s.take ? 'take' : s.acts.join(','));
    if (key === lastKey) {
      sameKeyRuns++;
      maxSame = Math.max(maxSame, sameKeyRuns);
      if (sameKeyRuns === 1) log('!! 同一步重复了：' + s.hint + ' | wact=' + s.wact + ' rc=' + await ev2(`window.__rc||0`));
    } else { sameKeyRuns = 0; }
    lastKey = key;
    if (s.phase !== lastPhase) { lastPhase = s.phase; }
    if (step % 14 === 0) await shot('02-进行中-' + step);

    if (s.take) {
      trace.push(`[${step}] ${s.who} · 点圆心认领`);
      /* 2026-10-05 轮盘改版：认领 = 点圆心（旧「我拿到了」按钮已删） */
      await ev2(`document.getElementById('bigCircle').click()`);
      await sleep(800);
      continue;
    }

    /* 🔴 2026-10-05：**多狼局的「就这个，确定」**—— 轮盘改版后它是
       WHEEL.act==='confirm' 态，点圆心即确认。 */
    if (s.cfm) {
      trace.push(`[${step}] ${s.who} · 确认刀口`);
      await ev2(`(function(){ var b = document.getElementById('hoConfirm');
                              if (b) b.click(); else document.getElementById('bigCircle').click(); })()`);
      await waitIdle(40000);
      continue;
    }

    /* 2026-10-06 P4：验人结果屏（done 态）—— 点圆心交回，
       否则 walker 卡在结果屏（真人看得懂「点圆心交回」）。 */
    if (s.done) {
      trace.push(`[${step}] ${s.who} · 看完结果交回`);
      await ev2(`document.getElementById('bigCircle').click()`);
      await sleep(700);
      continue;
    }

    /* 狼队共屏（多狼局）：点第一个可点号码 → 点圆心批量提交。
       🔴 2026-10-07 修：「这局人类没拿到神职，直接重开」路径自身卡住 —
          原来自这里到分支尾的代码**被一个多余的 } 吞进了 if (s.done) 里**
          （上面那个 continue 之后就是死代码）⇒ 多狼共屏一到，
          walker 落进兜底 else、sameKeyRuns 攒到 6 就报「第 N 局卡住」并 break。
          括号对不上是纯语法正确、谁都不报错的错 —— 只有真走一遍才抓得到。 */
    if (s.pack) {
      trace.push(`[${step}] ${s.who} · 狼队共屏点刀`);
      await ev2(`(function(){ var b = document.querySelector('#hoActs button.pick'); if (b) b.click(); })()`);
      await sleep(150);
      await ev2(`document.getElementById('bigCircle').click()`);
      await waitIdle(40000);
      continue;
    }

    /* 🔴 2026-10-05：女巫解药问句（✓/✕ 两个大按钮，不在 #hoActs 里）——
       不认识它 walker 就会死等步数兜底。「今晚被刀的是谁」这个私有结论在问句里已经给出。
       🔴 2026-10-07 第三期走查实锤：解药是**大按钮 toggle**（点一下=用、再点=取消，
       见 tableui.renderWheelAct）而**提交发生在点圆心（或窗口结束自动生效）**——
       原来这条"看到就点 ✓"会把 toggle 来回翻 100 多次（实测连点 109 次），
       最后是不是"用了解药"全看翻到了哪一面。现在：还没打开就点开，然后**点圆心提交**。 */
    if (s.heal) {
      privClicks++;
      const t0 = await ev2(`document.getElementById('bigWho').textContent`);
      if (/被刀/.test(t0)) privSawResult++;
      trace.push(`[${step}] ${s.who} · 女巫解药 ✓`);
      const _on = await ev2(`(typeof WHEEL!=='undefined') && !!WHEEL.healOn`);
      if (!_on) await ev2(`document.getElementById('wheelYes').click()`);
      await sleep(150);
      await ev2(`document.getElementById('bigCircle').click()`);
      await waitIdle(40000);
      continue;
    }

    /* 2026-10-07 第三期：noop 占位屏 = "看着节拍走完"（没有任何可点按钮）。
       在这里等，**不计入"同一步重复"**（continue 在计数之前）。 */
    if (s.noop) {
      trace.push(`[${step}] ${s.who} · 等待（noop 占位屏，无按钮可点）`);
      await sleep(1000);
      continue;
    }

    if (s.acts.length) {
      const act = s.acts[0].split(':')[0];
      trace.push(`[${step}] ${s.who} · ${s.acts[0]}`);

      /* 目标按钮必须都是**活着的人**。
         自己不能当目标 —— **但 heal 和 kill 除外**：
           · heal：被刀的正好是女巫自己时，标准规则允许自救
           · kill：**自刀**（面杀本来就有这一手；2026-09-17 批二把它做成了引擎级合法动作，
                   前端照实把自己列出来）
         这是「座位号差一位」那类错误的第二道网 —— 光看截图暗屏里的圆点看不出来。 */
      /* 按钮必须真的落在视口里、且有面积 —— 这是"投票界面不出现"那类 bug 的唯一探针 */
      const vis = await ev2(`(function(){
        var bs = document.querySelectorAll('#hoActs button[data-act]');
        if (!bs.length) return 'no-button';
        for (var i = 0; i < bs.length; i++) {
          var r = bs[i].getBoundingClientRect();
          if (r.width < 4 || r.height < 4) return 'zero-size@' + i;
          if (r.top < 0 || r.bottom > window.innerHeight + 1) return 'out-of-view@' + i;
        }
        return 'ok';
      })()`);
      if (vis !== 'ok') {
        invisibleBtns++;
        if (invisibleBtns <= 3) log(`!! ${s.who} 的动作按钮看不见（${vis}）—— 可能是被挤出视口`);
      }

      const bad = await ev2(`(function(){
        var out = [];
        var btns = document.querySelectorAll('#hoActs button[data-act]');
        if (!btns.length || !MYVIEW || !MYVIEW.players) return out;
        var alive = {};
        MYVIEW.players.forEach(function(p){ if (p.alive) alive[p.seat + 1] = 1; });
        Array.prototype.forEach.call(btns, function(b){
          var t = Number(b.getAttribute('data-target'));
          var a = b.getAttribute('data-act');
          /* 2026-10-05：**空刀按钮**（data-target="-1"）是合法目标 ——
             引擎本就支持 kill=-1（wolf_agree_kill 放行），前端这次补了入口。
             校验必须先放行它，否则"一局能真的点完"会被这条假红拖死。 */
          if (a === 'kill' && t === -1) return;
          if (!alive[t]) { out.push(t + '-不是活人'); return; }
          /* 合法的"指向自己"有三个，其余一律是 bug（座位号算错一位）：
               heal = 女巫自救（被刀的正好是自己）
               kill = **自刀**（面杀本来就有；批二做成引擎级合法动作）
               vote = **自投**（engine.cast_vote：2026-09-17 起允许，标准局里
                      自投≈弃权的一种表达 —— 见 _smoke-rules.py 的 T2）
             ⚠️ 这条断言的教训：它一开始只放行 heal，后来**红了两次** ——
             一次因为自刀、一次因为自投。两次都是"断言没跟上产品决定"，
             不是产品错了。所以改这里，而不是把整条删掉（那才是真丢防线）。
             ⚠️ 报错里必须带**动作名**：原来只写"N-是自己"，看到的人根本不知道
             是哪个动作把自己列进去了。 */
          if (t === MYVIEW.seat + 1 && a !== 'heal' && a !== 'kill' && a !== 'vote') {
            out.push(t + '-是自己(' + a + ',角色' + MYVIEW.role_label + ')');
          }
        });
        return out;
      })()`);
      if (bad.length) {
        badTargets.push('步骤' + step + ':' + bad.join(','));
        log(`!! ${s.who} 的${act}目标里出现了不该有的座位：${bad.join(',')}`);
      }

      /* 私有结论：**女巫的在点之前就在**（法官私下告诉她今晚谁被刀），
         预言家的要点完才出 —— 所以两边分开取，别混成一个时序。 */
      let privBefore = '';
      if (act === 'heal' || act === 'poison') {
        privBefore = await ev2(`document.getElementById('hoActs').textContent + '|' +
                                document.getElementById('hoTip').textContent + '|' +
                                document.getElementById('bigWho').textContent`);
      }
      await ev2(`document.querySelector('#hoActs button[data-act]').click()`);
      /* 2026-10-05 轮盘改版：点数字 = 选中，**再点圆心 = 确认**（旧「就这个」已删）。
         只点数字不会提交任何东西 —— 界面停在原地，走查会报「反复卡在同一步」。 */
      await sleep(130);
      await ev2(`document.getElementById('bigCircle').click()`);
      var _probeN = await ev2(`JSON.stringify({mode: (typeof WHEEL!=='undefined')?WHEEL.mode:null, act: (typeof WHEEL!=='undefined')?WHEEL.act:null, pick: (typeof WHEEL!=='undefined')?WHEEL.pick:null, busy: (typeof BUSY!=='undefined')?BUSY:null})`);
      trace.push(`[${step}] probe ${_probeN} ws=` + await ev2(`JSON.stringify((window.__wsLog||[]).slice(-3))`));
      await waitIdle(40000);
      /* 夜间的特殊动作：**不写死是哪个角色**。
         2026-09-15 改组局规则后，4~8 人局只有女巫、没有预言家 ——
         原来这里只认 investigate，那条断言会变成「永远不可能通过」。
         现在按「这个角色能不能拿到只属于他的结论」来判：
           验人 → 看得到「好／坏」；用药 → 看得到「今晚被刀的是谁」。 */
      if (act === 'investigate' || act === 'heal' || act === 'poison') {
        privClicks++;
        const pat = (act === 'investigate') ? /你验的结果|是狼人|是好人/ : /被刀/;
        let t = privBefore;
        /* 2026-10-05：等待窗口 10×300ms → 20×500ms。真模型下视图刷新要等服务端
           drive 回来（LLM 慢时十几秒），原窗口不够 —— 4 次动作只等到 3 次结论，
           纯时序抖动，不是页面 bug（链路本身已由"看到 3 次"证明）。 */
        for (let k = 0; k < 20 && !pat.test(t); k++) {
          await sleep(500);
          t += await ev2(`document.getElementById('hoActs').textContent + '|' +
                          document.getElementById('hoTip').textContent + '|' +
                          document.getElementById('bigWho').textContent`);
        }
        if (pat.test(t)) privSawResult++;
        await shot('03-' + act + '结果');
      }
    } else if (s.say) {
      trace.push(`[${step}] ${s.who} · 发言`);
      /* 2026-10-05：打字兜底改走透明输入条（fbText/fbGo） */
      await ev2(`(function(){var t=document.getElementById('fbText'); if(t) t.value='我先过'; document.getElementById('fbGo').click();})()`);
      await waitIdle(40000);
    } else if (s.decline) {
      trace.push(`[${step}] ${s.who} · 放弃（点圆心）`);
      await ev2(`document.getElementById('bigCircle').click()`);
      await waitIdle(40000);
    } else if (s.back) {
      trace.push(`[${step}] ${s.who} · 交回`);
      await ev2(`document.getElementById('hoBack').click()`);
      await sleep(800);
      lastKey = '';                                  /* 交回不算一步 */
    } else {
      /* 什么都没得做。
         这里以前是**静默 sleep 然后继续** —— 页面一旦停在某个不认识的中间态，
         它就一路空转到把总步数预算烧光，最后只报一句「结束了 0 局」，
         完全看不出卡在哪（2026-09-15 就被这个坑过，红绿还随机）。
         现在用已有的「同一步重复」计数当闸门：重复 6 次（≈3 秒）还走不通，
         就把当时的 phase / who / hint 打出来再退出 —— **失败要能诊断**。 */
      /* ⚠️ 服务端正在替 AI 干活（大人数局一次跑不完一个回合）时，
         这一屏"没有可操作项"是**正常的**，不是卡住 —— 别把它当失败。 */
      if (sameKeyRuns >= 6 && String(s.hint).indexOf('AI 正在想') < 0) {
        log(`!! 第 ${games + 1} 局卡住：phase=${s.phase} who=${s.who} hint=${s.hint}`);
        log(`!!   wact=${s.wact} done=${s.done} acts=${s.acts.join(',')} take=${s.take}`);
        log('!!   （take / acts / say / decline / back 五条分支都走不通）');
        break;
      }
      await sleep(500);
    }
  }

  console.log('\n--- 走过的轨迹（前 46 步）---');
  trace.slice(0, 46).forEach(t => log(t));
  if (trace.length > 46) log(`… 共 ${trace.length} 步`);

  const finalPhase = await ev2(`(typeof PUBLIC !== 'undefined' && PUBLIC) ? PUBLIC.phase : ''`);
  check('身份牌能逐张走完（「看完，交给下一位」不是死按钮）', cards > 0, String(cards));
  check('一局能真的点完（不是卡在某一步）', endedGames > 0,
    '结束了 ' + endedGames + ' 局，最后 phase=' + finalPhase);
  check('没有反复卡在同一个「谁＋什么动作」上', maxSame < 3, '最多连了 ' + (maxSame + 1) + ' 次');
  check('★ 真的走到了「夜间特殊角色」这一步（否则下一条是假绿）',
    privClicks > 0, `开了 ${games + 1} 局才覆盖到`);
  check('★ 特殊角色拿得到只属于他的私有结论（验人好／坏，或今晚被刀的是谁）',
    privClicks > 0 && privSawResult === privClicks,
    `夜间特殊动作 ${privClicks} 次，看到私有结论 ${privSawResult} 次`);
  check('★ 动作按钮真的看得见（不是被挤出视口）',
    invisibleBtns === 0, `${invisibleBtns} 次看不见`);
  check('给玩家选的目标都是活人、且不含他自己',
    badTargets.length === 0, badTargets.slice(0, 3).join(' / '));
  check('页面没有抛未捕获异常', errors.length === 0, errors.slice(0, 2).join(' | '));

  /* ================================================================
     批八 ① 合规三样 + 断线恢复 + 再来一局
     为什么必须在**真 Chrome** 里验：这四条全是"页面级状态" ——
     计时跨局累计、提醒横幅、localStorage 锚点、按钮有没有绑上 onclick。
     打 API 测不出来、假 DOM 也测不出来（"漏绑 onclick"正是这个脚本的老本行）。
     ================================================================ */

  // ①-a 场次计时**跨局累计**：隔 30 分钟以内沿用旧起点；超过就算新的一场
  const series = await ev2(`(function(){
    var now = Date.now();
    localStorage.setItem('tt.since', String(now - 10 * 60 * 1000));
    var keptOld = Math.abs(seriesSince() - (now - 10 * 60 * 1000)) < 5000;
    localStorage.setItem('tt.since', String(now - 40 * 60 * 1000));
    var resetNew = seriesSince() > now - 5000;
    return { keptOld: keptOld, resetNew: resetNew };
  })()`);
  check('★ 场次计时跨局累计（法规说的是「连续使用」，不是单局时长）',
    series.keptOld, JSON.stringify(series));
  check('★ 隔 30 分钟以上算新的一场（否则「连续使用」永远算不清）',
    series.resetNew, JSON.stringify(series));

  // ①-b 断线恢复锚点：localStorage 里存在的是**当前这一局的局号**
  const anchor = await ev2(`(function(){
    return { stored: localStorage.getItem('tt.gid'),
             cur: (typeof GID !== 'undefined') ? GID : '' };
  })()`);
  check('★ 断线恢复锚点 = 当前局号（刷新/锁屏后要接回这一局，不是回封面）',
    !!anchor.stored && anchor.stored === anchor.cur,
    `${anchor.stored} vs ${anchor.cur}`);

  // ①-c 静态 DOM：场次行与提醒横幅不能被 foot() 的 textContent 清掉
  const domOk = await ev2(`(function(){
    var s = document.getElementById('series'), l = document.getElementById('longUse');
    return { ok: !!s && !!l && s.textContent.indexOf('本场') >= 0,
             t: s ? s.textContent : '' };
  })()`);
  check('★ 场次行没被 foot() 覆盖掉（玩到一半这行消失 = 合规计时形同不存在）',
    domOk.ok, domOk.t.slice(0, 60));

  // ①-d 「再来一局」按钮必须真的绑了 onclick（漏绑 ⇒ 点了毫无反应）
  const again = await ev2(`(function(){
    var b = document.getElementById('btnAgain');
    return { has: !!b, bound: !!(b && typeof b.onclick === 'function') };
  })()`);
  check('★ 「再来一局」绑上了处理函数（漏绑 onclick 这一类，只有真点才知道）',
    again.has && again.bound, JSON.stringify(again));

  // ①-e2 局末出口必须能被**收回去**。
  // 2026-09-29 用户实测报的 bug：开下一局时，屏幕下部还挂着上一局的「本局战报」，
  // 而且那个 href 指着**上一局**的战报文件（点开是上一局所有人的身份）。
  // 根因就是"局末把它显出来、开新局却没人收"。
  const endLinks = await ev2(`(function(){
    var el = document.getElementById('endLinks');
    if (!el) return { no: true };
    el.style.display = 'flex';
    $('lnkReport').setAttribute('href', '/api/game/deadbeef/report.md');
    resetEndLinks();
    return { display: getComputedStyle(el).display,
             hasHref: $('lnkReport').hasAttribute('href') };
  })()`);
  check('★ 局末出口能被收回去（否则下一局屏幕下部还挂着上一局的「本局战报」）',
    !endLinks.no && endLinks.display === 'none' && endLinks.hasHref === false,
    JSON.stringify(endLinks));

  // ①-e 超 2 小时横幅：把起点往前推 121 分钟，提醒必须真的出现
  //     ⚠️ 它会改页面里的 WSINCE，所以放在**最后**跑，别影响上面几条。
  const lu = await ev2(`(function(){
    WSINCE = Date.now() - 121 * 60 * 1000;
    WREMINDED = 0;
    checkLongUse();
    var el = document.getElementById('longUse');
    return { on: el.classList.contains('on'), text: el.textContent };
  })()`);
  check('★ 连续使用超 2 小时必须出现提醒横幅', lu.on, JSON.stringify(lu).slice(0, 120));
  check('……提醒里要写明依据（合规提醒不能只是一句「休息一下」）',
    lu.text.indexOf('人工智能拟人化互动服务管理暂行办法') >= 0
    && lu.text.indexOf('施行') >= 0, lu.text.slice(0, 100));

  // ①-f 上一局不存在（服务端重启过）⇒ 必须清掉锚点、并收起封面那个「继续上一局」
  //      （2026-10-01：`tryResume` **不再返回布尔值** —— 它现在的职责只是"发现活着的局
  //        就把按钮摆出来"，"要不要进去"由用户按按钮决定。判据跟着换成**按钮可见性**，
  //        那才是现在的不变量。）
  const resumed = await ev2(`(async function(){
    var old = localStorage.getItem('tt.gid');
    localStorage.setItem('tt.gid', 'no-such-game-404');
    var err = '';
    try { await tryResume(); } catch (e) { err = String(e); }
    var b = document.getElementById('btnResume');
    var o = { cleared: localStorage.getItem('tt.gid') === null,
              btnHidden: !!(b && b.hidden), err: err };
    if (old) localStorage.setItem('tt.gid', old); else localStorage.removeItem('tt.gid');
    return o;
  })()`);
  check('★ 上一局取不到时清掉锚点、并收起「继续上一局」按钮（否则每次打开都白试一次）',
    resumed.cleared === true && resumed.btnHidden === true,
    JSON.stringify(resumed).slice(0, 140));

  // ①-g 新加的规则开关（发言方式）**不用改前端**就该出现在开关区 ——
  //      控件由 /api/game/settings 那张表生成，前端不写死键名。
  //      这条断言就是那个设计的验收：后端加一条，界面自动多一个。
  const sw = await ev2(`(function(){
    var row = document.getElementById('switchRow');
    var sels = row ? row.querySelectorAll('select') : [];
    var hit = null;
    for (var i = 0; i < sels.length; i++){
      var opts = sels[i].options;
      for (var j = 0; j < opts.length; j++) if (opts[j].value === 'challenge') hit = opts[j].textContent;
    }
    return { selects: sels.length, found: hit };
  })()`);
  check('★ 新增的规则开关自动出现在界面上（后端加一条、前端一行都不用改）',
    !!sw.found, JSON.stringify(sw));

  /* ================================================================
     ★ 前置：把页面推回「交接屏开着、按住说话在屏幕上」的状态
     ⚠️ 为什么必须有这一步（2026-09-21 实测定位，别再删）：
       下面批九 A ~ 批十的那几条手机断言，原来紧跟在走查主循环之后 ——
       而那一刻**一局已经打完**（phase=ended）、交接浮层早就收掉了。
       #hoSay / #hoGo 是 renderHandoff() 动态建出来的，那时根本不存在
       （实测 getElementById('hoSay') === null、handoff.on === false）。
       于是它们不是测不到（TypeError: Cannot set properties of null，就抛在
       startSay 的 $('hoSay').textContent 那一行），就是假绿（keepBtn 的 skip 兜底）。
       这正是 HANDOFF §9.4 记的「8 条从没跑过」的真正原因 —— 写了，但从没能在
       它假设的状态下跑过。
       修法：现开一局，只推到**第一次出现「按住说话」**就停下。
       那一刻交接屏开着、按住说话在屏幕上，正是这些断言被写出来时假设的状态。
     ================================================================ */
  await ev2(`(function(){document.getElementById('nHuman').value=${NH};document.getElementById('nAi').value=${NA};})()`);
  await ev2(`document.getElementById('btnNew').click()`);
  await sleep(1600);
  let atSpeak = false;
  for (let k = 0; k < 400 && !atSpeak; k++) {
    /* 判据（2026-10-02 改）：发言交互有两种形态了 ——
       ① 交接卡片开着、带「说这句」（s.hand && s.say）
       ② 大圆发言模式（s.spk，卡片不开）
       ⚠️ 不能再看 #hoSay 在不在：它现在在 #spkHolder 里**常驻**（大圆模式
          复用 startSay/stopSay 需要它在），在不在不再区分任何状态。 */
    if (await ev2(`(function(){
      var h = document.getElementById('handoff');
      var on = h && h.classList.contains('on');
      return (on && !!document.getElementById('fbGo'))
          || ((typeof SPK_MODE !== 'undefined') && !!SPK_MODE);
    })()`)) { atSpeak = true; break; }
    const s = await state();
    if (s.p3) {                                   /* 身份牌逐张看完 */
      await ev2(`document.getElementById('bigcard').click()`);
      await sleep(500);
      await ev2(`document.getElementById('btnNextCard').click()`);
      await sleep(500);
      continue;
    }
    if (s.take) { await ev2(`document.getElementById('bigCircle').click()`); await sleep(700); continue; }
    /* 🔴 2026-10-07：这一段原来只认 take/acts/decline/heal/back ——
       多狼配置下它会撞上主循环才认识的那几个屏（共屏/确认/结果屏）然后空转 400 步，
       「前置」断言随即红（连带下面三条手机断言测不到东西）。
       补上同样的分支；局打完了就再开一局接着等。 */
    if (s.phase === 'ended') {
      await ev2(`document.getElementById('btnNew').click()`);
      await sleep(1600);
      continue;
    }
    if (s.cfm) {
      await ev2(`(function(){ var b = document.getElementById('hoConfirm');
                              if (b) b.click(); else document.getElementById('bigCircle').click(); })()`);
      await waitIdle(40000);
      continue;
    }
    if (s.done) { await ev2(`document.getElementById('bigCircle').click()`); await sleep(700); continue; }
    if (s.pack) {
      await ev2(`(function(){ var b = document.querySelector('#hoActs button.pick'); if (b) b.click(); })()`);
      await sleep(150);
      await ev2(`document.getElementById('bigCircle').click()`);
      await waitIdle(40000);
      continue;
    }
    if (s.noop) { await sleep(1000); continue; }   /* noop 占位屏：等节拍走完 */
    if (s.acts.length) {
      /* 2026-10-05 轮盘改版：先点号码选中（校验 .sel 真的挂上了），
         再点圆心确认。没选中就点圆心不会提交任何东西。 */
      const picked = await ev2(`(function(){
        var b = document.querySelector('#hoActs button[data-act]');
        if (!b) return false;
        b.click();
        return !!document.querySelector('#hoActs button.sel');
      })()`);
for (let _rt = 0; _rt < 3 && !(await ev2(`!!document.querySelector('#hoActs button.sel')`)); _rt++) {
        await ev2(`document.querySelector('#hoActs button[data-act]').click()`);
        await sleep(200);
      }

      if (picked) {
        await ev2(`document.getElementById('bigCircle').click()`);
      }
      await waitIdle(40000);
      continue;
    }
    if (s.decline) { await ev2(`document.getElementById('bigCircle').click()`); await waitIdle(40000); continue; }
    if (s.heal) {
      // 同主循环：先点开 toggle（点开才用解药），再点圆心提交 —— 别循环点什么都不提交。
      const _hon = await ev2(`(typeof WHEEL!=='undefined') && !!WHEEL.healOn`);
      if (!_hon) await ev2(`document.getElementById('wheelYes').click()`);
      await sleep(150);
      await ev2(`document.getElementById('bigCircle').click()`);
      await waitIdle(40000);
      continue;
    }
    if (s.back) { await ev2(`document.getElementById('hoBack').click()`); await sleep(700); continue; }
    await sleep(500);
  }
  const preSpeak = await ev2(`(function(){
    var b = document.getElementById('hoSay'), h = document.getElementById('handoff');
    return { say: !!b, spk: (typeof SPK_MODE !== 'undefined') && !!SPK_MODE,
             hand: !!(h && h.classList.contains('on')),
             touchAction: b ? getComputedStyle(b).touchAction : '-' };
  })()`);
  /* 这条**是新增的前置断言**：它把「下面几条手机断言到底测到了东西没有」变成可判定的。
     没有它，缺 #hoSay 时下面会集体走成"假绿 / 测不到"，而不是红。
     2026-10-02：发言交互有了第二种形态（大圆模式）—— 卡片不开也算数。 */
  check('★ 前置：发言交互已出现（交接屏开着，或大圆发言模式）',
    preSpeak.spk || (preSpeak.say && preSpeak.hand), JSON.stringify(preSpeak));
  if (!atSpeak) log('!! 推了 400 步也没等到「按住说话」—— 下面的手机断言会测不到东西');

  /* 🔴 2026-09-30：从这一段到本段结束，**把识别引擎钉在"本地"**。
     理由：假麦克风现在喂的是真实语音（Chrome 的 --use-file-for-fake-audio-capture），
     而下面这些块测的是"一次交互只开一条会话 / 收尾要干净"这类**结构性不变量** ——
     它们不该取决于"浏览器内置识别的服务器认不认这段音频"（那会让它们时红时绿）。
     钉成本地 ⇒ 每次跑的都是同一条确定的路径。末尾恢复原值（测试不留副作用）。 */
  const _pinBefore = await ev2(`(function(){
    var o = localStorage.getItem('tt.asr.pin') || '';
    if (Rec.pinEngine) Rec.pinEngine('local');
    return o;
  })()`);
  /* 2026-10-02：大圆发言模式出现后，下面这些麦克风测试会在 SPK_MODE 还挂着的
     时候跑 —— 它们内部的 startSay/stopSay(0) 会把测试音频**当成真发言提交**
     （stopSay 的成功路径就是 submit），把局推进到不可预测的状态。
     ⇒ 先退出大圆模式，回到和旧版一致的"没有发言回合挂着"的环境。 */
  await ev2(`(function(){ if (typeof closeSpeakerMode === 'function') closeSpeakerMode(); })()`);
  await sleep(300);

  /* ================================================================
    批九 A：麦克风链（用户报「语音无法录入」）
     ⚠️ 起因是个真 bug（2026-09-19 用户真机反馈）：流式播报 api.speak() 会建一个
        AudioContext，而 Rec.ensure() 原来是「有 ctx 就 return」⇒ **放过语音之后
        麦克风那条链永远不会建立**（没人调 getUserMedia），按住说话录到的永远是空的。
        判据必须是"麦克风链建好了没有"，不是"有没有 ctx" —— 两条独立的链。
     这条断言就是钉它：造出"只有 ctx"的状态，然后 ensure() 必须真的去要麦克风。
     ================================================================ */
  const micPath = await ev2(`(async function(){
    var out = {};
    var ac0 = new (window.AudioContext || window.webkitAudioContext)();
    var ms = ac0.createMediaStreamDestination().stream;      // 真 MediaStream（无输出音轨）
    var calls = 0, orig = navigator.mediaDevices.getUserMedia;
    navigator.mediaDevices.getUserMedia = function(){ calls++; return Promise.resolve(ms); };
    try {
      /* 2026-10-02：主循环的大圆发言已经把麦克风链建好了（以前走查从不开麦）。
         这条测的是「只有 ctx 时 ensure() 必须去要麦克风」—— 先 release 把
         麦克风链放掉（ctx 保留），再造出那个前置状态，测试才有东西可测。 */
      Rec.release();
      var d0 = Rec.diag();
      if (!d0.ctx) {                    // 还没有 ctx：先放一句 TTS 造出"只有 ctx"的状态
        try { await (await Rec.speak('/ws/tts?text=' + encodeURIComponent('测试'))).promise; }
        catch (e) {}
      }
      var d1 = Rec.diag();
      await Rec.ensure();
      var d2 = Rec.diag();
      out = { ctxBefore: d1.ctx, micBefore: d1.mic, micAfter: d2.mic, calls: calls };
    } catch (e) { out.err = String(e); }
    finally { navigator.mediaDevices.getUserMedia = orig; }
    return out;
  })()`);
  check('★ 播报建过 AudioContext 之后，麦克风链**仍然要能建起来**（原来直接 return ⇒ 录不进去）',
    micPath.micAfter === true && micPath.calls >= 1, JSON.stringify(micPath));
  check('……前置：当时确实是「有 ctx、没麦克风链」的状态（否则这条断言测不到东西）',
    micPath.ctxBefore === true && micPath.micBefore === false, JSON.stringify(micPath));

  /* 批九 B：按一下就松手不许卡住（"录不进去"的另一半）
     Rec.stream() 里要等麦克风授权/建连，那期间松手时调用方手上没有句柄 ——
     原来会掉进"录完再传"分支（麦克风还没开始录）⇒ 只报「没有录到」。
     现在：先置位、松手时等句柄交出来再收尾。这条钉"状态不能卡住"。 */
  const quick = await ev2(`(async function(){
    var ac0 = new (window.AudioContext || window.webkitAudioContext)();
    var ms = ac0.createMediaStreamDestination().stream;
    var orig = navigator.mediaDevices.getUserMedia;
    navigator.mediaDevices.getUserMedia = function(){ return Promise.resolve(ms); };
    var out = {};
    try {
      var t0 = Date.now();
      while (SPEAKING && Date.now() - t0 < 8000) { await new Promise(function(r){ setTimeout(r, 100); }); }
      var p = startSay();
      await stopSay(0);                       // 立刻松手（模拟"点一下就放"）
      await p;
      /* ⚠️ 2026-09-29：按钮上**已经没有文字**了（改成点击切换 + 去字），
         所以这里看的是 .on 类（= 还在录音态），不再看 textContent。
         ⚠️⚠️ 本段在**模板字符串**里：注释里**绝对不许出现反引号**。
             2026-09-29 我在这里写了 .on 两个字（外面套了反引号）—— 两个反引号正好配成一对，
             所以 node --check **通过了**（语法完全合法！），只在运行时炸成
             「.on is not a function」，走查直接 exit 2。比语法错更难查。
             ⇒ 改这个文件后除了 node --check，**必须真跑一遍**。
             注意：写这段警告本身时我又犯了一次 —— 括号里说的"反引号"请照字面理解。 */
      var _b = document.getElementById('hoSay');
      out = { sayRec: SAY_REC, pending: !!SAY_PENDING,
              rec: !!(_b && _b.classList.contains('on')),
              status: (document.getElementById('hoStatus')||{}).textContent };
    } catch (e) { out.err = String(e); }
    finally { navigator.mediaDevices.getUserMedia = orig; }
    return out;
  })()`);
  /* ⚠️ 2026-09-29：这条断言原来锁的是「松手之后按钮回到『按住说话』」——
     而"按住说话"这个交互本身已经被用户决定改成**点击切换**了（见 play.html 的注释），
     按钮上也不再有任何文字。所以判据换成「不在录音态（.on）」。
     ⚠️ 这是"断言锁住了被推翻的交互"，改断言是对的；但**不是**为了让它变绿而放宽：
       原来的两个不变量（不许卡在录音中、不许留悬空会话）一条没少。 */
  check('★ 点一下开始后立刻结束，不许卡在录音态、也不许留下悬空会话',
    quick.sayRec === false && !quick.pending && quick.rec === false,
    JSON.stringify(quick).slice(0, 170));

  /* ================================================================
     批十：手机上能不能用（2026-09-19 用户要求"搞一个手机上能跑的页面"）
     手机与桌面**确实不一样**，而且差异全在这三条上 —— 所以三条都要在真 Chrome 里钉住：
       ① 一次按压被**重复触发**（手机先 touchstart，浏览器再补发 mousedown）
          ⇒ 原来会在 /ws/asr 上开出**两条**会话，第二条覆盖第一条的句柄
       ② 手势被系统**取消**（手指滑走 / 来电 / 通知下拉）⇒ 原来连 touchend 都不会来，
          录音永远停不下来（麦克风常亮）
       ③ AudioContext 必须在**用户手势里**解锁，否则手机上一直是 suspended，
          AI 说话一点声音都没有（桌面 Chrome 宽松，这个坑只在手机现形）
     ================================================================ */
  /* 手机上的两个真机报障（2026-09-19），都只有"真的点一下"才知道：
     ① 「按住说话按下去**秒自己松开**」——浮层 .sheet 是可滚容器，长按被浏览器判成
        滚动手势 ⇒ pointercancel ⇒ 被当成"松手"处理。
     ② 「诊断按钮**点不了**」——它原来在牌桌页的 .controls 里，而交接浮层正好盖在底部。
     两条都在这里钉死：touch-action 必须是 none；🩺 必须是悬浮的、且 elementFromPoint
     真的能命中它（"能不能点到"最直接的判据，比看样式可靠）。 */
  const touch = await ev2(`(function(){
    var say = document.getElementById('hoSay');
    var fab = document.getElementById('btnDiag');
    var out = { say: !!say, fab: !!fab };
    if (say) out.touchAction = getComputedStyle(say).touchAction;
    /* 同一浮层里的**另一个**按钮（说这句）。它必须仍然是 manipulation ——
       这条是"排除写法有没有把整条批量规则搞失效"的眼睛：如果
       #handoff .sheet button:not(#hoSay) 写错（选择器非法），
       浏览器会把**整条规则**丢掉，那时 #hoSay 仍然是对的（它有自己的规则），
       但兄弟按钮会退化成默认值 ⇒ 只有这条能发现。
       ⚠️ 本段在模板字符串里，**不许出现反引号**（2026-09-21 又踩一次，
          node --check 当场抓到）。 */
    var sib = document.getElementById('hoGo');
    if (sib) out.sibTouchAction = getComputedStyle(sib).touchAction;
    /* 2026-10-02：发言回合改走大圆后，说话那一刻浮层通常是关的（hoGo 不在 DOM）。
       这条守卫改成**探针按钮**：往 .sheet 里临时塞一个 button —— 批量规则的
       选择器（handoff 下 sheet 里的 button、排除 hoSay）不要求 .on，touch-action
       又是非布局属性，display:none 祖先下照样读得到计算值；读完即删。
       比依赖「恰好有一张带发言的卡片开着」稳得多。 */
    var probe = document.createElement('button');
    /* 2026-10-05 轮盘改版：#hoActs 变成了轮盘刻度盘（不在 .sheet 下了），
       批量规则的覆盖对象改成兜底输入条 #hoSpeak 里的按钮。 */
    document.getElementById('hoSpeak').appendChild(probe);
    out.probeTouchAction = getComputedStyle(probe).touchAction;
    probe.remove();
    if (fab){
      var r = fab.getBoundingClientRect();
      var cs = getComputedStyle(fab);
      var el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      out.pos = cs.position; out.z = cs.zIndex;
      out.inView = r.top >= 0 && r.bottom <= window.innerHeight && r.width > 0;
      out.hit = el ? (el.id || el.tagName) : null;
    }
    return out;
  })()`);
  check('★ 按住说话声明了 touch-action:none（否则可滚容器里长按被判成滚动 ⇒ pointercancel ⇒ 秒松开）',
    touch.touchAction === 'none', JSON.stringify(touch).slice(0, 170));
  /* 2026-09-21 新增：把"排除写法"的另一半也锁住 —— 见上面 sibTouchAction 的注释。
     这一条加进来的直接原因：`#handoff .sheet button`(0,1,1,1) 一直压着
     `#hoSay`(0,1,0,0)，把 §9.3 的修复压成了死代码；而只测 #hoSay 一条
     发现不了"整条批量规则被写坏"。 */
  check('……同一浮层里的其它按钮仍是 manipulation（排除写法不能让那条批量规则失效）',
    touch.probeTouchAction === 'manipulation'
    && (!('sibTouchAction' in touch) || touch.sibTouchAction === 'manipulation'),
    JSON.stringify(touch).slice(0, 200));
  check('★ 诊断按钮任何情况下都点得到（悬浮 + z-index 高于交接浮层；原来被浮层盖住）',
    touch.pos === 'fixed' && Number(touch.z) >= 50 && touch.inView && touch.hit === 'btnDiag',
    JSON.stringify(touch).slice(0, 200));

  const keepBtn = await ev2(`(async function(){
    var b0 = document.getElementById('hoSay');
    /* ⚠️ 原来这里是 return { skip: 1 }（注意：本段在模板字符串里，
       所以**不许出现反引号**），而断言写成 !!skip || same === true ——
       于是**按钮根本不存在时它永远通过**（2026-09-21 实测抓到：走查主循环跑完
       一局之后 #hoSay 已不存在，这条是假绿）。那正是 §5.3 第 3 条要追问的
       「这条在什么情况下会永远通过」。现在不许兜底：没有按钮就是失败。 */
    if (!b0) return { noButton: true };
    var sig0 = HANDOFF_SIG, rec0 = SAY_REC;
    HANDOFF_SIG = 'stale:stale';       // 伪造"服务端说的换了"
    SAY_REC = true;                    // 伪造"手上正按着麦克风"
    var t0 = Date.now();
    while (Date.now() - t0 < 2700) { await new Promise(function(r){ setTimeout(r, 200); }); }
    var same = document.getElementById('hoSay') === b0;
    SAY_REC = rec0; HANDOFF_SIG = sig0;
    return { same: same };
  })()`);
  /* ⚠️ 2026-09-29：交互改成点击切换后，这条的**理由变了但内容没变** ——
     原来是"重建交接屏 ⇒ 销毁按钮 ⇒ 指针被取消 ⇒ 变秒松开"，
     现在是"重建交接屏 ⇒ 按钮没了 ⇒ 用户再点不到'结束'，而麦克风还开着"。
     所以它仍然是必须的守卫，只是措辞跟着改了。 */
  check('★ 正录着音时不许重建交接屏（重建 = 按钮没了，用户点不到"结束"）',
    keepBtn.same === true, JSON.stringify(keepBtn));

  const mob = await ev2(`(async function(){
    var ac0 = new (window.AudioContext || window.webkitAudioContext)();
    var ms = ac0.createMediaStreamDestination().stream;
    var orig = navigator.mediaDevices.getUserMedia;
    /* 🔴 2026-09-30：这一段**必须钉在本地引擎**，否则不确定：
       假麦克风现在喂的是真实中文语音（见脚本里 Chrome 的
       --use-file-for-fake-audio-capture），而内置识别（Web Speech）**不走
       getUserMedia** ⇒ 下面这个"把 getUserMedia 换成静音流"的桩对它无效 ⇒
       它可能真认出字来、把这一轮发言**提交出去**，后面的断言就全乱套了，
       而且结果取决于"浏览器厂商的服务器认不认这段音频"⇒ 时红时绿。
       钉成本地之后：录音走被替换的静音流 ⇒ 稳定识别为空 ⇒ 不影响后续状态。 */
    var pinOld = localStorage.getItem('tt.asr.pin') || '';
    if (Rec.pinEngine) Rec.pinEngine('local');
    var out = {};
    try {
      var t0 = Date.now();
      while (SPEAKING && Date.now() - t0 < 8000) { await new Promise(function(r){ setTimeout(r, 100); }); }
      var btn = document.getElementById('hoSay');
      var s0 = Rec.diag().sessions;
      /* ⚠️ 2026-09-29：交互从「按住」改成「点一下开始、再点一下结束」（用户要求），
         所以这里测的东西跟着换 —— 但**不变量一个没少**：
           旧的「双触发」其实就是"一次交互开出两条会话"，新交互下对应"点一下只开一条"；
           旧的「pointercancel 必须收尾」在新交互下**不存在那个入口**（不再依赖手势），
           换成"再点一下必须收干净"。 */
      // ① 第一下 = 开始
      btn.click();
      await new Promise(function(r){ setTimeout(r, 700); });
      out.afterStart = { sessDelta: Rec.diag().sessions - s0, sayRec: SAY_REC,
                         on: btn.classList.contains('on'),
                         /* 2026-09-30 加：状态行必须写明"现在用哪一层识别"。
                            欠费那次用户看到的正是"点了没反应、面板还显示正常"——
                            引擎是哪层、为什么，界面上一个字都没有。 */
                         status: ((document.getElementById('hoStatus') || {}).textContent || ''),
                         label: (SAY_STREAM && SAY_STREAM.label) || '' };
      // ② 再点一下 = 结束
      btn.click();
      await new Promise(function(r){ setTimeout(r, 1200); });
      out.afterStop = { sessDelta: Rec.diag().sessions - s0, sayRec: SAY_REC,
                        pending: !!SAY_PENDING, on: btn.classList.contains('on') };
      // ③ 音频解锁
      await Rec.unlock();
      out.ctxState = Rec.diag().ctxState;
    } catch (e) { out.err = String(e); }
    finally { navigator.mediaDevices.getUserMedia = orig;
              if (Rec.pinEngine) Rec.pinEngine(pinOld); }
    return out;
  })()`);
  check('★ 点一下开始：只开**一条**识别会话，且按钮进入录音态',
    !!mob.afterStart && mob.afterStart.sessDelta === 1 && mob.afterStart.sayRec === true
    && mob.afterStart.on === true, JSON.stringify(mob).slice(0, 190));
  check('★ 再点一下结束：会话收干净、退出录音态（不许一直录着）',
    !!mob.afterStop && mob.afterStop.sayRec === false && !mob.afterStop.pending
    && mob.afterStop.on === false, JSON.stringify(mob.afterStop));
  check('★ 解锁后 AudioContext 必须是 running（手机上不是 running ⇒ AI 一点声音都没有）',
    mob.ctxState === 'running', String(mob.ctxState));
  /* 2026-09-30：状态行必须说明**当前用的是哪一层识别**。
     判据用 '识别中' 开头 —— 三条引擎（内置/本地/云端）都走这一句，
     所以不管这台机器支持哪一层，它都该成立；不稳的话说明"明示"这件事漏了。 */
  check('★ 开始说话时，状态行写明当前用哪一层识别（禁止静默降级）',
    !!mob.afterStart && String(mob.afterStart.status).indexOf('识别中') === 0
    && !!mob.afterStart.label
    && String(mob.afterStart.status).indexOf(mob.afterStart.label) >= 0,
    JSON.stringify(mob.afterStart));

  /* ================================================================
     识别引擎层（2026-09-30，阿里云欠费之后的免费兜底）
     ⚠️ 这几条**不碰网络、不碰麦克风**：引擎选择是纯函数（只读 localStorage），
        所以它们在任何环境下都必须稳定 —— 不能变成"有时红有时绿"的假信号。
     ================================================================ */
  const eng = await ev2(`(function(){
    var out = {};
    var KF='tt.asr.fail.', KN='tt.asr.failn.', KPIN='tt.asr.pin';
    var keys = ['cloud','local','webspeech'];
    var oldPin = localStorage.getItem(KPIN), savedF = {}, savedN = {};
    keys.forEach(function(k){ savedF[k]=localStorage.getItem(KF+k); savedN[k]=localStorage.getItem(KN+k);
                              localStorage.removeItem(KF+k); localStorage.removeItem(KN+k); });
    try {
      out.hasApi = !!(Rec.listen && Rec.pickEngine && Rec.engineLabel && Rec.pinEngine);
      out.speech = Rec.speechSupported();
      Rec.pinEngine('');
      out.auto = Rec.pickEngine().engine;                 // 默认该是 cloud
      /* ① 云端失败 ⇒ 必须自动落到本地 */
      localStorage.setItem(KF+'cloud', String(Date.now()));
      out.banCloud = Rec.pickEngine().engine;
      out.banCloudWhy = Rec.pickEngine().why;
      /* ② 连续失败 2 次 ⇒ 一天内不再优先（哪怕"11 分钟前失败过"也必须跳过） */
      localStorage.setItem(KN+'cloud', '2');
      localStorage.setItem(KF+'cloud', String(Date.now() - 11*60*1000));
      out.sticky = Rec.pickEngine().engine;
      /* ③ 只失败过 1 次、且已过 10 分钟 ⇒ 允许它回来（一次抖动不该永久拉黑） */
      localStorage.setItem(KN+'cloud', '1');
      out.ttlExpired = Rec.pickEngine().engine;
      localStorage.removeItem(KF+'cloud'); localStorage.removeItem(KN+'cloud');
      /* ④ 手动钉住 */
      Rec.pinEngine('local');
      out.pinned = Rec.pickEngine().engine;
      Rec.pinEngine('webspeech');
      out.pinnedWeb = Rec.pickEngine().engine;
      Rec.pinEngine('');
      out.restored = Rec.pickEngine().engine;
    } catch (e) { out.err = String(e); }
    /* 复原：别人的现场不能被走查改掉（这一条遵守"测试不许留副作用"） */
    keys.forEach(function(k){
      if (savedF[k]) localStorage.setItem(KF+k, savedF[k]); else localStorage.removeItem(KF+k);
      if (savedN[k]) localStorage.setItem(KN+k, savedN[k]); else localStorage.removeItem(KN+k);
    });
    if (oldPin) localStorage.setItem(KPIN, oldPin); else localStorage.removeItem(KPIN);
    Rec.pinnedEngine = oldPin || '';
    return out;
  })()`);
  check('★ 识别引擎层在（三层兜底）：能挑层、能答"为什么"',
    !!eng.hasApi && !!eng.auto, JSON.stringify(eng).slice(0, 190));
  /* 2026-10-01 改：默认第一层换成**云端**。用户实测「本地 whisper 可以、阿里云可以、
     浏览器内置不行」—— 内置识别必须连浏览器厂商（Google）的服务器，国内必然失败，
     排第一就是每次开局先白搭一次、还甩一句"网络不通"。 */
  check('★ 默认优先"能出字的云端"，不把"国内必失败"的浏览器内置排第一',
    eng.auto === 'cloud', 'auto=' + eng.auto + ' speechSupported=' + eng.speech);
  check('★ 云端失败 ⇒ 自动落到本地（免费、离线那层）',
    eng.banCloud === 'local', 'banCloud=' + eng.banCloud + ' why=' + eng.banCloudWhy);
  check('★ 连续失败 ≥2 次整天不再优先；只失败 1 次且过了 10 分钟允许它回来',
    eng.sticky === 'local' && eng.ttlExpired === 'cloud',
    'sticky=' + eng.sticky + ' ttlExpired=' + eng.ttlExpired);
  check('★ 手动钉住某层能生效（现场排查要能把一层钉死看清楚）',
    eng.pinned === 'local' && eng.pinnedWeb === 'webspeech' && eng.restored === eng.auto,
    JSON.stringify(eng).slice(0, 190));

  /* ================================================================
     2026-09-30：**上行音频必须真的被量化** —— 本轮最贵的一个 bug。

     `new Int16Array(float32)` 和 `DataView.setInt16(off, float)` 都会把
     **非整数直接截断成 0**（0.26→0、0.9→0、-0.7→0）⇒ 上行音频**整段全零** ⇒
     云端/本地都识别不出字，界面只会说「没听清」，而麦克风 / 采样链 / ASR /
     上传**全都正常** —— 就是用户报的「麦还是没法用，我看诊断上面都显示正常」。
     浏览器**两条**上行路（WAV 编码、ws 分片）原来都栽在这上面。

     ⚠️ 这条**不需要麦克风、也不需要云端**（量化是纯函数），所以它必须永远稳定地跑；
        不稳就说明量化又被谁复制了一份出去。
     ================================================================ */
  const pcm = await ev2(`(function(){
    if (!Rec.pcm16) return { err: 'Rec.pcm16 不存在（rec.js 是旧的？）' };
    var i = Rec.pcm16(new Float32Array([0, 0.5, -0.5, 1, -1, 0.999, 2, -2]));
    return { isI16: (i instanceof Int16Array), n: i.length,
             v: Array.prototype.slice.call(i) };
  })()`);
  check('★ 上行音频必须真的被量化（float→int16 不许被截断成全零）',
    !!pcm && pcm.isI16 && pcm.v[1] > 16000 && pcm.v[2] < -16000
    && pcm.v[3] === 32767 && pcm.v[4] === -32767
    && pcm.v[6] === 32767 && pcm.v[7] === -32767,
    JSON.stringify(pcm));

  /* ================================================================
     2026-10-01：**识别层失败必须"当场"留痕**（否则用户反复撞同一堵墙）

     实测背景（用户截图）：真 Chrome 上点开始说话，内置识别回 `network`
     （国内连不上浏览器厂商的识别服务器）。错误**报出来了**，但**没被记下来** ——
     因为留痕原来只写在收尾里，而那个错误在"点结束"**之前**就到了。
     ⇒ 下一次 `pickEngine()` 仍然先选它 ⇒ 用户只能说「还是不行」。
     这条断言把"当场记录"钉住：把内置识别桩成必然失败，跑一次会话后，
     下一次必须自动换到本地。**不需要麦克风、不需要云端**。
     ================================================================ */
  const fall = await ev2(`(async function(){
    var out = {};
    var KPIN='tt.asr.pin', KF='tt.asr.fail.webspeech', KN='tt.asr.failn.webspeech';
    var oldPin = localStorage.getItem(KPIN), oldF = localStorage.getItem(KF), oldN = localStorage.getItem(KN);
    var dSR = Object.getOwnPropertyDescriptor(window, 'SpeechRecognition');
    var dWK = Object.getOwnPropertyDescriptor(window, 'webkitSpeechRecognition');
    try {
      localStorage.removeItem(KF); localStorage.removeItem(KN);
      /* 把云端/本地也标成"刚失败过" ⇒ 这一次必然选到内置识别（否则默认第一层是云端） */
      localStorage.setItem('tt.asr.fail.cloud', String(Date.now()));
      localStorage.setItem('tt.asr.fail.local', String(Date.now()));
      Rec.pinEngine('');
      out.before = Rec.pickEngine().engine;
      var Fake = function(){
        var self = this;
        this.start = function(){ setTimeout(function(){ self.onerror && self.onerror({error:'network'}); }, 30); };
        this.stop  = function(){ setTimeout(function(){ self.onend && self.onend(); }, 30); };
        this.abort = function(){ if (self.onend) self.onend(); };
      };
      try { Object.defineProperty(window,'SpeechRecognition',{value:Fake,configurable:true,writable:true}); } catch (e) {}
      try { Object.defineProperty(window,'webkitSpeechRecognition',{value:Fake,configurable:true,writable:true}); } catch (e) {}
      var h = await Rec.listen({});
      out.engine = h.engine;
      await h.stop();                                  // 收尾（错误早就到了）
      out.afterPick = Rec.pickEngine().engine;         // 不许再选它
      out.err = (Rec.diag() || {}).lastErr;
      out.recorded = localStorage.getItem(KF) || '';    // "当场留痕"的直接证据
    } catch (e) { out.x = String(e); }
    finally {
      try { if (dSR) Object.defineProperty(window,'SpeechRecognition',dSR); } catch (e) {}
      try { if (dWK) Object.defineProperty(window,'webkitSpeechRecognition',dWK); } catch (e) {}
      if (oldPin) localStorage.setItem(KPIN, oldPin); else localStorage.removeItem(KPIN);
      if (oldF) localStorage.setItem(KF, oldF); else localStorage.removeItem(KF);
      if (oldN) localStorage.setItem(KN, oldN); else localStorage.removeItem(KN);
      localStorage.removeItem('tt.asr.fail.cloud');     // 上面临时借用过，清掉
      localStorage.removeItem('tt.asr.fail.local');
      Rec.pinnedEngine = oldPin || '';
      if (Rec.release) Rec.release();
    }
    return out;
  })()`);
  check('★ 识别层失败必须**当场留痕**（不等收尾），下一次就不再选它',
    !!fall && fall.before === 'webspeech' && fall.engine === 'webspeech'
    && !!fall.recorded && /失败（第 1 次）/.test(String(fall.err || ''))
    && fall.afterPick !== 'webspeech',
    JSON.stringify(fall).slice(0, 240));

  // 诊断面板：手机上唯一的排查手段
  await new Promise(r => setTimeout(r, 800));       // 等 /api/net 回来
  const diagOk = await ev2(`(function(){
    diagRefresh();
    var el = document.getElementById('diagPanel');
    return { /* 240 太短了：'识别引擎' 那行排在麦克风链/AudioContext 之后，
               截断之后根本读不到 —— 断言会因为"没截到"而假红（或假绿）。 */
             text: (el.textContent || '').slice(0, 700),
             net: (typeof DIAG_NET !== 'undefined') ? DIAG_NET.length : -1,
             hasBtn: !!document.getElementById('btnDiag'),
             retry: !!document.getElementById('diagRetry'),
             engineBtn: !!document.getElementById('diagEngine') };
  })()`);
  check('★ 诊断面板能打开并逐条说清环境（安全上下文/麦克风链/识别引擎/识别会话）',
    diagOk.hasBtn && diagOk.retry && diagOk.text.indexOf('安全上下文') >= 0
    && diagOk.text.indexOf('麦克风链') >= 0
    && diagOk.text.indexOf('识别引擎') >= 0, JSON.stringify(diagOk).slice(0, 260));
  check('★ 诊断面板上能手动切换识别引擎并显示"最近一次实际用的哪层"',
    diagOk.engineBtn && diagOk.text.indexOf('最近一次实际用的是') >= 0,
    'engineBtn=' + diagOk.engineBtn);
  check('★ 诊断面板里直接给出手机入口（局域网 https 地址），不用回电脑上看控制台',
    diagOk.net >= 1, String(diagOk.net));

  /* ================================================================
     ★ 真语音端到端（2026-09-30 新增）——**"回退之后到底能不能出字"的正面证据**

     为什么非要有这一条：用户报「麦没法用、诊断却显示正常」，查下来那次测试的录音是
     **全零静音**（`rms_dbfs = -100.0`、`verdict = 几乎没收到声音`），所以
     "回退到本地识别"这条路**从没被真人语音跑过** —— 它只是一堆"应该能行"。
     现在：Chrome 的假麦克风喂 `_test_zh3.wav`（「金水是四号，查杀六号…」），
     钉住本地引擎（不依赖任何厂商服务器、也不花钱），点一下开始、说 4 秒、再点一下结束，
     然后看**它有没有真的把这句话提交出去**。

     ⚠️ 它会**真的提交一次发言**（那正是"出字了"的证据）⇒ 必须放在所有
        "还停在发言交接屏上"的断言之后，所以它排在最后。
     ⚠️ 判据抓 `submit(seat,'speak',null,text)` 的第四个参数，而不是去读 DOM ——
        因为提交之后交接屏会被重渲染，`#hoText` 那时可能已经不存在了（读 DOM 会假红）。
     ================================================================ */
  /* 夹具音频（`_test_zh3.wav` = 「金水是四号，查杀六号，我建议今天先出六号。」）
     以 data URL 形式注入页面，喂给下面那条"自搭的假麦克风"。 */
  const FIXTURE_B64 = require('fs').readFileSync(path.join(__dirname, '_test_zh3.wav')).toString('base64');
  const real = await ev2(`(async function(){
    var out = { said: null, status: '' };
    var old = localStorage.getItem('tt.asr.pin') || '';
    if (Rec.pinEngine) Rec.pinEngine('local');
    var origSubmit = window.submit;
    try {
      var btn = document.getElementById('hoSay');
      if (!btn) { out.err = '没有 #hoSay（不在发言交接屏上）'; return out; }
      out.engine = Rec.pickEngine().engine;
      /* 🔴 必须先**放掉麦克风那条链**：上面 mob 那块把 getUserMedia 换成了静音流，
         而 rec.js 的 ensure() 见到 stream 与 node 都在就**直接返回** ⇒ 不放掉的话，
         这一轮录到的还是那条**已作废的静音流** ⇒ 识别必然为空。
         （注意：本段在模板字符串里，评论里**不许出现反引号**。）
         （2026-09-30 实测踩到：断言报的是"没听清"，看起来像识别坏，其实是录音链是旧的 ——
          这就是本项目最怕的那种形状：链路一切"正常"，结果永远是空。） */
      if (Rec.release) Rec.release();
      /* ---- 自搭一条假麦克风：解码夹具 WAV → 循环播 → MediaStreamDestination → 替换 getUserMedia ----
         为什么不用 Chrome 的 --use-file-for-fake-audio-capture：实测它在本机**不喂声音**
         （录到 peak=0/rms=0 全零），而界面表现成"没听清"，白查两轮。
         这条自己搭的路**可断言**：录制结果里能量必须非零，否则就是我们没喂进去。 */
      var origGUM = navigator.mediaDevices.getUserMedia;
      var ac = new (window.AudioContext || window.webkitAudioContext)();
      /* ⚠️ 必须 resume。headless 里新建的 AudioContext 常常是 **suspended**：
         suspended 时 decodeAudioData 照样成功（夹具"看起来"加载好了），
         但**一个采样都不会播出来** ⇒ destination 给出的是静音 ⇒ 录到的全是零。
         （2026-09-30 实测：fixtureSeconds=6.07 已解出来，而上传的录音 peak=0/rms=0。）
         （注意：本段在模板字符串里，注释里**不许出现反引号**。） */
      try { await ac.resume(); } catch (e) { /* ignore */ }
      out.acState = ac.state;
      var dest = ac.createMediaStreamDestination();
      var _r = await fetch('data:audio/wav;base64,${FIXTURE_B64}');
      var _ab = await _r.arrayBuffer();
      var _audio = await ac.decodeAudioData(_ab);
      var _src = ac.createBufferSource();
      _src.buffer = _audio; _src.loop = true; _src.connect(dest); _src.start();
      navigator.mediaDevices.getUserMedia = function(){ return Promise.resolve(dest.stream); };
      /* 顺手量"到底喂进去了没有"：Rec.onlevel 每帧给的是**真实采集节点**的 rms。
         这条比"识别结果为空"更能定位：有电平没字 = 识别问题；没电平 = 喂的问题。
         （本段在模板字符串里，注释里**不许出现反引号**。） */
      var maxLevel = 0;
      Rec.onlevel = function(rms){ if (rms > maxLevel) maxLevel = rms; };
      out.fixtureSeconds = Math.round(_audio.duration * 100) / 100;
      out.ctxRate = ac.sampleRate;
      window.__said = null;
      window.submit = function(seat, kind, a, b){
        if (kind === 'speak') window.__said = b;
        return origSubmit.apply(this, arguments);
      };
      btn.click();                                        // 点一下 = 开始
      await new Promise(function(r){ setTimeout(r, 4000); });   // 夹具循环播，说满 4 秒
      out.midStatus = (document.getElementById('hoStatus') || {}).textContent || '';
      btn.click();                                        // 再点一下 = 结束 → 本地识别
      var t0 = Date.now();
      while (Date.now() - t0 < 30000) {
        var st = (document.getElementById('hoStatus') || {}).textContent || '';
        if (window.__said !== null) { out.status = st; break; }
        if (st.indexOf('没听清') === 0 || st.indexOf('识别通道') === 0
            || st.indexOf('太短') === 0 || st.indexOf('用不了麦克风') === 0) {
          out.status = st; break;
        }
        await new Promise(function(r){ setTimeout(r, 250); });
      }
      out.said = window.__said;
    } catch (e) { out.err = String(e); }
    finally {
      out.maxLevel = Math.round(maxLevel * 100000) / 100000;   // 采集节点的峰值 rms（= 真喂进去了没）
      Rec.onlevel = null;
      window.submit = origSubmit;
      try { navigator.mediaDevices.getUserMedia = origGUM; } catch (e) { /* ignore */ }
      try { if (_src) _src.stop(); } catch (e) { /* ignore */ }
      /* 把"假麦克风"那条链也放掉：不放的话它会留在 rec.js 里，
         之后（或下一次真机）用到的就是这条已经作废的流 —— 这正是本轮踩过的坑。 */
      if (Rec.release) Rec.release();
      if (Rec.pinEngine) Rec.pinEngine(old);
    }
    return out;
  })()`);
  check('★ 真语音端到端：喂进真人语音，免费兜底（本地 whisper）必须把字吐出来',
    !!real && /金水|查杀|6号|六号/.test(String(real.said || '')),
    JSON.stringify(real).slice(0, 260));

  /* 收尾：把引擎钉回进入这一段之前的值（**测试不许留副作用** ——
     留在现场排查时会变成"莫名其妙不按预期选层"，那种问题最难查）。 */
  await ev2(`(function(){ if (Rec.pinEngine) Rec.pinEngine(${JSON.stringify(_pinBefore)}); })()`);

  /* ================================================================
     2026-10-01：**刷新/重开必须停在封面**（用户：「都是以同配置开一局，这不合理，
     应该直接回到初始界面才对」）

     原来：本地存着局号 ⇒ 刷新就**自动接回桌面**（用户看到的就是"强刷一下就把上一局
     接回来了/看起来像以同配置又开了一局"）；「再来一局」也是直接按同配置再开。
     现在：刷新一定停在封面，但若那局还活着，封面出现「继续上一局」按钮 ——
     **恢复能力没丢，只是不再替用户做决定**；「再来一局」也改成回封面。

     这条断言三段：① 刷新后封面可见、牌桌不可见 ② 出现「继续上一局」按钮
                    ③ 按了它**能真的接回桌面**。
     ⚠️ 它要重新加载页面 ⇒ 必须放在所有依赖页面状态的断言之后。
     ================================================================ */
  const rel = await (async () => {
    const gid = await ev2(`(function(){ var g = localStorage.getItem('tt.gid'); if (!g && GID) localStorage.setItem('tt.gid', GID); return localStorage.getItem('tt.gid'); })()`);
    await send('Page.navigate', { url: BASE + '/play' }, sessionId);
    await sleep(2800);
    const first = await ev2(`(function(){
      var p0 = document.getElementById('p0'), p4 = document.getElementById('p4');
      var b = document.getElementById('btnResume');
      return { cover: !!(p0 && p0.classList.contains('on')),
               table: !!(p4 && p4.classList.contains('on')),
               resumeBtn: !!(b && !b.hidden),
               resumeText: b ? String(b.textContent).slice(0, 40) : '' };
    })()`);
    let back = null;
    if (first && first.resumeBtn) {
      await ev2(`document.getElementById('btnResume').click()`);
      await sleep(2600);
      back = await ev2(`(function(){ var p4 = document.getElementById('p4');
        return { table: !!(p4 && p4.classList.contains('on')) }; })()`);
    }
    return { gid: gid, first: first, back: back };
  })();
  check('★ 刷新之后**停在封面**（不再自动接回桌面），且封面出现「继续上一局」按钮',
    !!rel.first && rel.first.cover === true && rel.first.table === false
    && rel.first.resumeBtn === true, JSON.stringify(rel).slice(0, 230));
  check('★ 按下「继续上一局」能真的接回桌面（恢复能力一点没少）',
    !!(rel.back && rel.back.table === true), JSON.stringify(rel.back));

  await shot('04-结束');
  console.log('\n' + '='.repeat(70));
  console.log(`结果：${PASS.length}/${PASS.length + FAIL.length} 通过`);
  if (FAIL.length) { console.log('失败项：'); FAIL.forEach(f => console.log('  - ' + f)); }
  ws.close();
  process.exit(FAIL.length ? 1 : 0);
})().catch(e => { console.error('走查脚本自己出错：' + e.message); process.exit(2); });
