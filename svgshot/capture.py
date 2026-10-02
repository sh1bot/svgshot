"""Merge platform-neutral accessibility observations into a visual reconstruction."""
from __future__ import annotations

import argparse
import re
import math
import sys
import unicodedata
from html import escape

from PIL import Image

from .model import Node, flatten
from .artwork import colored_icons, trace_artwork
from .recognize import _ocr_ui
from .svg import to_svg, _font
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
    if snapshot.get("_renderer_view") is not True or not isinstance(snapshot.get("root"), dict):
        raise ValueError("Unsupported or incomplete capture snapshot")
    bounds = snapshot.get("screen_bounds", [])
    if len(bounds) != 4 or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in bounds) or min(bounds[2:]) <= 0:
        raise ValueError("Snapshot has invalid screen_bounds")
    if snapshot.get("image_size") != list(image.size):
        raise ValueError("PNG dimensions do not match the semantic snapshot; use its original PNG")


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


def visible_items(root, snapshot, viewport=None):
    if not isinstance(root, dict) or root.get("offscreen"):
        return
    box = local_box(root.get("bounds", []), snapshot)
    if box and (viewport is None or overlap(box, viewport) > 0):
        yield root
    elif box and viewport:
        return
    if box and kind(root) in {"list", "tree", "table", "datagrid"}:
        viewport = box
    for child in root.get("children", []):
        yield from visible_items(child, snapshot, viewport)


def sample_color(image, box):
    # Median suppresses small dark captions without erasing large dark surfaces.
    from PIL import ImageStat
    x, y, w, h = box
    median = ImageStat.Stat(image.crop((x, y, x+w, y+h)).convert("RGB")).median
    return "#%02x%02x%02x" % tuple(median)


def normalize(text):
    return " ".join(text.replace("&", "").replace("…", "...").casefold().split())


def exposed_by_descendant(item, text):
    """Whether a container's text is already represented by a child control."""
    target = normalize(text)
    if not target:
        return False
    for child in walk(item):
        if child is item:
            continue
        if kind(child) in CAPTIONS and normalize(child.get("name", "")) == target:
            return True
        if any(normalize(line.get("text", "")) == target
               for line in child.get("text_ranges", [])):
            return True
    return False


def icon_text(text):
    return bool(text.strip()) and all(unicodedata.category(c) == "Co" or c.isspace() for c in text)



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


