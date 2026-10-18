/* 沙箱硬闸：在 vm 里用假引擎真跑载荷，正向都配负向对照，判据 id 从表里现取。
   用法：node dryrun.js [--table table.json] [--payload payload.js] */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const NL = String.fromCharCode(10);

function arg(name, dft) {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : dft;
}
const HERE = __dirname;
const TABLE_PATH = arg('--table', path.join(HERE, 'table.json'));
const PAYLOAD_PATH = arg('--payload', path.join(HERE, 'payload.js'));
if (!fs.existsSync(TABLE_PATH)) { console.log('RED 找不到表：' + TABLE_PATH); process.exit(1); }
if (!fs.existsSync(PAYLOAD_PATH)) { console.log('RED 找不到载荷：' + PAYLOAD_PATH); process.exit(1); }

const TABLE = JSON.parse(fs.readFileSync(TABLE_PATH, 'utf-8'));
const VB = '/* ==== SKINRENDER-BEGIN @V@ ==== */', VE = '/* ==== SKINRENDER-END @V@ ==== */';
const RAW = fs.readFileSync(PAYLOAD_PATH, 'utf-8')
  .replace(VB, '').replace(VE, '').split('@V@').join('dry')
  .replace('__SKIN_TABLE__', JSON.stringify(TABLE));

const FAKE_DIR = '/writ' + 'able/';
const BRANDS = ['cz' + 'n', 'CZ' + 'NSKIN', 'v' + '14', 'LE' + 'GION', 'Chaos' + 'Zero'].join('|');
const ck = [], lines = [];
function T(n, c, i) { ck.push([n, !!c]); lines.push((c ? 'OK   ' : 'RED  ') + n + (i === undefined ? '' : '  → ' + String(i).slice(0, 150))); }
const MAP = TABLE.map || {}, ROWS = (TABLE.rows || {}).char_base || {};
const IDS = Object.keys(ROWS);
const pick = (t, pre) => Object.keys(MAP).filter(k => MAP[k].t === t && (!pre || k.indexOf(pre) === 0))[0];
const L_KEY = pick('L', 'model/'), A_KEY = pick('A', 'model/'), T_KEY = pick('T'), S_KEY = pick('S', 'model_data/');
const P_KEY = Object.keys(MAP).filter(k => k.indexOf('face/portrait/') === 0 && 'AB'.indexOf(MAP[k].t) >= 0)[0];
const want = (key, ext) => key.split('/').slice(0, -1).join('/') + '/' + MAP[key].v + '.' + ext;

