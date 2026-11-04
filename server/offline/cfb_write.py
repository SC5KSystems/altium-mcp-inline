"""Write a Compound File Binary (OLE2) container.

    root = tree_from(CompoundFile.open(src))     # logical tree, streams as bytes
    root.find("Tracks6/Data").data = new_bytes   # change, add or remove entries
    data = to_bytes(root, like=cf)               # FAT, mini FAT, DIFAT, directory

When the set of entries is the same as the source's, the source directory is
reused entry for entry: names, CLSIDs, state bits, timestamps, colours and the
sibling tree all stay; only start sectors and sizes change. A storage whose
children changed gets a new sibling tree, ordered by the CFB name rule
(shorter first, then case-insensitive) and coloured as a valid red-black tree.
"""

import math
import struct
from pathlib import Path

from .cfb import (ENDOFCHAIN, FREESECT, FATSECT, DIFSECT, NOSTREAM, TYPE_EMPTY,
                  TYPE_ROOT, TYPE_STORAGE, TYPE_STREAM, CFBError, MAGIC)

ZERO16 = b"\0" * 16
ZERO8 = b"\0" * 8
RED, BLACK = 0, 1


class DirNode:
    def __init__(self, name, type_, data=b"", clsid=ZERO16, state=0, ctime=ZERO8, mtime=ZERO8,
                 orig=None):
        self.name = name
        self.type = type_
        self.data = data
        self.clsid = clsid
        self.state = state
        self.ctime = ctime
        self.mtime = mtime
        self.orig = orig          # source Entry, when there was one
        self.children = []        # storages and root only

    def child(self, name):
        for c in self.children:
            if c.name.upper() == name.upper():
                return c
        return None

    def find(self, path):
        node = self
        for part in path.strip("/").split("/"):
            node = node.child(part) if node is not None else None
        return node

    def ensure_storage(self, name):
        c = self.child(name)
        if c is None:
            c = DirNode(name, TYPE_STORAGE)
            self.children.append(c)
        return c

    def set_stream(self, path, data):
        parts = path.strip("/").split("/")
        node = self
        for p in parts[:-1]:
            node = node.ensure_storage(p)
        c = node.child(parts[-1])
        if c is None:
            c = DirNode(parts[-1], TYPE_STREAM)
            node.children.append(c)
        c.data = bytes(data)
        return c

    def remove(self, path):
        parts = path.strip("/").split("/")
        parent = self.find("/".join(parts[:-1])) if len(parts) > 1 else self
        if parent is None:
            return False
        before = len(parent.children)
        parent.children = [c for c in parent.children if c.name.upper() != parts[-1].upper()]
        return len(parent.children) != before

    def walk(self, prefix=""):
        for c in self.children:
            path = f"{prefix}{c.name}"
            yield path, c
            if c.type == TYPE_STORAGE:
                yield from c.walk(path + "/")


def tree_from(cf):
    """Logical tree of a CompoundFile, every stream read into memory."""
    def make(e):
        n = DirNode(e.name, e.type, b"", e.clsid, e.state, e.ctime, e.mtime, orig=e)
        if e.type == TYPE_STREAM:
            n.data = cf.read_entry(e)
        for c in e.children:
            n.children.append(make(c))
        return n
    root = make(cf.root)
    root.data = b""
    return root


def name_key(name):
    return (len(name), name.upper())


def _topology(node):
    return tuple(sorted(name_key(c.name) for c in node.children))


def _orig_topology(entry):
    return tuple(sorted(name_key(c.name) for c in entry.children))


def _can_preserve(root, cf):
    if cf is None or root.orig is None:
        return False
    stack = [root]
    seen = set()
    while stack:
        n = stack.pop()
        if n.orig is None or n.orig.type != n.type:
            return False
        if n.type in (TYPE_STORAGE, TYPE_ROOT):
            if _topology(n) != _orig_topology(n.orig):
                return False
            for c in n.children:
                if c.orig is None or c.orig.parent is not n.orig:
                    return False
                stack.append(c)
        seen.add(id(n.orig))
    return True


