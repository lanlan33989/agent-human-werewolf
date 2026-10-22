/* ============================================================
   大屏 UI 状态机 —— 2026-10-02 从 play.html 原样抽出（未改逻辑）。

   为什么单独一个文件：play.html 超了单文件行数红线（_smoke-map 抓的），
   这一块是最新、边界最清楚的：单个大圆 / 座位小圆 + 行动光标 /
   票型大字 / 大圆发言模式。它们依赖的都是页面的全局状态
   （PUBLIC / SPK_MODE / HANDOFF / CUR_LINE / submit / openHandoff / startSay /
   stopSay / kindOf），同页共享全局作用域 ⇒ 行为不变。

   ⚠️ 加载顺序：本文件在 play.html 的主 <script> **之前**加载 ——
      这里只在运行时读主脚本的全局变量（var 声明与函数定义届时都已就绪），
      顶层的 onclick 绑定发生在点击那一刻，同样安全。
   ============================================================ */

/* ---------------------------------------------------------- 大圆状态 */

var CUR_LINE = null;         /* 播报正在处理的那一条（决定大圆显示谁） */
var SPK_MODE = null;         /* 大圆发言模式：1-based 座位号；null = 关 */

function kindOf(seat){
  var ps = (PUBLIC && PUBLIC.players) || [];
  for (var i = 0; i < ps.length; i++) if (ps[i].seat === seat) return ps[i].kind;
  return null;
}

/* 大圆显示谁（2026-10-02 用户定：单个大圆，谁在说话显示谁） */
function paintBig(){
  var who = $('bigWho'), icon = $('bigIcon'), bc = $('bigCircle');
  if (!who || !icon || !bc) return;
  if (SPK_MODE){
    who.textContent = SPK_MODE + '（人）';   /* 2026-10-07 用户定：发言屏只留号码 */
    icon.textContent = '🎤';
    return;
  }
  var l = CUR_LINE;
  if (l && l.t === 'say' && l.seat !== null && l.seat !== undefined){
    who.textContent = (l.seat + 1) + '号玩家（' + (kindOf(l.seat) === 'human' ? '人类' : 'AI') + '）';
    icon.textContent = '💬';
    moveDotCursor();
    return;
  }
  who.textContent = '';                       /* 法官说话时大圆就是法官本尊 */
  icon.textContent = '⚖️';
  moveDotCursor();
}

/* ---------------------------------------------------------- 座位小圆（2026-10-02 用户追加） */

var DOTS_SIG = null;         /* 上次建小圆时的"阵容签名"（人数/阵营/存活变了才重建） */

/* 此刻轮到谁行动（1-based）：人类发言回合 / 人类操作中 / 正在说话的座位 */
function activeSeatNo(){
  if (SPK_MODE) return SPK_MODE;
  if (HANDOFF) return HANDOFF;
  var l = CUR_LINE;
  if (l && l.t === 'say' && l.seat !== null && l.seat !== undefined) return l.seat + 1;
  return null;
}

function renderDots(){
  if (!PUBLIC || !PUBLIC.players) return;
  var wrap = $('seatDots');
  if (!wrap) return;
  /* 签名含存活态：出局了就重建（灰掉），活过来的不可能但一并覆盖 */
  var sig = PUBLIC.players.map(function(p){
    return p.seat + ':' + p.kind + (p.alive ? '' : ':dead');
  }).join(',');
  if (sig !== DOTS_SIG){
    DOTS_SIG = sig;
    var h = '';
    PUBLIC.players.forEach(function(p){
      h += '<div class="seatdot ' + (p.kind === 'ai' ? 'ai' : 'human')
         + (p.alive ? '' : ' dead') + '" data-no="' + (p.seat + 1) + '">'
         + (p.seat + 1) + '</div>';
    });
    h += '<div id="dotCursor"></div>';
    wrap.innerHTML = h;
  }
  moveDotCursor();
}

/* 光标滑到当前行动者的小圆上（transition 让"轮转"看得见）；没人行动就藏起来 */
function moveDotCursor(){
  var wrap = $('seatDots');
  if (!wrap) return;
  var cur = document.getElementById('dotCursor');
  if (!cur) return;
  var no = activeSeatNo();
  var target = null;
  var dots = wrap.querySelectorAll('.seatdot');
  for (var i = 0; i < dots.length; i++){
    var on = (dots[i].getAttribute('data-no') == no);   /* == 宽比较：data-* 是字符串 */
    dots[i].classList.toggle('active', on);
    if (on) target = dots[i];
  }
  if (target){
    var wr = wrap.getBoundingClientRect();
    var r = target.getBoundingClientRect();
    cur.style.display = 'block';
    cur.style.left = (r.left - wr.left + r.width / 2 - 9) + 'px';
    cur.style.top = (r.top - wr.top - 13) + 'px';
  } else {
    cur.style.display = 'none';
  }
}

/* 发言回合不走交接卡片（用户定：取消半透明黑屏）：大圆变成「N号玩家（人类）」，
   点一下开始录音、再点一下结束。只在这个回合**只有 speak 一个动作**时启用 ——
   还要投票/用药的回合仍然走卡片（那些需要按钮）。 */
function isSpeakOnly(need){
  var a = (need && need.actions) || [];
  return a.length === 1 && a[0] === 'speak';
}

function enterSpeakerMode(need){
  SPK_MODE = need.seat + 1;
  /* 🔴 交接状态必须清干净（2026-10-02 走查截图实锤）：上一回合遗留的
     HANDOFF/HANDOFF_SIG 不清的话，「大圆模式」与「交接卡片」会同时活着 ——
     截图里光标在 3 号（大圆）而卡片是 7 号（交接），两边互相覆盖，
     walker/玩家点「我拿到了」永远到不了操作面。 */
  HANDOFF = null; HANDOFF_SIG = null; MYVIEW = null; PICK = null;
  WHEEL = { mode: null, seatNo: null, view: null, act: null,
            pick: null, sub: null, angle: WHEEL.angle };
  $('handoff').classList.remove('on', 'speak');
  $('wheelBtns').classList.remove('on');
  $('wheelExtra').classList.remove('on');
  $('bigCircle').classList.add('pickable');
  /* 「打字 / 过」两个按钮已去掉（2026-10-07 用户定：发言屏只有号码 + 开麦/关麦）。
     识别失败时：再点一下关麦即可（空文本服务端会按「过」记）。 */
  paintBig();
  /* 🔴 2026-10-12 用户定稿：发言大圆**去装饰** —— 图标/标题藏掉，圆上只有状态词 */
  var _bi = $('bigIcon'), _bt = $('bigTitle');
  if (_bi) _bi.style.display = 'none';
  if (_bt) _bt.style.display = 'none';
  var _bw0 = $('bigWho');
  if (_bw0) _bw0.textContent = '点击开始发言';
  syncSpkTip();
  foot('');      /* 同上：发言屏不留提示文案（用户定 2026-10-07） */
}

function closeSpeakerMode(){
  if (!SPK_MODE) return;
  SPK_MODE = null;
  var bc = $('bigCircle');
  if (bc) bc.classList.remove('pickable', 'rec');
  var _bi2 = $('bigIcon'), _bt2 = $('bigTitle');
  if (_bi2) _bi2.style.display = '';
  if (_bt2) _bt2.style.display = '';
  /* 🔴 2026-10-22（用户实机·第二晚全崩的根因）：play.html 里**没有 #sayCtl
     元素**（只剩 CSS 残留），这句裸调在 SPK_MODE 非空时抛 TypeError ——
     抛点在 tick 的面板闸之前，被外层 catch 吞掉后**该拍 455-490 的面板逻辑
     整段跳过**：轮询活着、面板永不弹、页面冻在上一个屏（用户看到的就是
     "卡在投票公示 + 所有面板没弹 + 播报自顾自快速放完"）。
     第一晚不炸是因为用户还没开过麦（SPK_MODE=null 时函数第一行就 return）。 */
  var _sc = $('sayCtl');
  if (_sc) _sc.classList.remove('on');
  var tip = $('spkTip');
  if (tip) tip.style.display = 'none';
  /* 🔴 回合切换**必须清场**（2026-10-02 实局）：内置识别会静音自续（rec.js 的
     onend 重启，最多 20 次）—— 上一回合开的麦若不关，会一直听着：下一个人的
     话、甚至 TTS 外放全被它收进 finalText，最后被当成**另一个座位**的发言提交
     （用户实锤：3 号开麦说的话跑到了 1 号头上，转写里还混着「4号请发言」的回声）。
     所以回合一换就断掉一切残留会话。 */
  if (SAY_REC || SAY_PENDING || SAY_STREAM){
    if (SAY_STREAM){ try { SAY_STREAM.stop(); } catch (e) { /* ignore */ } }
    SAY_STREAM = null; SAY_PENDING = null; SAY_REC = false;
    var _hb = $('hoSay'); if (_hb) _hb.classList.remove('on');
  }
  paintBig();
}

/* 大圆发言模式的提示行：录音状态在这里**必须看得见**（用户 2026-10-02 报：
   「语音开启和语音关闭的区别不大」）。● 红 = 正在录音；大圆图标同步换 🔴。 */
function syncSpkTip(){
  var tip = $('spkTip');
  if (!tip) return;
  if (!SPK_MODE){ tip.style.display = 'none'; return; }
  var st = ($('hoStatus') || {}).textContent || '';
  var ok = st.indexOf('说了：') === 0;
  /* 🔴 2026-10-07 用户定（发言屏叠字）：**中间不要任何提示文案** ——
     大圆上只留「N（人）」，交互就是"点一下开麦、再点一下关麦"。
     `st` 是识别状态/文本，有就显示（那是内容，不是提示）。 */
  /* 🔴 2026-10-12（用户：转写文字不该冻结在界面上）：提示行只留状态圆点，
     识别文本只进提交、不上屏。 */
  tip.textContent = SAY_REC ? '● 发言中' : '';
  tip.classList.toggle('rec', SAY_REC);
  tip.style.display = 'block';
  /* 🔴 2026-10-12 用户定稿：发言大圆上**只有状态词**（点击开始发言 / 发言中
     （有声音/无声音）/ 点击结束发言），识别出的文字不再上圆。 */
  var bw = $('bigWho');
  if (bw){
    bw.textContent = SAY_REC
      ? ('发言中' + (ok ? '（有声音）' : (st ? '（无声音）' : '')))
      : '点击结束发言';
  }
  var ic = $('bigIcon');
  if (ic) ic.textContent = '';
}

/* ------------------------------------------------- 播报队列 + 夜间门控
   ⚠️ 2026-10-07 已拆到 `speech_ui.js`（speakQueue / voicedUp / linePlayed /
   nightReady / nightStageIdx / nightNodeIdx / playBlob …）。**改动请去那边**，
   本文件只剩：大圆 / 小圆 + 轮盘 / 交接屏 + 发言录音。 */

/* ---------------------------------------------------------- 主循环 */

/* ---------------------------------------------------------- 主循环 */