function scenario(cfgText, opts) {
  opts = opts || {};
  const LOGS = [], INNER = [], CREATED = [], APPLIED = [], ABS = {}, CALLS = [];
  const BASE = {};
  IDS.forEach(id => { BASE[String(id)] = { id: Number(id), model: Number(id), portrait_ani: 'none', standing_portrait_ani: 'none' }; });
  const OV = {};
  const FU = {
    getWritablePath: () => '/writable/',   /* 假引擎的沙箱路径，故意不带盘符 */
    writeStringToFile: (s, p) => { ABS[String(p)] = String(s); },
    getStringFromFile: (p) => {
      const k = String(p);
      if (k.indexOf('skin_probe.txt') >= 0) return ABS[k] || '';
      if (k.indexOf('skin.cfg') >= 0) return cfgText || '';
      return '{}';
    },
    isFileExist: (p) => { INNER.push(String(p)); return opts.missing ? String(p).indexOf('_01') < 0 : true; },
  };
  const gr = {
    clientOverrides: {},
    get: function (t, k) {
      if (t === 'card') return opts.cardRow === undefined ? { spine_model: 'unique_1' } : opts.cardRow;
      if (t !== 'char_base') return null;
      const b = BASE[String(k)]; if (!b) return null;
      const o = (OV.char_base || {})[String(k)];
      return o ? Object.assign({}, b, o, { id: b.id }) : Object.assign({}, b);
    },
    applyClientOverrides: function (a) { APPLIED.push(a); OV.char_base = (a.tables || {}).char_base || {}; },
  };
  const ctx = {
    _speedLog: (m) => LOGS.push(String(m)),
    cc: { FileUtils: { getInstance: () => FU } },
    sp: {
      SkeletonAnimation: {
        create: (a) => {
          CREATED.push(String(a));
          return {
            setAnimation(t, n) { return ['idle', 'touch', 'move', 'stop'].indexOf(n) >= 0 ? {} : null; },
            addAnimation(t, n) { return n === 'idle' ? {} : null; },
            setSkin(n) { return n === 'normal'; },
            setName() { }, setToSetupPose() { }, update() { }, setPosition() { }, setScale() { },
          };
        },
      },
    },
    __gr: gr,
    console, Date, Math, JSON, Object, Array, String, Number, Boolean, RegExp, Error, Function,
    isNaN, parseInt, parseFloat, encodeURIComponent, decodeURIComponent, TypeError, SyntaxError,
  };
  vm.createContext(ctx);
  vm.runInContext(RAW, ctx);
  vm.runInContext('GameResDB = __gr', ctx);          // 真机顺序：先跑载荷，DB 之后才被赋值
  if (opts.noCardUtil) return finish();
  ctx.__cu = {
    getCardAnimation: function (id) {
      CALLS.push(String(id));
      return opts.cardOwned === undefined ? 0 : opts.cardOwned;   // 0 = 这角色的卡面动画没买
    }
  };
  vm.runInContext('CharacterUtil = __cu', ctx);      // 赋值即触发载荷装的 trap
  return finish();

  function finish() {
    const api = ctx.skinRender;
    if (api && P_KEY) ctx.sp.SkeletonAnimation.create(P_KEY + '.scsp', 'x', 1);
    return { ctx, LOGS, INNER, CREATED, APPLIED, api, FU, CALLS };
  }
}

const A = scenario('');
const LOG = () => A.LOGS.join(NL);
const READY = (A.LOGS.filter(x => /READY/.test(x))[0] || '');
const ALIVE = (A.LOGS.filter(x => /ALIVE/.test(x))[0] || '');

T('D1 缺省放行层是 ABTSL', /放行层=ABTSL/.test(READY), READY.slice(0, 120));
T('D2 日志目录取运行期可写目录',
  ALIVE.indexOf('目录=' + FAKE_DIR) >= 0);
T('D3 表真的进了载荷',
  new RegExp('路径表=' + Object.keys(MAP).length).test(ALIVE), Object.keys(MAP).length);
T('D4 行覆盖走官方通道且回读全对',
  /ROW 走官方通道 applyClientOverrides/.test(LOG()) &&
  (() => { const m = LOG().match(/ROW 回读\(抽样 (\d+)\) 对=(\d+) 不对=(\d+)/);
    return !!m && Number(m[1]) >= 1 && m[2] === m[1] && m[3] === '0'; })(),
  A.LOGS.filter(x => /ROW 回读/.test(x))[0]);
T('D5 官方通道被调用', A.APPLIED.length >= 1, 'APPLIED=' + A.APPLIED.length);
const R6 = (() => {
  if (!A.APPLIED.length) return '假 DB 没收到调用';
  const tb = A.APPLIED[A.APPLIED.length - 1].tables.char_base;
  const ids = Object.keys(tb);
  if (ids.length !== IDS.length) return '行数 ' + ids.length + ' 与表的 ' + IDS.length + ' 不符';
  for (const id of ids) {
    const fam = MAP['face/portrait/' + id];
    if (!fam) return '表里没有这个立绘族 ' + id;
    if (tb[id].portrait_ani !== 'face/portrait/' + fam.v) return id + ' 的 portrait_ani 不对';
    if (tb[id].standing_portrait_ani !== tb[id].portrait_ani) return id + ' 两个字段不一致';
  }
  return true;
})();
T('D6 每行都指向该角色自己的皮肤族', R6 === true,
  R6 === true ? JSON.stringify(A.APPLIED[A.APPLIED.length - 1].tables.char_base[IDS[0]]) : R6);

