# -*- coding: utf-8 -*-
"""出件管线：检测 -> 沙箱断言 -> 注入 -> 回读自证。

   界面与命令行共用这一份代码，只换日志出口；任何一步不过就不出货，不静默降级。
"""
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time

from bundle import Bundle, BundleError, INIT_NAME, derive_format, rebuild, to_container_format

APP_NAME = 'skinrender'
DEFAULT_VERSION = 'v1'
ANCHOR = 'global._external_interface_handler_call = function (type, jstr) {'
FIRST_PERF = 'const _application_start_contents'
LAST_TOP = 'addCustomEventListener'
BEGIN = '/* ==== SKINRENDER-BEGIN %s ==== */'
END = '/* ==== SKINRENDER-END %s ==== */'
TABLE_TOKEN = '__SKIN_TABLE__'
VER_TOKEN = '@V@'
# 历史块标记：重新出件前整块摘除，避免同一份 init.js 里叠两块载荷
LEGACY = 'CZ' + 'NSKIN'      # 上一代工具的块标记族，重新出件前要摘干净；这里不留完整字面量
# 整块摘除（含块体），只摘标记行的话，重复出件会把两份载荷叠在同一份 init.js 里
PURGE = [r'(?s)/\* ==== (?:SKINRENDER|' + LEGACY + r')-BEGIN[^\n]*==== \*/'
         r'.*?/\* ==== (?:SKINRENDER|' + LEGACY + r')-END[^\n]*==== \*/\n?',
         r'[^\n]*\(function\(\)\{try\{if\(globalThis\._speedLog\)[^\n]*\n?']
CFG_KEYS = ('off', 'nomap', 'nofu', 'nocreate', 'norow', 'noatl', 'heal', 'tier', 'drop', 'add', 'row')


class EngineError(RuntimeError):
    """能用人话说清的原因；调用方只打印这句话，不抛栈。"""


def app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def res(name):
    """随包资源的位置：冻结件在解包目录，源码运行在本目录。"""
    if getattr(sys, 'frozen', False):
        base = getattr(sys, '_MEIPASS', app_dir())
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def default_out(src_path):
    """默认产物位置：工具自己的 out/，绝不落在源件所在目录。"""
    return os.path.join(app_dir(), 'out',
                        os.path.splitext(os.path.basename(src_path))[0] + '-skin.exe')


def _dir_of(path):
    return os.path.normcase(os.path.abspath(os.path.dirname(os.path.abspath(path)) or '.'))


def report_path(tag):
    """运行记录位置：exe 同级 logs/。"""
    d = os.path.join(app_dir(), 'logs')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = app_dir()
    return os.path.join(d, 'skinrender-%s.txt' % tag)


def require_assets():
    """随包三件必须齐备且自洽，缺哪件就说哪件。"""
    need = {'table.json': None, 'payload.js': None, 'dryrun.js': None}
    for name in need:
        p = res(name)
        if not os.path.isfile(p):
            raise EngineError('包里缺 %s（应在 %s）。这份件不完整，不能用来出件。' % (name, app_dir()))
        need[name] = p
    txt, table, counts = load_table(need['table.json'])
    payload = io.open(need['payload.js'], encoding='utf-8').read()
    if TABLE_TOKEN not in payload:
        raise EngineError('payload.js 里没有表占位符，这份载荷进不了舱')
    if BEGIN % VER_TOKEN not in payload:
        raise EngineError('payload.js 的块标记不对，出件后无法判定是哪一块在跑')
    return {'table': txt, 'counts': counts, 'payload': payload,
            'rows': sum(len(v) for v in table['rows'].values()),
            'entries': len(table['map'])}


def sha16(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1 << 24), b''):
            h.update(blk)
    return h.hexdigest()


def stamp(path):
    return (sha16(path), os.path.getsize(path), int(os.path.getmtime(path)))


def load_table(path):
    if not os.path.isfile(path):
        raise EngineError('找不到分层表：%s' % os.path.basename(path))
    try:
        txt = io.open(path, encoding='utf-8').read()
        table = json.loads(txt)
    except ValueError as e:
        raise EngineError('分层表不是合法 JSON（%s）：%s' % (os.path.basename(path), str(e)[:80]))
    for key in ('map', 'rows', 'revision'):
        if key not in table:
            raise EngineError('分层表缺 %r 字段，这份表不能用' % key)
    counts = {}
    for k, m in table['map'].items():
        if not isinstance(m, dict) or 'v' not in m or 't' not in m or 'e' not in m:
            raise EngineError('分层表条目 %s 形状不对（要有 v/t/e）' % k)
        counts[m['t']] = counts.get(m['t'], 0) + 1
    if not table['map']:
        raise EngineError('分层表是空的：这台客户端里没有可换的族，出件没有意义')
    return txt, table, counts


def normalize(init_bytes):
    text = init_bytes.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n').rstrip('\n')
    for pat in PURGE:
        text = re.sub(pat, '', text)
    return text