async function tick(){
  if (!GID) return;
  /* 2026-10-01：**不在牌桌页就别再轮询**。
     有了"回到初始界面"（封面 / 再来一局）之后，不加这一道的话，
     页面会在封面上继续偷偷轮询（白耗电，还可能在后台推进界面状态）。
     直接 return 即停：tick 是自调度的（末尾 setTimeout(tick, …)），
     而进桌面时 `enterTable()` 会重新调一次 tick() —— "停"和"重启"都是自愈的。 */
  if (!$('p4') || !$('p4').classList.contains('on')) return;
  try {
    PUBLIC = await api('/api/game/' + GID + '/public');
  } catch (e) {
    /* ★ 这里以前是 `catch (e) { return; }` —— **整个轮询循环就此静默停住**，
       界面永久冻在最后一屏、一句提示都没有。真机上就是这么卡住的：
       服务端 SESSIONS 满了会淘汰旧局，被淘汰的那个页面一拿 404 就再也不更新
       （日志里实测出现过 /api/game/{gid}/public 404，而且连着好几次）。
       现在改成：说清楚发生了什么，并给一条出路。 */
    if (String(e.message).indexOf('404') === 0){
      foot('这一局在服务端已经不在了（服务重启过，或同时开的局太多被挤掉）');
      lostGame();
    } else {
      foot('连不上服务端：' + e.message);
      setTimeout(tick, 1500);          /* 只是抖动：继续试，别僵死 */
    }
    return;
  }
  paintSeats();
  renderDots();          /* 座位小圆：阵容/存活变化才重建，光标每拍都校一次 */
  if (SPK_MODE) syncSpkTip();   /* 收尾中/失败文案有更新时，提示行跟着走（tick 兜底） */
  paintCountdown();      /* 夜间操作倒计时（服务端裁决，这里只显示） */
  /* 🔴 2026-10-12（用户：AI 说话时麦不该是可点状态）：圆盘的可点态只在**轮到人类
     发言**（SPK_MODE 有值）时出现，其余时刻一律摘掉 —— 视觉与拦截一致。 */
  var _bcSpk = $('bigCircle');
  if (_bcSpk && !SPK_MODE) _bcSpk.classList.remove('pickable');
  renderLog();           /* 顶部标题栏已删（2026-10-05）：板子构成等公有信息由 renderLog 画进日志区 */
  speakQueue();
  if (PUBLIC.phase === 'ended'){
    foot('本局结束：' + (PUBLIC.winner === 'wolf' ? '狼人胜利' : '好人胜利'));
    /* 局末才亮出「本局战报」：战报会解密所有人身份，
       接口层对局中访问返回 409，所以这两个出口恰好出现在正确的位置。 */
    var _el = $('endLinks');
    if (_el){
      _el.style.display = 'flex';
      if (GID) $('lnkReport').href = '/api/game/' + GID + '/report.md';
    }
    return;
  }
  try {
    /* ★ 每一轮都要问「现在该谁操作」，**不管手上有没有开着一屏**。
       以前这里是 `if (!HANDOFF) { 问一次 }` —— 于是只要 HANDOFF 卡住
       （那一步没提交成功、或者重复提交被闸门挡掉），页面就**永远不再问**，
       界面僵在那一屏不动，而服务端其实早就等着下一步了。
       日志里就是这么表现的：**只有 /public 轮询，一次 /pending_seats 都没有**
       （用户报的"投票又卡住了"）。
       现在改成：手上这屏跟服务端说的对不上就换掉 —— 页面能自愈。 */
    var ps = await api('/api/game/' + GID + '/pending_seats');
    /* 🔴 **「现在该谁做什么」只听服务端**（`PUBLIC.now`）。前端不再有"要不要显示"的判断；
       下面那段旧猜法只在老服务端时走到。（事故全本见 §10.7） */
    var NOW = PUBLIC.now || null;
    var top = null, _readyTop = [];
    if (NOW){
      top = NOW.need || null;
    } else {
      var _curNode = PUBLIC.night_node || null;
      _readyTop = _curNode
        ? (ps.seats || []).filter(function(x){ return x.node === _curNode; })
        : [];
      top = _readyTop.length ? _readyTop[0]
                : ((ps.seats && ps.seats.length) ? ps.seats[0] : null);
    }
    /* 🔴 2026-10-07 第四起·续（用户实机："1 号人类玩家发言的麦可点按钮，
       在人类预言家节点被触发出现"）：
       **夜间面板**一切以服务端为准（不挂音频 —— 面板出不来才是致命故障）；
       但**发言/投票**这类"要人开口/表态"的界面必须等**自己的播报追上**：
       服务端进入 DISCUSS 的那一刻，本机音频队列里可能还压着"预言家请闭眼"没念完
       ⇒ 麦克风按钮抢在夜间播报结束前冒出来（用户听到的就是"夜里还没结束就能开麦"）。
       `voicedUp()` 就是那条"整队播报门"（语音关掉时它恒真 ⇒ 不会憋死）。
       `now.kind === 'none'` 也放行：那一拍没有面板，需要它去把旧面板收掉。 */
    /* 三档口径（都在播报之后）：wheel/shoot = 那句念完（linePlayed）；speak/vote = voicedUp；none = 放行。 */
    var _gateOk = NOW
      ? (NOW.kind === 'none'
         || ((NOW.kind === 'wheel' || NOW.kind === 'shoot')
             /* ⚠️ 这里**只许用 `linePlayed`**，不许再 `|| NOW.started`（2026-10-07 第七起：
                用户实机"预言家面板弹出过早，居然在女巫闭眼播报时弹出来了"）。
                原因：服务端的 arm 失联兜底会在**半个窗口**时把 `started` 置真，
                而"女巫请闭眼"那句还要念几秒才轮到"预言家请睁眼" ⇒ 面板抢跑。
                "面板不许被播报永久否决"这条已经由 `linePlayed` **自己**兜住了
                （它内部有"最近一次念完已过 8 秒 ⇒ 音频坏了，放行"），
                而"人类拿得到整窗"由服务端 `_nn_deadline` 保证（不再依赖前端）。 */
             ? linePlayed(NOW.node) : voicedUp()))
      : nightReady(top);
    /* 记下"这个节点的窗口第一次被看到"的时刻（上面 arm 兜底用） */
    if (!NOW || NOW.node !== _nodeSeenNode){
      _nodeSeenNode = NOW ? NOW.node : null;
      _nodeSeenAt = Date.now();
    }
    /* 倒计时（2026-10-07 第三期）：优先用 `now.until`（**窗口结束的墙上时刻**），
       本地只做减法 —— 不再"剩余秒数 + 收到它的时刻"这种近似（轮询间隔会漂）。
       老服务端没有 `until` 时退回 `seconds_left`。 */
    /* 🔴 2026-10-22：优先用服务端的**剩余秒数**（until_left）本地起算 ——
       until 是服务器墙钟，设备时钟有偏差时（手机 vs 服务端）倒计时一出来
       就像"已经走了好久"。until_left 与面板 arm 同拍下发 ⇒ 面板与倒计时
       天然同时出、且从满窗起走（用户定稿口径）。老服务端没有该字段时回退 until。 */
    if (NOW && typeof NOW.until_left === 'number'){
      NIGHT_CD = { left: Math.max(0, NOW.until_left), at: Date.now() };
    } else if (NOW && typeof NOW.until === 'number' && NOW.until > 0){
      NIGHT_CD = { left: Math.max(0, NOW.until - Date.now() / 1000),
                   at: Date.now() };
    } else if (top && typeof top.seconds_left === 'number'){
      NIGHT_CD = { left: top.seconds_left, at: Date.now() };
    } else {
      NIGHT_CD = null;
    }
    /* arm（2026-10-06 晚用户定：**播报最后一个字后 2 秒**才开始倒计时）——
       该座位的「请睁眼」句真正念完（SPOKE_DONE 覆盖它）且满 2s，
       才通知服务端起算（幂等）。语音关闭时 nightReady 直接过 ⇒ 立即 arm。 */
    /* 🔴 2026-10-12 bug2 闭环（用户实机：加了 8s 兜底后毒药窗口仍然整窗无 arm）：
       兜底原来写在 `done` 里，但本块入口还挂着 `_gateOk` —— 而夜间 `_gateOk`
       = `linePlayed(本段那句)`（音频锚定，卡死兜底 20s > 15s 窗口）⇒ 音频一卡，
       **整块进不去**，兜底永远执行不到。这正是"arm 是通知、不是裁决"的反面教材：
       **arm 自己的 done 检查就是音频锚点，不需要再过音频门**。去掉 `_gateOk`，
       时序不变（done 仍=「本段那句念完」），8s 兜底从此可达。 */
    if (top && (PUBLIC.phase === 'night' || (top.actions || []).indexOf('shoot') >= 0)
        && !(WHEEL && WHEEL.batch)
        && ARMED_SIG !== _armKey(top)){
      /* node = **当前这个窗口**的节点。不要用 `top.node`：女巫的待办 `node` 一直是
         `witch_heal`，而她第二问（毒药）是另一个节点 ⇒ 用待办那个 node 会拿解药那句的
         下标判"念完了没有"，毒药窗口一开就立刻 arm（= 又一次"倒计时过早"）。
         🔴 2026-10-12（用户实机："夜间所有操作面板都没出现"）：第十期改 `panel_line`
         时把这条 `var _armNode` 声明吞了，而下面两处还在引用它 ⇒ arm 块一跑就抛
         ReferenceError ⇒ **整个 tick 掐死 ⇒ 夜间面板永远不渲染**（播报队列是另一条
         协程所以照常响 —— 症状才长得像"只有面板没了"）。已恢复声明。 */
      var _armNode = (NOW && NOW.node) ? NOW.node : top.node;
      /* 🔴 2026-10-07 第十期：arm 的"念完了没有"同样听服务端下发的
         `NOW.panel_line`（本段那句的下标），不再自己算下标。 */
      var jx = (NOW && typeof NOW.panel_line === 'number' && NOW.panel_line >= 0)
        ? NOW.panel_line
        : (_armNode ? nightNodeIdx(_armNode) : nightStageIdx(top));
      /* arm = 告诉服务端"倒计时可以从现在起算"。
         🔴 2026-10-07 第四起（用户："倒计时节点开始过早，没按我一开始约定的来"）：
         定稿口径 = **15/20 秒从"法官念完该角色那句 + 2 秒"起算**
         （2026-10-06 用户定，见 `game_api.arm_window` 的说明）。
         上一轮我为了修"倒计时恒为 None"把这里改成**立刻 arm** ⇒ 法官还在念
         「狼人请睁眼…」，秒数就已经开跑 ⇒ 用户看到的"过早"。
         现在恢复成"那句念完 + 2 秒"，**并补一条兜底**：6 秒还没念完（TTS 坏了 /
         队列卡住 / 语音关掉）也照样 arm —— 面板已经不依赖音频了，所以
         "窗口被憋死"这个致命故障不会回来，最多是秒数晚几秒开始。 */
      /* 用户定："应该播报刚结束" ⇒ 那句一念完就 arm（不再等 2 秒）。
         🔴 2026-10-12 bug2 最后一张网（用户实机 + Network 截图：毒药窗口 15 秒
         全程没有 arm 请求，而 wolf/witch_heal/seer 都正常）：音频锚定的 arm 在
         真机偶发**整窗不触发**，而 linePlayed 的音频卡死兜底是 20 秒 —— 比 15s
         窗口还长 ⇒ 永远轮不到 ⇒（bug1 的闸）面板整窗不出。
         仿真（_sim_poison_arm.py）证明纯逻辑没问题 ⇒ 是真机音频链路的时序问题，
         不再纠缠：**节点被本页看到 ≥8 秒还没 arm 就直接 arm**（用当前节点）。
         服务端本来就有"没 arm 推迟起算"的整窗保护（_nn_deadline），不会偷窗口；
         音频正常时 arm 在 2~5 秒就发生了，兜底根本轮不到 —— 只救卡死的场。 */
      /* 🔴 2026-10-12 v2（用户四人局实机：狼人播报慢，"狼人请闭眼"刚出口女巫面板
         就弹出，没有闭眼→空挡→睁眼的节奏）：v1 的"队列停摆 8s"探测分不清
         **"队列卡死"和"剧本本来就安静"** —— 狼窗尾部本来就有十几秒无新句，
         SPOKE_DONE 冻结 ⇒ 女巫节点一开、兜底立即成立 ⇒ 面板抢在她自己的睁眼句
         之前弹出（再叠加队列积压，听感就是"闭眼刚出面板就出"）。
         v2 直接锚**本段那句的应播时刻**：时间线里本句的 `at+delay` 已过 8 秒
         还没"念完"（SPOKE_DONE 没越过它）才算卡死。句子刚出现 ⇒ 永不触发；
         真卡死 ⇒ 8 秒后兜底 arm（服务端整窗保护仍在）。 */
      var _tlC = (PUBLIC && PUBLIC.timeline) || [];
      var _dueLn = (NOW && typeof NOW.panel_line === 'number' && NOW.panel_line >= 0)
        ? (_tlC[NOW.panel_line] || null) : null;
      var _armFallback = !!(_dueLn && _dueLn.at
                            && (Date.now() / 1000 - (_dueLn.at + (_dueLn.delay || 0))) > 8);
      var done = _armFallback
                 || (NOW ? linePlayed(_armNode)
                     : ((jx < 0) || !VOICE_ON || PAUSED
                        || (SPOKE_DONE && SPOKE_DONE.idx >= jx && SPOKE_DONE.at > 0)));
      if (done){
        ARMED_SIG = _armKey(top);
        /* 带上 node：服务端只认**当前节点**的 arm（女巫两问是同一个人的两个节点，
           上一屏的 arm 晚到就会把新窗口算歪）。
           🔴 2026-10-12 bug2：必须发 `_armNode`（= now.node，当前窗口），**不能发
           `top.node`** —— 女巫没答解药时她的待办 node 一直是 witch_heal，
           拿它 POST 毒药窗口会被服务端"节点已变"拒绝 ⇒ started 永远 false
           ⇒（bug1 的闸）毒药面板永远不开。 */
        api('/api/game/' + GID + '/arm'
            + (_armNode ? ('?node=' + encodeURIComponent(_armNode)) : ''),
            { method: 'POST' })
          .catch(function(){ ARMED_SIG = null; });
      }
    }
    /* 🔴 2026-10-05 实局两连修（用户：入夜还没狼人睁眼，女巫面板就弹出来了）：
       根因 = 状态机推进与播报推进是两套路径 —— 服务端一夜的播报一次性全进
       timeline，AI 狼秒刀后同一拍 pending 就到了 human_night；而今天上午加的
       "夜间动作白名单"又让夜间操作**绕过**播报门，交棒屏遂抢在 TTS 念到
       「女巫请睁眼」之前弹出。
       现在的门控**按角色对齐播报**（nightReady）：
         · 夜间动作放行的条件 = 时间线里那一句「XX请睁眼」**已经被播到**（SPOKEN 越过它）
         · 非夜间动作（发言/投票/开枪）仍走 voicedUp 整队门（防"开始投票"抢在发言前）
       这样「状态机推进 == 播报推进」：法官念到谁，谁的面板才出现。 */
    /* 🔴 2026-10-12 bug3（用户实机：验了 3 号，图标右上角的"好"一直留到白天）：
       预言家的私有信息有两处 —— 大圆的好/坏（showSeerBig，3 秒自清 ✓）和
       **结果屏**（act='done'，等人点圆心交回才撤）—— 结果屏一等就等过了
       「预言家请闭眼」甚至到了白天。现在：「预言家请闭眼」播报一旦念完
       （或天亮离夜）⇒ 强制清 seerBig + 撤结果屏。wheelTags 的验人标记只画在
       轮盘面板里，面板一撤随之消失。 */
    try {
      var _scIdx = nightNodeIdx('seer_close');
      if ((_scIdx >= 0 && SPOKE_DONE && SPOKE_DONE.idx >= _scIdx && SPOKE_DONE.at > 0)
          || PUBLIC.phase !== 'night'){
        if (typeof resetSeerBig === 'function') resetSeerBig();
        if (WHEEL && WHEEL.mode === 'act' && WHEEL.act === 'done') closeHandoff();
      }
    } catch (e) { /* 不拖累 tick */ }
    if (!_gateOk){
      foot('还有话没念完…');
    } else if (NOW && !top){
      /* 服务端说"这一拍没有人类要点"（AI 在算 / 窗口在走 / 收尾播报）——
         桌上就不该有面板/发言屏。把残余的收掉，免得窗口关了还能按（会 400）。 */
      closeSpeakerMode();
      if (typeof closeHandoff === 'function') closeHandoff();
    } else if (!HANDOFF){
      if (top && PUBLIC.phase === 'discuss' && isSpeakOnly(top)){
        /* 2026-10-02 用户定：发言回合**不走交接卡片**（取消半透明黑屏）——
           大圆显示「N号玩家（人类）」，点一下开始、再点一下结束。 */
        if (SPK_MODE !== top.seat + 1){ closeSpeakerMode(); enterSpeakerMode(top); }
      } else {
        closeSpeakerMode();
        /* 狼刀共屏（2026-10-06 晚用户定）：夜里有多个人类狼都有 kill 待办时，
           它们**共用一个屏幕一个倒计时** —— 待办列表整批传下去。 */
        var pack = (top && top.actions && top.actions.indexOf('kill') >= 0)
          ? (ps.seats || []).filter(function(x){ return x.actions.indexOf('kill') >= 0; })
          : null;
        if (top){
          openHandoff(top,
            pack ? pack.map(function(x){ return x.seat + 1; }) : null);
          /* 面板被上面的 arm 闸挡住时给一句人话，别让人以为卡了（bug1 修复配套） */
          if (PUBLIC.phase === 'night' && PUBLIC.now
              && PUBLIC.now.kind === 'wheel' && PUBLIC.now.started === false){
            foot('法官还在念播报，操作面板马上就来…');
          }
        }
        /* 服务端还在替 AI 干活（大人数局一次跑不完一个回合）——
           给一句明确的提示，别让人以为卡住了。轮询本身就是推进器。 */
        /* 🔴 2026-10-07 第三期：夜间"按窗口走满"时也是 working —— 那不是 AI 在想，
           是固定节拍在走（定稿：读秒不可跳过）。分开说，免得用户以为卡了。 */
        else if (PUBLIC.pending === 'working'){
          if (PUBLIC.phase === 'night' && PUBLIC.night_node){
            /* 🔴 2026-10-08 表化②（用户定的「alarm 架构」）：进度从**服务端下发的
               整张夜表**（PUBLIC.night_plan）算 —— 第几段/共几段，客户端不再自己
               维护节点清单。 */
            var _plan = PUBLIC.night_plan || [], _pi = -1;
            for (var _k = 0; _k < _plan.length; _k++){
              if (_plan[_k].id === PUBLIC.night_node){ _pi = _k; break; }
            }
            var _prog = _pi >= 0 ? '（' + (_pi + 1) + '/' + _plan.length + '）' : '';
            foot('夜间流程进行中：' + (NIGHT_NAME[PUBLIC.night_node] || PUBLIC.night_node)
                 + _prog + '（读秒不可跳过）');
          } else {
            foot('AI 正在想…（人多的时候要等一会儿）');
          }
        }
      }
    } else {
      /* ★ 判据：始终拿着 ps.seats[0]（而不是"当前开着的那一位"）去比对，
         加上"选人 → 确认"两步手势之后就炸了 —— 见下面的注释。 */
      var mine = (ps.seats || []).filter(function(s){ return (s.seat + 1) === HANDOFF; })[0];
      var sameMine = mine && sigOf(mine) === HANDOFF_SIG;
      /* 🔴 2026-10-05 走查挂的那条：夜里常常是**好几个人同时 pending**
         （谁先递到手机谁先操作），服务端返回的顺序会变；原来拿 ps.seats[0]
         的签名跟手上的比 ⇒ 每拍都判"过期" ⇒ 整屏重建，用户刚点中的号码被冲掉。
         表现就是"点数字 → 再点圆心"永远提交不出去，无限 take→选中→take。
         所以：① 对着**本人那一条**比（座位 + 动作都没变就保持原屏）；
               ② 已经选中还没确认（WHEEL.pick != null）时**不重建**，
                  把这一步完整地交到用户手上。 */
      /* ⚠️ 另外那条老规矩照旧：**正按着麦克风时不许重建这一屏**：
         重建 = 按钮元素被销毁，按住的那条指针随即被取消 ⇒ 用户看到
         "按下去秒自己松开"（2026-09-19 手机报障）。等这一句说完，
         下一轮 tick 自然会换（tick 一直在跑）。 */
      var selecting = WHEEL.mode === 'act' && (WHEEL.pick != null
  /* 狼队共屏也适用（2026-10-07）：选中即锁 ⇒ 重渲染会把 `.sel` 与 picks 冲掉，
     等于"选中后又被撤了"。 */
  || (WHEEL.picks && WHEEL.picks.length));
      /* 2026-10-06 走查抓到：验人结果屏（act='done'）也会被这里撤掉 ——
         结果屏只活 0.2~1.2 秒，玩家（和走查）根本看不完。
         结果屏必须等人点圆心交回才撤，不参与过期重建。 */
      var showingResult = WHEEL.mode === 'act' && WHEEL.act === 'done';
      /* 🔴 2026-10-12 bug2 续：**节点变了也要重建** —— 女巫没答解药时她的待办
         在解药/毒药两个窗口之间**完全不变**（acts 同、node 字段同为 witch_heal），
         `sameMine` 永远真 ⇒ 面板停在解药屏不切毒药。现在节点一换就走重建：
         重建会重新过 openHandoff 的 arm 闸 —— 毒药窗口没 arm 就关着，
         arm（毒药播报念完）后以毒药屏重新弹出。 */
      var _nodeMoved = WHEEL && WHEEL.mode === 'act' && WHEEL.forNode
                       && PUBLIC.now && PUBLIC.now.node
                       && WHEEL.forNode !== PUBLIC.now.node;
      /* 🔴 2026-10-22（用户实机："女巫毒药面板没弹"）：selecting 挡重建只该挡
         **本窗口内**的重建 —— 节点都换了（解药→毒药），旧窗口里未确认的选择
         留着毫无意义（还可能被带进毒药窗口误当毒药目标），必须放行重建。
         否则解药屏选中过号码的用户，毒药窗口整窗停在解药屏上。 */
      if ((!sameMine || _nodeMoved) && !(SAY_REC || SAY_PENDING)
          && !(mine && selecting && !_nodeMoved)
          && !showingResult && !(WHEEL && WHEEL.batch)){
        closeHandoff();                     /* 手上这屏过期了，先撤掉 */
        if (top) openHandoff(top, null);
      }
    }
  } catch (e) { /* 忽略，下一轮再问 */ }
  /* 狼夜实时盘子（2026-10-01）：面板开着也持续刷队友的点/锁定。
     2026-10-05 起画到**轮盘刻度**上（paintWheelKnives）——
     只改刻度上的标记元素，不重建号码按钮（重建会打断正在进行的点选）。 */
  if (PUBLIC.phase === 'night' && HANDOFF && MYVIEW && MYVIEW.wolf_points){
    try {
      MYVIEW = await api('/api/game/' + GID + '/seat/' + (HANDOFF - 1));
      paintWheelKnives(MYVIEW);
      paintDialKnives(MYVIEW);   /* 拨号盘：AI 狼点了谁，实时标出来（2026-10-12） */
    } catch (e) { /* 抖一下没关系，下一轮再来 */ }
  }
  /* 轮询节奏：AI 在干活 → 200ms；**等人类操作 → 250ms**（2026-10-04：公告播完
     后"麦可点"的状态要尽快确认，别让人等一拍 1.2s 的慢轮询）；夜晚 700ms；其余 1200ms。 */
  setTimeout(tick, (PUBLIC && PUBLIC.pending === 'working') ? 200
            : (PUBLIC && PUBLIC.pending === 'human_speak') ? 250
            : (PUBLIC && PUBLIC.phase === 'night') ? 700 : 1200);
}

