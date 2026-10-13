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
            'rows': len(table['rows'].get('char_base', {})),
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
        add('分层表', True, '%d 条，分 %s；行覆盖 %d 条' % (
            len(table['map']), ' '.join('%s%d' % (k, counts[k]) for k in sorted(counts)),
            len(table['rows'].get('char_base', {}))))
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
    blob = re.findall(rb'[a-z0-9_]+(?:/[a-zA-Z0-9_]+)+\.(?:scsp|atlas|sct|srmd|srcs|srue)', data)
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
    return {'need': len(need), 'missing': miss}


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
             'rows': len(det['table']['table']['rows'].get('char_base', {})),
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
    return out
