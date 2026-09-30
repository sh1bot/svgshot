"""Separate UIA-assisted capture/replay CLI; imports the existing vectorizer.

The Windows SDK helper supplies physical screen coordinates and a WGC PNG.
Replay and SVG generation work on any platform without Windows dependencies.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from html import escape
from pathlib import Path

from PIL import Image

from .model import Node, flatten
from .recognize import Options, reconstruct
from .svg import to_svg

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
    return TYPES.get(item.get("control_type"), "custom")


def validate_snapshot(snapshot, image):
    if snapshot.get("version") != 1 or not isinstance(snapshot.get("root"), dict):
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


def merge_uia(scene, image, snapshot):
    """UIA supplies exact strings/states. OCR supplies ink placement and gaps.

    Keep semantic names separate from visible captions: image and container
    names are not painted as text. The original UIA tree is retained unchanged.
    """
    items = list(visible_items(snapshot["root"], snapshot))
    ocr = [n for n in flatten(scene) if n.kind == "text"]
    suppressed = set()
    controls, text_nodes = [], []
    emitted = set()
    passwords = [local_box(n["bounds"], snapshot) for n in items if n.get("password")]

    def emit(text, box, owner, centered=False):
        text = text.strip("\r\n")
        if not text or not box:
            return
        matches = [n for n in ocr if id(n) not in suppressed and contains(box, n.box)]
        exact = [n for n in matches if normalize(n.text) == normalize(text)]
        placement = (exact or matches) if kind(owner) == "text" or centered else exact
        if placement:
            # UIA controls have padded bounds; OCR gives tighter typographic bounds.
            chosen = max(placement, key=lambda n: overlap(n.box, box))
            ink = chosen.box
        else:
            x, y, w, h = box
            height = min(14, max(7, h-4))
            if centered:
                width = min(max(1, w-8), max(8, round(len(text)*height*.52)))
                ink = (x+(w-width)//2, y+(h-height)//2, width, height)
            else:
                ink = (x, y+(h-height)//2, max(1, w), height)
        signature = (text, ink)
        if signature in emitted:
            return
        emitted.add(signature)
        for n in matches:
            suppressed.add(id(n))
        node = Node("text", ink, text=text, color="#171717",
                    vector_data={"source": "uia", "uia_id": owner.get("id", "")})
        text_nodes.append(node)

    for item in items:
        box = local_box(item["bounds"], snapshot)
        role = kind(item)
        if item.get("password"):
            continue
        if role in {"button", "splitbutton", "edit", "combobox", "list", "tree", "datagrid", "table", "checkbox", "radiobutton", "tabitem"}:
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
                emit(snapshot["root"]["name"], (x+28, y+3, max(1, w-140), max(1, h-6)), item)
        if role in CAPTIONS and name:
            children = [n for n in walk(item) if n is not item and kind(n) == "text" and not n.get("offscreen")]
            if any(normalize(n.get("name", "")) == normalize(name) for n in children):
                continue
            if role == "button" and any(kind(n) == "image" for n in walk(item)):
                continue  # The accessible name of an icon button is not a caption.
            if role in {"checkbox", "radiobutton"}:
                x, y, w, h = box
                pad = min(16, h, w)+5
                if w <= pad:
                    continue
                box = (x+pad, y, w-pad, h)
            emit(name, box, item, centered=role in {"button", "tabitem"})
        elif role == "edit" and item.get("states", {}).get("value"):
            x, y, w, h = box
            # A multiline edit should expose TextPattern; avoid squeezing its
            # entire (possibly scrolled) value into one line.
            value = str(item["states"]["value"])
            if "\n" not in value and "\r" not in value:
                emit(value, (x+4, y+2, max(1, w-8), max(1, h-4)), item)

    def prune(node):
        retained = []
        for child in node.children:
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
    surfaces = {"content-panel", "dialog-panel", "window-header", "footer-panel", "rect", "decorative-background", "selected-row", "text-selection"}
    controls.sort(key=lambda n: n.box[2]*n.box[3], reverse=True)
    scene.children = ([n for n in scene.children if n.kind in surfaces] + controls
                      + [n for n in scene.children if n.kind not in surfaces] + text_nodes)
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
    for key in ("help_text", "access_key", "accelerator_key"):
        if item.get(key):
            pieces.append(item[key])
    return ", ".join(str(s) for s in pieces if s)


def semantic_svg(scene, snapshot, font_family="auto"):
    visual = to_svg(scene, font_family)
    start, body = visual.split(">", 1)
    title = snapshot["root"].get("name") or "Window capture"
    # An atomic role=img would hide the navigable descendants.
    start = start.replace('role="img"', 'role="graphics-document group" aria-labelledby="capture-title" aria-describedby="capture-description"')
    parts = [start+">", f'<title id="capture-title">{escape(title)}</title>',
             '<desc id="capture-description">Static window capture. Represented controls are informational and cannot be operated.</desc>',
             '<metadata id="uia-snapshot">'+escape(json.dumps(snapshot, ensure_ascii=False))+'</metadata>',
             '<g aria-hidden="true" data-kind="visual-reconstruction">', body.rsplit("</svg>", 1)[0], '</g>']
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
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="transparent" aria-hidden="true"/>')
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
    parser = argparse.ArgumentParser(description="Capture a Windows window as semantic SVG, or replay a saved PNG/UIA pair")
    parser.add_argument("output", type=Path)
    parser.add_argument("--image", type=Path, help="Replay an existing captured PNG")
    parser.add_argument("--uia", type=Path, help="UIA snapshot paired with --image")
    parser.add_argument("--helper", type=Path, help="Native Windows helper executable")
    parser.add_argument("--hwnd", help="Capture a specific window handle (decimal or 0x hexadecimal)")
    parser.add_argument("--foreground", action="store_true", help="Capture the foreground window after --delay")
    parser.add_argument("--delay", type=int, default=0)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--no-ocr", action="store_true")
    parser.add_argument("--allow-raster", action="store_true", help="Opt in to the existing small-raster fallback")
    parser.add_argument("--scene", type=Path)
    parser.add_argument("--html", type=Path, help="Accessible HTML preview with inline SVG and a text outline")
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != ".svg":
            raise ValueError("Output must have an .svg extension")
        if bool(args.image) != bool(args.uia):
            raise ValueError("Replay requires both --image and --uia")
        if not 0 <= args.delay <= 60 or (args.hwnd and args.foreground):
            raise ValueError("Delay must be 0–60; choose --hwnd or --foreground")
        if args.image and (args.hwnd or args.foreground or args.delay or args.helper):
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
        if any(p and p.resolve() in {image_path.resolve(), uia_path.resolve()} for p in outputs):
            raise ValueError("Output paths must not overwrite capture inputs")
        if len({p.resolve() for p in outputs if p}) != sum(p is not None for p in outputs):
            raise ValueError("Output, scene, and HTML paths must differ")
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