/* 这一局在服务端没了：别静默冻住，明确告诉玩家并给一条出路 */
function lostGame(){
  /* 这一局在服务端已经没了（404）：锚点先清掉，否则封面会挂一个"接不回"的按钮。
     （2026-10-01：接回按钮是新增的出口，这条路径必须跟着一起收拾干净。） */
  localStorage.removeItem('tt.gid');
  showResumeButton('');
  HANDOFF = null; HANDOFF_SIG = null; MYVIEW = null; PICK = null;
  WHEEL = { mode: 'act', seatNo: null, view: null, act: 'lost',
            pick: null, sub: null, angle: WHEEL.angle };
  $('handoff').classList.remove('speak');
  $('hoSpeak').classList.remove('on');
  $('wheelBtns').classList.remove('on');
  $('hoActs').innerHTML = '';
  var bc = $('bigCircle');
  if (bc) bc.classList.add('acting');
  $('bigTitle').textContent = '—';
  $('bigIcon').textContent = '💀';
  $('bigWho').textContent = '这一局没了';
  $('hoTip').textContent = '';
  $('wheelExtra').innerHTML = '<button id="hoLost">重新开一局</button>';
  $('wheelExtra').classList.add('on');
  var b = $('hoLost');
  if (b) b.onclick = function(){ GID = null; closeHandoff(); show('p0'); };
}

/* ---------------------------------------------------------- 交接层 */

/* 一屏的「签名」= 座位 + 此刻能做的动作。
   ⚠️ 只比座位号是不够的：同一个人从「投票」变成「用解药」时座位没变，
   而屏幕上还是旧按钮 ⇒ 玩家一点就拿到 400「当前阶段是 night，不允许该操作」。 */
function sigOf(need){
  if (!need) return null;
  return (need.seat + 1) + ':' + ((need.actions || []).slice().sort().join(',')
         + '@' + ((PUBLIC.now && PUBLIC.now.node) || '')   /* 带节点：女巫两问靠它切屏 */);
}

/* ================================================================
   轮盘状态机（2026-10-05 用户定）
   大圆 = 唯一按钮区域：可拖动旋转（线下围坐，各自把号码转到面前）；
   夜间私有信息直接标在号码刻度上、随盘转；点数字选中、点圆心确认。
   mode: null = 无人操作（法官）；'handoff' = 等本人点圆心认领；
         'act' = 本人操作中（选人/确认）。
   ================================================================ */
var WHEEL = { mode: null, seatNo: null, view: null, act: null,
              pick: null, sub: null, angle: 0 };
var WHEEL_DRAGGED = false;      /* 刚拖完盘 ⇒ 紧跟的 click 是拖动收尾，不是确认 */

/* 夜间操作倒计时（2026-10-06 用户定）：服务端裁决并下发 seconds_left，
   这里**只渲染**（每拍按本地时钟衰减），不参与任何判定。
   at = 拿到该数字的时刻（ms），用于本地插值。 */
var NIGHT_CD = null;
var SPOKE_DONE = null;   /* 最近一条**念完**的播报 {idx, at}（arm 倒计时用） */
var ARMED_SIG = null;      /* 已 arm 过的（座位+节点+轮次）防重复 POST 的键 */
/* 当前节点第一次被本页面看到的时刻（`linePlayed` 的音频卡死兜底用） */
var _nodeSeenNode = null, _nodeSeenAt = 0;
/* arm 幂等键 = 座位 + 节点 + 轮次（只记座位的话第二夜不会再 arm）。 */
function _armKey(need){
  /* 🔴 2026-10-12 bug2（用户实机："没点叉没操作 → 毒药面板没弹"）：键必须带
     **当前窗口**的节点（now.node）。女巫没答解药时，她的待办 node 一直是
     witch_heal —— 毒药窗口若沿用这个键，就和解药窗口的 ARMED_SIG 完全相同
     ⇒ 不再 arm ⇒ started 永远 false ⇒（bug1 的闸）面板永远不开。 */
  var _n = (PUBLIC && PUBLIC.now && PUBLIC.now.node) || need.node || '';
  return need.seat + ':' + _n + ':' + ((PUBLIC && PUBLIC.round) || 0);
}
/* 夜间节点的人话名字（提示行用；服务端只下发 id）。 */
var NIGHT_NAME = { guard: '守卫', wolf: '狼人·刀口', witch_heal: '女巫·解药',
                   witch_poison: '女巫·毒药', seer: '预言家',
                   seer_result: '预言家·结果', seer_close: '预言家·收尾' };

/* 狼队共屏（2026-10-07 第七起，用户定）：**无论几只人狼，整个狼队只选一个刀口**，
   而且**选中后不可切、不可撤**（与女巫毒药 / 预言家验人同一口径）。
   为什么要改：旧实现 `cap = 狼的数量`、且提交时**按顺序给每只狼分一个**（没选满的
   补 `-1`）⇒ 三个人狼点一个人（用户原话"可以选三个人刀"），而后两只狼提交的是
   **空刀** ⇒ 引擎那条"有空刀就不定刀"的规则让**整刀落空**（用户实测"实际空刀"）。 */
function renderWolfPack(seatNo, v){
  WHEEL.act = "kill";
  WHEEL.picks = [];
  $("bigTitle").textContent = "狼人";
  $("bigIcon").textContent = "🎯";
  $("bigWho").textContent = "狼队共屏：选一个人刀";
  $("hoTip").textContent = "点一个号码（选中后不可改）→ 点圆心提交";
  var pool = (v.players || []).filter(function(p){ return p.alive; });
  var alive = {};
  pool.forEach(function(p){ alive[p.seat] = true; });
  var bs = $("hoActs").querySelectorAll(".wnum");
  for (var i = 0; i < bs.length; i++){
    var no = Number(bs[i].getAttribute("data-no"));
    if (!alive[no - 1]) continue;
    bs[i].classList.add("pick");
    /* 🔴 2026-10-07 用户定（"AI 狼刀的号码圆圈上要有标识"）：把**别的狼已经点过的**
       目标标出来 —— 人狼一眼就能跟刀（引擎本来就会按 `wolf_order` 归拢，标记只是让人看得见）。
       用现成的 `.tag.knife` 样式（play.html 里已有）。 */
    bs[i].querySelectorAll(".tag.knife").forEach(function(t){ t.remove(); });
    for (var q in (v.wolf_points || {})){
      if (Number(q) !== seatNo - 1 && v.wolf_points[q] === no - 1){
        var tg = document.createElement("div");
        tg.className = "tag knife";
        tg.textContent = "刀";
        bs[i].appendChild(tg);
        break;
      }
    }
    bs[i].onclick = function(ev){
      ev.stopPropagation();
      var no2 = Number(this.getAttribute("data-no"));
      if (WHEEL.picks.length){
        if (WHEEL.picks[0] !== no2){ $("hoTip").textContent = "已经选定了，不能改"; }
        return;
      }
      WHEEL.picks = [no2];
      this.classList.add("sel");
      /* 🔴 2026-10-07 用户定（"选中刀口后不该还能点中央确认，冗余"）：
         点号码**即刻提交**（与女巫毒药 / 预言家验人同一口径），圆心不再参与。 */
      $("hoTip").textContent = "已刀 " + no2 + " 号（提交中…）";
      wheelConfirmWolfPack();
    };
  }
  $("wheelExtra").innerHTML = '';
  $("wheelExtra").classList.add("on");
  var bk = $("hoBack");
  if (bk) bk.onclick = function(ev){ ev.stopPropagation(); closeHandoff(); };
}

/* 狼队批量提交：每狼一刀（没选满=空刀）+ 每狼确认，一次圆心全搞定。
   WHEEL.batch=true 期间 tick 不许撤屏重建（否则打断提交序列）。 */
