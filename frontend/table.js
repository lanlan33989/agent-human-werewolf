/* 牌桌渲染 / 椭圆排座 / 座位特效 —— 从 demo/index.html 原样提取（2026-09-15）。
 *
 * 为什么要单独一个文件：新页面 h5/play.html 要照 demo 的界面形式来，
 * 但它必须能被真引擎驱动。样式与排座算法原样复用，才能保证两边长得一模一样；
 * 手抄一遍必然会漂移。
 * 抽出来的时候没有改动 demo/index.html 一个字符。
 *
 * 依赖：页面里要有一个全局 S（见 play.html），以及 #tablewrap / #table。
 */

function $(id){ return document.getElementById(id); }
function seat(no){ return S.seats[no - 1]; }

function renderTable(){
  var t = $('table');
  /* 清掉旧座位与动效，保留中心法官 */
  var olds = t.querySelectorAll('.seat,.wound');
  for (var i = 0; i < olds.length; i++) olds[i].remove();

  S.seats.forEach(function(s){
    var d = document.createElement('div');
    d.className = 'seat' + (s.kind === 'ai' ? ' ai' : '');
    d.id = 'seat-' + s.no;
    d.innerHTML =
      '<div class="inner">' +
        '<div class="f a"><div class="n">' + s.no + '</div>' +
          '<div class="k">' + (s.kind === 'ai' ? 'AI' : '人') + '</div></div>' +
        '<div class="f b" id="sfb-' + s.no + '">好人</div>' +
      '</div>';
    d.onclick = function(){ onSeatTap(s.no); };
    t.appendChild(d);
  });
}

/**
 * 排布牌桌：座位沿**椭圆**摆放，圆圈撑到最大，且**相邻圆圈之间的间距一致**。
 *
 * 为什么用椭圆：竖屏里可用区域是「横窄竖长」。按正圆排，直径被宽度卡死，
 * 剩下大量竖直空间白白浪费。椭圆周长比同宽的正圆长得多，圈就能显著变大。
 * （需求原话：围成的形状是椭圆形都没事。）
 *
 * ── 间距一致怎么保证 ─────────────────────────────────────────
 * 判据是「相邻圆圈中心距（弦长）相等」—— 圆圈直径相同时，
 * 间隙 = 弦长 − d，所以弦长齐 ⇒ 间隙齐。
 *
 * ⚠️ 这里连踩过两个坑，都不是随手一改能对的：
 *
 * 坑 1：按**角度**均分（p = −90° + i·360°/n）。椭圆上等角度 ≠ 等间距，
 *       实测弦长极差达 49%，5 人局相邻间隙相差 60px，肉眼非常明显。
 *
 * 坑 2：改成**等弧长**后好一些（平均极差 11%），但仍不齐 ——
 *       弧长相等时，曲率大的地方弦长短。7 人局间隙仍相差 30px，
 *       9 人局甚至出现负间隙（圆圈重叠）。
 *
 * 正解：直接求**等弦长**解。椭圆上严格等弦长的内接 n 边形是存在的
 *       （含奇数 n，用打靶法可验证），取到之后弦长极差是 0.000%。
 *       做法：固定起始点在正上方，给定目标弦长 L 就一步步"走"过去
 *       （每步取前向最近的、距离正好为 L 的位置），L 合适时走完 n 步
 *       正好闭合 ⇒ 对 L 求根即可（打靶法）。
 *
 * ⚠️ 求根时不能用「整段二分」：椭圆上从某点出发的距离在 (prev, prev+π) 内
 *    **先增后减**（从短轴端点出发时，走到对径点只有 2a，比中途的局部峰值还小），
 *    直接二分会把"有解"误判成"无解"（4 人局就是这么失败过）。
 *    所以先粗扫定位、再二分精修。
 *
 * 怎么定尺寸：座位直径 d 与椭圆半径互相制约 —— d 越大、能用的半径越小，
 * 所以要迭代：猜 d → 反推可用的 rx/ry → 求该椭圆上的等弦长解 L
 * → 把 d 调到 min(上限, 0.94L) → 再迭代几轮。
 */

/* 兜底用：把椭圆 (A·cos p, B·sin p) 按「等弧长」切成 n 份的参数角。
   只在打靶法失败（可用区过小等极端情况）时使用。 */
