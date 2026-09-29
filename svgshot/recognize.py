"""Conservative GUI geometry and text recognition from a PNG."""
from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .model import Node


@dataclass
class Options:
    colors: int = 10
    min_area: int = 18
    max_raster: int = 128
    raster_fallback: bool = True
    ocr: bool = True
    language: str = "eng"
    font_family: str = "auto"

    @classmethod
    def from_file(cls, path: str | None) -> "Options":
        if not path:
            return cls()
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
        result = cls(**data)
        if not 2 <= result.colors <= 32 or result.min_area < 1 or result.max_raster < 0:
            raise ValueError("colors must be 2–32, min_area positive, max_raster nonnegative")
        if not result.font_family or len(result.font_family) > 100:
            raise ValueError("font_family must be a nonempty font name")
        return result


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(x) for x in rgb[:3])


def _luma(rgb) -> float:
    return float(np.dot(rgb[:3], [0.2126, 0.7152, 0.0722]))


def _ocr(image: Image.Image, language: str, psm: int = 11) -> list[Node]:
    if not shutil.which("tesseract"):
        raise RuntimeError("Tesseract OCR is required (install it or pass --no-ocr)")
    data = io.BytesIO()
    # Small Windows UI fonts are commonly 9–12 pixels high. Doubling before
    # OCR recovers words that Tesseract otherwise splits or drops entirely.
    scale = 2 if image.width <= 1200 and image.height <= 1000 else 1
    sample = image.convert("RGB")
    if scale == 2:
        sample = sample.resize((sample.width*2, sample.height*2), Image.Resampling.BICUBIC)
    sample.save(data, format="PNG")
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", language, "--psm", str(psm), "tsv"],
        input=data.getvalue(), capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError("Tesseract failed: " + result.stderr.decode(errors="replace").strip())
    import csv
    lines = defaultdict(list)
    reader = csv.DictReader(io.StringIO(result.stdout.decode("utf-8-sig")), delimiter="\t")
    for row in reader:
        word = row["text"].strip()
        try:
            confidence = float(row["conf"])
            x, y, w, h = (round(int(row[k])/scale) for k in ("left", "top", "width", "height"))
        except (ValueError, KeyError):
            continue
        if not word or confidence < 35 or w <= 0 or h <= 0:
            continue
        key = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
        lines[key].append((x, y, w, h, word, confidence))
    nodes = []
    for words in lines.values():
        words.sort(key=lambda w: w[0])
        # OCR frequently mistakes a radio ring beside a label for © or ®.
        # Let the geometry stage see the original pixels in that box.
        symbol = words[0][4]
        looks_like_control = (symbol in ("©", "®", "○", "◉", "O", "0") or
                              (len(symbol) <= 3 and (symbol.startswith("@") or
                                                       not any(ch.isalnum() for ch in symbol))))
        if len(words) > 1 and looks_like_control:
            first, second = words[:2]
            if 7 <= first[2] <= 28 and 0 <= second[0]-(first[0]+first[2]) <= 28:
                words = words[1:]
        x = min(w[0] for w in words)
        y = min(w[1] for w in words)
        right = max(w[0] + w[2] for w in words)
        bottom = max(w[1] + w[3] for w in words)
        nodes.append(Node("text", (x, y, right-x, bottom-y),
                          text=" ".join(w[4] for w in words),
                          confidence=sum(w[5] for w in words) / (100 * len(words))))
    return sorted(nodes, key=lambda n: (n.box[1], n.box[0]))


def _pixel_color(image: np.ndarray, box, pad=2):
    x, y, w, h = box
    height, width = image.shape[:2]
    left, top = max(0, x-pad), max(0, y-pad)
    right, bottom = min(width, x+w+pad), min(height, y+h+pad)
    crop = image[top:bottom, left:right].reshape(-1, 3)
    if len(crop) == 0:
        return np.array([0, 0, 0])
    # Prefer high-contrast pixels relative to the perimeter, for both themes.
    lum = crop @ np.array([.2126, .7152, .0722])
    perimeter = np.concatenate((image[top, left:right], image[bottom-1, left:right],
                                image[top:bottom, left], image[top:bottom, right-1]))
    local = _luma(np.median(perimeter, axis=0))
    return crop[int(np.argmax(np.abs(lum-local)))]