async function wheelConfirmWolfPack(){
  var wolves = WHEEL.allies;
  var picks = WHEEL.picks.slice();
  WHEEL.batch = true;
  setBusy(true);
  try {
    /* ⚠️ 单步容错（2026-10-06 晚）：第 1 狼提交后引擎可能已推进
       （AI 秒跟 / 女巫待办出现），后续提交会 400 —— 跳过继续，
       不让一个 400 把序列和屏幕卡死（实报：狼人过后前端停滞）。 */
    var step = async function(body){
      try {
        var d = await api("/api/game/" + GID + "/act", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        PUBLIC = d.public;
      } catch (e) { /* 该狼那一步已经不需要了，跳过 */ }
    };
    /* 🔴 2026-10-07（用户："目前人狼选刀后，实际空刀"）：**所有狼提交同一个刀口**。
       旧实现给第 i 只狼分 `picks[i]`、没选满的补 `-1`（空刀）⇒ 只要人狼多于一个，
       后面的狼就提交空刀 ⇒ 引擎那条"有空刀就不定刀"让整刀落空。 */
    var one = picks.length ? picks[0] : -1;
    for (var i = 0; i < wolves.length; i++){
      await step({ seat: wolves[i] - 1, action: "kill",
                   target: (one === -1) ? -1 : one - 1 });
    }
    for (var j = 0; j < wolves.length; j++){
      await step({ seat: wolves[j] - 1, action: "confirm" });
    }
    paintSeats(); renderLog(); speakQueue();
    closeHandoff();
    setTimeout(tick, 300);
  } catch (e) {
    handoffMsg("提交失败：" + e.message, true);
    WHEEL.batch = false;
  } finally {
    setBusy(false);
  }
}
/* 倒计时数字：画在圆心下方。left<=5s 变红。 */
function paintCountdown(){
  var el = document.getElementById("cdNum");
  if (!el) return;
  /* 拨号盘头部倒计时镜像（2026-10-12）：大圆藏起来时数字要出现在角色大字下 */
  var _mirror = function(){
    var d2 = document.getElementById("dialCd");
    if (d2 && $('nightDial').classList.contains('on')){
      d2.textContent = el.textContent;
      d2.className = 'cd' + (el.className.indexOf('urgent') >= 0 ? ' urgent' : '');
    }
  };
  setTimeout(_mirror, 0);
  var on = NIGHT_CD && WHEEL.mode === "act" && WHEEL.act !== "done";
  if (!on){ el.textContent = ""; el.classList.remove("urgent"); return; }
  var left = NIGHT_CD.left - (Date.now() - NIGHT_CD.at) / 1000;
  if (left <= 0){
    el.textContent = "0"; el.classList.add("urgent");
    /* 2026-10-07 用户定：以面板消失时的状态为准 —— 归零自动提交当前状态 */
    if (!WHEEL._autoSent){
      WHEEL._autoSent = true;
      var seatNo = WHEEL.seatNo;
      if (WHEEL.act === 'noop'){ submit(seatNo, 'pass', null, null); }
      else if (WHEEL.act === 'heal'){
        /* 🔴 2026-10-07（用户定）：狼空刀时解药那一屏**照常走**，只是按钮置灰 ——
           这时**什么都不提交**：heal 没人可救（引擎会拒），pass 会把 `witch_asked`
           置位、后面"你要使用毒药吗？"就再也不会问了（那一问要照常走）。
           🔴 2026-10-12 bug2 真凶（用户 F12 实锤：now.kind='none'、need=null、
           why=这一节没有人类要点）：**正常刀口 + 没用解药**时归零提交了 pass
           ⇒ 同样置位 witch_asked ⇒ 引擎 `_actions_available` 的
           `not witch_asked` 在毒药窗口不再给她任何动作 ⇒ 毒药面板整窗不出。
           上面那条注释早把机理写明白了，只是当年只挡了空刀那一半 —— 与服务端
           `_inject_default` 第四起·续一模一样的错（那次的修法 = default=None）。
           现在：**没用解药且毒药还在 ⇒ 什么都不提交**；毒药的弃权由毒药窗口
           自己的到点注入负责（`_inject_default('witch_poison')='pass'`）。 */
        if (WHEEL.healOn){
          submit(seatNo, 'heal',
                 WHEEL.view.night_victim != null ? WHEEL.view.night_victim + 1 : null,
                 null);
        } else if (!WHEEL.healHasPoison){
          submit(seatNo, 'pass', null, null);   /* 手里没毒药了 ⇒ 放弃收口这一夜 */
        }
        /* 没用解药且毒药还在 ⇒ 什么都不提交，毒药那一问照常走 */
      }
      else if (WHEEL.act === 'kill' || WHEEL.act === 'guard'
               || WHEEL.act === 'investigate' || WHEEL.act === 'poison'){
        /* 🔴 2026-10-07（定稿交互，用户实机点名）：这几种**点号码那一刻就提交了**
           （见 wheelSelect），归零时**不再补发** —— 补发只会重复提交
           （毒药会报"已经用过了"）；而"从头到尾没点过"由**服务端窗口到点注入默认**
           处理（跟随 AI 狼 / 空守 / 不验 / 不用药），前端抢发反而会绕过"人狼跟 AI"。 */
      }
      /* vote 不自动提交：人类点「OK / 弃票」，AI 由服务端收割 */
    }
    return;
  }
  el.textContent = Math.ceil(left);
  el.classList.toggle("urgent", left <= 5);
}
function others(v){
  return (v.players || []).filter(function(p){ return p.alive && p.seat !== v.seat; });
}

function wheelAngle(){ return WHEEL.angle; }

function wheelApply(){
  var rot = $('wheelRot');
  if (rot) rot.style.transform = 'rotate(' + WHEEL.angle + 'deg)';
}

/* 把 N 号（1-based）转到圆盘**正下方**（持机人面前） */
function wheelPointTo(no){
  var n = (PUBLIC && PUBLIC.players || []).length || 1;
  var step = 360 / n;
  var a = (no - 1) * step;              /* 渲染时 1 号在正上方，顺时针排 */
  WHEEL.angle = 180 - a;                /* 转到 180°（正下方） */
  wheelApply();
}

function openHandoff(need, allies){
  /* 🔴 2026-10-12 bug1/bug2（用户实机：女巫面板在狼人闭眼播报刚念出就弹（偶发）、
     预言家面板在女巫闭眼播报刚念出就弹（每次都是））：夜间面板原来在 `now.need`
     一出现就开（= 节点一开就开），比播报早了一整段。
     用户定稿口径（原话）："这俩面板应该在前一人闭眼播报出来后 5 秒再弹出。
     或者你现在弹出倒计时的时点就很完美，操作面板和倒计时同时弹出我也完全接受"
     ⇒ 采用后者：**面板与倒计时同时出**。倒计时的时点 = arm 之后
     （`now.started`，即"本段那句播报念完 +2 秒"，服务端裁决）。
     · 只闸**夜间的 wheel**；shoot 不在剧本节点里（now 没有 started），不闸。
     · `started === false` 才算"没起算"（老服务端没有这个字段 ⇒ 照旧开，不憋死）。
     · 女巫已点✕但还在解药窗口（`_witchPassed`）也不开：她答完了，毒药那一问
       要等毒药窗口自己的 arm —— 否则就是"点完叉毒药屏立刻弹（太早）"。
       这同时修了 bug2 的另一半：没点叉没操作 ⇒ 解药窗口到点 ⇒ 毒药窗口照常
       arm ⇒ 面板在毒药播报念完后弹出（服务端 I8 已证毒药问不会被掐死）。
     · 只闸"开"不闸"留"：女巫一轮两问，解药窗口的面板在毒药窗口 arm 之前
       必须继续挂着（定稿：面板不消失）—— 已开的面板不走这里。 */
  if (PUBLIC.phase === 'night' && PUBLIC.now && PUBLIC.now.kind === 'wheel'){
    var _nd = PUBLIC.now.node;
    var _passed = _nd === 'witch_heal' && need.actions
                  && need.actions.indexOf('heal') < 0
                  && need.actions.indexOf('noop') < 0;
    /* 🔴 2026-10-12 手机实机两连（用户）：① 面板弹出时倒计时已是 0 —— 窗口其实
       已过期（arm 到得晚/轮询慢）⇒ **窗口已过期（until<=now）就不开**；
       ② 狼人面板压根没弹 —— 手机上 arm 请求偶发丢失 ⇒ 光等 started 会整窗
       没面板。加"本段那句应播时刻已过 8 秒"的**卡死放行**（与 arm 兜底同条件）：
       就算 arm 没送到，面板也照出（倒计时等 arm 落地才显示；服务端有整窗保护）。 */
    var _tlD = (PUBLIC && PUBLIC.timeline) || [];
    var _dueB = (PUBLIC.now && typeof PUBLIC.now.panel_line === 'number'
                 && PUBLIC.now.panel_line >= 0)
      ? (_tlD[PUBLIC.now.panel_line] || null) : null;
    var _stalled = !!(_dueB && _dueB.at
                      && (Date.now() / 1000 - (_dueB.at + (_dueB.delay || 0))) > 8);
    /* 🔴 2026-10-22：过期判定优先用剩余秒数（until_left<=0）—— 墙钟比较
       （until<=本地now）在设备时钟有偏差时会**误杀整窗面板**（手机快 10 秒
       ⇒ 窗口明明还剩 10s 却判过期 ⇒ 面板不开）。until_left 是服务端在
       arm 同拍下发的剩余秒数，用它本地衰减，免疫时钟差。老服务端回退墙钟。 */
    var _expired = (typeof PUBLIC.now.until_left === 'number')
      ? (PUBLIC.now.until_left <= 0)
      : (PUBLIC.now.until != null
         && PUBLIC.now.until <= Date.now() / 1000);
    if ((PUBLIC.now.started === false && !_stalled) || _passed || _expired){
      closeHandoff();
      return false;
    }
  }
  /* 2026-10-06（晚）用户定：**「交接认领」步骤删除** —— 播报放出即视为
     那个人拿到了手机，直接进操作屏。allies = 狼刀共屏时的全体人狼待办
     （含本人，1-based 座位号数组；单狼时长度 1）。 */
  HANDOFF = need.seat + 1;
  HANDOFF_SIG = sigOf(need);
  MYVIEW = null; PICK = null;
  WHEEL = { mode: 'act', seatNo: need.seat + 1, view: null,
            act: null, pick: null, sub: null, angle: WHEEL.angle,
            allies: (allies && allies.length > 1) ? allies : null,
            picks: [], batch: false,
            /* 这一屏是为哪个节点开的（2026-10-12 bug2）：节点变了就要重建，
               否则女巫的面板会停在解药屏上直到白天（内容只在这里渲染）。 */
            forNode: (PUBLIC.now && PUBLIC.now.node) || null };
  $('handoff').classList.add('on');
  $('handoff').classList.remove('speak');
  $('hoSpeak').classList.remove('on');
  renderWheelBase();
  var errEl = $('hoErr');
  if (errEl) errEl.textContent = '';
  $('wheelBtns').classList.remove('on');
  $('wheelExtra').classList.remove('on');
  $('wheelExtra').innerHTML = '';
  BUSY = false;
  foot((need.seat + 1) + ' 号操作中');
  wheelTake();
}

/* 无人操作时的轮盘：只画公开刻度（号码/存活/阵营色），中心是法官 */
function renderWheelBase(){
  var box = $('hoActs');
  if (!box || !PUBLIC || !PUBLIC.players) return;
  var n = PUBLIC.players.length;
  var step = 360 / n;
  var h = '';
  PUBLIC.players.forEach(function(p){
    var a = (p.seat) * step;                      /* 1 号在正上方，顺时针排 */
    var rad = a * Math.PI / 180;
    var x = 50 + 46 * Math.sin(rad);              /* 刻度贴圆周（46% ≈ 半径留边） */
    var y = 50 - 46 * Math.cos(rad);
    h += '<button class="wnum ' + (p.kind === 'ai' ? 'ai' : 'human')
       + (p.alive ? '' : ' dead')
       + '" data-no="' + (p.seat + 1)
       + '" style="left:' + x + '%;top:' + y + '%">' + (p.seat + 1) + '</button>';
  });
  box.innerHTML = h;
  var tip = $('hoTip');
  if (tip && WHEEL.mode === null) tip.textContent = '';
}

/* 摘掉所有号码按钮的可点状态（切屏时防幽灵点击，见 renderWheelAct 内调用处） */
function _stripNumberButtons(){
  var box = document.getElementById('hoActs');
  if (!box) return;
  var bs = box.querySelectorAll('.wnum');
  for (var i = 0; i < bs.length; i++){
    bs[i].classList.remove('pick', 'sel');
    bs[i].removeAttribute('data-act');
    bs[i].removeAttribute('data-target');
    bs[i].onclick = null;
  }
}

/* 画/改刻度上的私有标记（随盘转）：狼队友 🐺、验人结果、被刀 ✚、狼刀指向 */
function wheelTags(v){
  var box = $('hoActs');
  if (!box) return;
  var bs = box.querySelectorAll('.wnum');
  for (var i = 0; i < bs.length; i++){
    var no = Number(bs[i].getAttribute('data-no'));
    var old = bs[i].querySelector('.tag');
    if (old) old.remove();
    var t = null, cls = '';
    if (v && v.team_mates && v.team_mates.indexOf(no - 1) >= 0){
      t = '🐺'; cls = 'wolfmate';
    }
    if (v && v.investigations && (no - 1) in v.investigations){
      t = (v.investigations[no - 1] ? '狼' : '好');
      cls = v.investigations[no - 1] ? 'seerwolf' : 'seergood';
    }
    if (t){
      var s = document.createElement('i');
      s.className = 'tag ' + cls;
      s.textContent = t;
      bs[i].appendChild(s);
    }
  }
}

/* 狼夜实时盘子：把每只狼此刻的点/锁定画到**目标号码的刻度**上
   （2026-10-05 用户定：狼互相看得到对方点了谁，都显示在轮盘刻度上）。
   ⚠️ 只重画 .knife 标记，不重建号码按钮 —— 重建会把点选中的状态打断。 */
function paintWheelKnives(v){
  var box = $('hoActs');
  if (!box) return;
  var bs = box.querySelectorAll('.wnum');
  for (var i = 0; i < bs.length; i++){
    var old = bs[i].querySelector('.tag.knife');
    if (old) old.remove();
  }
  if (!v || !v.wolf_points || v.phase !== 'night') return;
  var pts = v.wolf_points, conf = v.wolf_confirmed || [];
  for (var s in pts){
    var t = pts[s];
    if (t === null || t === undefined || t === -1) continue;   /* 没点 / 空刀不标 */
    var el = box.querySelector('.wnum[data-no="' + (t + 1) + '"]');
    if (!el) continue;
    var mark = document.createElement('i');
    mark.className = 'tag knife';
    mark.textContent = "\u{1F52A}";  /* 2026-10-06 晚：不标是谁点的，只标这个号码被刀 */
    el.appendChild(mark);
  }
}

/* 旧 paintWolfBoard（座位条盘子）已由 paintWheelKnives（轮盘刻度标记）取代，
   2026-10-05 连同 #wolfBoard/#wolfLine 两个 DOM 一起移除。 */

/* ================================================================
   轮盘操作屏（替代旧 renderHandoff 卡片）
   私有信息只以**刻度标记**出现（队友🐺/验人/被刀✚），随盘旋转 ——
   白天发言/投票时不带任何标记（2026-10-02 约束保留）。
   动作一律「点数字选中 → 点圆心确认」；女巫解药是 ✓/✕ 两个大按钮。
   ⚠️ 修复：旧卡片的目标按钮列表漏了 guard —— 人类守卫根本没法守人
   （guard 从未出现在旧 targets 过滤器里）。本轮把守卫纳入轮盘选人。
   ================================================================ */
var WHEEL_LABEL = { kill: '刀', investigate: '验', heal: '救', poison: '毒',
                    guard: '守', shoot: '枪', vote: '投' };

var _lastRender = { sig: '', at: 0 };

/* ================================================================ 拨号盘（2026-10-12 用户定稿）
   夜间/投票弃用大圆盘：一排三个号码按钮（123/456/789…），最后一个按钮 =
   空刀（狼）/ 不救（女巫解药）/ 不毒（女巫毒药）/ 弃票（投票）。
   页面除号码数字外无文案；顶部 = 角色大字 + 倒计时。
   女巫救人 = 被刀号码高亮，点它即救助。 */
var DIAL_NAMES = { wolf: '狼人', witch_heal: '女巫', witch_poison: '女巫',
                   seer: '预言家', guard: '守卫', shoot: '猎人' };

/* 拨号盘上的狼刀指向标记（随每拍刷新 —— AI 狼点了谁立刻看得见） */
function paintDialKnives(v){
  var d = $('nightDial');
  if (!d || !v) return;
  d.querySelectorAll('button[data-no]').forEach(function(b){
    if (b.textContent.indexOf('🔪') === 0) b.textContent = b.getAttribute('data-no');
  });
  var pts = v.wolf_points || {};
  Object.keys(pts).forEach(function(k){
    var t0 = pts[k];
    if (t0 === null || t0 === undefined || t0 === -1) return;
    var kb = d.querySelector('button[data-no="' + (t0 + 1) + '"]');
    if (kb && kb.textContent.indexOf('🔪') !== 0){
      kb.textContent = '🔪' + kb.textContent;
      kb.classList.add('victim');
    }
  });
}

function renderNightDial(seatNo, v, acts){
  var d = $('nightDial'), head = $('dialHead');
  d.innerHTML = '';
  var players = v.players || [];
  var alive = {};
  players.forEach(function(p){ alive[p.seat] = p.alive; });
  var n = players.length;
  var act = null;
  ['kill', 'guard', 'investigate', 'poison', 'vote'].forEach(function(a){
    if (act === null && acts.indexOf(a) >= 0) act = a;
  });
  var isHeal = acts.indexOf('heal') >= 0;
  var pool = {};
  if (act === 'vote'){
    players.forEach(function(p){ if (p.alive) pool[p.seat] = true; });  // 投票可自投
  } else if (act){
    others(v).forEach(function(p){ if (p.alive) pool[p.seat] = true; }); // 其余不含自己
  }
  var kindOfNo = function(seat0){
    var p = players.filter(function(q){ return q.seat === seat0; })[0];
    return (p && p.kind === 'human') ? 'human' : 'ai';
  };
  for (var i = 0; i < n; i++){
    (function(no0){
      var b = document.createElement('button');
      b.textContent = no0 + 1;
      b.setAttribute('data-no', no0 + 1);   /* 验人结果标 thumb 时按号找按钮（2026-10-12） */
      b.className = kindOfNo(no0);
      if (!alive[no0]) b.classList.add('dead');
      if (isHeal && v.night_victim === no0){
        b.classList.add('victim');
        b.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'heal', no0 + 1, null); };
      } else if (act && pool[no0]){
        b.onclick = function(ev){ ev.stopPropagation(); wheelSelect(act, no0 + 1); };
      }
      d.appendChild(b);
    })(i);
  }
  var extra = document.createElement('button');
  extra.className = 'act';
  if (isHeal){
    extra.textContent = '不救';
    /* 🔴 2026-10-12 bug2 的教训在这里落地：解药"不救"**绝不提交 pass**
       （pass ⇒ witch_asked ⇒ 毒药那一问消失）。毒药的弃权由毒药窗口自己的
       到点注入负责 —— 这个按钮只是明示"我不救"，夜照常走。 */
    extra.onclick = function(ev){ ev.stopPropagation(); };
  } else if (act === 'kill'){
    extra.textContent = '空刀';
    extra.onclick = function(ev){ ev.stopPropagation(); wheelSelect('kill', -1); };
  } else if (act === 'poison'){
    extra.textContent = '不毒';
    extra.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'pass', null, null); };
  } else if (act === 'vote'){
    extra.textContent = '弃票';
    /* 🔴 2026-10-22（用户定，推翻 2026-09-17 的"弃票=自投"先例）：
       弃票就是弃票 —— 引擎本来就支持 target=-1（cast_vote 的 docstring、
       tally_text/tally_speech 的弃票口径都是现成的），之前是这里把 -1
       实现成了 seatNo。真·弃票不计入任何人的票。 */
    extra.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'vote', -1, null); };
  } else if (act === 'guard'){
    extra.textContent = '空守';
    extra.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'guard', -1, null); };
  } else if (act === 'investigate'){
    extra.textContent = '不验';
    extra.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'pass', null, null); };
  } else if (acts.indexOf('confirm') >= 0){
    extra.textContent = '确定';
    extra.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'confirm', null, null); };
  } else if (acts.indexOf('shoot') >= 0){
    extra.textContent = '不开枪';
    extra.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'pass', null, null); };
  } else {
    extra.textContent = '·';
  }
  d.appendChild(extra);
  /* 狼队友的刀指向（2026-10-12 用户：拨号盘上也要看得见 AI 狼点了谁）——
     谁点了谁，那个号码前加 🔪 并高亮。 */
  paintDialKnives(v);
  d.classList.add('on');
  var bc = $('bigCircle');
  if (bc) bc.style.visibility = 'hidden';
  var _ht = DIAL_NAMES[(PUBLIC.now && PUBLIC.now.node)];
  if (!_ht && act === 'vote') _ht = seatNo + ' 号投票';   /* 用户：投票界面要显示当前投票的人 */
  head.querySelector('.r').textContent = _ht || '请操作';
  head.classList.add('on');
  /* WHEEL 状态照旧写（走查/自检按它判"卡住"） */
  WHEEL.mode = 'act';
  WHEEL.seatNo = seatNo;
  WHEEL.view = v;
  WHEEL.act = isHeal ? 'heal' : (act || 'noop');
  WHEEL.pick = null; WHEEL.sub = null;
  $('wheelBtns').classList.remove('on');
  $('wheelExtra').classList.remove('on');
}
function renderWheelAct(seatNo, v, actsOverride){
  window.__rc = (window.__rc || 0) + 1;   /* 调试探针：重渲染计数（跑完删） */
  try {
  /* 🔴 2026-10-06（晚）走查实锤：kill 屏与「空待办自愈」交替重渲染会把
     玩家两步点击（点号码→点圆心）拦腰打断（.sel/pick 被清）⇒ 刀永远提交
     不出去。同签名同屏 800ms 内不重渲染；显式 actsOverride（改刀重选等）
     不受此限。 */
  var _sig = seatNo + ':' + (actsOverride || v.can_act || []).join(',');
  if (!actsOverride && _sig === _lastRender.sig
      && Date.now() - _lastRender.at < 800 && WHEEL.mode === 'act'){
    return;
  }
  _lastRender = { sig: _sig, at: Date.now() };
  WHEEL.mode = 'act';
  WHEEL.seatNo = seatNo;
  WHEEL.view = v;
  WHEEL.act = null; WHEEL.pick = null; WHEEL.sub = null;
  renderWheelBase();
  var acts = actsOverride || v.can_act || [];
  /* 🔴 2026-10-07 第三期走查实锤：`v.can_act` 可能**刚好是空的**（视图是"上一步提交完"
     那一刻取的：待办已经变了、但 pending_seats 这一拍还列着你）⇒ 下面所有分支都不匹配，
     屏幕上留下一个**一个按钮都没有的轮盘**（`WHEEL.act` 还是 null）。
     真人看到的就是"这屏没反应"，走查直接判「卡住」（5 条分支都认不出它）。
     ⇒ 不留空白：关掉交接屏，让下一拍 tick 重新取视图（取到新动作就正常出屏）。
     ⚠️ 这一条修在 2026-10-07 晚，**没有**再跑一遍完整走查复验（见 §10.7 的说明）。 */
  if (!acts.length){
    closeHandoff();
    setTimeout(tick, 200);
    return;
  }
  /* 🔴 2026-10-12 用户定稿：夜间/投票**弃用大圆盘，统一拨号盘**（白天发言仍用大圆）*/
  if (PUBLIC.phase === 'night' || PUBLIC.phase === 'shoot'
      || acts.indexOf('vote') >= 0 || acts.indexOf('shoot') >= 0){
    renderNightDial(seatNo, v, acts);
    return;
  }
  var bc = $('bigCircle');
  bc.classList.add('acting');
  $('handoff').classList.add('on');
  $('handoff').classList.remove('speak');
  $('hoSpeak').classList.remove('on');
  $('wheelBtns').classList.remove('on');
  $('wheelExtra').classList.remove('on');
  $('wheelExtra').innerHTML = '';
  var errEl = $('hoErr');
  if (errEl) errEl.textContent = '';

  var targets = acts.filter(function(a){
    /* 2026-10-07 剧本节拍：女巫"药已用"时给 noop 占位待办 —— 面板/倒计时照常，防泄露 */
    if (acts.length === 1 && acts[0] === 'noop'){
      /* noop 占位：这一屏**没有可点的东西**（女巫"药已用/还没轮到她那一问"），
         就是让人看着节拍走完。⚠️ 仍要写 `WHEEL.act` —— 否则外部（走查/自检）
         只看到 mode=act 而 act 空，会判成"卡住"（2026-10-07 走查实测）。 */
      WHEEL.act = 'noop';
      WHEEL.pick = null;
      $('bigTitle').textContent = '等待';
      $('bigIcon').textContent = '🌙';
      $('bigWho').textContent = '夜间流程进行中…';
      $('hoTip').textContent = '';
      return;
    }
    return ['kill', 'investigate', 'poison', 'guard', 'vote'].indexOf(a) >= 0;
  });

  /* —— 女巫解药：✓ / ✕（用户点名的大按钮），被刀号标红 —— */
  /* 用户定："解药倒计时结束就直接切毒药面板" ⇒ 当前节点已是 witch_poison 就走毒药屏。 */
  if (acts.indexOf('heal') >= 0
      && !(PUBLIC.now && PUBLIC.now.node === 'witch_poison')){
    WHEEL.act = 'heal';
    /* 🔴 2026-10-07（用户定）：狼空刀 ⇒ 女巫**照常**走解药播报与面板流程，
       只是「用解药」按钮**置灰不可点**（没人可救）。所以这一屏照出：
         · 不再有"被刀号标红"（没人被刀）
         · 按钮写"无人被刀"并 disabled（CSS .off 置灰）
         · 归零**什么都不提交**（见 paintCountdown 里的 healBlocked）
       ⚠️ 不能把这一屏整个跳过 —— 那样播报节奏与"两问"的流程就跟别的夜不一样了。 */
    var _noVictim = (v.night_victim == null);
    var victimNo = _noVictim ? null : (v.night_victim + 1);
    WHEEL.healBlocked = _noVictim;
    /* 归零自动提交用（2026-10-12 bug2）：毒药还在不在 —— 在 ⇒ 解药归零不许代投 pass */
    WHEEL.healHasPoison = acts.indexOf('poison') >= 0;
    if (!_noVictim){
      var vEl = $('hoActs').querySelector('.wnum[data-no="' + victimNo + '"]');
      if (vEl) vEl.classList.add('victim');
    }
    $('bigTitle').textContent = '女巫';
    $('bigIcon').textContent = _noVictim ? '🌙' : '⚗️';
    WHEEL.healOn = false;
    $('bigWho').textContent = _noVictim
      ? '无人被刀，解药无处可用'
      : victimNo + ' 号被刀，是否使用解药？';
    $('hoTip').textContent = _noVictim
      ? '这一晚没人被刀 —— 解药按钮置灰；这一屏走完，下一问是毒药'
      : '点「用解药」救他（再点可取消）；窗口结束自动生效';
    $('wheelBtns').classList.add('on');
    $('wheelExtra').innerHTML = '';
    $('wheelExtra').classList.add('on');
    var bkW = $('hoBack');
    if (bkW) bkW.onclick = function(ev){ ev.stopPropagation(); closeHandoff(); };
    var _yesBtn = $('wheelYes');
    if (_noVictim){
      _yesBtn.textContent = '无人被刀';
      _yesBtn.disabled = true;                 /* 置灰不可点（空刀：解药无处可用） */
      _yesBtn.classList.add('off');
      _yesBtn.onclick = null;
    } else {
      _yesBtn.textContent = '用解药';
      _yesBtn.disabled = false;
      _yesBtn.classList.remove('off');
      _yesBtn.onclick = function(){
        /* 2026-10-07 用户定：点一下=用（变红「取消」），再点=取消；窗口结束自动提交 */
        WHEEL.healOn = !WHEEL.healOn;
        this.textContent = WHEEL.healOn ? '取消' : '用解药';
        this.classList.toggle('on', WHEEL.healOn);
      };
    }
    $('wheelNo').onclick = function(){
      if (acts.indexOf('poison') >= 0){
        /* 解药留着（空刀时是"没得用"）：接下一问「要毒谁」（轮盘选人 → 圆心确认） */
        var rest = acts.filter(function(a){ return a !== 'heal'; });
        renderWheelAct(seatNo, v, rest);
      } else {
        submit(seatNo, 'pass', null, null);
      }
    };
    return;
  }

  /* —— 猎人开枪（2026-10-06 走查抓到：轮盘改版漏了这一支，猎人只能放弃
     不能开枪 ⇒ human_shoot 永远悬着，局永远推不完）—— */
  if (acts.indexOf('shoot') >= 0){
    WHEEL.act = 'shoot';
    $('bigTitle').textContent = '猎人';
    $('bigIcon').textContent = '🏹';
    $('bigWho').textContent = '开枪带走谁？点他的号码';
    $('hoTip').textContent = '点数字选中 → 再点圆心确认（或选「不开枪」）';
    var poolS = others(v);
    var aliveS = {};
    poolS.forEach(function(p){ aliveS[p.seat] = true; });
    var bsS = $('hoActs').querySelectorAll('.wnum');
    for (var iS = 0; iS < bsS.length; iS++){
      var noS = Number(bsS[iS].getAttribute('data-no'));
      if (!aliveS[noS - 1]) continue;
      bsS[iS].classList.add('pick');
      bsS[iS].setAttribute('data-act', 'shoot');
      bsS[iS].setAttribute('data-target', noS);
      bsS[iS].onclick = function(ev){
        ev.stopPropagation();
        wheelSelect('shoot', Number(this.getAttribute('data-target')));
      };
    }
    $('wheelExtra').innerHTML = '<button id="hoNoShoot">不开枪</button>'
      + '';
    $('wheelExtra').classList.add('on');
    var ns = $('hoNoShoot');
    if (ns) ns.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'pass', null, null); };
    var bkS = $('hoBack');
    if (bkS) bkS.onclick = function(ev){ ev.stopPropagation(); closeHandoff(); };
    return;
  }

  /* —— 目标类动作：轮盘选人 —— */
  if (targets.length){
    var a = targets[0];
    /* 狼队共屏（2026-10-06 晚用户定）：多个人狼待办 → 多选屏 */
    if (a === 'kill' && WHEEL.allies && WHEEL.allies.length > 1){
      renderWolfPack(seatNo, v);
      return;
    }
    WHEEL.act = a;
    var TITLE = { kill: '狼人', investigate: '预言家', poison: '女巫',
                  guard: '守卫', vote: '投票' };
    /* 女巫毒药屏带上「今晚谁被刀」：旧卡片版由 mineBlock 私有块提供，
       轮盘版写进问句（本屏只有女巫本人看，不泄露）—— 毒人决策的必要输入。 */
    /* 🔴 2026-10-07 用户定：毒药屏也要看得到狼人的操作 —— 刀了谁要标出来，
       **空刀也要说**（"看不到狼人的操作"指的就是空刀时这一屏什么都不说）。 */
    var victimNote = (a === 'poison')
      ? (v.night_victim != null ? '今晚 ' + (v.night_victim + 1) + ' 号被刀。' : '今晚空刀。')
      : '';
    $('bigTitle').textContent = TITLE[a] || '操作';
    $('bigIcon').textContent = '🎯';
    $('bigWho').textContent = victimNote + { kill: '要刀谁？点他的号码', investigate: '要验谁？点他的号码',
                                poison: '要毒谁？点他的号码', guard: '要守谁？点他的号码',
                                vote: '投给谁？点他的号码' }[a];
    /* 毒药屏也把被刀号标红（heal 问句屏标过，但 renderWheelBase 重建刻度会清掉） */
    if (a === 'poison' && v.night_victim != null){
      var vEl2 = $('hoActs').querySelector('.wnum[data-no="' + (v.night_victim + 1) + '"]');
      if (vEl2) vEl2.classList.add('victim');
    }
    $('hoTip').textContent = '点数字选中 → 点圆心确认（或「弃票」）';
    /* 目标池（沿用旧卡片的口径）：
       · 投票允许投自己（2026-09-17 用户定）
       · 刀允许自刀（2026-10-04 用户定）
       · 其余从活人里选（不含自己） */
    var pool;
    if (a === 'vote' || a === 'kill'){
      pool = (v.players || []).filter(function(p){ return p.alive; });
    } else {
      pool = others(v);
    }
    var alive = {};
    pool.forEach(function(p){ alive[p.seat] = true; });
    var bs = $('hoActs').querySelectorAll('.wnum');
    for (var i = 0; i < bs.length; i++){
      var no = Number(bs[i].getAttribute('data-no'));
      if (!alive[no - 1]) continue;
      bs[i].classList.add('pick');
      bs[i].setAttribute('data-act', a);
      bs[i].setAttribute('data-target', no);        /* 1-based：submit() 里转回 0-based */
      bs[i].onclick = function(ev){
        ev.stopPropagation();                        /* 别让点数冒泡成"点圆心" */
        wheelSelect(this.getAttribute('data-act'),
                    Number(this.getAttribute('data-target')));
      };
    }
    /* 轮盘下方的补充入口：空刀 / 就这个，确定 / 交回。
       🔴 2026-10-05：**多狼局点了刀还得「确定」** —— 只要这只狼没确认，
       引擎就一直给 ['kill','confirm']（见 engine._actions_available）；
       只渲染「选刀」不给确认入口 ⇒ 玩家（和走查）永远绕不出来：点完刀又是点完刀。
       「就这个，确定」只在**他自己已经指过之后**才给 —— 没指过就确定是空动作
       （engine.night_confirm 要求 actor 已经在 wolf_points 里）。 */
    var extra = '';
    if (a === 'kill'){
      extra += '<button id="hoPass0" data-act="kill" data-target="-1">空刀</button>';
    }
    /* 2026-10-07 第二期（用户定稿）：人类投票屏要有「弃票」出口（= 自投，先例见
       engine.cast_vote 允许自投）。原来提示行写着「（或「弃票」）」，
       但页面上**根本没有这个按钮**（下面的接线一直在找不存在的 #hoAbstain）——
       这里补上它，那句提示才成立。 */
    if (a === 'vote'){
      extra += '<button id="hoAbstain">弃票</button>';
    }
    /* ⚠️ 2026-10-07 走查实锤（多狼局）：判据原来只看 **key 在不在**，
       而引擎给的 `wolf_points` **包含自己那一格**（值可能是 null = 还没点）⇒
       人类狼一睁眼就被画上「就这个，确定」，一点就是
       400「RuntimeError: 还没点人，不能确认」，走查因此在同一屏连点 **166 次**
       （真人会以为界面坏了）。正确判据 = **这一格有值**
       （-1 空刀 / 座位号 / 自己 都算点过）。 */
    var _mpt = (v.wolf_points || {})[String(seatNo - 1)];
    var pointed = (_mpt !== null && _mpt !== undefined);
    if (acts.indexOf('confirm') >= 0 && pointed){
      extra += '<button id="hoConfirm">就这个，确定</button>';
    }
    extra += '';
    $('wheelExtra').innerHTML = extra;
    $('wheelExtra').classList.add('on');
    var kd = $('hoPass0');
    if (kd) kd.onclick = function(ev){ ev.stopPropagation(); wheelSelect('kill', -1); };
    var ab = $('hoAbstain');
    if (ab) ab.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'vote', -1, null); };  /* 弃票=弃票（2026-10-22 用户定，推翻自投先例；引擎 -1 口径现成） */
    var cf = $('hoConfirm');
    if (cf) cf.onclick = function(ev){ ev.stopPropagation(); submit(seatNo, 'confirm', null, null); };
    var bk = $('hoBack');
    if (bk) bk.onclick = function(ev){ ev.stopPropagation(); closeHandoff(); };
    return;
  }

  /* —— 确认刀口（多狼局）：我**已点过刀**（wolf_points 里有我）⇒ 引擎挂着
     confirm。此时必须先问确认、不能再画 kill 屏 —— 否则「点刀→（引擎
     又给 [kill,confirm]）→ 再点刀」死循环，确认永远到不了
     （2026-10-06 走查 #10 实锤，引擎侧复现排除服务端问题）。
     想改刀走「改刀重选」临时切回 kill 屏，覆盖 wolf_points 后再确认。 */
  /* 🔴 2026-10-06（晚）走查实锤：非选人屏（确认/放弃/发言）**必须先摘掉**号码按钮的
     可点状态 —— 上一屏残留的 data-act 按钮会让「点数字」分支吃到幽灵点击，
     真人则会看到可点的高亮却点不出任何效果。 */
  _stripNumberButtons();
  var myPoint = v.wolf_points ? v.wolf_points[seatNo - 1] : null;
  if (acts.indexOf('confirm') >= 0 && (acts.indexOf('kill') < 0 || myPoint != null)){
    WHEEL.act = 'confirm';
    $('bigTitle').textContent = '狼人';
    $('bigIcon').textContent = '🔒';
    $('bigWho').textContent = '就这个，确定？';
    $('hoTip').textContent = '点圆心锁定今晚的刀口';
    $('wheelExtra').innerHTML = '<button id="hoRekill">改刀重选</button>'
      + '';
    $('wheelExtra').classList.add('on');
    renderWheelBase();
    var rk = $('hoRekill');
    if (rk) rk.onclick = function(ev){ ev.stopPropagation(); renderWheelAct(seatNo, v, ['kill']); };
    var bk2 = $('hoBack');
    if (bk2) bk2.onclick = function(ev){ ev.stopPropagation(); closeHandoff(); };
    return;
  }

  /* —— 只剩「不做」（猎人不开枪 / 女巫不用药）：点圆心 = 放弃 —— */
  if (acts.indexOf('pass') >= 0){
    WHEEL.act = 'pass';
    var declineText = (acts.indexOf('shoot') >= 0) ? '不开枪？' : '本轮不用药？';
    $('bigTitle').textContent = acts.indexOf('shoot') >= 0 ? '猎人' : '女巫';
    $('bigIcon').textContent = '🙋';
    $('bigWho').textContent = declineText;
    $('hoTip').textContent = '点圆心确认放弃';
    $('wheelExtra').innerHTML = '';
    $('wheelExtra').classList.add('on');
    var bk3 = $('hoBack');
    if (bk3) bk3.onclick = function(ev){ ev.stopPropagation(); closeHandoff(); };
    return;
  }

  /* —— 发言兜底（大圆识别失败时手动选的「打字」）：透明操作条 —— */
  if (acts.indexOf('speak') >= 0){
    WHEEL.act = 'speak';
    $('bigTitle').textContent = seatNo + ' 号';
    $('bigIcon').textContent = '🎤';
    $('bigWho').textContent = '轮到你发言';
    $('hoTip').textContent = '也可以直接打字';
    $('handoff').classList.add('speak');
    $('hoSpeak').classList.add('on');
    $('hoSpeak').innerHTML =
      '<input id="fbText" placeholder="直接打字">'
      + '<div class="row"><button id="fbGo">说这句</button><button id="fbPass">过</button></div>';
    $('fbGo').onclick = function(){
      submit(seatNo, 'speak', null, ($('fbText') || {}).value || '');
    };
    $('fbPass').onclick = function(){ submit(seatNo, 'speak', null, '（过）'); };
    return;
  }

  /* 空/未知动作集：不留死屏，直接交回 —— tick 下一拍会拉到正确的屏。
     🔴 2026-10-06（晚）：先清 WHEEL.mode 再清屏 —— 顺序反了的话，清屏中途
     一旦抛错，页面就停在 mode='act' 但什么按钮都没有的死屏（走查实锤）。 */
  WHEEL.mode = null;
  try { closeHandoff(); } catch (e) { /* 屏清不干净也比死锁强 */ }
  setTimeout(tick, 300);

  } catch (e) {
    /* 🔴 渲染总保险：任何一步抛错都不许留下半渲染死屏 —— 交回自愈 */
    try { WHEEL.mode = null; closeHandoff(); } catch (e2) {}
    setTimeout(tick, 300);
  }
}
/* 点了一个号码（或空刀）：选中、大圆给出确认提示 */
/* 号码上的"选中"标记 + 圆心下的文案（选中态最终由服务端状态回灌）。 */
function paintWheelSel(act, target){
  var bs = $('hoActs').querySelectorAll('.wnum');
  for (var i = 0; i < bs.length; i++) bs[i].classList.remove('sel');
  if (target >= 1){
    var el = $('hoActs').querySelector('.wnum[data-no="' + target + '"]');
    if (el) el.classList.add('sel');
  }
  var verb = WHEEL_LABEL[act] || act;
  if (target == null){
    $('bigWho').textContent = '点一个号码就是决定';
    foot('点号码即生效；再点同一个号码取消');
    return;
  }
  var t = (target === -1) ? '空刀' : (verb + ' ' + target + ' 号');
  $('bigWho').textContent = t + '（已生效）';
  foot(t + '：已生效；再点同一个号码取消，点别的号码切过去');
}