function equalArcAngles(A, B, n){
  var M = 1440;                                  /* 采样密度：每段 0.25° */
  var step = 2 * Math.PI / M;
  var cum = [0];
  var px = A * Math.cos(-Math.PI / 2), py = B * Math.sin(-Math.PI / 2);
  for (var i = 1; i <= M; i++){
    var p = -Math.PI / 2 + i * step;
    var x = A * Math.cos(p), y = B * Math.sin(p);
    var dx = x - px, dy = y - py;
    cum.push(cum[i - 1] + Math.sqrt(dx * dx + dy * dy));
    px = x; py = y;
  }
  var total = cum[M];
  var out = [];
  for (var k = 0; k < n; k++){
    var target = total * k / n;
    var lo = 0, hi = M;
    while (lo < hi){
      var mid = (lo + hi) >> 1;
      if (cum[mid] < target) lo = mid + 1; else hi = mid;
    }
    var frac = 0;
    if (lo > 0){
      var span = cum[lo] - cum[lo - 1];
      frac = span > 1e-9 ? (target - cum[lo - 1]) / span : 0;
    }
    out.push(-Math.PI / 2 + (lo - 1 + frac) * step);
  }
  return out;
}

/* 椭圆上两点的距离 */
function chordOf(A, B, p, q){
  var dx = A * Math.cos(q) - A * Math.cos(p);
  var dy = B * Math.sin(q) - B * Math.sin(p);
  return Math.sqrt(dx * dx + dy * dy);
}

/* 从 prev 出发，找「前向最近的」使距离正好等于 L 的参数位置。
   注意不能整段二分 —— 见上面「坑」的说明，距离在区间内先增后减。 */
function stepByChord(A, B, prev, L){
  var cx = A * Math.cos(prev), cy = B * Math.sin(prev);
  /* 扫描只负责把根「圈」在一个小区间里（每 2.5° 一个采样点），
     精度完全由后面的二分保证 —— 采样点太密纯属浪费 */
  var M = 72, span = Math.PI;
  var last = prev + 1e-9, lastD = 0;
  for (var i = 1; i <= M; i++){
    var p = prev + span * i / M;
    var dx = A * Math.cos(p) - cx, dy = B * Math.sin(p) - cy;
    var d = Math.sqrt(dx * dx + dy * dy);
    if (lastD < L && d >= L){
      var lo = last, hi = p;
      for (var k = 0; k < 30; k++){
        var mid = (lo + hi) / 2;
        var mx = A * Math.cos(mid) - cx, my = B * Math.sin(mid) - cy;
        if (Math.sqrt(mx * mx + my * my) < L) lo = mid; else hi = mid;
      }
      return (lo + hi) / 2;
    }
    last = p; lastD = d;
  }
  return null;                                   /* 这一段里达不到 L */
}

/* 在椭圆 (A,B) 上求等弦长内接 n 边形：返回 {L, angles}；失败返回 null。
   取「最小的有序根」—— 它对应"绕一圈均匀铺开"的那个解，
   更大的根是绕多圈的伪解（已被有序性检查挡掉一部分）。 */
