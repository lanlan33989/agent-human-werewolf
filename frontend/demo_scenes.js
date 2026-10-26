/* ============================================================
   前端演示场景（2026-10-27）：play.html?demo=<场景名> 用。
   数据结构照实机 /public 抓包（mock/fixtures/public.json）改字段，
   所以渲染管线走的是**真实页面代码** —— 画廊所见即产品所得。

   加新场景：在 DEMO_SCENES 里加一项即可；场景数据尽量从真实
   对局的 /public 响应拷贝再改，不要凭空造字段。
   ============================================================ */

/* 实机抓包的底版（6 人局夜晚，见 mock/fixtures/public.json） */
function _basePub() {
  return {
    gid: "DEMO",
    board: "6 人 · 2 狼 1 巫 1 预 2 民",
    board_key: "dyn6",
    phase: "night",
    round: 1,
    winner: null,
    night_node: "wolf",
    night_plan: [
      { id: "open", say: "第1夜，天黑请闭眼", win: 0, gap: 3, close: null, close_gap: 3 },
      { id: "wolf", say: "狼人请睁眼，请选择今晚你们要击杀的人", win: 20, gap: 0, close: "狼人请闭眼", close_gap: 3 },
      { id: "witch_heal", say: "女巫请睁眼，今晚死的是他，你要救吗？", win: 15, gap: 0, close: null, close_gap: 3 },
      { id: "witch_poison", say: "你要使用毒药吗？", win: 15, gap: 0, close: "女巫请闭眼", close_gap: 3 },
      { id: "seer", say: "预言家请睁眼，请选择今晚你要查验的人", win: 15, gap: 0, close: null, close_gap: 3 },
      { id: "dawn", say: "天亮了，请所有玩家睁眼", win: 0, gap: 0, close: null, close_gap: 3 }
    ],
    now: { kind: "none", seat: null, node: "wolf", until: null, need: null,
           why: "这一节没有人类要点", started: false },
    public_log: ["游戏开始，6 人局，每人 1 条命。", "第 1 夜，天黑请闭眼。"],
    speech: [],
    timeline: [
      { t: "sys", s: "游戏开始，6 人局，每人 1 条命。", voice: "Neil", ts: 1793034399.25 },
      { t: "sys", s: "第 1 夜，天黑请闭眼。", voice: "Neil", ts: 1793034399.26 }
    ],
    players: [
      { seat: 0, display: "1 号", lives: 1, alive: true,  kind: "human" },
      { seat: 1, display: "2 号", lives: 1, alive: true,  kind: "ai" },
      { seat: 2, display: "3 号", lives: 1, alive: true,  kind: "ai" },
      { seat: 3, display: "4 号", lives: 1, alive: true,  kind: "ai" },
      { seat: 4, display: "5 号", lives: 1, alive: true,  kind: "ai" },
      { seat: 5, display: "6 号", lives: 1, alive: true,  kind: "ai" }
    ],
    speaking_order: [],
    current_speaker: null,
    voted_seats: [],
    votes: {},
    pending: "working",
    level: "娱乐",
    speech_mode: "rotation",
    suspicion: { "0": 0, "1": 0, "2": 0, "3": 0, "4": 0, "5": 0 }
  };
}

/* 各座位身份（身份牌场景 + demo seat 接口用）：0-based 座位 → 角色 */
var DEMO_ROLES = {
  0: { role: "wolf",  role_label: "狼人",   team_mates: [4] },
  1: { role: "seer",  role_label: "预言家", team_mates: [] },
  2: { role: "witch", role_label: "女巫",   team_mates: [] },
  3: { role: "guard", role_label: "守卫",   team_mates: [] },
  4: { role: "wolf",  role_label: "狼人",   team_mates: [0] },
  5: { role: "villager", role_label: "村民", team_mates: [] }
};

function _pub(mod) {
  var p = _basePub();
  if (mod) mod(p);
  return p;
}