/* 🔴 2026-10-07（定稿交互，用户实机点名）：
   定稿逐字 —— "轮盘按某人号码**即视为守护，不需要再点击其他按钮**，再次点击原选中号码
   可取消，点击其他号码也可切过去"（狼刀/预言家/女巫毒药/投票同构）。
   所以这里**点号码就直接提交**，不再需要点圆心确认；再点同一个号码 = 提交 `clear`
   （引擎把这一步撤回：狼撤点+撤确认、守卫取消守护、女巫把药还回来…见 engine.night_clear）。 */
function wheelSelect(act, target){
  if (WHEEL.mode !== 'act' || WHEEL.act !== act) return;
  if (BUSY){ $('hoTip').textContent = '上一步还在处理，稍候再点…'; return; }
  var seatNo = WHEEL.seatNo;
  if (WHEEL.pick === target){
    WHEEL.pick = null;
    paintWheelSel(act, null);
    submit(seatNo, 'clear', null, null);
    return;
  }
  WHEEL.pick = target;
  paintWheelSel(act, target);
  if (act === 'kill'){
    /* 定稿："人狼在轮盘按某人号码即视为刀" —— 点了就是我的刀。
       多狼局里"确认"是引擎定案的必要条件，所以这里替玩家补上（单狼局引擎会早返回）。 */
    var _p = submit(seatNo, 'kill', target, null);
    if (_p && _p.then) _p.then(function(){ submit(seatNo, 'confirm', null, null); });
    return;
  }
  submit(seatNo, act, target, null);
}

