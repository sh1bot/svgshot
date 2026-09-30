"""Emit deliberately simple, editable SVG."""
from __future__ import annotations

from html import escape
from functools import lru_cache
from pathlib import Path
import subprocess

from PIL import ImageFont

from .model import Node, flatten


def _contrast(foreground: str, background: str, minimum=4.4) -> str:
    def luminance(hexcolor):
        values = [int(hexcolor[i:i+2], 16) / 255 for i in (1, 3, 5)]
        values = [v / 12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values]
        return sum(a*b for a, b in zip(values, (.2126, .7152, .0722)))
    a, b = luminance(foreground), luminance(background)
    if (max(a,b)+.05)/(min(a,b)+.05) >= minimum:
        return foreground
    if minimum <= 3:
        return "#666666" if b >= .45 else "#ffffff"
    return "#171717" if (b+.05)/.05 >= (1.05)/(b+.05) else "#ffffff"


def resolved_family(family: str) -> str:
    if family == "auto":
        return "Segoe UI" if Path("C:/Windows/Fonts/segoeui.ttf").is_file() else "Arial"
    return family


@lru_cache(maxsize=16)
def _font(family: str):
    family = resolved_family(family)
    windows = {"Segoe UI": "segoeui.ttf", "Arial": "arial.ttf"}
    candidates = [Path("C:/Windows/Fonts") / windows.get(family, "")]
    try:
        match = subprocess.run(["fc-match", family, "-f", "%{file}"],
                               capture_output=True, text=True, check=False)
        if match.returncode == 0:
            candidates.append(Path(match.stdout.strip()))
    except FileNotFoundError:
        pass
    candidates.append(Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    for candidate in candidates:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), 100)
    return None


def text_layout(node: Node, font_family: str = "auto") -> tuple[float, float]:
    """Fit OCR ink bounds using measured font ink rather than a height multiplier.

    Returns (font size, baseline). SVG textLength handles remaining differences
    between the measurement font and the final SVG renderer.
    """
    x, y, width, height = node.box
    font = _font(font_family)
    if font is None or not node.text:
        size = max(8, height * 1.1)
        return size, y + height
    left, top, right, bottom = font.getbbox(node.text, anchor="ls")
    ink_height = max(1, bottom-top)
    ink_width = max(1, font.getlength(node.text))
    size = max(6, min(40, 100 * min(width/ink_width, height/ink_height)))
    baseline = y - top * size/100
    return round(size, 2), round(baseline, 2)


