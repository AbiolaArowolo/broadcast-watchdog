"""Generates the Watchdog's PWA icons from the Bulldog mark -- the very same SVG markup the page
header, the loading screen and the offline screen use (BULLDOG_BODY in core-listener/app/dashboard.py).
That markup is deliberately plain (paths made only of straight M/L/Z segments, plus lines), so this
script can rasterise it with Pillow alone and no SVG renderer. Change the Bulldog in dashboard.py,
re-run this, and every icon follows. This replaces the old waveform mark.

Outputs, into core-listener/app/static_icons: icon-192.png, icon-512.png, apple-touch-icon.png,
favicon-32.png, and icon-512-maskable.png (full-bleed, mark kept inside the central safe zone so an
Android launcher can crop it to any shape; the splash screen Android builds for an installed app is
made from these icons plus the manifest's background colour).

Not a runtime dependency: the service never imports Pillow. To regenerate: pip install pillow
"""
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

LISTENER_DIR = Path(__file__).resolve().parent.parent / "core-listener"
OUT_DIR = LISTENER_DIR / "app" / "static_icons"
sys.path.insert(0, str(LISTENER_DIR))
from app.dashboard import BULLDOG_BODY, BULLDOG_VIEWBOX  # noqa: E402

SVG_NS = "{http://www.w3.org/2000/svg}"
BACKGROUND = (5, 9, 17, 255)       # the dashboard's own --bg
CARD = (11, 20, 38, 255)
SUPERSAMPLE = 4                    # draw big, shrink once: smooth edges without an SVG renderer


def parse_color(value: str):
    if value in ("none", "", None):
        return None
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def parse_path(d: str):
    """Only M, L and Z are understood; anything else is a hard error so the Bulldog can never
    quietly render wrong. Returns (points, closed)."""
    points, closed = [], False
    tokens = re.findall(r"[A-Za-z]|-?\d+(?:\.\d+)?", d)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("M", "L"):
            points.append((float(tokens[index + 1]), float(tokens[index + 2])))
            index += 3
        elif token == "Z":
            closed = True
            index += 1
        elif re.fullmatch(r"-?\d+(?:\.\d+)?", token):  # implicit repeat of the previous L
            points.append((float(token), float(tokens[index + 1])))
            index += 2
        else:
            raise ValueError(f"unsupported SVG path command {token!r} in the Bulldog; keep it to M, L and Z")
    return points, closed


def bulldog_shapes():
    """The Bulldog as drawing primitives: (points, closed, stroke_rgb, stroke_width, fill_rgb)."""
    root = ET.fromstring(f'<svg xmlns="http://www.w3.org/2000/svg">{BULLDOG_BODY}</svg>')
    shapes = []

    def walk(node, inherited):
        style = dict(inherited)
        for key in ("stroke", "stroke-width", "fill"):
            if key in node.attrib:
                style[key] = node.attrib[key]
        tag = node.tag.replace(SVG_NS, "")
        if tag == "g":
            for child in node:
                walk(child, style)
        elif tag == "path":
            points, closed = parse_path(node.attrib["d"])
            shapes.append((points, closed, parse_color(style["stroke"]), float(style["stroke-width"]), parse_color(style["fill"])))
        elif tag == "line":
            points = [(float(node.attrib["x1"]), float(node.attrib["y1"])), (float(node.attrib["x2"]), float(node.attrib["y2"]))]
            shapes.append((points, False, parse_color(style["stroke"]), float(style["stroke-width"]), None))
        else:
            raise ValueError(f"unsupported SVG element <{tag}> in the Bulldog")

    for child in root:
        walk(child, {"stroke": "none", "fill": "none", "stroke-width": "1"})
    return shapes


def draw_bulldog(size: int, mark_fraction: float) -> Image.Image:
    """The Bulldog alone, glowing, on a transparent layer of size x size, filling mark_fraction of it."""
    big = size * SUPERSAMPLE
    scale = big * mark_fraction / BULLDOG_VIEWBOX
    offset = (big - BULLDOG_VIEWBOX * scale) / 2

    def place(point):
        return (offset + point[0] * scale, offset + point[1] * scale)

    sharp = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sharp)
    for points, closed, stroke, width, fill in bulldog_shapes():
        placed = [place(p) for p in points]
        if fill and closed:
            draw.polygon(placed, fill=fill + (255,))
        if stroke:
            line = placed + [placed[0]] if closed else placed
            pixel_width = max(1, round(width * scale))
            draw.line(line, fill=stroke + (255,), width=pixel_width)
            radius = pixel_width / 2  # round joins and caps, as the SVG's stroke-linecap/linejoin ask
            for x, y in placed:
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=stroke + (255,))

    glow = sharp.filter(ImageFilter.GaussianBlur(radius=max(2, big // 40)))
    glow = Image.composite(glow, Image.new("RGBA", glow.size, (0, 0, 0, 0)), glow.split()[3])
    layer = Image.alpha_composite(glow, sharp)
    # a second, tighter halo so the mark still reads at 32 px
    halo = sharp.filter(ImageFilter.GaussianBlur(radius=max(1, big // 120)))
    layer = Image.alpha_composite(halo, layer)
    return layer.resize((size, size), Image.LANCZOS)


def render_icon(size: int, maskable: bool = False) -> Image.Image:
    image = Image.new("RGBA", (size, size), BACKGROUND)
    if not maskable:
        inset = int(size * 0.07)
        ImageDraw.Draw(image).rounded_rectangle((inset, inset, size - inset, size - inset), radius=int(size * 0.18), fill=CARD)
    centre = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(centre).ellipse((size * 0.18, size * 0.18, size * 0.82, size * 0.82), fill=(59, 184, 255, 46))
    centre = centre.filter(ImageFilter.GaussianBlur(radius=max(6, size // 10)))
    image = Image.alpha_composite(image, centre)
    # maskable icons must keep the mark inside the central 80% (the launcher may crop the rest)
    mark = draw_bulldog(size, 0.50 if maskable else 0.66)
    return Image.alpha_composite(image, mark)


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, size, maskable in [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-512-maskable.png", 512, True),
        ("apple-touch-icon.png", 180, False),
        ("favicon-32.png", 32, False),
    ]:
        render_icon(size, maskable).convert("RGB").save(OUT_DIR / name)
        print("wrote", name)