def _balanced(nodes):
    """Sibling tree for one storage: (root index, {i: (left, right, color)})."""
    nodes = sorted(nodes, key=lambda n: name_key(n.name))
    links = {}
    full = int(math.floor(math.log2(len(nodes) + 1))) if nodes else 0

    def build(lo, hi, depth):
        if lo > hi:
            return None
        mid = (lo + hi) // 2
        left = build(lo, mid - 1, depth + 1)
        right = build(mid + 1, hi, depth + 1)
        links[mid] = (left, right, RED if depth >= full else BLACK)
        return mid
    rootpos = build(0, len(nodes) - 1, 0)
    return nodes, rootpos, links


class _Dir:
    __slots__ = ("node", "left", "right", "child", "color", "start", "size")

    def __init__(self, node):
        self.node = node
        self.left = self.right = self.child = NOSTREAM
        self.color = BLACK
        self.start = 0
        self.size = 0


def _directory(root, cf, preserve):
    """[_Dir or None (unused slot)] indexed by sid."""
    if preserve:
        out = [None] * len(cf.entries)
        stack = [root]
        while stack:
            n = stack.pop()
            e = n.orig
            d = _Dir(n)
            d.left, d.right, d.child, d.color = e.left, e.right, e.child, e.color
            out[e.sid] = d
            stack.extend(n.children)
        return out
    out = [_Dir(root)]
    root_d = out[0]

    def place(parent_d, parent):
        if not parent.children:
            parent_d.child = NOSTREAM
            return
        nodes, rootpos, links = _balanced(parent.children)
        base = len(out)
        ds = [_Dir(n) for n in nodes]
        out.extend(ds)
        for i, d in enumerate(ds):
            left, right, color = links[i]
            d.left = NOSTREAM if left is None else base + left
            d.right = NOSTREAM if right is None else base + right
            d.color = color
        parent_d.child = base + rootpos
        for d in ds:
            if d.node.type == TYPE_STORAGE:
                place(d, d.node)

    root_d.color = BLACK
    place(root_d, root)
    return out


