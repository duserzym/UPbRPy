"""Generate the UPbRPy Studio design master from a Mineral Habit Matrix STL.

Usage: python3 -I GUI/assets/icons/make_icon.py <stl_dir> [--variant A|B]
       [--out PATH]

Standard library + numpy only. Variant A is the default SVG master; write
variant B to a scratch path with --out. No shipped icon assets are rebuilt.
Crystal models: https://github.com/duserzym/mineral-habit-matrix
MIT licence, by Yiming Zhang.
"""

import argparse
import struct
from pathlib import Path

import numpy as np

# Palettes, projection helpers and HEAD copied from
# grainatlas/tauri/icon/zircons.py by Yiming Zhang (shared lab app icon style).
# Keep the shared tile, paper, facet edges and shadow definitions identical.
STL = Path()
PALETTE = {"100": "#eab25a", "110": "#c8683a", "101": "#f6d29a", "211": "#dc9356", "301": "#d08a5c"}
GREY = {"100": "#c9d3df", "110": "#a9b6c5", "101": "#e3e9f0", "211": "#d2dbe5", "301": "#bfc9d5"}
JBLUE = {"100": "#7b93e6", "110": "#4063d8", "101": "#c3cff6", "211": "#9db0ee", "301": "#6a84e0"}
JGREEN = {"100": "#6fc45a", "110": "#389826", "101": "#c6ecb8", "211": "#95d682", "301": "#5cb848"}
JPURPLE = {"100": "#b98bcc", "110": "#9558b2", "101": "#e2cdea", "211": "#cba9d9", "301": "#a673c0"}
JRED = {"100": "#e2756d", "110": "#cb3c33", "101": "#f5c3bf", "211": "#ec9a94", "301": "#d8574f"}

def read_stl(p):
    b = p.read_bytes(); n = struct.unpack_from("<I", b, 80)[0]
    tri = np.frombuffer(b, dtype=np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")]), count=n, offset=84)
    return tri["v"].reshape(-1, 3, 3).astype(float)

def faces_from_tris(tris):
    """Merge coplanar triangles into convex polygon faces; return list of (normal, loop points 3D)."""
    v = tris
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]); n /= np.linalg.norm(n, axis=1)[:, None]
    d = np.einsum("ij,ij->i", n, v[:, 0])
    key = [tuple(np.round(np.r_[n[i], d[i]], 4)) for i in range(len(v))]
    groups = {}
    for i, k in enumerate(key): groups.setdefault(k, []).append(i)
    out = []
    for k, idx in groups.items():
        nn = np.array(k[:3]); pts = np.unique(np.round(v[idx].reshape(-1, 3), 6), axis=0)
        # 2D hull in the face plane
        a = np.cross(nn, [0, 0, 1]); a = np.cross(nn, [1, 0, 0]) if np.linalg.norm(a) < 1e-6 else a
        a /= np.linalg.norm(a); b = np.cross(nn, a)
        uv = np.c_[pts @ a, pts @ b]; h = hull(uv)
        loop = [pts[np.argmin(np.linalg.norm(uv - q, axis=1))] for q in h]
        out.append((nn, np.array(loop)))
    return out

def hull(pts):
    pts = sorted(set(map(tuple, pts)))
    cross = lambda o, a, b: (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0: lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0: up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]

def form(n):
    """Classify a tetragonal zircon face normal: prisms {100}/{110}, pyramids {101}/{211}/{301}."""
    az = np.degrees(np.arctan2(abs(n[1]), abs(n[0]))) % 90; az = min(az, 90 - az)
    pol = np.degrees(np.arccos(abs(n[2])))
    if pol > 85: return "110" if az > 30 else "100"
    if az > 12: return "211"
    return "301" if pol > 64 else "101"

