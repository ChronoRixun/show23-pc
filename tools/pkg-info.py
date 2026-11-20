"""Header and file table of a PS4 package, read-only.

    python tools\\pkg-info.py <file.pkg>

Prints what the unencrypted header says (content id, type flags, sizes) and the table of the
small files in front of the game data (param.sfo and the like), and the title, version and
category from param.sfo if it is readable. Nothing is written or decrypted.
"""
import struct
import sys


def main():
    path = sys.argv[1]
    with open(path, 'rb') as f:
        head = f.read(0x1000)
        magic, flags = struct.unpack_from('>II', head, 0)
        print(f'magic 0x{magic:08x} ({"PS4 package" if magic == 0x7f434e54 else "not a PS4 package"}), type flags 0x{flags:08x}')
        entry_count, = struct.unpack_from('>I', head, 0x10)
        table_offset, = struct.unpack_from('>I', head, 0x18)
        body_offset, body_size = struct.unpack_from('>QQ', head, 0x20)
        content_offset, content_size = struct.unpack_from('>QQ', head, 0x30)
        content_id = head[0x40:0x64].split(b'\0')[0].decode('latin-1')
        drm_type, content_type, content_flags = struct.unpack_from('>III', head, 0x70)
        pfs_offset, pfs_size = struct.unpack_from('>QQ', head, 0x410)
        print(f'content id {content_id}')
        print(f'drm type {drm_type}, content type 0x{content_type:x}, content flags 0x{content_flags:08x}')
        print(f'entries {entry_count}, table at 0x{table_offset:x}; body 0x{body_offset:x}+0x{body_size:x}; '
              f'game data (pfs image) at 0x{pfs_offset:x}, {pfs_size / 2**30:.2f} GB')
        f.seek(table_offset)
        table = f.read(entry_count * 32)
        names = {0x1000: 'param.sfo', 0x1001: 'playgo-chunk.dat', 0x1002: 'playgo-chunk.sha', 0x1003: 'playgo-manifest.xml',
                 0x1004: 'pronunciation.xml', 0x1006: 'pic1.png', 0x1200: 'icon0.png', 0x1220: 'pic0.png', 0x1240: 'snd0.at9',
                 0x1260: 'changeinfo/changeinfo.xml', 0x400: 'license.dat', 0x401: 'license.info', 0x402: 'nptitle.dat',
                 0x403: 'npbind.dat', 0x409: 'psreserved.dat', 0x100: 'digests', 0x10: 'entry keys', 0x20: 'image key', 0x80: 'general digests', 0x200: 'entry names'}
        sfo = None
        for i in range(entry_count):
            eid, name_off, f1, f2, off, size = struct.unpack_from('>IIIIII', table, i * 32)
            enc = ' encrypted' if f1 & 0x80000000 else ''
            print(f'  entry 0x{eid:04x} {names.get(eid, ""):28s} offset 0x{off:x} size {size}{enc}')
            if eid == 0x1000:
                sfo = (off, size)
        if sfo:
            f.seek(sfo[0])
            data = f.read(sfo[1])
            if data[:4] == b'\0PSF':
                key_off, data_off, count = struct.unpack_from('<III', data, 8)
                for i in range(count):
                    ko, fmt, ln, mx, do = struct.unpack_from('<HHIII', data, 0x14 + i * 16)
                    key = data[key_off + ko:data.index(b'\0', key_off + ko)].decode()
                    raw = data[data_off + do:data_off + do + ln]
                    val = struct.unpack('<I', raw)[0] if fmt == 0x404 else raw.split(b'\0')[0].decode('utf-8', 'replace')
                    if key in ('TITLE', 'TITLE_ID', 'APP_VER', 'VERSION', 'CATEGORY', 'CONTENT_ID', 'PUBTOOLINFO', 'SYSTEM_VER'):
                        print(f'  param.sfo {key} = {val if not isinstance(val, int) else hex(val)}')
            else:
                print('  param.sfo is not readable in the clear')


if __name__ == '__main__':
    main()