def to_svg(root: Node, font_family: str = "auto") -> str:
    _, _, width, height = root.box
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
             f'width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img">',
             f'<rect width="{width}" height="{height}" fill="{root.color}"/>']

    filled = [node for node in flatten(root) if node.kind in ("rect", "button", "gradient-title") or
              (node.kind == "outlined-button" and node.background)]

    def surface_at(node: Node, inherited: str) -> str:
        x, y, w, h = node.box
        matches = [shape for shape in filled if shape is not node and
                   shape.box[0] <= x and shape.box[1] <= y and
                   shape.box[0]+shape.box[2] >= x+w and
                   shape.box[1]+shape.box[3] >= y+h]
        if matches:
            shape = min(matches, key=lambda shape: shape.box[2]*shape.box[3])
            return shape.background or shape.color
        return inherited

    def draw(node: Node, surface: str):
        x, y, w, h = node.box
        kind = node.kind
        paint = node.color
        grouped = kind in ("button", "outlined-button", "beveled-button", "radio", "radio-selected",
                           "checkbox", "checkbox-selected", "dropdown", "radio-group")
        if grouped:
            parts.append(f'<g data-kind="{kind}">')
        if kind == "text":
            paint = _contrast(paint, surface_at(node, surface))
            size, baseline = text_layout(node, font_family)
            weight = ' font-weight="600"' if 14.5 <= size < 22 and len(node.text) < 30 else ""
            parts.append(f'<text x="{x}" y="{baseline:g}" fill="{paint}" '
                         f'font-family="{escape(resolved_family(font_family), quote=True)},sans-serif" font-size="{size:g}"'
                         f' textLength="{w}" lengthAdjust="spacingAndGlyphs"{weight}>'
                         f'{escape(node.text)}</text>')
        elif kind in ("circle", "ring", "radio", "radio-selected"):
            radius = min(w,h)/2 - .75
            cx, cy = x+w/2, y+h/2
            is_ring = kind in ("ring", "radio", "radio-selected")
            fill = "none" if is_ring else paint
            stroke = (_contrast(paint, surface, 3) if kind in ("radio", "radio-selected")
                      else paint if is_ring else "none")
            parts.append(f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" '
                         f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
            if kind == "radio-selected":
                parts.append(f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius*.48:g}" fill="{stroke}"/>')
        elif kind in ("rect", "button"):
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                         f'rx="{min(4, h/6):g}" fill="{paint}"/>')
            surface = paint
        elif kind == "beveled-button":
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{paint}"/>')
            parts.append(f'<path d="M{x+w-2} {y+1} V{y+h-2} H{x+1}" '
                         f'fill="none" stroke="#202020" stroke-width="4"/>')
            parts.append(f'<path d="M{x+2} {y+h-5} V{y+2} H{x+w-5}" '
                         f'fill="none" stroke="#f4f4f4" stroke-width="3"/>')
            parts.append(f'<path d="M{x+5} {y+h-8} V{y+6} H{x+w-8}" '
                         f'fill="none" stroke="#969696" stroke-width="1.5"/>')
            if any(child.text == "OK" for child in node.children):
                parts.append(f'<rect x="{x+10}" y="{y+10}" width="{max(0,w-23)}" '
                             f'height="{max(0,h-23)}" fill="none" stroke="#555555" '
                             f'stroke-width="1" stroke-dasharray="2 3"/>')
            surface = paint
        elif kind in ("outline", "outlined-button", "checkbox", "checkbox-selected"):
            stroke = paint if kind == "outline" else _contrast(paint, surface, 3)
            parts.append(f'<rect x="{x+.5:g}" y="{y+.5:g}" width="{max(0,w-1)}" '
                         f'height="{max(0,h-1)}" rx="{min(4,h/6):g}" '
                         f'fill="{node.background or "none"}" stroke="{stroke}" stroke-width="1.2"/>')
            if kind == "checkbox-selected":
                parts.append(f'<path d="M{x+3} {y+h/2:g} L{x+5.5:g} {y+h-3} '
                             f'L{x+w-2} {y+3}" fill="none" stroke="{stroke}" '
                             f'stroke-width="1.4"/>')
            surface = node.background or surface
        elif kind == "dropdown":
            parts.append(f'<rect x="{x+.5:g}" y="{y+.5:g}" width="{w-1}" height="{h-1}" '
                         f'fill="{node.background}" stroke="{paint}" stroke-width="1"/>')
            arrow = "#999999" if int(node.background[1:3],16) < 215 else "#606060"
            parts.append(f'<path d="M{x+w-13} {y+h/2-2:g} l4 4 4 -4" '
                         f'fill="none" stroke="{arrow}" stroke-width="1"/>')
        elif kind in ("tab", "tab-active"):
            parts.append(f'<rect x="{x+.5:g}" y="{y+.5:g}" width="{w-1}" height="{h-1}" '
                         f'fill="{node.background}" stroke="{paint}" stroke-width="1"/>')
            if kind == "tab-active":
                parts.append(f'<path d="M{x+1} {y+h-1} h{w-2}" stroke="#ffffff"/>')
        elif kind == "line":
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{paint}"/>')
        elif kind == "close-icon":
            parts.append(f'<path data-kind="close-icon" d="M{x} {y} L{x+w-1} {y+h-1} '
                         f'M{x+w-1} {y} L{x} {y+h-1}" fill="none" '
                         f'stroke="{paint}" stroke-width="{max(1,(min(w,h)-10)*.22):g}" '
                         f'stroke-linecap="square"/>')
        elif kind == "close-dot":
            parts.append(f'<circle data-kind="close-dot" cx="{x+w/2:g}" cy="{y+h/2:g}" '
                         f'r="{min(w,h)/2:g}" fill="{paint}"/>')
        elif kind == "gradient-title":
            parts.append(f'<image data-kind="gradient-title" x="{x}" y="{y}" width="{w}" '
                         f'height="{h}" preserveAspectRatio="none" '
                         f'xlink:href="data:image/png;base64,{node.image_data}"/>')
        elif kind in ("raster", "fidelity-raster"):
            parts.append(f'<image x="{x}" y="{y}" width="{w}" height="{h}" '
                         f'xlink:href="data:image/png;base64,{node.image_data}"/>')
        for child in node.children:
            draw(child, surface)
        if grouped:
            parts.append('</g>')

    # Surfaces precede controls; tiny title-bar icons must remain above even
    # broad rectangles recovered later by the quantized geometry pass.
    surfaces = [n for n in root.children if n.kind == "rect" and n.box[2]*n.box[3] > 1000]
    for node in sorted(surfaces,key=lambda n:n.box[2]*n.box[3],reverse=True):
        draw(node, root.color)
    for node in root.children:
        if node not in surfaces and node.kind not in ("text", "close-icon", "close-dot",
                                                     "fidelity-raster", "gradient-title"):
            draw(node, root.color)
    for node in root.children:
        if node.kind == "gradient-title":
            draw(node, root.color)
    for node in root.children:
        if node.kind == "text":
            draw(node, root.color)
    for node in root.children:
        if node.kind == "fidelity-raster":
            draw(node, root.color)
    for node in root.children:
        if node.kind in ("close-icon", "close-dot"):
            draw(node, root.color)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
