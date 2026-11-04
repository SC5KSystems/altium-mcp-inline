"""Write a PcbDoc, and add or remove records without breaking cross-references.

Every save re-encodes only what changed (untouched records and streams are
written back byte for byte), refreshes record-count headers, rebuilds the
container with cfb_write, then reads the result back and runs
check_integrity() on it. A save that fails the check writes nothing.

The cross-references kept consistent are listed in PCBDOC_FORMAT.md.
"""

import contextlib
import re
import struct
from pathlib import Path

from .cfb import CompoundFile
from .cfb_write import tree_from, to_bytes, write
from .pcbdoc import (BOARD16, NONE16, FormatError, PcbDoc, Storage, encode_widestrings,
                     parse_float)
from . import records as R


class IntegrityError(FormatError):
    pass


# ------------------------------------------------------------------ encode / save

def encode_streams(doc):
    """{stream path: bytes} for the whole document as it is now."""
    out = dict(doc.streams)
    for name, st in doc.storages.items():
        if st.kind == "widestrings":
            data = encode_widestrings(doc.widestrings, doc.widestring_order)
            count = len(doc.widestring_order)
        else:
            data = st.encode_data()
            count = len(st.records)
        out[f"{name}/Data"] = data
        if st.header_is_count:
            out[f"{name}/Header"] = struct.pack("<I", count)
        elif st.header is not None:
            out[f"{name}/Header"] = st.header
    return out


def to_document_bytes(doc):
    streams = encode_streams(doc)
    root = tree_from(doc.cf)
    for path, data in streams.items():
        if root.find(path) is None or root.find(path).data != data:
            root.set_stream(path, data)
    return to_bytes(root, like=doc.cf)


def save(doc, out_path, check=True):
    """Write the document to out_path (never onto the file it was read from).

    Returns the integrity report of the re-read result.
    """
    out_path = Path(out_path)
    if doc.path is not None and out_path.exists() and out_path.resolve() == Path(doc.path).resolve():
        raise FormatError(f"refusing to overwrite the input file {out_path}")
    doc.reindex()
    problems = check_integrity(doc)
    if problems:
        raise IntegrityError("document fails its integrity check before saving: "
                             + "; ".join(problems[:10]))
    data = to_document_bytes(doc)
    report = {"bytes": len(data)}
    if check:
        again = PcbDoc(CompoundFile(data))
        problems = check_integrity(again)
        if problems:
            raise IntegrityError("written document fails its integrity check: "
                                 + "; ".join(problems[:10]))
        for name, st in doc.storages.items():
            st2 = again.storages.get(name)
            n1 = len(doc.widestring_order) if st.kind == "widestrings" else len(st.records)
            n2 = (len(again.widestring_order) if st2 and st2.kind == "widestrings"
                  else len(st2.records) if st2 else 0)
            if n1 != n2:
                raise IntegrityError(f"{name}: wrote {n1} records but read back {n2}")
        report["counts"] = again.summary()
    write(out_path, data, source=doc.path)
    report["path"] = str(out_path)
    return report


# ------------------------------------------------------------------ integrity

def _uid_records(doc):
    st = doc.storages.get("UniqueIDPrimitiveInformation")
    return st.records if st else []