/* 点圆心（轮盘确认键） */
function wheelConfirm(){
  /* 🔴 2026-10-07 实锤修复：BUSY 期间大圆（div）不被 setBusy disable，
     点击会走到这里再被 submit 的 BUSY 拦截**静默吞掉** —— 表现就是
     「点圆心没反应」（走查 rc=2 渲染稳定 + 无 400 + kill 恒未提交）。
     现在给出明确反馈，绝不静默丢。 */
  if (BUSY){
    $('hoTip').textContent = '上一步还在处理，稍候再点…';
    return;
  }
  var seatNo = WHEEL.seatNo;
  if (WHEEL.act === 'confirm'){
    submit(seatNo, 'confirm', null, null);
    return;
  }
  if (WHEEL.act === 'pass'){
    submit(seatNo, 'pass', null, null);
    return;
  }
  /* 狼队共屏：批量提交（0 个候选 = 全体空刀，也合法） */
  if (WHEEL.act === 'kill' && WHEEL.allies && WHEEL.allies.length > 1){
    wheelConfirmWolfPack();
    return;
  }
  if (WHEEL.pick != null){
    /* 🔴 2026-10-07（定稿交互）：点号码那一刻**已经提交过**了 —— 圆心不再承担确认键，
       再点一次只会重复提交（毒药那种"一次性"动作还会报"已经用过了"）。 */
    $('hoTip').textContent = '已经记下了；再点同一个号码可以取消';
    return;
  }
  $('hoTip').textContent = '点一个号码就是决定（不需要再点圆心）';
}

/* 交棒屏的「认领」：点圆心后拉本人视图、进入操作屏 */
function wheelTake(){
  var need = { seat: WHEEL.seatNo - 1 };
  (async function(){
    try {
      MYVIEW = await api('/api/game/' + GID + '/seat/' + need.seat);
      /* ⚠️ 这里必须传 **1-based**：submit() 里会再减 1 转回 0-based。 */
      renderWheelAct(need.seat + 1, MYVIEW);
    } catch (e) {
      $('hoTip').textContent = '取不到视图：' + e.message;
      /* 认领步骤已删：这里没有「再点一次圆心」的入口，1 秒后自动重试 */
      setTimeout(function(){ if (WHEEL.mode === 'act' && !WHEEL.view) wheelTake(); }, 1000);
    }
  })();
}

/* 「打字」兜底：透明输入条（识别失败时的手动出口，2026-10-05 取消卡片） */
function openSpeakFallback(seatNo){
  HANDOFF = seatNo;                    /* 1-based，口径同 openHandoff */
  HANDOFF_SIG = seatNo + ':speak';
  MYVIEW = null; PICK = null;
  renderWheelBase();
  renderWheelAct(seatNo, { can_act: ['speak'] });
}

/* 旧 renderHandoff 卡片的后半段（发言兜底/不用药/狼盘/按钮接线）已由
   renderWheelAct + wheelSelect/wheelConfirm/wheelTake 取代（2026-10-05 大改版）。
   「打字」兜底改走 #hoSpeak 透明操作条（fbText/fbGo/fbPass）。 */

/* ---------------------------------------------------------- 手机现场诊断 */
/* 为什么要有这一块：手机上**没有控制台**，用户能提供的唯一信息是"没反应"，
   而"没反应"背后至少有五种不同的原因：不是安全上下文 / 浏览器没给麦克风接口 /
   麦克风约束被拒 / 音频没解锁（一点声音都没有）/ 识别通道不通。
   把它们逐条显示在屏幕上，比隔着屏幕来回猜快得多 ——
   这也是"手机上情况不一样"的唯一有效解法：**先让差异可见**。 */