def assemble(payload_text, table_text, init_bytes, version):
    """返回 (新 init.js 的 LF 文本字节, 信息 dict)。锚点不写死行号，全部现算。"""
    mv = re.search(re.escape('SKINRENDER-BEGIN ') + r'\s*(\S+)\s*==== \*/', payload_text)
    tag = mv.group(1) if mv else None
    v = version or (tag if tag and tag != VER_TOKEN else None)
    if not v:
        raise EngineError('没给版本号，载荷标记里也没有：请在界面里填一个，或让载荷自带版本')
    body = payload_text.replace(BEGIN % (tag or VER_TOKEN), '').replace(END % (tag or VER_TOKEN), '')
    body = body.replace(VER_TOKEN, v).strip('\n')
    if body.count(TABLE_TOKEN) != 1:
        raise EngineError('载荷里的表占位符 %s 出现 %d 次（应为 1）：载荷被改坏了'
                          % (TABLE_TOKEN, body.count(TABLE_TOKEN)))
    body = body.replace(TABLE_TOKEN, table_text)
    for tok in (TABLE_TOKEN, VER_TOKEN):
        if tok in body:
            raise EngineError('占位符没展开干净，残留 %s' % tok)
    lines = normalize(init_bytes).split('\n')
    idx = [i for i, l in enumerate(lines) if l.strip() == ANCHOR]
    if len(idx) != 1:
        raise EngineError('那句官方外部桥定义（锚点）在这份 init.js 里出现 %d 次，应为 1 次；'
                          '客户端启动层结构与本工具的预期不符，先停' % len(idx))
    a = idx[0]
    perf = [i for i, l in enumerate(lines) if FIRST_PERF in l]
    last = [i for i, l in enumerate(lines) if LAST_TOP in l]
    if not perf or not last:
        raise EngineError('找不到可执行窗口的边界（测量语句 / 末句监听注册），这份启动层读不出边界')
    if not (perf[0] < a < last[0]):
        raise EngineError('锚点不在可执行窗口内（测量句@%d 锚点@%d 末句@%d）：写进去也不会被执行'
                          % (perf[0] + 1, a + 1, last[0] + 1))
    block = BEGIN % v + '\n' + body + '\n' + END % v
    lines.insert(a, block)
    final = ('\n'.join(lines).rstrip('\n') + '\n').encode('utf-8')
    return final, {'version': v, 'anchor_line': a + 1,
                   'window': (perf[0] + 1, last[0] + 1), 'block_bytes': len(block.encode('utf-8')),
                   'orig': len(init_bytes), 'new': len(final)}


def node_gate(script, table_path, payload_path, log):
    """出货前的沙箱硬闸：缺 node、跑不起来、条数与自报应有数不符，一律拒绝。"""
    try:
        r = subprocess.run(['node', script, '--table', table_path, '--payload', payload_path],
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           cwd=os.path.dirname(script) or None)
    except FileNotFoundError:
        raise EngineError('这台机器上没有 node：沙箱断言是出货硬闸，缺它不能出件。'
                          '请装 Node（命令行输入 node --version 能看到版本）后重试。')
    txt = (r.stdout or '') + (r.stderr or '')
    m = re.search(r'合计 (\d+) 条（应有 (\d+)）', txt)
    if r.returncode != 0:
        raise EngineError('沙箱断言没过（退出码 %d），不出货' % r.returncode)
    if not m:
        raise EngineError('沙箱断言没自报条数，这道闸形同装饰，拒绝出货')
    if int(m.group(1)) != int(m.group(2)):
        raise EngineError('沙箱断言只跑了 %s 条，应有 %s 条：有判据被跳过，不算通过'
                          % (m.group(1), m.group(2)))
    return int(m.group(1)), txt


def syntax_gate(payload_text, table_text, version, log):
    """把即将进舱的那份文本落成临时文件让 node 语法检查，检查的是真货而不是模板。"""
    body = payload_text.replace(BEGIN % version, '').replace(END % version, '') \
        .replace(VER_TOKEN, version).strip('\n').replace(TABLE_TOKEN, table_text)
    tmp = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-check.js')
    io.open(tmp, 'w', encoding='utf-8', newline='\n').write(body)
    try:
        r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
    except FileNotFoundError:
        raise EngineError('这台机器上没有 node，无法做载荷语法检查')
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if r.returncode:
        raise EngineError('载荷语法检查没过：\n%s' % (r.stderr or r.stdout)[:400])
    log('载荷语法检查通过')


