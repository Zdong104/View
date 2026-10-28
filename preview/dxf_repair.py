"""Loosely written DXF files: fix up the raw tag stream so ezdxf can load it.

Small DXF writers (CAD exports, scripts, CAM tools) often write entities that
only exist since R13/R2000 -- LWPOLYLINE, MTEXT, SPLINE, HATCH... -- the R12
way: without the subclass markers (100/AcDbEntity, 100/AcDbPolyline...) and
without handles, frequently with no $ACADVER either, which makes the file R12,
a version that has no such entities at all.  ezdxf rejects those entities even
in recover mode ("missing 'AcDbPolyline' subclass"), or, when the markers are
there but the header says R12, loads them with their flags and layer lost.

normalize() rewrites such a file, as raw bytes, into the structure ezdxf
expects (ezdxf's recover mode then decodes and audits it as usual):

* a post-R12 entity whose markers are missing or incomplete gets them: the
  common entity group codes (layer, color, linetype...) that lead the entity
  go into AcDbEntity, the rest -- its geometry -- into the entity's own
  subclass, extended data stays at the end;
* entities without a handle get a unique one;
* a file holding post-R12 entities while claiming R12 (or no version at all)
  is declared R2000.

R12-era entities (LINE, ARC, CIRCLE, TEXT, POLYLINE, INSERT...) are left as
they are: ezdxf reads them without markers.  The viewer only takes this path
after the normal and the recover loader failed (or misread the file), so
well-formed files never pay for it.
"""
from __future__ import annotations

R2000 = b"AC1015"

# Post-R12 entities with a single subclass after AcDbEntity: ezdxf's loaders
# look that subclass up by position and refuse (or mangle) marker-less ones.
SUBCLASS = {
    b"LWPOLYLINE": b"AcDbPolyline",
    b"MTEXT": b"AcDbMText",
    b"SPLINE": b"AcDbSpline",
    b"HATCH": b"AcDbHatch",
    b"MPOLYGON": b"AcDbMPolygon",
    b"ELLIPSE": b"AcDbEllipse",
    b"RAY": b"AcDbRay",
    b"XLINE": b"AcDbXline",
    b"LEADER": b"AcDbLeader",
    b"TOLERANCE": b"AcDbFcf",
    b"MLINE": b"AcDbMline",
    b"MESH": b"AcDbSubDMesh",
    b"MULTILEADER": b"AcDbMLeader",
    b"MLEADER": b"AcDbMLeader",
}
# other entities a DXF R12 file cannot contain
NEWER = set(SUBCLASS) | {b"IMAGE", b"WIPEOUT", b"HELIX", b"3DSOLID", b"REGION", b"BODY",
                         b"SURFACE", b"ARC_DIMENSION", b"LARGE_RADIAL_DIMENSION",
                         b"PDFUNDERLAY", b"DWFUNDERLAY", b"DGNUNDERLAY", b"OLE2FRAME",
                         b"ACAD_TABLE"}

# R12-era entities: ezdxf reads them without any markers, but not with half of them
R12 = {b"LINE", b"POINT", b"CIRCLE", b"ARC", b"TRACE", b"SOLID", b"TEXT", b"SHAPE",
       b"BLOCK", b"ENDBLK", b"INSERT", b"ATTDEF", b"ATTRIB", b"VERTEX", b"SEQEND",
       b"POLYLINE", b"3DFACE", b"3DLINE", b"VIEWPORT", b"DIMENSION"}

BASE_CODES = {5, 105, 330, 360}                   # handle, owner, (102 groups too)
# AcDbEntity: paperspace, layout, layer, linetype, material, color, lineweight,
# linetype scale, invisible, proxy graphics, true color, color name,
# transparency, plot style, shadow mode
ENTITY_CODES = {67, 410, 8, 6, 347, 62, 370, 48, 60, 92, 310, 420, 430, 440, 390, 284}
# group codes an entity's own subclass uses too (with another meaning): after
# the leading run of entity tags, these stay with the geometry
OWN = {
    b"HATCH": {92, 310, 330},
    b"MPOLYGON": {92, 310, 330},
    b"MESH": {92, 310},
    b"MTEXT": {48},
}
POSITIONAL = {b"MULTILEADER", b"MLEADER"}           # codes everywhere: go by position only


def read_tags(data: bytes):
    """[(code, raw value)] of an ASCII DXF file, comments dropped; None if it
    is not one (binary DXF, or a group code that is not a number)."""
    if data[:22].startswith(b"AutoCAD Binary DXF"):
        return None
    lines = data.splitlines()
    if lines and lines[0].startswith(b"\xef\xbb\xbf"):          # UTF-8 BOM
        lines[0] = lines[0][3:]
    tags = []
    for i in range(0, len(lines) - 1, 2):
        try:
            code = int(lines[i])
        except ValueError:
            return None
        value = lines[i + 1]
        if code == 999:
            continue
        tags.append((code, value))
        if code == 0 and value.strip().upper() == b"EOF":
            break
    return tags


def _name(tag):
    return tag[1].strip().upper()


