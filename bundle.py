# -*- coding: utf-8 -*-
"""读写 exe 内嵌的明文脚本容器：PE 资源 -> XOR -> ZIP_STORED -> 等长重建。

   只替换资源那一段字节；PE 头、节表、校验和、签名一概不动。
   偏移与长度全部现算，不写死。
"""
import io
import os
import shutil
import zipfile

RESOURCE_XOR = 0x5A
ZIP_MAGIC = b'PK\x03\x04'
PREFIX_SCAN = 4096
PADDING_NAME = 'pre/dummy_padding.bin'
INIT_NAME = 'pre/javascript/init.js'
# 只接管已有的明文脚本层，不替客户端创建这一层
JS_PREFIX = 'pre/javascript/'

_XOR_TABLE = bytes(v ^ RESOURCE_XOR for v in range(256))


class BundleError(RuntimeError):
    """容器层能说清楚的错误：调用方负责翻译成人话，不许抛栈给使用者。"""


def xor(data):
    return data.translate(_XOR_TABLE)


def _walk(node, path):
    for entry in getattr(node, 'entries', ()):
        key = getattr(entry, 'name', None)
        if key is None:
            key = getattr(entry, 'id', None)
        here = path + (key,)
        sub = getattr(entry, 'directory', None)
        if sub is not None:
            for hit in _walk(sub, here):
                yield hit
        data = getattr(entry, 'data', None)
        if data is not None:
            yield here, data


def _open_pe(path):
    try:
        import pefile
    except ImportError:
        raise BundleError('缺 pefile：本模块要靠它定位 PE 资源表。装依赖（requirements.txt）或用打包件。')
    try:
        pe = pefile.PE(path, fast_load=True)
    except Exception as exc:
        raise BundleError('打不开 PE：%s（%s）' % (os.path.basename(path), type(exc).__name__))
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
    except Exception as exc:
        pe.close()
        raise BundleError('资源表解析失败：%s' % type(exc).__name__)
    if not hasattr(pe, 'DIRECTORY_ENTRY_RESOURCE'):
        pe.close()
        raise BundleError('这个 PE 没有资源目录，里面不会有明文脚本层')
    return pe


class Bundle(object):
    """一份 exe 的内嵌容器：只读。"""

    def __init__(self, exe_path):
        if not os.path.isfile(exe_path):
            raise BundleError('文件不存在：%s' % exe_path)
        self.path = exe_path
        self.size = os.path.getsize(exe_path)
        pe = _open_pe(exe_path)
        try:
            leaves = list(_walk(pe.DIRECTORY_ENTRY_RESOURCE, ()))
            hit = lambda x: tuple(x[0][:2]) == (242, 241)
            order = [x for x in leaves if hit(x)] + [x for x in leaves if not hit(x)]
            found = None
            for rpath, rdata in order:
                rva, rsz = rdata.struct.OffsetToData, rdata.struct.Size
                try:
                    blob = xor(pe.get_data(rva, rsz))
                except Exception:
                    continue
                zoff = blob[:PREFIX_SCAN].find(ZIP_MAGIC)
                if zoff < 0:
                    continue
                try:
                    with zipfile.ZipFile(io.BytesIO(blob[zoff:])) as probe:
                        probe.infolist()
                except (OSError, zipfile.BadZipFile):
                    continue
                found = (rpath, rdata, blob, zoff)
                break
            if found is None:
                raise BundleError(
                    '没找到内嵌的明文脚本容器（XOR 0x%02X + ZIP）。这通常是官方纯净件：'
                    '它那一层还是编译后的字节码，本工具不替它创建明文层。' % RESOURCE_XOR)
            rpath, rdata, blob, zoff = found
            self.resource_path = tuple(rpath)
            self.rva = rdata.struct.OffsetToData
            self.resource_size = rdata.struct.Size
            if len(blob) != self.resource_size:
                raise BundleError('资源读出来 %s B，资源表记 %s B，对不上' % (
                    format(len(blob), ','), format(self.resource_size, ',')))
            self.blob = blob
            self.zip_off = zoff
            self.prefix = blob[:zoff]
            self.zip_bytes = blob[zoff:]
            try:
                self.file_offset = pe.get_physical_by_rva(self.rva)
            except Exception:
                raise BundleError('资源 RVA 换算不到文件偏移，这台 exe 的段对齐读不了')
        finally:
            pe.close()
        with zipfile.ZipFile(io.BytesIO(self.zip_bytes)) as z:
            self.entries = list(z.infolist())
            self.archive_comment = z.comment
            self.names = [i.filename for i in self.entries]
            self.js_names = [i.filename for i in self.entries
                             if i.filename.startswith(JS_PREFIX) and i.filename.endswith('.js')]
            if INIT_NAME not in self.names:
                raise BundleError('容器里有 %d 个条目，但没有 %s：脚本层入口对不上' % (
                    len(self.entries), INIT_NAME))
            self.init_bytes = z.read(INIT_NAME)
            self.padding_index = next((n for n, i in enumerate(self.entries)
                                       if i.filename == PADDING_NAME), -1)

    def read(self, name):
        with zipfile.ZipFile(io.BytesIO(self.zip_bytes)) as z:
            return z.read(name)

    def summary(self):
        pad = self.entries[self.padding_index].file_size if self.padding_index >= 0 else 0
        return {'exe_size': self.size, 'resource_path': '/'.join(str(x) for x in self.resource_path),
                'resource_offset': self.file_offset, 'resource_size': self.resource_size,
                'zip_offset': self.zip_off, 'entries': len(self.entries),
                'js_files': len(self.js_names), 'init_js': len(self.init_bytes),
                'padding': pad, 'comment': len(self.archive_comment)}


