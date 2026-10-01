"""Platform-neutral, static capture schema and legacy UIA import adapter."""
from copy import deepcopy
import math

FORMAT = 'svgshot.capture'
VERSION = 3
ROLES = ('button calendar checkbox combobox edit hyperlink image listitem list menu menubar '
         'menuitem progressbar radiobutton scrollbar slider spinner statusbar tab tabitem text '
         'toolbar tooltip tree treeitem custom group thumb datagrid dataitem document splitbutton '
         'window pane header headeritem table titlebar separator semanticzoom appbar').split()


def observation(status='value', value=None, **details):
    result = {'status': status, **details}
    if status == 'value':
        result['value'] = value
    return result


def rgb(value):
    if type(value) is int and 0 <= value <= 0xffffff:
        return '#%02x%02x%02x' % (value & 255, value >> 8 & 255, value >> 16 & 255)


def uia_style(attributes):
    out = {}
    names = {'40005': 'font_family', '40006': 'font_size', '40007': 'font_weight',
             '40014': 'italic', '40015': 'hidden', '40018': 'read_only',
             '40031': 'underline', '40026': 'strikethrough'}
    for key, record in attributes.items():
        if record.get('status') != 'value':
            continue
        if key in ('40001', '40008'):
            color = rgb(record.get('value'))
            if color:
                out['background' if key == '40001' else 'foreground'] = color
        elif key in names:
            out[names[key]] = record.get('value')
            if key == '40006':
                out['font_size_unit'] = 'pt'
            elif key in ('40031', '40026'):
                out[names[key]] = bool(record.get('value'))
    return out


def from_uia(snapshot):
    """Normalize without deleting the privacy-filtered native snapshot."""
    if snapshot.get('version') == VERSION:
        validate(snapshot)
        return snapshot
    if snapshot.get('version') not in (1, 2):
        raise ValueError('Unsupported legacy capture schema')
    width, height = snapshot['image_size']
    sx, sy, sw, sh = snapshot['screen_bounds']
    if any(type(v) is not int or v <= 0 for v in (width,height)) or any(type(v) not in (int,float) or not math.isfinite(v) for v in (sx,sy,sw,sh)) or sw<=0 or sh<=0:
        raise ValueError('Invalid legacy image dimensions or bounds')
    ids = {}
    def index(n):
        ids.setdefault(n.get('id', ''), 'n%d' % (len(ids) + 1))
        for child in n.get('children', []):
            index(child)
    index(snapshot['root'])
    def rect(b):
        x, y, w, h = b
        return [(x-sx)*width/sw, (y-sy)*height/sh, max(0,w*width/sw), max(0,h*height/sh)]
    def text(r):
        result = {'content': r.get('text', ''), 'rectangles': [rect(b) for b in r.get('rectangles', [])],
                  'style': uia_style(r.get('attributes', {}))}
        result['runs'] = [text(t) for t in r.get('format_runs', {}).get('ranges', [])]
        return result
    def node(n):
        role_id = n.get('control_type', 0) - 50000
        out = {'id': ids[n.get('id', '')], 'role': ROLES[role_id] if 0 <= role_id < len(ROLES) else 'custom',
               'bounds': rect(n.get('bounds', [0, 0, 0, 0])), 'label': n.get('name', ''),
               'help': n.get('help_text', ''), 'role_description': n.get('localized_control_type', ''),
               'states': {}, 'relationships': {}, 'text': {'status': n.get('text_capture', {}).get('status', 'not_captured'),
               'lines': [text(r) for r in n.get('text_ranges', [])],
               'selections': observation(n.get('text_selection', {}).get('status','not_captured'),
                   [text(r) for r in n.get('text_selection', {}).get('ranges', [])])},
               'hints': {'toolkit': n.get('framework_id', ''), 'class': n.get('class_name', '')},
               'native_ref': n.get('id', ''), 'children': [node(c) for c in n.get('children', [])]}
        for old, new in [('enabled', 'enabled'), ('keyboard_focus', 'focused'), ('focusable', 'focusable'),
                         ('password', 'protected'), ('offscreen', 'offscreen')]:
            if old in n:
                out['states'][new] = n[old]
        states = n.get('states', {})
        out['states'].update({k: v for k, v in states.items() if k in ('selected', 'read_only')})
        if 'toggle' in states:
            out['states']['checked'] = {0: 'unchecked', 1: 'checked', 2: 'mixed'}.get(states['toggle'], 'unknown')
        if 'expand_collapse' in states:
            out['states']['expansion'] = {0: 'collapsed', 1: 'expanded', 2: 'partial', 3: 'leaf'}.get(states['expand_collapse'], 'unknown')
        out['value'] = observation('value', states['value']) if 'value' in states else observation('not_captured')
        if 'range_value' in states:
            out['value'] = observation('value', states['range_value'])
        label = n.get('labeled_by')
        if label in ids:
            out['relationships']['labelled_by'] = [ids[label]]
        out['shortcuts'] = {k: n[k] for k in ('access_key', 'accelerator_key') if n.get(k)}
        if n.get('password') or n.get('content_redacted'):
            out['text'] = {'status': 'redacted', 'lines': []}
            out['value'] = observation('redacted')
        # Older convenience fields can default to false after a failed getter.
        # Typed property records are authoritative when present.
        statuses = {}
        if min(out['bounds'][2:]) == 0:
            statuses['bounds'] = observation('not_captured')
        for key, record in n.get('properties', {}).items():
            prop = snapshot.get('property_names', {}).get(key, '')
            field = next((dst for src, dst in [('IsEnabled','enabled'), ('HasKeyboardFocus','focused'),
                        ('IsKeyboardFocusable','focusable'), ('IsOffscreen','offscreen')]
                        if prop.endswith(src+'Property') or prop.endswith(src)), None)
            if field and record.get('status') != 'value':
                out['states'].pop(field, None)
                statuses['states.'+field] = deepcopy(record)
        if statuses:
            out['field_status'] = statuses
        return out
    result = {'format': FORMAT, 'version': VERSION, 'source': {'platform': 'windows', 'provider': 'windows-uia'},
              'image': {'size': [width, height], 'coordinate_space': 'image-pixels',
                        'source_bounds': snapshot['screen_bounds'], 'source_to_image': [width/sw, 0, 0, height/sh, -sx*width/sw, -sy*height/sh]},
              'capture_policy': deepcopy(snapshot.get('capture_policy', {})), 'warnings': snapshot.get('warnings', []),
              'root': node(snapshot['root']), 'native': {'provider': 'windows-uia', 'snapshot': deepcopy(snapshot)}}
    validate(result)
    return result


