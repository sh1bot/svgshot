"""Side-by-side OCR target boxes and reconstructed text for inspection."""
from __future__ import annotations

import base64
import io
from xml.etree import ElementTree as ET

from PIL import Image

from .model import Node, flatten
from .svg import text_layout


SVG = "http://www.w3.org/2000/svg"
XLINK = "http://www.w3.org/1999/xlink"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", XLINK)


def make_diagnostic(source: Image.Image, output_svg: str, scene: Node,
                    font_family: str = "auto") -> str:
    width, height = source.size
    gap, header = 24, 28
    canvas = ET.Element(f"{{{SVG}}}svg", {
        "width": str(width*2+gap), "height": str(height+header),
        "viewBox": f"0 0 {width*2+gap} {height+header}",
    })
    ET.SubElement(canvas, f"{{{SVG}}}rect", {
        "width": "100%", "height": "100%", "fill": "#ffffff",
    })
    for x, label in ((0, "Input PNG / detected text"),
                     (width+gap, "Output SVG / target boxes and baselines")):
        title = ET.SubElement(canvas, f"{{{SVG}}}text", {
            "x": str(x+4), "y": "19", "font-family": "Arial,sans-serif",
            "font-size": "14", "fill": "#202020",
        })
        title.text = label
    data = io.BytesIO()
    source.convert("RGB").save(data, format="PNG")
    ET.SubElement(canvas, f"{{{SVG}}}image", {
        "x": "0", "y": str(header), "width": str(width), "height": str(height),
        f"{{{XLINK}}}href": "data:image/png;base64,"+base64.b64encode(data.getvalue()).decode("ascii"),
    })
    output_group = ET.SubElement(canvas, f"{{{SVG}}}g", {
        "transform": f"translate({width+gap},{header})",
    })
    rendered = ET.fromstring(output_svg)
    output_group.extend(list(rendered))
    for x_offset in (0, width+gap):
        overlay = ET.SubElement(canvas, f"{{{SVG}}}g", {
            "transform": f"translate({x_offset},{header})",
            "fill": "none", "stroke": "#e00084", "stroke-width": "1",
        })
        for node in flatten(scene):
            if node.kind != "text":
                continue
            x, y, w, h = node.box
            box = ET.SubElement(overlay, f"{{{SVG}}}rect", {
                "x": str(x), "y": str(y), "width": str(w), "height": str(h),
            })
            tooltip = ET.SubElement(box, f"{{{SVG}}}title")
            tooltip.text = node.text
            if x_offset:
                _, baseline = text_layout(node, font_family)
                ET.SubElement(overlay, f"{{{SVG}}}line", {
                    "x1": str(x), "x2": str(x+w), "y1": str(baseline),
                    "y2": str(baseline), "stroke": "#008bc2",
                    "stroke-dasharray": "3 2",
                })
    return ET.tostring(canvas, encoding="unicode", xml_declaration=True)+"\n"