var DEMO_SCENES = {

  /* 身份牌：逐座位翻牌（demo 特殊分支，不进桌） */
  cards: {
    kind: "cards",
    title: "身份牌（逐座位翻牌）",
    note: "翻面样式按座位轮换：狼人/预言家/女巫/守卫/村民。看完 4 张会顺势进桌。",
    roles: DEMO_ROLES
  },

  /* 夜 · 狼刀：轮盘 + 队友/刀口标记 */
  night_wolf: {
    title: "夜 · 狼人刀人",
    note: "先「请把手机交给 1 号」认领 → 轮盘选目标；🐺=队友，🔪=队友已点过。",
    public: _pub(function (p) {
      p.night_node = "wolf";
      p.pending = "human_night:wolf";
      p.timeline.push({ t: "sys", s: "狼人请睁眼，请选择今晚你们要击杀的人", voice: "Neil", delay: 0, stage: "wolf", node: "wolf", at: 1793034402 });
      p.wolf_points = { "0": 2 };
      p.wolf_confirmed = [];
      var need = { seat: 0, node: "wolf", actions: ["kill"], seconds_left: 18 };
      p.now = { kind: "wheel", seat: 0, node: "wolf", need: need,
                until: Date.now() / 1000 + 18, until_left: 18,
                started: true, why: "狼人：点号码 = 提议刀口" };
      p._ps = { seats: [need] };
    })
  },

  /* 夜 · 女巫（✓✕ + 被刀标记） */
  night_witch: {
    title: "夜 · 女巫（解药 ✓✕）",
    note: "被刀者标 ☠️；空刀夜解药按钮置灰。",
    public: _pub(function (p) {
      p.night_node = "witch_heal";
      p.pending = "human_night:witch_heal";
      p.night_victim = 4;
      p.timeline.push({ t: "sys", s: "女巫请睁眼，今晚死的是他，你要救吗？", voice: "Neil", delay: 0, stage: "witch_heal", node: "witch_heal", at: 1793034405 });
      var need = { seat: 0, node: "witch_heal", actions: ["heal", "pass"], seconds_left: 15 };
      p.now = { kind: "wheel", seat: 0, node: "witch_heal", need: need,
                until: Date.now() / 1000 + 15, until_left: 15,
                started: true, why: "5 号被刀，是否使用解药？" };
      p._ps = { seats: [need] };
    })
  },

  /* 夜 · 预言家 */
  night_seer: {
    title: "夜 · 预言家（验人）",
    note: "点号码 = 决定验谁；验完出好人/坏人大字。",
    public: _pub(function (p) {
      p.night_node = "seer";
      p.pending = "human_night:seer";
      p.timeline.push({ t: "sys", s: "预言家请睁眼，请选择今晚你要查验的人", voice: "Neil", delay: 0, stage: "seer", node: "seer", at: 1793034408 });
      var need = { seat: 0, node: "seer", actions: ["investigate"], seconds_left: 15 };
      p.now = { kind: "wheel", seat: 0, node: "seer", need: need,
                until: Date.now() / 1000 + 15, until_left: 15,
                started: true, why: "点一个号码 = 决定今晚验谁" };
      p._ps = { seats: [need] };
    })
  },

  /* 白天 · 轮到真人发言 */
  day_say: {
    title: "白天 · 轮到真人发言",
    note: "大圆发言模式：点一下开始录音（phase=discuss + actions=[speak]）。",
    public: _pub(function (p) {
      p.phase = "discuss"; p.round = 1; p.night_node = null; p.night_plan = [];
      p.pending = "human_say";
      p.current_speaker = 0;
      p.speaking_order = [0, 1, 2];
      p.speech = [{ seat: 0, spoken: false }];
      p.timeline.push({ t: "sys", s: "天亮了。昨夜 5 号出局。", voice: "Neil", ts: 1793034420 });
      p.timeline.push({ t: "sys", s: "从 1 号开始发言。", voice: "Neil", ts: 1793034422 });
      p.timeline.push({ t: "say", s: "我觉得 3 号昨天的话前后对不上，他大概率有问题。", seat: 1, voice: "Ethan", spoken: false });
      var need = { seat: 0, node: null, actions: ["speak"], seconds_left: 30 };
      p.now = { kind: "say", seat: 0, node: null, need: need,
                until: Date.now() / 1000 + 30, until_left: 30,
                started: true, why: "轮到你发言（点大圆开始/结束）" };
      p._ps = { seats: [need] };
    })
  },

  /* 投票进行中 */
  vote: {
    title: "投票（点号码 → 圆心确认）",
    note: ".sel = 已选中待确认；投票允许投自己。",
    public: _pub(function (p) {
      p.phase = "vote";
      p.pending = "human_vote";
      p.votes = { "1": 2, "2": 0 };
      p.voted_seats = [1, 2];
      p.timeline.push({ t: "sys", s: "发言结束，开始投票。", voice: "Neil", ts: 1793034460 });
      var need = { seat: 0, node: "vote", actions: ["vote", "pass"], seconds_left: 15 };
      p.now = { kind: "wheel", seat: 0, node: "vote", need: need,
                until: Date.now() / 1000 + 15, until_left: 15,
                started: true, why: "点号码选中 → 点圆心确认（可投自己）" };
      p._ps = { seats: [need] };
    })
  },

  /* 票型大字 */
  vote_result: {
    title: "投票结果（票型大字）",
    note: "#voteBig 只摆票型，不显示 AI 文本。",
    public: _pub(function (p) {
      p.phase = "day"; p.night_node = null; p.night_plan = [];
      p.votes = { "0": 2, "1": 2, "3": 0, "4": 2, "5": 4 };
      p.voted_seats = [0, 1, 3, 4, 5];
      p.timeline.push({ t: "sys", s: "投票结果：3 号 3 票，1 号 2 票。", voice: "Neil", ts: 1793034470 });
      p.timeline.push({ t: "sys", s: "3 号出局。", voice: "Neil", ts: 1793034472 });
      p.players[2].alive = false; p.players[2].lives = 0;
      p.now = { kind: "none", seat: null, node: null, need: null, started: false };
    })
  },

  /* 终局 */
  ended: {
    title: "终局（胜负 + 战报入口）",
    note: "winner 非 null 即结束；局末出口（战报/导出）只在此时可取。",
    public: _pub(function (p) {
      p.phase = "ENDED"; p.round = 3; p.night_node = null; p.night_plan = [];
      p.winner = "好人胜利";
      p.pending = "ended";
      p.timeline.push({ t: "sys", s: "最后一名狼人出局。", voice: "Neil", ts: 1793034700 });
      p.timeline.push({ t: "sys", s: "游戏结束：好人胜利！", voice: "Neil", ts: 1793034702 });
      p.now = { kind: "none", seat: null, node: null, need: null, started: false };
    })
  }
};
