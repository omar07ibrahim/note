# Third-party notices

RecallLedger production code declares no runtime dependencies and this
repository does not copy a font file or other third-party visual asset into its
source tree. The following component is used only by the deterministic hosted
visual-evidence renderer.

## Pillow 12.3.0

- Role: dev/CI-only PNG and GIF encoding plus raster text drawing.
- Runtime impact: none; project.dependencies remains empty.
- Exact Linux CPython 3.12 wheel:
  pillow-12.3.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
- Wheel SHA-256:
  78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91
- Upstream release source:
  <https://github.com/python-pillow/Pillow/tree/12.3.0>
- Package index release:
  <https://pypi.org/project/pillow/12.3.0/>
- License: MIT-CMU, with the upstream text at
  <https://github.com/python-pillow/Pillow/blob/12.3.0/LICENSE>.

The renderer calls PIL.ImageFont.load_default(size=...). Under the exact wheel
this returns the limited-character-set Aileron Regular font embedded inside
Pillow itself; the renderer verifies the reported family/style
("Aileron", "Regular") before drawing. RecallLedger neither downloads nor
bundles a separate font file. Pillow identifies the embedded font and its
origin in
<https://github.com/python-pillow/Pillow/blob/12.3.0/src/PIL/ImageFont.py#L1089-L1130>.
The Aileron author page describes the typeface as "No Rights Reserved" and
permits modification and redistribution:
<https://dotcolon.net/fonts/aileron/>.

Generated PNG/GIF files contain only rasterized glyph pixels. They do not
embed the Aileron font program, an operating-system font, or an external asset.