def semantic_icon_boxes(image, snapshot):
    """Identify leading icons and header sort marks before their pixels reach OCR."""
    snapshot = render_view(snapshot)
    items = list(visible_items(snapshot["root"], snapshot))
    slots = []
    menus = [local_box(n["bounds"],snapshot) for n in items if kind(n) == "menubar"]
    if menus and min(b[1] for b in menus) >= 24:
        slots.append((0, 0, 40, min(45,min(b[1] for b in menus))))
    for item in items:
        x, y, w, h = local_box(item["bounds"], snapshot)
        if kind(item) == "listitem" and 14 <= h <= 48 and w > 80:
            slots.append((x, y, min(32,w), h))
        elif kind(item) == "treeitem" and 12 <= h <= 48:
            slots.append((max(0,x-24), max(0,y-4), 36, h+8))
    boxes = colored_icons(image, slots)
    import numpy as np
    from scipy import ndimage
    pixels = np.asarray(image.convert("RGB"))
    for item in items:
        if kind(item) != "headeritem":
            continue
        x,y,w,h = local_box(item["bounds"],snapshot)
        strip = pixels[y:y+h//3,x:x+w]
        labels, _ = ndimage.label(strip.max(axis=2) < 170, structure=np.ones((3,3)))
        for slices in ndimage.find_objects(labels):
            if slices is None:
                continue
            ys,xs = slices
            iw,ih = xs.stop-xs.start,ys.stop-ys.start
            # Exclude cut-off caption strokes along the strip's lower edge.
            if 6 <= iw <= 16 and 3 <= ih <= 10 and ys.stop < strip.shape[0]:
                boxes.append((x+xs.start-1,y+ys.start-1,iw+2,ih+2))
    return sorted(set(boxes))


def tab_outline(image, box):
    """Fit sloping tab sides inside semantic bounds, away from caption ink."""
    import numpy as np
    x,y,w,h = box
    if w < 30 or h < 12:
        return {}
    pixels = np.asarray(image.crop((x,y,x+w,y+h)).convert('RGB')).astype(float)
    background = np.median(pixels.reshape(-1,3),axis=0)
    edge = np.linalg.norm(pixels-background,axis=2) > 65
    margin = min(w//3, round(h*.65))
    corners = []
    for rows in (range(1,4),range(h-4,h-1)):
        left,right = [],[]
        for row in rows:
            a = np.flatnonzero(edge[row,:margin])
            b = np.flatnonzero(edge[row,w-margin:])
            if len(a): left.append(int(a[0]))
            if len(b): right.append(int(b[0])+w-margin)
        if not left or not right:
            return {}
        corners.append((float(np.median(left)),float(np.median(right))))
    (tl,tr),(bl,br) = corners
    if max(abs(tl-bl),abs(tr-br)) < 3:
        return {}
    return {'tab_corners':[tl,tr,br,bl]}


def merge_uia(scene, image, snapshot, language="eng", *, ocr_enabled=True):
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
    controls, text_nodes = [], []
    icon_boxes = semantic_icon_boxes(image, snapshot)
    artwork = [trace_artwork(image, b) for b in icon_boxes]
    text_boxes = [local_box(n["bounds"], snapshot) for n in items if kind(n) == "text"]
    terminals = {id(n) for n in items if n.get("class_name") == "TermControl"}
    authoritative = [local_box(n["bounds"], snapshot) for n in items
                     if id(n) in terminals or kind(n) in {"list", "tree", "appbar", "tab", "tabitem", "header", "statusbar"}
                     or kind(n) == "edit" and n.get("framework_id") == "XAML"]
    surfaces = []
    for item in items:
        if (id(item) in terminals or kind(item) in {"list", "tree", "appbar", "tab", "tabitem", "header", "statusbar"}
                or kind(item) == "pane" and local_box(item["bounds"], snapshot)[3] < 150
                and local_box(item["bounds"], snapshot)[2] > image.width*.5):
            b = local_box(item["bounds"], snapshot)
            if b[2]*b[3] > 4000:
                surfaces.append(Node("rect", b, color=sample_color(image, b)))
    if terminals:
        top = min(local_box(n["bounds"], snapshot)[1] for n in items if id(n) in terminals)
        if top > 0:
            header = (0, 0, image.width, top)
            surfaces.append(Node("rect", header, color=sample_color(image, header)))
    surfaces = list({(n.box, n.color): n for n in surfaces}.values())
    surfaces.sort(key=lambda n: n.box[2]*n.box[3], reverse=True)
    cells = {id(child) for item in items if kind(item) in {"listitem", "dataitem"}
             for child in walk(item) if kind(child) == "edit"}
    native_caption_buttons = []
    emitted = set()
    passwords = [local_box(n["bounds"], snapshot) for n in items
                 if n.get("password") and not n.get("debug_unredacted")]

    def emit(text, box, owner, centered=False):
        text = text.strip("\r\n")
        if not text.strip() or not box:
            return
        if icon_text(text):
            artwork.append(trace_artwork(image, box))
            text_boxes.append(box)
            suppressed.update(id(n) for n in ocr if overlap(n.box, box) > n.box[2]*n.box[3]*.35)
            return
        if id(owner) in cells:
            x,y,w,h = box
            for item in items:
                if kind(item) == "scrollbar":
                    scroll = local_box(item["bounds"], snapshot)
                    if scroll and scroll[2] >= scroll[3] and overlap(box,scroll) > w*h*.5:
                        return
                    if scroll and scroll[2] < scroll[3] and overlap(box,scroll):
                        w = min(w, max(1,scroll[0]-x))
            box = (x,y,w,h)
        matches = [n for n in ocr if id(n) not in suppressed and
                   overlap(box, n.box) > min(n.box[2]*n.box[3], box[2]*box[3])*.35]
        exact = [n for n in matches if normalize(n.text) == normalize(text)]
        # Cropped captions can be joined to neighbours, truncated, or prefixed
        # with OCR from an icon. Retry only inside a verified semantic region.
        if (ocr_enabled and owner.get("framework_id") == "MSAA"
                and (kind(owner) == "tabitem" or not exact or any(n.box[3] > box[3]*.7 for n in exact))
                and kind(owner) in {"menuitem", "headeritem", "tabitem", "treeitem", "listitem"}):
            x,y,w,h = box
            if kind(owner) == "tabitem":
                pad = min(round(h*.55), w//5)
                x += pad
                w -= pad*2
                y += 4
                h -= 8
            nearby_icons = [b for b in icon_boxes if overlap(b,box)]
            if nearby_icons:
                left = max(b[0]+b[2]+2 for b in nearby_icons)
                if kind(owner) == "treeitem":
                    w = min(image.width-x, max(w, len(text)*h*.65+left-x))
                w -= left-x
                x = left
            if kind(owner) == "listitem":
                headers = [local_box(n["bounds"],snapshot) for n in items if kind(n) == "headeritem"]
                first = next((b for b in headers if b[0] <= x < b[0]+b[2]), None)
                if first:
                    w = min(w, first[0]+first[2]-x-2)
            if w > 0:
                for candidate in _ocr_ui(image.crop((x,y,x+w,y+h)),language,psm=7):
                    border_marks = " /;,()" if kind(owner) == "tabitem" else " /"
                    full_match = normalize(candidate.text).strip(border_marks) == normalize(text).strip(border_marks)
                    # Some suffix glyphs receive zero OCR confidence. A strong
                    # prefix plus additional ink in the same bounded cell lets
                    # the accessible name supply those missing characters.
                    ink = ink_box(image,(x,y,w,h)) if kind(owner) == "listitem" else None
                    prefix_match = (candidate.confidence >= .8 and len(candidate.text) >= 8
                                    and normalize(text).startswith(normalize(candidate.text)+" ")
                                    and ink and ink[2] > candidate.box[2]+20)
                    if full_match or prefix_match:
                        a,b,c,d = candidate.box
                        candidate.box = ink if prefix_match else (x+a,y+b,c,d)
                        exact = [candidate]
                        break
        if owner.get("framework_id") == "MSAA" or (owner.get("framework_id") == "Chrome" and kind(owner) in {"listitem", "tabitem"}):
            # Accessible names can be complete while the painted cell is clipped.
            # Never use a row-wide rectangle to erase the other table columns.
            from difflib import SequenceMatcher
            def matches_name(node):
                a, b = normalize(node.text), normalize(text)
                prefix = a.rstrip(". ")
                return a == b or (a.endswith("...") and len(prefix) >= 6 and b.startswith(prefix)) or (
                    len(a) >= 6 and SequenceMatcher(None, a, b).ratio() >= .92)
            candidates = exact or [n for n in ocr if id(n) not in suppressed and matches_name(n)]
            if candidates:
                chosen = min(candidates, key=lambda n: abs(n.box[1]-box[1])+abs(n.box[0]-box[0]))
                matches = exact = [chosen]
                if normalize(chosen.text).endswith("..."):
                    text = text[:len(chosen.text.rstrip(". "))].rstrip()+"…"
            elif kind(owner) in {"listitem", "menuitem", "headeritem", "treeitem"}:
                return  # Unverified geometry must not create captions over other ink.
        placement = exact
        ink = ink_box(image, box) if owner.get("framework_id") != "MSAA" and (kind(owner) == "text" or id(owner) in cells or kind(owner) == "treeitem") else None
        if not ink and placement:
            ink = max(placement, key=lambda n: overlap(n.box, box)).box
        if not ink:
            x, y, w, h = box
            height = min(14, max(7, h-4))
            width = min(max(1, w-8), max(8, round(len(text)*height*.52)))
            ink = (x+(w-width)//2 if centered else x, y+(h-height)//2, width, height)
        layout = {}
        # A TextBlock can expose its full name even when the visible label clips.
        # Use an ellipsis in the visual layer; the semantic tree keeps the full name.
        if (kind(owner) == "text" and owner.get("framework_id") == "XAML") or id(owner) in cells and len(text) > 20:
            font = _font("auto")
            if font:
                _, top, _, bottom = font.getbbox(text, anchor="ls")
                size = ink[3]*100/max(1, bottom-top)
                if font.getlength(text)*size/100 > ink[2]*1.2:
                    layout = {"font_size": size, "baseline": ink[1]-top*size/100}
                    while text and font.getlength(text+"…")*size/100 > ink[2]:
                        text = text[:-1]
                    text += "…"
        if ink[3] < 5:
            return
        text_boxes.append(ink)
        suppressed.update(id(n) for n in ocr if contains(n.box, ink) and
                          normalize(text) in normalize(n.text))
        signature = (text, ink)
        if signature in emitted:
            return
        for n in matches:
            suppressed.add(id(n))
        area = max(1, ink[2]*ink[3])
        for previous in text_nodes:
            if normalize(previous.text) != normalize(text):
                continue
            shared = overlap(previous.box, ink)
            if shared / max(1, min(area, previous.box[2]*previous.box[3])) >= .7:
                return
        emitted.add(signature)
        node = Node("text", ink, text=text, color="#171717",
                    vector_data={"source": "uia", "uia_id": owner.get("id", ""), "font_weight": 400, **layout})
        text_nodes.append(node)

    for item in items:
        box = local_box(item["bounds"], snapshot)
        role = kind(item)
        if item.get("password") and not item.get("debug_unredacted") or id(item) in chrome:
            continue
        if (role in {"button", "radiobutton", "checkbox"} and item.get("name")
                and 14 <= box[2] <= 64 and 14 <= box[3] <= 64
                and .75 <= box[2]/box[3] <= 1.4
                and not any(normalize(n.text) == normalize(item["name"]) for n in ocr if contains(box,n.box))
                and not any(len(n.text.strip()) > 3 and n.confidence >= .6
                            and overlap(n.box,box) > n.box[2]*n.box[3]*.35 for n in ocr)):
            artwork.append(trace_artwork(image, box))
            suppressed.update(id(n) for n in ocr if overlap(n.box,box) > n.box[2]*n.box[3]*.35)
            continue
        caption = str(item.get("label", item.get("name", ""))).casefold()
        titlebar_button = (role == "button" and box[1] <= 4 and box[3] <= 42
                           and box[0] >= image.width-120
                           and any(word in caption for word in
                                   ("close", "minimize", "maximize", "restore")))
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
        if (not titlebar_button and
                ((role in {"button", "splitbutton", "checkbox", "radiobutton"} and native)
                or role == "edit" and id(item) not in cells and item.get("framework_id") != "MSAA"
                or role in {"combobox", "tabitem"})):
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
                                 vector_data={"source": "uia", "uia_id": item.get("id", ""),
                                              **(tab_outline(image,visual_box) if role == "tabitem" else {})}))
        if id(item) in terminals:
            text_nodes.extend(terminal_text(image, item, snapshot))
            continue
        ranges = item.get("text_ranges", [])
        if ranges:
            for line in ranges:
                rects = line.get("rectangles", [])
                # Never duplicate a single string across multiple rectangles.
                # Native helper normally splits ranges by TextUnit_Line.
                if len(rects) == 1 and not exposed_by_descendant(item, line.get("text", "")):
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
            if (role in {"button", "radiobutton", "tabitem"}
                    and not (role == "tabitem" and item.get("framework_id") == "MSAA")
                    and (not native or box[2] < box[3]*1.8 and len(name) > 3)
                    and not any(normalize(n.text) == normalize(name) for n in ocr if contains(box, n.box))):
                continue
            emit(name, box, item, centered=role in {"button", "tabitem"})
        elif role == "splitbutton" and name:
            if not any(kind(n) == "text" for n in walk(item) if n is not item) and any(normalize(n.text) == normalize(name) for n in ocr if contains(box, n.box)):
                emit(name, box, item)
        elif role == "edit" and item.get("framework_id") == "MSAA" and name and not item.get("states", {}).get("value"):
            emit(name, box, item)
        elif role == "edit" and item.get("states", {}).get("value"):
            x, y, w, h = box
            # A multiline edit should expose TextPattern; avoid squeezing its
            # entire (possibly scrolled) value into one line.
            value = str(item["states"]["value"])
            if "\n" not in value and "\r" not in value:
                emit(value, (x+4, y+2, max(1, w-8), max(1, h-4)), item)
        elif role == "edit" and (not item.get("password") or item.get("debug_unredacted")):
            # UIA may expose the edit control but omit its ValuePattern. If
            # full-window OCR saw ink in the field, retry just that visible
            # region so an adjacent label cannot consume the whole OCR line.
            x, y, w, h = box
            source_nodes = [n for n in ocr if overlap(n.box, box) > n.box[2]*n.box[3]*.12]
            left, top, right, bottom = x+3, y+1, x+w-4, y+h-1
            if ocr_enabled and source_nodes and right > left and bottom > top:
                candidates = [n for n in _ocr_ui(image.crop((left, top, right, bottom)),
                                                    language, psm=7)
                              if n.confidence >= .65 and any(ch.isalnum() for ch in n.text)]
                if candidates:
                    value = max(candidates, key=lambda n: n.confidence)
                    vx, vy, vw, vh = value.box
                    value_box = (left+vx, top+vy, vw, vh)
                    signature = (value.text, value_box)
                    if signature not in emitted:
                        emitted.add(signature)
                        text_nodes.append(Node("text", value_box, text=value.text,
                            color=source_nodes[0].color or "#171717", confidence=value.confidence,
                            vector_data={"source": "ocr", "role": "field-value"}))
                    suppressed.update(id(n) for n in source_nodes
                                      if overlap(n.box, box) > n.box[2]*n.box[3]*.30)

    # Field-specific OCR is created after the initial OCR list. It needs the
    # same duplicate check, including captions on nested breadcrumb controls.
    semantic_text = [t for t in text_nodes if t.vector_data.get("source") == "uia"]
    def duplicates_semantics(n):
        represented = [t for t in semantic_text if overlap(n.box,t.box) and normalize(t.text) in normalize(n.text)]
        return represented and sum(len(t.text) for t in represented) > len(n.text)*.5
    text_nodes = [n for n in text_nodes if n.vector_data.get("source") != "ocr" or not duplicates_semantics(n)]
    for n in ocr:
        represented = [t for t in text_nodes if overlap(n.box,t.box) and normalize(t.text) in normalize(n.text)]
        if len(represented) >= 2 and sum(len(t.text) for t in represented) > len(n.text)*.5:
            suppressed.add(id(n))
    suppressed.update(id(n) for n in ocr if any(
        kind(item) == "scrollbar" and overlap(local_box(item["bounds"], snapshot), n.box) > n.box[2]*n.box[3]*.35
        for item in items))
    suppressed.update(id(n) for n in ocr if any(overlap(n.box, a.box) > n.box[2]*n.box[3]*.35 for a in artwork))
    for item in items:
        if kind(item) == "headeritem":
            box = local_box(item["bounds"],snapshot)
            for n in ocr:
                if (len(n.text.strip()) <= 1 and contains(box,n.box)
                        and n.box[3] <= box[3]*.3):
                    artwork.append(trace_artwork(image,n.box))
                    suppressed.add(id(n))
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
    if not item.get("password") or item.get("debug_unredacted"):
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
    snapshot = render_view(snapshot)
    visual = to_svg(scene, font_family)
    start, body = visual.split(">", 1)
    title = snapshot["root"].get("name") or "Window capture"
    # An atomic role=img would hide the navigable descendants.
    start = start.replace('role="img"', 'role="graphics-document group" aria-labelledby="capture-title" aria-describedby="capture-description"')
    parts = [start+">", f'<title id="capture-title">{escape(title)}</title>',
             '<desc id="capture-description">Static window capture. Represented controls are informational and cannot be operated.</desc>',
             '<g aria-hidden="true" data-kind="visual-reconstruction">', body.rsplit("</svg>", 1)[0]]
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
            # Preserve role and identity alongside the descriptive label.
            parts.append(f'<g id="capture-node-{serial}" role="group" aria-label="{escape(label, quote=True)}" '
                         f'data-uia-id="{escape(item.get("id", ""), quote=True)}" data-uia-role="{kind(item)}">')
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#000000" fill-opacity="0" aria-hidden="true"/>')
            # UIA text ranges may carry document content absent from Name/Value.
            if not item.get("password") or item.get("debug_unredacted"):
                for line in item.get("text_ranges", []):
                    text = line.get("text", "")
                    if (text.strip() and normalize(text) != normalize(item.get("name", ""))
                            and not exposed_by_descendant(item, text)):
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
        if not item.get("password") or item.get("debug_unredacted"):
            lines = "".join('<li>'+escape(line.get("text", ""))+'</li>'
                             for line in item.get("text_ranges", [])
                             if line.get("text", "").strip()
                             and not exposed_by_descendant(item, line.get("text", "")))
        nested = '<ul>'+lines+children+'</ul>' if children or lines else ""
        return '<li>'+label+nested+'</li>' if local_box(item.get("bounds", []), snapshot) else children
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Window capture</title>'
            '<style>body{font:1rem system-ui;max-width:80rem;margin:2rem auto;padding:1rem}svg{max-width:100%;height:auto}</style>'
            '<h1>Static window capture</h1>'+svg+'<h2>Captured interface information</h2>'
            '<p>This textual outline also works in readers that flatten SVG accessibility.</p><ul>'
            +outline(snapshot["root"])+"</ul></html>\n")


def main(argv=None):
    """Compatibility entry point; all conversion and capture now use the main CLI."""
    from .cli import main as convert
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--image")
    old, remaining = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    return convert(([old.image] if old.image else ["--capture"])+remaining)


if __name__ == "__main__":
    raise SystemExit(main())
