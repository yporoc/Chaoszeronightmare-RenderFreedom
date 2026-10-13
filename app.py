# -*- coding: utf-8 -*-
"""图形界面与无界面入口，共用同一条出件管线。

   界面只有：选件、只读检测、执行。执行只认刚刚那次检测。
"""
import glob
import io
import os
import queue
import shutil
import sys
import threading
import traceback
from types import SimpleNamespace

import customtkinter as ctk
from tkinter import filedialog

import engine

BUILD_TAG = 'b2'
COL = {'bg': '#15161a', 'panel': '#1e2026', 'log': '#101116', 'accent': '#0a84ff',
       'ok': '#3fb950', 'err': '#f85149', 'warn': '#d29922', 'fg': '#e6edf3', 'dim': '#8b949e'}


def find_manifest(exe_path):
    """在客户端目录结构里找资源清单，取最新的；没有返回 None。"""
    d = os.path.dirname(os.path.abspath(exe_path))
    cand = []
    for pat in (os.path.join(d, 'appdata', '*', 'gameres', 'manifest.ssra'),
                os.path.join(d, 'gameres', 'manifest.ssra'),
                os.path.join(d, os.pardir, 'appdata', '*', 'gameres', 'manifest.ssra')):
        cand += [p for p in glob.glob(pat) if os.path.isfile(p)]
    return max(cand, key=os.path.getmtime) if cand else None


