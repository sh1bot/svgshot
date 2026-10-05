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
VERSION = 2


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


def _fingerprint(image, background):
    """Three mean composited channels; a cheap, translation-tolerant filter."""
    pixels = np.asarray(image.convert('RGBA'),dtype=np.float32)
    alpha = pixels[:,:,3:]/255
    rgb = pixels[:,:,:3]*alpha+np.asarray(background,dtype=np.float32)*(1-alpha)
    return bytes(np.rint(rgb.mean(axis=(0,1))).astype(np.uint8))


def _fingerprint_might_match(query, known, query_size, known_size):
    """Reject only means incompatible with the pixel matcher's error budget.

    A one-pixel translation and a one-pixel size difference can each discard
    up to two rows/columns from the compared region. Account for those border
    pixels at maximum contrast so this filter cannot discard a valid match.
    """
    qw,qh = query_size
    kw,kh = known_size
    q_border = (1+max(0,qw-kw))/qw+(1+max(0,qh-kh))/qh
    k_border = (1+max(0,kw-qw))/kw+(1+max(0,kh-qh))/kh
    # Mean RGB error <= 3 implies any one channel differs by at most 9.
    # Rounding each mean to a byte contributes at most one more level.
    limit = 10+255*(q_border+k_border)
    return all(abs(a-b)<=limit for a,b in zip(query,known))


def _sample_fingerprint(image, background):
    """Store actual composited pixels on an even grid for a cheap match bound."""
    pixels = np.asarray(image.convert('RGBA'),dtype=np.float32)
    alpha = pixels[::2,::2,3:]/255
    rgb = pixels[::2,::2,:3]*alpha+np.asarray(background,dtype=np.float32)*(1-alpha)
    return np.rint(rgb).astype(np.uint8).tobytes()