def read_back(src_exe, dst_exe, version, table_text, init_before):
    """出舱回读自证：每条都独立成立，任一 RED 都不该出货。"""
    c0, c1 = Bundle(src_exe), Bundle(dst_exe)
    txt = c1.init_bytes.decode('utf-8', 'replace')
    vb, ve = BEGIN % version, END % version
    n_map = table_text.count('"t":"')
    has_rows = '"rows":{"char_base":{"' in table_text   # 载荷里也有 char_base 字样，必须用结构标记
    stripped = re.sub(re.escape(vb) + r'[^\n]*\n.*?' + re.escape(ve) + r'[^\n]*\n?', '', txt, flags=re.S)
    stripped = re.sub(r'[^\n]*SKINRENDER-(BEGIN|END)[^\n]*\n?', '', stripped)
    a = [l.rstrip() for l in normalize(init_before).split('\n') if l.strip()]
    b = [l.rstrip() for l in stripped.replace('\r\n', '\n').split('\n') if l.strip()]
    other = [n for n in c0.js_names if n != INIT_NAME]
    out = []
    add = lambda n, ok: out.append((n, bool(ok)))
    add('输出大小 == 源', os.path.getsize(dst_exe) == os.path.getsize(src_exe))
    add('载荷块唯一', c1.init_bytes.count(vb.encode()) == 1 and c1.init_bytes.count(ve.encode()) == 1)
    add('无其它版本残留块', len(re.findall(r'SKINRENDER-BEGIN', txt)) == 1 and
        LEGACY + '-BEGIN' not in txt)
    add('载荷落在可执行窗口内',
        0 < txt.find(vb) < txt.find(LAST_TOP) and txt.find(vb) > txt.find(FIRST_PERF))
    add('占位符已展开', TABLE_TOKEN.encode('utf-8') not in c1.init_bytes and
        VER_TOKEN.encode('utf-8') not in c1.init_bytes)
    add('分层表 %d 条已进舱' % n_map, txt.count('"t":"') == n_map)
    add('行覆盖表随表进舱', ('"rows":{"char_base":{"' in txt) == bool(has_rows))
    add('官方行覆盖通道名在内', 'applyClientOverrides' in txt)
    add('十一个开关名都在', all(k in txt for k in CFG_KEYS))
    add('缺省放行层是 ABTSL', "'ABTSL'" in txt)
    add('摘块后与源逐行全等', a == b and len(a) == len(b))
    add('其余 %d 个 JS 未变' % len(other), all(c1.read(n) == c0.read(n) for n in other))
    add('容器仍有填充余量', c1.entries[c1.padding_index].file_size > 0 if c1.padding_index >= 0 else False)
    add('未修改条目字节相同',
        all(c1.read(i.filename) == c0.read(i.filename) for i in c0.entries
            if i.filename not in (INIT_NAME, 'pre/dummy_padding.bin')))
    summary = {'sha_src': sha16(src_exe)[:16], 'sha_dst': sha16(dst_exe)[:16],
               'entries_in_exe': n_map, 'padding_left': (c1.entries[c1.padding_index].file_size
                                                         if c1.padding_index >= 0 else 0),
               'size': os.path.getsize(dst_exe), 'lines': len(a)}
    return out, summary


def detect(exe_path, table_path=None, payload_path=None, manifest=None, log=print,
           version=None):
    """只读检测：把「这颗件能不能加工、加工会长多少」全部量出来，不做任何写入。"""
    rows = []
    add = lambda nm, ok, why='': rows.append((nm, bool(ok), why))

    def quiet(nm, ok, why):
        """通过就不占一行；出事才出声，判据一条不少。"""
        if not ok:
            add(nm, False, why)
    if not os.path.isfile(exe_path):
        raise EngineError('文件不存在：%s' % exe_path)
    size = os.path.getsize(exe_path)
    add('文件', True, '%s  %s B  %s…' % (os.path.basename(exe_path), format(size, ','),
                                      sha16(exe_path)[:12]))
    try:
        c = Bundle(exe_path)
    except BundleError as e:
        add('内嵌明文脚本层', False, str(e))
        add('结论', False, '这颗件不能加工：明文脚本层读不出来')
        return {'rows': rows, 'container': None, 'ok': False, 'init_bytes': b'', 'text': '',
                'table': None, 'payload': None, 'manifest': {}, 'size': size,
                'sha': sha16(exe_path)}
    add('内嵌脚本层', True, '条目 %d，明文脚本 %d 个' % (len(c.entries), len(c.js_names)))
    init = c.init_bytes
    quiet('入口脚本', len(init) > 1000, '只有 %d B，不像真的脚本层' % len(init))
    nl, bom, tail = derive_format(init)
    if not nl or tail < 0:
        raise EngineError('容器格式推不出来')
    text = normalize(init)
    lines = text.split('\n')
    idx = [i for i, l in enumerate(lines) if l.strip() == ANCHOR]
    perf = [i for i, l in enumerate(lines) if FIRST_PERF in l]
    last = [i for i, l in enumerate(lines) if LAST_TOP in l]
    add('落点', len(idx) == 1, '第 %d 行（可执行区间 %d–%d）' % (
            idx[0] + 1, perf[0] + 1 if perf else 0, last[0] + 1 if last else 0)
        if len(idx) == 1 else '锚点出现 %d 次，应为 1 次' % len(idx))
    quiet('可执行区间', bool(perf) and bool(last) and len(idx) == 1 and perf[0] < idx[0] < last[0],
          '边界读不出来或锚点不在区间内')
    found = re.findall(r'(?:SKINRENDER|' + LEGACY + r')-BEGIN (\S+)', init.decode('utf-8', 'replace'))
    add('已有载荷', True, '会先摘掉旧块（%s）' % '/'.join(found) if found else '无')
    table_info = None
    if table_path:
        txt, table, counts = load_table(table_path)
        table_info = {'text': txt, 'table': table, 'counts': counts}
        add('分层表', True, '%d 条，分 %s；行覆盖 %s' % (
            len(table['map']), ' '.join('%s%d' % (k, counts[k]) for k in sorted(counts)),
            ' '.join('%s%d' % (k, len(v)) for k, v in sorted(table['rows'].items()) if v)
            or '无'))
        bad = [k for k, m in table['map'].items() if not m['e'] or '/' not in k]
        quiet('分层表形状', not bad, '%d 条不完整' % len(bad))
    payload_info = None
    if payload_path:
        pt = io.open(payload_path, encoding='utf-8').read()
        payload_info = pt
        quiet('载荷', os.path.getsize(payload_path) > 2000, '载荷文件太小')
        if table_info:
            final, info = assemble(pt, table_info['text'], init, version or DEFAULT_VERSION)
            packed = to_container_format(final, derive_format(init))
            pad = c.entries[c.padding_index].file_size if c.padding_index >= 0 else 0
            grow = len(packed) - len(init)
            add('装得下', grow <= pad,
                ('比原有载荷省 %s B' % format(-grow, ',')) if grow < 0
                else ('需 %s B' % format(grow, ',')) + '，余 %s B' % format(pad, ','))
        quiet('运行期开关齐全', all(k in pt for k in CFG_KEYS) and "'ABTSL'" in pt,
              '开关名或缺省放行层缺失')
    extra = {}
    if manifest:
        extra = manifest_check(table_info['table'] if table_info else None, manifest, add)
    else:
        add('资源清单', True, '未提供，这一项未测')
        add('目标文件在盘上', True, '未测')
    ok = all(r[1] for r in rows)
    add('结论', ok, '可以出件' if ok else '%d 项不合格' % sum(1 for r in rows if not r[1]))
    return {'rows': rows, 'container': c, 'text': text, 'table': table_info, 'payload': payload_info,
            'ok': ok, 'manifest': extra, 'init_bytes': init, 'size': size,
            'sha': sha16(exe_path)}