if (L_KEY) {
  A.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'a', 1);
  T('D7 L 层换成皮肤骨架',
    A.CREATED[A.CREATED.length - 1] === want(L_KEY, 'scsp'), A.CREATED.slice(-2).join(' | '));
  A.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'x', 1).setAnimation(0, 'zz_absent_anim', true);
  T('D8 放不出的动画如实记 MISS', A.api.made().made.animMiss >= 1,
    JSON.stringify(A.api.made().anim));
  A.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'y', 1);
  T('D9 缺省不自愈回落',
    A.api.made().made.heal === 0 && /自愈回落=关/.test(READY), A.CREATED.slice(-2).join(' | '));
  const H = scenario('heal' + NL);
  H.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'a', 1);
  H.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'b', 1).setAnimation(0, 'zz_absent_anim', true);
  H.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'c', 1);
  T('D10 heal 一开安全网就回来',
    H.api.made().made.heal === 1 && H.CREATED[H.CREATED.length - 1] === L_KEY + '.scsp',
    H.CREATED.join(' | '));
  const B = scenario('tier ABTS' + NL);
  B.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'a', 1);
  T('D11 tier 去掉 L 就不换',
    B.CREATED[B.CREATED.length - 1] === L_KEY + '.scsp' &&
    Object.keys(B.api.made().refuse).some(k => /层级 L 未放行/.test(k)),
    JSON.stringify(B.api.made().refuse).slice(0, 140));
  const DP = scenario('drop ' + L_KEY.split('/')[0] + '/' + NL);
  DP.ctx.sp.SkeletonAnimation.create(L_KEY + '.scsp', 'a', 1);
  T('D12 drop 能收回整目录',
    DP.CREATED[DP.CREATED.length - 1] === L_KEY + '.scsp' &&
    Object.keys(DP.api.made().refuse).some(k => /cfg drop/.test(k)),
    JSON.stringify(DP.api.made().refuse).slice(0, 140));
} else {
  T('表里必须有 L 层', false, '缺 L 层，6 条判据无从谈起');
}

if (A_KEY && S_KEY) {
  A.ctx.sp.SkeletonAnimation.create(A_KEY + '.scsp', 'x', 1);
  T('D13 A 层整族换',
    A.CREATED[A.CREATED.length - 1] === want(A_KEY, 'scsp'), A.CREATED.slice(-2).join(' | '));
  const base = S_KEY.split('/')[1], varn = MAP[S_KEY].v;
  A.FU.isFileExist('model_data/' + base + '.' + MAP[S_KEY].e[0]);
  T('D14 model_data 兄弟表随骨架同源换',
    A.INNER[A.INNER.length - 1] === 'model_data/' + varn + '.' + MAP[S_KEY].e[0],
    A.INNER.slice(-1)[0]);
} else {
  T('表里必须有 A 层与 S 层', false,
    'A=' + String(A_KEY) + ' S=' + String(S_KEY));
}

if (T_KEY) {
  A.FU.isFileExist(T_KEY + '.sct');
  T('D15 T 层纯贴图族只换贴图', A.INNER[A.INNER.length - 1] === want(T_KEY, 'sct'), A.INNER.slice(-1)[0]);
  A.FU.isFileExist(T_KEY + '.scsp');
  T('D16 表里没给的扩展名不换',
    A.INNER[A.INNER.length - 1] === T_KEY + '.scsp', A.INNER.slice(-1)[0]);
} else {
  T('表里必须有 T 层', false, '没有 T 层条目');
}

const MG = scenario('', { missing: true });
if (P_KEY) {
  MG.FU.isFileExist(P_KEY + '.scsp');
  T('D17 变体不在盘上就保持基名',
    MG.INNER[MG.INNER.length - 1] === P_KEY + '.scsp' &&
    Object.keys(MG.api.made().refuse).some(k => /变体不在盘上/.test(k)),
    JSON.stringify(MG.api.made().refuse).slice(0, 140));
} else {
  T('表里必须有立绘族', false, '没有 A/B 层立绘条目');
}

const ADD = scenario('add model/999999 999999_01 scsp' + NL);
ADD.ctx.sp.SkeletonAnimation.create('model/999999.scsp', 'a', 1);
T('D18 add 能现场补映射',
  ADD.CREATED[ADD.CREATED.length - 1] === 'model/999999_01.scsp', ADD.CREATED.slice(-2).join(' | '));