def to_bytes(root, like=None, major=None, mini_cutoff=None, preserve=True):
    """Serialize a DirNode tree. `like` (a CompoundFile) supplies header fields
    and, when the entry set is unchanged, the directory to reuse."""
    major = major or (like.major_version if like else 3)
    if major not in (3, 4):
        raise CFBError(f"cannot write major version {major}")
    ssz = 512 if major == 3 else 4096
    msz = 64
    cutoff = mini_cutoff or (like.mini_cutoff if like else 4096)
    minor = like.minor_version if like else 0x3E
    transaction = like.transaction if like else 0
    header_clsid = like.header_clsid if like else ZERO16

    keep = preserve and _can_preserve(root, like)
    dirs = _directory(root, like, keep)

    sectors = []
    fat = []

    def alloc(data, step=ssz):
        if not data:
            return ENDOFCHAIN
        n = (len(data) + ssz - 1) // ssz
        start = len(sectors)
        for i in range(n):
            chunk = data[i * ssz:(i + 1) * ssz]
            sectors.append(chunk + b"\0" * (ssz - len(chunk)))
            fat.append(start + i + 1 if i < n - 1 else ENDOFCHAIN)
        return start

    mini = bytearray()
    minifat = []
    for d in dirs:
        if d is None or d.node.type != TYPE_STREAM:
            continue
        data = d.node.data
        d.size = len(data)
        if not data:
            d.start = ENDOFCHAIN
        elif len(data) < cutoff:
            n = (len(data) + msz - 1) // msz
            start = len(mini) // msz
            mini += data + b"\0" * (n * msz - len(data))
            for i in range(n):
                minifat.append(start + i + 1 if i < n - 1 else ENDOFCHAIN)
            d.start = start
        else:
            d.start = alloc(data)
    root_d = dirs[0]
    root_d.start = alloc(bytes(mini)) if mini else ENDOFCHAIN
    root_d.size = len(mini)
    mf_bytes = struct.pack(f"<{len(minifat)}I", *minifat) if minifat else b""
    if mf_bytes and len(mf_bytes) % ssz:
        mf_bytes += struct.pack("<I", FREESECT) * ((ssz - len(mf_bytes) % ssz) // 4)
    first_minifat = alloc(mf_bytes) if mf_bytes else ENDOFCHAIN
    n_minifat = len(mf_bytes) // ssz

    per_dir = ssz // 128
    n_slots = len(dirs)
    n_slots = ((n_slots + per_dir - 1) // per_dir) * per_dir
    dir_bytes = bytearray()
    for i in range(n_slots):
        d = dirs[i] if i < len(dirs) else None
        dir_bytes += _entry_bytes(d, major)
    first_dir = alloc(bytes(dir_bytes))
    n_dir = len(dir_bytes) // ssz

    per_fat = ssz // 4
    base = len(sectors)
    n_fat = n_difat = 0
    while True:
        n_difat = max(0, math.ceil((n_fat - 109) / (per_fat - 1)))
        need = math.ceil((base + n_fat + n_difat) / per_fat)
        if need <= n_fat:
            break
        n_fat = need
    fat_secs = list(range(base, base + n_fat))
    difat_secs = list(range(base + n_fat, base + n_fat + n_difat))
    fat.extend([FATSECT] * n_fat)
    fat.extend([DIFSECT] * n_difat)
    fat.extend([FREESECT] * (n_fat * per_fat - len(fat)))
    for i in range(n_fat):
        sectors.append(struct.pack(f"<{per_fat}I", *fat[i * per_fat:(i + 1) * per_fat]))
    rest = fat_secs[109:]
    for k in range(n_difat):
        chunk = rest[k * (per_fat - 1):(k + 1) * (per_fat - 1)]
        chunk = chunk + [FREESECT] * (per_fat - 1 - len(chunk))
        nxt = difat_secs[k + 1] if k + 1 < n_difat else ENDOFCHAIN
        sectors.append(struct.pack(f"<{per_fat}I", *chunk, nxt))

    head = bytearray(512)
    head[0:8] = MAGIC
    head[8:24] = header_clsid
    struct.pack_into("<HHHHH", head, 0x18, minor, major, 0xFFFE, 9 if ssz == 512 else 12, 6)
    struct.pack_into("<IIIIIIIII", head, 0x28, n_dir if major == 4 else 0, n_fat, first_dir,
                     transaction, cutoff, first_minifat, n_minifat,
                     difat_secs[0] if difat_secs else ENDOFCHAIN, n_difat)
    difat0 = fat_secs[:109] + [FREESECT] * (109 - min(109, len(fat_secs)))
    struct.pack_into("<109I", head, 0x4C, *difat0)
    out = bytearray(head)
    if ssz > 512:
        out += b"\0" * (ssz - 512)
    for s in sectors:
        out += s
    return bytes(out)


def _entry_bytes(d, major):
    raw = bytearray(128)
    if d is None:
        struct.pack_into("<III", raw, 0x44, NOSTREAM, NOSTREAM, NOSTREAM)
        return bytes(raw)
    n = d.node
    name = n.name.encode("utf-16-le")
    if len(name) > 62:
        raise CFBError(f"entry name too long: {n.name!r}")
    raw[0:len(name)] = name
    struct.pack_into("<H", raw, 0x40, len(name) + 2)
    raw[0x42] = TYPE_ROOT if n.type == TYPE_ROOT else n.type
    raw[0x43] = d.color
    struct.pack_into("<III", raw, 0x44, d.left, d.right, d.child)
    raw[0x50:0x60] = n.clsid
    struct.pack_into("<I", raw, 0x60, n.state)
    raw[0x64:0x6C] = n.ctime
    raw[0x6C:0x74] = n.mtime
    if n.type == TYPE_STORAGE:
        start, size = 0, 0
    else:
        start, size = d.start, d.size
    struct.pack_into("<IQ", raw, 0x74, start, size)
    return bytes(raw)


def write(path, data, source=None):
    """Write bytes to `path`, refusing to overwrite the file they came from."""
    path = Path(path)
    if source is not None and path.exists() and path.resolve() == Path(source).resolve():
        raise CFBError(f"refusing to overwrite the input file {path}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
