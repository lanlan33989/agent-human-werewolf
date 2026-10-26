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
  var PHRASE_PAUSE_MS = 260;  // 分句间停顿

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
    var parts = String(text).split(/([，。；：！？,.;:!?])/);
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

  function utterOnce(text, v) {
    return new Promise(function (resolve) {
      var done = false;
      function fin() {
        if (done) return;
        done = true;
        try { clearTimeout(guard); } catch (e) { /* ignore */ }
        resolve();
      }
      /* 🔴 看门狗（2026-10-22，走查实测踩坑）：headless Chrome / 某些环境里
         speak() 可能**永远不回调 onend/onerror**（无声卡、合成器不工作）——
         不兜底的话 speakAwait 永不 resolve ⇒ 门控时钟 SPOKE_DONE.at 永不更新
         ⇒ 夜间面板被播报**永久憋死**（HANDOFF §10.7 明令禁止的形态）。
         超时按"这句念完"处理并 cancel；若超时真的发生，还把本会话标记为
         **本地不可用**（见 _brokenSince），后续句子直接走云端 —— 与 rec.js
         的"失败留痕、TTL 内不再优先"同一套纪律。 */
      var guard = setTimeout(function () {
        _brokenSince = Date.now();
        try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
        fin();
      }, utterGuardMs(text));
      try {
        var u = new SpeechSynthesisUtterance(text);
        u.lang = 'zh-CN';
        if (v) u.voice = v;
        u.rate = SPEECH_RATE; u.pitch = SPEECH_PITCH; u.volume = SPEECH_VOLUME;
        u.onend = function () { fin(); };
        u.onerror = function () { fin(); };   // 出错也 resolve，绝不卡死播报链
        window.speechSynthesis.speak(u);
      } catch (e) { fin(); }
    });
  }

  // 单句看门狗时长：按字数放余量（0.98 倍速念中文 ~4 字/秒），5s 下限、20s 上限
  function utterGuardMs(text) {
    return Math.min(20000, Math.max(5000, 2500 + String(text).length * 350));
  }

  function muted() {
    try { return localStorage.getItem('werewolf.muted') === '1'; } catch (e) { return false; }
  }

  // 播完（或被打断）才 resolve 的版本 —— 播报队列用它喂门控时钟（SPOKE_DONE.at）
  function speakAwait(text, interrupt) {
    return new Promise(function (resolve) {
      if (muted() || !text) { resolve(); return; }
      var synth = window.speechSynthesis;
      if (!synth) { resolve(); return; }
      speakToken += 1;
      var my = speakToken;
      if (interrupt) { try { synth.cancel(); } catch (e) { /* ignore */ } }
      clearPauseTimer();
      var v = pickVoice();
      var parts = splitForSpeech(text);
      (function run(i) {
        if (my !== speakToken) { resolve(); return; }   // 每步都查令牌：被新播报顶掉即退
        if (i >= parts.length) { resolve(); return; }
        utterOnce(parts[i], v).then(function () {
          if (my !== speakToken) { resolve(); return; }
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
      if (_brokenSince && Date.now() - _brokenSince < 5 * 60 * 1000) return false;
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
      for (var i = 0; i < evs.length; i++)
        document.removeEventListener(evs[i], hit, true);
    }
    for (var i = 0; i < evs.length; i++)
      document.addEventListener(evs[i], hit, true);
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

  /* ---------------------------------------------------------- 导出 */
  return {
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
