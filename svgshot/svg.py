"""Emit deliberately simple, editable SVG."""
from __future__ import annotations

from html import escape

from .model import Node


def _contrast(foreground: str, background: str, minimum=4.5) -> str:
    def luminance(hexcolor):
        values = [int(hexcolor[i:i+2], 16) / 255 for i in (1, 3, 5)]
        values = [v / 12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values]
        return sum(a*b for a, b in zip(values, (.2126, .7152, .0722)))
    a, b = luminance(foreground), luminance(background)
    if (max(a,b)+.05)/(min(a,b)+.05) >= minimum:
        return foreground
    return "#171717" if (b+.05)/.05 >= (1.05)/(b+.05) else "#ffffff"


def to_svg(root: Node) -> str:
    _, _, width, height = root.box
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" role="img">',
             f'<rect width="{width}" height="{height}" fill="{root.color}"/>']

    def draw(node: Node, surface: str):
        x, y, w, h = node.box
        kind = node.kind
        paint = node.color
        grouped = kind in ("button", "outlined-button", "radio", "radio-selected",
                           "checkbox", "radio-group")
        if grouped:
            parts.append(f'<g data-kind="{kind}">')
        if kind == "text":
            paint = _contrast(paint, surface)
            # OCR returns ink bounds, not the font's em box. Size/baseline are
            # approximate; validation allows slight displacement of text.
            size = max(8, round(h * 1.28, 1))
            parts.append(f'<text x="{x}" y="{y+h}" fill="{paint}" '
                         f'font-family="Segoe UI,Arial,sans-serif" font-size="{size}">'
                         f'{escape(node.text)}</text>')
        elif kind in ("circle", "ring", "radio", "radio-selected"):
            radius = min(w,h)/2 - .75
            cx, cy = x+w/2, y+h/2
            is_ring = kind in ("ring", "radio", "radio-selected")
            fill = "none" if is_ring else paint
            stroke = _contrast(paint, surface, 3) if is_ring else "none"
            parts.append(f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" '
                         f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
            if kind == "radio-selected":
                parts.append(f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius*.48:g}" fill="{stroke}"/>')
        elif kind in ("rect", "button"):
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                         f'rx="{min(4, h/6):g}" fill="{paint}"/>')
            surface = paint
        elif kind in ("outline", "outlined-button", "checkbox"):
            stroke = _contrast(paint, surface, 3)
            parts.append(f'<rect x="{x+.5:g}" y="{y+.5:g}" width="{max(0,w-1)}" '
                         f'height="{max(0,h-1)}" rx="{min(4,h/6):g}" '
                         f'fill="none" stroke="{stroke}" stroke-width="1.5"/>')
        elif kind == "line":
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{paint}"/>')
        elif kind == "raster":
            parts.append(f'<image x="{x}" y="{y}" width="{w}" height="{h}" '
                         f'href="data:image/png;base64,{node.image_data}"/>')
        for child in node.children:
            draw(child, surface)
        if grouped:
            parts.append('</g>')

    # Text goes last, including text inside controls.
    for node in root.children:
        if node.kind != "text":
            draw(node, root.color)
    for node in root.children:
        if node.kind == "text":
            draw(node, root.color)
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
