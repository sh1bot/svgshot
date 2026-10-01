"""Separate UIA-assisted capture/replay CLI; imports the existing vectorizer.

The Windows SDK helper supplies physical screen coordinates and a WGC PNG.
Replay and SVG generation work on any platform without Windows dependencies.
"""
from __future__ import annotations

import argparse
import json
import re
import math
import os
import shutil
import subprocess
import sys
import unicodedata
from html import escape
from pathlib import Path

from PIL import Image

from .model import Node, flatten
from .recognize import Options, reconstruct
from .svg import to_svg, _font
from .snapshot import read_snapshot
from .schema import render_view

TYPES = dict(enumerate((
    "button", "calendar", "checkbox", "combobox", "edit", "hyperlink", "image",
    "listitem", "list", "menu", "menubar", "menuitem", "progressbar", "radiobutton",
    "scrollbar", "slider", "spinner", "statusbar", "tab", "tabitem", "text",
    "toolbar", "tooltip", "tree", "treeitem", "custom", "group", "thumb",
    "datagrid", "dataitem", "document", "splitbutton", "window", "pane", "header",
    "headeritem", "table", "titlebar", "separator", "semanticzoom", "appbar",
), 50000))
CAPTIONS = {"button", "checkbox", "radiobutton", "hyperlink", "menuitem", "listitem",
            "treeitem", "tabitem", "headeritem", "text"}


def walk(root):
    if not isinstance(root, dict):
        return
    yield root
    for child in root.get("children", []):
        yield from walk(child)


def kind(item):
    return item.get("role") or TYPES.get(item.get("control_type"), "custom")


def validate_snapshot(snapshot, image):
    snapshot = render_view(snapshot)
    if snapshot.get("version") not in (1, 2) or not isinstance(snapshot.get("root"), dict):
        raise ValueError("Unsupported or incomplete UIA snapshot")
    bounds = snapshot.get("screen_bounds", [])
    if len(bounds) != 4 or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in bounds) or min(bounds[2:]) <= 0:
        raise ValueError("Snapshot has invalid screen_bounds")
    if snapshot.get("image_size") != list(image.size):
        raise ValueError("PNG dimensions do not match the UIA snapshot; use its original PNG")


def local_box(bounds, snapshot):
    if len(bounds) != 4 or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in bounds):
        return None
    ox, oy, sw, sh = snapshot["screen_bounds"]
    iw, ih = snapshot["image_size"]
    x, y, w, h = bounds
    if w <= 0 or h <= 0:
        return None
    left, top = max(0, round((x-ox)*iw/sw)), max(0, round((y-oy)*ih/sh))
    right, bottom = min(iw, round((x+w-ox)*iw/sw)), min(ih, round((y+h-oy)*ih/sh))
    return (left, top, right-left, bottom-top) if right > left and bottom > top else None


def contains(outer, inner):
    x, y, w, h = outer
    a, b, c, d = inner
    return a >= x-2 and b >= y-2 and a+c <= x+w+2 and b+d <= y+h+2


def overlap(a, b):
    x, y, w, h = a
    q, r, s, t = b
    return max(0, min(x+w, q+s)-max(x, q))*max(0, min(y+h, r+t)-max(y, r))


def visible_items(root, snapshot):
    if not isinstance(root, dict) or root.get("offscreen"):
        return
    if local_box(root.get("bounds", []), snapshot):
        yield root
    for child in root.get("children", []):
        yield from visible_items(child, snapshot)


def sample_color(image, box):
    # Median suppresses small dark captions without erasing large dark surfaces.
    from PIL import ImageStat
    x, y, w, h = box
    median = ImageStat.Stat(image.crop((x, y, x+w, y+h)).convert("RGB")).median
    return "#%02x%02x%02x" % tuple(median)


def normalize(text):
    return " ".join(text.replace("&", "").replace("…", "...").casefold().split())


def icon_text(text):
    return bool(text.strip()) and all(unicodedata.category(c) == "Co" or c.isspace() for c in text)


