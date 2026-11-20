"""Names behind the XML name hashes of the game's reply parser (h = h*131 + c, 32 bits, start 0).

    python tools\\ol-namehash.py 0xfdc2857e 0x1d697e4b ...     which strings of the executable hash to these
    python tools\\ol-namehash.py =response =id                 the hash of a name
"""
import os
import re
import sys

# The prepared game executable, relative to the project root (override with the EBOOT environment variable).
EBOOT = os.environ.get('EBOOT') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                'Prepared', 'AnyPS5', 'CUSA35521', 'eboot.elf')


def h(s):
    v = 0
    for c in s.encode():
        v = (v * 131 + (c - 256 if c > 127 else c)) & 0xffffffff
    return v


def main():
    want = {}
    for a in sys.argv[1:]:
        if a.startswith('='):
            print(f'{a[1:]}: 0x{h(a[1:]):08x}')
        else:
            want[int(a, 16)] = []
    if not want:
        return
    data = open(EBOOT, 'rb').read()
    seen = set()
    for m in re.finditer(rb'[A-Za-z_][A-Za-z0-9_.\-]{0,40}', data):
        s = m.group().decode()
        # every suffix too: names are often the tail of a longer literal
        for i in range(len(s)):
            t = s[i:]
            if t in seen:
                continue
            seen.add(t)
            v = h(t)
            if v in want:
                want[v].append(t)
    for v, names in want.items():
        print(f'0x{v:08x}: {sorted(set(names), key=len)[:12]}')


if __name__ == '__main__':
    main()