function equalChord(A, B, n, hint){
  if (!(A > 2 && B > 2) || n < 3) return null;

  function walk(L){
    var ps = [-Math.PI / 2];
    for (var i = 1; i < n; i++){
      var nx = stepByChord(A, B, ps[i - 1], L);
      if (nx === null) return null;
      ps.push(nx);
    }
    /* 必须严格按参数顺序、且总跨度不超过一圈 —— 否则是伪解 */
    for (var j = 1; j < n; j++) if (!(ps[j] > ps[j - 1] + 1e-6)) return null;
    if (ps[n - 1] - ps[0] > 2 * Math.PI - 1e-6) return null;
    return { ps: ps, g: chordOf(A, B, ps[n - 1], ps[0]) - L };
  }

  /* 周长近似（Ramanujan）—— 既用来定扫描区间，也用来估初值 */
  var P = Math.PI * (3 * (A + B) - Math.sqrt((3 * A + B) * (A + 3 * B)));

  /* ── 快速通道 ──
     根的位置基本由「周长 / n」决定（实测各 n 下都在它的 0.86~0.98 倍之间），
     外层迭代时形状又只微变，所以有一个相当好的初值可用。
     用割线法在初值附近精修，比每次重跑「粗扫 + 二分」省 5 倍以上。
     收敛阈值必须收紧到 0.02 —— 它直接等于"弦长最大误差"（单位像素），
     放到 0.5 时弦长极差会退化到 0.3%。 */
  if (!(hint > 0.5)) hint = P / n * 0.95;
  var wB = walk(hint);
  if (wB){
    var step = Math.max(0.4, hint * 0.02);
    var wA = walk(hint - step);
    if (wA){
      var La = hint - step, gA = wA.g, Lb = hint, gB = wB.g;
      var cap = 2 * Math.min(A, B);
      for (var s = 0; s < 16; s++){
        if (gB === gA) break;
        var Ln = Lb - gB * (Lb - La) / (gB - gA);
        if (!(Ln > 0.5) || Ln > cap) break;
        var wN = walk(Ln);
        if (!wN) break;
        La = Lb; gA = gB; Lb = Ln; gB = wN.g;
        if (Math.abs(gB) < 1e-9) break;
      }
      if (Math.abs(gB) < 0.02){
        var wF = walk(Lb);
        if (wF) return { L: Lb, angles: wF.ps };
      }
    }
  }

  /* ── 完整通道 ── 初值不可用时才走：粗扫定位变号区间 → 二分精修 */
  var lo = Math.max(1, P / n * 0.30), hi = P / n * 1.05;
  var N = 24, a = null, b = null, ga = null;
  for (var i = 0; i <= N; i++){
    var L = lo + (hi - lo) * i / N;
    var w = walk(L);
    if (!w){ ga = null; continue; }
    if (ga !== null && ga * w.g < 0){
      a = lo + (hi - lo) * (i - 1) / N; b = L; break;
    }
    ga = w.g;
  }
  if (a === null) return null;

  var wa = walk(a);
  if (!wa) return null;
  for (var k = 0; k < 48; k++){
    var mid = (a + b) / 2;
    var wm = walk(mid);
    if (!wm) break;
    if (wa.g * wm.g <= 0) b = mid; else { a = mid; wa = wm; }
  }
  var Lf = (a + b) / 2;
  var wf = walk(Lf);
  return wf ? { L: Lf, angles: wf.ps } : null;
}

/* 从一组角度里取「最大 |cos| / |sin|」—— 决定椭圆能推到多靠边而不出框 */
function shapeOfAngles(angles){
  var mc = 0, ms = 0;
  angles.forEach(function(a){
    mc = Math.max(mc, Math.abs(Math.cos(a)));
    ms = Math.max(ms, Math.abs(Math.sin(a)));
  });
  return { mc: mc < 0.01 ? 1 : mc, ms: ms < 0.01 ? 1 : ms };
}

function chordsBetween(A, B, angles){
  var out = [];
  for (var i = 0; i < angles.length; i++){
    out.push(chordOf(A, B, angles[i], angles[(i + 1) % angles.length]));
  }
  return out;
}