def manifest_check(table, manifest_path, add):
    """目标文件在不在盘上：只按字符串扫资源清单，不要求清单结构，扫不到的条目单独报数。"""
    if not os.path.isfile(manifest_path):
        add('资源清单', False, '找不到 %s' % os.path.basename(manifest_path))
        return {}
    data = open(manifest_path, 'rb').read()
    blob = re.findall(rb'[a-z0-9_]+(?:/[a-zA-Z0-9_]+)+\.(?:scsp|atlas|sct|srmd|srcs|srue|opt|brmd)', data)
    have = set(x.decode() for x in blob)
    if not have:
        add('清单可解析', False, '扫不出资源路径，清单格式和预期不符')
    if table is None:
        return {}
    need = []
    for key, m in table['map'].items():
        d, base = key.rsplit('/', 1)
        for ext in m['e']:
            need.append('%s/%s.%s' % (d, m['v'], ext))
    miss = sorted(x for x in need if x not in have)
    add('目标文件在盘上', not miss, '%d 个全在' % len(need) if not miss
        else '缺 %d/%d 个（%s）' % (len(miss), len(need), ', '.join(miss[:2])))
    # 附加页闸：本体另有 <stem>_am.sct / <stem>.opt 而变体没有时，本体的附加页矩形会压到皮肤的页上。
    # 豁免：同一条皮肤连 model_data 一起换成同一变体（动画命令表跟着换）时实测不出这种色块，放行。
    orphan, exempt = [], []
    for key, m in table['map'].items():
        d, base = key.rsplit('/', 1)
        md = table['map'].get('model_data/' + base)
        paired = bool(md and md.get('v') == m['v'] and 'srmd' in md.get('e', []))
        for suf in ('_am.sct', '.opt'):
            src = '%s/%s%s' % (d, base, suf)
            dst = '%s/%s%s' % (d, m['v'], suf)
            if src in have and dst not in have:
                (exempt if paired else orphan).append(src)
    add('本体附加页有对应', not orphan,
        ('无孤立附加页' if not orphan else
         '这些条目要摘掉重定向，盘上没有对应的变体附加页：%s' % ', '.join(orphan[:3]))
        + ('；另有 %d 条因连 model_data 同变体而豁免：%s' % (len(exempt), ', '.join(exempt[:3]))
           if exempt else ''))
    # 小人换装闸。局内的美术和动画命令表是同一个名字要来的（battle_stage.js 两处 load 都走
    # CombatantSkinUtil.getBattleModel），只换 model/ 不换 model_data/ 时，本体命令表要
    # attack_ready/hit/... 而变体骨架里只有 move/stop，isExistAnimation 全落空，
    # 于是角色停在摆放姿态 = 正面站立毫无动作。反编译实测：变体只有带自己的 .srcs 才是完整一套
    # （官方闸门 getBattleSkin 对 SPECIAL 也同样要求 .srcs）。_battle_ready 例外，
    # animation_node.js 见到名字含 _battle_ready 就不加载命令表。
    rig, rig_exempt, rig_pair_bad = [], [], []
    for key, m in sorted(table['map'].items()):
        if not key.startswith('model/') or key.endswith('_battle_ready'):
            continue
        base = key[len('model/'):]
        md = table['map'].get('model_data/' + base)
        if not md or md.get('v') != m['v']:
            rig_pair_bad.append('%s->%s（model_data/%s %s）'
                                % (key, m['v'], base, '缺行' if not md else '目标是 ' + str(md.get('v'))))
            continue
        lack = [x for x in ('srmd', 'srcs') if x not in md.get('e', [])]
        if lack:
            rig.append('%s->%s（配对行没列 %s，命令表还是本体的）' % (key, m['v'], '/'.join(lack)))
        elif 'model_data/%s.srcs' % m['v'] not in have:
            (rig_exempt if m.get('x') else rig).append('%s->%s（容器里没有 model_data/%s.srcs）'
                                                       % (key, m['v'], m['v']))
    for key, m in sorted(table['map'].items()):
        if key.startswith('model_data/') and not key.endswith('_battle_ready'):
            base = key[len('model_data/'):]
            if ('model/' + base) not in table['map']:
                rig_pair_bad.append('%s->%s（model/%s 缺行）' % (key, m['v'], base))
    add('小人换装带同变体命令表', not rig and not rig_pair_bad,
        ('%d 条 model/ 换装都配了同变体的 model_data/（srmd+srcs 都在行里且都在盘上）'
         % len([k for k in table['map'] if k.startswith('model/')]) if not rig and not rig_pair_bad else
         ('这些条目换了小人骨架但命令表还是本体的，局内只能停在摆放姿态：%s' % ', '.join(rig[:3])
          if rig else '')
         + ('；配对不完整：%s' % ', '.join(rig_pair_bad[:3]) if rig_pair_bad else ''))
        + ('；豁免 %d 条：%s' % (len(rig_exempt), ', '.join(rig_exempt[:3]) if rig_exempt else '无')
           if rig_exempt else ''))
    # 行顶替给出的卡面美术名，客户端是按 card/ 与 card_illustration/ 前缀直接去要的
    # （实测参照系：card 表 202 个 spine_model 全部在 card/ 有 .scsp，effect/ 一个都没有），
    # 所以名字必须能在盘上落地，否则等于让客户端去请求一个不存在的文件。
    bad_names = []
    card_rows = (table.get('rows') or {}).get('card') or {}
    for cid, p in card_rows.items():
        sm = p.get('spine_model')
        if sm and sm != 'none' and ('card/%s.scsp' % sm) not in have:
            bad_names.append('%s→card/%s.scsp' % (cid, sm))
        sn = p.get('sct_name')
        if sn and sn != 'none' and ('card_illustration/%s.sct' % sn) not in have:
            bad_names.append('%s→card_illustration/%s.sct' % (cid, sn))
    add('卡面行顶替的目标名可解析', not bad_names,
        '%d 条都能在盘上落地' % len(card_rows)
        if not bad_names else '这些名字客户端要到但盘上没有：%s' % ', '.join(bad_names[:3]))
    # 同一角色的皮肤号必须全局同一个。脸族是 `face/character/<族名>_<token>` 一类族名各自独立地
    # 按同一个 token 拼出来的（反编译 game/character_model.js 的 getFaceByToken：
    # token = model_number 或 char_base.model），所以一个角色只能有一套皮肤在被顶；
    # 若某族指 _01、别的族指 _02，玩家就会在编队/聊天那一屏看到"脸和身体不是同一套皮肤"。
    # 卡面族（card/ 与 card_illustration/ 的 _NN 是卡面序号）不参与这条判断。
    SKIN_DIRS = ('face/character', 'face/portrait', 'model', 'model_data')
    byc = {}
    for key, m in table['map'].items():
        d, base = key.rsplit('/', 1)
        if d not in SKIN_DIRS:
            continue
        mv = re.match(r'^(.*?)(\d{3,7})(?:_(\d{2}))?$', str(m.get('v') or ''))
        if not mv or not mv.group(3):
            continue                      # 没带皮肤号 = 顶到本体号，不参与统一性判定
        byc.setdefault(mv.group(2), {}).setdefault(d, set()).add(mv.group(3))
    clash = []
    for cid in sorted(byc):
        nums = set()
        for exts in byc[cid].values():
            nums |= exts
        if len(nums) > 1:
            clash.append('%s 用了 %s（%s）' % (cid, '/'.join(sorted('_' + n for n in nums)),
                            ', '.join('%s:%s' % (d, '/'.join(sorted(v)))
                                      for d, v in sorted(byc[cid].items()))))
    add('同一角色的皮肤号全局一致', not clash,
        '%d 个角色的皮肤号各自统一' % len(byc) if not clash
        else '这些角色的族与族之间皮肤号不一致：%s' % '; '.join(clash[:3]))
    return {'need': len(need), 'missing': miss, 'orphan_extra': orphan, 'orphan_exempt': exempt,
            'card_names': bad_names, 'rig_no_srcs': rig, 'rig_pair_bad': rig_pair_bad,
            'rig_exempt': rig_exempt}