def fix_entity(rec, name):
    """The entity's tags with AcDbEntity + subclass markers (or, for an R12-era
    entity with an incomplete set, with none), or `rec` itself if they are fine
    already or the entity is not one this knows."""
    sub = SUBCLASS.get(name)
    if sub is None and name not in R12:
        return rec
    body = rec[1:]
    xdata = next((i for i, (c, _v) in enumerate(body) if c == 1001), len(body))
    body, xdata = body[:xdata], body[xdata:]
    markers = [v.strip().lower() for c, v in body if c == 100]
    if sub is None:
        if not markers or (markers[0] == b"acdbentity" and len(markers) > 1):
            return rec
        return [rec[0], *(t for t in body if t[0] != 100), *xdata]
    if markers == [b"acdbentity", sub.lower()]:
        return rec
    body = [t for t in body if t[0] != 100]
    own = OWN.get(name, set())
    positional = name in POSITIONAL
    base, entity, geometry = [], [], []
    leading = True
    group = None                                    # inside a 102 {...} app data group
    for t in body:
        code = t[0]
        if group is not None:
            group.append(t)
            if code == 102 and t[1].strip().startswith(b"}"):
                group = None
            continue
        if code == 102 and t[1].strip().startswith(b"{") and (leading or not positional):
            group = [t]
            base.append(group)
            continue
        if leading and code not in BASE_CODES and code not in ENTITY_CODES:
            leading = False
        if leading or not positional:
            if code in (5, 105) or (code in BASE_CODES and (leading or code not in own)):
                base.append([t])
                continue
            if code in ENTITY_CODES and (leading or code not in own):
                entity.append(t)
                continue
        geometry.append(t)
    base = [t for g in base for t in g]
    return ([rec[0], *base, (100, b"AcDbEntity"), *entity, (100, sub), *geometry]
            + xdata)


def _header_var(header, name):
    for j, (code, value) in enumerate(header):
        if code == 9 and value.strip().upper() == name:
            return header[j + 1][1] if j + 1 < len(header) and header[j + 1][0] != 9 else None
    return None


def _set_header_var(header, name, code, value):
    for j, (c, v) in enumerate(header):
        if c == 9 and v.strip().upper() == name:
            if j + 1 < len(header) and header[j + 1][0] != 9:
                header[j + 1] = (code, value)
            else:
                header.insert(j + 1, (code, value))
            return
    at = next((j + 1 for j, t in enumerate(header) if t[0] == 2), len(header))
    header[at:at] = [(9, name), (code, value)]


def normalize(data: bytes):
    """The DXF file `data` rewritten for ezdxf (see the module doc), or None if
    there is nothing this can fix."""
    tags = read_tags(data)
    if not tags:
        return None
    records = []
    for t in tags:
        if t[0] == 0 or not records:
            records.append([])
        records[-1].append(t)

    used = set()
    for code, value in tags:
        if code in (5, 105):
            try:
                used.add(int(value.strip(), 16))
            except ValueError:
                pass
    next_handle = max(used, default=0) + 1

    changed = newer = False
    first_new = next_handle
    section = None
    header = None
    for i, rec in enumerate(records):
        name = _name(rec[0]) if rec[0][0] == 0 else b""
        if name == b"SECTION":
            section = next((_name(t) for t in rec[1:] if t[0] == 2), None)
            if section == b"HEADER" and header is None:
                header = rec
            continue
        if name == b"ENDSEC":
            section = None
            continue
        if section not in (b"ENTITIES", b"BLOCKS") or not name:
            continue
        if name in NEWER:
            newer = True
        fixed = fix_entity(rec, name)
        if fixed is not rec:
            records[i] = rec = fixed
            changed = True
        if not any(c == 5 for c, _v in rec[1:]):
            rec.insert(1, (5, b"%X" % next_handle))
            next_handle += 1
            changed = True

    if not changed and not newer:
        return None
    if header is None:
        header = [(0, b"SECTION"), (2, b"HEADER")]
        records[:0] = [header, [(0, b"ENDSEC")]]
    if newer:
        ver = _header_var(header, b"$ACADVER")
        ver = ver.strip().upper() if ver is not None else b""
        if not (len(ver) == 6 and ver.startswith(b"AC") and ver > b"AC1009"):
            _set_header_var(header, b"$ACADVER", 1, R2000)
            changed = True
    if not changed:
        return None
    if next_handle > first_new:
        # ezdxf hands out handles from $HANDSEED to whatever still lacks one
        # (table entries...): start above the ones given here
        _set_header_var(header, b"$HANDSEED", 5, b"%X" % next_handle)
    out = [b"%d\n%s\n" % t for rec in records for t in rec]
    if not (records[-1][0][0] == 0 and _name(records[-1][0]) == b"EOF"):
        out.append(b"0\nEOF\n")
    return b"".join(out)


def misread(doc) -> bool:
    """True if ezdxf loaded `doc` as R12 although it holds newer entities (with
    their subclass markers, which R12 loading ignores, losing their data)."""
    if doc.dxfversion > "AC1009":
        return False
    newer = {n.decode() for n in NEWER}
    return any(e.dxftype() in newer for e in doc.entitydb.values())


def read_loose(path: str):
    """ezdxf Drawing of the normalized file at `path`, or None if normalizing
    does not apply.  May raise what ezdxf raises."""
    import io

    from ezdxf import recover
    with open(path, "rb") as fp:
        data = normalize(fp.read())
    if data is None:
        return None
    doc, _auditor = recover.read(io.BytesIO(data))
    return doc
