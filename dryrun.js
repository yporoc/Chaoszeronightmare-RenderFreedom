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
const PAYLOAD_SRC = fs.readFileSync(PAYLOAD_PATH, 'utf-8');
function rawOf(tab) {
  return PAYLOAD_SRC.replace(VB, '').replace(VE, '').split('@V@').join('dry')
    .replace('__SKIN_TABLE__', JSON.stringify(tab));
}
const RAW = rawOf(TABLE);
const RAW_LIVE = RAW;

const FAKE_DIR = '/writ' + 'able/';
const BRANDS = ['cz' + 'n', 'CZ' + 'NSKIN', 'v' + '14', 'LE' + 'GION', 'Chaos' + 'Zero'].join('|');
const ck = [], lines = [];
function T(n, c, i) { ck.push([n, !!c]); lines.push((c ? 'OK   ' : 'RED  ') + n + (i === undefined ? '' : '  → ' + String(i).slice(0, 150))); }
const MAP = TABLE.map || {}, ROWS = (TABLE.rows || {}).char_base || {};
const IDS = Object.keys(ROWS);
const pick = (t, pre) => Object.keys(MAP).filter(k => MAP[k].t === t && (!pre || k.indexOf(pre) === 0))[0];
const L_KEY = pick('L', 'model/'), A_KEY = pick('A', 'model/'), T_KEY = pick('T'), S_KEY = pick('S', 'model_data/');
const P_KEY = Object.keys(MAP).filter(k => k.indexOf('face/portrait/') === 0 && 'AB'.indexOf(MAP[k].t) >= 0)[0];
const want = (key, ext, tab) => (tab || MAP)[key]
  ? key.split('/').slice(0, -1).join('/') + '/' + (tab || MAP)[key].v + '.' + ext : key + '.' + ext;

/* L 层（"只换美术、动画表跟着变体走"）现在的实测结论是：棋盘件（command 只有 move/stop）
   绝不该拿来换局内小人，所以交付表里一条 L 都没有。可载荷里那套 L 逻辑（换骨架、
   MISS 记账、heal 回落、tier/drop 收回）还得有判据打着——这里造一条夹具行，
   只进沙箱，不进交付表。 */
const FIX_KEY = 'model/900001';
const FIXTAB = Object.assign({}, MAP, {
  [FIX_KEY]: { v: '900001_01', e: ['scsp', 'atlas', 'sct'], t: 'L', va: ['idle', 'stop'] },
});
const RAW_FIX = rawOf(Object.assign({}, TABLE, { map: FIXTAB }));

function scenario(cfgText, opts, fixTab) {
  opts = opts || {};
  const RAW = fixTab ? RAW_FIX : RAW_LIVE;
  const LOGS = [], INNER = [], CREATED = [], APPLIED = [], ABS = {};
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
  const api = ctx.skinRender;
  if (api && P_KEY) ctx.sp.SkeletonAnimation.create(P_KEY + '.scsp', 'x', 1);
  return { ctx, LOGS, INNER, CREATED, APPLIED, api, FU };
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
  (() => { const exp = Math.min(3, IDS.length);
    const m = LOG().match(/ROW 回读\(每表抽 \d+\) 对=(\d+) 不对=(\d+)/);
    return !!m && m[1] === String(exp) && m[2] === '0'; })(),
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

{
  const L = scenario('', {}, 1);
  L.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'a', 1);
  T('D7 L 层换成皮肤骨架',
    L.CREATED[L.CREATED.length - 1] === want(FIX_KEY, 'scsp', FIXTAB), L.CREATED.slice(-2).join(' | '));
  L.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'x', 1).setAnimation(0, 'zz_absent_anim', true);
  T('D8 放不出的动画如实记 MISS', L.api.made().made.animMiss >= 1,
    JSON.stringify(L.api.made().anim));
  L.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'y', 1);
  T('D9 缺省不自愈回落',
    L.api.made().made.heal === 0 && /自愈回落=关/.test(L.LOGS.filter(x => /READY/.test(x))[0] || ''),
    L.CREATED.slice(-2).join(' | '));
  const H = scenario('heal' + NL, {}, 1);
  H.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'a', 1);
  H.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'b', 1).setAnimation(0, 'zz_absent_anim', true);
  H.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'c', 1);
  T('D10 heal 一开安全网就回来',
    H.api.made().made.heal === 1 && H.CREATED[H.CREATED.length - 1] === FIX_KEY + '.scsp',
    H.CREATED.join(' | '));
  const B = scenario('tier ABTS' + NL, {}, 1);
  B.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'a', 1);
  T('D11 tier 去掉 L 就不换',
    B.CREATED[B.CREATED.length - 1] === FIX_KEY + '.scsp' &&
    Object.keys(B.api.made().refuse).some(k => /层级 L 未放行/.test(k)),
    JSON.stringify(B.api.made().refuse).slice(0, 140));
  const DP = scenario('drop model/' + NL, {}, 1);
  DP.ctx.sp.SkeletonAnimation.create(FIX_KEY + '.scsp', 'a', 1);
  T('D12 drop 能收回整目录',
    DP.CREATED[DP.CREATED.length - 1] === FIX_KEY + '.scsp' &&
    Object.keys(DP.api.made().refuse).some(k => /cfg drop/.test(k)),
    JSON.stringify(DP.api.made().refuse).slice(0, 140));
  T('D12b 交付表里一条 L 都没有（棋盘件不换局内小人）',
    !Object.keys(MAP).some(k => MAP[k].t === 'L' && k.indexOf('model/') === 0),
    Object.keys(MAP).filter(k => MAP[k].t === 'L').join(' ') || '无 L 层');
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

