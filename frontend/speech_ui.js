/* ============================================================================
   speech_ui.js —— 播报队列 + 夜间门控（2026-10-07 从 tableui.js 拆出）
   ----------------------------------------------------------------------------
   为什么拆：`tableui.js` 撞到 `_smoke-map.py` 的 1900 行硬线（一度 1916），
   而那条线的道理是"AI 一次读不完一个文件"。这一段（播报回放 + 三条门控判据）
   与"轮盘 / 交接屏"互不共享状态，是最干净的切面。

   装在哪：`play.html` 里**排在 tableui.js 之前**（两者共用页面全局作用域，
   rec.js / table.js 也是这样）。本文件只声明，不立即执行任何东西。

   门控三档（用户定：都在**播报之后**）：
     wheel/shoot = 那句念完（linePlayed）· speak/vote = voicedUp · none = 放行
   ⚠️ 反向教训同样有效：**不许用播报把面板永久憋死**（linePlayed 有音频卡死兜底）。
   事故全本见 HANDOFF §10.7。
   ============================================================================ */

/* ---------------------------------------------------------- 播报 */

async function playBlob(blob){
  var url = URL.createObjectURL(blob);
  try {
    var a = new Audio(url);
    await new Promise(function(res, rej){ a.onended = res; a.onerror = rej; a.play().catch(rej); });
  } finally { URL.revokeObjectURL(url); }
}

/* 最近一次 TTS 的结果（2026-10-05 用户报「AI 不发声」）：诊断面板/控制台要看的
   就是它 —— 到底是"没请求"、"合成失败"还是"播放被拦"，不再靠猜。 */
var LAST_TTS = null;
/* 过期跳过的"追线"旗（2026-10-12）：只在追上第一条新鲜句子之前允许跳过。
   队列停摆（切后台节流 / TTS 悬挂）恢复时，最老的残句跳、当前段的句子照播 ——
   夜听起来完整（用户实机：狼人睁眼整段消失、直接听到闭眼）。 */
var _catchupSkip = false;
function time_ms(){
  return (window.performance && performance.now) ? performance.now() : Date.now();
}