class App(ctk.CTk):

    def __init__(self, interactive=True):
        super().__init__()
        self.title('皮肤渲染出件 · %s' % BUILD_TAG)
        self.geometry('940x600')
        self.minsize(780, 460)
        self.configure(fg_color=COL['bg'])
        ctk.set_appearance_mode('dark')
        self.q = queue.Queue()
        self.busy = False
        self.det = None                 # (路径, 大小, mtime)：执行只认这份检测结果
        self.interactive = interactive
        self.buffer = []                # 无界面模式一次性落盘的内容
        self.last_report = ''
        self.table_path = engine.res('table.json')
        self.payload_path = engine.res('payload.js')

        body = ctk.CTkFrame(self, fg_color='transparent')
        body.pack(fill='both', expand=True, padx=12, pady=10)
        self.log = ctk.CTkTextbox(body, font=('Consolas', 13), fg_color=COL['log'],
                                  text_color=COL['fg'], corner_radius=10, wrap='word')
        self.log.pack(side='left', fill='both', expand=True)
        self._tags()

        panel = ctk.CTkFrame(body, fg_color=COL['panel'], width=310, corner_radius=10)
        panel.pack(side='right', fill='y', padx=(12, 0))
        panel.pack_propagate(False)
        ctk.CTkLabel(panel, text='源文件', text_color=COL['dim'], anchor='w',
                     font=('Segoe UI', 12)).pack(fill='x', padx=14, pady=(16, 2))
        row = ctk.CTkFrame(panel, fg_color='transparent')
        row.pack(fill='x', padx=12)
        self.src = ctk.CTkEntry(row, text_color=COL['fg'], fg_color='#0c0d11',
                                border_color='#33343c')
        self.src.pack(side='left', fill='x', expand=True)
        ctk.CTkButton(row, text='浏览', width=56, height=26, fg_color='#2b2c34',
                      hover_color='#35363f', text_color=COL['fg'],
                      command=self.pick).pack(side='right', padx=(6, 0))

        self.btn_detect = ctk.CTkButton(panel, text='只读检测', height=42, fg_color='#2b2c34',
                                        hover_color='#35363f', text_color=COL['fg'],
                                        font=('Segoe UI', 14),
                                        command=lambda: self.go(self.do_detect))
        self.btn_detect.pack(fill='x', padx=14, pady=(20, 6))
        self.btn_build = ctk.CTkButton(panel, text='执行', height=50, fg_color=COL['accent'],
                                       hover_color='#0672d9', text_color='#fff',
                                       font=('Segoe UI', 16, 'bold'),
                                       command=lambda: self.go(self.do_build))
        self.btn_build.pack(fill='x', padx=14, pady=(0, 12))
        ctk.CTkButton(panel, text='打开输出所在文件夹', height=28, fg_color='transparent',
                      hover_color='#2b2c34', border_color='#33343c', border_width=1,
                      text_color=COL['dim'], font=('Segoe UI', 11),
                      command=self.open_out).pack(fill='x', padx=14)

        self.status = ctk.CTkLabel(self, text='就绪：选择 exe，点“只读检测”', anchor='w',
                                   text_color=COL['fg'], fg_color=COL['panel'], height=30,
                                   corner_radius=0)
        self.status.pack(fill='x', side='bottom')
        self.after(60, self.pump)

    # ---------- 通用 ----------
    def _tags(self):
        tb = getattr(self.log, '_textbox', None)
        if tb is not None:
            for tag, c in (('ok', COL['ok']), ('err', COL['err']), ('warn', COL['warn']),
                           ('dim', COL['dim'])):
                tb.tag_configure(tag, foreground=c)

    def _tb(self):
        return getattr(self.log, '_textbox', None)

    def say(self, s, col=None):
        tb = self._tb()
        if tb is None:
            self.log.insert('end', s + '\n')
        else:
            tag = {COL['ok']: 'ok', COL['err']: 'err', COL['warn']: 'warn', COL['dim']: 'dim'}.get(col)
            tb.insert('end', s + '\n', (tag,) if tag else ())
        self.log.see('end')

    def emit(self, s, col=None):
        s = str(s)
        self.buffer.append(s)
        self.q.put(('line', (s, col)))

    def head(self, s):
        self.emit('', COL['dim'])
        self.emit('── %s ' % s + '─' * max(2, 46 - len(s)), COL['dim'])

    def pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'line':
                    txt, col = payload
                    self.say(txt, col)
                else:
                    self.busy = False
                    self.status.configure(text=payload[0], text_color=payload[1])
        except queue.Empty:
            pass
        self.after(60, self.pump)

    def go(self, fn):
        if self.busy:
            self.status.configure(text='正在执行，请稍候', text_color=COL['warn'])
            return
        self.busy = True
        self.status.configure(text='执行中…', text_color=COL['warn'])
        snap = self.snapshot()                       # 主线程取值，工作线程不碰控件
        threading.Thread(target=self.worker, args=(fn, snap), daemon=True).start()

    def worker(self, fn, snap):
        ok, msg = False, '没跑起来'
        try:
            ok, msg = fn(snap)
        except engine.EngineError as e:
            self.emit('拒绝：%s' % e, COL['err'])
            msg = '失败：%s' % str(e)[:60]
        except Exception as e:
            self.emit('异常：%r' % e, COL['err'])
            self.dump_trace()
            msg = '异常退出（现场已记入 %s）' % self.last_report
        finally:
            self.last_report = self.flush_report('gui')
            self.emit('（本轮记录已写入 %s）' % self.last_report, COL['dim'])
            self.q.put(('done', (msg, COL['ok'] if ok else COL['err'])))

    def dump_trace(self):
        self.buffer.append(traceback.format_exc())

    def flush_report(self, tag):
        p = engine.report_path(tag)
        try:
            io.open(p, 'w', encoding='utf-8', newline='\n').write('\n'.join(self.buffer) + '\n')
        except OSError:
            p = '（日志写不下去：%s）' % os.path.basename(p)
        return p

    # ---------- 输入 ----------
    def pick(self):
        p = filedialog.askopenfilename(title='选择要加工的 exe',
                                       filetypes=[('exe', '*.exe'), ('所有文件', '*.*')])
        if p:
            self.src.delete(0, 'end')
            self.src.insert(0, p)
            self.det = None
            self.status.configure(text='已选件，点“只读检测”', text_color=COL['fg'])

    def open_out(self):
        path = self.src.get().strip().strip('"')
        d = engine.default_out(path) if path else engine.app_dir()
        d = os.path.dirname(os.path.abspath(d))
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
        except Exception as e:
            self.emit('打不开文件夹：%r' % e, COL['warn'])

    def snapshot(self):
        """控件只能在主线程读：点按钮时拍快照交给工作线程。"""
        return SimpleNamespace(src=self.src.get().strip().strip('"'),
                               man=None, ver=engine.DEFAULT_VERSION, out=None)

    def inputs(self, snap):
        if not snap.src:
            raise engine.EngineError('还没选要加工的 exe')
        return os.path.abspath(snap.src)

    def guard(self, snap):
        """执行前两道闸：刚检测过，且文件没被动过。"""
        ap = self.inputs(snap)
        if not self.det or self.det[0] != ap:
            raise engine.EngineError('先做一次只读检测；换过文件或文件被改动过，检测就作废')
        if os.path.getsize(ap) != self.det[1] or int(os.path.getmtime(ap)) != self.det[2]:
            raise engine.EngineError('这个文件在检测之后变过了，重新检测再来')
        return ap

    # ---------- ① 只读检测 ----------
    def do_detect(self, snap):
        path = self.inputs(snap)
        man = snap.man or find_manifest(path)
        snap.man = man
        self.head('检测（只读，不写任何文件）')
        det = engine.detect(path, self.table_path, self.payload_path, man,
                            log=lambda x: self.emit(x), version=snap.ver)
        for nm, ok, why in det['rows']:
            self.emit('%-6s %-22s %s' % ('在' if ok else '缺', nm, str(why)[:110]),
                      COL['fg'] if ok else COL['err'])
        if det['ok']:
            self.det = (os.path.abspath(path), det['size'], int(os.path.getmtime(path)))
            return True, '检测通过，可以执行'
        self.det = None
        return False, '检测没过 %d 项' % sum(1 for r in det['rows'] if not r[1])

    # ---------- ② 执行出件 ----------
    def do_build(self, snap, out=None):
        ap = self.guard(snap)
        out = out or snap.out or engine.default_out(ap)
        need = os.path.getsize(ap) + (4 << 20)
        free = shutil.disk_usage(engine.app_dir()).free
        if free < need:
            raise engine.EngineError('本工具所在磁盘只剩 %s B，写不下这份 %s B 的件' % (
                format(free, ','), format(os.path.getsize(ap), ',')))
        self.head('执行')
        try:
            res = engine.produce(ap, out, self.table_path, self.payload_path,
                                 version=snap.ver, manifest=snap.man or find_manifest(ap),
                                 log=self.emit)
        except PermissionError as e:
            raise engine.EngineError('输出文件写不动（%s）：多半是上一次的结果还开着；'
                                      '关掉占用它的程序再重试，本次未留下半成品' % e.__class__.__name__)
        pf = res['proof']
        self.head('结果')
        self.emit('产物    %s' % out)
        self.emit('        %s B    sha256 %s' % (format(pf['out_size'], ','), pf['out_sha256']))
        self.emit('输入    未改动（sha / 大小 / 修改时间一致）')
        self.emit('分层    %s  行覆盖 %d 条  版本 %s' % (
            str(pf['entries']), pf['rows'], pf['version']))
        self.emit('自证    %d 项全过  用时 %ss' % (len(pf['checks']), pf['seconds']))
        self.emit('证明    %s' % os.path.basename(out + '.proof.json'))
        self.emit('装机    产物需自行放到目标位置后启动（本工具不改动任何既有目录）')
        return True, '完成：%s' % os.path.basename(out)

    # ---------- ③ 闸门自测 ----------
    def do_gate(self, snap=None):
        self.head('闸门自测（负向对照，不写文件）')
        rows = engine.gate_selftest(self.table_path, self.payload_path)
        bad = 0
        for nm, ok, why in rows:
            bad += 0 if ok else 1
            self.emit('%-6s %-24s %s' % ('拦住' if ok else '漏了', nm, str(why)[:80]),
                      COL['fg'] if ok else COL['err'])
        self.head('沙箱断言')
        try:
            n, txt = engine.node_gate(engine.res('dryrun.js'), self.table_path,
                                      self.payload_path, self.emit)
            self.emit('沙箱断言 %d 条全过' % n)
        except engine.EngineError as e:
            self.emit(str(e))
            return False, '闸门自测没过：沙箱断言红灯'
        if bad:
            return False, '闸门自测没过：%d 道闸没拦住坏输入' % bad
        return True, '闸门自测通过（%d 道 + 沙箱 %d 条）' % (len(rows), n)


