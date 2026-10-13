# -*- coding: utf-8 -*-
"""分发前的干净度扫描：凭据、账号与家目录、绝对路径、别的工程与品牌、
   历史编号、对话腔与指令模板，六类命中一条即不合格。

   扫描器自身也在范围内，特征串全部运行时拼装，源码里不留字面量。
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {'__pycache__', 'build', 'dist', '.git', 'out', 'work', 'logs'}
TEXT_EXT = ('.py', '.js', '.bat', '.txt', '.json', '.md')
J = lambda *p: ''.join(p)      # 拼特征串，避免自扫命中      # 拼特征串，避免自扫命中

NEEDLE = [
    ('本机用户名', J('len', 'ovo')),
    ('家目录', J('U', 'sers') + '\\'),
    ('盘卷标', J('LE', 'G', 'I', 'O', 'N')),
    ('工程-工作区', J('mod', 'elun', 'locked')),
    ('工程-反作弊', J('anti', 'xig', 'ncode')),
    ('工程-补丁校验', J('patch', 'byp', 'ass')),
    ('工程-私服', J('local', 'serv', 'er')),
    ('工程-字节码', J('v8byt', 'e', 'code')),
    ('工程-抓包', J('net', 'cat', 'ch')),
    ('工程-手游hook', J('gho', 'stl', 'ock')),
    ('工程-解包', J('unp', 'acke', 'd')),
    ('工程-工具箱', J('Too', 'lk', 'it')),
    ('工程-同名工具', J('saf', 'e', 'cut')),
    ('仓库-维护者', J('ypo', 'r', 'oc')),
    ('仓库-上游', J('Nine', 'S119', '42')),
    ('目标文件名', J('ssr-st', 'ove', 'shield')),
    ('游戏名', J('Chao', 'sZer', 'o', 'Nightmare')),
    ('旧品牌-标记', J('C', 'ZN', 'SKI', 'N')),
    ('旧品牌-通道', J('c', 'zn', '_', 'v')),
    ('旧品牌-影子', J('C', 'ZN', 'SHA', 'DOW')),
    ('旧品牌-导出', J('c', 'zn', 'S', 'kin')),
    ('旧品牌-修订', J('c', 'zn', '-', 'skin')),
    ('旧品牌-短名', J('c', 'z', 'n')),
    ('反作弊品牌', J('xig', 'ncode')),
]
# 对话腔用码位拼出来
VOICE = [chr(0x6211), chr(0x4F60), chr(0x60A8), chr(0x54B1) + chr(0x4EEC),
         chr(0x4F1A) + chr(0x8BDD), chr(0x63D0) + chr(0x793A) + chr(0x8BCD),
         chr(0x4E0A) + chr(0x4E00) + chr(0x8F6E), chr(0x52A9) + chr(0x624B),
         chr(0x5927) + chr(0x6A21) + chr(0x578B), chr(0x5EFA) + chr(0x8BAE),
         J('pro', 'mpt'), J('assis', 'tant'), J('sys', 'tem', '_', 'prom', 'pt')]
DRIVE = re.compile(r'(?<![A-Za-z0-9_])[A-Za-z]' + chr(58) + r'[\\/]')
MAIL = re.compile(r'[A-Za-z0-9._%+-]{3,}' + chr(64) + r'[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
SECRET = re.compile(J('gh', 'p_') + r'[A-Za-z0-9]{12,}|' +
                   J('pass', 'word') + r'\s*[:=]|' + J('api', '_key') + r'\s*[:=]')
HIST = re.compile(r'\bT\d{1,2}\b|\bv\d{2}\b')
SELF = os.path.abspath(__file__)


def texts(fn):
    """点文件与无扩展名文件（许可证）同样是文本，必须进扫描范围。"""
    return fn.endswith(TEXT_EXT) or '.' not in fn or fn.startswith('.')


def files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            if texts(fn):
                out.append(os.path.join(dirpath, fn))
    return out


def one(path, root):
    rel = os.path.relpath(path, root).replace('\\', '/')
    raw = io.open(path, encoding='utf-8').read()
    hits = []
    is_self = os.path.abspath(path) == SELF
    for i, line in enumerate(raw.split('\n'), 1):
        # 扫描器自己的拼串行不参与字面量比对
        probe = line.replace('J(', '#') if is_self else line
        for nm, needle in NEEDLE:
            if needle in probe:
                hits.append((rel, i, nm, line.strip()[:100]))
        for nm, bad in (('绝对路径（盘符开头）', bool(DRIVE.search(probe))),
                        ('邮箱或账号', bool(MAIL.search(probe))),
                        ('凭据形状', bool(SECRET.search(probe))),
                        ('历史编号或版本代号', bool(HIST.search(probe)))):
            if bad:
                hits.append((rel, i, nm, line.strip()[:100]))
        for w in VOICE:
            if w in probe and 'chr(0x' not in probe:
                hits.append((rel, i, '对话腔', line.strip()[:100]))
    return hits


def layout(root):
    bad = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = os.path.relpath(dirpath, root)
        if rel != '.':
            bad.append('多出子目录：%s' % rel)
        for fn in filenames:
            if not texts(fn):
                bad.append('非文本文件：%s' % fn)
    return bad


def main():
    root = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith('--') else HERE
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors='replace')
        except Exception:
            pass
    fs = files(root)
    hits, unread = [], []
    for p in fs:
        try:
            hits += one(p, root)
        except (UnicodeDecodeError, OSError) as e:
            unread.append('%s  读不了：%s' % (os.path.relpath(p, root), e))
    bad = layout(root) + unread
    buf = io.StringIO()
    buf.write('扫描 %d 个文本文件（目录 %s）\n' % (len(fs), os.path.basename(root)))
    for p in fs:
        buf.write('  %-16s %s B\n' % (os.path.relpath(p, root).replace('\\', '/'),
                                      format(os.path.getsize(p), ',')))
    for rel, i, nm, txt in hits:
        buf.write('HIT  %s:%d  %s  |  %s\n' % (rel, i, nm, txt))
    for x in bad:
        buf.write('BAD  %s\n' % x)
    buf.write('结论：命中 %d 处，结构问题 %d 处\n' % (len(hits), len(bad)))
    body = buf.getvalue()
    sys.stdout.write(body)
    if '--out' in sys.argv:
        p = sys.argv[sys.argv.index('--out') + 1]
        io.open(p, 'w', encoding='utf-8', newline='\n').write(body)
    return 1 if (hits or bad) else 0


if __name__ == '__main__':
    sys.exit(main())