def check_integrity(doc):
    """Problems that would make the file inconsistent; empty when sound."""
    probs = []
    nn, nc, npoly = len(doc.nets), len(doc.components), len(doc.polygons)
    for st in doc.storages.values():
        if st.kind != "binary":
            continue
        for r in st.records:
            try:
                n = r._get("H", 3, r._main)
                c = r._get("H", 7, r._main)
                p = r._get("H", 5, r._main)
            except struct.error:
                probs.append(f"{st.name}[{r.index}] too short")
                continue
            if r.KIND in ("text", "body"):
                n = NONE16
            if r.KIND in ("via", "pad", "text", "body"):
                p = NONE16
            if n != NONE16 and n >= nn:
                probs.append(f"{st.name}[{r.index}] net index {n} >= {nn} nets")
            if c != NONE16 and c >= nc:
                probs.append(f"{st.name}[{r.index}] component index {c} >= {nc} components")
            if p not in (NONE16, BOARD16) and p >= npoly:
                probs.append(f"{st.name}[{r.index}] polygon index {p} >= {npoly} polygons")
    for a, b in (("Regions6", "ShapeBasedRegions6"),
                 ("ComponentBodies6", "ShapeBasedComponentBodies6")):
        if a in doc.storages and b in doc.storages:
            ra, rb = doc.storages[a].records, doc.storages[b].records
            if len(ra) != len(rb):
                probs.append(f"{a} has {len(ra)} records, {b} has {len(rb)}")
            else:
                for x, y in zip(ra, rb):
                    if bytes(x.subs[0][:9]) != bytes(y.subs[0][:9]) or \
                            bytes(x.subs[0][14:16]) != bytes(y.subs[0][14:16]):
                        probs.append(f"{a}[{x.index}] and {b}[{y.index}] disagree")
                        break
    for p in doc.polygons:
        n = p.props.get("NET")
        if n not in (None, "") and parse_float(n, -1) >= 0 and int(parse_float(n)) != NONE16 \
                and int(parse_float(n)) >= nn:
            probs.append(f"polygon {p.index} NET={n} >= {nn} nets")
    seen_idx, seen_uid = set(), set()
    for u in _uid_records(doc):
        kind = u.get("PRIMITIVEOBJECTID", "")
        idx = int(parse_float(u.get("PRIMITIVEINDEX", "-1"), -1))
        uid = u.get("UNIQUEID", "")
        coll = {"Pad": doc.pads, "Via": doc.vias, "Track": doc.tracks, "Arc": doc.arcs,
                "Fill": doc.fills, "Region": doc.regions, "Text": doc.texts}.get(kind)
        if coll is None:
            continue
        if not 0 <= idx < len(coll):
            probs.append(f"UniqueIDPrimitiveInformation points at {kind} {idx} of {len(coll)}")
        if (kind, idx) in seen_idx:
            probs.append(f"UniqueIDPrimitiveInformation lists {kind} {idx} twice")
        if uid and uid in seen_uid:
            probs.append(f"unique id {uid} used twice")
        seen_idx.add((kind, idx))
        seen_uid.add(uid)
    dims = doc.storages.get("Dimensions6")
    for i, d in enumerate(dims.records if dims else []):
        for k, v in d.items:
            m = re.fullmatch(r"REFERENCE(\d+)PRIM", k.upper())
            if not m:
                continue
            obj = d.get(f"REFERENCE{m.group(1)}OBJECTSTRING", "")
            coll = {"Track": doc.tracks, "Arc": doc.arcs, "Pad": doc.pads, "Via": doc.vias,
                    "Fill": doc.fills, "Text": doc.texts}.get(obj)
            if coll is not None and v is not None and not 0 <= int(parse_float(v, -1)) < len(coll):
                probs.append(f"dimension {i} references {obj} {v} of {len(coll)}")
    ws = set(doc.widestring_order)
    if ws:
        for t in doc.texts:
            i = t.widestring_index
            if i is not None and i not in ws and i != 0:
                probs.append(f"text {t.index} uses wide string {i}, which does not exist")
    for name, st in doc.storages.items():
        if st.kind == "binary":
            for i, r in enumerate(st.records):
                if r.index != i:
                    probs.append(f"{name}[{i}] carries index {r.index}")
                    break
    probs += _index_table_problems(doc)
    return probs


GUID_KINDS = {0x0D01: "Arcs6", 0x0E02: "Pads6", 0x0F03: "Vias6", 0x1004: "Tracks6",
              0x1105: "Texts6", 0x1206: "Fills6", 0x1359: "Regions6",
              0x1459: "ShapeBasedRegions6", 0x155A: "ComponentBodies6",
              0x165A: "ShapeBasedComponentBodies6", 0x0208: "Nets6", 0x0309: "Components6",
              0x040A: "Polygons6", 0x0116: "Classes6", 0x050B: "Dimensions6"}
PADVIA_KINDS = {2: "Pads6", 3: "Vias6"}
OBJECT_STORAGE = {"Track": "Tracks6", "Arc": "Arcs6", "Via": "Vias6", "Pad": "Pads6",
                  "Fill": "Fills6", "Region": "Regions6", "Text": "Texts6",
                  "ComponentBody": "ComponentBodies6"}


