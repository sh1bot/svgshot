"""Conservative GUI geometry and text recognition from a PNG."""
from __future__ import annotations

import base64
import copy
import difflib
import io
import json
import re
import shutil
import subprocess
from functools import lru_cache
from collections import Counter, defaultdict
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

from .model import Node
from .artwork import photo_regions, trace_artwork
from .icons import simplify_scene_artwork


@dataclass
class Options:
    colors: int = 10
    min_area: int = 18
    max_raster: int = 64
    raster_fallback: bool = True
    fidelity_fallback: bool = False
    ocr: bool = True
    language: str = "eng"
    font_family: str = "auto"
    smooth_icons: bool = False
    pixel_boundary_icons: bool = False
    estimate_alpha: bool = False
    icon_palette_size: int | None = None
    icon_blur: float = .5
    icon_cache_dir: str | None = None

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
        if ((result.icon_palette_size is not None and (not isinstance(result.icon_palette_size, int)
                or not 2 <= result.icon_palette_size <= 32)) or not np.isfinite(result.icon_blur) or not 0 <= result.icon_blur <= 4):
            raise ValueError("icon_palette_size must be 2–32 and icon_blur 0–4 source pixels")
        if result.smooth_icons and result.pixel_boundary_icons:
            raise ValueError("smooth_icons and pixel_boundary_icons cannot both be enabled")
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


def _ocr_ui(image: Image.Image, language: str, psm: int = 7) -> list[Node]:
    """OCR small controls, retrying with a hard threshold when antialiasing
    makes the normal RGB pass unreadable."""
    result = _ocr(image, language, psm)
    if result:
        return result
    gray = ImageOps.grayscale(image).point(lambda p: 0 if p < 150 else 255)
    return _ocr(gray.convert("RGB"), language, psm)


