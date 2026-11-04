"""Read a Compound File Binary (OLE2) container, the format of .PcbDoc/.PcbLib.

Everything the writer (cfb_write.py) needs to rebuild the file is kept: each
directory entry's raw fields (name, type, colour, siblings, child, CLSID, state
bits, timestamps) and the header fields. Only sector numbers and sizes change
on a rewrite.

    cf = CompoundFile.open(path)
    cf.read("Tracks6/Data")          # bytes
    cf.listdir()                     # ["Arcs6/Data", "Arcs6/Header", ...]
"""

import struct
from pathlib import Path

MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
NOSTREAM = 0xFFFFFFFF

TYPE_EMPTY, TYPE_STORAGE, TYPE_STREAM, TYPE_ROOT = 0, 1, 2, 5


class CFBError(ValueError):
    pass


class Entry:
    """One 128-byte directory entry."""

    __slots__ = ("sid", "name", "type", "color", "left", "right", "child", "clsid",
                 "state", "ctime", "mtime", "start", "size", "children", "parent", "path")

    def __init__(self, sid, raw):
        self.sid = sid
        name_len = struct.unpack_from("<H", raw, 0x40)[0]
        self.name = raw[:max(0, name_len - 2)].decode("utf-16-le", errors="replace")
        self.type = raw[0x42]
        self.color = raw[0x43]
        self.left, self.right, self.child = struct.unpack_from("<III", raw, 0x44)
        self.clsid = raw[0x50:0x60]
        self.state = struct.unpack_from("<I", raw, 0x60)[0]
        self.ctime = raw[0x64:0x6C]
        self.mtime = raw[0x6C:0x74]
        self.start = struct.unpack_from("<I", raw, 0x74)[0]
        self.size = struct.unpack_from("<Q", raw, 0x78)[0]
        self.children = []
        self.parent = None
        self.path = ""

    def __repr__(self):
        return f"Entry({self.sid}, {self.path!r}, type={self.type}, size={self.size})"


