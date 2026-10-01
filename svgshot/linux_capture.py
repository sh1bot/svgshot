"""AT-SPI2 capture. X11 pixels are automatic; Wayland needs an explicit window bitmap.

Never silently attach the semantics of one window to a portal-selected other window.
"""
import io
import os
import time
from .schema import FORMAT, VERSION, observation, validate


def api():
    try:
        import gi
        gi.require_version('Atspi', '2.0')
        from gi.repository import Atspi
        Atspi.set_timeout(1000, 5000)
        return Atspi
    except (ImportError, ValueError) as e:
        raise RuntimeError('Install python3-gi and gir1.2-atspi-2.0 (and enable desktop accessibility)') from e


def windows(Atspi):
    desktop = Atspi.get_desktop(0)
    result = []
    for i in range(min(desktop.get_child_count(), 1000)):
        app = desktop.get_child_at_index(i)
        for j in range(min(app.get_child_count(), 1000)):
            n = app.get_child_at_index(j)
            if n and n.get_role_name() in ('frame', 'dialog', 'window'):
                result.append((f'{i}:{j}', app, n))
    return result


def list_windows():
    Atspi = api()
    return [{'id': key, 'application': app.get_name(), 'title': node.get_name()}
            for key, app, node in windows(Atspi)]


ROLE_MAP = {'push button':'button', 'check box':'checkbox', 'radio button':'radiobutton',
 'text':'edit', 'password text':'edit', 'label':'text', 'static':'text', 'entry':'edit',
 'frame':'window', 'dialog':'window', 'panel':'pane', 'filler':'pane', 'section':'group',
 'combo box':'combobox', 'page tab':'tabitem', 'page tab list':'tab', 'scroll bar':'scrollbar',
 'progress bar':'progressbar', 'spin button':'spinner', 'menu item':'menuitem', 'menu bar':'menubar',
 'list item':'listitem', 'tree item':'treeitem', 'table cell':'dataitem', 'table row':'dataitem',
 'table column header':'headeritem', 'table row header':'headeritem', 'tool bar':'toolbar',
 'status bar':'statusbar', 'document frame':'document', 'document text':'document', 'link':'hyperlink'}