def project(name, turn, tilt, size, centre, pal, edge, zoning=False, light=(-0.45, -0.8, 0.55), roll=0.0):
    tris = read_stl(STL / f"zircon_{name}.stl"); faces = faces_from_tris(tris)
    t, x = np.radians(turn), np.radians(tilt)
    rz = np.array([[np.cos(t), -np.sin(t), 0], [np.sin(t), np.cos(t), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(x), -np.sin(x)], [0, np.sin(x), np.cos(x)]])
    R = rx @ rz; L = np.array(light) / np.linalg.norm(light)
    allv = np.vstack([f[1] for f in faces]) @ R.T
    xy = np.c_[allv[:, 0], -allv[:, 2]]; lo, hi = xy.min(0), xy.max(0)
    s = size / (hi - lo).max(); c = np.array(centre)
    rr = np.radians(roll); M = np.array([[np.cos(rr), -np.sin(rr)], [np.sin(rr), np.cos(rr)]])
    def P(v):
        V = v @ R.T; q = np.c_[V[:, 0], -V[:, 2]]; return ((q - (lo + hi) / 2) * s) @ M.T + c
    polys, edges = [], []
    for nn, loop in faces:
        n = R @ nn
        if n[1] >= -1e-9: continue
        shade = 0.66 + 0.48 * max(0.0, float(n @ L))
        base = np.array([int(pal[form(nn)][i:i+2], 16) for i in (1, 3, 5)], float)
        col = "#" + "".join(f"{int(np.clip(v, 0, 255)):02x}" for v in base * shade + 255 * 0.06)
        pts = " ".join(f"{a:.1f},{b:.1f}" for a, b in P(loop))
        polys.append(f'<polygon points="{pts}" fill="{col}"/>'); edges.append(f'<polygon points="{pts}"/>')
    H = np.array(hull(np.round(P(np.vstack([f[1] for f in faces])), 2)))
    hp = " ".join(f"{a:.1f},{b:.1f}" for a, b in H)
    g = [f'<polygon points="{hp}" fill="{pal["110"]}"/>'] + polys
    g.append(f'<g fill="none" stroke="{edge}" stroke-opacity="0.55" stroke-width="2.6" stroke-linejoin="round">' + "".join(edges) + "</g>")
    g.append(f'<polygon points="{hp}" fill="none" stroke="{edge}" stroke-width="4" stroke-linejoin="round" stroke-opacity="0.8"/>')
    if zoning:  # oscillatory CL zoning suggested by inset outlines of the silhouette
        cen = H.mean(0)
        for k, op in ((0.78, 0.55), (0.58, 0.45), (0.40, 0.5), (0.22, 0.45)):
            Z = cen + (H - cen) * k
            zp = " ".join(f"{a:.1f},{b:.1f}" for a, b in Z)
            g.append(f'<polygon points="{zp}" fill="none" stroke="#fff1dc" stroke-opacity="{op}" stroke-width="3" stroke-linejoin="round"/>')
    return "\n    ".join(g)

HEAD = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-labelledby="title desc">
  <title id="title">{title}</title>
  <desc id="desc">{desc}</desc>
  <defs>
    <linearGradient id="tile" x2="0.8" y2="1"><stop stop-color="#ffffff"/><stop offset="1" stop-color="#e9eef5"/></linearGradient>
    <linearGradient id="paper" x2="0.8" y2="1"><stop stop-color="#ffffff"/><stop offset="1" stop-color="#f1f5f9"/></linearGradient>
    <linearGradient id="sem" x2="0.6" y2="1"><stop stop-color="#dfe6f7"/><stop offset="1" stop-color="#b9c6ec"/></linearGradient>
    <linearGradient id="sem2" x2="0.6" y2="1"><stop stop-color="#e9edf9"/><stop offset="1" stop-color="#c9d3f0"/></linearGradient>
    <linearGradient id="blue" x2="0.8" y2="1"><stop stop-color="#a8b8f2"/><stop offset="1" stop-color="#4063d8"/></linearGradient>
    <linearGradient id="orange" x2="0.8" y2="1"><stop stop-color="#efa39d"/><stop offset="1" stop-color="#cb3c33"/></linearGradient>
    <radialGradient id="spotglow" cx="0.5" cy="0.5" r="0.5"><stop stop-color="#7dd3fc" stop-opacity="0.9"/><stop offset="0.5" stop-color="#38bdf8" stop-opacity="0.35"/><stop offset="1" stop-color="#38bdf8" stop-opacity="0"/></radialGradient>
    <filter id="shadow" x="-25%" y="-25%" width="150%" height="150%"><feDropShadow dx="0" dy="9" stdDeviation="10" flood-color="#0f1b2d" flood-opacity="0.25"/></filter>
    <filter id="soft" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="1.6"/></filter>
    <clipPath id="mosaic"><rect x="96" y="96" width="320" height="320" rx="22"/></clipPath>
  </defs>
  <rect x="36" y="36" width="440" height="440" rx="88" fill="url(#tile)"/>
  <rect x="36" y="36" width="440" height="440" rx="88" fill="none" stroke="#d9e1ec" stroke-width="10"/>