def _background(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    # The outer pixel is often a window frame, not a background. Use the
    # dominant color over the interior instead of a border sample.
    interior = image[2:max(3,h-2):3, 2:max(3,w-2):3].reshape(-1, 3)
    quant = ((interior.astype(np.uint16) + 8) // 16).clip(0, 15)
    key = quant[:, 0] * 256 + quant[:, 1] * 16 + quant[:, 2]
    common = Counter(key.tolist()).most_common(1)[0][0]
    return np.median(interior[key == common], axis=0).astype(np.uint8)


def _classify(component: np.ndarray, width: int, height: int) -> tuple[str, float]:
    area = int(component.sum())
    ratio = area / (width * height)
    if min(width, height) <= 2:
        return "line", .85
    if .78 <= width / height <= 1.28 and 7 <= width <= 60:
        yy, xx = np.nonzero(component)
        radius = np.hypot((xx - (width-1)/2) / (width/2),
                          (yy - (height-1)/2) / (height/2))
        # A ring or disk must reach all four sides, with few occupied corners.
        corners = np.r_[component[:2, :2].ravel(), component[:2, -2:].ravel(),
                        component[-2:, :2].ravel(), component[-2:, -2:].ravel()]
        if float(corners.mean()) < .25 and (ratio > .65 or
                (ratio > .12 and np.mean(np.abs(radius-1) < .23) > .72)):
            return ("circle" if ratio > .65 else "ring"), .87
    if ratio >= .78:
        return "rect", min(.98, ratio)
    border = np.r_[component[:2].ravel(), component[-2:].ravel(),
                   component[:, :2].ravel(), component[:, -2:].ravel()]
    if width >= 8 and height >= 6 and border.mean() >= .68 and ratio < .65:
        return "outline", .72
    return "unknown", .25


def _raster(image: Image.Image, box) -> str:
    x, y, w, h = box
    buffer = io.BytesIO()
    image.crop((x, y, x+w, y+h)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _contains(parent: Node, child: Node, tolerance=3) -> bool:
    x, y, w, h = parent.box
    a, b, c, d = child.box
    return (x-tolerance <= a and y-tolerance <= b and
            a+c <= x+w+tolerance and b+d <= y+h+tolerance)


def _radio_from_pixels(image: np.ndarray, box) -> tuple[tuple[int,int,int,int], bool, str] | None:
    """Fit the outer ring and read selection from its center, not its fill."""
    x, y, w, h = box
    height, width = image.shape[:2]
    left, top = max(0,x-2), max(0,y-2)
    right, bottom = min(width,x+w+2), min(height,y+h+2)
    patch = image[top:bottom, left:right].astype(np.int16)
    if min(patch.shape[:2]) < 7:
        return None
    corners = np.array([patch[0,0],patch[0,-1],patch[-1,0],patch[-1,-1]])
    background = np.median(corners, axis=0)
    difference = np.max(np.abs(patch-background), axis=2)
    yy, xx = np.nonzero(difference > 55)
    if len(xx) < 12:
        return None
    x0, x1, y0, y1 = xx.min(), xx.max()+1, yy.min(), yy.max()+1
    diameter_x, diameter_y = x1-x0, y1-y0
    if not (9 <= diameter_x <= 24 and 9 <= diameter_y <= 24 and
            .75 <= diameter_x/diameter_y <= 1.33):
        return None
    shape = difference[y0:y1, x0:x1] > 55
    corners = np.r_[shape[:2,:2].ravel(), shape[:2,-2:].ravel(),
                    shape[-2:,:2].ravel(), shape[-2:,-2:].ravel()]
    if corners.mean() >= .45:  # Square checkbox border, not a circular ring.
        return None
    cx, cy = (x0+x1-1)/2, (y0+y1-1)/2
    center = difference[max(0,round(cy)-1):round(cy)+2,
                        max(0,round(cx)-1):round(cx)+2]
    selected = float(np.mean(center > 55)) > .55
    radii = np.hypot((xx-cx)/(diameter_x/2), (yy-cy)/(diameter_y/2))
    ring_pixels = patch[yy[radii > .65], xx[radii > .65]]
    color = _hex(np.median(ring_pixels, axis=0)) if len(ring_pixels) else "#555555"
    return tuple(map(int, (left+x0, top+y0, diameter_x, diameter_y))), selected, color


def _close_icon(image: np.ndarray) -> Node | None:
    """Find a small diagonal X in the upper-right title-bar corner."""
    height, width = image.shape[:2]
    if width < 100 or height < 60:
        return None
    left, top = width-50, 4
    patch = image[top:min(height//4, 36), left:width-4]
    # Keep this restricted to dark ink on a light title bar, away from the
    # window frame; other small header glyphs should remain untouched.
    if patch.size == 0 or np.median(patch.reshape(-1, 3)) < 225:
        return None
    ink = np.max(patch, axis=2) < 160
    labels, count = ndimage.label(ink, structure=np.ones((3, 3), dtype=int))
    for label, region in enumerate(ndimage.find_objects(labels), start=1):
        if region is None:
            continue
        ys, xs = region
        w, h = xs.stop-xs.start, ys.stop-ys.start
        if not (7 <= w <= 18 and 7 <= h <= 18 and .8 <= w/h <= 1.25):
            continue
        mask = labels[region] == label
        yy, xx = np.nonzero(mask)
        forward = np.abs(xx*(h-1) - yy*(w-1)) <= max(w,h)
        backward = np.abs((w-1-xx)*(h-1) - yy*(w-1)) <= max(w,h)
        if (forward.mean() < .38 or backward.mean() < .38 or
                (forward | backward).mean() < .9 or
                not all(mask[y, x] for y, x in ((0,0),(0,w-1),(h-1,0),(h-1,w-1)))):
            continue
        color = _hex(np.median(patch[region][mask], axis=0))
        return Node("close-icon", (left+xs.start, top+ys.start, w, h), color)
    return None


def _group_frames(image: np.ndarray) -> list[Node]:
    """Recover thin, low-contrast group boxes from their paired side rails."""
    height, width = image.shape[:2]
    frames = []
    for x in range(12, width-110):
        column = image[:, x]
        changes = np.r_[0, np.flatnonzero(np.any(column[1:] != column[:-1], axis=1))+1, height]
        for top, bottom in zip(changes[:-1], changes[1:]):
            if not (35 <= bottom-top <= 130 and 50 < top < height-25):
                continue
            color = column[top]
            outside = image[top:min(bottom,height), x-2]
            if np.median(np.max(np.abs(outside.astype(int)-color.astype(int)), axis=1)) < 10:
                continue
            row = image[bottom-1, x:width-8]
            same = np.all(row == color, axis=1)
            if len(same) < 100 or not same[0]:
                continue
            end = int(np.flatnonzero(~same)[0]) if (~same).any() else len(same)
            if end < 100:
                continue
            right = x+end-1
            if np.mean(np.all(image[top:bottom, right] == color, axis=1)) < .95:
                continue
            box = (x, int(top), end, int(bottom-top))
            if any(abs(old.box[0]-x) <= 3 and abs(old.box[1]-top) <= 3 for old in frames):
                continue
            frames.append(Node("outline", box, _hex(color)))
    return frames


def _checkboxes(image: np.ndarray, texts: list[Node]) -> list[Node]:
    """Read square controls beside labels directly, including their checkmarks."""
    height, width = image.shape[:2]
    controls = []
    for text in texts:
        if not (7 <= text.box[3] <= 18 and text.box[0] >= 35 and
                text.box[1] < height-45):
            continue
        tx, ty, _, th = text.box
        for x in range(tx-21, tx-13):
            for y in range(ty-4, ty+1):
                if x < 1 or y < 1 or x+13 >= width or y+13 >= height:
                    continue
                patch = image[y:y+13, x:x+13]
                border = np.r_[patch[0], patch[-1], patch[:,0], patch[:,-1]]
                if (np.max(np.std(border.astype(float), axis=0)) > 25 or
                        np.mean(np.max(np.abs(border.astype(int)-image[y,x-1].astype(int)), axis=1)) < 30 or
                        np.max(np.abs(np.median(border,axis=0)-patch[1,1])) < 30):
                    continue
                if not np.all(np.max(np.abs(patch[1:-1,1:-1].astype(int)-patch[1,1].astype(int)),axis=2) < 220):
                    continue
                selected = np.mean(np.min(patch[3:-3,3:-3],axis=2) < 125) > .07
                controls.append(Node("checkbox-selected" if selected else "checkbox",
                                     (x,y,13,13), _hex(np.median(border,axis=0)),
                                     background=_hex(patch[1,1])))
                break
            else:
                continue
            break
    return controls


def _flat_runs(row: np.ndarray, minimum: int, maximum: int):
    ends = np.r_[0, np.flatnonzero(np.any(row[1:] != row[:-1], axis=1))+1, len(row)]
    for left, right in zip(ends[:-1], ends[1:]):
        if minimum <= right-left <= maximum:
            yield int(left), int(right), row[left]


def _dropdowns(image: np.ndarray, texts: list[Node]) -> list[Node]:
    height, width = image.shape[:2]
    found = []
    for label in texts:
        if not (7 <= label.box[3] <= 20 and 20 <= label.box[0] < width-100):
            continue
        tx, ty, _, _ = label.box
        for y in range(max(1,ty-8),ty+1):
            for x, right, color in _flat_runs(image[y], 100, width-10):
                if not (tx-8 <= x <= tx and right > tx+80 and right < width-8):
                    continue
                if np.max(np.abs(color.astype(int)-image[y,x-2].astype(int))) < 15:
                    continue
                if np.max(np.abs(color.astype(int)-image[y+4,x+12].astype(int))) < 10:
                    continue
                for bottom in range(y+16,min(height,y+31)):
                    if np.mean(np.all(image[bottom,x:right] == color,axis=1)) < .98:
                        continue
                    found.append(Node("dropdown", (x,y,right-x,bottom-y+1),
                                      _hex(color), background=_hex(image[y+4,x+12])))
                    break
            if found and found[-1].box[1] == y:
                break
    return found


def _footer_buttons(image: np.ndarray, source: Image.Image, language: str) -> tuple[list[Node], list[Node]]:
    height, width = image.shape[:2]
    controls, labels = [], []
    for y in range(max(1,height-65),height-17):
        for x, right, color in _flat_runs(image[y], 50, min(150,width//2)):
            if len(controls) >= 12:
                return controls, labels
            if x < 2 or right >= width-2 or np.max(np.abs(color.astype(int)-image[y,x-2].astype(int))) < 20:
                continue
            if np.max(np.abs(color.astype(int)-image[y+4,x+5].astype(int))) < 10:
                continue
            if any(abs(x-control.box[0]) < 3 and abs(y-control.box[1]) < 3 for control in controls):
                continue
            bottoms = [bottom for bottom in range(y+17,min(height,y+34))
                       if np.mean(np.all(image[bottom,x:right] == color,axis=1)) >= .98]
            for bottom in reversed(bottoms):
                crop = source.crop((x,y,right,bottom+1))
                recognized = [t for t in _ocr(crop,language,psm=7)
                              if t.confidence >= .7 and any(c.isalpha() for c in t.text)]
                if len(recognized) != 1:
                    break
                label = recognized[0]
                lx,ly,lw,lh = label.box
                label.box = (x+lx,y+ly,lw,lh)
                controls.append(Node("outlined-button", (x,y,right-x,bottom-y+1),
                                     _hex(color), background=_hex(image[y+4,x+5])))
                labels.append(label)
                break
    return controls, labels


def _tabs(image: np.ndarray, source: Image.Image, language: str) -> tuple[list[Node], list[Node]]:
    """Find the active white tab and its neighboring inactive tab."""
    height, width = image.shape[:2]
    if width < 160 or height < 110:
        return [], []
    for y in range(32, min(65,height-24)):
        for x, right, border in _flat_runs(image[y], 45, 150):
            if not (5 <= x <= 20 and 195 <= np.min(border) <= 235):
                continue
            if not np.all(np.min(image[y+1,x+2:right-2],axis=1) >= 250):
                continue
            # The selected tab's right border continues down to the panel.
            bottom = y+22
            if bottom >= height or np.mean(np.all(image[y+1:bottom-1,right-1] == border,axis=1)) < .8:
                continue
            neighbors = [(a,b,c) for a,b,c in _flat_runs(image[y+2], 40, 125)
                         if abs(a-(right-1)) <= 2 and np.max(np.abs(c.astype(int)-border.astype(int))) <= 3]
            if not neighbors:
                continue
            _, next_right, _ = neighbors[0]
            active_box = (x,y,right-x,bottom-y)
            inactive_box = (right-1,y+2,next_right-right+2,bottom-y-2)
            labels = []
            for bx,by,bw,bh in (active_box,inactive_box):
                local = [t for t in _ocr(source.crop((bx,by,bx+bw,by+bh)),language,psm=7)
                         if t.confidence > .7 and any(c.isalpha() for c in t.text)]
                if len(local) != 1:
                    break
                t = local[0]
                tx,ty,tw,th = t.box
                t.box = (bx+tx,by+ty,tw,th)
                labels.append(t)
            if len(labels) != 2:
                continue
            return ([Node("tab-active",active_box,_hex(border),background="#ffffff"),
                     Node("tab",inactive_box,_hex(border),background="#f0f0f0"),
                     Node("line",(next_right,bottom-2,width-next_right-9,1),_hex(border)),
                     Node("line",(x+1,bottom-1,width-x-11,1),"#ffffff")], labels)
    return [], []


def _structure(image: np.ndarray) -> list[Node]:
    """Recover broad white header surfaces and long pale panel dividers."""
    height, width = image.shape[:2]
    if height < 60 or width < 100:
        return []
    white = np.min(image, axis=2) >= 250
    nodes = []
    interior = np.median(image[2:height-2:5,2:width-2:5].reshape(-1,3),axis=0)
    # Screen captures often include a one-pixel window frame with a color
    # unrelated to the dominant interior surface.
    for box, sample in (((0,0,1,height),image[:,0]),
                        ((width-1,0,1,height),image[:,width-1]),
                        ((0,0,width,1),image[0]),
                        ((0,height-1,width,1),image[height-1])):
        color = np.median(sample.reshape(-1,3),axis=0)
        if np.max(np.abs(color-interior)) > 40:
            nodes.append(Node("line",box,_hex(color)))
    # A wide run of white rows at the top is a distinct surface, even when
    # palette quantization merges it with the surrounding light grey.
    row_fraction = white[:, 2:width-2].mean(axis=1)
    header_rows = np.flatnonzero(row_fraction[:height//2] > .8)
    start = 0
    end = 0
    if len(header_rows):
        runs = np.split(header_rows, np.where(np.diff(header_rows) > 1)[0]+1)
        top_run = max(runs, key=len)
        if len(top_run) >= 12 and top_run[0] <= 4:
            start, end = int(top_run[0]), int(top_run[-1])+1
            color = _hex(np.median(image[start:end, 2:width-2].reshape(-1,3), axis=0))
            nodes.append(Node("rect", (1,start,width-2,end-start), color))
    close = _close_icon(image)
    if close:
        nodes.append(close)
    nodes.extend(_group_frames(image))
    from_y = max(end, 2)
    if height-from_y < 40:
        return nodes
    # Adjacent one-pixel white columns form a single divider. Check the
    # neighboring columns so a broad white panel is not misread as a line.
    column_fraction = white[from_y:height-2].mean(axis=0)
    columns = np.flatnonzero(column_fraction > .85)
    for group in np.split(columns, np.where(np.diff(columns) > 1)[0]+1) if len(columns) else []:
        left, right = int(group[0]), int(group[-1])+1
        if not (1 <= right-left <= 3 and 2 <= left and right < width-2):
            continue
        if column_fraction[left-1] < .25 and column_fraction[right] < .25:
            nodes.append(Node("line", (left,from_y,right-left,height-from_y-2), "#ffffff"))
    # Isolated long white rows delimit footer/control regions.
    for y in range(from_y+1,height-2):
        row = white[y]
        labels, count = ndimage.label(row)
        if not count:
            continue
        slices = ndimage.find_objects(labels)
        run = max((s[0] for s in slices if s), key=lambda s:s.stop-s.start)
        left, right = run.start, run.stop
        if right-left < width*.35:
            continue
        if white[y-1,left:right].mean() < .15 and white[y+1,left:right].mean() < .15:
            nodes.append(Node("line", (left,y,right-left,1), "#ffffff"))
    return nodes


def reconstruct(image: Image.Image, options: Options) -> Node:
    if image.width * image.height > 25_000_000:
        raise ValueError("PNG exceeds the 25-megapixel limit")
    rgb_image = image.convert("RGB")
    pixels = np.asarray(rgb_image)
    h, w = pixels.shape[:2]
    background = _background(pixels)
    root = Node("window", (0, 0, w, h), _hex(background))
    structural = _structure(pixels)
    texts = _ocr(rgb_image, options.language) if options.ocr else []
    if options.ocr:
        # Page segmentation modes fail differently on compact dialogs. Prefer
        # the higher-confidence reading of a shared line; keep isolated lines.
        for alternate in _ocr(rgb_image, options.language, psm=6):
            overlaps = [t for t in texts if
                        max(t.box[1], alternate.box[1]) < min(t.box[1]+t.box[3], alternate.box[1]+alternate.box[3]) and
                        max(t.box[0], alternate.box[0]) < min(t.box[0]+t.box[2], alternate.box[0]+alternate.box[2])]
            if not overlaps:
                texts.append(alternate)
            elif len(overlaps) == 1 and alternate.confidence > overlaps[0].confidence+.08 and (
                    alternate.box[2] < overlaps[0].box[2]*1.6):
                texts.remove(overlaps[0])
                texts.append(alternate)
        footer, footer_labels = _footer_buttons(pixels, rgb_image, options.language)
        for label in footer_labels:
            texts = [t for t in texts if not (
                max(t.box[0],label.box[0]) < min(t.box[0]+t.box[2],label.box[0]+label.box[2]) and
                max(t.box[1],label.box[1]) < min(t.box[1]+t.box[3],label.box[1]+label.box[3]))]
            texts.append(label)
        tabs, tab_labels = _tabs(pixels, rgb_image, options.language)
        if tab_labels:
            tab_top = min(t.box[1] for t in tab_labels)-4
            tab_bottom = max(t.box[1]+t.box[3] for t in tab_labels)+4
            texts = [t for t in texts if not (tab_top <= t.box[1] < tab_bottom and
                                               t.box[0] < tab_labels[-1].box[0]+tab_labels[-1].box[2])]
            texts.extend(tab_labels)
    else:
        footer, tabs = [], []
    checkboxes = _checkboxes(pixels, texts)
    dropdowns = _dropdowns(pixels, texts)
    controls = checkboxes + dropdowns + footer + tabs
    reserved = np.zeros((h, w), dtype=bool)
    for node in texts:
        x, y, tw, th = node.box
        reserved[max(0,y-2):min(h,y+th+3), max(0,x-2):min(w,x+tw+3)] = True
        node.color = _hex(_pixel_color(pixels, node.box))

    # Quantization removes antialiasing variants and decorative micro-shades.
    small = rgb_image.copy()
    small.thumbnail((1200, 1200))
    palette_image = small.quantize(colors=options.colors, method=Image.Quantize.MEDIANCUT)
    palette = np.array(palette_image.getpalette()[:options.colors*3], dtype=np.uint8).reshape(-1, 3)
    if len(palette) == 0:
        palette = background[None, :]
    # Pixel assignment in stripes bounds memory on large screenshots.
    indices = np.empty((h, w), dtype=np.uint8)
    for y in range(0, h, 128):
        stripe = pixels[y:y+128].astype(np.int32)
        distances = ((stripe[:, :, None, :] - palette[None, None, :, :].astype(np.int32)) ** 2).sum(axis=3)
        indices[y:y+128] = np.argmin(distances, axis=2)
    bg_index = int(np.argmin(((palette.astype(int)-background.astype(int))**2).sum(axis=1)))
    candidates = []
    for index, color in enumerate(palette):
        if index == bg_index:
            continue
        mask = (indices == index) & ~reserved
        labels, count = ndimage.label(mask)
        slices = ndimage.find_objects(labels)
        for label, region in enumerate(slices, start=1):
            if region is None:
                continue
            ys, xs = region
            bw, bh = xs.stop-xs.start, ys.stop-ys.start
            area = int(np.count_nonzero(labels[region] == label))
            if area < options.min_area or bw < 2 or bh < 2 or bw*bh > w*h*.85:
                continue
            box = (xs.start, ys.start, bw, bh)
            component = labels[region] == label
            kind, confidence = _classify(component, bw, bh)
            if kind == "unknown":
                if not options.raster_fallback or max(bw, bh) > options.max_raster or area < 35:
                    continue
                node = Node("raster", box, confidence=confidence, image_data=_raster(rgb_image, box))
            else:
                # Quantization is only for segmentation: using the palette's
                # averaged color can wash out a selected blue row to pale cyan.
                original_color = np.median(pixels[region][component], axis=0)
                node = Node(kind, box, _hex(original_color), confidence=confidence)
                if kind == "outline" and bw >= 30 and bh >= 16:
                    inner = pixels[ys.start+3:ys.stop-3, xs.start+3:xs.stop-3]
                    if inner.size:
                        node.background = _hex(np.median(inner.reshape(-1,3), axis=0))
            candidates.append(node)

    # A button border sometimes quantizes into an irregular ring. When a short
    # rectangular crop encloses OCR text, replace that crop with a clean button.
    for node in candidates:
        if node.kind != "raster":
            continue
        x, y, bw, bh = node.box
        if not (50 <= bw <= 220 and 16 <= bh <= 50 and
                any(_contains(node, t, 2) for t in texts)):
            continue
        crop = pixels[y:y+bh, x:x+bw]
        if bh < 6 or bw < 12:
            continue
        edges = np.concatenate((crop[0, 4:-4], crop[-1, 4:-4],
                                crop[3:-3, 0], crop[3:-3, -1]))
        if np.max(np.std(edges.astype(float), axis=0)) > 35:
            continue
        node.kind = "outlined-button"
        node.color = _hex(np.median(edges, axis=0))
        node.background = _hex(np.median(crop[3:-3, 3:-3].reshape(-1,3), axis=0))
        node.image_data = ""
        node.confidence = .7

    # A colored/outlined control can produce both a vector candidate and an
    # unknown outer component. Keeping that outer crop duplicates its text.
    candidates = [node for node in candidates if not (
        (node.kind == "outline" and node.box[2] > w*.7 and node.box[3] > h*.65) or
        any(_contains(control,node,3) or _contains(node,control,3) for control in controls))]
    candidates = [node for node in candidates if node.kind != "raster" or not any(
        other.kind in ("rect", "outline", "outlined-button") and
        other.box[2]*other.box[3] >= node.box[2]*node.box[3]*.55 and
        (_contains(node, other, 1) or _contains(other, node, 1))
        for other in candidates if other is not node)]
    candidates = [node for node in candidates if node.kind != "outlined-button" or not any(
        other is not node and other.kind == "outlined-button" and
        other.box[2]*other.box[3] > node.box[2]*node.box[3] and
        node.box[2]*node.box[3] >= other.box[2]*other.box[3]*.7 and
        _contains(other, node, 2)
        for other in candidates)]

    # Keep a shallow semantic hierarchy; preserve paint order by drawing large
    # surfaces before controls, and text last.
    candidates.sort(key=lambda n: n.box[2]*n.box[3], reverse=True)
    consumed = set()
    for node in candidates:
        if node.kind in ("circle", "ring", "raster") and 8 <= node.box[2] <= 24 and 8 <= node.box[3] <= 24:
            right = node.box[0]+node.box[2]
            nearby = any(0 <= t.box[0]-right <= 30 and
                         abs((t.box[1]+t.box[3]/2)-(node.box[1]+node.box[3]/2)) < 12
                         for t in texts)
            fitted = _radio_from_pixels(pixels, node.box) if nearby else None
            if fitted:
                node.box, selected, node.color = fitted
                node.kind = "radio-selected" if selected else "radio"
                node.image_data = ""
                for other in candidates:
                    if other is node or other.kind not in ("circle", "ring", "raster"):
                        continue
                    if _contains(node, other, 2) and other.box[2]*other.box[3] <= node.box[2]*node.box[3]:
                        consumed.add(id(other))
        elif node.kind in ("rect", "outline") and node.box[2] <= 28 and node.box[3] <= 28:
            right = node.box[0] + node.box[2]
            if any(0 <= t.box[0]-right <= 28 and
                   abs((t.box[1]+t.box[3]/2)-(node.box[1]+node.box[3]/2)) < 10 for t in texts):
                node.kind = "checkbox"
    candidates = [n for n in candidates if id(n) not in consumed]
    if options.ocr:
        examined = 0
        for control in candidates:
            x, y, cw, ch = control.box
            if control.kind not in ("rect", "outline", "outlined-button") or not (55 <= cw <= 300 and 18 <= ch <= 65):
                continue
            if any(_contains(control, t) for t in texts):
                continue
            examined += 1
            if examined > 30:
                break
            for local in _ocr(rgb_image.crop((x,y,x+cw,y+ch)), options.language, psm=7):
                if local.confidence < .65 or sum(c.isalpha() for c in local.text) < 2:
                    continue
                lx, ly, lw, lh = local.box
                local.box = (x+lx, y+ly, lw, lh)
                local.color = _hex(_pixel_color(pixels, local.box))
                if not any(_contains(t, local, 5) for t in texts):
                    texts.append(local)
    root.children = structural + candidates + controls
    for text in texts:
        parents = [n for n in candidates if n.kind in ("rect", "outline", "outlined-button") and
                   n.box[2] > text.box[2]+8 and _contains(n, text)]
        if parents:
            parent = min(parents, key=lambda n: n.box[2]*n.box[3])
            if parent.box[3] <= 65 and parent.box[2] <= 300 and parent.box[0] > 0:
                parent.kind = "button" if parent.kind == "rect" else "outlined-button"
                parent.children.append(text)
                continue
        root.children.append(text)
    # Pair controls with their captions, then group repeated aligned radios.
    for control in candidates:
        if control.kind not in ("radio", "radio-selected", "checkbox"):
            continue
        right = control.box[0]+control.box[2]
        matches = [t for t in root.children if t.kind == "text" and
                   0 <= t.box[0]-right <= 28 and
                   abs((t.box[1]+t.box[3]/2)-(control.box[1]+control.box[3]/2)) < 10]
        if matches:
            caption = min(matches, key=lambda t: t.box[0]-right)
            root.children.remove(caption)
            control.children.append(caption)
    radios = [n for n in candidates if n.kind in ("radio", "radio-selected")]
    clusters = defaultdict(list)
    for radio in radios:
        clusters[round(radio.box[0]/4)].append(radio)
    for row in clusters.values():
        row.sort(key=lambda n:n.box[1])
        if len(row) < 2:
            continue
        for control in row:
            root.children.remove(control)
        all_nodes = [n for control in row for n in (control, *control.children)]
        x = min(n.box[0] for n in all_nodes)
        y = min(n.box[1] for n in all_nodes)
        right = max(n.box[0]+n.box[2] for n in all_nodes)
        bottom = max(n.box[1]+n.box[3] for n in all_nodes)
        root.children.append(Node("radio-group", (x,y,right-x,bottom-y), children=row))
    return root