def derive_format(raw):
    """从条目自身推出行尾/BOM/结尾空行数，不写死表。"""
    bom = raw.startswith(b'\xef\xbb\xbf')
    body = raw[3:] if bom else raw
    nl = b'\r\n' if b'\r\n' in body else b'\n'
    n = 0
    while body.endswith(nl * (n + 1)):
        n += 1
    return nl, bom, n


def to_container_format(text_bytes, fmt):
    nl, bom, tail = fmt
    text = text_bytes.decode('utf-8-sig').replace(b'\r\n'.decode(), '\n').replace('\r', '\n')
    text = text.rstrip('\n') + ('\n' * tail)
    out = text.replace('\n', nl.decode()).encode('utf-8')
    return (b'\xef\xbb\xbf' + out) if bom else out


def _copy_info(source):
    info = zipfile.ZipInfo(source.filename, date_time=source.date_time)
    info.compress_type = zipfile.ZIP_STORED
    info.comment = source.comment
    info.extra = source.extra
    info.create_system = source.create_system
    info.create_version = source.create_version
    info.extract_version = source.extract_version
    info.internal_attr = source.internal_attr
    info.external_attr = source.external_attr
    return info


def _pack(entries, padding_bytes=None, comment=b''):
    """entries: [(ZipInfo, bytes)] 按原顺序；padding_bytes 不为 None 时在末尾加填充条目。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED, allowZip64=True) as z:
        for info, data in entries:
            z.writestr(_copy_info(info), data)
        if padding_bytes is not None:
            pad = zipfile.ZipInfo(PADDING_NAME, date_time=(1980, 1, 1, 0, 0, 0))
            pad.compress_type = zipfile.ZIP_STORED
            z.writestr(pad, padding_bytes)
        z.comment = comment
    return buf.getvalue()


def rebuild(container, new_init_bytes, out_exe, src_exe=None):
    """替换 init.js 并等长重建容器；长度对不上就在写盘之前抛出，不留半件。"""
    src_exe = src_exe or container.path
    if os.path.normcase(os.path.abspath(src_exe)) == os.path.normcase(os.path.abspath(out_exe)):
        raise BundleError('输出不能覆盖输入本身')
    fmt = derive_format(container.init_bytes)
    packed_init = to_container_format(new_init_bytes, fmt)
    entries = []
    with zipfile.ZipFile(io.BytesIO(container.zip_bytes)) as z:
        for info in container.entries:
            if info.filename == PADDING_NAME:
                continue
            data = packed_init if info.filename == INIT_NAME else z.read(info.filename)
            entries.append((info, data))
    dropped_pad = sum(1 for i in container.entries if i.filename == PADDING_NAME)
    base = _pack(entries, comment=container.archive_comment)
    delta = len(container.zip_bytes) - len(base)
    if delta < 0:
        raise BundleError('装不下：新内容比原容器多 %s B，可用余量已经用光' % format(-delta, ','))
    pad_size = 0
    if delta == 0:
        final = base
    else:
        probe = _pack(entries, padding_bytes=b'', comment=container.archive_comment)
        overhead = len(probe) - len(base)
        if overhead <= delta:
            pad_size = delta - overhead
            final = _pack(entries, padding_bytes=b'\x00' * pad_size,
                          comment=container.archive_comment)
        else:
            if len(container.archive_comment) + delta > 0xFFFF:
                raise BundleError('剩余 %d B 既放不下填充条目，也超出 comment 上限' % delta)
            final = _pack(entries, comment=container.archive_comment + b'\x00' * delta)
    if len(final) != len(container.zip_bytes):
        raise BundleError('定长重建失败：原 %s B，新 %s B' % (
            format(len(container.zip_bytes), ','), format(len(final), ',')))
    with zipfile.ZipFile(io.BytesIO(final)) as z:
        bad = z.testzip()
        if bad:
            raise BundleError('重建后的容器自检坏了：%s' % bad)
        if z.read(INIT_NAME) != packed_init:
            raise BundleError('重建后回读 init.js 与送入的不一致')
    blob = container.prefix + final
    if len(blob) != container.resource_size:
        raise BundleError('整段资源长度变了（%s 对 %s），拒绝写盘' % (
            format(len(blob), ','), format(container.resource_size, ',')))
    out_dir = os.path.dirname(os.path.abspath(out_exe))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.abspath(out_exe) + '.tmp'
    try:
        shutil.copy2(src_exe, tmp)
        if os.path.getsize(tmp) != container.size:
            raise BundleError('复制出来的工作件长度就变了，先别写')
        with open(tmp, 'r+b') as fh:
            fh.seek(container.file_offset)
            fh.write(xor(blob))
        os.replace(tmp, out_exe)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return {'init_orig': len(container.init_bytes), 'init_new': len(packed_init),
            'grow': len(packed_init) - len(container.init_bytes),
            'padding_left': pad_size, 'dropped_padding_entries': dropped_pad,
            'entries': len(container.entries), 'container_format': (fmt[0], fmt[1], fmt[2]),
            'zip_size': len(final), 'out_size': os.path.getsize(out_exe)}


def selftest(exe_path):
    """自证：不改内容重建一次，必须与原文件逐字节相同。"""
    c = Bundle(exe_path)
    out_dir = os.path.join(os.environ.get('TEMP', '.') or '.', 'skinrender-selftest')
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, 'roundtrip.exe')
    try:
        rebuild(c, c.init_bytes, tmp)
        with open(exe_path, 'rb') as a, open(tmp, 'rb') as b:
            same = a.read() == b.read()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if not same:
        raise BundleError('原样重建的结果与原文件不同：定长重建工艺与这份 exe 不匹配，别出货')
    return c.summary()
