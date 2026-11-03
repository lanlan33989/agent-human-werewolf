/* ============================================================================
   judge_voice.js —— 法官播报本地合成通道（Web Speech API，2026-10-22 双通道改造）
   ----------------------------------------------------------------------------
   为什么：夜间相位切换的播报原先也走云端 TTS（~1s/句起步、波动大），相位推进
   的时序预算被它吃掉。法官台词是"固定模式 + 少量变量"（座位号、死讯），不需要
   音色人格 ⇒ 改用浏览器自带 speechSynthesis **本地合成**：文本到手就开口，
   首字 <300ms，完全不经服务器与云端。AI 玩家发言仍走云端（layers_tts.py）——
   那才需要音色人格与情绪。

   装载顺序：play.html 里排在 speech_ui.js **之前**。本文件只声明 +
   挂三个被动监听（首次手势解锁 / 离房收声 / 终局收声），不做任何主动播报。

   方案来源：《法官播报零延迟方案-Web-Speech-本地合成.md》（队友1 audio.ts 移植）。
   与原文档的两处刻意偏离（都在对应位置有注释）：
     ① 分句不用正则后行断言 (?<=...) —— iOS Safari <16.4 解析期就抛语法错误，
        整个文件会报废；改用捕获组重组，断句效果一字不差。
     ② 不另起"边沿触发的独立播报线" —— 本项目的 timeline 本身就是边沿触发的
        播报源（每条相位台词只出现一次），另起一线会双重播报 + 与门控失联。
        接入点在 speech_ui.js 的播报队列里：sys 条目路由到本通道，云端兜底。
   ============================================================================ */