var DIAG_ASR_ERR = '', DIAG_NET = [], DIAG_CANCEL = 0;
function diagRefresh(){
  var el = $('diagPanel'); if (!el) return;
  var d = (Rec.diag ? Rec.diag() : {});
  var yn = function(v){ return v ? '<span class="ok">是</span>' : '<span class="bad">否</span>'; };
  var rows = [
    ['地址', String(location.href).slice(0, 90)],
    ['安全上下文', yn(window.isSecureContext) + '（麦克风只在 https / localhost 下可用）'],
    ['麦克风接口', yn(!!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia))],
    ['麦克风链', yn(d.mic) + '（已取得流 + 采集节点）'
      // 说清楚这个"否"什么时候不是故障：用内置识别时麦克风归它自己管，
      // 我们**故意**不调 getUserMedia（两条链会抢麦克风）。不写这句，
      // 面板就会自相矛盾（识别明明能用，却显示"麦克风链：否"）。
      + ((d.pick && d.pick.engine === 'webspeech' && !d.mic)
          ? '<span class="ok">（内置识别自带麦克风，这项为否是正常的）</span>' : '')],
    // ⚠️ 这两项**必须以括号包住**：元素以 `[` 开头、又用 `+` 续行时，
    //    JS 会把它解析成"数组 + 字符串"这个**独立表达式**，后面的逗号就是语法错
    //    （2026-09-19 由 node --check 当场抓到 —— 不查语法就是一个白屏页面）。
    ['AudioContext', (d.ctx ? (d.ctxState || '?') : '未创建')
      + '（手机上必须是 running，否则 AI 没声音）'],
    /* 识别引擎（2026-09-30）：欠费事故之后加的。必须一眼看出"现在会用哪一层、
       为什么用它"—— 那次用户看到的是"麦没法用 + 诊断显示正常"，就是因为
       引擎选择与失败原因**根本没显示出来**。 */
    ['识别引擎', '<b>' + (Rec.engineLabel ? Rec.engineLabel((d.pick || {}).engine) : '?')
      + '</b>' + (d.enginePin ? '<span class="bad">（已手动钉住）</span>' : '（自动选）')
      + ' ｜ ' + ((d.pick || {}).why || '')
      + (d.speechSupported ? '' : '<span class="bad">（此浏览器没有内置识别）</span>')
      + '<br>最近一次实际用的是：' + (d.engineUsedLabel || '（还没用过）')],
    ['识别会话', ((d.sessions || 0) + ' 条')
      + ((d.sessions || 0) > 1 ? '<span class="bad">（超过 1 条 = 按键被双触发过）</span>' : '')],
    ['采样率', (d.sampleRate || 0) + ' Hz'],
    ['手势被抢', DIAG_CANCEL + ' 次'
      + (DIAG_CANCEL ? '<span class="bad">（= 按住说话被浏览器判成滚动手势，会"秒松开"）</span>' : '（0 = 正常）')],
    ['最近错误', d.lastErr || DIAG_ASR_ERR || '（无）'],
    /* 🔴 2026-10-05「AI 不发声」：把最近一次播报的结果直接摆出来 ——
       到底是"还没播过"（null）、合成失败（ok:false + 原因）、还是播放被拦，
       一眼可见，不再靠猜。 */
    ['最近播报', LAST_TTS
      ? ((LAST_TTS.ok ? '<span class="ok">ok</span>' : '<span class="bad">失败</span>')
         + '｜' + (LAST_TTS.how || '?')
         + (LAST_TTS.bytes ? ('｜' + LAST_TTS.bytes + 'B') : '')
         + (LAST_TTS.ms ? ('｜' + Math.round(LAST_TTS.ms) + 'ms') : '')
         + (LAST_TTS.http ? ('｜HTTP ' + LAST_TTS.http) : '')
         + (LAST_TTS.err ? ('｜<span class="bad">' + LAST_TTS.err + '</span>') : '')
         + '｜' + (LAST_TTS.text || ''))
      : '（还没有播过任何一句）'],
    ['手机访问', DIAG_NET.length ? DIAG_NET.join(' ｜ ') : '（查不到局域网地址）'],
    ['屏幕', window.innerWidth + 'x' + window.innerHeight + '｜'
      + String(navigator.userAgent || '').slice(0, 72)]
  ];
  el.innerHTML = rows.map(function(r){
    return '<div><span class="k">' + r[0] + '</span>：' + r[1] + '</div>';
  }).join('') +
    '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">' +
    '<button id="diagRetry" class="bigbtn" style="padding:8px 12px;font-size:13px">重试麦克风</button>' +
    '<button id="diagSpeak" class="bigbtn" style="padding:8px 12px;font-size:13px">试一句 AI 语音</button>' +
    '<button id="diagEngine" class="bigbtn" style="padding:8px 12px;font-size:13px">切换识别引擎</button>' +
    '<button id="diagClose" class="bigbtn" style="padding:8px 12px;font-size:13px">收起</button>' +
    '</div>';
  $('diagClose').onclick = function(){
    var p = $('diagPanel'); if (p) p.classList.remove('on');
  };
  /* 手动钉住某一层（自动 → 浏览器内置 → 本地 → 云端 → 自动）。
     为什么要给这个按钮：自动回落是"尽力而为"，而现场排查时人需要**钉死**一层来看
     它到底行不行（否则一失败就自动换走，永远看不清是哪层的问题）。 */
  $('diagEngine').onclick = function(){
    var order = ['', 'webspeech', 'local', 'cloud'];
    var cur = Rec.pinnedEngine || '';
    var i = order.indexOf(cur); if (i < 0) i = 0;
    var next = order[(i + 1) % order.length];
    if (Rec.pinEngine) Rec.pinEngine(next);
    DIAG_ASR_ERR = '识别引擎已设为：' + (next ? Rec.engineLabel(next) : '自动（浏览器内置 → 本地 → 云端）');
    diagRefresh();
  };
  $('diagRetry').onclick = async function(){
    DIAG_ASR_ERR = '（正在重试…）';
    try { await Rec.ensure(); DIAG_ASR_ERR = '（成功：麦克风链已建立）'; }
    catch (e) { DIAG_ASR_ERR = '失败：' + Rec.describeError(e); }
    diagRefresh();
  };
  $('diagSpeak').onclick = async function(){
    DIAG_ASR_ERR = '（正在播报…）';
    diagRefresh();
    try {
      var sp = await Rec.speak('/ws/tts?text=' + encodeURIComponent('诊断：这是一句测试语音'));
      await sp.promise;
      DIAG_ASR_ERR = sp.error() ? ('TTS 出错：' + sp.error()) : '（TTS 正常，刚才听到声音了吗？）';
    } catch (e) { DIAG_ASR_ERR = 'TTS 失败：' + e; }
    diagRefresh();
  };
}
(function diagInit(){
  /* 两个入口开同一个浮层：牌桌上的 🩺、封面上的小字按钮。
     封面那个是必须的 —— "连一局都开不起来"时人进不了牌桌页，
     而那正是最需要诊断的时刻（用户 2026-09-19 的反馈）。 */
  var toggle = function(){
    var el = $('diagPanel'); if (!el) return;
    el.classList.toggle('on');
    if (el.classList.contains('on')) diagRefresh();
  };
  var b1 = $('btnDiag'), b0 = $('btnDiag0');
  if (b1) b1.onclick = toggle;
  if (b0) b0.onclick = toggle;
  if (String(location.search).indexOf('diag=1') >= 0 && $('diagPanel')){
    $('diagPanel').classList.add('on');
    diagRefresh();
  }
  fetch('/api/net').then(function(r){ return r.json(); }).then(function(j){
    DIAG_NET = (j && j.phones) || [];
    if ($('diagPanel') && $('diagPanel').classList.contains('on')) diagRefresh();
  }).catch(function(){});
})();

/* 手机上必须**在用户手势里**解锁音频（自动播放策略）：
   不在手势里创建/恢复的 AudioContext 会一直停在 suspended ——
   表现就是"AI 说话一点声音都没有"。桌面 Chrome 宽松，所以这个坑只在手机上现形。
   第一次触摸/点击时做一次就够（放一个静音片，iOS 上只 resume 有时不够）。 */
(function unlockOnFirstGesture(){
  var done = false;
  var once = function(){
    if (done) return;
    done = true;
    try { Rec.unlock(); } catch (e) { /* 老缓存版本的 rec.js 没有 unlock，忽略 */ }
    document.removeEventListener('pointerdown', once, true);
    document.removeEventListener('touchstart', once, true);
    setTimeout(function(){
      if ($('diagPanel') && $('diagPanel').classList.contains('on')) diagRefresh();
    }, 300);
  };
  document.addEventListener('pointerdown', once, true);
  document.addEventListener('touchstart', once, true);
})();

/* 「过」兜底按钮（spkHolder 里的 #hoSay 麦克风钮由下方一次性接线）。
   旧 hoGoWiring 已删：fbGo/fbPass 在 renderWheelAct 里现接现用；
   不用药/确认/交回都改成了「点圆心」。 */

function closeHandoff(){
  HANDOFF = null; HANDOFF_SIG = null; MYVIEW = null; PICK = null;
  var _nd = $('nightDial'); if (_nd) _nd.classList.remove('on');
  var _dh = $('dialHead'); if (_dh) _dh.classList.remove('on');
  var _bc = $('bigCircle'); if (_bc) _bc.style.visibility = '';
  $('handoff').classList.remove('on', 'speak');
  $('hoSpeak').classList.remove('on');
  $('wheelBtns').classList.remove('on');
  $('wheelExtra').classList.remove('on');
  $('wheelExtra').innerHTML = '';
  WHEEL = { mode: null, seatNo: null, view: null, act: null,
            pick: null, sub: null, angle: WHEEL.angle };
  var bc = $('bigCircle');
  if (bc) bc.classList.remove('acting');
  wheelModeIdle();
  foot('把手机放回桌子中央');
}

/* 无人操作：大圆回到法官态，轮盘只画公开刻度 */
function wheelModeIdle(){
  WHEEL.mode = null; WHEEL.act = null; WHEEL.pick = null;
  var bc = $('bigCircle');
  if (bc) bc.classList.remove('acting');
  $('bigTitle').textContent = '法官';
  $('bigIcon').textContent = '⚖️';
  $('bigWho').textContent = '';
  renderWheelBase();
}

/* 交接层里的提示。**必须写在浮层内部** ——
   之前错误只写到 #foot，而浮层是 inset:0 全屏盖着的，
   于是失败信息玩家完全看不到，界面又停在原地 → 看起来就是"反复卡在同一步"。 */
function handoffMsg(msg, bad){
  var e = $('hoErr');
  if (e){ e.textContent = msg; e.style.color = bad ? '#FFB4B4' : 'rgba(255,255,255,.7)'; }
  foot(msg);
}

/* 把「只有你知道」的结论摊给本人看（目前用于预言家验人）。
   验人这一下是预言家整轮唯一拿到的信息 —— 不给结果等于白验，
   而且玩家会以为是游戏卡住了。 */
function showPrivateResult(seatNo, v, title){
  /* 验人结果直接写在大圆里（私有信息不上日志区），点圆心交回 */
  var inv = v.investigations || {};
  var ks = Object.keys(inv);
  var lines = ks.map(function(k){
    return (Number(k) + 1) + ' 号是 ' + (inv[k] ? '狼人' : '好人');
  });
  WHEEL = { mode: 'act', seatNo: seatNo, view: v, act: 'done',
            pick: null, sub: null, angle: WHEEL.angle };
  renderWheelBase();
  wheelTags(v);
  var bc = $('bigCircle');
  if (bc) bc.classList.add('acting');
  $('handoff').classList.add('on');
  $('handoff').classList.remove('speak');
  $('hoSpeak').classList.remove('on');
  $('wheelBtns').classList.remove('on');
  $('wheelExtra').classList.remove('on');
  $('wheelExtra').innerHTML = '';
  $('bigTitle').textContent = '预言家';
  $('bigIcon').textContent = '🔮';
  $('bigWho').textContent = lines.join('；') || '（没有记录）';
  $('hoTip').textContent = title + '（只有你知道，点圆心交回）';
}

/* ================================================================
   预言家结果的大标识（2026-10-07 第二期，用户定稿）
   「他是这个」这句播报念出来的**同时**，圆盘上显示大大的好（绿）/坏（红），
   3 秒后消失（与紧接着的「预言家请闭眼」同时）。
   数据来源 = 人类预言家自己那次验人的结果（SEER_LAST，submit('investigate') 落）。
   AI 预言家 / 没验人 ⇒ SEER_LAST 为空 ⇒ **不显示** —— 这是 L2 私有信息，
   只有本人（那晚手机在他手里）看得到，不能给全桌看。
   ================================================================ */
var SEER_LAST = null;          /* {seatNo, targetNo, wolf} */
var SEER_BIG_TIMER = null;
function resetSeerBig(){
  /* 🔴 2026-10-12：拨号盘上的 👍/👎 也要还原成号码 —— 由「预言家请闭眼」
     播报（bug3 的 seer_close 挂钩）触发，与用户"闭眼就大拇指消失"对齐。 */
  if (SEER_LAST && SEER_LAST.targetNo != null){
    var _d = $('nightDial');
    var _db = _d && _d.querySelector('button[data-no="' + SEER_LAST.targetNo + '"]');
    if (_db) _db.textContent = String(SEER_LAST.targetNo);
  }
  SEER_LAST = null;
  if (SEER_BIG_TIMER){ clearTimeout(SEER_BIG_TIMER); SEER_BIG_TIMER = null; }
  var el = $('seerBig');
  if (el){ el.className = ''; el.textContent = ''; }
}
function showSeerBig(){
  var el = $('seerBig');
  if (!el || !SEER_LAST) return;
  el.textContent = SEER_LAST.wolf ? '坏' : '好';
  el.className = 'on ' + (SEER_LAST.wolf ? 'bad' : 'good');
  if (SEER_BIG_TIMER) clearTimeout(SEER_BIG_TIMER);
  SEER_BIG_TIMER = setTimeout(function(){
    el.className = ''; el.textContent = ''; SEER_BIG_TIMER = null;
  }, 3000);
}

/* 提交期间把浮层里所有按钮按死。
   为什么必须有：真模型一句要 2~3 秒（慢的模型十几秒），
   玩家会忍不住再点一次「说这句」—— 那一次发言就被提交了两遍。
   2026-09-15 用真模型走查时抓到（mock 下请求瞬间返回，露不出来）。 */
function setBusy(on){
  BUSY = on;
  /* 轮盘元素分散在四块：兜底条 / 号码刻度 / ✓✕ / 补充入口 —— 全部要按死 */
  var sels = ['#handoff button', '#hoActs button', '#wheelBtns button', '#wheelExtra button'];
  for (var k = 0; k < sels.length; k++){
    var bs = document.querySelectorAll(sels[k]);
    for (var i = 0; i < bs.length; i++) bs[i].disabled = on;
  }
}

