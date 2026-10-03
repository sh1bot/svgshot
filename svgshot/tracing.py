"""Trace a cropped image over a known background, with editable SVG cache files.

The SQLite index links every exact source hash to a canonical group. Each
group retains all observed PNGs, plus one independently editable SVG per
algorithm and settings. Offsets are stored as floats for later registration
work; the current bitmap matcher only tests integer translations.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from html import escape

import numpy as np
from PIL import Image

from .model import Node
from .tracing_core import simplify_icon

SVG_NS = 'http://www.w3.org/2000/svg'
ET.register_namespace('', SVG_NS)
VERSION = 1


def default_cache_dir() -> Path:
    override = os.environ.get('SVGSHOT_ICON_CACHE')
    if override:
        return Path(override).expanduser()
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData'/'Local'))/'svgshot'/'traces'
    if sys.platform == 'darwin':
        return Path.home()/'Library'/'Caches'/'svgshot'/'traces'
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home()/'.cache'))/'svgshot'/'traces'


def _background(value):
    if isinstance(value, str):
        value = value.lstrip('#')
        if len(value) != 6 or not re.fullmatch('[0-9a-fA-F]{6}', value):
            raise ValueError('background must be an RGB colour')
        return tuple(bytes.fromhex(value))
    values = tuple(int(v) for v in value)
    if len(values) != 3 or any(not 0 <= v <= 255 for v in values):
        raise ValueError('background must contain three RGB channels')
    return values


def _hash(image, background):
    digest = hashlib.sha256()
    digest.update(bytes(background))
    digest.update(image.width.to_bytes(4, 'big'))
    digest.update(image.height.to_bytes(4, 'big'))
    digest.update(image.convert('RGBA').tobytes())
    return digest.hexdigest()


def _atomic_bytes(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _filename(algorithm, palette_size, blur):
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,50}', algorithm):
        raise ValueError('algorithm names must use letters, digits, hyphens or underscores')
    settings = json.dumps([VERSION, algorithm, palette_size,
                           blur if algorithm == 'smooth-palette' else None], separators=(',', ':'))
    return f'{algorithm}-{hashlib.sha256(settings.encode()).hexdigest()[:16]}'


def _make_svg(node):
    data = node.vector_data
    w, h = data['icon_size']
    parts = [f'<svg xmlns="{SVG_NS}" width="{w}" height="{h}" viewBox="0 0 {w} {h}">']
    if data.get('clip_to_first'):
        outline = data['paths'][0]['d']
        parts.append(f'<defs><clipPath id="silhouette"><path d="{outline}" '
                     'clip-rule="evenodd"/></clipPath></defs>')
    if data.get('icon_opacity', 1) != 1:
        parts.append(f'<g opacity="{data["icon_opacity"]}">')
    if data.get('clip_to_first'):
        parts.append('<g clip-path="url(#silhouette)">')
    for path in data['paths']:
        opacity = f' fill-opacity="{path["opacity"]}"' if 'opacity' in path else ''
        parts.append(f'<path data-kind="capture-artwork" d="{path["d"]}" fill="{path["fill"]}" '
                     f'fill-rule="evenodd"{opacity}/>')
    if data.get('clip_to_first'):
        parts.append('</g>')
    if data.get('icon_opacity', 1) != 1:
        parts.append('</g>')
    return ''.join(parts)+'</svg>\n'


def _read_svg(svg, group, hints=None):
    root = ET.fromstring(svg)
    if root.tag != f'{{{SVG_NS}}}svg':
        raise ValueError('Cached trace must be a standalone SVG')
    view_box = root.get('viewBox', '').replace(',', ' ').split()
    if len(view_box) != 4 or any(not np.isfinite(float(v)) for v in view_box):
        raise ValueError('Cached trace needs a finite viewBox')
    w, h = (float(view_box[2]), float(view_box[3]))
    if min(w, h) <= 0:
        raise ValueError('Cached trace needs a positive viewBox')
    paths = []
    hints = hints or {}
    def visit(element):
        if element.tag in (f'{{{SVG_NS}}}defs', f'{{{SVG_NS}}}clipPath'):
            return
        if element.tag == f'{{{SVG_NS}}}path':
            paths.append({'d':element.get('d',''), 'fill':element.get('fill','#000000'),
                          **({'opacity':float(element.get('fill-opacity'))}
                             if element.get('fill-opacity') else {})})
        for child in element:
            visit(child)
    visit(root)
    return {**hints,'local':True, 'icon_size':[w,h], 'paths':paths,
            'approximation':hints.get('approximation','cached-trace'),
            'cache_group':group, 'standalone_svg':svg}


def _bitmap_match(query, known, background):
    """Strict full-colour agreement, allowing at most one integer-pixel shift."""
    if abs(query.width-known.width)>1 or abs(query.height-known.height)>1:
        return None
    if query.width*query.height < 9:
        return None
    q = np.asarray(query.convert('RGBA'),dtype=np.float32)
    k = np.asarray(known.convert('RGBA'),dtype=np.float32)
    bg = np.asarray(background,dtype=np.float32)
    def flatten(p):
        alpha = p[:,:,3:]/255
        return p[:,:,:3]*alpha+bg*(1-alpha)
    q, k = flatten(q), flatten(k)
    best = None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            y0, x0 = max(0,dy),max(0,dx)
            ky, kx = max(0,-dy),max(0,-dx)
            h = min(q.shape[0]-y0,k.shape[0]-ky)
            w = min(q.shape[1]-x0,k.shape[1]-kx)
            if h < min(q.shape[0],k.shape[0])-1 or w < min(q.shape[1],k.shape[1])-1:
                continue
            a,b = q[y0:y0+h,x0:x0+w],k[ky:ky+h,kx:kx+w]
            error = np.abs(a-b).mean(axis=2)
            foreground_a = np.linalg.norm(a-bg,axis=2)>40
            foreground_b = np.linalg.norm(b-bg,axis=2)>40
            union = (foreground_a|foreground_b).sum()
            overlap = (foreground_a&foreground_b).sum()/max(1,union)
            if overlap < .97 or float(error.mean()) > 3 or float(np.percentile(error,95)) > 12:
                continue
            score = (float(error.mean()), abs(dx)+abs(dy))
            if best is None or score < best[0]:
                best = (score,(float(dx),float(dy)))
    return best


def _svg_match(first, second):
    """Compare generated geometry and colour; preserve edits in the stored SVG."""
    try:
        a, b = ET.fromstring(first), ET.fromstring(second)
        if a.get('viewBox') != b.get('viewBox'):
            return False
        alpha_a = [float(g.get('opacity','1')) for g in a.iter(f'{{{SVG_NS}}}g')]
        alpha_b = [float(g.get('opacity','1')) for g in b.iter(f'{{{SVG_NS}}}g')]
        if len(alpha_a)!=len(alpha_b) or any(abs(x-y)>.02 for x,y in zip(alpha_a,alpha_b)):
            return False
        shapes = {f'{{{SVG_NS}}}{name}' for name in ('path','rect','circle','ellipse',
                                                    'polygon','polyline','line','text','image')}
        def visible_shapes(root):
            result=[]
            def visit(element):
                if element.tag in (f'{{{SVG_NS}}}defs',f'{{{SVG_NS}}}clipPath'):
                    return
                if element.tag in shapes:
                    result.append(element.tag)
                for child in element:
                    visit(child)
            visit(root)
            return result
        if visible_shapes(a)!=visible_shapes(b):
            return False
        clips_a = [p.get('d') for clip in a.iter(f'{{{SVG_NS}}}clipPath')
                   for p in clip.iter(f'{{{SVG_NS}}}path')]
        clips_b = [p.get('d') for clip in b.iter(f'{{{SVG_NS}}}clipPath')
                   for p in clip.iter(f'{{{SVG_NS}}}path')]
        if clips_a!=clips_b:
            return False
        a = [p for p in a.iter(f'{{{SVG_NS}}}path') if p.get('data-kind') == 'capture-artwork']
        b = [p for p in b.iter(f'{{{SVG_NS}}}path') if p.get('data-kind') == 'capture-artwork']
        if not a or len(a) != len(b):
            return False
        for x,y in zip(a,b):
            # Same topology and near-identical control points/colours.
            xs = re.findall(r'[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?',x.get('d',''))
            ys = re.findall(r'[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?',y.get('d',''))
            if len(xs)!=len(ys):
                return False
            for p,q in zip(xs,ys):
                if p.isalpha() or q.isalpha():
                    if p!=q:return False
                elif abs(float(p)-float(q))>.25:
                    return False
            fills = (x.get('fill','#000000'),y.get('fill','#000000'))
            if any(not re.fullmatch(r'#[0-9a-fA-F]{6}',v) for v in fills):
                return False
            if max(abs(c-d) for c,d in zip(*(bytes.fromhex(v[1:]) for v in fills)))>5:
                return False
            if abs(float(x.get('fill-opacity','1'))-float(y.get('fill-opacity','1')))>.02:
                return False
        return True
    except (ValueError,TypeError,ET.ParseError):
        return False


@dataclass
class TraceResult:
    node: Node | None
    svg: str | None
    svg_path: Path | None
    review_path: Path | None
    source_hash: str
    group_id: str
    offset: tuple[float,float]
    match: str


class TraceCache:
    def __init__(self, directory=None):
        self.root = Path(directory) if directory is not None else default_cache_dir()
        self.root.mkdir(parents=True,exist_ok=True)
        self.db = sqlite3.connect(self.root/'index.sqlite3',timeout=30)
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS groups (
              id TEXT PRIMARY KEY, background TEXT NOT NULL, canonical_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bitmaps (
              hash TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id),
              background TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
              dx REAL NOT NULL DEFAULT 0, dy REAL NOT NULL DEFAULT 0,
              match_kind TEXT NOT NULL DEFAULT 'unrecorded');
            CREATE TABLE IF NOT EXISTS traces (
              group_id TEXT NOT NULL REFERENCES groups(id), algorithm TEXT NOT NULL,
              status TEXT NOT NULL, hints TEXT NOT NULL DEFAULT '{}',
              generated_hash TEXT,
              PRIMARY KEY (group_id, algorithm));
            CREATE TABLE IF NOT EXISTS names (
              hash TEXT NOT NULL REFERENCES bitmaps(hash), name TEXT NOT NULL,
              PRIMARY KEY (hash, name));
            CREATE INDEX IF NOT EXISTS bitmap_dimensions ON bitmaps(width,height,background);
        ''')
        if 'hints' not in [row[1] for row in self.db.execute('PRAGMA table_info(traces)')]:
            self.db.execute("ALTER TABLE traces ADD COLUMN hints TEXT NOT NULL DEFAULT '{}'")
        if 'generated_hash' not in [row[1] for row in self.db.execute('PRAGMA table_info(traces)')]:
            self.db.execute('ALTER TABLE traces ADD COLUMN generated_hash TEXT')
        if 'match_kind' not in [row[1] for row in self.db.execute('PRAGMA table_info(bitmaps)')]:
            self.db.execute("ALTER TABLE bitmaps ADD COLUMN match_kind TEXT NOT NULL DEFAULT 'unrecorded'")
        review_marker = self.root/'.review-pages-v2'
        if not review_marker.is_file():
            for group, in self.db.execute('SELECT DISTINCT group_id FROM traces WHERE status=?',
                                          ('vector',)).fetchall():
                self._refresh_review_pages(group)
            _atomic_bytes(review_marker,b'1\n')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _bitmap_path(self, group, digest):
        return self.root/'groups'/group/'bitmaps'/f'{digest}.png'

    def _bitmap(self, digest):
        group, = self.db.execute('SELECT group_id FROM bitmaps WHERE hash=?',(digest,)).fetchone()
        with Image.open(self._bitmap_path(group,digest)) as image:
            return image.copy()

    def _svg_path(self, group, algorithm):
        return self.root/'groups'/group/'algorithms'/f'{algorithm}.svg'

    def _refresh_review_pages(self, group):
        background, = self.db.execute('SELECT background FROM groups WHERE id=?',
                                      (group,)).fetchone()
        variants = self.db.execute('''SELECT hash,width,height,dx,dy,match_kind FROM bitmaps
            WHERE group_id=? ORDER BY CASE WHEN hash=? THEN 0 ELSE 1 END, hash''',
            (group,group)).fetchall()
        if not variants:
            return
        by_hash = {}
        for digest,name in self.db.execute('''SELECT n.hash,n.name FROM names n
            JOIN bitmaps b ON b.hash=n.hash WHERE b.group_id=? ORDER BY n.name''',(group,)):
            by_hash.setdefault(digest,[]).append(name)
        traces = self.db.execute('''SELECT algorithm,status,hints,generated_hash FROM traces
            WHERE group_id=? ORDER BY algorithm''',(group,)).fetchall()
        w,h = variants[0][1:3]
        observations = []
        for index,(digest,width,height,dx,dy,match_kind) in enumerate(variants,1):
            names = by_hash.get(digest,[])
            label = ', '.join(names) if names else 'Unlabelled bitmap'
            labels = ''.join(f'<li>{escape(name)}</li>' for name in names)
            observations.append(
                f'<article><h3>Bitmap {index}: {escape(label)}</h3>'
                f'<img class="source-preview" src="../bitmaps/{digest}.png" '
                f'width="{width*16}" height="{height*16}" '
                f'alt="Source bitmap: {escape(label,quote=True)}">'
                f'<dl><dt>Source SHA-256</dt><dd><code>{digest}</code></dd>'
                f'<dt>Size</dt><dd>{width} × {height} pixels</dd>'
                f'<dt>Match</dt><dd>{escape(match_kind)}</dd>'
                f'<dt>Offset</dt><dd>({dx:g}, {dy:g}) pixels</dd></dl>'
                f'{"<p>Accessible name(s):</p><ul>"+labels+"</ul>" if names else ""}'
                '</article>')
        diagnostics = (('approximation','Approximation'),('silhouette_iou','Silhouette IoU'),
                       ('colour_error','Colour error'),('palette_size','Palette colours'),
                       ('blur_radius','Blur radius'),('layering','Layering'),
                       ('icon_opacity','Group opacity'),('clip_to_first','Clipped to silhouette'))
        trace_cards = []
        for algorithm,status,stored_hints,generated_hash in traces:
            svg_path = self._svg_path(group,algorithm)
            hints = json.loads(stored_hints)
            pairs = [(label,hints[key]) for key,label in diagnostics if key in hints]
            pairs.extend((key.replace('_',' ').capitalize(),value)
                         for key,value in sorted(hints.items())
                         if key not in dict(diagnostics))
            details = ''.join(f'<dt>{escape(label)}</dt><dd>{escape(str(value))}</dd>'
                              for label,value in pairs)
            if status == 'vector' and svg_path.is_file():
                current_svg = svg_path.read_bytes()
                edited = bool(generated_hash and
                              hashlib.sha256(current_svg).hexdigest()!=generated_hash)
                body = (f'<img class="vector-preview" src="{escape(svg_path.name,quote=True)}" '
                        f'width="{w*16}" height="{h*16}" '
                        f'alt="SVG trace from {escape(algorithm,quote=True)}">'
                        f'<p><a href="{escape(svg_path.name,quote=True)}">Open SVG</a> · '
                        f'{len(current_svg)} bytes</p>'
                        + ('<p class="edited">SVG edited since tracing; initial quality metrics '
                           'may no longer describe it.</p>' if edited else '')
                        + (f'<dl>{details}</dl>' if details else ''))
            else:
                body = '<p>No SVG met the trace quality threshold.</p>' if status != 'vector' else \
                       '<p>The cached SVG file is missing.</p>'
            trace_cards.append((algorithm,body))
        for algorithm,status,_,_ in traces:
            if status!='vector':
                continue
            svg_path = self._svg_path(group,algorithm)
            if not svg_path.is_file():
                continue
            artwork = ''.join(
                f'<article{ " class=\"selected\"" if name==algorithm else ""}'
                f' data-algorithm="{escape(name,quote=True)}">'
                f'<h3>{escape(name)}</h3>{body}</article>' for name,body in trace_cards)
            markup = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                      f'<title>Icon comparison: {escape(algorithm)}</title>'
                      f'<style>:root{{--icon-background:{background}}}'
                      'body{font:16px system-ui,sans-serif;color:#181818;background:#fafafa;'
                      'margin:1.5rem}main{display:grid;grid-template-columns:minmax(0,1fr) '
                      'minmax(0,1fr);gap:1.5rem}section{min-width:0}article{overflow:auto;'
                      'margin-bottom:1.5rem;border:1px solid #aaa;padding:1rem;background:#fff}'
                      'article.selected{border:3px solid #2669ac}img{max-width:none;'
                      'background:var(--icon-background);border:1px solid #777}'
                      '.source-preview{image-rendering:pixelated}dt{font-weight:bold}'
                      'dd{margin:0 0 .35rem 0}dl{margin:.6rem 0;display:grid;'
                      'grid-template-columns:max-content minmax(0,1fr);column-gap:.7rem}'
                      'code{overflow-wrap:anywhere}.edited{font-weight:bold;color:#a33}'
                      '@media(max-width:700px){main{display:block}}'
                      '</style>'
                      f'<h1>Icon comparison</h1><p>Canonical group: <code>{group}</code>. '
                      f'Background: <code>{background}</code>. '
                      f'{len(variants)} source bitmap(s) and {len(traces)} trace result(s). '
                      'Images are shown at 16×; bitmap scaling uses nearest neighbour.</p>'
                      '<main><section><h2>Observed bitmaps</h2>'
                      + ''.join(observations) + '</section><section><h2>Algorithm results</h2>'
                      + artwork + '</section></main></html>\n')
            page = svg_path.with_suffix('.html')
            data = markup.encode('utf-8')
            if not page.is_file() or page.read_bytes()!=data:
                _atomic_bytes(page,data)

    def trace(self, image, background, *, algorithm='palette', palette_size=None, blur=.5,
              allow_raster=True, name=None):
        image = image.copy()
        if not image.width or not image.height:
            raise ValueError('The image to trace must be nonempty')
        background = _background(background)
        rgb = '#%02x%02x%02x'%background
        if algorithm not in ('palette','smooth-palette'):
            raise ValueError('Unknown tracing algorithm: '+algorithm)
        key = _filename(algorithm,palette_size,blur)
        digest = _hash(image,background)
        row = self.db.execute('SELECT group_id,dx,dy FROM bitmaps WHERE hash=?',(digest,)).fetchone()
        match = 'exact' if row else None
        if row:
            group,dx,dy = row
        else:
            group, dx, dy = digest, 0., 0.
            # Fuzzy matching is scoped to the same background and near-equal
            # dimensions. Each observed source image remains independently saved.
            candidates = self.db.execute('''SELECT hash,group_id,dx,dy FROM bitmaps
                WHERE background=? AND abs(width-?)<=1 AND abs(height-?)<=1''',
                (rgb,image.width,image.height))
            best = None
            for known_hash, known_group, known_dx, known_dy in candidates:
                similarity = _bitmap_match(image,self._bitmap(known_hash),background)
                if similarity and (best is None or similarity[0] < best[0]):
                    best = (similarity[0],known_group,known_dx+similarity[1][0],
                            known_dy+similarity[1][1])
            if best:
                _,group,dx,dy = best
                match = 'bitmap'
        existing = self.db.execute('SELECT status,hints FROM traces WHERE group_id=? AND algorithm=?',
                                   (group,key)).fetchone() if match else None
        if existing and existing[0]=='vector' and not self._svg_path(group,key).is_file():
            existing = None  # An SVG removed for retracing is regenerated.
        candidate_svg = None
        if not existing:
            # Another algorithm always traces the canonical first sighting.
            sample = self._bitmap(group) if match and group != digest else image
            bg = background if group == digest else _background(self.db.execute(
                'SELECT background FROM groups WHERE id=?',(group,)).fetchone()[0])
            node = simplify_icon(sample,(0,0,sample.width,sample.height),
                                 allow_raster=True,smooth=algorithm=='smooth-palette',
                                 palette_size=palette_size,blur=blur,background=bg)
            candidate_svg = _make_svg(node) if node is not None and node.kind=='capture-artwork' else None
            if not match and candidate_svg:
                for known_group, in self.db.execute('''SELECT t.group_id FROM traces t
                    JOIN groups g ON g.id=t.group_id
                    WHERE t.algorithm=? AND t.status=? AND g.background=?''',
                    (key,'vector',rgb)):
                    stored = self._svg_path(known_group,key).read_text(encoding='utf-8')
                    if _svg_match(candidate_svg,stored):
                        group,dx,dy,match = known_group,0.,0.,'svg'
                        break
                existing = self.db.execute('SELECT status,hints FROM traces WHERE group_id=? AND algorithm=?',
                                           (group,key)).fetchone() if match else None
        if not row:
            output = io.BytesIO()
            image.save(output,format='PNG',optimize=True)
            _atomic_bytes(self._bitmap_path(group,digest),output.getvalue())
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO groups VALUES (?,?,?)',(group,rgb,group))
                self.db.execute('''INSERT OR IGNORE INTO bitmaps
                    (hash,group_id,background,width,height,dx,dy,match_kind)
                    VALUES (?,?,?,?,?,?,?,?)''',
                    (digest,group,rgb,image.width,image.height,dx,dy,match or 'new'))
        if isinstance(name,str):
            name = ' '.join(name.split())
            if name:
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO names VALUES (?,?)',(digest,name))
        if not existing:
            status = 'vector' if candidate_svg else 'raster' if node is not None else 'empty'
            hints = {k:v for k,v in node.vector_data.items() if k not in
                     ('paths','local','icon_size')} if candidate_svg else {}
            if candidate_svg:
                _atomic_bytes(self._svg_path(group,key),candidate_svg.encode('utf-8'))
            with self.db:
                self.db.execute('''INSERT INTO traces
                    (group_id,algorithm,status,hints,generated_hash)
                    VALUES (?,?,?,?,?) ON CONFLICT(group_id,algorithm) DO UPDATE SET
                    status=excluded.status,hints=excluded.hints,
                    generated_hash=excluded.generated_hash''',
                    (group,key,status,json.dumps(hints),
                     hashlib.sha256(candidate_svg.encode('utf-8')).hexdigest()
                     if candidate_svg else None))
        else:
            status,hints = existing[0],json.loads(existing[1])
        svg = self._svg_path(group,key).read_text(encoding='utf-8') if status=='vector' else None
        if svg:
            data = _read_svg(svg,group,hints)
            data['trace_offset'] = [dx,dy]
            result = Node('capture-artwork',(0,0,image.width,image.height),vector_data=data)
        elif status=='raster' and allow_raster:
            import base64
            output=io.BytesIO();image.save(output,format='PNG',optimize=True)
            result=Node('raster',(0,0,image.width,image.height),
                        image_data=base64.b64encode(output.getvalue()).decode('ascii'),
                        vector_data={'icon_fallback':True})
        else:
            result=None
        self._refresh_review_pages(group)
        path = self._svg_path(group,key) if svg else None
        return TraceResult(result,svg,path,path.with_suffix('.html') if path else None,
                           digest,group,(dx,dy),match or 'new')


def trace(image, background, *, cache_dir=None, algorithm='palette', palette_size=None,
          blur=.5, allow_raster=True, name=None):
    """Trace an image over RGB background, returning an inline node and SVG.

    The SVG on disk is the source of truth on every cache hit. ``cache_dir``
    defaults to the platform's user cache; use a directory of your choice to
    inspect or hand edit the bitmaps and per-algorithm SVGs.
    """
    with TraceCache(cache_dir) as cache:
        return cache.trace(image,background,algorithm=algorithm,
                           palette_size=palette_size,blur=blur,allow_raster=allow_raster,name=name)


def main(argv=None):
    """Trace a standalone icon PNG and print the editable cached SVG path."""
    import argparse
    parser=argparse.ArgumentParser(description='Trace a cropped icon into the shared SVG cache')
    parser.add_argument('image',type=Path,help='Cropped source PNG')
    parser.add_argument('background',help='Background colour, e.g. #ffffff')
    parser.add_argument('--cache-dir',type=Path)
    parser.add_argument('--algorithm',choices=('palette','smooth-palette'),default='palette')
    parser.add_argument('--palette-size',type=int)
    parser.add_argument('--blur',type=float,default=.5)
    parser.add_argument('--name',help='Optional human label for this source image')
    args=parser.parse_args(argv)
    with Image.open(args.image) as image:
        result=trace(image,args.background,cache_dir=args.cache_dir,
                     algorithm=args.algorithm,palette_size=args.palette_size,
                     blur=args.blur,name=args.name)
    if result.svg_path:
        print(result.svg_path)
        return 0
    parser.exit(1,'No vector trace met the quality threshold; bitmap retained in cache.\n')


if __name__=='__main__':
    raise SystemExit(main())