class CompoundFile:
    def __init__(self, data, source=None):
        self.data = data
        self.source = source
        self._parse()

    @classmethod
    def open(cls, path):
        path = Path(path)
        return cls(path.read_bytes(), source=path)

    # ------------------------------------------------------------ parsing
    def _parse(self):
        d = self.data
        if len(d) < 512 or d[:8] != MAGIC:
            raise CFBError("not a compound file (bad signature)")
        (self.minor_version, self.major_version, byte_order, sector_shift,
         mini_shift) = struct.unpack_from("<HHHHH", d, 0x18)
        if byte_order != 0xFFFE:
            raise CFBError("unexpected byte order mark")
        if self.major_version not in (3, 4):
            raise CFBError(f"unsupported major version {self.major_version}")
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_shift
        (self.n_dir_sectors, self.n_fat_sectors, self.first_dir, self.transaction,
         self.mini_cutoff, self.first_minifat, self.n_minifat, self.first_difat,
         self.n_difat) = struct.unpack_from("<IIIIIIIII", d, 0x28)
        self.header_clsid = d[0x08:0x18]

        difat = list(struct.unpack_from("<109I", d, 0x4C))
        sec = self.first_difat
        seen = set()
        per = self.sector_size // 4 - 1
        for _ in range(self.n_difat):
            if sec in (ENDOFCHAIN, FREESECT) or sec in seen:
                break
            seen.add(sec)
            vals = struct.unpack_from(f"<{per + 1}I", d, self._offset(sec))
            difat.extend(vals[:per])
            sec = vals[per]
        fat_sectors = [s for s in difat if s not in (FREESECT, ENDOFCHAIN)][:self.n_fat_sectors]
        if len(fat_sectors) != self.n_fat_sectors:
            raise CFBError(f"DIFAT lists {len(fat_sectors)} FAT sectors, header says "
                           f"{self.n_fat_sectors}")
        self.fat_sectors = fat_sectors
        n = self.sector_size // 4
        fat = []
        for s in fat_sectors:
            fat.extend(struct.unpack_from(f"<{n}I", d, self._offset(s)))
        self.fat = fat

        dir_bytes = self._chain_bytes(self.first_dir, self.fat, self.sector_size, self._sector)
        self.entries = [Entry(i, dir_bytes[i * 128:(i + 1) * 128])
                        for i in range(len(dir_bytes) // 128)]
        if not self.entries or self.entries[0].type != TYPE_ROOT:
            raise CFBError("first directory entry is not the root")
        self.root = self.entries[0]

        if self.n_minifat and self.first_minifat not in (ENDOFCHAIN, FREESECT):
            mf = self._chain_bytes(self.first_minifat, self.fat, self.sector_size, self._sector)
            self.minifat = list(struct.unpack(f"<{len(mf) // 4}I", mf))
        else:
            self.minifat = []
        if self.root.start not in (ENDOFCHAIN, FREESECT) and self.root.size:
            self.ministream = self._chain_bytes(self.root.start, self.fat, self.sector_size,
                                                self._sector)[:self.root.size]
        else:
            self.ministream = b""

        self._build_tree()

    def _offset(self, sector):
        return (sector + 1) * self.sector_size

    def _sector(self, sector):
        off = self._offset(sector)
        if off + self.sector_size > len(self.data) + self.sector_size:
            raise CFBError(f"sector {sector} lies past the end of the file")
        return self.data[off:off + self.sector_size]

    def _mini_sector(self, sector):
        off = sector * self.mini_sector_size
        return self.ministream[off:off + self.mini_sector_size]

    @staticmethod
    def _chain(start, table):
        out, sec, seen = [], start, set()
        while sec not in (ENDOFCHAIN, FREESECT):
            if sec in seen or sec >= len(table):
                raise CFBError(f"broken sector chain at {sec}")
            seen.add(sec)
            out.append(sec)
            sec = table[sec]
        return out

    def _chain_bytes(self, start, table, size, getter):
        return b"".join(getter(s) for s in self._chain(start, table))

    def _build_tree(self):
        def walk(sid, parent, depth=0):
            stack, node, out = [], sid, []
            while stack or node != NOSTREAM:
                while node != NOSTREAM:
                    if node >= len(self.entries) or len(stack) > len(self.entries):
                        raise CFBError("directory tree is corrupt")
                    stack.append(node)
                    node = self.entries[node].left
                node = stack.pop()
                out.append(node)
                node = self.entries[node].right
            for c in out:
                e = self.entries[c]
                e.parent = parent
                e.path = (parent.path + "/" if parent.path else "") + e.name
                parent.children.append(e)
                if e.type in (TYPE_STORAGE, TYPE_ROOT) and e.child != NOSTREAM:
                    walk(e.child, e, depth + 1)

        self.root.path = ""
        if self.root.child != NOSTREAM:
            walk(self.root.child, self.root)
        self.by_path = {}
        for e in self.entries:
            if e.path:
                self.by_path[e.path.lower()] = e

    # ------------------------------------------------------------ access
    def entry(self, path):
        e = self.by_path.get(path.strip("/").lower())
        if e is None:
            raise KeyError(path)
        return e

    def exists(self, path):
        return path.strip("/").lower() in self.by_path

    def read(self, path):
        e = self.entry(path) if isinstance(path, str) else path
        if e.type != TYPE_STREAM:
            raise CFBError(f"{e.path} is not a stream")
        return self.read_entry(e)

    def read_entry(self, e):
        size = e.size if self.major_version == 4 else e.size & 0xFFFFFFFF
        if size == 0:
            return b""
        if size < self.mini_cutoff:
            raw = b"".join(self._mini_sector(s) for s in self._chain(e.start, self.minifat))
        else:
            raw = self._chain_bytes(e.start, self.fat, self.sector_size, self._sector)
        if len(raw) < size:
            raise CFBError(f"stream {e.path} is truncated ({len(raw)} < {size} bytes)")
        return raw[:size]

    def listdir(self, streams=True, storages=False):
        out = []
        for e in self.entries:
            if not e.path:
                continue
            if (streams and e.type == TYPE_STREAM) or (storages and e.type == TYPE_STORAGE):
                out.append(e.path)
        return sorted(out)

    def streams(self):
        """{path: bytes} for every stream."""
        return {e.path: self.read_entry(e) for e in self.entries
                if e.path and e.type == TYPE_STREAM}

    def storages(self):
        return [e.path for e in self.entries if e.path and e.type == TYPE_STORAGE]