def run_headless(mode, argv):
    """无界面入口：结果既打印也落盘，退出码即结论。"""
    app = App(interactive=False)
    flag = '--' + mode
    rest = argv[argv.index(flag) + 1:]
    pos = [x for x in rest if not x.startswith('--')]
    for key, attr in (('--table', 'table_path'), ('--payload', 'payload_path')):
        if key in rest:
            setattr(app, attr, os.path.abspath(rest[rest.index(key) + 1]))
    app.update()
    if mode == 'selfcheck':
        txt = 'RESULT OK | python=%s frozen=%s ctk=%s stdout是None=%s node=%s 目录=%s 表=%d 条 %d 行\n' % (
            sys.version.split()[0], bool(getattr(sys, 'frozen', False)),
            getattr(ctk, '__version__', '?'), sys.stdout is None,
            '有' if shutil.which('node') else '无', engine.app_dir(),
            len(engine.load_table(engine.res('table.json'))[1]['map']),
            engine.load_table(engine.res('table.json'))[1]['meta']['rows'])
        try:
            print(txt)
        except Exception:
            pass
        io.open(engine.report_path('selfcheck'), 'w', encoding='utf-8').write(txt)
        app.destroy()
        return 0
    try:
        if mode == 'gate':
            ok, msg = app.do_gate()
        else:
            if not pos:
                app.emit('RED 缺参数：%s 后面要跟一个 exe 路径' % flag)
                return 2
            app.src.insert(0, pos[0])
            snap = app.snapshot()
            if '--manifest' in rest:
                snap.man = os.path.abspath(rest[rest.index('--manifest') + 1])
            ok, msg = app.do_detect(snap)
            if ok and mode == 'build':
                if len(pos) > 1:
                    snap.out = os.path.abspath(pos[1])
                ok, msg = app.do_build(snap)
    except Exception as e:
        app.emit('异常：%r' % e)
        app.dump_trace()
        ok, msg = False, '异常'
    app.pump()
    app.update()
    txt = '\n'.join(app.buffer) + '\nRESULT %s | %s\n' % ('OK' if ok else 'RED', msg)
    try:
        print(txt)
    except Exception:
        pass
    io.open(engine.report_path(mode), 'w', encoding='utf-8', newline='\n').write(txt)
    app.destroy()
    return 0 if ok else 1


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors='replace')
        except Exception:
            pass
    try:
        engine.require_assets()
    except engine.EngineError as e:
        sys.stderr.write('%s\n' % e)
        try:
            if sys.stdout is not None:
                from tkinter import messagebox, Tk
                Tk().withdraw()
                messagebox.showerror('这份件不完整', str(e))
        except Exception:
            pass
        return 3
    argv = sys.argv
    for mode in ('selfcheck', 'analyze', 'build', 'gate'):
        if '--%s' % mode in argv:
            return run_headless(mode, argv)
    App().mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