def trace_artwork(image, box):
    """Simplify screenshot ink into flat vector contours, without font dependence."""
    import numpy as np
    x, y, w, h = box
    patch = image.crop((x, y, x+w, y+h)).convert("RGB")
    patch.thumbnail((256, 256))
    scale_x, scale_y = w/patch.width, h/patch.height
    w, h = patch.size
    pixels = np.asarray(patch)
    background = np.median(np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1])), axis=0)
    foreground = np.max(np.abs(pixels.astype(float)-background), axis=2) > 35
    # Fixed palette bins keep tracing deterministic and remove antialias texture.
    colors = np.minimum((pixels.astype("uint16")//48)*48+24, 255)
    paths = []
    for color in np.unique(colors[foreground], axis=0):
        mask = (foreground & np.all(colors == color, axis=2)).astype('uint8')
        # Trace pixel boundaries; omit collinear vertices to keep paths compact.
        edges = {}
        for row, column in np.argwhere(mask):
            a, b = int(column), int(row)
            for start, end, outside in (
                ((a,b),(a+1,b), b == 0 or not mask[b-1,a]),
                ((a+1,b),(a+1,b+1), a == w-1 or not mask[b,a+1]),
                ((a+1,b+1),(a,b+1), b == h-1 or not mask[b+1,a]),
                ((a,b+1),(a,b), a == 0 or not mask[b,a-1]),
            ):
                if outside:
                    edges.setdefault(start, []).append(end)
        segments = []
        while edges:
            start = next(iter(edges))
            points, current = [start], start
            while current in edges:
                following = edges[current].pop()
                if not edges[current]:
                    del edges[current]
                current = following
                if current == start:
                    break
                points.append(current)
            if current != start or len(points) < 4:
                continue
            simple = [point for i, point in enumerate(points)
                      if (point[0]-points[i-1][0], point[1]-points[i-1][1]) !=
                         (points[(i+1)%len(points)][0]-point[0], points[(i+1)%len(points)][1]-point[1])]
            if len(simple) >= 3:
                segments.append("M"+" L".join(f"{x+a*scale_x:g} {y+b*scale_y:g}" for a, b in simple)+" Z")
        if segments:
            paths.append({"d": " ".join(segments), "fill": "#%02x%02x%02x" % tuple(color)})
    return Node("capture-artwork", box, vector_data={"paths": paths})


def ink_box(image, box):
    """UIA bounds include line spacing; recover only the visible foreground ink."""
    import numpy as np
    x, y, w, h = box
    pixels = np.asarray(image.crop((x, y, x+w, y+h)).convert("RGB"))
    background = np.median(pixels.reshape(-1, 3), axis=0)
    mask = np.max(np.abs(pixels.astype(float)-background), axis=2) > 45
    ys, xs = np.where(mask)
    if len(xs):
        return (x+int(xs.min()), y+int(ys.min()), int(xs.max()-xs.min()+1), int(ys.max()-ys.min()+1))
    return None


def uia_foreground(attributes):
    """Decode UIA's foreground COLORREF (0x00BBGGRR), when supported."""
    attribute = attributes.get("40008", {})
    value = attribute.get("value")
    if attribute.get("status") == "value" and type(value) is int and 0 <= value <= 0xffffff:
        return (value & 255, (value >> 8) & 255, (value >> 16) & 255)
    return None


def terminal_text(image, item, snapshot):
    """Keep UIA's fixed cells and colours; sample pixels only for missing colours."""
    import numpy as np
    nodes = []
    for line in item.get("text_ranges", []):
        rects = line.get("rectangles", [])
        text = line.get("text", "").rstrip("\r\n")
        if len(rects) != 1 or not text.strip():
            continue
        box = local_box(rects[0], snapshot)
        if not box:
            continue
        x, y, width, height = box
        cell = width / len(text)
        pixels = np.asarray(image.crop((x, y, x+width, y+height)).convert("RGB"))
        background = np.median(pixels.reshape(-1, 3), axis=0)
        def foreground(r):
            color = r.get("style", {}).get("foreground")
            if isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                return tuple(int(color[i:i+2], 16) for i in (1, 3, 5))
            return uia_foreground(r.get("attributes", {}))
        foregrounds = [foreground(line)] * len(text)
        offset = 0
        for run in line.get("format_runs", {}).get("ranges", []):
            content = run.get("text", "").rstrip("\r\n")
            if not content:
                continue
            start = text.find(content, offset)
            if start < 0:
                continue
            offset = start + len(content)
            run_color = foreground(run)
            if run_color is not None:
                foregrounds[start:offset] = [run_color] * len(content)
        runs = []
        for index, char in enumerate(text.rstrip()):
            semantic_color = foregrounds[index]
            color = semantic_color or (runs[-1][2] if runs else (204, 204, 204))
            if semantic_color is None and not char.isspace():
                patch = pixels[:, round(index*cell):round((index+1)*cell)]
                distance = np.max(np.abs(patch.astype(float)-background), axis=2)
                ink = patch[(distance > 60) & (distance >= np.percentile(distance, 80))]
                if len(ink):
                    color = tuple(int(v) for v in np.median(ink, axis=0))
            if runs and (color == runs[-1][2] or semantic_color is None
                         and max(abs(a-b) for a, b in zip(color, runs[-1][2])) < 35):
                runs[-1][1] += char
            else:
                runs.append([index, char, color])
        for start, content, color in runs:
            left, right = round(x+start*cell), round(x+(start+len(content))*cell)
            nodes.append(Node("text", (left, y, right-left, height), text=content,
                              color="#%02x%02x%02x" % color,
                              vector_data={"source": "uia", "uia_id": item.get("id", ""),
                                           "font_family": "monospace", "font_weight": 400,
                                           "font_size": height*.8, "baseline": y+height*.8}))
    return nodes


def merge_uia(scene, image, snapshot):
    """UIA supplies exact strings/states. OCR supplies ink placement and gaps.

    Keep semantic names separate from visible captions: image and container
    names are not painted as text. The original UIA tree is retained unchanged.
    """
    snapshot = render_view(snapshot)
    items = list(visible_items(snapshot["root"], snapshot))
    chrome = {id(child) for item in items if kind(item) == "titlebar"
              for child in walk(item) if child is not item}
    ocr = [n for n in flatten(scene) if n.kind == "text"]
    chrome_boxes = [local_box(n["bounds"], snapshot) for n in items if id(n) in chrome]
    suppressed = {id(n) for n in ocr if any(contains(b, n.box) for b in chrome_boxes)}
    controls, text_nodes, artwork = [], [], []
    text_boxes = [local_box(n["bounds"], snapshot) for n in items if kind(n) == "text"]
    terminals = {id(n) for n in items if n.get("class_name") == "TermControl"}
    authoritative = [local_box(n["bounds"], snapshot) for n in items
                     if id(n) in terminals or kind(n) in {"list", "tree", "appbar", "tab", "header", "statusbar"}
                     or kind(n) == "edit" and n.get("framework_id") == "XAML"]
    surfaces = []
    for item in items:
        if id(item) in terminals or kind(item) in {"pane", "list", "tree", "appbar", "tab", "tabitem", "header", "statusbar"}:
            b = local_box(item["bounds"], snapshot)
            if b[2]*b[3] > 4000:
                surfaces.append(Node("rect", b, color=sample_color(image, b)))
    if terminals:
        top = min(local_box(n["bounds"], snapshot)[1] for n in items if id(n) in terminals)
        if top > 0:
            header = (0, 0, image.width, top)
            surfaces.append(Node("rect", header, color=sample_color(image, header)))
    surfaces.sort(key=lambda n: n.box[2]*n.box[3], reverse=True)
    cells = {id(child) for item in items if kind(item) in {"listitem", "dataitem"}
             for child in walk(item) if kind(child) == "edit"}
    native_caption_buttons = []
    emitted = set()
    passwords = [local_box(n["bounds"], snapshot) for n in items if n.get("password")]

    def emit(text, box, owner, centered=False):
        text = text.strip("\r\n")
        if not text or not box:
            return
        if icon_text(text):
            artwork.append(trace_artwork(image, box))
            text_boxes.append(box)
            suppressed.update(id(n) for n in ocr if overlap(n.box, box) > n.box[2]*n.box[3]*.35)
            return
        text_boxes.append(box)
        matches = [n for n in ocr if id(n) not in suppressed and
                   overlap(box, n.box) > min(n.box[2]*n.box[3], box[2]*box[3])*.35]
        exact = [n for n in matches if normalize(n.text) == normalize(text)]
        placement = exact
        ink = ink_box(image, box) if kind(owner) == "text" or id(owner) in cells or kind(owner) == "treeitem" else None
        if not ink and placement:
            ink = max(placement, key=lambda n: overlap(n.box, box)).box
        if not ink:
            x, y, w, h = box
            height = min(14, max(7, h-4))
            width = min(max(1, w-8), max(8, round(len(text)*height*.52)))
            ink = (x+(w-width)//2 if centered else x, y+(h-height)//2, width, height)
        # A TextBlock can expose its full name even when the visible label clips.
        # Use an ellipsis in the visual layer; the semantic tree keeps the full name.
        if kind(owner) == "text" and owner.get("framework_id") == "XAML":
            font = _font("auto")
            if font:
                _, top, _, bottom = font.getbbox(text, anchor="ls")
                size = ink[3]*100/max(1, bottom-top)
                if font.getlength(text)*size/100 > ink[2]*1.2:
                    while text and font.getlength(text+"…")*size/100 > ink[2]:
                        text = text[:-1]
                    text += "…"
        signature = (text, ink)
        if signature in emitted:
            return
        emitted.add(signature)
        for n in matches:
            suppressed.add(id(n))
        node = Node("text", ink, text=text, color="#171717",
                    vector_data={"source": "uia", "uia_id": owner.get("id", ""), "font_weight": 400})
        text_nodes.append(node)

    for item in items:
        box = local_box(item["bounds"], snapshot)
        role = kind(item)
        if item.get("password") or id(item) in chrome:
            continue
        if role == "button" and item.get("class_name") == "Button" and item.get("name"):
            native_caption_buttons.append(box)
        if role == "listitem":
            name_cells = [n for n in walk(item) if kind(n) == "edit" and id(n) in cells]
            if name_cells:
                cell = local_box(name_cells[0]["bounds"], snapshot)
                if cell and cell[0]-box[0] >= 28:
                    artwork.append(trace_artwork(image, (box[0], box[1]+2, cell[0]-box[0], max(1,box[3]-4))))
        if role == "image":
            artwork.append(trace_artwork(image, box))
        native = (item.get("class_name") == "Button" and item.get("framework_id") == "Win32"
                  or not item.get("framework_id"))
        if ((role in {"button", "splitbutton", "checkbox", "radiobutton"} and native)
                or role == "edit" and id(item) not in cells
                or role == "combobox" or role == "tabitem" and native):
            x, y, w, h = box
            shape = {"button": "outlined-button", "splitbutton": "outlined-button", "edit": "outline",
                     "combobox": "dropdown", "tabitem": "tab-active" if item.get("states", {}).get("selected") else "tab"}.get(role, "outline")
            if role in {"checkbox", "radiobutton"}:
                size = min(16, h, w)
                shape = "checkbox" if role == "checkbox" else "radio"
                if item.get("states", {}).get("toggle") == 1 or item.get("states", {}).get("selected"):
                    shape += "-selected"
                visual_box = (x, y+(h-size)//2, size, size)
            else:
                visual_box = box
            controls.append(Node(shape, visual_box, color="#505050", background=sample_color(image, visual_box),
                                 vector_data={"source": "uia", "uia_id": item.get("id", "")}))
        if id(item) in terminals:
            text_nodes.extend(terminal_text(image, item, snapshot))
            continue
        ranges = item.get("text_ranges", [])
        if ranges:
            for line in ranges:
                rects = line.get("rectangles", [])
                # Never duplicate a single string across multiple rectangles.
                # Native helper normally splits ranges by TextUnit_Line.
                if len(rects) == 1:
                    emit(line.get("text", ""), local_box(rects[0], snapshot), item)
            if any(len(line.get("rectangles", [])) == 1 for line in ranges):
                continue
        name = item.get("name", "")
        if role == "titlebar" and snapshot["root"].get("name"):
            x, y, w, h = box
            if w > 100:
                emit(snapshot["root"]["name"], (x+8, y+3, max(1, w-140), max(1, h-6)), item)
                # Window chrome glyphs sometimes produce stray OCR characters.
                # The title is already known exactly; glyphs stay vector artwork.
                suppressed.update(id(n) for n in ocr if contains(box, n.box))
        if role in CAPTIONS and name:
            children = [n for n in walk(item) if n is not item and kind(n) == "text" and not n.get("offscreen")]
            if any(normalize(n.get("name", "")) == normalize(name) for n in children):
                continue
            if role == "button" and any(kind(n) in {"image", "text"} for n in walk(item) if n is not item):
                continue  # The accessible name of an icon button is not a caption.
            if role in {"checkbox", "radiobutton"}:
                x, y, w, h = box
                pad = min(16, h, w)+5
                if w <= pad:
                    continue
                box = (x+pad, y, w-pad, h)
            if role == "listitem" and any(kind(n) == "edit" and normalize(str(n.get("states", {}).get("value", ""))) == normalize(name) for n in walk(item)):
                continue
            if role == "treeitem":
                # Shell names include navigation hints absent from the caption.
                x, y, w, h = box
                # Shell item bounds describe the caption, with an adjacent icon.
                if item.get("framework_id") == "Win32" or item.get("framework_id") == "DirectUI":
                    if x >= 28:
                        artwork.append(trace_artwork(image, (x-28, y+(h-24)//2, 24, min(24,h))))
                name = name.removeprefix("Start of Quick Access - ").removeprefix("End of Quick Access - ").removesuffix(" (pinned)")
            if role in {"button", "radiobutton", "tabitem"} and (not native or box[2] < box[3]*1.8 and len(name) > 3) and not any(normalize(n.text) == normalize(name) for n in ocr if contains(box, n.box)):
                continue
            emit(name, box, item, centered=role in {"button", "tabitem"})
        elif role == "splitbutton" and name:
            if not any(kind(n) == "text" for n in walk(item) if n is not item) and any(normalize(n.text) == normalize(name) for n in ocr if contains(box, n.box)):
                emit(name, box, item)
        elif role == "edit" and item.get("states", {}).get("value"):
            x, y, w, h = box
            # A multiline edit should expose TextPattern; avoid squeezing its
            # entire (possibly scrolled) value into one line.
            value = str(item["states"]["value"])
            if "\n" not in value and "\r" not in value:
                emit(value, (x+4, y+2, max(1, w-8), max(1, h-4)), item)

    suppressed.update(id(n) for n in ocr if any(
        kind(item) == "scrollbar" and contains(local_box(item["bounds"], snapshot), n.box)
        for item in items))
    suppressed.update(id(n) for n in ocr if any(overlap(n.box, a.box) > n.box[2]*n.box[3]*.35 for a in artwork))
    for n in ocr:
        if n.box[1] < 45 and n.box[0] > image.width-160 and len(n.text) <= 2:
            artwork.append(trace_artwork(image, n.box))
            suppressed.add(id(n))

    def prune(node):
        retained = []
        for child in node.children:
            if child.kind == "text" and any(overlap(child.box, b) > child.box[2]*child.box[3]*.35 for b in text_boxes):
                continue
            if child.kind != "text" and sum(overlap(b, child.box) for b in authoritative) > child.box[2]*child.box[3]*.15:
                for sub in child.children:
                    prune(sub)
                    if sub.kind == "text" and id(sub) not in suppressed:
                        retained.append(sub)
                continue
            if child.kind != "text" and not child.children and any(contains(b, child.box) for b in text_boxes + [n.box for n in artwork] + [local_box(n["bounds"], snapshot) for n in items if kind(n) == "scrollbar"]):
                continue
            if child.kind in {"outline", "input-field", "dropdown"} and any(overlap(b, child.box) > child.box[2]*child.box[3]*.3 for b in text_boxes):
                continue
            if child.kind == "header-icon" and any(contains(b, child.box) for b in native_caption_buttons):
                # This detector fits a large window illustration; it can falsely
                # fit a native button's rectangular border and caption strokes.
                continue
            if id(child) in suppressed:
                continue
            if child.kind == "text" and any(overlap(child.box, b) for b in passwords):
                continue
            # Remove duplicate recognized control outlines, not enclosing panels
            # or illustrations. OCR text and icon artwork survive this pass.
            if child.kind in {"button", "outlined-button", "checkbox", "checkbox-selected", "radio", "radio-selected", "dropdown", "outline", "tab", "tab-active"}:
                if any(overlap(child.box, n.box) / max(1, child.box[2]*child.box[3], n.box[2]*n.box[3]) > .75 for n in controls):
                    # Detach captions before discarding the old control.
                    for sub in child.children:
                        if id(sub) not in suppressed:
                            retained.append(sub)
                    continue
            prune(child)
            retained.append(child)
        node.children = retained
    prune(scene)
    # Insert outlines after underlying panels; to_svg puts root text above them.
    surface_kinds = {"content-panel", "dialog-panel", "window-header", "footer-panel", "rect", "decorative-background", "selected-row", "text-selection"}
    controls.sort(key=lambda n: n.box[2]*n.box[3], reverse=True)
    scene.children = ([n for n in scene.children if n.kind in surface_kinds and
                                   not any(contains(b, n.box) for b in authoritative)] + surfaces + controls
                      + [n for n in scene.children if n.kind not in surface_kinds] + artwork + text_nodes)
    return scene


def describe(item):
    role = item.get("localized_control_type") or kind(item)
    pieces = [item.get("name", ""), role]
    if item.get("password"):
        pieces.append("password field")
    states = item.get("states", {})
    if not item.get("password"):
        for key in ("value", "range_value"):
            if key in states and str(states[key]) != item.get("name"):
                pieces.append(str(states[key]))
    if "toggle" in states:
        pieces.append({0: "unchecked", 1: "checked", 2: "mixed"}.get(states["toggle"], "unknown toggle state"))
    if states.get("selected"):
        pieces.append("selected")
    if "expand_collapse" in states:
        pieces.append({0: "collapsed", 1: "expanded", 2: "partly expanded", 3: "leaf"}.get(states["expand_collapse"], ""))
    if not item.get("enabled", True):
        pieces.append("disabled")
    if states.get("read_only"):
        pieces.append("read only")
    if item.get("keyboard_focus"):
        pieces.append("focused")
    for key in ("description", "help_text", "access_key", "accelerator_key"):
        if item.get(key):
            pieces.append(item[key])
    return ", ".join(str(s) for s in pieces if s)


def semantic_svg(scene, snapshot, font_family="auto"):
    original = snapshot
    snapshot = render_view(snapshot)
    visual = to_svg(scene, font_family)
    start, body = visual.split(">", 1)
    title = snapshot["root"].get("name") or "Window capture"
    # An atomic role=img would hide the navigable descendants.
    start = start.replace('role="img"', 'role="graphics-document group" aria-labelledby="capture-title" aria-describedby="capture-description"')
    parts = [start+">", f'<title id="capture-title">{escape(title)}</title>',
             '<desc id="capture-description">Static window capture. Represented controls are informational and cannot be operated.</desc>',
             '<metadata id="uia-snapshot">'+escape(json.dumps(original, ensure_ascii=False))+'</metadata>',
             '<g aria-hidden="true" data-kind="visual-reconstruction">', body.rsplit("</svg>", 1)[0]]
    for node in flatten(scene):
        if node.kind == "capture-artwork":
            for path in node.vector_data["paths"]:
                parts.append(f'<path data-kind="capture-artwork" d="{path["d"]}" fill="{path["fill"]}" fill-rule="evenodd"/>')
    parts.append('</g>')
    serial = 0

    def accessible(item):
        nonlocal serial
        if not isinstance(item, dict) or item.get("offscreen"):
            return
        box = local_box(item.get("bounds", []), snapshot)
        meaningful = box and (item.get("control_element") or item.get("content_element") or item.get("name"))
        if meaningful:
            serial += 1
            x, y, w, h = box
            label = describe(item)
            # Descriptive groups avoid promising live button/edit interactions.
            # Exact original role/properties remain in data attributes + metadata.
            parts.append(f'<g id="capture-node-{serial}" role="group" aria-label="{escape(label, quote=True)}" '
                         f'data-uia-id="{escape(item.get("id", ""), quote=True)}" data-uia-role="{kind(item)}">')
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#000000" fill-opacity="0" aria-hidden="true"/>')
            # UIA text ranges may carry document content absent from Name/Value.
            if not item.get("password"):
                for line in item.get("text_ranges", []):
                    text = line.get("text", "")
                    if text and normalize(text) != normalize(item.get("name", "")):
                        parts.append(f'<g role="group" aria-label="{escape(text, quote=True)}"><title>{escape(text)}</title></g>')
        for child in item.get("children", []):
            accessible(child)
        if meaningful:
            parts.append('</g>')
    accessible(snapshot["root"])
    extras = [n for n in flatten(scene) if n.kind == "text" and n.vector_data.get("source") != "uia"]
    if extras:
        parts.append('<g role="group" aria-label="Additional visible text identified by OCR">')
        for node in extras:
            parts.append(f'<g role="group" aria-label="{escape(node.text, quote=True)}"><title>{escape(node.text)}</title></g>')
        parts.append('</g>')
    parts.append('</svg>')
    return "\n".join(parts)+"\n"


def accessible_html(svg, snapshot):
    snapshot = render_view(snapshot)
    def outline(item):
        if not isinstance(item, dict) or item.get("offscreen"):
            return ""
        children = "".join(outline(n) for n in item.get("children", []))
        label = escape(describe(item))
        lines = ""
        if not item.get("password"):
            lines = "".join('<li>'+escape(line.get("text", ""))+'</li>' for line in item.get("text_ranges", []))
        nested = '<ul>'+lines+children+'</ul>' if children or lines else ""
        return '<li>'+label+nested+'</li>' if local_box(item.get("bounds", []), snapshot) else children
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Window capture</title>'
            '<style>body{font:1rem system-ui;max-width:80rem;margin:2rem auto;padding:1rem}svg{max-width:100%;height:auto}</style>'
            '<h1>Static window capture</h1>'+svg+'<h2>Captured interface information</h2>'
            '<p>This textual outline also works in readers that flatten SVG accessibility.</p><ul>'
            +outline(snapshot["root"])+"</ul></html>\n")


def native_helper(explicit):
    if explicit:
        return str(explicit.resolve())
    env = os.environ.get("SVGSHOT_CAPTURE_HELPER")
    if env:
        return env
    executable = shutil.which("svgshot-capture-win")
    if executable:
        return executable
    checkout = Path(__file__).resolve().parents[1]
    for path in (checkout/"build/capture/Release/svgshot-capture-win.exe", checkout/"build/capture/svgshot-capture-win.exe"):
        if path.is_file():
            return str(path)
    raise RuntimeError("Build capture/windows with CMake, or set SVGSHOT_CAPTURE_HELPER to svgshot-capture-win.exe")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render a semantic PNG capture as SVG, or capture a Windows window")
    parser.add_argument("output", type=Path)
    parser.add_argument("--image", type=Path, help="Replay a PNG with an embedded UIA snapshot")
    parser.add_argument("--uia", type=Path, help="Optional JSON sidecar for a legacy PNG capture")
    parser.add_argument("--helper", type=Path, help="Native Windows helper executable")
    parser.add_argument("--hwnd", help="Capture a specific window handle (decimal or 0x hexadecimal)")
    parser.add_argument("--foreground", action="store_true", help="Capture the foreground window after --delay")
    parser.add_argument("--delay", type=int, default=0)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--include-hidden-content", action="store_true", help="Opt in to offscreen UIA content and full values; may include private data")
    parser.add_argument("--allow-raster", action="store_true", help="Opt in to the existing small-raster fallback")
    parser.add_argument("--scene", type=Path)
    parser.add_argument("--html", type=Path, help="Accessible HTML preview with inline SVG and a text outline")
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != ".svg":
            raise ValueError("Output must have an .svg extension")
        if args.uia and not args.image:
            raise ValueError("--uia requires --image")
        if not 0 <= args.delay <= 60 or (args.hwnd and args.foreground):
            raise ValueError("Delay must be 0–60; choose --hwnd or --foreground")
        if args.image and (args.hwnd or args.foreground or args.delay or args.helper or args.include_hidden_content):
            raise ValueError("Window selection options do not apply to replay")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.image:
            image_path, uia_path = args.image, args.uia
        else:
            if sys.platform != "win32":
                raise RuntimeError("Live capture requires Windows; use --image and --uia to replay elsewhere")
            prefix = args.output.with_suffix("")
            image_path, uia_path = Path(str(prefix)+".png"), Path(str(prefix)+".uia.json")
            command = [native_helper(args.helper), "--out", str(prefix.resolve())]
            if args.include_hidden_content:
                command += ["--include-hidden-content"]
            if args.hwnd:
                command += ["--hwnd", args.hwnd]
            if args.foreground:
                command += ["--foreground"]
            if args.delay:
                command += ["--delay", str(args.delay)]
            result = subprocess.run(command, check=False)
            if result.returncode:
                return result.returncode
        outputs = [args.output, args.scene, args.html]
        inputs = {p.resolve() for p in (image_path, uia_path) if p}
        if any(p and p.resolve() in inputs for p in outputs):
            raise ValueError("Output paths must not overwrite capture inputs")
        if len({p.resolve() for p in outputs if p}) != sum(p is not None for p in outputs):
            raise ValueError("Output, scene, and HTML paths must differ")
        if args.image:
            snapshot = json.loads(uia_path.read_text(encoding="utf-8")) if uia_path else read_snapshot(image_path)
        else:
            # New helpers embed the snapshot. Only fall back for pre-chunk helpers.
            from .snapshot import png_chunks
            if any(png_chunks(image_path)):
                snapshot = read_snapshot(image_path)
            else:
                snapshot = json.loads(uia_path.read_text(encoding="utf-8"))
        with Image.open(image_path) as image:
            image.load()
            validate_snapshot(snapshot, image)
            options = Options.from_file(str(args.config) if args.config else None)
            options.raster_fallback = args.allow_raster
            options.fidelity_fallback = False
            if args.no_ocr:
                options.ocr = False
            scene = merge_uia(reconstruct(image, options), image, snapshot)
            svg = semantic_svg(scene, snapshot, options.font_family)
            args.output.write_text(svg, encoding="utf-8")
            if args.scene:
                args.scene.write_text(json.dumps(scene.to_dict(), indent=2)+"\n", encoding="utf-8")
            if args.html:
                args.html.write_text(accessible_html(svg, snapshot), encoding="utf-8")
        for warning in snapshot.get("warnings", []):
            print("svgshot capture: "+warning, file=sys.stderr)
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        print(f"svgshot capture: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