async function speakQueue(){
  if (SPEAKING || !VOICE_ON || !PUBLIC) return;
  SPEAKING = true;
  var prevType = null;         /* 上一条的类型：间隔挂在**下一条开头**（见循环顶） */
  var prevLine = null;         /* 上一条本身：delay 挂在它身上 */
  try {
    for (;;){
      if (PAUSED){ await sleep(250); continue; }
      var tl = PUBLIC.timeline || [];
      if (SPOKEN >= tl.length) break;
      /* 2026-10-04 用户定：**最后一条播完立刻就绪** —— 间隔从"播完之后睡"
         挪到"下一条之前睡"。原来播完公告还要白等 0.7~1.6s 才算"念完了"，
         「N号请发言」→ 麦可点的延迟凭空多出这一截。 */
      /* 2026-10-05：夜间法官播报的条目带 delay（秒）—— 闭眼后留白，面杀节奏。 */
  var _pd = (prevLine && prevLine.delay) ? Number(prevLine.delay) * 1000
          : (prevType === 'sys' ? 700 : 1600);
  /* 🔴 2026-10-08 第十二期（用户实机：一整局只听见 5 句 —— 上一轮的"过期跳过"干的好事）：
     `not_before = at+delay` **没算前一句 TTS 的时长**，客户端正常跟读时也会被判
     "迟到了 4 秒" ⇒ 睁眼句被整段跳掉。修正成两档：
     · 落后 >60s（整页刷新级别）⇒ 真·跳过（半夜刷新不该重播整夜）；
     · 落后 10~60s ⇒ **不扔句子，只压掉留白**（"播报快"指的是节奏被压，
       从来不是"句子该消失"）—— 播报完整，只是追进度。 */
      var _due = tl[SPOKEN];
      var _late = (_due && _due.at)
        ? (Date.now() / 1000 - (_due.at + (_due.delay || 0))) : 0;
      /* 🔴 2026-10-12（用户实机：狼人睁眼播报+面板整段消失，直接听到"狼人请闭眼、
         女巫请睁眼"）：队列停摆一次（切后台被浏览器节流 / 某句 TTS 悬挂）⇒ 恢复时
         "过期跳过"把最老的狼人睁眼句吃掉、较新的照播 ⇒ 夜不连贯，且停摆期间
         arm 也发不出（面板随窗口一起消失）。
         改成**追线制**：只在追上第一条"还新鲜"（≤60s）的句子之前允许跳过；
         追上之后这一局不再跳过 —— 停摆恢复时当前段的句子照播（晚是晚，但完整）。 */
      if (_due && _due.t === 'sys' && _late > 60 && !_catchupSkip){
        SPOKEN++;
        SPOKE_DONE = { idx: SPOKEN - 1, at: Date.now() };
        prevType = _due.t; prevLine = _due;
        continue;
      }
      if (_late <= 60) _catchupSkip = true;
      /* 🔴 2026-10-12（用户：夜里"播报过快没有空挡"）：追进度压缩只许压**白天**
         的句子 —— 夜间句子（带 node）的留白是定稿节奏（闭眼→3s→睁眼），
         压掉就是"没有空挡"。队列慢时夜间保持原节奏（服务端产句比播放慢，
         自然会追上）。 */
      if (_late > 10 && !(_due && _due.node)) _pd = 0;
  if (prevType) await sleep(_pd);
      /* 🔴 2026-10-11（用户实机："听不到播报"）：上一轮第十二期的 replace 把这行
         吞了 —— `line` 变成未定义，L68 `CUR_LINE = line` 在 try 块**外**抛
         ReferenceError ⇒ 整条播报协程无声死掉 ⇒ 一个字都不念。
         ⚠️ `node --check` 只查语法不查未定义变量，所以当时没拦住 —— 这也说明
         播报队列需要一个"哨兵"：任何一句抛出都要能在诊断面板看见（LAST_TTS.how
         = 'queue'），见下面的外层 try。 */
      var line = tl[SPOKEN++];
      var _t1 = 0;                     /* 本条合成耗时（填进 LAST_TTS） */
      /* 哨兵（2026-10-11 事故的直接教训）：try 块**之前**的这几行也绝不允许
         无声弄死整个队列 —— 这次的 ReferenceError 就抛在这里、页面毫无提示。
         失败记进 LAST_TTS（how='queue'），诊断面板能看见。 */
      try { CUR_LINE = line; paintBig(); }
      catch (e) { LAST_TTS = { ok: false, how: 'queue', err: String(e).slice(0, 80) }; }
      /* 倒计时 arm 的依据（2026-10-06 晚用户定：播报最后一个字后 2 秒起算）——
         每条**真正念完**的时刻记在这里（idx + 时刻），nightReady 的那句被覆盖
         且满 2s，前端才向服务端 arm 起算。 */ 
      SPOKE_DONE = { idx: SPOKEN - 1, at: 0 };   /* at 在播放完成后填 */
      /* 播报进度条已随顶部标题栏一起删除（2026-10-05 用户定：公有信息只进日志区） */

      /* 🔴 2026-10-05 用户报「AI 不发声」：
         原来这一整段是**裸**的 —— 任何一句抛异常（渲染、拉起发言模式、
         音频播放被拦…）都会把整条播报队列打断，而 SPOKEN 已经加过了 ⇒
         那一句永远不再播，后面也停摆（tick 每拍重试同一句 ⇒ 一直卡在同一处）。
         现在：单条失败 = 记下来 + 跳过这一句，队列继续；诊断面板能看见真实原因。 */
      try {
      /* 2026-10-02 用户定：**人类的话只进不复述** —— 人类座位刚亲口说过的话，
         不再用 AI 音色播一遍（旧行为：所有 say 条目都 TTS，人类的话也念）。 */
      if (line.t === 'say' && kindOf(line.seat) === 'human'){
        setLine('');
        await sleep(400);
        prevType = line.t; prevLine = line;
      if (SPOKE_DONE && SPOKE_DONE.idx === SPOKEN - 1) SPOKE_DONE.at = Date.now();
        continue;
      }

      /* 字幕策略（同一次用户反馈）：AI 文本不上屏。
         say 条目只把大圆切成「N号玩家（AI）」，字幕清空；
         sys（法官）条目照常上字幕。 */
      setLine(line.t === 'sys' ? line.s : '');

      /* 2026-10-07 第二期（用户定稿）：播报「他是这个」的**同时**，圆盘上给出
         大大的好（绿）/坏（红）标识，3 秒后消失（与「预言家请闭眼」同时）。 */
      if (line.stage === 'seer_result') showSeerBig();

      /* 2026-10-02 用户定：报名字归法官（服务端已在每条发言前插「N号请发言」），
         所以正文里那个「N号：」前缀在播报时剥掉 —— 否则同一个名字响两遍。 */
      var txt = line.s;
      if (line.t === 'say'){
        var m = txt.match(/^\d+号：([\s\S]*)$/);
        if (m) txt = m[1];
      }

      /* 语速：这是**相对** SAPI_RATE（.env 里那个基准）再加的偏移。
         ⚠️ 2026-09-16 改：原来 sys=-1 / say=+3，配上基准 2 ⇒ 实际 1 / 5，
         语速偏快（用户反馈"太快"）。现在基准降到 0、偏移改成 -2 / 0：
         法官稳重偏慢，玩家说话正常速。
         ⚠️⚠️ 2026-09-19：这个 rate **只有本地 SAPI 认**。切到云端 TTS 之后它已经没有
         效果了（云端没有语速参数）——想真的控制语速得用 instruct 模型 + instructions，
         那是下一步。现在法官与玩家是靠**音色**区分的（比语速区分更可靠）。 */
      var rate = (line.t === 'sys') ? -2 : 0;
      /* 音色（人格的听觉那一半）：**服务端在时间线上给好了**（法官一个音色、
         每个 AI 座位按人格各一个），前端只负责原样转过去 —— 不在这里另抄映射。
         切到云端 TTS（TTS=qwen）才听得出区别；本地 SAPI 会静默忽略 voice。 */
      var voice = line.voice || '';

      /* 🔴 2026-10-22 双通道：法官台词（sys）优先走**浏览器本地** speechSynthesis
         （judge_voice.js）—— 文本到手就开口，无 HTTP 往返，夜间相位切换不再被
         云端链路（~1s/句起步）拖住。AI 发言（say）仍走云端 —— 音色人格在那条通道。
         降级链（保命设计，不能砍）：speechSynthesis 不存在 / 音色列表没到 /
         没有中文音色（部分国产 Android ROM）⇒ 本地失败或不可用，**逐句**回落
         现有云端通道，一个字都不会少念。
         打断规则：相位切换类（天黑/天亮）interrupt=true 立即打断旧播报；
         其余法官台词排队不打断。 */
      var _loc = false;
      if (line.t === 'sys' && typeof JudgeVoice !== 'undefined' && JudgeVoice.usable()){
        try {
          var _lv0 = time_ms();
          await JudgeVoice.speakAwait(txt, JudgeVoice.isPhaseSwitch(txt));
          _loc = true;
          /* at_lag_ms = 服务端时间戳 → 本地出声的全程延迟（验收指标 <300ms 的观测值；
             跨设备时含本机与服务端的时钟差，桌面同机测量最准）。 */
          LAST_TTS = { ok: true, how: 'local', ms: time_ms() - _lv0,
                       at_lag_ms: Math.max(0, Math.round(
                         (Date.now() / 1000 - (line.at || Date.now() / 1000)) * 1000)),
                       text: txt.slice(0, 40) };
        } catch (e) {
          /* 本地任何异常都按"不可用"处理：记诊断、_loc 保持 false ⇒ 照走云端 */
          LAST_TTS = { ok: false, how: 'local', err: String(e).slice(0, 80),
                       text: txt.slice(0, 40) };
        }
      }
      var spoke = false, sp = null;
      if (!_loc && VOICE_STREAM){
        try {
          sp = await Rec.speak('/ws/tts?text=' + encodeURIComponent(txt)
                               + (voice ? ('&voice=' + encodeURIComponent(voice)) : ''));
          SPEAK_STREAM = sp;
          await sp.promise;
          SPEAK_STREAM = null;
          spoke = (sp.firstMs() !== null);
          if (SPOKE_DONE && SPOKE_DONE.idx === SPOKEN - 1) SPOKE_DONE.at = Date.now();
      /* 出过声就算成功（被跳过时 firstMs 可能为 null） */
        } catch (e) {
          SPEAK_STREAM = null;
          spoke = false;
        }
      }
      _t1 = time_ms();
      if (!_loc && !spoke){
        try {
          /* 🔴 2026-10-12（用户实机：开局首段播报迟迟不出、面板随之不出）：
             TTS 请求原来**没有超时** —— 服务端冷启动/网络一卡，队列就整个挂住。
             给它 12 秒 AbortController：超时按合成失败走（记 LAST_TTS、跳过这句），
             队列继续；面板有 8s 停摆兜底，不会再被一句 TTS 拖死。 */
          var _tac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
          var _ttm = _tac ? setTimeout(function(){ try { _tac.abort(); } catch (e) {} }, 12000) : null;
          var res = await fetch('/api/tts?text=' + encodeURIComponent(txt)
                                + '&rate=' + rate
                                + (voice ? ('&voice=' + encodeURIComponent(voice)) : ''),
                                _tac ? { signal: _tac.signal } : {});
          if (_ttm) clearTimeout(_ttm);
          if (!res.ok){
            LAST_TTS = { ok: false, how: 'api/tts', http: res.status,
                         text: txt.slice(0, 40) };
            voiceHint('这一句没合成出来（HTTP ' + res.status + '）');
          } else {
            var _blob = await res.blob();
            LAST_TTS = { ok: true, how: spoke ? 'stream' : 'api/tts',
                         bytes: _blob.size, ms: _t1 ? (time_ms() - _t1) : null,
                         text: txt.slice(0, 40) };
            await playBlob(_blob);
          }
        } catch (e) {
          LAST_TTS = { ok: false, how: 'api/tts', err: String(e).slice(0, 80),
                       text: txt.slice(0, 40) };
        }
      } else {
        LAST_TTS = { ok: true, how: 'stream', text: txt.slice(0, 40) };
      }

      /* 2026-10-04 用户报：报完「N号请发言」，麦要**立刻**可点 —— 原来要等
         tick 下一轮（最慢 1.2s）。公告一播完、且服务端确实在等这位人类发言
         ⇒ 直接拉起大圆发言模式，不等轮询。（AI 发言轮 pending 是 working，
         这个守卫保证它只在人类发言回合触发。） */
      var _mAnn = (line.t === 'sys') ? line.s.match(/^(\d+)号请发言$/) : null;
      if (_mAnn && PUBLIC.pending === 'human_speak'
          && PUBLIC.current_speaker === Number(_mAnn[1]) - 1
          && SPK_MODE !== Number(_mAnn[1])){
        enterSpeakerMode({ seat: Number(_mAnn[1]) - 1, actions: ['speak'] });
      }
      prevType = line.t; prevLine = line;
      if (SPOKE_DONE && SPOKE_DONE.idx === SPOKEN - 1) SPOKE_DONE.at = Date.now();
      } catch (e) {                     /* ← 上面 try 的收口：单条失败不拖死队列 */
        LAST_TTS = { ok: false, how: 'line', err: String(e).slice(0, 100),
                     text: (line && line.s ? line.s : '').slice(0, 40) };
        voiceHint('这一句播报失败（已跳过，后面的照常念）：' + String(e).slice(0, 60));
        prevType = (line && line.t) || prevType; prevLine = line || prevLine;
        if (SPOKE_DONE && SPOKE_DONE.idx === SPOKEN - 1) SPOKE_DONE.at = Date.now();
      }
    }
  } finally {
    SPEAKING = false; CUR_LINE = null; paintBig();
    /* 诊断面板开着的话刷一下：刚发生的播报结果（含失败）立即可见 */
    try {
      if ($('diagPanel') && $('diagPanel').classList.contains('on')) diagRefresh();
    } catch (e) { /* 面板不在就忽略 */ }
  }
}