def _count(doc, storage):
    st = doc.storages.get(storage)
    return len(st.records) if st else 0


def _index_table_problems(doc):
    probs = []
    st = doc.storages.get("PrimitiveGuids")
    for e in (st.records if st else []):
        kind, idx = struct.unpack_from("<II", e, 0)
        name = GUID_KINDS.get(kind)
        if name and idx >= _count(doc, name):
            probs.append(f"PrimitiveGuids entry {name}[{idx}] past {_count(doc, name)} records")
            break
    st = doc.storages.get("PadViaCacheLibraryLinksSection")
    for e in (st.records if st else []):
        idx, kind = struct.unpack_from("<I", e, 1)[0], e[5]
        name = PADVIA_KINDS.get(kind)
        if name and idx >= _count(doc, name):
            probs.append(f"PadViaCacheLibraryLinksSection entry {name}[{idx}] past the end")
            break
    for sname, key_obj in (("ExtendedPrimitiveInformation", "PRIMITIVEOBJECTID"),
                           ("SharedUnion", "OBJECTID"), ("CustomMaskShapes", None)):
        st = doc.storages.get(sname)
        for r in (st.records if st else []):
            name = OBJECT_STORAGE.get(r.get(key_obj, "")) if key_obj else "Pads6"
            idx = int(parse_float(r.get("PRIMITIVEINDEX", "-1"), -1))
            if name and not 0 <= idx < _count(doc, name):
                probs.append(f"{sname} cites {name}[{idx}] past {_count(doc, name)} records")
                break
    return probs


# ------------------------------------------------------------------ adding

def ensure_storage(doc, name, kind="binary"):
    st = doc.storages.get(name)
    if st is None:
        st = Storage(name, kind, struct.pack("<I", 0), [], b"")
        st.header_is_count = True
        doc.storages[name] = st
        doc.streams[f"{name}/Header"] = st.header
        doc.streams[f"{name}/Data"] = b""
        if name in doc.unparsed:
            doc.unparsed.remove(name)
    return st


@contextlib.contextmanager
def batch(doc):
    """Add many records with one reindex at the end instead of one per add.
    Lookups on the document are stale until the block ends."""
    depth = getattr(doc, "_batch_depth", 0)
    doc._batch_depth = depth + 1
    try:
        yield doc
    finally:
        doc._batch_depth = depth
        if depth == 0:
            doc.reindex()


def _reindex(doc):
    if not getattr(doc, "_batch_depth", 0):
        doc.reindex()


def add(doc, rec):
    """Append a record built by records.py; returns it with its index set.

    Pads get a UniqueIDPrimitiveInformation entry, texts a wide string,
    regions their ShapeBasedRegions6 twin (pass the pair from records.region).
    """
    if isinstance(rec, tuple):
        a, b = rec
        sa, sb = ensure_storage(doc, a.storage), ensure_storage(doc, b.storage)
        if len(sa.records) != len(sb.records):
            raise IntegrityError(f"{a.storage} and {b.storage} are out of step")
        a.index, b.index = len(sa.records), len(sb.records)
        sa.records.append(a)
        sb.records.append(b)
        _reindex(doc)
        return a
    st = ensure_storage(doc, rec.storage)
    rec.index = len(st.records)
    st.records.append(rec)
    if rec.KIND == "pad":
        uid_st = ensure_storage(doc, "UniqueIDPrimitiveInformation", "props")
        existing = {u.get("UNIQUEID") for u in uid_st.records}
        uid_st.records.append(R.pad_uid_record(rec.index, R.unique_id(existing)))
    if rec.KIND == "text":
        s = getattr(rec, "pending_string", None)
        if s is not None and "WideStrings6" in doc.storages and rec._has(115, 4):
            idx = (max(doc.widestring_order) + 1) if doc.widestring_order else 0
            doc.widestrings[idx] = s
            doc.widestring_order.append(idx)
            rec._set("I", 115, idx)
    _reindex(doc)
    return rec