def produce(src_exe, dst_exe, table_path, payload_path, version=None, manifest=None, log=print):
    """完整出件：检测 -> 两道闸 -> 注入 -> 回读自证 -> 复核。任一步 RED 就不留产物。"""
    t0 = time.time()
    table_path = table_path or res('table.json')
    payload_path = payload_path or res('payload.js')
    if os.path.normcase(os.path.abspath(src_exe)) == os.path.normcase(os.path.abspath(dst_exe)):
        raise EngineError('输出不能是输入本身')
    if not os.path.isfile(src_exe):
        raise EngineError('要加工的 exe 不存在：%s' % src_exe)
    if _dir_of(src_exe) == _dir_of(dst_exe):
        raise EngineError('输出目录与输入所在目录相同：请换到别的目录，别把产物混进待加工的那份里')
    v = version or DEFAULT_VERSION
    before = stamp(src_exe)
    det = detect(src_exe, table_path, payload_path, manifest, log, version=v)
    if not det['ok']:
        bad = [r[0] for r in det['rows'] if not r[1]]
        raise EngineError('检测没通过：%s' % '、'.join(bad))
    table_text = det['table']['text']
    counts = det['table']['counts']
    rc, dry = node_gate(res('dryrun.js'), table_path, payload_path, lambda *a: None)
    log('沙箱断言 %d 条全过' % rc)
    syntax_gate(det['payload'], table_text, v, lambda *a: None)
    final, info = assemble(det['payload'], table_text, det['init_bytes'], v)
    c = det['container']
    st = rebuild(c, final, dst_exe, src_exe=src_exe)
    log('注入：入口脚本 +%s B，落在第 %d 行' % (format(st['grow'], ','), info['anchor_line']))
    checks, summ = read_back(src_exe, dst_exe, v, table_text, det['init_bytes'])
    for nm, ok in checks:
        if not ok:
            log('  回读自证未过：%s' % nm)
    if stamp(src_exe) != before:
        checks.append(('输入文件未被改动', False))
        log('  回读自证未过：输入文件被改动了')
    else:
        checks.append(('输入文件未被改动', True))
    bad = [nm for nm, ok in checks if not ok]
    proof = {'app': APP_NAME, 'version': v, 'src': os.path.basename(src_exe),
             'src_sha256': before[0], 'src_size': before[1],
             'out': os.path.basename(dst_exe), 'out_sha256': summ['sha_dst'],
             'out_size': summ['size'], 'table_sha256': sha16(table_path),
             'payload_sha256': sha16(payload_path), 'entries': counts,
             'rows': sum(len(v) for v in det['table']['table']['rows'].values()),
             'padding_left': summ['padding_left'], 'init_js': {'orig': info['orig'], 'new': info['new']},
             'anchor_line': info['anchor_line'], 'window': list(info['window']),
             'checks': {nm: bool(ok) for nm, ok in checks}, 'seconds': round(time.time() - t0, 1)}
    if bad:
        try:
            os.remove(dst_exe)
        except OSError:
            pass
        proof['rejected'] = bad
        io.open(dst_exe + '.rejected.json', 'w', encoding='utf-8').write(
            json.dumps(proof, ensure_ascii=False, indent=1))
        raise EngineError('%d 条回读自证没过，产物已删除，只留下 %s 记录原因' % (
            len(bad), os.path.basename(dst_exe + '.rejected.json')))
    io.open(dst_exe + '.proof.json', 'w', encoding='utf-8').write(
        json.dumps(proof, ensure_ascii=False, indent=1))
    log('出件通过：%s  %s B  sha256 %s…  用时 %ss' % (
        os.path.basename(dst_exe), format(summ['size'], ','), summ['sha_dst'][:12], proof['seconds']))
    return {'proof': proof, 'checks': checks}


