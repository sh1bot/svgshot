"""Compact icon approximation shared by semantic tracing and raster fallback.

Filled contours work for both glyphs and colour artwork. Simplification is
accepted only after a supersampled render checks silhouette and colour error;
PNG remains the fallback for textures or shapes outside that error budget.
Photographs and explicit fidelity overlays do not enter this path.

Developer metrics: accept silhouette IoU >= .82 and mean premultiplied RGB/
alpha error <= .16 over foreground pixels, with at most 500 vertices. Compare
at the original icon size after 4x supersampling; transparent RGB is ignored.
These bounds preserve recognisability rather than reproducing every pixel.
"""
import base64
import io

import numpy as np
from PIL import Image, ImageDraw

from .model import Node


def _contours(mask):
    h, w = mask.shape
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
    contours = []
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
        if current == start and len(points) >= 4:
            contours.append(np.asarray(points, dtype=float))
    return contours


def _rdp(points, tolerance):
    if len(points) <= 2:
        return points
    delta = points[-1]-points[0]
    length = float(np.dot(delta,delta))
    t = np.clip((points-points[0]) @ delta / max(length,1e-9),0,1)
    distance = np.linalg.norm(points-(points[0]+t[:,None]*delta),axis=1)
    index = int(distance.argmax())
    if distance[index] <= tolerance:
        return points[[0,-1]]
    return np.concatenate((_rdp(points[:index+1],tolerance)[:-1],_rdp(points[index:],tolerance)))


def _simplify(points, tolerance):
    # Split a closed contour at distant vertices; a closed zero-length baseline
    # would otherwise collapse a loop or create an arbitrary corner.
    pivot = int(np.linalg.norm(points-points[0],axis=1).argmax())
    first = _rdp(points[:pivot+1],tolerance)
    last = _rdp(np.concatenate((points[pivot:],points[:1])),tolerance)
    return np.concatenate((first[:-1],last[:-1]))


def _render(layers, size):
    w,h = size
    output = Image.new('RGBA',(w*4,h*4))
    for color, contours in layers:
        mask = np.zeros((h*4,w*4),dtype=bool)
        for points in contours:
            part = Image.new('1',(w*4,h*4))
            ImageDraw.Draw(part).polygon([(round(x*4),round(y*4)) for x,y in points],fill=1)
            mask ^= np.asarray(part,dtype=bool)  # even-odd holes remain transparent
        layer = Image.new('RGBA',output.size,tuple(color))
        layer.putalpha(Image.fromarray(mask.astype('uint8')*int(color[3])))
        output.alpha_composite(layer)
    return output.resize(size,Image.Resampling.LANCZOS)


def _quality(target, rendered):
    a,b = np.asarray(target).astype(float)/255,np.asarray(rendered).astype(float)/255
    am = a[:,:,3] > max(.03,float(a[:,:,3].max())*.35)
    bm = b[:,:,3] > max(.03,float(b[:,:,3].max())*.35)
    union = am|bm
    if not union.any():
        return 0.,1.
    iou = (am&bm).sum()/union.sum()
    # Premultiplied colour ignores RGB in fully transparent pixels.
    error = np.abs(a[:,:,:3]*a[:,:,3:] - b[:,:,:3]*b[:,:,3:]).mean(axis=2)
    alpha_error = np.abs(a[:,:,3]-b[:,:,3])
    return float(iou),float((error[union].mean()+alpha_error[union].mean())/2)


def _target(image, box, *, force_colour=False):
    x,y,w,h = box
    patch = image.crop((x,y,x+w,y+h)).convert('RGBA')
    pixels = np.asarray(patch).astype(float)
    rgb, alpha = pixels[:,:,:3],pixels[:,:,3]/255
    has_alpha = bool((alpha < .99).any())
    if not has_alpha:
        # A screenshot is already composited. Its surrounding pixels usually
        # provide a better background estimate than a tightly cropped icon edge.
        pad = np.asarray(image.crop((max(0,x-2),max(0,y-2),min(image.width,x+w+2),min(image.height,y+h+2))).convert('RGB'))
        border = np.concatenate((pad[0],pad[-1],pad[:,0],pad[:,-1]))
        background = np.median(border,axis=0)
        distance = np.linalg.norm(rgb-background,axis=2)
        foreground = distance > 40
    else:
        foreground = alpha > max(.03,float(alpha.max())*.35)
        background = np.zeros(3)
        distance = np.linalg.norm(rgb-background,axis=2)
    if foreground.sum() < 3:
        return None,False
    strong = foreground & (alpha > .8) if has_alpha else foreground & (distance >= np.percentile(distance[foreground],65))
    color = np.median(rgb[strong if strong.any() else foreground],axis=0)
    if has_alpha:
        monochrome = np.percentile(np.linalg.norm(rgb[foreground]-color,axis=1),90) < 30
    else:
        direction = color-background
        coverage = np.clip((rgb-background) @ direction/max(1,np.dot(direction,direction)),0,1)
        residual = np.linalg.norm(rgb-(background+coverage[:,:,None]*direction),axis=2)
        monochrome = np.percentile(residual[foreground],90) < 24
        if monochrome:
            alpha = coverage
    monochrome = monochrome and not force_colour
    if monochrome:
        rgb = np.broadcast_to(color,rgb.shape).copy()
    elif not has_alpha:
        alpha = foreground.astype(float)
    rgba = np.concatenate((rgb,alpha[:,:,None]*255),axis=2).clip(0,255).astype('uint8')
    return Image.fromarray(rgba),monochrome


