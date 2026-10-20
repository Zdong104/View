"""Write small sample files for the Windows CI startup traces.

    python packaging/windows/test-files.py OUTDIR

sample.pdf uses Helvetica without embedding it (like many real PDFs), so
Poppler has to look up a substitute font; sample.stl is a 20 mm cube;
sample.png is a photo-sized image.  The video is made with gst-launch.
"""
import os
import sys

from PIL import Image, ImageDraw

out = sys.argv[1]
os.makedirs(out, exist_ok=True)

# ---- PDF with a non-embedded base-14 font
text = b"BT /F1 24 Tf 72 720 Td (View startup test) Tj ET"
objs = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
    b"/Resources << /Font << /F1 5 0 R >> >> >>",
    b"<< /Length %d >>\nstream\n" % len(text) + text + b"\nendstream",
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
]
pdf, offsets = bytearray(b"%PDF-1.4\n"), []
for i, body in enumerate(objs, 1):
    offsets.append(len(pdf))
    pdf += b"%d 0 obj\n" % i + body + b"\nendobj\n"
xref = len(pdf)
pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
pdf += b"".join(b"%010d 00000 n \n" % o for o in offsets)
pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
with open(os.path.join(out, "sample.pdf"), "wb") as f:
    f.write(pdf)

# ---- STL: a 20 mm cube
v = [(x, y, z) for x in (0, 20) for y in (0, 20) for z in (0, 20)]
quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
with open(os.path.join(out, "sample.stl"), "w") as f:
    f.write("solid cube\n")
    for a, b, c, d in quads:
        for tri in ((a, b, c), (a, c, d)):
            f.write(" facet normal 0 0 0\n  outer loop\n")
            for i in tri:
                f.write("   vertex %g %g %g\n" % v[i])
            f.write("  endloop\n endfacet\n")
    f.write("endsolid cube\n")

# ---- image
im = Image.new("RGB", (3000, 2000), (90, 140, 200))
ImageDraw.Draw(im).ellipse((800, 400, 2200, 1600), fill=(240, 200, 60))
im.save(os.path.join(out, "sample.jpg"), quality=90)
print("test files in", out)