/* 桌上那句话念完了没（播报队列追上了时间线）？
   ⚠️ 用它挡住「下一屏」：实测**投票那一屏会抢在最后一个发言人的话之前弹出来** ——
   因为最后一次 act() 里服务端可能同时跑完了"最后一个 AI 发言 + 开始投票"，
   而播报还在队列里没放完。
   两种情况不等：语音关掉（没人会念）、用户按了暂停（否则两边互相等死）。 */
function voicedUp(){
  if (!VOICE_ON || PAUSED) return true;
  var tl = (PUBLIC && PUBLIC.timeline) || [];
  return SPOKEN >= tl.length;
}

/* 夜间门控：某个人类座位的夜间操作屏，要等到法官**播到他那一句「请睁眼」**才放行
   （2026-10-05 用户定：状态机推进 == 播报推进，见 tick 里的注释）。
   2026-10-06 P1（夜间单一时间源）：判据从「前端正则猜播报文本」改为
   **结构化对齐** —— 服务端在 timeline 条目上打了 stage（= 角色键），
   在 pending_seats 条目上带了 role。谁该动 == 播报指针越过了第一条
   stage === need.role 的句子。改播报文案不会再弄坏门控；
   老局 timeline 没有 stage 时回退整队播报门（保守，不会更糟）。 */