def _refine_ambiguous_text(source: Image.Image, texts: list[Node], language: str) -> None:
    """Retry short, uncertain labels in isolation to avoid full-window OCR noise."""
    for node in texts:
        x, y, width, height = node.box
        if not (.35 <= node.confidence < .72 and 4 <= height <= 22 and
                2 <= width <= 180 and len(node.text) <= 24):
            continue
        pad_x, pad_y = max(2, min(8, width // 5)), max(2, min(6, height // 2))
        left, top = max(0, x-pad_x), max(0, y-pad_y)
        right, bottom = min(source.width, x+width+pad_x), min(source.height, y+height+pad_y)
        candidates = [item for item in _ocr_ui(source.crop((left, top, right, bottom)),
                                                language, psm=7)
                      if item.confidence >= max(.68, node.confidence+.12)]
        if not candidates:
            continue
        better = max(candidates, key=lambda item: item.confidence)
        bx, by, bw, bh = better.box
        node.text = better.text
        node.box = (left+bx, top+by, bw, bh)
        node.confidence = better.confidence


def _covered_small_fragment(text: Node, labels: list[Node]) -> bool:
    """Discard tiny OCR shards already included in a clear selected-row label."""
    x, y, width, height = text.box
    area = max(1, width*height)
    for label in labels:
        if label.confidence < .7 or len(text.text.strip()) > 4:
            continue
        lx, ly, lw, lh = label.box
        intersection = max(0, min(x+width,lx+lw)-max(x,lx)) * max(0, min(y+height,ly+lh)-max(y,ly))
        if intersection/area >= .65 and text.text.casefold().strip("|.,'\"") in label.text.casefold():
            return True
    return False


@lru_cache(maxsize=1)
def _tesseract_version() -> tuple[int, int]:
    result = subprocess.run(["tesseract", "--version"], capture_output=True,
                            text=True, check=False)
    match = re.search(r"tesseract\s+(\d+)\.(\d+)", result.stdout.lower())
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


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
    # A collage often has a white canvas around several grey windows; the
    # dominant interior color then belongs to the windows, not the canvas.
    if min(h,w) >= 200:
        margin = np.concatenate((image[4,4:w-4],image[h-5,4:w-4],
                                 image[4:h-4,4],image[4:h-4,w-5]))
        if np.mean(np.min(margin,axis=1) >= 248) > .58:
            return np.array([255,255,255],dtype=np.uint8)
    # The outer pixel is often a window frame, not a background. Use the
    # dominant color over the interior instead of a border sample.
    interior = image[2:max(3,h-2):3, 2:max(3,w-2):3].reshape(-1, 3)
    quant = ((interior.astype(np.uint16) + 8) // 16).clip(0, 15)
    key = quant[:, 0] * 256 + quant[:, 1] * 16 + quant[:, 2]
    common = Counter(key.tolist()).most_common(1)[0][0]
    return np.median(interior[key == common], axis=0).astype(np.uint8)


def _white_card(image: np.ndarray) -> tuple[int,int,int,int] | None:
    """A large interior white panel with text holes, distinct from the canvas."""
    h,w = image.shape[:2]
    labels,_ = ndimage.label(np.min(image,axis=2)>=250)
    cards = []
    for label,region in enumerate(ndimage.find_objects(labels),1):
        if region is None:
            continue
        ys,xs = region
        cw,ch = xs.stop-xs.start,ys.stop-ys.start
        if (cw > w*.4 and ch > h*.3 and xs.start > 20 and ys.start > 40 and
                xs.stop < w-20 and ys.stop < h-20 and
                np.mean(labels[region]==label) > .78):
            cards.append((xs.start,ys.start,cw,ch))
    return max(cards,key=lambda b:b[2]*b[3]) if cards else None


def _window_shell(image: np.ndarray, source: Image.Image,
                  language: str, ocr: bool) -> tuple[list[Node], Node | None]:
    """Promote the outer title strip and inset card to independent UI surfaces."""
    card = _white_card(image)
    if card is None:
        return [],None
    height,width = image.shape[:2]
    white_rows = np.mean(np.min(image[:,2:width-2],axis=2)>=248,axis=1)
    first = next((y for y in range(1,min(height//5,60)) if white_rows[y]>.8),None)
    if first is None:
        return [Node("dialog-panel",card,"#ffffff",confidence=.9)],None
    end = first
    while end < min(height//5,70) and white_rows[end]>.8:
        end += 1
    surfaces = [Node("dialog-panel",card,"#ffffff",confidence=.95)]
    title = None
    if 20 <= end-first <= 55:
        surfaces.insert(0,Node("window-header",(1,first,width-2,end-first),
                               "#ffffff",confidence=.9))
        if ocr:
            left,top,right,bottom = 8,first+2,width-65,end-2
            local = _ocr(source.crop((left,top,right,bottom)),language,psm=7)
            if len(local)==1 and local[0].confidence>.7:
                title = local[0]
                x,y,w,h = title.box
                title.box = (left+x,top+y,w,h)
                title.vector_data = {"role":"window-title"}
    return surfaces,title


def _multicolor_marks(image: np.ndarray) -> list[Node]:
    """Identify compact 2×2 grids of solid, differently colored squares."""
    chroma = np.max(image,axis=2).astype(int)-np.min(image,axis=2).astype(int)
    labels,_ = ndimage.label((chroma>80)&(np.max(image,axis=2)>150))
    squares = []
    for label,region in enumerate(ndimage.find_objects(labels),1):
        if region is None:
            continue
        ys,xs = region
        w,h = xs.stop-xs.start,ys.stop-ys.start
        if (7 <= w <= 30 and 7 <= h <= 30 and .85 <= w/h <= 1.15 and
                np.mean(labels[region]==label)>.8):
            color = _hex(np.median(image[region][labels[region]==label],axis=0))
            squares.append(Node("rect",(xs.start,ys.start,w,h),color))
    marks = []
    for a in squares:
        x,y,w,h = a.box
        neighbors = []
        for px,py in ((x+w+1,y),(x,y+h+1),(x+w+1,y+h+1)):
            match = next((b for b in squares if abs(b.box[0]-px)<=2 and
                          abs(b.box[1]-py)<=2 and abs(b.box[2]-w)<=2 and
                          abs(b.box[3]-h)<=2),None)
            if match is None:
                break
            neighbors.append(match)
        if len(neighbors)==3 and len({n.color for n in [a,*neighbors]})==4:
            marks.append(Node("multicolor-mark",(x,y,2*w+1,2*h+1),
                              confidence=.95,children=[a,*neighbors]))
    return marks


def _input_underlines(image: np.ndarray, texts: list[Node],
                      card: tuple[int,int,int,int] | None) -> list[Node]:
    """Find the horizontal rule beneath an email field in a white dialog."""
    if card is None:
        return []
    cx,cy,cw,ch = card
    rules = []
    for label in texts:
        if "@" not in label.text or not _contains_box(card,label.box):
            continue
        tx,ty,tw,th = label.box
        for y in range(ty+th+2,min(ty+th+22,cy+ch-5)):
            left,right = cx+20,cx+cw-20
            row = image[y,left:right]
            dark = np.max(row,axis=1)<170
            bounds = np.r_[0,np.flatnonzero(dark[1:]!=dark[:-1])+1,len(dark)]
            for start,end in zip(bounds[:-1],bounds[1:]):
                x = left+int(start)
                width = int(end-start)
                if (dark[start] and width>cw*.55 and
                        abs(x-tx)<25 and x+width>tx+tw+cw*.1):
                    rules.append(Node("input-underline",(x,y,width,1),
                                      _hex(np.median(row[start:end],axis=0)),
                                      confidence=.95))
                    break
            if rules and rules[-1].box[1]==y:
                break
    return rules


def _contains_box(outer,inner) -> bool:
    x,y,w,h = outer
    a,b,c,d = inner
    return x<=a and y<=b and a+c<=x+w and b+d<=y+h


def _embedded_panels(image: np.ndarray) -> list[Node]:
    """Large white bounded content panes inside otherwise gray windows."""
    h,w = image.shape[:2]
    white = np.min(image,axis=2)>=249
    labels,_ = ndimage.label(white)
    result = []
    for label,region in enumerate(ndimage.find_objects(labels),1):
        if region is None:
            continue
        ys,xs = region
        pw,ph = xs.stop-xs.start,ys.stop-ys.start
        if (w*.25<pw<w*.8 and h*.12<ph<h*.55 and
                xs.start>20 and ys.start>h*.25 and
                xs.stop<w-20 and ys.stop<h-20 and
                np.mean(labels[region]==label)>.9 and
                np.mean(image[ys.start-1,xs.start:xs.stop])<245):
            panel=Node("content-panel",(xs.start-1,ys.start-1,pw+2,ph+2),
                       "#ffffff",background=_hex(image[ys.start-1,xs.start]),
                       confidence=.92)
            for x in range(xs.start+15,xs.stop-15):
                column=image[ys.start+1:ys.start+25,x].astype(int)
                below=image[ys.start+28,x]
                if (len(column)==24 and 215<=np.median(column)<=240 and
                        np.mean(np.max(column,axis=1)-np.min(column,axis=1)<5)>.95 and
                        np.min(below)>248):
                    if not panel.children or x-panel.children[-1].box[0]>3:
                        panel.children.append(Node("column-divider",(x,ys.start+1,1,24),
                                                   _hex(np.median(column,axis=0))))
            result.append(panel)
    return result


def _footer_surface(image: np.ndarray) -> Node | None:
    h,w=image.shape[:2]
    if h<150 or w<250:
        return None
    for y in range(int(h*.55),h-30):
        row=image[y,5:w-5]
        prev=image[y-1,5:w-5]
        color=np.median(row,axis=0)
        if (220<=np.min(color)<=248 and np.ptp(color)<5 and
                np.mean(np.max(np.abs(row.astype(int)-color),axis=1)<5)>.95 and
                np.mean(np.min(prev,axis=1)>=249)>.95 and
                np.mean(np.max(np.abs(image[h-5,5:w-5].astype(int)-color),axis=1)<5)>.95):
            return Node("footer-panel",(1,y,w-2,h-y-1),_hex(color),confidence=.95)
    return None


def _header_illustration(image: np.ndarray) -> Node | None:
    """Compact illustration at the left of a white dialog's body heading."""
    h,w=image.shape[:2]
    if w<250 or h<150:
        return None
    roi=image[35:min(95,h-40),5:min(60,w//4)]
    mask=np.min(roi,axis=2)<245
    yy,xx=np.where(mask)
    if not len(xx):
        return None
    x,y,bw,bh=(int(xx.min()+5),int(yy.min()+35),
               int(xx.max()-xx.min()+1),int(yy.max()-yy.min()+1))
    if not (8<=x<=25 and 40<=y<=70 and 20<=bw<=50 and 18<=bh<=45):
        return None
    crop=image[y:y+bh,x:x+bw].astype(int)
    blue=((crop[:,:,2]-crop[:,:,0]>40)&(crop[:,:,2]-crop[:,:,1]>10)).mean()
    style="window-stack" if blue>.14 else "document"
    return Node("header-icon",(x,y,bw,bh),confidence=.68,
                vector_data={"style":style})


def _titlebar_icon(image: np.ndarray) -> Node | None:
    if image.shape[1]<250 or image.shape[0]<150:
        return None
    crop=image[5:26,5:29].astype(int)
    colored=(crop[:,:,2]-crop[:,:,0]>45)&(crop[:,:,2]-crop[:,:,1]>10)
    yy,xx=np.where(colored)
    neutral=((np.max(crop,axis=2)-np.min(crop,axis=2)<35)&
             (np.mean(crop,axis=2)<110))
    if colored.mean()<.18 or neutral.mean()>.012:
        return None
    x,y,w,h=(int(xx.min()+5),int(yy.min()+5),int(xx.max()-xx.min()+1),
             int(yy.max()-yy.min()+1))
    if not (7<=x<=18 and 7<=y<=17 and 9<=w<=22 and 7<=h<=17):
        return None
    return Node("title-icon",(x-1,y-1,w+2,h+2),confidence=.7)


def _text_area_labels(image: np.ndarray, source: Image.Image,
                      structures: list[Node], language: str) -> list[Node]:
    """Recover text in large white edit boxes whose border confuses OCR."""
    labels=[]
    for panel in structures:
        if panel.kind not in ("outline","rect") or panel.box[2]<180 or panel.box[3]<25:
            continue
        x,y,w,h=panel.box
        crop_x,crop_y,crop_w,crop_h=x,y,w,h
        # The component pass often sees only the lower white half of a text
        # box. Restore the full border from the adjacent label-sized region.
        if panel.kind=="outline" and h>=35:
            panel.box=(max(1,x-1),max(1,y-13),w+2,h+13)
            panel.color="#707070"
            panel.background="#ffffff"
        crop=source.crop((max(0,crop_x+2),max(0,crop_y-14),
                          min(source.width,crop_x+crop_w-2),
                          min(source.height,crop_y+crop_h+16)))
        for local in _ocr_ui(crop,language,psm=7):
            if local.confidence<.35 or not any(c.isalpha() for c in local.text):
                continue
            local.text=local.text.strip(" |'\"")
            lx,ly,lw,lh=local.box
            local.box=(max(0,crop_x+2)+lx,max(0,crop_y-14)+ly,lw,lh)
            labels.append(local)
    return labels


def _blue_rectangles(image: np.ndarray) -> list[tuple[int,int,int,int]]:
    rgb = image.astype(int)
    blue = ((rgb[:,:,2]-rgb[:,:,0]>90)&(rgb[:,:,2]-rgb[:,:,1]>35)&
            (rgb[:,:,1]>60))
    merged = ndimage.binary_closing(blue,structure=np.ones((3,5)),border_value=0)
    labels,_ = ndimage.label(merged)
    boxes = []
    for region in ndimage.find_objects(labels):
        if region is not None:
            ys,xs=region
            boxes.append((xs.start,ys.start,xs.stop-xs.start,ys.stop-ys.start))
    return boxes


def _focused_controls(image: np.ndarray, source: Image.Image,
                      language: str, ocr: bool) -> tuple[list[Node],list[Node]]:
    h,w = image.shape[:2]
    controls,texts = [],[]
    for x,y,bw,bh in _blue_rectangles(image):
        if (w*.55<=bw<=w*.9 and 18<=bh<=32 and
                30<y<h*.75 and x>35):
            controls.append(Node("input-field",(x,y,bw,bh),"#0078d7",
                                 background="#ffffff",confidence=.93))
            # A selected value is a smaller blue rectangle within the field.
            for left in range(x+3,min(x+12,x+bw-40)):
                runs = image[y+3:y+bh-3,left]
                if len(runs)<6 or np.mean((runs[:,2].astype(int)-runs[:,0].astype(int)>80))<.6:
                    continue
                start = left
                end = start
                while end<x+bw-40 and (
                    int(image[y+3,end,2])-int(image[y+3,end,0])>80):
                    end += 1
                if end-start<20:
                    continue
                controls.append(Node("text-selection",(start,y+3,end-start,bh-8),
                                     "#0078d7",confidence=.95))
                if ocr:
                    crop = source.crop((start,y+3,end,y+bh-4))
                    for label in _ocr(crop,language,psm=7):
                        if label.confidence>.7:
                            tx,ty,tw,th=label.box
                            label.box=(start+tx,y+3+ty,tw,th)
                            label.color="#ffffff"
                            label.vector_data["role"]="selected-text"
                            texts.append(label)
                break
        elif (w*.12<bw<w*.55 and 15<=bh<=30 and
              x<=w*.05 and h*.1<y<h*.75):
            box=(max(1,x-1),y,min(w-2,bw+1),bh)
            controls.append(Node("selected-row",box,"#0078d7",confidence=.95))
            if ocr:
                left,top=box[0]+3,y+1
                for label in _ocr(source.crop((left,top,box[0]+box[2]-2,y+bh-1)),
                                  language,psm=7):
                    if label.confidence>.7:
                        tx,ty,tw,th=label.box
                        label.box=(left+tx,top+ty,tw,th)
                        label.color="#ffffff"
                        texts.append(label)
    return controls,texts


def _disabled_buttons(image: np.ndarray) -> list[Node]:
    h,w=image.shape[:2]
    result=[]
    for y in range(int(h*.35),int(h*.85)):
        for x,right,color in _flat_runs(image[y],80,150):
            if not (x>w*.55 and 175<=int(color[0])<=205 and
                    np.max(color)-np.min(color)<5 and
                    0<y+3<h and np.mean(image[y+2,x+3].astype(int))>=195 and
                    np.mean(image[y+2,x+3].astype(int))<=215):
                continue
            if any(abs(n.box[0]-x)<3 and abs(n.box[1]-y)<30 for n in result):
                continue
            for bottom in range(y+19,min(y+31,h-1)):
                if np.mean(np.all(image[bottom,x:right]==color,axis=1))>.98:
                    result.append(Node("disabled-button",(x,y,right-x,bottom-y+1),
                                       "#cccccc",background=_hex(color),confidence=.9))
                    break
    return result


def _decorative_background(image: np.ndarray) -> Node | None:
    """Classify a smooth illustrated backdrop behind a centered white card."""
    h,w = image.shape[:2]
    card = _white_card(image)
    if card is None:
        return None
    header = next((y for y in range(5,min(h//4,100)) if
                   np.mean(np.min(image[y,10:w-10],axis=1)<248)>.65),0)
    if header < 10:
        return None
    samples = [image[header+25,int(w*.1)],image[header+25,int(w*.9)],
               image[h-65,int(w*.9)]]
    if np.ptp(np.array(samples).astype(int),axis=0).max() < 12:
        return None
    pale = ((image[:,:,0]>=246)&(image[:,:,1]>=249)&(image[:,:,2]>=250))
    left = [y for y in range(max(header+50,int(h*.3)),h-40)
            if pale[y,5:12].mean()>.85 and pale[y+1:y+20,5:12].mean()>.8]
    wave = ""
    if left:
        start = left[0]
        rightmost = []
        for y in range(card[1]+card[3]+5,h-5):
            row = pale[y,5:w-5]
            first_nonwhite = np.flatnonzero(~row)
            edge = int(first_nonwhite[0]+5) if len(first_nonwhite) else w-5
            rightmost.append((edge,y))
        peak_x,peak_y = max(rightmost,default=(0,0))
        bottom_x = rightmost[-1][0] if rightmost else 0
        if peak_x > w*.35 and bottom_x > w*.2:
            wave = (f'M 1 {start} C {card[0]+45} {start+95}, '
                    f'{peak_x-95} {peak_y-45}, {peak_x} {peak_y} '
                    f'Q {peak_x-8} {peak_y+42}, {bottom_x} {h-2} '
                    f'L 1 {h-2} Z')
    return Node("decorative-background",(1,header,w-2,h-header-2),
                confidence=.7,vector_data={
                    "top_left":_hex(samples[0]),"top_right":_hex(samples[1]),
                    "bottom_right":_hex(samples[2]),
                    "wave_path":wave,
                    "wave_color":_hex(image[h-35,min(35,w-1)])})


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


def _clipped_outline(node: Node, width: int, height: int) -> bool:
    x,y,bw,bh=node.box
    return (node.kind=="outline" and bw*bh>width*height*.02 and
            (x<=1 or y<=1 or x+bw>=width-1 or y+bh>=height-1))


def _raster(image: Image.Image, box) -> str:
    x, y, w, h = box
    buffer = io.BytesIO()
    image.crop((x, y, x+w, y+h)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _title_gradient(image: np.ndarray, box) -> str:
    """One clean horizontal PNG strip, stretched across the entire title bar."""
    x,y,w,h = box
    # Sample below the caption and close control, retaining the source's
    # horizontal gradient without embedding either control in the bitmap.
    first,last = (2,5) if h < 45 else (h-10,h-5)
    row = np.median(image[y+first:y+last,x:x+w],axis=0)
    row = ndimage.gaussian_filter1d(row,4,axis=0).clip(0,255).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(row[None,:,:],"RGB").save(buffer,format="PNG")
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


def _blue_title_bars(image: np.ndarray) -> list[tuple[int,int,int,int]]:
    """Locate saturated title strips independently of the canvas origin."""
    rgb = image.astype(np.int16)
    blue = ((rgb[:,:,2]-rgb[:,:,0] > 40) &
            (rgb[:,:,2]-rgb[:,:,1] > 18) & (rgb[:,:,2] > 80))
    joined = ndimage.binary_closing(blue, structure=np.ones((3,9),dtype=bool),
                                     iterations=2)
    labels, _ = ndimage.label(joined)
    bars = []
    for region in ndimage.find_objects(labels):
        if region is None:
            continue
        ys, xs = region
        w,h = xs.stop-xs.start, ys.stop-ys.start
        if (w >= 180 and 15 <= h <= 110 and w/h >= 3 and
                ys.stop < image.shape[0]-20 and blue[region].mean() > .65):
            bars.append((xs.start,ys.start,w,h))
    return bars


def _blue_window_surfaces(image: np.ndarray) -> list[Node]:
    """Fill the continuous body below each blue title bar as one surface."""
    height,width = image.shape[:2]
    surfaces = []
    rgb = image.astype(np.int16)
    blue = ((rgb[:,:,2]-rgb[:,:,0] > 40) &
            (rgb[:,:,2]-rgb[:,:,1] > 18) & (rgb[:,:,2] > 80))
    for x,y,w,h in _blue_title_bars(image):
        if blue[y:y+h,x:x+w].mean() < .85:
            continue
        first,last = (2,5) if h < 45 else (h-10,h-5)
        title_color = _hex(np.median(image[y+first:y+last,x:x+w]
                                     .reshape(-1,3),axis=0))
        surfaces.append(Node("gradient-title",(x,y,w,h),title_color,
                             image_data=_title_gradient(image,(x,y,w,h))))
        left,right = max(0,x-3),min(width,x+w+3)
        start = y+h+40
        if start >= height-20:
            continue
        dark = np.mean(np.max(image[start:,left:right],axis=2)<155,axis=1) > .92
        complete = np.flatnonzero(dark[:-2] & dark[1:-1] & dark[2:])
        if not len(complete):
            continue
        bottom = start+int(complete[0])
        if bottom-y < h+60:
            continue
        body = image[y+h:bottom,left:right]
        color = _background(body)
        surfaces.append(Node("rect",(left,y+h,right-left,bottom-y-h),_hex(color)))
    return surfaces


def _x_components(image: np.ndarray, box) -> list[Node]:
    left,top,width,height = box
    patch = image[top:top+height,left:left+width]
    if patch.size == 0:
        return []
    ink = np.max(patch,axis=2) < 155
    labels,_ = ndimage.label(ink,structure=np.ones((3,3),dtype=int))
    found = []
    for label,region in enumerate(ndimage.find_objects(labels),start=1):
        if region is None:
            continue
        ys,xs = region
        w,h = xs.stop-xs.start,ys.stop-ys.start
        if not (7 <= w <= 45 and 7 <= h <= 45 and .7 <= w/h <= 1.4):
            continue
        shape = labels[region] == label
        yy,xx = np.nonzero(shape)
        tolerance = max(1.25,min(w,h)*.18)
        a = np.abs(xx*(h-1)/max(1,w-1)-yy) <= tolerance
        b = np.abs((w-1-xx)*(h-1)/max(1,w-1)-yy) <= tolerance
        margin = max(2,round(min(w,h)*.13))
        corners = (shape[:margin,:margin],shape[:margin,-margin:],
                   shape[-margin:,:margin],shape[-margin:,-margin:])
        if (a.mean() < .25 or b.mean() < .25 or (a|b).mean() < .60 or
                not all(corner.any() for corner in corners)):
            continue
        color = _hex(np.median(patch[region][shape],axis=0))
        found.append(Node("close-icon",(left+xs.start,top+ys.start,w,h),color))
    return found


def _window_close_controls(image: np.ndarray, texts: list[Node]) -> list[Node]:
    """Find X icons beside title strips and red close dots on the left."""
    height,width = image.shape[:2]
    found = []
    for x,y,w,h in _blue_title_bars(image):
        for left,right in ((max(0,x-8),min(width,x+min(w,70))),
                           (max(0,x+w-min(w,80)),min(width,x+w+8))):
            top,bottom = max(0,y-8),min(height,y+h+8)
            found.extend(_x_components(image,(left,top,right-left,bottom-top)))
    # Light title bars may have no saturated pixels (modern Windows, macOS).
    # Require a nearby OCR title to avoid interpreting ordinary X characters.
    top_limit = min(height//3,220)
    for title in texts:
        tx,ty,tw,th = title.box
        if ty > top_limit or th < 7 or title.confidence < .55:
            continue
        left = max(tx+tw+35,width//2)
        if left >= width-4:
            continue
        top,bottom = max(0,ty-16),min(height,ty+th+17)
        for icon in _x_components(image,(left,top,width-left-3,bottom-top)):
            if abs(icon.box[1]+icon.box[3]/2-(ty+th/2)) <= max(18,th):
                found.append(icon)
        left_end = min(max(0,tx-30),width//3)
        if left_end > 8:
            for icon in _x_components(image,(3,top,left_end-3,bottom-top)):
                ix,iy,iw,ih = icon.box
                surround = image[max(0,iy-3):min(height,iy+ih+3),
                                 max(0,ix-3):min(width,ix+iw+3)]
                if (abs(iy+ih/2-(ty+th/2)) <= max(18,th) and
                        np.min(np.median(surround.reshape(-1,3),axis=0)) >= 210):
                    found.append(icon)
    # A red circular control at the left of a title is a common macOS close
    # button. Its small color component is less ambiguous than a dark X.
    rgb = image.astype(np.int16)
    red = ((rgb[:,:,0]-rgb[:,:,1] > 55) &
           (rgb[:,:,0]-rgb[:,:,2] > 45) & (rgb[:,:,0] > 130))
    labels,_ = ndimage.label(red[:top_limit],structure=np.ones((3,3),dtype=int))
    for label,region in enumerate(ndimage.find_objects(labels),start=1):
        if region is None:
            continue
        ys,xs = region
        w,h = xs.stop-xs.start,ys.stop-ys.start
        if not (8 <= w <= 30 and 8 <= h <= 30 and .75 <= w/h <= 1.3):
            continue
        if any(t.box[0] > xs.stop and abs(t.box[1]+t.box[3]/2-(ys.start+h/2)) < 20
               for t in texts):
            color = _hex(np.median(image[region][labels[region]==label],axis=0))
            found.append(Node("close-dot",(xs.start,ys.start,w,h),color))
    unique = []
    for node in found:
        if not any(abs(node.box[0]-n.box[0]) < 4 and abs(node.box[1]-n.box[1]) < 4
                   for n in unique):
            unique.append(node)
    return unique


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
        if not (7 <= label.box[3] <= 20 and 20 <= label.box[0] < width-100
                and label.box[1] >= 30 and label.text not in ("Exception", "Type")):
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
                # Newer Tesseract releases can change the confidence or split
                # punctuation on a small bordered button. Read the interior
                # with several sparse modes and choose the strongest single
                # candidate instead of requiring one exact TSV result.
                inset = (x+2,y+2,max(x+3,right-2),max(y+3,bottom-1))
                crop = source.crop((x,y,right,bottom+1))
                origin = (x,y)
                recognized = [t for t in _ocr_ui(crop,language,psm=7)
                              if t.confidence >= .7 and any(c.isalpha() for c in t.text)]
                if len(recognized) > 1:
                    break
                if not recognized and _tesseract_version() >= (5, 5):
                    crop = source.crop(inset)
                    origin = inset[:2]
                    candidates = []
                    for psm in (6,7,11):
                        candidates.extend(_ocr_ui(crop,language,psm=psm))
                    recognized = [t for t in candidates
                                  if t.confidence >= .35 and any(c.isalpha() for c in t.text)]
                    # The sparse modes commonly return the same label with
                    # slightly different punctuation. Treat those as one
                    # reading when their boxes overlap.
                    unique = []
                    for candidate in sorted(recognized,key=lambda t:t.confidence,reverse=True):
                        if not any(_iou_boxes(candidate.box,old.box)>.5 and
                                   difflib.SequenceMatcher(None,
                                       candidate.text.casefold(),old.text.casefold()).ratio()>.75
                                   for old in unique):
                            unique.append(candidate)
                    recognized = unique
                if len(recognized) != 1:
                    break
                label = recognized[0]
                lx,ly,lw,lh = label.box
                label.box = (origin[0]+lx,origin[1]+ly,lw,lh)
                controls.append(Node("outlined-button", (x,y,right-x,bottom-y+1),
                                     _hex(color), background=_hex(image[y+4,x+5])))
                labels.append(label)
                break
    return controls, labels


def _large_beveled_buttons(image: np.ndarray, source: Image.Image,
                           language: str) -> list[Node]:
    """Recover wide, raised buttons from their dark bottom and right rails."""
    height,width = image.shape[:2]
    gray = image.astype(float) @ np.array([.2126,.7152,.0722])
    buttons = []
    for y in range(max(25,int(height*.65)),height-20):
        labels,_ = ndimage.label(gray[y] < 175)
        for region in ndimage.find_objects(labels):
            if region is None:
                continue
            x,right = region[0].start,region[0].stop
            bw = right-x
            if not (max(200,int(width*.16)) <= bw <= width*.45):
                continue
            if any(abs(x-n.box[0]) < 8 and abs(y-(n.box[1]+n.box[3])) < 12
                   for n in buttons):
                continue
            rail_x = right-4
            top = y
            while top > max(1,y-130) and gray[top-1,rail_x] < 175:
                top -= 1
            bottom = y+1
            while bottom < min(height,y+12) and gray[bottom,rail_x] < 175:
                bottom += 1
            bh = bottom-top
            if not (50 <= bh <= 120 and top >= height*.6):
                continue
            inside = np.median(gray[top+12:bottom-12,x+20:right-20])
            upper = np.median(gray[top+3:top+8,x+20:right-20])
            rail = np.median(gray[top+12:bottom-12,rail_x])
            if not (inside > 170 and upper > inside+12 and rail < inside-45):
                continue
            inset = (x+25,top+15,right-25,bottom-18)
            a,b,c,d = inset
            labels = [n for n in _ocr(source.crop(inset),language,psm=7)
                      if n.confidence > .65 and n.text.isalpha()]
            if len(labels) != 1:
                continue
            label = labels[0]
            lx,ly,lw,lh = label.box
            label.box = (a+lx,b+ly,lw,lh)
            label.color = _hex(_pixel_color(image,label.box))
            color = _hex(np.median(image[top+12:bottom-12,x+20:right-20]
                                    .reshape(-1,3),axis=0))
            buttons.append(Node("beveled-button",(x,top,bw,bh),color,
                                confidence=.85,children=[label]))
    return buttons


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
    nodes.extend(_blue_window_surfaces(image))
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


def _table_text(source, texts, language):
    """Read dense report tables by column; whole-page OCR often joins or skips rows.

    Require aligned headers, visible column dividers, and repeated body rows.
    This pixel-based pass works equally with and without accessibility metadata.
    """
    pixels = np.asarray(source.convert("RGB"))
    height, width = pixels.shape[:2]
    groups = []
    for text in sorted(texts, key=lambda n: n.box[1]):
        if not 2 <= len(text.text.strip()) <= 24 or text.box[3] > 40:
            continue
        center = text.box[1] + text.box[3]/2
        group = next((g for g in groups if abs(center-g[0]) < 10), None)
        if group is None:
            groups.append([center, [text]])
        else:
            group[1].append(text)
    for _, headers in groups:
        headers.sort(key=lambda n: n.box[0])
        if len(headers) < 4 or headers[-1].box[0]-headers[0].box[0] < width*.3:
            continue
        top = max(0, min(n.box[1] for n in headers)-10)
        bottom = min(height, max(n.box[1]+n.box[3] for n in headers)+3)
        strip = pixels[top:bottom]
        gray = strip.mean(axis=2)
        separators = np.flatnonzero(((gray > 205) & (gray < 248) &
                                     (strip.max(axis=2)-strip.min(axis=2) < 12)).mean(axis=0) > .65)
        cuts = [max(0, headers[0].box[0]-3)]
        for left, right in zip(headers, headers[1:]):
            choices = separators[(separators > left.box[0]+left.box[2]) & (separators < right.box[0])]
            if not len(choices):
                break
            cuts.append(int(choices[-1]))
        if len(cuts) != len(headers):
            continue
        last = headers[-1]
        choices = separators[separators > last.box[0]+last.box[2]]
        if not len(choices):
            continue
        cuts.append(int(choices[0]))
        # A full-width frame marks the end of the scrollable body.
        body = pixels[bottom:, cuts[0]:cuts[-1]].mean(axis=2)
        lines = np.flatnonzero(((body > 45) & (body < 195)).mean(axis=1) > .7)
        lines = lines[lines > 80]
        end = bottom+int(lines[0]) if len(lines) else height
        columns = []
        for index, (left, right) in enumerate(zip(cuts, cuts[1:])):
            left += 2
            nodes = _ocr(source.crop((left, bottom, right-2, end)), language, psm=6)
            if index == 0 and len(nodes) >= 6:
                # Repeated row icons occupy a narrow strip before the labels.
                inset = round(float(np.median([n.box[0] for n in nodes])))
                if 8 <= inset <= 36:
                    left += inset
                    nodes = _ocr(source.crop((left, bottom, right-2, end)), language, psm=6)
            typical_height = float(np.median([n.box[3] for n in nodes])) if nodes else 0
            nodes = [n for n in nodes if n.box[3] >= typical_height*.6]
            for node in nodes:
                x, y, w, h = node.box
                node.box = (left+x, bottom+y, w, h)
                node.vector_data.update(source="ocr", role="table-cell", font_weight=400)
                node.color = _hex(_pixel_color(pixels, node.box))
            columns.append(nodes)
        if sum(len(c) >= 6 for c in columns) < 2:
            continue
        # Record the inferred cells so semantic row bounds cannot consume neighbours.
        for index, column in enumerate(columns):
            for node in column:
                node.vector_data["column_bounds"] = [cuts[index], bottom, cuts[index+1]-cuts[index], end-bottom]
        return (cuts[0], bottom, cuts[-1]-cuts[0], end-bottom), [n for c in columns for n in c]
    return None, []


def reconstruct(image: Image.Image, options: Options, *, artwork_boxes=(),
                source_image=None) -> Node:
    if image.width * image.height > 25_000_000:
        raise ValueError("PNG exceeds the 25-megapixel limit")
    rgb_image = image.convert("RGB")
    if artwork_boxes:
        rgb_image = rgb_image.copy()
        from PIL import ImageDraw
        draw = ImageDraw.Draw(rgb_image)
        for x,y,w,h in artwork_boxes:
            draw.rectangle((x,y,x+w-1,y+h-1), fill="white")
    pixels = np.asarray(rgb_image)
    h, w = pixels.shape[:2]
    background = _background(pixels)
    root = Node("window", (0, 0, w, h), _hex(background))
    decorative = _decorative_background(pixels)
    marks = _multicolor_marks(pixels)
    disabled = _disabled_buttons(pixels)
    footer_surface = _footer_surface(pixels)
    illustration = _header_illustration(pixels)
    title_icon = _titlebar_icon(pixels)
    structural = _structure(pixels)+_embedded_panels(pixels)+disabled
    if illustration:
        structural.append(illustration)
    if title_icon:
        structural.append(title_icon)
    if footer_surface:
        structural.insert(0,footer_surface)
    photos = photo_regions(rgb_image)
    texts = _ocr(rgb_image, options.language) if options.ocr else []
    primary_texts = copy.deepcopy(texts)
    focused,focused_text = _focused_controls(pixels,rgb_image,options.language,options.ocr)
    structural.extend(focused)
    shell,title = _window_shell(pixels,rgb_image,options.language,options.ocr)
    if shell:
        structural = [n for n in structural if not any(
            n.kind=="rect" and _contains(panel,n,2) and
            n.box[2]*n.box[3] > panel.box[2]*panel.box[3]*.85
            for panel in shell)]
        structural = shell+structural
    # A group-box detector can join an interior divider to the captured
    # window's outside frame. That partial edge is not a complete panel.
    structural = [n for n in structural if not _clipped_outline(n,w,h)]
    if options.ocr:
        for label in _text_area_labels(pixels,rgb_image,structural,options.language):
            texts = [old for old in texts if _iou_boxes(old.box,label.box)<.2]
            label.color = _hex(_pixel_color(pixels,label.box))
            texts.append(label)
    for icon in _window_close_controls(pixels,texts):
        if not any(abs(old.box[0]-icon.box[0]) < 4 and
                   abs(old.box[1]-icon.box[1]) < 4
                   for old in structural if old.kind in ("close-icon","close-dot")):
            structural.append(icon)
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
        _refine_ambiguous_text(rgb_image, texts, options.language)
        if focused_text:
            texts = [text for text in texts if not _covered_small_fragment(text, focused_text)]
        # Sparse full-image OCR can miss short white captions on small blue
        # bars. Read the left end of each bar separately, away from its X.
        for bx,by,bw,bh in _blue_title_bars(pixels):
            if bh >= 45 or any(t.box[0] < bx+150 and
                    by-4 <= t.box[1] <= by+bh and t.box[0]+t.box[2] > bx
                    for t in texts):
                continue
            crop = rgb_image.crop((bx+4,by,bx+min(bw-50,190),by+bh))
            labels = _ocr(crop,options.language,psm=7)
            if not labels or labels[0].confidence < .58:
                continue
            label = labels[0]
            word = label.text.split()[0].strip("|—-.,")
            if not word or not any(ch.isalpha() for ch in word):
                continue
            lx,ly,lw,lh = label.box
            text_height = min(lh,round(bh*.66))
            texts.append(Node("text",(bx+4+lx,max(by+4,by+ly),
                                       min(lw,max(20,round(text_height*.57*len(word)))),text_height),
                              "#ffffff",text=word,confidence=label.confidence))
        for mark in marks:
            mx,my,mw,mh = mark.box
            nearby = [t for t in texts if t.box[0] <= mx+mw+6 and
                      t.box[0]+t.box[2] > mx+mw and
                      max(my,t.box[1]) < min(my+mh,t.box[1]+t.box[3])]
            if not nearby:
                continue
            right = max(t.box[0]+t.box[2] for t in nearby)+8
            crop_x = mx+mw+4
            crop_y = max(0,my-4)
            crop = rgb_image.crop((crop_x,crop_y,min(w,right),min(h,my+mh+7)))
            local = [t for t in _ocr(crop,options.language,psm=7)
                     if t.confidence>.75 and len(t.text)>2]
            if local:
                texts = [t for t in texts if t not in nearby]
                t = local[0]
                tx,ty,tw,th = t.box
                t.box = (crop_x+tx,crop_y+ty,tw,th)
                texts.append(t)
        if title:
            texts = [t for t in texts if not (
                abs(t.box[1]-title.box[1])<10 and t.box[0]<title.box[0]+title.box[2]+30)]
            texts.append(title)
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
    for label in focused_text:
        texts = [t for t in texts if _iou_boxes(t.box,label.box)<.2]
        texts.append(label)
    # Sparse OCR occasionally loses the first word of a short field label.
    for node in texts:
        if not (node.text.endswith(":") and 40<node.box[0]<w-100):
            continue
        x,y,tw,th=node.box
        left,top=max(0,x-50),max(0,y-3)
        candidates = _ocr(rgb_image.crop((left,top,min(w,x+tw+4),min(h,y+th+3))),
                          options.language,psm=7)
        if len(candidates)==1 and candidates[0].confidence>.8 and (
                candidates[0].text.endswith(node.text) and
                len(candidates[0].text)>len(node.text)+3):
            better=candidates[0]
            bx,by,bw,bh=better.box
            node.text=better.text
            node.box=(left+bx,top+by,bw,bh)
    # Blue links inside a focus rectangle can lose their first glyph to the
    # dotted border. A tight local crop restores the complete phrase without
    # letting the rectangle enter the OCR line.
    blue_links = []
    for node in texts:
        x,y,tw,th = node.box
        if tw < 35 or th < 7 or x < 10:
            continue
        crop = pixels[y:y+th,x:x+tw].astype(np.int16)
        colored = (crop[:,:,2]-crop[:,:,0]>55)&(crop[:,:,2]-crop[:,:,1]>10)
        fraction = colored.mean() if colored.size else 0
        if not (.02 < fraction < .45):
            continue
        blue_links.append(node)
        left,top = x-10,max(0,y-1)
        right,bottom = min(w,x+tw+2),min(h,y+th+1)
        candidates = [t for t in _ocr(rgb_image.crop((left,top,right,bottom)),
                                       options.language,psm=7)
                      if t.confidence>.8 and len(t.text)>len(node.text)]
        if len(candidates)==1:
            better = candidates[0]
            better_text = better.text.strip(" ‘'|:;,. ")
            if len(better_text)>len(node.text) and better_text.casefold().endswith(node.text.casefold()):
                lx,ly,lw,lh = better.box
                node.text = better_text
                node.box = (left+lx,top+ly,lw,lh)
    texts = [t for t in texts if not (t.box[2]<=3 and t.box[3]<=5 and any(
        abs(t.box[0]-(link.box[0]+link.box[2]))<10 and
        abs(t.box[1]-(link.box[1]+link.box[3]))<8 for link in blue_links))]
    checkboxes = _checkboxes(pixels, texts)
    dropdowns = _dropdowns(pixels, texts)
    broad_buttons = _large_beveled_buttons(pixels,rgb_image,options.language) if options.ocr else []
    for button in broad_buttons:
        bx,by,bw,bh = button.box
        texts = [t for t in texts if not (
            max(bx,t.box[0]) < min(bx+bw,t.box[0]+t.box[2]) and
            max(by,t.box[1]) < min(by+bh,t.box[1]+t.box[3]))]
    controls = checkboxes + dropdowns + footer + tabs + broad_buttons
    reserved = np.zeros((h, w), dtype=bool)
    for node in texts:
        x, y, tw, th = node.box
        reserved[max(0,y-2):min(h,y+th+3), max(0,x-2):min(w,x+tw+3)] = True
        node.color = ("#ffffff" if node.vector_data.get("role")=="selected-text"
                      else _hex(_pixel_color(pixels, node.box)))
        crop = pixels[y:y+th,x:x+tw].astype(np.int16)
        colored = (crop[:,:,2]-crop[:,:,0]>55)&(crop[:,:,2]-crop[:,:,1]>10)
        fraction = colored.mean() if colored.size else 0
        neutral_dark = ((np.max(crop,axis=2)-np.min(crop,axis=2)<35)&
                        (np.mean(crop,axis=2)<110)).mean() if crop.size else 0
        if .02 < fraction < .45 and neutral_dark < .015 and node.vector_data.get("role")!="selected-text":
            node.color = _hex(np.median(crop[colored],axis=0))
            node.vector_data["role"] = "link"

    structural += _input_underlines(pixels,texts,_white_card(pixels))

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
                if max(bw, bh) > options.max_raster or area < 35:
                    continue
                crop = pixels[ys.start:ys.stop,xs.start:xs.stop].astype(np.int16)
                base = np.median(crop.reshape(-1,3),axis=0)
                if np.percentile(np.max(np.abs(crop-base),axis=2),95) < 75:
                    continue
                node = Node("raster", box, confidence=confidence,
                            image_data=_raster(rgb_image, box) if options.raster_fallback else "")
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
        _clipped_outline(node,w,h) or
        any(_contains(control,node,3) or _contains(node,control,3) for control in controls))]
    candidates = [node for node in candidates if not any(_contains(mark,node,2) for mark in marks)]
    candidates = [node for node in candidates if not any(
        node.kind=="rect" and _contains(panel,node,3) and
        node.box[2]*node.box[3] > panel.box[2]*panel.box[3]*.85
        for panel in shell if panel.kind=="dialog-panel")]
    def overlaps_button(node):
        x,y,cw,ch = node.box
        for button in broad_buttons:
            bx,by,bw,bh = button.box
            intersection = max(0,min(x+cw,bx+bw)-max(x,bx))*max(0,min(y+ch,by+bh)-max(y,by))
            if intersection > cw*ch*.35:
                return True
        return False
    candidates = [node for node in candidates if not overlaps_button(node)]
    title_bars = [n for n in structural if n.kind == "gradient-title"]
    candidates = [node for node in candidates if not any(
        bar.box[0]-3 <= node.box[0] and node.box[0]+node.box[2] <= bar.box[0]+bar.box[2]+3 and
        ((bar.box[1]-25 <= node.box[1] <= bar.box[1]+bar.box[3]+30 and node.box[3] <= 32) or
         (bar.box[1]-3 <= node.box[1] and
          node.box[1]+node.box[3] <= bar.box[1]+bar.box[3]+3 and
          node.box[2] >= bar.box[2]*.8) or
         (node.kind == "raster" and bar.box[1] <= node.box[1] and
          node.box[1]+node.box[3] <= bar.box[1]+bar.box[3] and
          node.box[2] <= 75 and node.box[3] <= 75))
        for bar in title_bars)]
    # Faint watermark fragments inside an otherwise flat window body are not
    # useful editable controls. Keep strong-contrast geometry and text.
    body_surfaces = [n for n in structural if n.kind == "rect" and
                     n.box[2] > w*.3 and n.box[3] > h*.15]
    def faint_body_detail(node):
        if node.kind not in ("rect","line","ring","circle") or node.box[2]*node.box[3] > 2000:
            return False
        for body in body_surfaces:
            if not _contains(body,node,-20):
                continue
            color = np.array([int(node.color[i:i+2],16) for i in (1,3,5)])
            base = np.array([int(body.color[i:i+2],16) for i in (1,3,5)])
            if np.max(np.abs(color-base)) < 35 and not any(
                abs((t.box[0]+t.box[2]/2)-(node.box[0]+node.box[2]/2)) < 45 and
                abs((t.box[1]+t.box[3]/2)-(node.box[1]+node.box[3]/2)) < 35
                for t in texts):
                return True
        return False
    candidates = [node for node in candidates if not faint_body_detail(node)]
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
    if illustration:
        candidates=[n for n in candidates if not _contains(illustration,n,3)]
    if title_icon:
        candidates=[n for n in candidates if not _contains(title_icon,n,3)]
    candidates = [n for n in candidates if not any(
        _iou_boxes(n.box,button.box)>.6 for button in disabled)]
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
            for local in _ocr_ui(rgb_image.crop((x,y,x+cw,y+ch)), options.language, psm=7):
                if local.confidence < .65 or sum(c.isalpha() for c in local.text) < 2:
                    continue
                lx, ly, lw, lh = local.box
                local.box = (x+lx, y+ly, lw, lh)
                local.color = _hex(_pixel_color(pixels, local.box))
                if not any(_contains(t, local, 5) for t in texts):
                    texts.append(local)
    if options.ocr:
        table_box, table_text = _table_text(rgb_image, texts, options.language)
        if table_box:
            table = Node("rect", table_box, color=_hex(_background(pixels[
                table_box[1]:table_box[1]+table_box[3], table_box[0]:table_box[0]+table_box[2]])))
            texts = [n for n in texts if not (
                max(0, min(n.box[0]+n.box[2], table_box[0]+table_box[2])-max(n.box[0], table_box[0])) *
                max(0, min(n.box[1]+n.box[3], table_box[1]+table_box[3])-max(n.box[1], table_box[1])) >
                n.box[2]*n.box[3]*.3)] + table_text
            candidates = [n for n in candidates if not _contains(table, n, 3)]
            structural.append(table)
    # Control heuristics can mistake large headline strokes for an edit border.
    # Retain a confident full-page reading when a local retry lost its prefix.
    restored = []
    def shared_area(a, b):
        x,y,w,h = a.box; q,r,s,t = b.box
        return max(0,min(x+w,q+s)-max(x,q))*max(0,min(y+h,r+t)-max(y,r))
    for original in primary_texts:
        if original.confidence < .85 or original.box[3] < 28:
            continue
        broken = [n for n in texts if shared_area(n,original) > min(n.box[2]*n.box[3],original.box[2]*original.box[3])*.6
                  and len(n.text) < len(original.text)*.9
                  and difflib.SequenceMatcher(None,n.text.casefold(),original.text.casefold()).ratio() > .65]
        if broken:
            texts = [n for n in texts if n not in broken]
            original.color = _hex(_pixel_color(pixels,original.box))
            texts.append(original)
            restored.append(original)
    candidates = [n for n in candidates if not any(shared_area(n,t) > n.box[2]*n.box[3]*.4 for t in restored)]
    structural = [n for n in structural if not any(shared_area(n,t) > n.box[2]*n.box[3]*.4 for t in restored)]
    controls = [n for n in controls if not any(shared_area(n,t) > n.box[2]*n.box[3]*.4 for t in restored)]
    # A second OCR pass may return a whole menu row already represented by
    # separate captions. Keep those local boxes, rather than stretching a line.
    duplicates = set()
    for n in texts:
        contained = [t for t in texts if t is not n and t.box[2] < n.box[2]*.8 and _contains(n,t,4)]
        if len(contained) >= 2:
            joined = ' '.join(t.text for t in sorted(contained,key=lambda t:t.box[0]))
            if difflib.SequenceMatcher(None,joined.casefold(),n.text.casefold()).ratio() > .8:
                duplicates.add(id(n))
    texts = [n for n in texts if id(n) not in duplicates]
    pictures = [trace_artwork(rgb_image, box, opaque=True) for box in photos]
    if pictures:
        texts = [n for n in texts if not any(_contains(p, n, 3) for p in pictures)]
        candidates = [n for n in candidates if not any(_contains(p, n, 3) for p in pictures)]
        structural = [n for n in structural if not any(_contains(p, n, 3) for p in pictures)]
    # Dense dark panels containing several text lines are surfaces, not ink.
    labels, _ = ndimage.label(pixels.max(axis=2) < 65)
    for label, slices in enumerate(ndimage.find_objects(labels), 1):
        if slices is None:
            continue
        ys,xs = slices
        pw,ph = xs.stop-xs.start, ys.stop-ys.start
        if not (200 <= pw < w*.8 and 70 <= ph < h*.6):
            continue
        panel = Node("rect", (xs.start,ys.start,pw,ph), color=_hex(np.median(pixels[ys,xs].reshape(-1,3),axis=0)))
        if (labels[ys,xs] == label).mean() > .7 and sum(_contains(panel,t,2) for t in texts) >= 3:
            structural.append(panel)
    root.children = ([decorative] if decorative else []) + structural + candidates + marks + controls + pictures
    for text in texts:
        if any(_contains(button,text,2) for button in disabled):
            text.vector_data["role"]="disabled-text"
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
    return simplify_scene_artwork(root, image, allow_raster=options.raster_fallback,
        smooth=options.smooth_icons, pixel_boundaries=options.pixel_boundary_icons,
        estimate_alpha=options.estimate_alpha,
        palette_size=options.icon_palette_size, blur=options.icon_blur,
        cache_dir=options.icon_cache_dir if options.icon_cache_dir != '' else False,
        source_image=source_image)


def _iou_boxes(a,b):
    x,y,w,h=a; p,q,r,s=b
    overlap=max(0,min(x+w,p+r)-max(x,p))*max(0,min(y+h,q+s)-max(y,q))
    return overlap/(w*h+r*s-overlap) if overlap else 0.0