def _sample_might_match(query, known, query_size, known_size):
    """Reject if sampled disagreements alone exceed the full match's 95th percentile."""
    width,height = known_size
    expected = ((height+1)//2)*((width+1)//2)*3
    if len(known)!=expected:
        return True
    sample = np.frombuffer(known,dtype=np.uint8).reshape((height+1)//2,(width+1)//2,3)
    for dy in (-1,0,1):
        for dx in (-1,0,1):
            y0,x0=max(0,dy),max(0,dx)
            ky,kx=max(0,-dy),max(0,-dx)
            h=min(query_size[1]-y0,height-ky)
            w=min(query_size[0]-x0,width-kx)
            if h < min(query_size[1],height)-1 or w < min(query_size[0],width)-1:
                continue
            limit=.05*h*w+2
            coarse_y=np.arange(ky+(-ky)%4,ky+h,4)
            coarse_x=np.arange(kx+(-kx)%4,kx+w,4)
            if len(coarse_y)*len(coarse_x)>limit:
                observed=query[np.ix_(coarse_y+dy,coarse_x+dx)]
                reference=sample[np.ix_(coarse_y//2,coarse_x//2)].astype(np.float32)
                if np.count_nonzero(np.abs(observed-reference).mean(axis=2)>13)>limit:
                    continue
            ys=np.arange(ky+(ky%2),ky+h,2)
            xs=np.arange(kx+(kx%2),kx+w,2)
            if not len(ys) or not len(xs):
                continue
            observed=query[np.ix_(ys+dy,xs+dx)]
            reference=sample[np.ix_(ys//2,xs//2)].astype(np.float32)
            # Stored RGB rounds by at most half a level; 13 is a conservative
            # threshold for the precise comparator's 12-level p95 limit.
            errors=np.abs(observed-reference).mean(axis=2)
            if np.count_nonzero(errors>13) <= limit:
                return True
    return False


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


def _filename(algorithm, palette_size, blur, *, version=VERSION,
              estimate_alpha=False):
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,50}', algorithm):
        raise ValueError('algorithm names must use letters, digits, hyphens or underscores')
    settings = [version,algorithm,palette_size,
                blur if algorithm == 'smooth-palette' else None]
    if not estimate_alpha:
        settings.append('no-estimated-alpha')
    settings = json.dumps(settings,separators=(',', ':'))
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
              match_kind TEXT NOT NULL DEFAULT 'unrecorded', fingerprint BLOB,
              sample BLOB);
            CREATE TABLE IF NOT EXISTS traces (
              group_id TEXT NOT NULL REFERENCES groups(id), algorithm TEXT NOT NULL,
              status TEXT NOT NULL, hints TEXT NOT NULL DEFAULT '{}',
              generated_hash TEXT,
              PRIMARY KEY (group_id, algorithm));
            CREATE TABLE IF NOT EXISTS names (
              hash TEXT NOT NULL REFERENCES bitmaps(hash), name TEXT NOT NULL,
              PRIMARY KEY (hash, name));
            CREATE INDEX IF NOT EXISTS bitmap_search ON bitmaps(background,width,height);
        ''')
        if 'hints' not in [row[1] for row in self.db.execute('PRAGMA table_info(traces)')]:
            self.db.execute("ALTER TABLE traces ADD COLUMN hints TEXT NOT NULL DEFAULT '{}'")
        if 'generated_hash' not in [row[1] for row in self.db.execute('PRAGMA table_info(traces)')]:
            self.db.execute('ALTER TABLE traces ADD COLUMN generated_hash TEXT')
        if 'match_kind' not in [row[1] for row in self.db.execute('PRAGMA table_info(bitmaps)')]:
            self.db.execute("ALTER TABLE bitmaps ADD COLUMN match_kind TEXT NOT NULL DEFAULT 'unrecorded'")
        if 'fingerprint' not in [row[1] for row in self.db.execute('PRAGMA table_info(bitmaps)')]:
            self.db.execute('ALTER TABLE bitmaps ADD COLUMN fingerprint BLOB')
        if 'sample' not in [row[1] for row in self.db.execute('PRAGMA table_info(bitmaps)')]:
            self.db.execute('ALTER TABLE bitmaps ADD COLUMN sample BLOB')
        self.db.execute('DROP INDEX IF EXISTS bitmap_dimensions')
        (self.root/'.review-pages-v5').unlink(missing_ok=True)
        (self.root/'.review-pages-v6').unlink(missing_ok=True)

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

    def _summary_path(self, group):
        return self.root/'summaries'/group

    def _review_path(self, group):
        return self._summary_path(group)/'review.html'

    def _preview_path(self, group, algorithm, stage):
        return self._summary_path(group)/f'{algorithm}.{stage}.png'

    def _summary_json(self, path, value):
        data=(json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8')
        if not path.is_file() or path.read_bytes()!=data:
            _atomic_bytes(path,data)

    def _sync_summary(self, group):
        """Materialize index facts beside the trace diagnostics for offline review."""
        directory=self._summary_path(group)
        background,=self.db.execute('SELECT background FROM groups WHERE id=?',
                                    (group,)).fetchone()
        self._summary_json(directory/'group.json',{'id':group,'background':background})
        names={}
        for digest,name in self.db.execute('''SELECT n.hash,n.name FROM names n
            JOIN bitmaps b ON b.hash=n.hash WHERE b.group_id=? ORDER BY n.name''',(group,)):
            names.setdefault(digest,[]).append(name)
        for digest,width,height,dx,dy,kind in self.db.execute('''
                SELECT hash,width,height,dx,dy,match_kind FROM bitmaps
                WHERE group_id=?''',(group,)):
            self._summary_json(directory/f'{digest}.source.json',
                {'hash':digest,'width':width,'height':height,'dx':dx,'dy':dy,
                 'match_kind':kind,'names':names.get(digest,[])})
        for algorithm,status,hints,generated_hash in self.db.execute('''
                SELECT algorithm,status,hints,generated_hash FROM traces
                WHERE group_id=?''',(group,)):
            self._summary_json(directory/f'{algorithm}.trace.json',
                {'algorithm':algorithm,'status':status,'hints':json.loads(hints),
                 'generated_hash':generated_hash})

    def _migrate_group_layout(self, group):
        """Move former SVG-adjacent diagnostics into a separate review tree."""
        directory = self.root/'groups'/group/'algorithms'
        (self.root/'groups'/group/'review.html').unlink(missing_ok=True)
        for algorithm, in self.db.execute('SELECT algorithm FROM traces WHERE group_id=?',
                                          (group,)).fetchall():
            old_paths=list(directory.glob(f'{algorithm}.*'))
            old_paths.extend((self.root/'groups'/group/'diagnostics'/algorithm).glob('*'))
            for old in old_paths:
                if old.name == f'{algorithm}.html':
                    old.unlink()
                    continue
                remainder = (old.name[len(algorithm)+1:] if old.parent==directory else old.name)
                if '.' not in remainder:
                    continue
                stage, suffix = remainder.rsplit('.',1)
                if ((stage in ('target','quantized','islands') or
                     re.fullmatch(r'layer-\d+',stage)) and suffix == 'png') or \
                        (stage == 'palette' and suffix == 'json'):
                    new = self._preview_path(group,algorithm,stage).with_suffix('.'+suffix)
                    new.parent.mkdir(parents=True,exist_ok=True)
                    if new.exists():
                        old.unlink()
                    else:
                        old.replace(new)
            previous=self.root/'groups'/group/'diagnostics'/algorithm
            if previous.is_dir() and not any(previous.iterdir()):
                previous.rmdir()
        old_diagnostics=self.root/'groups'/group/'diagnostics'
        if old_diagnostics.is_dir() and not any(old_diagnostics.iterdir()):
            old_diagnostics.rmdir()

    def _review_stale(self, group):
        page = self._review_path(group)
        if not page.is_file():
            return True
        updated = page.stat().st_mtime_ns
        for algorithm, in self.db.execute('SELECT algorithm FROM traces WHERE group_id=?',
                                          (group,)):
            svg = self._svg_path(group,algorithm)
            if svg.is_file() and svg.stat().st_mtime_ns > updated:
                return True
        return False

    def _write_previews(self, group, algorithm, previews):
        _atomic_bytes(self._preview_path(group,algorithm,'palette').with_suffix('.json'),
                      (json.dumps(previews['palette'],separators=(',',':'))+'\n').encode('utf-8'))
        for stage in ('target','quantized','islands'):
            buffer = io.BytesIO()
            previews[stage].save(buffer,format='PNG',optimize=True)
            _atomic_bytes(self._preview_path(group,algorithm,stage),buffer.getvalue())
        for index,layer in enumerate(previews['layers']):
            buffer = io.BytesIO()
            layer.save(buffer,format='PNG',optimize=True)
            _atomic_bytes(self._preview_path(group,algorithm,f'layer-{index:02d}'),buffer.getvalue())

    def _restore_previews(self, group):
        """Replay old traces only when the resulting original SVG is verified."""
        background, = self.db.execute('SELECT background FROM groups WHERE id=?',
                                      (group,)).fetchone()
        for algorithm,status,stored_hints,generated_hash in self.db.execute('''
                SELECT algorithm,status,hints,generated_hash FROM traces
                WHERE group_id=?''',(group,)).fetchall():
            svg_path = self._svg_path(group,algorithm)
            if status != 'vector' or not svg_path.is_file() or \
                    (self._preview_path(group,algorithm,'target').is_file() and
                     self._preview_path(group,algorithm,'palette').with_suffix('.json').is_file()):
                continue
            hints = json.loads(stored_hints)
            selected_palette = hints.get('palette_size')
            if selected_palette is not None and selected_palette < 2:
                selected_palette = None  # Monochrome's implicit one-colour palette.
            with Image.open(self._bitmap_path(group,group)) as source:
                image = source.copy()
            previews = {}
            node = simplify_icon(image,(0,0,*image.size),smooth=algorithm.startswith('smooth-palette-'),
                                 pixel_boundaries=algorithm.startswith('pixel-boundary-'),
                                 estimate_alpha=not hints.get('no_estimated_alpha',False),
                                 palette_size=selected_palette,
                                 blur=hints.get('blur_radius',.5),background=_background(background),
                                 previews=previews)
            if node is None or node.kind != 'capture-artwork':
                continue
            generated = _make_svg(node).encode('utf-8')
            original_hash = generated_hash or hashlib.sha256(svg_path.read_bytes()).hexdigest()
            if hashlib.sha256(generated).hexdigest() == original_hash:
                self._write_previews(group,algorithm,previews)

    def _refresh_review_pages(self, group):
        """Build the page from summary files; the database is not read here."""
        directory=self._summary_path(group)
        background=json.loads((directory/'group.json').read_text(encoding='utf-8'))['background']
        sources=[json.loads(path.read_text(encoding='utf-8'))
                 for path in directory.glob('*.source.json')]
        sources.sort(key=lambda item:(item['hash']!=group,item['hash']))
        variants=[(item['hash'],item['width'],item['height'],item['dx'],item['dy'],
                   item['match_kind']) for item in sources]
        if not variants:
            return
        by_hash={item['hash']:item['names'] for item in sources}
        records=[json.loads(path.read_text(encoding='utf-8'))
                 for path in directory.glob('*.trace.json')]
        records.sort(key=lambda item:item['algorithm'])
        traces=[(item['algorithm'],item['status'],item['hints'],item['generated_hash'])
                for item in records]
        w,h = variants[0][1:3]
        observations = []
        for index,(digest,width,height,dx,dy,match_kind) in enumerate(variants,1):
            names = by_hash.get(digest,[])
            label = ', '.join(names) if names else 'Unlabelled bitmap'
            labels = ''.join(f'<li>{escape(name)}</li>' for name in names)
            observations.append(
                f'<article><h3>Bitmap {index}: {escape(label)}</h3>'
                f'<img class="source-preview" src="../../groups/{group}/bitmaps/{digest}.png" '
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
        for algorithm,status,hints,generated_hash in traces:
            svg_path = self._svg_path(group,algorithm)
            pairs = [(label,hints[key]) for key,label in diagnostics if key in hints]
            pairs.extend((key.replace('_',' ').capitalize(),value)
                         for key,value in sorted(hints.items())
                         if key not in dict(diagnostics))
            details = ''.join(f'<dt>{escape(label)}</dt><dd>{escape(str(value))}</dd>'
                              for label,value in pairs)
            palette_file = self._preview_path(group,algorithm,'palette').with_suffix('.json')
            palette_html = ''
            if palette_file.is_file():
                swatches = json.loads(palette_file.read_text(encoding='utf-8'))
                palette_html = '<h4>Selected palette</h4><ul class="palette">' + ''.join(
                    f'<li><span class="swatch" style="background-color:{entry["rgb"]}"></span>'
                    f'<code>{entry["rgb"]}</code> · {entry["pixels"]} grid pixels</li>'
                    for entry in swatches if re.fullmatch(r'#[0-9a-fA-F]{6}',entry['rgb'])) + '</ul>'
            previews = []
            for stage,label in (('target','Foreground target'),
                                ('quantized','Quantized palette'),
                                ('islands','Visible palette islands')):
                path = self._preview_path(group,algorithm,stage)
                if path.is_file():
                    with Image.open(path) as preview:
                        native = preview.size
                    previews.append((path,label,native))
            index = 0
            while self._preview_path(group,algorithm,f'layer-{index:02d}').is_file():
                path = self._preview_path(group,algorithm,f'layer-{index:02d}')
                with Image.open(path) as preview:
                    native = preview.size
                label = ('Background reference (not exported)' if
                         index == 0 and hints.get('no_estimated_alpha') else
                         f'Layer {index+1} contour mask')
                previews.append((path,label,native))
                index += 1
            preview_html = ''.join(
                f'<figure><figcaption>{escape(label)} · {native[0]} × {native[1]} pixels'
                f'</figcaption><a href="{escape(path.name,quote=True)}">'
                f'<img class="stage-preview" src="{escape(path.name,quote=True)}" '
                f'width="{w*16}" height="{h*16}" alt="{escape(label,quote=True)} from '
                f'{escape(algorithm,quote=True)}"></a></figure>'
                for path,label,native in previews)
            if status == 'vector' and svg_path.is_file():
                current_svg = svg_path.read_bytes()
                edited = bool(generated_hash and
                              hashlib.sha256(current_svg).hexdigest()!=generated_hash)
                body = palette_html + '<div class="stages">' + (preview_html or
                        '<p>Intermediate images unavailable for this older trace.</p>') + (
                        f'<figure><figcaption>SVG trace</figcaption>'
                        f'<img class="vector-preview" src="../../groups/{group}/algorithms/{escape(svg_path.name,quote=True)}" '
                        f'width="{w*16}" height="{h*16}" '
                        f'alt="SVG trace from {escape(algorithm,quote=True)}"></figure></div>'
                        f'<p><a href="../../groups/{group}/algorithms/{escape(svg_path.name,quote=True)}">Open SVG</a> · '
                        f'{len(current_svg)} bytes</p>'
                        + ('<p class="edited">SVG edited since tracing; initial quality metrics '
                           'may no longer describe it.</p>' if edited else '')
                        + (f'<dl>{details}</dl>' if details else ''))
            else:
                body = '<p>No SVG met the trace quality threshold.</p>' if status != 'vector' else \
                       '<p>The cached SVG file is missing.</p>'
            trace_cards.append((algorithm,body))
        artwork = ''.join(
            f'<article data-algorithm="{escape(name,quote=True)}">'
            f'<h3>{escape(name)}</h3>{body}</article>' for name,body in trace_cards)
        markup = ('<!doctype html><html lang="en"><meta charset="utf-8">'
                  f'<title>Icon comparison: {group}</title>'
                  f'<style>:root{{--icon-background:{background}}}'
                  'body{font:16px system-ui,sans-serif;color:#181818;background:#fafafa;'
                  'margin:1.5rem}main{display:grid;grid-template-columns:minmax(0,1fr) '
                  'minmax(0,1fr);gap:1.5rem}section{min-width:0}article{overflow:auto;'
                  'margin-bottom:1.5rem;border:1px solid #aaa;padding:1rem;background:#fff}'
                  'img{max-width:none;'
                  'background-color:#f2f2f2;'
                  'background-image:repeating-conic-gradient(from 45deg,'
                  '#c8c8c8 0 25%,#f2f2f2 0 50%);background-size:20px 20px;'
                  'border:1px solid #777}'
                  '#captured-background:checked ~ main img{background:var(--icon-background)}'
                  '.source-preview{image-rendering:pixelated}dt{font-weight:bold}'
                  '.stage-preview{image-rendering:pixelated}figure{margin:1rem 0}'
                  'figcaption{font-weight:bold;margin-bottom:.3rem}'
                  '.palette{display:flex;flex-wrap:wrap;gap:.75rem;list-style:none;padding:0}'
                  '.palette li{display:flex;align-items:center;gap:.35rem}'
                  '.swatch{display:inline-block;width:2.5rem;height:2.5rem;'
                  'border:1px solid #555;flex:none}'
                  '.stages{display:grid;grid-template-columns:repeat(auto-fit,'
                  'minmax(min(100%,290px),1fr));gap:1rem}.stages figure{overflow:auto}'
                  'dd{margin:0 0 .35rem 0}dl{margin:.6rem 0;display:grid;'
                  'grid-template-columns:max-content minmax(0,1fr);column-gap:.7rem}'
                  'code{overflow-wrap:anywhere}.edited{font-weight:bold;color:#a33}'
                  '@media(max-width:700px){main{display:block}}'
                  '</style>'
                  f'<h1>Icon comparison</h1><p>Canonical group: <code>{group}</code>. '
                  f'Background: <code>{background}</code>. '
                  f'{len(variants)} source bitmap(s) and {len(traces)} trace result(s). '
                  'Images are shown at 16×; bitmap scaling uses nearest neighbour.</p>'
                  '<input type="checkbox" id="captured-background">'
                  '<label for="captured-background">Show captured background</label>'
                  '<p>The diagonal checker indicates transparency.</p>'
                  '<main><section><h2>Observed bitmaps</h2>'
                  + ''.join(observations) + '</section><section><h2>Algorithm results</h2>'
                  + artwork + '</section></main></html>\n')
        page = self._review_path(group)
        data = markup.encode('utf-8')
        if not page.is_file() or page.read_bytes()!=data:
            _atomic_bytes(page,data)

    def trace(self, image, background, *, algorithm='palette', palette_size=None, blur=.5,
              allow_raster=True, name=None, estimate_alpha=False):
        image = image.copy()
        if not image.width or not image.height:
            raise ValueError('The image to trace must be nonempty')
        background = _background(background)
        rgb = '#%02x%02x%02x'%background
        if algorithm not in ('palette','smooth-palette','pixel-boundary'):
            raise ValueError('Unknown tracing algorithm: '+algorithm)
        key = _filename(algorithm,palette_size,blur,estimate_alpha=estimate_alpha)
        digest = _hash(image,background)
        row = self.db.execute('SELECT group_id,dx,dy FROM bitmaps WHERE hash=?',(digest,)).fetchone()
        changed = row is None
        match = 'exact' if row else None
        fingerprint = None
        query_pixels = None
        if row:
            group,dx,dy = row
        else:
            group, dx, dy = digest, 0., 0.
            # Fuzzy matching is scoped to the same background and near-equal
            # dimensions. Each observed source image remains independently saved.
            fingerprint = _fingerprint(image,background)
            query_pixels = np.asarray(image.convert('RGBA'),dtype=np.float32)
            alpha = query_pixels[:,:,3:]/255
            query_pixels = query_pixels[:,:,:3]*alpha+np.asarray(background,dtype=np.float32)*(1-alpha)
            candidates = self.db.execute('''SELECT hash,group_id,dx,dy,width,height,fingerprint,sample
                FROM bitmaps WHERE background=? AND width BETWEEN ? AND ?
                AND height BETWEEN ? AND ?''',
                (rgb,image.width-1,image.width+1,image.height-1,image.height+1))
            best = None
            for known_hash, known_group, known_dx, known_dy, width, height, known_fp, sampled in candidates:
                known_image = None
                if known_fp is None:  # Index created by a previous version: backfill on demand.
                    known_image = self._bitmap(known_hash)
                    known_fp = _fingerprint(known_image,background)
                    with self.db:
                        self.db.execute('UPDATE bitmaps SET fingerprint=? WHERE hash=?',
                                        (known_fp,known_hash))
                if not _fingerprint_might_match(fingerprint,known_fp,
                                                image.size,(width,height)):
                    continue
                if sampled is None:
                    known_image = known_image or self._bitmap(known_hash)
                    sampled = _sample_fingerprint(known_image,background)
                    with self.db:
                        self.db.execute('UPDATE bitmaps SET sample=? WHERE hash=?',
                                        (sampled,known_hash))
                if not _sample_might_match(query_pixels,sampled,image.size,(width,height)):
                    continue
                similarity = _bitmap_match(image,known_image or self._bitmap(known_hash),background)
                if similarity and (best is None or similarity[0] < best[0]):
                    best = (similarity[0],known_group,known_dx+similarity[1][0],
                            known_dy+similarity[1][1])
            if best:
                _,group,dx,dy = best
                match = 'bitmap'
        existing = self.db.execute('SELECT status,hints FROM traces WHERE group_id=? AND algorithm=?',
                                   (group,key)).fetchone() if match else None
        registered = existing is not None
        if existing and existing[0]=='vector' and not self._svg_path(group,key).is_file():
            existing = None  # An SVG removed for retracing is regenerated.
        if not registered and match and algorithm == 'palette' and estimate_alpha:
            # A new tracer version must not silently discard hand edits made
            # to the previous version's standalone SVG.
            previous = _filename(algorithm,palette_size,blur,version=1,
                                 estimate_alpha=True)
            old = self.db.execute('''SELECT status,hints,generated_hash FROM traces
                WHERE group_id=? AND algorithm=?''',(group,previous)).fetchone()
            old_path = self._svg_path(group,previous)
            if old and old[0]=='vector' and old_path.is_file():
                contents = old_path.read_bytes()
                if old[2] is None or hashlib.sha256(contents).hexdigest()!=old[2]:
                    _atomic_bytes(self._svg_path(group,key),contents)
                    inherited_hints = {**json.loads(old[1]),'migrated_edit_from':previous}
                    with self.db:
                        self.db.execute('''INSERT INTO traces
                            (group_id,algorithm,status,hints,generated_hash)
                            VALUES (?,?,?,?,?)''',
                            (group,key,'vector',json.dumps(inherited_hints),old[2]))
                    existing = ('vector',json.dumps(inherited_hints))
        candidate_svg = None
        previews = {}
        if not existing:
            # Another algorithm always traces the canonical first sighting.
            sample = self._bitmap(group) if match and group != digest else image
            bg = background if group == digest else _background(self.db.execute(
                'SELECT background FROM groups WHERE id=?',(group,)).fetchone()[0])
            node = simplify_icon(sample,(0,0,sample.width,sample.height),
                                 allow_raster=True,smooth=algorithm=='smooth-palette',
                                 pixel_boundaries=algorithm=='pixel-boundary',
                                 estimate_alpha=estimate_alpha,
                                 palette_size=palette_size,blur=blur,background=bg,
                                 previews=previews)
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
                    (hash,group_id,background,width,height,dx,dy,match_kind,fingerprint,sample)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (digest,group,rgb,image.width,image.height,dx,dy,match or 'new',
                     fingerprint,_sample_fingerprint(image,background)))
        if isinstance(name,str):
            name = ' '.join(name.split())
            if name:
                with self.db:
                    changed |= bool(self.db.execute(
                        'INSERT OR IGNORE INTO names VALUES (?,?)',(digest,name)).rowcount)
        if not existing:
            changed = True
            status = 'vector' if candidate_svg else 'raster' if node is not None else 'empty'
            hints = {k:v for k,v in node.vector_data.items() if k not in
                     ('paths','local','icon_size')} if candidate_svg else {}
            if candidate_svg:
                _atomic_bytes(self._svg_path(group,key),candidate_svg.encode('utf-8'))
                self._write_previews(group,key,previews)
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
        if not self._review_path(group).is_file():
            self._migrate_group_layout(group)
            self._restore_previews(group)
            changed = True
        if changed or self._review_stale(group):
            self._sync_summary(group)
            self._refresh_review_pages(group)
        path = self._svg_path(group,key) if svg else None
        return TraceResult(result,svg,path,self._review_path(group),
                           digest,group,(dx,dy),match or 'new')


def trace(image, background, *, cache_dir=None, algorithm='palette', palette_size=None,
          blur=.5, allow_raster=True, name=None, estimate_alpha=False):
    """Trace an image over RGB background, returning an inline node and SVG.

    The SVG on disk is the source of truth on every cache hit. ``cache_dir``
    defaults to the platform's user cache; use a directory of your choice to
    inspect or hand edit the bitmaps and per-algorithm SVGs.
    """
    with TraceCache(cache_dir) as cache:
        return cache.trace(image,background,algorithm=algorithm,
                           palette_size=palette_size,blur=blur,allow_raster=allow_raster,
                           name=name,estimate_alpha=estimate_alpha)


def main(argv=None):
    """Trace a standalone icon PNG and print the editable cached SVG path."""
    import argparse
    parser=argparse.ArgumentParser(description='Trace a cropped icon into the shared SVG cache')
    parser.add_argument('image',type=Path,help='Cropped source PNG')
    parser.add_argument('background',help='Background colour, e.g. #ffffff')
    parser.add_argument('--cache-dir',type=Path)
    parser.add_argument('--algorithm',choices=('palette','smooth-palette','pixel-boundary'),
                        default='palette')
    parser.add_argument('--palette-size',type=int)
    parser.add_argument('--blur',type=float,default=.5)
    parser.add_argument('--estimate-alpha',action='store_true',
                        help='Infer icon transparency from its captured background')
    parser.add_argument('--name',help='Optional human label for this source image')
    args=parser.parse_args(argv)
    with Image.open(args.image) as image:
        result=trace(image,args.background,cache_dir=args.cache_dir,
                     algorithm=args.algorithm,palette_size=args.palette_size,
                     blur=args.blur,name=args.name,
                     estimate_alpha=args.estimate_alpha)
    if result.svg_path:
        print(result.svg_path)
        return 0
    parser.exit(1,'No vector trace met the quality threshold; bitmap retained in cache.\n')


if __name__=='__main__':
    raise SystemExit(main())
