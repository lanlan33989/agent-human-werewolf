/* 共享录音模块 —— 把麦克风 PCM 采成 16k 单声道 WAV。
 *
 * 为什么要抽成模块：语音探针页与对局界面都要录音。
 * 上一轮我分散实现时踩过一个坑 —— 用目标采样率(16k)去比实际采样率(48k)的采样数，
 * 阈值被放宽 3 倍，0.09 秒的空录音也能通过。集中一处就不会再漂移。
 *
 * 另外两个必须一起处理的点：
 *   1. 安全上下文：getUserMedia 只在 HTTPS 或 localhost 下可用，必须先在页面上自检
 *   2. 半双工：AI 说话时不能把它的声音当成用户输入上行（回声/自激）
 */
window.Rec = (function () {
  const SR = 16000;                 // 上行统一 16k 单声道
  const MIN_SECONDS = 0.3;          // 短于这个时长视为误触

  let ctx = null, stream = null, node = null, mute = null;
  let recording = false, monitoring = false;
  let chunks = [];
  // 流式识别（2026-09-19）：边录边把 PCM 分片发出去 ⇒ 说话的同时就在出字。
  // 和"录完再上传"并存：调用方选哪条，streamSink 为空时这个分支就是死的。
  let streaming = false, streamSink = null;
  // 诊断计数：**开过几条识别会话**。手机上一次按压会同时触发 touchstart 和
  // 合成 mousedown（2026-09-19 的事故），于是开出两条会话 —— 这条计数就是它的眼睛。
  let sessions = 0, lastErr = '';

  const api = {
    SR: SR,
    onlevel: null,                  // (rms 0..1) => void，每帧回调，用于插嘴检测
    sampleRate: () => (ctx ? ctx.sampleRate : 0),
    isRecording: () => recording,
  };

  /* ---------------------------------------------------------- 能力自检 */

  api.diagnose = function () {
    if (!window.isSecureContext) {
      return '这个页面不是安全上下文，浏览器在这种页面上直接禁用了麦克风。'
        + '这跟浏览器和手机型号无关，换哪个都一样。'
        + '解法：本机用 http://localhost:8000 打开，或用 HTTPS 打开。';
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return '这个浏览器没有暴露麦克风接口。换 Chrome / Edge / Safari 的较新版本再试。';
    }
    return null;
  };

  api.describeError = function (e) {
    const name = (e && e.name) ? e.name : '';
    if (name === 'NotAllowedError' || name === 'SecurityError') {
      return '浏览器拒绝了麦克风授权。检查地址栏左侧的权限图标，把麦克风改成允许，然后刷新。';
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return '没有检测到麦克风设备。';
    }
    if (name === 'NotReadableError') {
      return '麦克风被别的程序占用了（会议软件、录音软件等），关掉再试。';
    }
    if (name === 'TypeError') {
      return api.diagnose() || '接口调用方式不对。';
    }
    return (e && e.message) ? e.message : String(e);
  };

  /* ---------------------------------------------------------- 初始化 */

  api.ensure = async function () {
    /* 🔴 **不能**"有 ctx 就 return"。
       2026-09-19 实测的线上问题：流式播报（api.speak）为了放音频会先建一个 ctx，
       而这里原来的写法见到 ctx 就直接返回 ⇒ **放过语音之后麦克风那条链永远不会建立**
       （没人调 getUserMedia）⇒ 按住说话录到的永远是空的，界面上只看到"没有录到"。
       判据必须是"**麦克风那条链**建好了没有"，不是"有没有 ctx" —— 两件事。 */
    if (ctx && ctx.state === 'suspended') { await ctx.resume(); }
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (stream && node) return;          // 麦克风链已就绪（ctx 可能来自播报，那不算）

    const diag = api.diagnose();
    if (diag) throw new Error(diag);
    /* 约束：先按"对 ASR 最有利"要（关掉 AEC/ANS/AGC —— 它们会削发言开头、破坏齿音、
       抬底噪），**但要不到就退回最朴素的请求**。
       ⚠️ 这是"电脑上好好的、手机上死活录不到"的经典成因：这些约束不是所有手机都给得了，
       给不了时 getUserMedia 直接报 OverconstrainedError / NotReadableError（权限明明已允许）。
       先能录到，再谈音质。 */
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
          channelCount: 1
        }
      });
    } catch (e) {
      lastErr = '约束被拒（' + ((e && e.name) || e) + '），已退回默认音频';
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    }
    const src = ctx.createMediaStreamSource(stream);
    node = ctx.createScriptProcessor(4096, 1, 1);
    mute = ctx.createGain();
    mute.gain.value = 0;              // 不把自己的声音放出来（否则自激）
    node.onaudioprocess = e => {
      const buf = e.inputBuffer.getChannelData(0);
      if (recording) chunks.push(new Float32Array(buf));
      if (streaming && streamSink) streamSink(buf, ctx.sampleRate);
      if (api.onlevel) {
        let s = 0;
        for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
        api.onlevel(Math.sqrt(s / buf.length));
      }
    };
    src.connect(node);
    node.connect(mute);
    mute.connect(ctx.destination);
  };

  /* ---------------------------------------------------------- 录制 */

  api.start = async function () {
    await api.ensure();
    chunks = [];
    recording = true;
    return ctx.sampleRate;
  };

  api.stop = async function () {
    if (!recording) return null;
    recording = false;

    let total = 0;
    chunks.forEach(c => { total += c.length; });
    const all = new Float32Array(total);
    let o = 0;
    chunks.forEach(c => { all.set(c, o); o += c.length; });
    chunks = [];

    const seconds = all.length / ctx.sampleRate;   // 用实际采样率算阈值，别用 SR
    if (seconds < MIN_SECONDS) {
      return { tooShort: true, seconds: seconds };
    }
    const pcm = downsample(all, ctx.sampleRate, SR);
    return { blob: encodeWav(pcm, SR), seconds: seconds };
  };

  /* 放掉**麦克风那条链**（ctx 留着 —— 播报还要用它）。
   *
   * 为什么需要这个口子（2026-09-30，两条独立的原因，别删）：
   *   ① **引擎切换会互相抢麦**：本地识别靠 `getUserMedia` 拿着麦克风；
   *      内置识别（Web Speech）是**它自己去拿**的。我们不放，两边就撞，
   *      表现是内置识别报 `audio-capture`（"拿不到麦克风"）——
   *      而用户看到的是"切成内置识别就坏"，没人会想到是上一条链没撒手。
   *   ② 实测踩到的（走查里现形）：把 `getUserMedia` 换成静音流的**桩撤掉之后**，
   *      `ensure()` 因为 `if (stream && node) return` 直接返回 ⇒
   *      下一次录音拿到的**还是那条已经作废的静音流**，识别永远为空、
   *      界面显示"没听清" —— 录音链路看起来一切正常，这才是最难查的形状。
   */
  api.release = function () {
    try { if (node) node.disconnect(); } catch (e) { /* ignore */ }
    try { if (mute) mute.disconnect(); } catch (e) { /* ignore */ }
    try { if (stream) stream.getTracks().forEach(t => t.stop()); } catch (e) { /* ignore */ }
    node = null; mute = null; stream = null;
    recording = false; streaming = false; streamSink = null; chunks = [];
  };

  /* ---------------------------------------------------------- 流式识别
   *
   * 和「录完再上传」并存的第二条路：边录边把 **16k PCM 分片**发到 `/ws/asr`，
   * 服务端中继到云端 realtime ASR，增量文字立刻回调。
   *
   * 为什么值得多一条路（都是实测，不是感觉）：
   *     整段上传：**说完之后**还要等 1.2~1.4 秒才出字
   *     流式：    首字 **0.49 秒**（音频总长 5.2 秒）—— 还在说的时候字就出来了
   *
   * ⚠️ 上游 VAD 会自动断句 ⇒ **一次会话可能有好几个 final**。所以这里把每个
   *    final 都累加起来（`finalText`）——只留最后一个会丢半句，而且毫无提示。
   * ⚠️ 收尾必须发 `{type:"finish"}`：不发的话上游不会把缓冲区里最后半句识别完。
   *    服务端那边已经处理了"收尾要等上游 session.finished"的顺序坑。
   * ⚠️ 服务端才是 key 的持有者（浏览器里不放 key）——所以这里连的是**我们自己的** /ws/asr。
   */
  api.stream = async function (opts) {
    opts = opts || {};
    const onPartial = opts.onPartial || function () {};
    const onFinal = opts.onFinal || function () {};
    /* 错误统一记一笔 lastErr：手机上没有控制台，界面上那行诊断就是唯一的线索。
       否则用户只能说"没反应"，我们只能猜。 */
    const onErrorCb = opts.onError || function () {};
    /* hardFail：这条会话有没有"硬失败"（≠ 没识别出字）。引擎层靠它决定要不要换下一层。 */
    let hardFail = null;
    const onError = function (e) {
      hardFail = hardFail || e;
      lastErr = String((e && e.message) || e);
      onErrorCb(e);
    };
    sessions++;                    // 开一条会话就 +1（双触发会看到 2 —— 这就是它的眼睛）
    await api.ensure();

    const proto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
    const ws = new WebSocket(proto + location.host + '/ws/asr');
    ws.binaryType = 'arraybuffer';

    let ready = false, closed = false;
    const carry = [];              // 攒到 100ms 再发一片（小块更跟手，但别一个采样发一次）
    const pending = [];            // ready **之前**收到的 PCM：先攒着，ready 后补发
    const finals = [];
    let sentSamples = 0;
    let flushPending = function () {};   // ready 时补发（下面赋值；这里先占位给 onmessage 用）

    ws.onmessage = ev => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === 'ready') { ready = true; flushPending(); }
      else if (m.type === 'partial') { onPartial((m.text || '') + (m.stash || '')); }
      else if (m.type === 'final') {
        finals.push(m.transcript || '');
        onFinal(finals.join(''));
      } else if (m.type === 'error') {
        closed = true;
        onError(new Error(m.message || '服务端流式识别出错'));
      } else if (m.type === 'finished') {
        closed = true;
        try { ws.close(); } catch (e) { /* 已经关了 */ }
      }
    };
    ws.onerror = () => {
      if (!closed) { closed = true; onError(new Error('流式连接失败（/ws/asr 不通？）')); }
    };
    ws.onclose = () => {
      /* 🔴 2026-09-30：**"干净关闭"也是一种失败**（用户报"麦没法用、诊断却显示正常"，
         根因就在这一行）。阿里云账号欠费时，上游把我们这条连接**正常地**关掉
         （客户端只看到 code 1000 OK、没有任何 error 事件），而这里原来只是
         `closed = true` —— 既不报错也不提示 ⇒ 界面上"点了说话没反应"，
         诊断面板的"最近错误"是空的，用户只能说"麦坏了"，双方都只能猜。
         规矩：**只要一个字都没识别出来就闭嘴关掉，就是失败，必须说出来。**
         （服务端现在也会先发一条 type=error 说明原因；这条是兜底 —— 两条路都堵上。） */
      const alreadyClosed = closed;
      closed = true;
      if (!alreadyClosed && finals.length === 0) {
        onError(new Error('识别通道没返回任何结果就被关掉了（云端被拒？欠费 / 未开通 / 额度用尽）'));
      }
    };

    /* ⚠️ **不等 ready 就返回**（实测踩过）：
       原来这里会 `await` 到 ready 才返回（最长 8 秒），带来两个问题 ——
         ① 用户"按下就松手"时，调用方还在 await 这个函数、手上**没有句柄可以 stop**
            ⇒ play.html 那边落进"录完再传"分支，而麦克风其实还没开始录 ⇒ 界面报"没有录到"
         ② 头几秒的音频卡在麦克风里白等
       现在：立刻返回，ready 之前收到的 PCM 先**攒着**（最多 1.5 秒），ready 后一次性补发。
       这样"按下去就开始说"的头几个字不会被吞掉。 */
    const sendCarry = () => {
      while (carry.length >= SR / 10) {                  // 每片 100ms
        const chunk = carry.splice(0, SR / 10);
        /* 🔴 必须走 toPcm16（见它的注释）。原来这里是 `new Int16Array(chunk)` ——
           `chunk` 是 [-1,1] 的浮点，而 Int16Array 会**把非整数截断成 0** ⇒
           发给云端的每一片都是**静音** ⇒ 云端一个字都识别不出来，
           界面只会显示"没听清"。这是与 `encodeWav` 同源的**第二份**拷贝，
           2026-09-30 一起修掉（两条上行路都必须被同一处量化管着）。 */
        if (ws.readyState === 1) ws.send(toPcm16(chunk).buffer);
        sentSamples += chunk.length;
      }
    };
    flushPending = () => {
      if (closed || ws.readyState !== 1) return;
      for (let i = 0; i < pending.length; i++) carry.push(pending[i]);
      pending.length = 0;
      sendCarry();
    };
    const waitReady = async (ms) => {
      const t0 = Date.now();
      while (!ready && !closed && Date.now() - t0 < ms) {
        await new Promise(r => setTimeout(r, 50));
      }
      return ready;
    };

    streamSink = (buf, rate0) => {
      const pcm = downsample(buf, rate0, SR);
      if (!ready || closed) {
        for (let i = 0; i < pcm.length; i++) pending.push(pcm[i]);
        while (pending.length > SR * 1.5) pending.shift();   // 只留最近 1.5 秒
        return;
      }
      for (let i = 0; i < pcm.length; i++) carry.push(pcm[i]);
      sendCarry();
    };
    streaming = true;

    return {
      engine: 'cloud',
      isReady: () => ready,
      finalText: () => finals.join(''),
      finalCount: () => finals.length,
      failed: () => hardFail,
      stop: async function () {
        streaming = false;
        streamSink = null;
        if (!closed) {
          // 松手时可能还没 ready（用户按得快）：稍等一下、把攒下的音频补发再收尾，
          // 否则"一按就说"的那几个字会整段丢掉。
          if (!ready) await waitReady(2000);
          flushPending();
          try { ws.send(JSON.stringify({ type: 'finish' })); } catch (e) { /* ignore */ }
          const t1 = Date.now();
          while (!closed && Date.now() - t1 < 8000) {
            await new Promise(r => setTimeout(r, 80));
          }
        }
        try { ws.close(); } catch (e) { /* ignore */ }
        return { text: finals.join(''), seconds: sentSamples / SR,
                 finalCount: finals.length };
      },
      cancel: function () {
        streaming = false;
        streamSink = null;
        closed = true;
        try { ws.send(JSON.stringify({ type: 'cancel' })); } catch (e) { /* ignore */ }
        try { ws.close(); } catch (e) { /* ignore */ }
      }
    };
  };

  api.isStreaming = () => streaming;

  /* 诊断用：把"两条链各自建好了没有"暴露出来。
     ⚠️ 分开报，是因为它们**是两条独立的链** —— 播报只用 ctx，录音还要麦克风那条。
     混成一个 bool 正是上面那个 bug 的温床（"有 ctx 就算好了"）。 */
  api.diag = function () {
    return {
      ctx: !!ctx,
      ctxState: (ctx ? ctx.state : 'none'),   // 手机上常见 suspended ⇒ 一点声音都没有
      mic: !!(stream && node),        // 麦克风链：getUserMedia + ScriptProcessor
      recording: recording,
      streaming: streaming,
      sessions: sessions,             // 开过几条识别会话（双触发会看到 2）
      lastErr: lastErr,
      sampleRate: (ctx ? ctx.sampleRate : 0),
      /* 识别引擎层（2026-09-30）：面板要能回答"现在会用哪一层、为什么"。
         engineUsed = 最近一次真正用起来的；pick = 下一次会选的（含理由）。 */
      engineUsed: engineInfo,
      engineUsedLabel: engineInfo ? api.engineLabel(engineInfo) : '',
      pick: api.pickEngine(),
      enginePin: api.pinnedEngine,
      speechSupported: api.speechSupported()
    };
  };

  /* 手机上的音频解锁（自动播放策略）：
     AudioContext 必须在**用户手势里**创建/恢复，否则它一直是 suspended ——
     表现是"AI 说话一点声音都没有"（桌面 Chrome 宽松，手机上必现）。
     放一个 1 帧的静音片是因为 iOS 上只 resume 有时还不够。 */
  api.unlock = async function () {
    try {
      if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (ctx.state === 'suspended') {
        try { await ctx.resume(); } catch (e) { /* 手势里失败就先放着 */ }
      }
      const b = ctx.createBuffer(1, 1, ctx.sampleRate);
      const s = ctx.createBufferSource();
      s.buffer = b;
      s.connect(ctx.destination);
      s.start(0);
      return ctx.state;
    } catch (e) {
      lastErr = 'unlock: ' + e;
      return 'error';
    }
  };

  /* ---------------------------------------------------------- 流式播报
   *
   * 边合成边出声：接 `/ws/tts`（服务端中继到云端 realtime TTS），
   * PCM16 分片到一片就**排一片**到 Web Audio。
   *
   * 为什么用 Web Audio 排片、而不是 <audio> + MediaSource：
   * 上游给的是**裸 PCM16**（没有容器），<audio> 播不了裸 PCM；
   * createBuffer + 按时序 start() 是流式 PCM 最直接的做法，也不用额外转码。
   *
   * 实测（2026-09-19，4.56 秒的一句话）：
   *     合成完再播：要等 1.05 秒才拿到第一个字节
   *     流式：      **首片音频 471 毫秒**就出声，合成在后台继续
   *
   * 返回 { promise, stop, firstMs, error }：
   *     promise 在**播完**时 resolve(true)，被 stop 打断时 resolve(false)
   *     error() 非空表示这一句**一个音都没出**（调用方据此回落成"合成完再播"）
   */
  api.speak = async function (path, opts) {
    opts = opts || {};
    const proto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
    // 播报不碰麦克风 ⇒ 这里**不能**调 ensure()（那会弹麦克风授权）
    if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
    if (ctx.state === 'suspended') { try { await ctx.resume(); } catch (e) { /* ignore */ } }

    const ws = new WebSocket(proto + location.host + path);
    ws.binaryType = 'arraybuffer';
    const srcs = [];
    let nextTime = 0, rate = 24000, done = false, failed = null, firstAt = null;
    const t0 = performance.now();
    let resolveDone = function () {};
    const finished = new Promise(r => { resolveDone = r; });

    ws.onmessage = ev => {
      if (typeof ev.data === 'string') {
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.type === 'start') {
          rate = m.sample_rate || 24000;
          /* ⚠️ 这里**不能**就把 nextTime 定成 now + lead —— `start` 事件比音频早到约 1 秒
             （上游还要合成），等第一片 PCM 到的时候那个时间点早就过期了。
             排片时间统一交给下面那处"钳到当前时刻"处理。 */
          nextTime = 0;
        } else if (m.type === 'done') {
          done = true;
          try { ws.close(); } catch (e) { /* ignore */ }
        } else if (m.type === 'error') {
          failed = new Error(m.message || '流式 TTS 出错');
          try { ws.close(); } catch (e) { /* ignore */ }
        }
        return;
      }
      if (!nextTime) nextTime = ctx.currentTime + 0.08;
      const pcm = new Int16Array(ev.data);
      const buf = ctx.createBuffer(1, pcm.length, rate);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
      const s = ctx.createBufferSource();
      s.buffer = buf;
      s.connect(ctx.destination);
      if (firstAt === null) {
        firstAt = performance.now() - t0;
        if (opts.onFirst) opts.onFirst(firstAt);
      }
      /* 🔴 排片时间**绝不能落在过去**。
         实测（2026-09-19 用户反馈"一卡一卡、像两三个人交替在说一句话"）：
         `start` 事件先到、第一片 PCM 晚到约 1 秒，那一刻 nextTime 已经过期 ⇒
         `start(过去时间)` 被浏览器**立即播放**，前几片于是挤在同一瞬间互相盖住。
         钳到"现在 + 一点余量"就正常了（也让首片一到达就出声，这正是要的）。 */
      const _now = ctx.currentTime;
      if (nextTime < _now + 0.02) nextTime = _now + 0.05;
      s.start(nextTime);
      srcs.push(s);
      nextTime += buf.duration;
      s.onended = () => {
        const i = srcs.indexOf(s);
        if (i >= 0) srcs.splice(i, 1);
        if (!srcs.length && done) resolveDone(true);
      };
    };
    ws.onerror = () => { if (!failed) failed = new Error('流式 TTS 连接失败'); };
    ws.onclose = () => {
      if (!done && !srcs.length) {
        // 一个音都没排上：算失败（调用方好回落），别静默什么都不放
        if (!failed) failed = new Error('流式 TTS 没给出音频');
        resolveDone(false);
      } else if (done && !srcs.length) {
        resolveDone(true);
      }
    };

    return {
      firstMs: () => firstAt,
      promise: finished,
      error: () => failed,
      stop: function () {
        failed = failed || new Error('已跳过');
        try { ws.close(); } catch (e) { /* ignore */ }
        srcs.forEach(s => { try { s.stop(); } catch (e) { /* ignore */ } });
        srcs.length = 0;
        resolveDone(false);
      }
    };
  };

  /* 只监听不录：AI 说话时用它检测「人插嘴」，但不把 AI 的声音上行 */
  api.monitor = async function (on) {
    if (on) {
      await api.ensure();
      chunks = [];
      monitoring = true;
    } else {
      monitoring = false;
    }
  };
  api.isMonitoring = () => monitoring;

  /* ================================================================ 识别引擎层
   * 2026-09-30 加。**起因是阿里云账号欠费**：云端实时识别被直接拒（Access denied），
   * 而用户看到的是"麦没法用、诊断面板却显示正常"。
   * 免费兜底的能力其实项目里早就有（本地 faster-whisper、浏览器内置识别），
   * 缺的是两件事：**接通**（前端根本不会回落到它们）和**说清楚**（失败被显示成"你说不清"）。
   *
   * 三层，按"好用程度 + 免费"排序：
   *   webspeech  浏览器内置（Web Speech API）：免费、边说边出字；音频走浏览器厂商的服务器
   *   local      本地 whisper（服务端 /api/utter）：免费、离线、不外发；说完等几秒
   *   cloud      云端流式（/ws/asr）：原设计、质量最好；要 key 且账号得有钱
   *
   * 三条规矩（跟项目其它地方一致，别丢）：
   *   ① **失败必须留痕**：某层硬失败就记时间戳，TTL 内不再优先 —— 自动回落是有据可查的
   *   ② **必须明示**：engineInfo() 随时能答"现在用哪一层、为什么"（禁止静默降级）
   *   ③ **可以钉住**：把某一层写进 localStorage，钉住后不再自动切（诊断面板上的按钮）
   */
  const ENGINE_LABEL = {
    webspeech: '浏览器内置识别',
    local: '本地识别（whisper）',
    cloud: '云端流式识别'
  };
  const ASR_FAIL_TTL = 10 * 60 * 1000;            // 某层失败后，10 分钟内不再优先试它
  /* 连续失败 ≥2 次 ⇒ 一天内不再优先（2026-10-01 加）。
     为什么需要：Chrome 的内置识别**必须连浏览器厂商的服务器**（Google）。
     在国内网络下它**必然**失败 —— 只按 10 分钟 TTL 的话，每过 10 分钟就会
     再浪费用户一次（点了、说了、等来一句"网络不通"）。连续失败两次就当环境不支持。 */
  const ASR_FAIL_TTL_HARD = 24 * 60 * 60 * 1000;
  const PIN_KEY = 'tt.asr.pin';
  const FAIL_KEY = 'tt.asr.fail.';                // 最近一次失败时间
  const FAILN_KEY = 'tt.asr.failn.';              // 连续失败次数（成功一次清零）
  let engineInfo = '';

  function lsGet(k) { try { return localStorage.getItem(k) || ''; } catch (e) { return ''; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 隐私模式写不了，无所谓 */ } }

  api.engineLabel = function (name) { return ENGINE_LABEL[name] || name || '?'; };
  api.speechSupported = function () {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  };
  api.engineInfo = function () { return engineInfo; };
  api.pinnedEngine = lsGet(PIN_KEY);

  /* 手动钉一层（'' = 恢复自动）。诊断面板上的按钮用它。 */
  api.pinEngine = function (name) {
    if (name && !ENGINE_LABEL[name]) return api.pinnedEngine;
    api.pinnedEngine = name || '';
    lsSet(PIN_KEY, api.pinnedEngine);
    return api.pinnedEngine;
  };

  function markEngineFailed(name, why) {
    const n = Number(lsGet(FAILN_KEY + name) || 0) + 1;
    lsSet(FAILN_KEY + name, String(n));
    lsSet(FAIL_KEY + name, String(Date.now()));
    /* ⚠️ 这行会让诊断面板的"最近错误"带上引擎名 —— 走查/排查就是靠**有没有这个前缀**
       判断"这次失败到底被记下来没有"。别把它改成不带前缀的写法。 */
    lastErr = api.engineLabel(name) + '失败（第 ' + n + ' 次）：' + why;
  }
  /* 成功一次就把连续失败清零（否则一次偶发网络抖动会把它钉死一整天） */
  function markEngineOk(name) {
    if (Number(lsGet(FAILN_KEY + name) || 0) !== 0) lsSet(FAILN_KEY + name, '0');
  }
  function engineRecentlyFailed(name) {
    const t = Number(lsGet(FAIL_KEY + name) || 0);
    if (!t) return false;
    const n = Number(lsGet(FAILN_KEY + name) || 0);
    return (Date.now() - t) < (n >= 2 ? ASR_FAIL_TTL_HARD : ASR_FAIL_TTL);
  }

  /* 选哪一层。**纯函数**（只读 localStorage）：调用方可以先问再动手，好把界面文案对齐。
   *
   * 🔴 2026-10-01 **默认顺序调整过**（用户实测结论：本地 whisper 可以、阿里云可以、
   *    浏览器内置不行）。原来把"浏览器内置"排第一，但它**必须连浏览器厂商（Google）的
   *    服务器** —— 国内网络下必然失败，等于每次开局先白搭一次、还甩用户一句
   *    「当前网络不通」，看起来就像功能坏了。现在的顺序是：
   *      ① cloud      阿里云流式：质量最好 + 边说边出字（首片约 0.5 秒）
   *      ② local      本地 whisper：免费、离线、不外发；说完等 3~6 秒
   *      ③ webspeech  浏览器内置：**只当手动可选项**（有梯子 / 换网络时才用得上）
   *    任何一层失败都**当场留痕**并自动换下一层；要钉住某层就按诊断面板上的按钮。 */
  api.pickEngine = function () {
    const pin = api.pinnedEngine;
    if (pin && ENGINE_LABEL[pin]) {
      return { engine: pin, why: '手动指定', pinned: true };
    }
    if (!engineRecentlyFailed('cloud')) {
      return { engine: 'cloud', why: '阿里云流式：质量最好、边说边出字；失败会自动落到本地' };
    }
    if (!engineRecentlyFailed('local')) {
      return { engine: 'local', why: '本地 whisper：免费、离线、不外发；说完等几秒' };
    }
    if (api.speechSupported() && !engineRecentlyFailed('webspeech')) {
      return { engine: 'webspeech',
               why: '浏览器内置：免费、边说边出字（需要连得上浏览器厂商的服务器）' };
    }
    /* 三层刚都失败过：仍挑**本地**（离线、免费，最不依赖外部条件），并把话说清楚。 */
    return { engine: 'local', why: '前三层刚都失败过，仍用本地 whisper（离线、免费）' };
  };

  /* ---------- 引擎 A：浏览器内置识别（Web Speech API + 同类 handle） ---------- */
  api.speechStream = async function (opts) {
    opts = opts || {};
    const onPartial = opts.onPartial || function () {};
    const onFinal = opts.onFinal || function () {};
    const onErrorCb = opts.onError || function () {};
    let hard = null;                       // 硬失败（和"用户没说话"是两件事）
    const onError = function (e) {
      hard = hard || e;
      lastErr = String((e && e.message) || e);
      onErrorCb(e);
    };
    const Klass = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Klass) { const e = new Error('这个浏览器没有内置语音识别'); onError(e); throw e; }
    sessions++;                            // 跟流式那条共用计数：双触发会看到 2

    const rec = new Klass();
    rec.lang = opts.lang || 'zh-CN';
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;

    const finals = [];
    let interim = '', closed = false, stopRequested = false, restarts = 0;
    const t0 = Date.now();
    let resolveStopped = function () {};
    const stoppedP = new Promise(r => { resolveStopped = r; });

    rec.onresult = ev => {
      interim = '';
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        const txt = (r[0] && r[0].transcript) || '';
        if (r.isFinal) finals.push(txt); else interim += txt;
      }
      onPartial(finals.join('') + interim);
      if (finals.length) onFinal(finals.join(''));
    };
    rec.onerror = ev => {
      const code = (ev && ev.error) || 'unknown';
      if (code === 'aborted') return;        /* 我们自己 abort 的，不算故障 */
      /* ⚠️ no-speech **不算故障**：用户就是没出声。把它记成故障会让一次静默
         就把内置识别踢掉 10 分钟，面板上还会显示一条假故障。 */
      if (code === 'no-speech') return;
      if (code === 'not-allowed' || code === 'service-not-allowed') {
        onError(new Error('内置识别被拒绝（' + code + '）：多半是没给"语音识别/麦克风"权限'));
      } else if (code === 'network') {
        onError(new Error('内置识别要联网（识别在浏览器厂商的服务器上做），当前网络不通'));
      } else if (code === 'audio-capture') {
        onError(new Error('内置识别拿不到麦克风（可能被别的 App 占着）'));
      } else {
        onError(new Error('内置识别出错：' + code));
      }
    };
    rec.onend = () => {
      if (closed) return;
      /* ⚠️ Android Chrome 上 continuous 一遇静音就会 end。**还没让它停就自己续上**，
         否则"说完第一句就再也不出字"—— 那比截断更糟：看起来像卡死。
         但续的次数要封顶，不然网络断了会变成不停重试。 */
      if (!stopRequested && !hard && restarts < 20) {
        restarts++;
        try { rec.start(); return; } catch (e) { onError(e); }
      }
      closed = true;
      resolveStopped();
    };
    try { rec.start(); } catch (e) { closed = true; onError(e); throw e; }

    return {
      engine: 'webspeech',
      isReady: () => true,
      finalText: () => finals.join(''),
      finalCount: () => finals.length,
      failed: () => hard,
      stop: async function () {
        stopRequested = true;
        try { rec.stop(); } catch (e) { /* ignore */ }
        await Promise.race([stoppedP, new Promise(r => setTimeout(r, 3000))]);
        closed = true;
        /* ⚠️ 最后半句常常只以 interim 存在（尤其"说完就点结束"）。一个字都定不下来时
           用草稿兜底，比"没听清"强 —— 反正填进输入框后玩家还能改。 */
        return { text: finals.join('') || interim,
                 seconds: (Date.now() - t0) / 1000, finalCount: finals.length };
      },
      cancel: function () {
        stopRequested = true; closed = true;
        try { rec.abort(); } catch (e) { /* ignore */ }
      }
    };
  };

  /* ---------- 引擎 B：本地识别（服务端 /api/utter，跑 faster-whisper） ---------- */
  api.localStream = async function (opts) {
    opts = opts || {};
    let hard = null;
    sessions++;
    /* 这条要用标准 getUserMedia 录音 ⇒ 会跟内置识别抢麦克风，所以**两条链不会同时在跑**。
       这也是为什么它跟"录完再传"共用 api.start/api.stop/api.transcribe，一行新逻辑都没有。 */
    await api.ensure();
    await api.start();
    return {
      engine: 'local',
      isReady: () => true,
      finalText: () => '',
      finalCount: () => 0,
      failed: () => hard,
      stop: async function () {
        const r = await api.stop();
        if (!r) return { text: '', seconds: 0, tooShort: true };
        if (r.tooShort) return { text: '', seconds: r.seconds, tooShort: true };
        try {
          const j = await api.transcribe(r.blob, { asrOnly: true });
          const t = (j && j.text) || '';
          if (opts.onFinal) opts.onFinal(t);
          return { text: t, seconds: r.seconds, roundtrip_ms: j && j.roundtrip_ms };
        } catch (e) {
          hard = e;
          lastErr = '本地识别失败：' + ((e && e.message) || e);
          if (opts.onError) opts.onError(e);
          return { text: '', seconds: r.seconds };
        }
      },
      cancel: function () { try { api.stop(); } catch (e) { /* ignore */ } }
    };
  };

  /* ---------- 统一入口：按优先级选一层，硬失败自动换下一层 ---------- */
  api.listen = async function (opts) {
    opts = opts || {};
    const pick = api.pickEngine();
    engineInfo = pick.engine;
    const userOnError = opts.onError || function () {};
    let recorded = false;              // 这一次会话的失败是否已经记过（避免数两遍）
    /* 🔴 **失败要当场留痕**（2026-10-01 实测踩到，别再挪回收尾里）：
       内置识别的 `network` 错误往往在用户"点结束"**之前**就发生了，而原来只在
       `stop()` 里检查 ⇒ 那次失败**根本没被记下来** ⇒ 下一次仍然先选它 ⇒
       用户反复点、反复"这个不行"（面板"最近错误"有字，而"识别引擎"那行还写着它）。
       当场记录之后，下一次就会自动跳过它、落到下一层 —— 这才叫回退。 */
    const wrappedOpts = Object.assign({}, opts, {
      onError: function (e) {
        recorded = true;
        markEngineFailed(pick.engine, String((e && e.message) || e));
        userOnError(e);
      }
    });
    let h;
    if (pick.engine === 'webspeech') {
      /* 内置识别**自己去拿麦克风** ⇒ 先把我们那条 getUserMedia 的链放掉，
         否则两边抢麦，内置识别报 audio-capture（见 api.release 的注释①）。 */
      api.release();
      h = await api.speechStream(wrappedOpts);
    }
    else if (pick.engine === 'local') h = await api.localStream(wrappedOpts);
    else h = await api.stream(wrappedOpts);
    h.label = api.engineLabel(pick.engine);
    h.why = pick.why;
    h.pinned = !!pick.pinned;
    /* stop 外面再兜一层（backstop）：万一某条路是"收尾时才发现失败"（HTTP 500、
       WebSocket error 等），这里补记一次 —— 但**只补记没记过的**，
       否则同一次失败数两遍，连续失败计数虚高、会被更快地整天拉黑。 */
    const rawStop = h.stop;
    h.stop = async function () {
      const r = await rawStop.apply(h, arguments);
      const f = h.failed ? h.failed() : null;
      if (f && !recorded) markEngineFailed(h.engine, String((f && f.message) || f));
      if (r && r.text) markEngineOk(h.engine);      // 出了字 ⇒ 这层是好的，连续失败清零
      return r;
    };
    return h;
  };

  /* ---------------------------------------------------------- 工具 */

  function downsample(buf, inRate, outRate) {
    if (inRate === outRate) return buf;
    const ratio = inRate / outRate;
    const n = Math.floor(buf.length / ratio);
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos);
      const i1 = Math.min(i0 + 1, buf.length - 1);
      const f = pos - i0;
      out[i] = buf[i0] * (1 - f) + buf[i1] * f;
    }
    return out;
  }

  /* float32[-1,1] → int16 PCM。**所有上行音频都必须过这一个函数**。
   *
   * 🔴 为什么单独抽出来（2026-09-30，这是个把整个语音功能废掉的 bug）：
   *    `new Int16Array(float32Array)` 和 `view.setInt16(off, floats[i])` 都会对
   *    **非整数直接截断**：0.26 → 0、0.9 → 0、-0.7 → 0。也就是说，直接把
   *    [-1,1] 的浮点采样丢进去，拿到的**永远是全零**。
   *    而浏览器的**两条**上行路（`encodeWav` 写 WAV、`api.stream` 发 ws 分片）
   *    原来**都**是这么干的 ⇒ 上行音频**永远是静音** ⇒ 云端/本地都识别不出字，
   *    界面只会说「没听清」，而麦克风、采样链、ASR、上传**全部正常**。
   *    这就是用户报的「麦还是没法用，我看诊断上面都显示正常」的根因。
   *    ⇒ 量化只留这一处，谁也别再自己写一遍。
   */
  function toPcm16(samples) {
    const out = new Int16Array(samples.length);
    for (let i = 0; i < samples.length; i++) {
      const v = samples[i] < -1 ? -1 : (samples[i] > 1 ? 1 : samples[i]);
      out[i] = Math.round(v * 32767);
    }
    return out;
  }
  api.pcm16 = toPcm16;      // 走查直接断言它（不需要麦克风，也不需要云端）

  function encodeWav(samples, rate) {
    const pcm = toPcm16(samples);                    // ← 唯一的一处量化
    const buf = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buf);
    const wr = (off, s) => {
      for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i));
    };
    wr(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true);
    wr(8, 'WAVE'); wr(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    wr(36, 'data'); view.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true);
    return new Blob([buf], { type: 'audio/wav' });
  }

  /* 上行：把一段 WAV 交给服务端做 ASR，返回 {text, timings, input_audio}
   *
   * opts.asrOnly = true 时**只做转写**，跳过 LLM 与 TTS。
   * 为什么需要：对局界面只要文字（识别完填进输入框，再由玩家点提交），
   * 但 /api/utter 默认会把这条链路跑到底 —— 一次 LLM + 一次 TTS，
   * 结果被调用方整个丢掉。LLM 是 mock 时看不出来，接真模型后就是白花钱。
   * 探针页不用这个开关，它要的就是完整链路（听写 → 回复 → 语音）。
   */
  api.transcribe = async function (blob, opts) {
    const t0 = performance.now();
    const asrOnly = !!(opts && opts.asrOnly);
    const res = await fetch('/api/utter' + (asrOnly ? '?only=asr' : ''), {
      method: 'POST',
      headers: { 'Content-Type': 'audio/wav', 'X-Sample-Rate': String(SR) },
      body: blob
    });
    const j = await res.json();
    j.roundtrip_ms = Math.round(performance.now() - t0);
    if (j.error) throw new Error(j.error);
    return j;
  };

  return api;
})();