function layout(){
  var wrap = $('tablewrap');
  var W = wrap.clientWidth, H = wrap.clientHeight;
  var n = S.seats.length;
  if (!(W > 40 && H > 40 && n > 0)) return;

  var MAXD = 110;                                   /* 圆圈直径上限（审美） */
  var wolves = Math.max(1, S.wolfNos.length);

  var angles = [];
  for (var i = 0; i < n; i++) angles.push((-90 + i * 360 / n) * Math.PI / 180);
  var sh = shapeOfAngles(angles);
  var d = 64, rx = 0, ry = 0;
  var hint = 0;                    /* 上轮的弦长，给求根当初值（省 5 倍时间） */
  var unit = 0;                    /* 单位弦长比：L = unit × rx —— 等弦长解对椭圆尺度等变 */

  var prevRatio = 0;
  for (var pass = 0; pass < 8; pass++){
    var base = Math.max(3, d * 0.045);
    var reserve = base * wolves;
    rx = (W / 2 - d / 2 - reserve) / sh.mc;
    ry = (H / 2 - d / 2 - reserve) / sh.ms;
    if (rx < 6 || ry < 6) break;

    var sol = equalChord(rx, ry, n, hint);
    if (sol){
      angles = sol.angles;
      hint = sol.L;
      unit = sol.L / rx;             /* 尺度等变 ⇒ 换个大椭圆，弦长按同一比例放大 */
    } else {
      /* 极端可用区下打靶失败：退回等弧长，按最小弦长保守取 d */
      angles = equalArcAngles(rx, ry, n);
      hint = 0;
      unit = Math.min.apply(null, chordsBetween(rx, ry, angles)) / rx;
    }
    sh = shapeOfAngles(angles);

    /* 内层只调 d。形状锁定时弦长是 d 的解析函数，不必再求根 ——
       否则每轮外层都要重跑一次打靶，单次 layout 会到 100ms 量级（实测过）。 */
    for (var k = 0; k < 16; k++){
      var bk = Math.max(3, d * 0.045);
      var rk = bk * wolves;
      var rxk = (W / 2 - d / 2 - rk) / sh.mc;
      if (rxk < 6) break;
      var next = Math.min(MAXD, unit * rxk * 0.94);
      if (Math.abs(next - d) < 0.2){ d = next; break; }
      d = d + 0.7 * (next - d);                     /* 阻尼，避免来回抖 */
    }

    /* ⚠️ 收敛判据必须是「椭圆的**形状比例**」，不能只看 d 是否稳定：
       等弦长的角度是在某个比例上解出来的，比例一变，解就不再等弦长。
       只看 d 会提前收工 —— 实测那样会留下 0.3~0.5% 的弦长极差。 */
    var bp = Math.max(3, d * 0.045);
    var rp = bp * wolves;
    var ratio = ((H / 2 - d / 2 - rp) / sh.ms) / ((W / 2 - d / 2 - rp) / sh.mc);
    if (pass > 0 && Math.abs(ratio - prevRatio) < 2e-4) break;
    prevRatio = ratio;
  }
  d = Math.max(30, d);

  /* 定稿：用最终的 d 与角度重算半径，保证座位 + 刀口红圈都不出框 */
  var baseF = Math.max(3, d * 0.045);
  var reserveF = baseF * wolves;
  rx = Math.max(4, W / 2 - d / 2 - reserveF) / sh.mc;
  ry = Math.max(4, H / 2 - d / 2 - reserveF) / sh.ms;

  S.seatPx = Math.round(d);
  S.woundBase = baseF;
  S.chords = chordsBetween(rx, ry, angles);          /* 供自检与测试 */
  S.gaps = S.chords.map(function(c){ return c - d; });
  S.rx = rx; S.ry = ry;
  document.documentElement.style.setProperty('--seat', Math.round(d) + 'px');
  document.documentElement.style.setProperty('--judge', Math.round(d * 1.05) + 'px');

  S.seats.forEach(function(s, i){
    s.px = rx * Math.cos(angles[i]);                 /* 相对中心的像素偏移 */
    s.py = ry * Math.sin(angles[i]);
    s.x = 50 + s.px / W * 100;                       /* 换算成百分比定位 */
    s.y = 50 + s.py / H * 100;
    var el = $('seat-' + s.no);
    if (el){ el.style.left = s.x + '%'; el.style.top = s.y + '%'; }
  });
}

function seatEl(no){ return $('seat-' + no); }
function addCls(no, c){ var e = seatEl(no); if (e) e.classList.add(c); }
function delCls(no, c){ var e = seatEl(no); if (e) e.classList.remove(c); }

function markSeer(on){
  if (on) addCls(S.seerNo, 'seer'); else delCls(S.seerNo, 'seer');
}
function markWolves(on){
  S.wolfNos.forEach(function(no){ if (on) addCls(no, 'wolf'); else delCls(no, 'wolf'); });
}
function showWound(no, count){
  var e = seatEl(no);
  if (!e) return;
  var base = S.woundBase || 4;      /* 与 layout() 里给红圈预留的余量一致 */
  for (var i = 0; i < count; i++){
    var w = document.createElement('div');
    w.className = 'wound';
    var pad = Math.round(base * (i + 1));
    w.style.left = '-' + pad + 'px';
    w.style.top = '-' + pad + 'px';
    w.style.right = '-' + pad + 'px';
    w.style.bottom = '-' + pad + 'px';
    e.appendChild(w);
  }
}
function killSeat(no){
  addCls(no, 'dead');
  var s = seat(no);
  if (s) s.alive = false;
}
function radar(no, on){
  if (on) addCls(no, 'speaking'); else delCls(no, 'speaking');
}
function acting(no, on){
  if (on) addCls(no, 'acting'); else delCls(no, 'acting');
}
function flipSeatTo(no, text){
  var e = seatEl(no);
  if (!e) return;
  if (text){
    $('sfb-' + no).textContent = text;
    e.classList.add('flip', 'chosen');
  } else {
    e.classList.remove('flip', 'chosen');
  }
}

/* ============================================================
   10. 预言家查验（已去掉指示手指图案：直接点座位即可）
   ============================================================ */