/* 本轮定案的结构性判据：局内的美术和动画命令表是同一个名字要来的，
   只换 model/ 会让骨架里没有 attack_ready/hit，角色停在摆放姿态。 */
{
  const unpaired = [];
  for (const k of Object.keys(MAP)) {
    if (k.indexOf('model/') !== 0 || /_battle_ready$/.test(k)) continue;
    const base = k.slice('model/'.length), md = MAP['model_data/' + base];
    if (!md || md.v !== MAP[k].v) { unpaired.push(k + '→' + MAP[k].v + ' 没有同变体的 model_data 行'); continue; }
    const lack = ['srmd', 'srcs'].filter(x => (md.e || []).indexOf(x) < 0);
    if (lack.length) unpaired.push(k + ' 的配对行没列 ' + lack.join('/'));
  }
  T('D24 每条小人换装都带着同变体的命令表', unpaired.length === 0, unpaired.join(' | ')
    || Object.keys(MAP).filter(k => k.indexOf('model/') === 0).join(' '));
  const back = Object.keys(MAP).filter(k => /_battle_ready$/.test(k))
    .filter(k => !MAP[k.replace('model/', 'model_data/')] ||
                 MAP[k.replace('model/', 'model_data/')].v !== MAP[k].v);
  T('D24b 背身件也成对（.brmd 跟着换）', back.length === 0,
    back.join(' ') || '背身行 ' + Object.keys(MAP).filter(k => /_battle_ready$/.test(k)).length + ' 条');
  const gone = ['1056', '30084', '30093', '30097', '30113', '30115', '30117', 'lobby_planet_earth']
    .filter(id => MAP['model/' + id] || MAP['model_data/' + id]);
  T('D25 走停棋盘件不再被拿来换局内小人', gone.length === 0,
    gone.length ? '还在表里：' + gone.join(',') : '8 个基名都不在 model/ 与 model_data/ 里');
  /* 一个角色只能顶一套皮肤：脸族是 <族名>_<token> 各自独立拼的（game/character_model.js
     getFaceByToken，token = model_number 或 char_base.model），族与族之间号不一样就会
     "脸是另一套皮肤、身体是这一套"。card 族的 _NN 是卡面序号，不参与。 */
  {
    const dirs = ['face/character', 'face/portrait', 'model', 'model_data'];
    const byc = {};
    for (const k of Object.keys(MAP)) {
      const d = k.slice(0, k.lastIndexOf('/'));
      if (dirs.indexOf(d) < 0) continue;
      const mv = String(MAP[k].v).match(/^(.*?)(\d{3,7})(?:_(\d{2}))?$/);
      if (!mv || !mv[3]) continue;
      byc[mv[2]] = byc[mv[2]] || {};
      byc[mv[2]][d] = byc[mv[2]][d] || new Set();
      byc[mv[2]][d].add(mv[3]);
    }
    const clash = [];
    for (const cid of Object.keys(byc).sort()) {
      const nums = new Set();
      Object.keys(byc[cid]).forEach(d => byc[cid][d].forEach(n => nums.add(n)));
      if (nums.size > 1) clash.push(cid + ' 用了 ' + Array.from(nums).sort().join('/') +
        '（' + Object.keys(byc[cid]).map(d => d + ':' + Array.from(byc[cid][d]).sort().join(',')).join(' ') + '）');
    }
    T('D26 同一角色所有族顶的是同一个皮肤号', clash.length === 0,
      clash.join(' | ') || Object.keys(byc).length + ' 个角色的族号统一');
  }
}

const bad = ck.filter(c => !c[1]).length;
const SHOULD = 28;
console.log(lines.join(NL));
console.log(NL + '合计 ' + ck.length + ' 条（应有 ' + SHOULD + '），RED ' + bad);
process.exit((bad || ck.length !== SHOULD) ? 1 : 0);