def _vector_candidate(target, monochrome, box):
    x,y,w,h = box
    pixels = np.asarray(target)
    visible = pixels[:,:,3] > max(8,float(pixels[:,:,3].max())*.35)
    best = None
    for count in ((1,) if monochrome else (4,8,12)):
        # Quantize foreground samples only: transparent background colours must
        # neither consume the palette nor bleed into visible icon edges.
        sample = Image.fromarray(pixels[:,:,:3][visible].reshape(1,-1,3))
        quantized = sample.quantize(colors=count,method=Image.Quantize.MEDIANCUT,dither=Image.Dither.NONE)
        palette = np.asarray(quantized.convert('RGB')).reshape(-1,3)
        colors = np.unique(palette,axis=0)
        distances = ((pixels[:,:,:3].astype(float)[:,:,None,:]-colors[None,None,:,:])**2).sum(axis=3)
        indices = distances.argmin(axis=2)
        raw = [(tuple(int(v) for v in color)+(int(np.percentile(pixels[:,:,3][visible & (indices==i)],90)),),
                _contours(visible & (indices==i))) for i,color in enumerate(colors)
               if (visible & (indices==i)).any()]
        for tolerance in (1.,.65,.35):
            layers = [(color,[_simplify(p,tolerance) for p in paths]) for color,paths in raw]
            layers = [(color,[p for p in paths if len(p)>=3]) for color,paths in layers]
            rendered = _render(layers,(w,h))
            iou,error = _quality(target,rendered)
            vertices = sum(len(p) for _,paths in layers for p in paths)
            if iou < .82 or error > .16 or vertices > 500:
                continue
            paths = []
            for color,contours in layers:
                d = ' '.join('M'+' '.join(f'{a:g},{b:g}' for a,b in contour)+'Z' for contour in contours)
                if d:
                    paths.append({'d':d,'fill':'#%02x%02x%02x'%color[:3],
                                  **({'opacity':round(color[3]/255,3)} if color[3] != 255 else {})})
            size = sum(len(p['d']) for p in paths)
            if best is None or size < best[0]:
                best = (size,Node('capture-artwork',box,vector_data={'paths':paths,'local':True,
                    'icon_size':[w,h],'approximation':'monochrome' if monochrome else 'colour',
                    'silhouette_iou':round(iou,3),'colour_error':round(error,3)}))
    return best


def simplify_icon(image, box, *, allow_raster=True):
    """Prefer a bounded vector approximation; retain PNG when it loses detail."""
    x,y,w,h = box
    target,monochrome = _target(image,box)
    if target is None:
        return None
    best = _vector_candidate(target,monochrome,box)
    if best is None and monochrome:
        # A shaded colour icon can lie close to one colour axis. Failure of a
        # glyph approximation is a reason to try colour, not immediately PNG.
        target,_ = _target(image,box,force_colour=True)
        best = _vector_candidate(target,False,box)
    if best:
        return best[1]
    if not allow_raster:
        return None
    buffer = io.BytesIO()
    # Preserve the source crop when approximation failed; do not invent alpha
    # around textures merely because their edge colour happened to be common.
    image.crop((x,y,x+w,y+h)).save(buffer,format='PNG',optimize=True)
    return Node('raster',box,image_data=base64.b64encode(buffer.getvalue()).decode('ascii'),
                vector_data={'icon_fallback':True})


def simplify_scene_artwork(scene, image, *, allow_raster=True):
    """Both recognition routes converge here, after control/text heuristics."""
    def visit(parent):
        retained = []
        for node in parent.children:
            visit(node)
            if (node.children or node.kind not in ('raster','capture-artwork')
                    or node.vector_data.get('opaque') or node.vector_data.get('approximation')
                    or node.vector_data.get('icon_fallback') or max(node.box[2:]) > 96):
                retained.append(node)
                continue
            replacement = simplify_icon(image,node.box,allow_raster=allow_raster)
            if replacement is not None:
                replacement.confidence = node.confidence
                retained.append(replacement)
            elif node.kind != 'raster' or allow_raster:
                retained.append(node)
        parent.children = retained
    visit(scene)
    return scene