function nightStageIdx(need){
  /* 该座位的「请睁眼」句（stage === role）在时间线里的下标（-1 = 老局/非夜间）。 */
  var role = need && need.role;
  if (!role) return -1;
  var tl = (PUBLIC && PUBLIC.timeline) || [];
  for (var j = 0; j < tl.length; j++){
    if (tl[j].stage === role) return j;
  }
  return -1;
}
/* 节点 id 在时间线里的下标（2026-10-07 第三期：门控认**节点**）。
   -1 = 这一段的句子还没出现在时间线里（还没轮到它）⇒ 面板不许出。 */
function nightNodeIdx(nid){
  if (!nid) return -1;
  var tl = (PUBLIC && PUBLIC.timeline) || [];
  for (var j = 0; j < tl.length; j++){
    if (tl[j].node === nid) return j;
  }
  return -1;
}
/* 这一段的那句播报**念完了没有**（用户定：面板与读秒都在"播报刚结束"）。
   兜底 = "最近一次念完一行已过去 8 秒"（音频真坏了才命中）；
   **不要**改成"从节点出现算起 N 秒"—— 那会在播报刚开始就放行。 */
function linePlayed(node){
  /* 🔴 2026-10-07 第十期（兑现"四时钟"账）：**面板该在哪句之后开，由服务端下发**
     （`NOW.panel_line` = 时间线里本段那句的下标）—— 客户端不再自己
     nightNodeIdx/nightStageIdx 猜下标。老服务端没有这个字段时才退回旧算_indexes。 */
  var jn = (typeof NOW !== 'undefined' && NOW
            && typeof NOW.panel_line === 'number' && NOW.panel_line >= 0)
    ? NOW.panel_line : (node ? nightNodeIdx(node) : -1);
  /* 🔴 2026-10-07 第十起：原来 `jn < 0` 也放行 —— 那等于"还没轮到就能进"。
     现在找不到这句就**不放行**（下面的音频卡死兜底仍会兜住，不会永久憋死）。 */
  if (jn < 0) return false;
  if (!VOICE_ON || PAUSED) return true;         /* 语音关掉 ⇒ 没有"念完"这回事 */
  if (SPOKE_DONE && SPOKE_DONE.idx >= jn && SPOKE_DONE.at > 0) return true;
  /* 音频卡死兜底：最近一次念完一行已过去 20 秒才放行。
     🔴 第十起：原来是 **8 秒** —— 而夜里"上一人闭眼(≈3s)+留白3s+本段睁眼(≈3s)"
     恰好压在 8 秒线上 ⇒ 音频稍慢，面板就在"前一人刚闭眼"时被放行（用户实测）。
     20 秒意味着：正常节奏永远到不了，只有队列真死了才会命中。 */
  var last = (SPOKE_DONE && SPOKE_DONE.at) || _nodeSeenAt || 0;
  return !!(last && Date.now() - last > 20000);
}
function nightReady(need){
  if (!need) return true;
  if (!VOICE_ON || PAUSED) return true;
  /* 🔴 2026-10-07 第三期：**认节点 id** —— 服务端给每个夜间待办带 `node`，
     门控 = "时间线里带这个 node 的那一句已经被播到"。
     比旧的"stage === role"强在：同一个角色的**第二个窗口**（女巫毒药）分得开，
     而且播报改成按窗口增量补播之后，"第几句"根本不再对应"哪一段"。 */
  if (need.node){
    /* 老服务端兜底；**不许挂音频游标 SPOKEN**（会让所有面板永不出现）。见 §10.7。 */
    var jn = nightNodeIdx(need.node);
    if (jn < 0) return false;
    var jc = PUBLIC.night_node ? nightNodeIdx(PUBLIC.night_node) : -1;
    if (jc >= 0 && jn > jc) return false;
    return true;
  }
  var role = need.role;
  if (!role) return voicedUp();       /* 非夜间动作：发言/投票沿用整队播报门 */
  var j = nightStageIdx(need);
  if (j < 0) return voicedUp();       /* 老局没有 stage 标 ⇒ 整队门兜底 */
  return SPOKEN > j;
}
