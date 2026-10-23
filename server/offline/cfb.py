"""Minimal read-only reader for OLE2 / MS-CFB compound files.

Altium's .PcbDoc, .SchDoc and .PcbLib are all compound files. This reads
directory entries and stream bytes; it does not write and does not validate
beyond what is needed to fail loudly on a corrupt file.
"""
import struct

FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
NOSTREAM = 0xFFFFFFFF
_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class CFBError(Exception):
    pass


class CompoundFile:
    def __init__(self, data: bytes):
        if data[:8] != _SIGNATURE:
            raise CFBError("not an OLE2 compound file (bad signature)")
        self.data = data
        _minor, major, byteorder, sector_shift, mini_shift = struct.unpack_from("<HHHHH", data, 0x18)
        if byteorder != 0xFFFE:
            raise CFBError("unexpected byte order mark")
        self.major = major
        self.ssize = 1 << sector_shift
        self.mssize = 1 << mini_shift
        (_n_dir, n_fat, first_dir, _tx, self.mini_cutoff, first_minifat, _n_minifat,
         first_difat, n_difat) = struct.unpack_from("<IIIIIIIII", data, 0x28)

        difat = list(struct.unpack_from("<109I", data, 0x4C))
        sec = first_difat
        for _ in range(n_difat):
            if sec in (ENDOFCHAIN, FREESECT):
                break
            ent = struct.unpack("<%dI" % (self.ssize // 4), self._sector(sec))
            difat.extend(ent[:-1])
            sec = ent[-1]
        self.fat = []
        for fs in [x for x in difat if x not in (FREESECT, ENDOFCHAIN)][:n_fat]:
            self.fat.extend(struct.unpack("<%dI" % (self.ssize // 4), self._sector(fs)))

        dirdata = self._chain(first_dir)
        self.entries = []
        for i in range(len(dirdata) // 128):
            raw = dirdata[i * 128:(i + 1) * 128]
            nlen = struct.unpack_from("<H", raw, 64)[0]
            left, right, child = struct.unpack_from("<III", raw, 68)
            start = struct.unpack_from("<I", raw, 116)[0]
            size = struct.unpack_from("<Q", raw, 120)[0]
            if major == 3:
                size &= 0xFFFFFFFF
            self.entries.append({
                "name": raw[:max(0, nlen - 2)].decode("utf-16-le", "replace"),
                "type": raw[66], "left": left, "right": right, "child": child,
                "start": start, "size": size,
            })
        root = self.entries[0]
        self._ministream = b"" if root["start"] in (ENDOFCHAIN, FREESECT) else self._chain(root["start"])[:root["size"]]
        self._minifat = []
        if first_minifat not in (ENDOFCHAIN, FREESECT):
            mf = self._chain(first_minifat)
            self._minifat = list(struct.unpack("<%dI" % (len(mf) // 4), mf))
        self.paths = {}
        self._walk(root["child"], "")

    def _sector(self, n):
        off = (n + 1) * self.ssize
        return self.data[off:off + self.ssize]

    def _chain(self, start):
        out, seen, s = [], set(), start
        while s not in (ENDOFCHAIN, FREESECT) and s < len(self.fat):
            if s in seen:
                raise CFBError("FAT chain loops")
            seen.add(s)
            out.append(self._sector(s))
            s = self.fat[s]
        return b"".join(out)

    def _mini_chain(self, start, size):
        out, seen, s = [], set(), start
        while s not in (ENDOFCHAIN, FREESECT) and s < len(self._minifat):
            if s in seen:
                raise CFBError("mini FAT chain loops")
            seen.add(s)
            off = s * self.mssize
            out.append(self._ministream[off:off + self.mssize])
            s = self._minifat[s]
        return b"".join(out)[:size]

    def _walk(self, idx, prefix):
        if idx == NOSTREAM or idx >= len(self.entries):
            return
        e = self.entries[idx]
        self._walk(e["left"], prefix)
        path = prefix + e["name"]
        self.paths[path] = idx
        if e["type"] == 1:
            self._walk(e["child"], path + "/")
        self._walk(e["right"], prefix)

    def exists(self, path):
        return path in self.paths

    def streams(self):
        """(path, size) for every stream."""
        return [(p, self.entries[i]["size"]) for p, i in self.paths.items() if self.entries[i]["type"] == 2]

    def read(self, path) -> bytes:
        e = self.entries[self.paths[path]]
        if e["type"] != 2:
            raise CFBError(path + " is not a stream")
        if e["size"] < self.mini_cutoff:
            return self._mini_chain(e["start"], e["size"])
        return self._chain(e["start"])[:e["size"]]