def capture(*, window=None, foreground=False, include_hidden=False, bitmap=None, window_bounds=None):
    Atspi = api()
    choices = windows(Atspi)
    if window is not None and foreground:
        raise ValueError('Choose --window or --foreground')
    if window is not None:
        selected = [n for key, _, n in choices if key == str(window)]
    elif foreground:
        selected = [n for _, _, n in choices if n.get_state_set().contains(Atspi.StateType.ACTIVE)]
    else:
        # A terminal picker works over SSH/X11 and does not activate application controls.
        if not os.isatty(0):
            raise ValueError('Use --list-windows then --window ID, or --foreground')
        import sys
        for key, app, n in choices:
            print(f'{key}: {app.get_name()} — {n.get_name()}', file=sys.stderr)
        print('Window ID: ', end='', file=sys.stderr, flush=True)
        selected_id = input()
        selected = [n for key, _, n in choices if key == selected_id]
    if len(selected) != 1:
        raise ValueError('Target window is missing or ambiguous; refresh --list-windows')
    target = selected[0]
    wayland = bool(os.environ.get('WAYLAND_DISPLAY'))
    if wayland and not bitmap:
        raise RuntimeError('Automatic Wayland window-to-AT-SPI association is not implemented. '
                           'Use --bitmap WINDOW.png --window-bounds X Y W H with --window ID; '
                           'the bitmap must represent exactly that window.')
    if bitmap and not window_bounds:
        raise ValueError('--bitmap requires explicit --window-bounds in AT-SPI window coordinates')
    if window_bounds and not bitmap:
        raise ValueError('--window-bounds requires --bitmap')
    coords = Atspi.CoordType.WINDOW if wayland else Atspi.CoordType.SCREEN
    before = target.get_component_iface().get_extents(coords)
    bounds = list(window_bounds) if bitmap else [before.x, before.y, before.width, before.height]
    if min(bounds[2:]) <= 0:
        raise ValueError('Target has no usable visible bounds')
    started = time.monotonic()
    ids, native, warnings = {}, {}, []
    def nid(n):
        if n not in ids:
            ids[n] = f'n{len(ids)+1}'
        return ids[n]
    def intersects(b):
        x,y,w,h=b; a,c,d,e=bounds
        return w>0 and h>0 and x<a+d and x+w>a and y<c+e and y+h>c
    def read(call):
        try:
            return observation('value', call())
        except Exception as error:
            return observation('error', message=type(error).__name__)
    def text_style(attrs):
        import re
        out = {}
        for key,dest in [('family-name','font_family'),('size','font_size'),('weight','font_weight'),
                         ('fg-color','foreground'),('bg-color','background'),('language','language')]:
            if key not in attrs: continue
            v=attrs[key]
            if dest in ('foreground','background'):
                parts=re.findall(r'\d+',v)
                if len(parts)==3 and all(0<=int(p)<=255 for p in parts):
                    out[dest]='#%02x%02x%02x'%tuple(map(int,parts))
            elif dest in ('font_size','font_weight'):
                try:
                    out[dest]=float(v)
                    if dest=='font_size':out['font_size_unit']='pt'
                except ValueError: pass
            else: out[dest]=v
        if 'style' in attrs: out['italic']=attrs['style']=='italic'
        return out
    def node(n,depth=0):
        if len(native)>=5000 or depth>64 or time.monotonic()-started>20:
            warnings.append('AT-SPI traversal truncated by count, depth, or time limit')
            return None
        key=nid(n)
        if key in native: return None  # Bound cyclic provider graphs.
        raw={};native[key]=raw
        role=read(n.get_role_name);raw['role']=role
        protected=role.get('status')!='value' or role.get('value')=='password text'
        state=read(n.get_state_set)
        states={'protected':protected}
        if state['status']=='value':
            ss=state['value']; raw['states']=observation('value',[s.value_nick for s in ss.get_states()])
            for flag,dest in [('ENABLED','enabled'),('FOCUSED','focused'),('FOCUSABLE','focusable'),
                              ('SELECTED','selected'),('EDITABLE','editable')]:
                states[dest]=ss.contains(getattr(Atspi.StateType,flag))
            states['offscreen']=not(ss.contains(Atspi.StateType.VISIBLE) and ss.contains(Atspi.StateType.SHOWING))
            if role.get('value') in ('check box','radio button','toggle button'):
                states['checked']='mixed' if ss.contains(Atspi.StateType.INDETERMINATE) else 'checked' if ss.contains(Atspi.StateType.CHECKED) else 'unchecked'
            if ss.contains(Atspi.StateType.EXPANDABLE):
                states['expansion']='expanded' if ss.contains(Atspi.StateType.EXPANDED) else 'collapsed'
        else:
            raw['states']=state;states['offscreen']=True
        ext=read(lambda:n.get_component_iface().get_extents(coords))
        b=[0,0,0,0]
        if ext['status']=='value':
            r=ext['value'];b=[r.x,r.y,max(0,r.width),max(0,r.height)];raw['bounds']=observation('value',b)
        else: raw['bounds']=ext
        hidden=states.get('offscreen',True) or not intersects(b)
        redacted=protected or (hidden and not include_hidden)
        out={'id':key,'role':ROLE_MAP.get(role.get('value'),role.get('value','custom').replace(' ','_')),
             'bounds':b,'states':states,'relationships':{},'children':[], 'native_ref':key,
             'text':{'status':'redacted' if redacted else 'not_supported','lines':[]},
             'value':observation('redacted' if redacted else 'not_captured')}
        if min(b[2:]) == 0:
            out['field_status']={'bounds':observation('error' if ext['status']=='error' else 'not_captured')}
        if not redacted:
            for getter,dest in [(n.get_name,'label'),(n.get_description,'description')]:
                record=read(getter);raw[dest]=record
                if record['status']=='value':out[dest]=record['value'][:65536]
            toolkit=read(lambda:n.get_application().get_toolkit_name())
            out['hints']={'toolkit':toolkit.get('value','')}
            # Only descriptive object attributes; URLs, IDs and arbitrary strings are omitted.
            attributes=read(n.get_attributes)
            if attributes['status']=='value':
                allowed={'level','setsize','posinset','placeholder-text','roledescription','live','atomic','relevant','invalid'}
                raw['attributes']=observation('value',{k:v for k,v in attributes['value'].items() if k in allowed})
            else:raw['attributes']=attributes
            if include_hidden:
                value=read(lambda:n.get_value_iface().get_current_value());raw['value']=value
                if value['status']=='value':out['value']=value
            text_iface=n.get_text_iface()
            if text_iface:
                try:
                    x,y,w,h=bounds
                    visible=text_iface.get_bounded_ranges(int(x),int(y),int(w),int(h),coords,
                                                          Atspi.TextClipType.BOTH,Atspi.TextClipType.BOTH)
                    lines=[]
                    for visible_range in visible[:2000]:
                        start,end=visible_range.start_offset,visible_range.end_offset
                        # Get only a bounded substring; never fetch the entire document.
                        content=text_iface.get_text(start,min(end,start+65536))
                        rect=text_iface.get_range_extents(start,min(end,start+65536),coords)
                        line={'content':content,'rectangles':[[rect.x,rect.y,rect.width,rect.height]],'style':{},'runs':[]}
                        offset=start
                        while offset<end and len(line['runs'])<2048:
                            attrs,a,z=text_iface.get_attribute_run(offset,True)
                            z=min(end,z,start+65536)
                            if attrs.get('invisible') in ('true','1') or attrs.get('hidden') in ('true','1'):
                                line={'content':'','rectangles':[],'style':{},'runs':[]}
                                warnings.append('Hidden formatted text redacted');break
                            if z<=offset:break
                            rr=text_iface.get_range_extents(offset,z,coords)
                            line['runs'].append({'content':text_iface.get_text(offset,z),'rectangles':[[rr.x,rr.y,rr.width,rr.height]],'style':text_style(attrs)})
                            offset=z
                        lines.append(line)
                    out['text']={'status':'value','lines':lines,'selections':observation('not_captured')}
                except Exception as error:
                    out['text']={'status':'error','message':type(error).__name__,'lines':[]}
            # Relationships refer to IDs only; do not read referenced names or contents.
            relationships=read(n.get_relation_set)
            if relationships['status']=='value':
                for relation in relationships['value']:
                    name=relation.get_relation_type().value_nick.replace('-','_')
                    out['relationships'][name]=[nid(relation.get_target(i)) for i in range(min(relation.get_n_targets(),256))]
        if not protected:
            children=read(n.get_child_count)
            raw['children_status']=children
            if children['status']=='value':
                for i in range(min(children['value'],5000)):
                    child=read(lambda:n.get_child_at_index(i))
                    if child['status']=='value' and child['value']:
                        result=node(child['value'],depth+1)
                        if result:out['children'].append(result)
        return out
    root=node(target)
    after=target.get_component_iface().get_extents(coords)
    if (before.x,before.y,before.width,before.height)!=(after.x,after.y,after.width,after.height):
        raise RuntimeError('Window moved or resized during capture')
    from PIL import Image,ImageGrab
    if bitmap:
        with Image.open(bitmap) as original:image=original.convert('RGB')
        warnings.append('Bitmap/window association supplied explicitly by the user')
    else:
        image=ImageGrab.grab(bbox=(bounds[0],bounds[1],bounds[0]+bounds[2],bounds[1]+bounds[3]),xdisplay=os.environ.get('DISPLAY'))
        warnings.append('X11 capture records visible screen pixels; overlapping windows may obscure the target')
    w,h=image.size; sx,sy,sw,sh=bounds
    def rect(b):
        x,y,a,c=b;return [(x-sx)*w/sw,(y-sy)*h/sh,a*w/sw,c*h/sh]
    def mapnode(n):
        n['bounds']=rect(n['bounds'])
        n['relationships']={k:[v for v in vs if v in native] for k,vs in n['relationships'].items()}
        def line(l):
            l['rectangles']=[rect(b) for b in l['rectangles']]
            for r in l.get('runs',[]):line(r)
        for l in n['text']['lines']:line(l)
        for c in n['children']:mapnode(c)
    mapnode(root)
    s={'format':FORMAT,'version':VERSION,'source':{'platform':'linux','provider':'linux-atspi'},
       'image':{'size':[w,h],'coordinate_space':'image-pixels','source_bounds':bounds,
                'source_to_image':[w/sw,0,0,h/sh,-sx*w/sw,-sy*h/sh]},'root':root,
       'capture_policy':{'include_hidden_content':include_hidden,'password_content':'redacted','actions_invoked':False},
       'warnings':list(dict.fromkeys(warnings)),'native':{'provider':'linux-atspi','nodes':native}}
    validate(s)
    stream=io.BytesIO();image.save(stream,format='PNG')
    return stream.getvalue(),s