window.JudgeVoice = (function () {
  'use strict';

  /* ---------------------------------------------------------- 参数（照抄方案，勿调） */
  var SPEECH_RATE = 0.98;     // 语速：法官要稳，比原速慢一点点
  var SPEECH_PITCH = 1.0;     // 音高：保持 1.0 别动 —— 神经音色自带完整语调，
                              // 降调会把好音色压得发闷发假（队友1 实测踩坑 4）
  var SPEECH_VOLUME = 0.95;
  var PHRASE_PAUSE_MS = 220;  // 分句间停顿
  /* 🔴 2026-10-29（用户报「法官全部音效又没了」）：
     `speak()` 被浏览器静默拒绝时（没手势 / 非安全上下文 / 合成器被系统接管），
     原来只能等**总时长看门狗**（最短 5 秒）才发现 —— 一句白等 5 秒，一整夜十
     几句就把播报队列拖后 60 秒以上，而 speech_ui 的"过期跳过"会把拖后的 sys
     句子全当迟到扔掉 ⇒ **法官整局一句不念**，且没有任何报错。
     现在拆成两道闸：起音闸（这么久还没 onstart 就判"不出声"）+ 总时长闸。 */
  var UTTER_START_MS = 900;
  /* 判定"本地通道不可用"后的降级时长。原来 5 分钟 ≈ 一整局 —— 一次误判就
     整局哑巴。改成 60 秒，且用户下次点屏幕（手势）立即解除（见 armUnlock）。 */
  var BROKEN_TTL_MS = 60 * 1000;

  /* 首选云扬（Edge 中文在线自然神经音色，男声，有权威感）；备选云希/晓晓。
     用关键词匹配，绝不写死整串名字 —— 同一个音色在不同 Edge 版本里后缀不一样
     （Chinese (Mainland) / Chinese (Mandarin, Simplified)），写死一换版本就断。 */
  var PREFERRED_VOICE = [/yunyang/i, /云扬/];
  var BACKUP_VOICES = [/yunxi/i, /云希/, /xiaoxiao/i, /晓晓/];

  /* ---------------------------------------------------------- 音色挑选 */
  // 自然（神经）音色识别
  function isNaturalVoice(v) {
    return /natural|neural|online|premium|enhanced|xiaoxiao|xiaoyi|yunxi|yunyang|yunxia|晓晓|云希|云扬/i
      .test(v.name + ' ' + v.voiceURI);
  }

  // 打分：神经语音 +100，natural/neural 等字样 +60，老拼接音色 +25，zh-CN +30，本地 +5
  function scoreVoice(v) {
    var s = 0;
    if (isNaturalVoice(v)) s += 100;
    if (/natural|neural/i.test(v.name)) s += 60;
    else if (v.localService) s += 25;
    if (/^zh\b|zh-CN|zh_CN/i.test(v.lang)) s += 30;
    if (v.localService) s += 5;
    return s;
  }

  function matchesAny(v, list) {
    for (var i = 0; i < list.length; i++) if (list[i].test(v.name)) return true;
    return false;
  }

  // 四级优先：云扬(自然) → 云扬(任意) → 备选(自然优先) → 全列表按分排序取最高。
  // ⚠️ 每次播报都重新挑、绝不缓存 —— 音色列表是异步到达的（voiceschanged），
  //    缓存"最早出现的那一个"会整局都错下去（踩坑 1）。getVoices() 很便宜。
  function pickVoice() {
    var vs;
    try { vs = window.speechSynthesis ? window.speechSynthesis.getVoices() : []; }
    catch (e) { return null; }
    if (!vs || !vs.length) return null;
    var natural = [], plain = [];
    var i, v;
    for (i = 0; i < vs.length; i++) {
      v = vs[i];
      if (!/^zh/i.test(v.lang || '') && !/中文|Chinese/i.test(v.name || '')) continue;
      (isNaturalVoice(v) ? natural : plain).push(v);
    }
    for (i = 0; i < natural.length; i++)
      if (matchesAny(natural[i], PREFERRED_VOICE)) return natural[i];
    for (i = 0; i < plain.length; i++)
      if (matchesAny(plain[i], PREFERRED_VOICE)) return plain[i];
    for (i = 0; i < natural.length; i++)
      if (matchesAny(natural[i], BACKUP_VOICES)) return natural[i];
    var pool = natural.length ? natural : plain;
    if (!pool.length) return null;
    pool.sort(function (a, b) { return scoreVoice(b) - scoreVoice(a); });
    return pool[0];
  }

  /* ---------------------------------------------------------- 分句念法 */
  // 只在 ，。！？；： （含全半角）处断——顿号（、）绝不断！顿号是列举内部停顿，
  // 在那儿断会变成「昨晚 3号、（停）7号 出局」，听着像结巴（踩坑 3）。
  // ⚠️ 不用 (?<=...) 后行断言：iOS Safari <16.4 在**解析期**就抛 SyntaxError，
  //    整个文件报废。用捕获组把标点并回前一句，效果与断言版一字不差。
  function splitForSpeech(text) {
    /* 🔴 2026-10-29（用户报「法官念数字声音有奇怪的延长」）：
       号码列举里的逗号被当成了断句点 —— "2，3，5号出局" 被切成
       ["2，", "3，", "5号出局，"] 三段，段间有 220ms 停顿、每段还要各自起音
       一次（Chrome 每句起音有 100~300ms 开销）⇒ 听感是"二……三……五号出局"，
       数字之间凭空多出近一秒。
       修法：**切分之前**把"号码之间的逗号/顿号"统一成顿号 ——
       顿号本来就在"绝不断句"的集合里（见下面注释），TTS 念 "2、3、5号"
       会自然连读。前瞻 `(?=)` 是 ES3 就有的语法，不碰 iOS 那个 `(?<=)` 的坑。 */
    text = String(text).replace(/(\d\s*号?)\s*[，,]\s*(?=\d)/g, '$1、');
    /* ★ 2026-10-29（用户第二次报："数字与汉字间不留顿号间隔"）：
       根因不在标点，在**空格** —— 座位显示名是 `f"{seat + 1} 号"`（engine.py
       Player.display），所以播报原文全是「3 号」「昨晚 3 号 出局。」
       这种"数字 + 空格 + 汉字"的形状。合成器把空格当**停顿**处理，
       听感就是"三… 号"，和顿号一模一样，用户只能描述成"数字与汉字之间留了间隔"。
       修法：把**夹在两个中文/数字之间**的空白全部删掉（"3 号" → "3号"，
       "昨晚 3 号 出局" → "昨晚3号出局"）。
       ⚠️ 用捕获组 + 前瞻，不用后行断言 `(?<=)` —— 后者在 iOS Safari <16.4
          是**解析期**就抛错，整个文件报废（上一轮已经踩过一次）。
       ⚠️ 只删"两汉字/数字之间"的空白：句首句尾和其它标点旁边的空白留着无害。 */
    text = text.replace(/([\u4e00-\u9fa5\d])\s+(?=[\u4e00-\u9fa5\d])/g, '$1');
    var parts = text.split(/([，。；：！？,.;:!?])/);
    var out = [], cur = '';
    for (var i = 0; i < parts.length; i++) {
      if (i % 2 === 1) {           // 捕获组命中的标点：并回前句尾
        cur += parts[i];
        if (cur.trim()) out.push(cur.trim());
        cur = '';
      } else {
        cur += parts[i];
      }
    }
    if (cur.trim()) out.push(cur.trim());
    return out;
  }

  /* ---------------------------------------------------------- 令牌打断机制 */
  var speakToken = 0;            // 每次新播报 +1，旧播报的分句循环看到令牌变了自动退出
  var pauseTimer = null;
  var _brokenSince = 0;          // 最近一次"看门狗超时"时刻：该环境合成器不出声的证据

  function clearPauseTimer() {
    if (pauseTimer) { clearTimeout(pauseTimer); pauseTimer = null; }
  }
  function pause(ms) {
    return new Promise(function (res) { pauseTimer = setTimeout(res, ms); });
  }

  /* 念一段。resolve(true) = 真的出过声；resolve(false) = 没出声（被拦/合成器不
     回调/出错），调用方据此**立刻**回落云端，不再白等。
     ⚠️ 两个闸缺一不可：
       · 起音闸 UTTER_START_MS：speak() 被静默拒绝时 onstart 永远不来 —— 这是
         "法官全部没声"的真正入口（队列被一句句 5 秒拖死 ⇒ 后续全被判过期跳过）。
       · 总时长闸：有些环境 onstart 来了、onend 永远不来 ⇒ 仍要能收尾。 */
  function utterOnce(text, v) {
    return new Promise(function (resolve) {
      var done = false, started = false;
      var guard = null, startGuard = null;
      function fin(ok) {
        if (done) return;
        done = true;
        try { clearTimeout(guard); } catch (e) { /* ignore */ }
        try { clearTimeout(startGuard); } catch (e) { /* ignore */ }
        if (!ok) _brokenSince = Date.now();
        resolve(ok);
      }
      startGuard = setTimeout(function () {
        if (started) return;
        try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
        fin(false);
      }, UTTER_START_MS);
      /* 总时长看门狗：来了 onstart 但 onend 不回调（无声卡 / 被系统接管）——
         按"这句念完"处理并 cancel，避免门控时钟 SPOKE_DONE.at 永不更新
         （HANDOFF §10.7 明令禁止"播报把面板永久憋死"）。已经出过声，算成功。 */
      guard = setTimeout(function () {
        try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
        fin(started);
      }, utterGuardMs(text));
      try {
        var u = new SpeechSynthesisUtterance(text);
        u.lang = 'zh-CN';
        if (v) u.voice = v;
        u.rate = SPEECH_RATE; u.pitch = SPEECH_PITCH; u.volume = SPEECH_VOLUME;
        u.onstart = function () {
          started = true;
          try { clearTimeout(startGuard); } catch (e) { /* ignore */ }
        };
        u.onend = function () { fin(true); };
        u.onerror = function () { fin(started); };  // 出错也 resolve，绝不卡死播报链
        window.speechSynthesis.speak(u);
      } catch (e) { fin(false); }
    });
  }

  // 单段看门狗时长：按字数放余量（0.98 倍速念中文 ~4 字/秒），4s 下限、20s 上限
  function utterGuardMs(text) {
    return Math.min(20000, Math.max(4000, 1500 + String(text).length * 400));
  }

  function muted() {
    try { return localStorage.getItem('werewolf.muted') === '1'; } catch (e) { return false; }
  }

  /* 播完（或被打断）才 resolve 的版本 —— 播报队列用它喂门控时钟（SPOKE_DONE.at）。
     返回值语义（2026-10-29）：
       true  = 这句是**本地念完**的（调用方不要再走云端，否则会念两遍）
       false = 本地没念出声 / 中途被拦（调用方应立即回落云端，**一个字都不能少**）
     老版本返回 undefined，调用方按 `!== false` 兼容，所以这个改动不会打破旧调用。 */
  function speakAwait(text, interrupt) {
    return new Promise(function (resolve) {
      if (muted() || !text) { resolve(true); return; }   // 静音 = 有意不念，别回落云端
      var synth = window.speechSynthesis;
      if (!synth) { resolve(false); return; }
      speakToken += 1;
      var my = speakToken;
      if (interrupt) { try { synth.cancel(); } catch (e) { /* ignore */ } }
      clearPauseTimer();
      var v = pickVoice();
      var parts = splitForSpeech(text);
      (function run(i) {
        if (my !== speakToken) { resolve(true); return; }   // 被新播报顶掉：不算失败
        if (i >= parts.length) { resolve(true); return; }
        utterOnce(parts[i], v).then(function (ok) {
          if (!ok) { resolve(false); return; }             // 一段没出声 ⇒ 整句交回云端
          if (my !== speakToken) { resolve(true); return; }
          if (i < parts.length - 1) {
            pause(PHRASE_PAUSE_MS).then(function () { run(i + 1); });
          } else { run(i + 1); }
        });
      })(0);
    });
  }

  // 即发即弃版（相位切换等不需要等完的场景可用）
  function speak(text, interrupt) {
    try { speakAwait(text, interrupt); } catch (e) { /* 语音永不影响游戏逻辑 */ }
  }

  function stopSpeaking() {
    try {
      speakToken += 1;
      clearPauseTimer();
      if (window.speechSynthesis) window.speechSynthesis.cancel();
    } catch (e) { /* ignore */ }
  }

  /* ---------------------------------------------------------- 特性检测（降级链的闸门） */
  // speechSynthesis 不存在、音色列表还没异步到达、或没有任何中文音色 ⇒ false。
  // false 时调用方（speech_ui 播报队列）逐句回落云端 TTS —— 国产 Android ROM
  // 经常没有本地中文音色，这条降级链是手机端的保命设计，不能砍。
  function usable() {
    try {
      /* 失败留痕（与 rec.js 同一纪律）：5 分钟内出现过"看门狗超时"⇒ 这个环境
         的合成器不出声，直接走云端，不再逐句白等；TTL 过后允许再试一次
         （万一只是浏览器临时抽风）。 */
      if (_brokenSince && Date.now() - _brokenSince < BROKEN_TTL_MS) return false;
      var s = window.speechSynthesis;
      if (!s || typeof s.getVoices !== 'function') return false;
      var vs = s.getVoices() || [];
      for (var i = 0; i < vs.length; i++) {
        var v = vs[i];
        if (/^zh/i.test(v.lang || '') || /中文|Chinese/i.test(v.name || '')) return true;
      }
      return false;
    } catch (e) { return false; }
  }

  /* 相位切换类台词（天黑/天亮）：允许打断 —— 这两类一来就是新阶段的头，
     等不得。其余法官台词（含死讯）默认排队不打断。 */
  function isPhaseSwitch(text) {
    return /天黑请闭眼|天亮了/.test(String(text || ''));
  }

  /* ---------------------------------------------------------- 被动接线 */
  // ① 音色预加载 + 监听变化：不做这个，第一句就是哑的或难听的（列表异步到达）
  var primed = false;
  function primeVoices() {
    if (primed) return;
    primed = true;
    try {
      var s = window.speechSynthesis;
      if (!s) return;
      pickVoice();
      if (s.addEventListener) {
        s.addEventListener('voiceschanged', function () { pickVoice(); });
      } else if ('onvoiceschanged' in s) {
        s.onvoiceschanged = function () { pickVoice(); };
      }
    } catch (e) { /* ignore */ }
  }

  // 首次用户交互解锁（浏览器要求先有手势才允许发声）。手机放桌面的场景天然满足：
  // 开局/入座必然点过屏幕。三个事件任一命中即全部摘除。
  (function armUnlock() {
    var evs = ['pointerdown', 'touchstart', 'keydown'];
    function hit() {
      primeVoices();
      _brokenSince = 0;              /* 手势到了 ⇒ 给本地通道一次重新证明自己的机会 */
      for (var i = 0; i < evs.length; i++)
        document.removeEventListener(evs[i], hit, true);
    }
    for (var i = 0; i < evs.length; i++)
      document.addEventListener(evs[i], hit, true);
    /* 之后每次点击都把"降级"清掉：手机放桌上时，第一次播报常常发生在用户还没
       碰屏幕之前（AI 局自动开局）—— 那次被拦不该让整局都哑。限流由 _brokenSince
       自身保证：清掉后若仍然不出声，下一句会在 900ms 内重新置位。 */
    document.addEventListener('pointerdown', function () {
      if (_brokenSince) _brokenSince = 0;
    }, true);
  })();

  // ② 离房收声：页面隐藏（切走/刷新/关闭）时立刻闭嘴
  window.addEventListener('pagehide', function () { stopSpeaking(); });

  // ③ 终局收声：PUBLIC.winner 出现（游戏结束）时闭嘴；开新局（winner 清空）后复位。
  //    不 hook tableui 的任何函数 —— 本文件与它零耦合，只读页面全局。
  var _stoppedFor = null;
  window.setInterval(function () {
    try {
      var w = window.PUBLIC && window.PUBLIC.winner;
      if (w && _stoppedFor !== w) { _stoppedFor = w; stopSpeaking(); }
      else if (!w) { _stoppedFor = null; }
    } catch (e) { /* ignore */ }
  }, 1000);

  /* ---------------------------------------------------------- 诊断（给 play.html 的 🩺 面板用）
     为什么单独给一个：法官走的是**浏览器本地合成**这条独立通道，与 AI 的云端通道
     没有任何共享状态。面板只显示「最近播报」(LAST_TTS) 会漏掉这一层 —— 最近一句
     可能正好是 AI 的。手机上"法官没声"时，要能一眼分清是三种里的哪一种：
       ① 没有中文音色（国产 Android ROM 常见）⇒ 本应回落云端
       ② 被静音标记挡住（localStorage.werewolf.muted=1）⇒ 静默且**不回落**（最像"消失"）
       ③ 合成器不回调（无声卡/被系统接管）⇒ 看门狗 TTL 内一律走云端
     只输出 HTML 片段，样式类由 play.html 的 .ok/.bad 提供。 */
  function diagText() {
    try {
      var syn = window.speechSynthesis;
      if (!syn) return '<span class="bad">此浏览器没有 speechSynthesis（法官只能走云端）</span>';
      var vs = syn.getVoices() || [], zh = 0;
      for (var i = 0; i < vs.length; i++)
        if (/^zh/i.test(vs[i].lang || '') || /中文|Chinese/i.test(vs[i].name || '')) zh++;
      var v = pickVoice();
      var bits = [usable() ? '<span class="ok">可用</span>'
                           : '<span class="bad">不可用（已回落云端）</span>',
        '音色 ' + (v ? (v.name + (v.localService ? '（本地）' : '（在线）')) : '（无中文音色）'),
        '音色表 ' + vs.length + ' 个 / 中文 ' + zh + ' 个'];
      if (muted()) bits.push('<span class="bad">被静音标记挡住（werewolf.muted=1）</span>');
      /* 2026-10-29：手机用 http://<IP>:8000 打开时，部分浏览器/ROM 会**静默拒绝**
         speechSynthesis（不报错、不回调）—— 表现就是"法官一句不念"，而云端兜底
         在 MOCK 模式下只有 0.8 秒提示音。这一行让排查不用再猜。 */
      if (typeof window.isSecureContext === 'boolean' && !window.isSecureContext) {
        bits.push('<span class="bad">非安全上下文（http + IP）：换成 https 端口访问</span>');
      }
      if (_brokenSince) {
        var left = BROKEN_TTL_MS - (Date.now() - _brokenSince);
        bits.push(left > 0
          ? ('<span class="bad">合成器不回调，' + Math.ceil(left / 1000) + 's 内全走云端</span>')
          : '看门狗已过 TTL');
      }
      return bits.join(' ｜ ');
    } catch (e) { return '<span class="bad">诊断失败：' + String(e).slice(0, 60) + '</span>'; }
  }

  /* ---------------------------------------------------------- 导出 */
  return {
    diagText: diagText,
    speak: speak,
    speakAwait: speakAwait,
    stopSpeaking: stopSpeaking,
    primeVoices: primeVoices,
    usable: usable,
    isPhaseSwitch: isPhaseSwitch,
    splitForSpeech: splitForSpeech,   // 诊断/测试用
    pickVoice: function () { try { return pickVoice(); } catch (e) { return null; } },
    setMuted: function (on) {         // 静音落 localStorage；静音即收声
      try { localStorage.setItem('werewolf.muted', on ? '1' : '0'); } catch (e) { /* ignore */ }
      if (on) stopSpeaking();
    },
    muted: muted,
  };
})();