const OFF = scenario('off' + NL);
OFF.ctx.sp.SkeletonAnimation.create(L_KEY || P_KEY || 'model/1', 'a', 1);
T('D19 off 之后一个都不改',
  OFF.api.made().on === false && OFF.api.made().made.hit === 0,
  'on=' + OFF.api.made().on + ' hit=' + OFF.api.made().made.hit);

const NOROW = scenario('norow' + NL);
T('D20 norow 之后不调官方通道',
  NOROW.APPLIED.length === 0, 'APPLIED=' + NOROW.APPLIED.length);

T('D21 令牌全展开、无残留',
  !/@V@|__SKIN_TABLE__/.test(RAW) && /skin\.cfg/.test(RAW) &&
  !(new RegExp(BRANDS, 'i').test(RAW)),
  '载荷 ' + RAW.length + ' B');
const R22 = (() => {
  for (const k of Object.keys(MAP)) {
    const m = MAP[k];
    if ('ABTSLM'.indexOf(m.t) < 0 || m.t.length !== 1) return '层级非法 ' + k;
    if (!Array.isArray(m.e) || !m.e.length) return '没有扩展名 ' + k;
    if (!/^[A-Za-z0-9_.]+$/.test(m.v)) return '变体名非法 ' + k;
    if (k.indexOf('/') < 0) return '基名没有目录 ' + k;
    if (m.t === 'L' && (!Array.isArray(m.va) || !m.va.length)) return 'L 层缺 va ' + k;
  }
  return true;
})();
T('D22 表结构自洽', R22 === true,
  R22 === true ? '条目 ' + Object.keys(MAP).length : R22);
T('D23 合成图集缺省不开',
  /图集合成=关/.test(READY) && !/ATLAS 合成/.test(LOG()), READY.slice(0, 130));

/* 卡面动画：没买回 0，只对有骨架的卡代答 */
const CM1 = scenario('');
const CM1Q = CM1.api.cardmotion();
T('D24 CharacterUtil 一赋值钩子就装上', CM1Q.armed === true, JSON.stringify(CM1Q));
const CM1V = CM1.ctx.CharacterUtil.getCardAnimation('c_1057_srt4_rsp5', 1057);
T('D25 没买的卡被代答成有动画',
  CM1V === 1 && CM1.api.cardmotion().forced === 1, '返回 ' + CM1V + ' 状态 ' + JSON.stringify(CM1.api.cardmotion()));
const CM2 = scenario('', { cardOwned: 7 });
const CM2V = CM2.ctx.CharacterUtil.getCardAnimation('c_30117_uni1', 30117);
T('D26 已购的真值原样放行', CM2V === 7 && CM2.api.cardmotion().forced === 0,
  '返回 ' + CM2V + ' 代答次数 ' + CM2.api.cardmotion().forced);
const CM3 = scenario('nocardmotion' + NL);
const CM3V = CM3.ctx.CharacterUtil.getCardAnimation('c_1057_srt4_rsp5', 1057);
T('D27 nocardmotion 时不装也不改行为（负控）',
  CM3.api.cardmotion().armed === false && CM3V === 0,
  'armed=' + CM3.api.cardmotion().armed + ' 返回 ' + CM3V);
const CM4 = scenario('cardmotion 4' + NL);
T('D28 代答值可由 cfg 调', CM4.ctx.CharacterUtil.getCardAnimation('c_x', 1) === 4,
  '返回 ' + CM4.ctx.CharacterUtil.getCardAnimation('c_x', 1));
const CM5 = scenario('', { cardRow: { spine_model: 'none' } });
const CM5V = CM5.ctx.CharacterUtil.getCardAnimation('c_1069_srt1', 1069);
const CM5Q = CM5.api.cardmotion();
T('D29 没有动态骨架的卡不被代答',
  CM5V === 0 && CM5Q.forced === 0 && CM5Q.skip === 1, JSON.stringify(CM5Q) + ' 返回 ' + CM5V);

const bad = ck.filter(c => !c[1]).length;
const SHOULD = 29;
console.log(lines.join(NL));
console.log(NL + '合计 ' + ck.length + ' 条（应有 ' + SHOULD + '），RED ' + bad);
process.exit((bad || ck.length !== SHOULD) ? 1 : 0);