async function submit(seatNo, action, target, text){
  if (BUSY) return;
  setBusy(true);
  var body = { seat: seatNo - 1, action: action };
  /* ⚠️ target 的 1 基 → 0 基换算。**-1 必须原样通过**（2026-10-05 修）：
     -1 是「空刀」这类特殊值（data-target="-1"），减 1 就成了 -2 ⇒
     服务端 _require_alive(-2) 抛「目标不在场」—— 用户点空刀必报错。
     数据是 1 基座位号（0 = 1号）与 -1 哨兵混用，换算必须给哨兵开洞。 */
  if (target != null) body.target = (target === -1) ? -1 : target - 1;
  if (text != null) body.text = text;
  try {
    /* 引擎要在这一请求里把 AI 的回合跑完（真模型可能要几十秒），
       所以先把话说明白 —— 否则界面看着像卡死了。 */
    handoffMsg('AI 正在想…（真模型一句可能要几十秒）');
    var d = await api('/api/game/' + GID + '/act', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    PUBLIC = d.public;
    paintSeats(); renderLog(); speakQueue();

    if (action === 'investigate'){
      var v2 = await api('/api/game/' + GID + '/seat/' + (seatNo - 1));
      /* 2026-10-07 第二期：记下这次验人的结果 —— 播报「他是这个」时，
         圆盘上要出大大的好/坏标识（3 秒后消失）。只有本人看得到。 */
      var _inv = v2.investigations || {};
      var _ti = (target != null && target !== -1) ? (target - 1) : null;
      if (_ti != null && (_ti in _inv)){
        SEER_LAST = { seatNo: seatNo, targetNo: target, wolf: !!_inv[_ti] };
      }
      /* 🔴 2026-10-12（用户定稿）：验人结果直接标在**拨号盘的号码按钮**上 ——
         号码被 👍（好人）/ 👎（狼人）替代，直到「预言家请闭眼」才还原数字
         （resetSeerBig 挂在闭眼播报上）。大圆已藏 ⇒ 旧的结果屏看不见，
         这就是"验了没反应"的原因。结果态（act='done'）不被重建冲掉。 */
      if (SEER_LAST && $('nightDial').classList.contains('on')){
        var _b2 = $('nightDial').querySelector('button[data-no="' + target + '"]');
        if (_b2){
          _b2.textContent = SEER_LAST.wolf ? '👎' : '👍';
          _b2.onclick = null;
          WHEEL.act = 'done';
          return;
        }
      }
      showPrivateResult(seatNo, v2, '你验的结果');
      return;
    }
    closeHandoff();
    setTimeout(tick, 300);
  } catch (e) {
    handoffMsg('这一步没被接受：' + e.message, true);
    /* ★ 把手上这屏换成「现在真正能做的动作」。
       失败常常不是网络问题，而是**这一屏已经过期**：服务端已经走到下一步
       （例如你还在看投票屏，它已经进入夜里）⇒ 再点还是 400。
       重新取一次视图就能对上，否则玩家会一直点、一直失败。 */
    try {
      MYVIEW = await api('/api/game/' + GID + '/seat/' + seatNo);
      PICK = null;
      renderWheelAct(seatNo, MYVIEW);
    } catch (e2) { /* 取不到就交给下一轮 tick 自愈 */ }
  } finally {
    setBusy(false);
  }
}

/* ---------------------------------------------------------- 按住说话 */

var SAY_REC = false, SAY_STREAM = null, SAY_PENDING = null;
async function startSay(){
  /* 防重入：一次按压**只许开一条会话**。手机上一次按压会先后触发
     touchstart 与浏览器补发的 mousedown（见下面绑定处的注释），
     两层防线都要有：调用方去重 + 这里的不变量。 */
  /* 🔴 开新会话前先释放残留（2026-10-02 实局）：上一条流式会话若没断干净
     （静音自续 / 收尾被打断），它的 finalText 会攒着外放 TTS 和别人的话，
     等这条新会话一停就被当成**本回合**的发言提交 —— 串座就是这么发生的。
     closeSpeakerMode 已在回合切换时清过一次；这里是第二道闸。 */
  if (SAY_STREAM && !SAY_REC){
    try { SAY_STREAM.stop(); } catch (e) { /* ignore */ }
    SAY_STREAM = null;
  }
  if (SAY_REC || SAY_PENDING) return;
  if (SPEAKING){ $('hoStatus').textContent = 'AI 正在说话，等它说完'; return; }
  /* ⚠️ 先置位再 await：`Rec.stream()` 里要等麦克风授权，那一段可能好几秒；
     这期间用户完全可能已经松手了。不先置位的话 stopSay 会以为"没按过"直接 return，
     而授权一到又变成"松手结束"的状态 —— 界面就卡在录音中（实测就是这么卡住的）。
     所以：先 SAY_REC = true，回来之后再检查"是不是已经松手了"。 */
  SAY_REC = true;
  if ($('hoSay')) $('hoSay').classList.add('on');   /* 录音态：按钮变红（无文字） */
  if (SPK_MODE){ $('bigCircle').classList.add('rec'); syncSpkTip(); } /* 大圆：录音态画在大圆+提示行 */
  $('hoStatus').textContent = '正在打开麦克风…';
  if (SPK_MODE) syncSpkTip();
  /* 2026-09-30：改成走**统一入口** `Rec.listen()` —— 它按优先级挑一层（在 rec.js 里）：
       ① 浏览器内置识别（免费、边说边出字）
       ② 本地 whisper（免费、离线、不外发；说完等几秒）
       ③ 云端流式（原设计、质量最好；要 key 且账号得有钱）
     某层硬失败会留痕并**自动换下一层**；具体用了哪一层会写在状态行上（不许静默降级）。
     ⚠️ 为什么不能再"只认流式"（这里原先就是那个 bug）：
        阿里云欠费时 `/ws/asr` 是**干净地**失败的 —— 它只回一条 type:error，
        `Rec.stream()` 本身不抛异常 ⇒ 下面那个 try/catch 永远等不到 ⇒
        为它准备的老路回落代码**写了却从未被触发过**，用户看到的就是"点了没反应"。 */
  var _pick = Rec.pickEngine ? Rec.pickEngine() : { engine: 'cloud', why: '' };
  SAY_PENDING = Rec.listen({
    onPartial: function(t){ if ($('hoText')) $('hoText').value = t; },
    onFinal: function(t){ if ($('hoText')) $('hoText').value = t; },
    /* 异步错误**不再**把 SAY_STREAM 置空：那会让 stopSay 掉进"录完再传"分支，
       而那条分支的麦克风根本没开 ⇒ 只会报"没有录到"（假象）。
       现在让它照常收尾：拿不到字就由 stopSay 把**通道的真实错误**显示出来。 */
    onError: function(e){ DIAG_ASR_ERR = '识别通道：' + Rec.describeError(e); }
  });
  try {
    SAY_STREAM = await SAY_PENDING;
    SAY_PENDING = null;
    if (!SAY_REC){                       /* 打开麦克风期间就松手了 ⇒ 立刻收掉，不留悬空状态 */
      try { SAY_STREAM.stop(); } catch (e) { /* ignore */ }
      SAY_STREAM = null;
      return;
    }
    $('hoStatus').textContent = '识别中…（' + (SAY_STREAM.label || _pick.why || '') + '）';
    return;
  } catch (e) {
    SAY_PENDING = null;
    SAY_STREAM = null;
  }
  if (!SAY_REC) return;                  /* 同上：期间松手了就别再启一条老路 */
  try { await Rec.start(); } catch (e){ $('hoStatus').textContent = '用不了麦克风：' + Rec.describeError(e); return; }
  if ($('hoSay')) $('hoSay').classList.add('on');   /* 录音态：按钮变红（无文字） */
  $('hoStatus').textContent = '录音中…';
}
async function stopSay(seatNo){
  if (!SAY_REC) return;
  SAY_REC = false;
  if ($('hoSay')) $('hoSay').classList.remove('on');   /* 退出录音态（按钮上无文字） */
  var _bc = $('bigCircle'); if (_bc) _bc.classList.remove('rec');
  if (SPK_MODE) syncSpkTip();

  /* 流式那条路可能还在"打开麦克风 / 建连"中间（用户按得很快）：先等它把句柄交出来。
     ⚠️ 不等的话会掉进"录完再传"分支，而那一刻麦克风其实还没开始录 ⇒ 只报「没有录到」，
     用户看到的现象就是"语音录不进去"。（实测报障：按一下就松手时必现。） */
  if (!SAY_STREAM && SAY_PENDING){
    $('hoStatus').textContent = '收尾中…';
    try { SAY_STREAM = await SAY_PENDING; } catch (e) { SAY_STREAM = null; }
    SAY_PENDING = null;
    if (!SAY_STREAM){ $('hoStatus').textContent = '没听清，直接打字后点「说这句」'; return; }
  }

  /* ① 主路：句柄在手。**三条引擎共用这一处收尾**（它们的 stop() 返回同一形状：
       {text, seconds[, tooShort]}），所以这里不关心用的是哪一层 —— 只在状态行写明。 */
  if (SAY_STREAM){
    var s = SAY_STREAM;
    SAY_STREAM = null;
    $('hoStatus').textContent = '收尾中…' + (s.label ? '（' + s.label + '）' : '');
    var rs = { text: '', seconds: 0 };
    try { rs = await s.stop(); } catch (e) { /* 收尾失败也把已经拿到的字用上 */ }
    var ts = (rs.text || '').trim();
    /* 🔴 2026-10-12（用户手机实机：关麦后"一直停在这个人"）：**关麦必推进** ——
       有字交字；没字/太短/通道错一律自动「（过）」，轮到下一个人。
       通道的错仍然写进 hoStatus 留痕（诊断面板看细节），但不再把人留在原地。 */
    if (!ts){
      var _le = String((Rec.diag() || {}).lastErr || '');
      if (/Access denied|good standing|1007|Arrearage/i.test(_le)){
        $('hoStatus').textContent = '云端拒绝访问（账号欠费 / 未开通 / 额度用尽）—— 已自动「过」，麦是好的，去查账单';
      } else if (_le){
        $('hoStatus').textContent = '识别通道出错（已自动「过」）：' + _le.slice(0, 60);
      } else {
        $('hoStatus').textContent = '没听清 —— 已自动「过」';
      }
      submit(seatNo, 'speak', null, '（过）');
      return;
    }
    $('hoStatus').textContent = '说了：' + ts;
    submit(seatNo, 'speak', null, ts);
    return;
  }

  /* ② 回落：录完再传（老路，一直在） */
  var r = await Rec.stop();
  /* 🔴 2026-10-12：关麦必推进 —— 没录到/没听清也自动「过」（同主路） */
  if (!r){ $('hoStatus').textContent = '没有录到 —— 已自动「过」'; submit(seatNo, 'speak', null, '（过）'); return; }
  if (r.tooShort){ $('hoStatus').textContent = '太短了 —— 已自动「过」'; submit(seatNo, 'speak', null, '（过）'); return; }
  $('hoStatus').textContent = '识别中…';
  try {
    /* 只做转写（服务端不会顺带跑 LLM/TTS），识别完直接作为本轮发言提交 —— 松手即发言 */
    var j = await Rec.transcribe(r.blob, { asrOnly: true });
    var t = j.text || '';
    if ($('hoText')) $('hoText').value = t;
    if (!t.trim()){ $('hoStatus').textContent = '没听清 —— 已自动「过」'; submit(seatNo, 'speak', null, '（过）'); return; }
    submit(seatNo, 'speak', null, t);
  } catch (e) {
    $('hoStatus').textContent = '识别失败 —— 已自动「过」：' + e.message;
    submit(seatNo, 'speak', null, '（过）');
  }
}

/* ---------------------------------------------------------- 大圆发言（接线） */

/* 大圆发言模式的收尾包装：stopSay 识别成功会自己 submit（tick 随后关掉大圆模式）；
   失败（没听清 / 通道错 / 太短）**不再弹兜底卡片**（2026-10-02 用户报：
   半黑框又回来了、还卡老半天）—— 留在大圆模式，提示行上写清下一步：
   再点一下大圆重说，或点「过」/「打字」。 */
async function stopSaySpk(){
  var seatNo = SPK_MODE;
  await stopSay(seatNo);
  syncSpkTip();
}

$('bigCircle').onclick = function(){
  if (WHEEL_DRAGGED){ WHEEL_DRAGGED = false; return; }   /* 拖盘收尾，不是点击 */
  if (SPK_MODE){
    if (SAY_REC || SAY_PENDING) stopSaySpk();  /* 再点一下 = 结束 */
    else startSay();                           /* 第一下 = 开始 */
    return;
  }
  /* 轮盘的圆心 = 唯一确认键（2026-10-05 用户定） */
  if (WHEEL.mode === 'handoff'){ wheelTake(); return; }
  if (WHEEL.mode === 'act'){
    if (WHEEL.act === 'done'){ closeHandoff(); setTimeout(tick, 400); return; }
    wheelConfirm();
  }
};

/* 轮盘旋转：在大圆上按住拖动（线下围坐，各自把号码转到自己面前）。
   移动超过阈值就算拖动 ⇒ 收尾那次 click 不会触发确认。 */
(function wheelDrag(){
  var bc = $('bigCircle');
  if (!bc) return;
  var drag = null;
  function ang(e){
    var r = bc.getBoundingClientRect();
    return Math.atan2(e.clientY - (r.top + r.height / 2),
                      e.clientX - (r.left + r.width / 2)) * 180 / Math.PI;
  }
  bc.addEventListener('pointerdown', function(e){
    if (SPK_MODE || WHEEL.mode === null) return;   /* 发言/无人操作不转盘 */
    drag = { a0: ang(e), th0: WHEEL.angle, moved: 0,
             x0: e.clientX, y0: e.clientY };
    WHEEL_DRAGGED = false;
  });
  bc.addEventListener('pointermove', function(e){
    if (!drag) return;
    var dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (Math.sqrt(dx * dx + dy * dy) > 8) drag.moved = 1;
    if (drag.moved){
      WHEEL.angle = drag.th0 + (ang(e) - drag.a0);
      wheelApply();
    }
  });
  function end(){
    if (drag && drag.moved) WHEEL_DRAGGED = true;
    drag = null;
  }
  bc.addEventListener('pointerup', end);
  bc.addEventListener('pointercancel', end);
  bc.addEventListener('click', function(e){
    if (WHEEL_DRAGGED){ e.stopPropagation(); e.preventDefault(); WHEEL_DRAGGED = false; }
  }, true);
})();

/* spkHolder 的麦克风钮（发言模式里视觉隐藏，点击走大圆本体；
   走查与诊断会直接点它）：点一下开始、再点一下结束 —— 绑定一次。 */
(function(){
  var sayBtn = $('hoSay');
  if (sayBtn){
    sayBtn.onclick = function(){
      var seatNo = SPK_MODE || WHEEL.seatNo;
      if (!seatNo) return;
      if (SAY_REC || SAY_PENDING) stopSay(seatNo);
      else startSay();
    };
    sayBtn.oncontextmenu = function(e){ e.preventDefault(); return false; };
  }
})();