def gate_selftest(table_path=None, payload_path=None):
    """闸门的负向对照：故意喂坏输入，结构闸必须真的拦下来。拦不住就是装饰。"""
    table_path = table_path or res('table.json')
    payload_path = payload_path or res('payload.js')
    table_text, _, _ = load_table(table_path)
    payload = io.open(payload_path, encoding='utf-8').read()
    perf, last = 'const _application_start_contents = 1;', "addCustomEventListener('x');"

    def win(order='perf,anchor,last', times=1):
        out = []
        for tok in order.split(','):
            if tok == 'perf':
                out.append(perf)
            elif tok == 'last':
                out.append(last)
            else:
                out.extend([ANCHOR] * times)
        return '\n'.join(out).encode('utf-8')

    cases = [
        ('锚点出现两次要拒绝', payload, win(times=2), 't', '出现 2 次'),
        ('锚点一次都没有要拒绝', payload, win(times=0), 't', '出现 0 次'),
        ('锚点在窗口之外要拒绝', payload, win('anchor,perf,last'), 't', '不在可执行窗口内'),
        ('找不到窗口边界要拒绝', payload, win('anchor'), 't', '读不出边界'),
        ('表占位符丢失要拒绝', payload.replace(TABLE_TOKEN, ''), win(), 't', '占位符'),
        ('版本号无处可取要拒绝', payload.replace(BEGIN % VER_TOKEN, '').replace(VER_TOKEN, ''),
         win(), None, '没给版本号'),
    ]
    out = []
    for nm, pl, fixture, ver, want in cases:
        try:
            assemble(pl, table_text, fixture, ver)
            out.append((nm, False, '没拦下来'))
        except EngineError as e:
            out.append((nm, want in str(e), str(e)[:76]))
    for nm, bad_text, want in (('表缺字段要拒绝', table_text.replace('"map"', '"maps"', 1), '缺'),
                               ('表不是 JSON 要拒绝', '{"map":', '合法 JSON')):
        p = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-badtable.json')
        io.open(p, 'w', encoding='utf-8', newline='\n').write(bad_text)
        try:
            load_table(p)
            out.append((nm, False, '没拦下来'))
        except EngineError as e:
            out.append((nm, want in str(e), str(e)[:76]))
        finally:
            os.remove(p)
    try:
        produce(os.path.join(os.environ.get('TEMP', '.'), '根本不存在的件.exe'),
                os.path.join(os.environ.get('TEMP', '.'), '输出.exe'),
                table_path, payload_path, log=lambda *a: None)
        out.append(('输入不存在要拒绝', False, '没拦下来'))
    except EngineError as e:
        out.append(('输入不存在要拒绝', '不存在' in str(e), str(e)[:76]))
    tmp_same = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-same.exe')
    io.open(tmp_same, 'wb').write(b'MZ')                     # 冻结件里 __file__ 指向的位置可能不存在，这里用真文件
    try:
        produce(tmp_same, tmp_same, table_path, payload_path, log=lambda *a: None)
        out.append(('输出等于输入要拒绝', False, '没拦下来'))
    except EngineError as e:
        out.append(('输出等于输入要拒绝', '不能是输入本身' in str(e), str(e)[:76]))
    finally:
        if os.path.exists(tmp_same):
            os.remove(tmp_same)

    def mcheck(variant_ready, with_md=None):
        """孤立附加页的对照：同一张表，只改"变体的附加页在不在盘上"和有没有 model_data 配对。"""
        paths = ['model/30084.scsp', 'model/30084.atlas', 'model/30084.sct',
                 'model/30084.opt', 'model/30084_am.sct',
                 'model/30084_01.scsp', 'model/30084_01.atlas', 'model/30084_01.sct']
        if variant_ready:
            paths += ['model/30084_01_am.sct', 'model/30084_01.opt']
        mp = {'model/30084': {'v': '30084_01', 't': 'L', 'e': ['scsp', 'atlas', 'sct']}}
        if with_md:
            mp['model_data/30084'] = {'v': with_md, 't': 'S', 'e': ['srmd']}
        p = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-manifest.bin')
        got = []
        io.open(p, 'wb').write(' '.join(paths).encode())
        try:
            manifest_check({'map': mp}, p, lambda nm, ok, why: got.append((nm, ok, why)))
        finally:
            os.remove(p)
        return {n: ok for n, ok, _ in got}, [w for n, ok, w in got if not ok]
    try:
        bad, badwhy = mcheck(False)
        out.append(('孤立附加页要拒绝', bad.get('本体附加页有对应') is False,
                    '没拦下来，这条目会画成全身色块' if bad.get('本体附加页有对应') is not False
                    else '拦下：%s' % ' '.join(badwhy)))
        good, _ = mcheck(True)
        out.append(('附加页齐备时不误杀',
                    good.get('本体附加页有对应') is True and good.get('目标文件在盘上') is True,
                    ' '.join('%s=%s' % (n, ok) for n, ok in good.items())))
        exm, exmwhy = mcheck(False, '30084_01')
        out.append(('model_data 同变体时豁免', exm.get('本体附加页有对应') is True,
                    '豁免失效：%s' % ' '.join(exmwhy) if exm.get('本体附加页有对应') is not True
                    else '豁免但仍需在文案里点名，防静默放行'))
        dif, difwhy = mcheck(False, '30084_99')
        out.append(('model_data 换了别的变体仍要拒绝', dif.get('本体附加页有对应') is False,
                    '配对判据过宽，已放行不同变体' if dif.get('本体附加页有对应') is not False
                    else '拦下：%s' % ' '.join(difwhy)))

        def rcheck(case):
            """小人换装闸的对照：同一套盘上文件，只改表里怎么配对、变体 .srcs 在不在。"""
            paths = ['model/30084.scsp', 'model/30084.atlas', 'model/30084.sct',
                     'model/30084_01.scsp', 'model/30084_01.atlas', 'model/30084_01.sct',
                     'model_data/30084.srmd', 'model_data/30084.srcs',
                     'model_data/30084_01.srmd', 'model_data/30084_01.srcs',
                     'model/30134_battle_ready.scsp', 'model/30134_02_battle_ready.scsp',
                     'model_data/30134_battle_ready.brmd', 'model_data/30134_02_battle_ready.brmd']
            md = {'v': '30084_01', 't': 'S', 'e': ['srmd', 'srcs']}
            mp = {'model/30084': {'v': '30084_01', 't': 'L', 'e': ['scsp', 'atlas', 'sct']}}
            if case == 'nopair':
                md = None
            elif case == 'nosrcs':
                md = {'v': '30084_01', 't': 'S', 'e': ['srmd']}
            elif case == 'othervar':
                md = {'v': '30084_02', 't': 'S', 'e': ['srmd', 'srcs']}
            elif case == 'brmdonly':
                mp = {'model/30134_battle_ready': {'v': '30134_02_battle_ready', 't': 'A',
                                                   'e': ['scsp', 'atlas', 'sct']},
                      'model_data/30134_battle_ready': {'v': '30134_02_battle_ready', 't': 'S',
                                                        'e': ['brmd']}}
                md = None
            if md is not None:
                mp['model_data/30084'] = md
            if case == 'srcsmissing':
                paths = [x for x in paths if x != 'model_data/30084_01.srcs']
            p = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-rig.bin')
            got = {}
            io.open(p, 'wb').write(' '.join(paths).encode())
            try:
                manifest_check({'map': mp}, p, lambda nm, ok, why: got.setdefault(nm, (ok, why)))
            finally:
                os.remove(p)
            return got.get('小人换装带同变体命令表', (None, '闸没跑到'))

        for case, nm in (('nopair', '小人换装缺 model_data 配对要拒绝'),
                         ('nosrcs', '只配 srmd 不配 srcs 仍要拒绝'),
                         ('othervar', '配对指向别的变体要拒绝'),
                         ('srcsmissing', '变体 .srcs 不在盘上要拒绝')):
            ok, why = rcheck(case)
            out.append((nm, ok is False, '没拦下来，局内小人会停在摆放姿态'
                        if ok is not False else '拦下：%s' % why))
        ok, why = rcheck('paired')
        out.append(('成对且变体带 srcs 时不误杀', ok is True, ' '.join(
            ['%s=%s' % (ok, why)] if ok is not True else ['30084 这一套放行正常'])))
        ok, why = rcheck('brmdonly')
        out.append(('背身件不要求命令表配对', ok is True, '误杀了 _battle_ready：%s' % why
                    if ok is not True else '按 animation_node 的 _battle_ready 分支放行'))

        def ucheck(same):
            """族与族之间皮肤号是否统一的对照：同一角色两条行，号一致 / 号差一个。"""
            n = '01' if same else '02'
            mp = {'face/character/face_character_40001':
                  {'v': 'face_character_40001_01', 't': 'T', 'e': ['sct']},
                  'face/portrait/40001': {'v': '40001_' + n, 't': 'A', 'e': ['scsp', 'atlas', 'sct']}}
            paths = ['face/character/face_character_40001_01.sct',
                     'face/portrait/40001_01.scsp', 'face/portrait/40001_01.atlas',
                     'face/portrait/40001_01.sct',
                     'face/portrait/40001_02.scsp', 'face/portrait/40001_02.atlas',
                     'face/portrait/40001_02.sct']
            p = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-uniform.bin')
            got = {}
            io.open(p, 'wb').write(' '.join(paths).encode())
            try:
                manifest_check({'map': mp}, p, lambda nm, ok, why: got.setdefault(nm, (ok, why)))
            finally:
                os.remove(p)
            return got.get('同一角色的皮肤号全局一致', (None, '闸没跑到'))

        ok, why = ucheck(False)
        out.append(('同角色两族号不同要拒绝', ok is False,
                    '没拦下来，编队那屏会脸和身体两套皮肤' if ok is not False else '拦下：%s' % why))
        ok, why = ucheck(True)
        out.append(('同角色两族号一致不误杀', ok is True, '误杀：%s' % why if ok is not True else '一致放行'))

        def ccheck(spinename):
            """卡面行顶替目标名的对照：给一个盘上有的名字，再给一个盘上没有的名字。"""
            txt = ('card/known_rig.scsp card/known_rig.atlas card/known_rig.sct '
                   'card_illustration/known_art.sct').encode()
            p = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-cardname.bin')
            got = {}
            io.open(p, 'wb').write(txt)
            try:
                manifest_check({'map': {}, 'rows': {'card': {'c_x': {'spine_model': spinename,
                                                                      'sct_name': 'none'}}}},
                               p, lambda nm, ok, why: got.setdefault(nm, ok))
            finally:
                os.remove(p)
            return got.get('卡面行顶替的目标名可解析')
        out.append(('卡面行目标名盘上没有要拒绝', ccheck('ghost_rig') is False,
                    '放行了一个客户端会请求但不存在的骨架名' if ccheck('ghost_rig') is not False
                    else '拦下 ghost_rig'))
        out.append(('卡面行目标名盘上有则不误杀', ccheck('known_rig') is True,
                    'good=%s' % ccheck('known_rig')))
    except Exception as e:
        out.append(('孤立附加页闸自检', False, '闸自己坏了 %s：%s' % (type(e).__name__, e)))
    return out