'''


PANEL = '''  <g filter="url(#shadow)">
    <rect x="96" y="96" width="320" height="320" rx="22" fill="url(#paper)" stroke="#cbd5e1" stroke-width="3"/>
    <path d="M160 128 V384 M224 128 V384 M288 128 V384 M352 128 V384 M128 160 H384 M128 224 H384 M128 288 H384 M128 352 H384" stroke="#e2e8f0" stroke-width="2"/>
    <path d="M128 128 V384 H384" fill="none" stroke="#334155" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>
    <!-- Integration excludes the signal rise and washout tail. -->
    <rect x="204" y="240" width="134" height="132" fill="#4063d8" fill-opacity="0.14"/>
    <path d="M204 240 V372 M338 240 V372" fill="none" stroke="#4063d8" stroke-width="3" stroke-opacity="0.55" stroke-dasharray="7 7"/>
    <!-- Gas blank, laser onset, downhole fractionation and washout. -->
    <g fill="none" stroke-width="9" stroke-linecap="round" stroke-linejoin="round">
      <path d="M142 343 C154 343 164 344 174 343 C182 343 182 247 196 247 C236 248 286 261 334 268 C348 270 348 341 370 343 L386 343" stroke="#cb3c33"/>
      <path d="M142 355 C154 355 164 356 174 355 C184 355 184 287 196 287 C238 288 286 300 334 306 C350 308 350 353 370 355 L386 355" stroke="#389826"/>
      <path d="M142 367 C154 367 164 368 174 367 C186 367 186 326 196 326 C238 327 286 336 334 341 C350 343 352 365 370 367 L386 367" stroke="#9558b2"/>
    </g>
'''

# A complete ellipse is split at its extrema so the rear half sits behind
# the crystal and the front half passes over it. Together they form one orbit.
ORBIT_BACK = '''    <g transform="rotate(-22 330 178)">
      <path d="M264 178 A66 24 0 0 1 396 178" fill="none" stroke="#9558b2" stroke-width="3.5"/>
      <circle cx="330" cy="154" r="5" fill="#9558b2"/>
    </g>
'''
ORBIT_FRONT = '''    <g transform="rotate(-22 330 178)">
      <path d="M396 178 A66 24 0 0 1 264 178" fill="none" stroke="#9558b2" stroke-width="3.5"/>
      <circle cx="330" cy="202" r="5" fill="#9558b2"/>
    </g>
'''
SPOT = '''    <circle cx="329" cy="174" r="23" fill="url(#spotglow)"/>
    <circle cx="329" cy="174" r="12" fill="none" stroke="#4063d8" stroke-width="4"/>
    <circle cx="329" cy="174" r="12" fill="none" stroke="#0ea5e9" stroke-width="2" filter="url(#soft)"/>
  </g>
</svg>
'''


def make_svg(stl_dir: Path, variant: str = "A") -> str:
    """Return the deterministic SVG for variant A or B using zircon_S8.stl."""
    global STL
    if variant not in ("A", "B"):
        raise ValueError("variant must be A or B")
    STL = Path(stl_dir)
    desc = ("Three time-resolved laser-ablation signals with gas blanks, "
            "sloping plateaus and washout tails, a blue integration window "
            "and an upper-right Pupin S8 zircon with a laser spot")
    if variant == "B":
        desc += " and an isotope orbit"
    desc += ", on a gridded paper panel and softly shaded light tile."
    head = HEAD.format(title="UPbRPy Studio icon", desc=desc)
    credit = ("  <!-- Zircon: Pupin S8 habit projected from the STL models of "
              "https://github.com/duserzym/mineral-habit-matrix "
              "(MIT licence, Yiming Zhang). -->\n")
    head = head.replace("  <title", credit + "  <title", 1)
    crystal = project("S8", -30, 14, 118, (330, 178), JBLUE, "#24408f", roll=28)
    return (head + PANEL + (ORBIT_BACK if variant == "B" else "")
            + "    " + crystal + "\n"
            + (ORBIT_FRONT if variant == "B" else "") + SPOT)


def main() -> None:
    """Parse the design-generator CLI and write one SVG."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stl_dir", type=Path, help="Mineral Habit Matrix zircon STL directory")
    parser.add_argument("--variant", choices=("A", "B"), default="A")
    parser.add_argument("--out", type=Path,
                        default=Path(__file__).resolve().with_name("upbrpy_studio.svg"))
    args = parser.parse_args()
    args.out.write_text(make_svg(args.stl_dir, args.variant), encoding="utf-8")


if __name__ == "__main__":
    main()