def validate(snapshot):
    if snapshot.get('format') != FORMAT or snapshot.get('version') != VERSION:
        raise ValueError('Unsupported unified capture schema')
    image = snapshot.get('image', {})
    size = image.get('size', [])
    if len(size) != 2 or any(type(v) is not int or v <= 0 for v in size) or image.get('coordinate_space') != 'image-pixels':
        raise ValueError('Invalid capture image description')
    ids, refs = set(), []
    def node(n, depth=0):
        if depth > 64 or len(ids) >= 5000:
            raise ValueError('Capture tree exceeds limits')
        if not isinstance(n, dict) or not isinstance(n.get('id'), str) or n['id'] in ids:
            raise ValueError('Invalid or duplicate capture node ID')
        ids.add(n['id'])
        if not isinstance(n.get('role'), str):
            raise ValueError('Capture node needs a named role')
        for b in [n.get('bounds', [])] + [r for l in n.get('text', {}).get('lines', []) for r in l.get('rectangles', [])]:
            if len(b) != 4 or any(type(v) not in (int, float) or not math.isfinite(v) for v in b) or min(b[2:]) < 0:
                raise ValueError('Invalid capture rectangle')
        for targets in n.get('relationships', {}).values():
            refs.extend(targets)
        for c in n.get('children', []):
            node(c, depth+1)
    node(snapshot.get('root'))
    if any(ref not in ids for ref in refs):
        raise ValueError('Unresolved capture relationship')


def render_view(snapshot):
    """Compatibility view for existing reconstruction; never used as saved metadata."""
    if snapshot.get('version') != VERSION:
        return snapshot
    validate(snapshot)
    size = snapshot['image']['size']
    def line(t):
        return {'text': t.get('content', ''), 'rectangles': t.get('rectangles', []),
                'style': t.get('style', {}), 'format_runs': {'ranges': [line(r) for r in t.get('runs', [])]}}
    def node(n):
        states = n.get('states', {})
        s = {k: v for k, v in states.items() if k in ('selected', 'read_only')}
        if states.get('checked') in ('unchecked', 'checked', 'mixed'):
            s['toggle'] = {'unchecked': 0, 'checked': 1, 'mixed': 2}[states['checked']]
        if states.get('expansion') in ('collapsed', 'expanded', 'partial', 'leaf'):
            s['expand_collapse'] = {'collapsed': 0, 'expanded': 1, 'partial': 2, 'leaf': 3}[states['expansion']]
        v = n.get('value', {})
        if v.get('status') == 'value':
            s['range_value' if type(v['value']) in (int, float) else 'value'] = v['value']
        return {'id': n['id'], 'role': n['role'], 'name': n.get('label', ''), 'bounds': n['bounds'],
                'localized_control_type': n.get('role_description', ''), 'help_text': n.get('help', ''),
                'description': n.get('description', ''), 'states': s, 'password': states.get('protected', False),
                'offscreen': states.get('offscreen', False), 'enabled': states.get('enabled', True),
                'keyboard_focus': states.get('focused', False), 'focusable': states.get('focusable', False),
                'control_element': True, 'content_element': True,
                'class_name': n.get('hints', {}).get('class', ''), 'framework_id': n.get('hints', {}).get('toolkit', ''),
                'text_ranges': [line(t) for t in n.get('text', {}).get('lines', [])],
                **n.get('shortcuts', {}), 'children': [node(c) for c in n.get('children', [])]}
    return {'version': 2, 'image_size': size, 'screen_bounds': [0, 0, *size], 'root': node(snapshot['root']),
            'warnings': snapshot.get('warnings', [])}
