"""Live GTK/AT-SPI/X11 smoke in a private CI desktop session."""
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version('Gtk','3.0')
gi.require_version('Atspi','2.0')
from gi.repository import Gtk,Atspi
from svgshot.schema import validate
from svgshot.snapshot import read_snapshot
import subprocess
if '--fixture' in sys.argv:
    w=Gtk.Window(title='svgshot Linux Fixture');w.set_default_size(320,180)
    box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.pack_start(Gtk.Label(label='Visible text'),True,True,0)
    button=Gtk.CheckButton(label='Preserve semantics');button.set_active(True)
    box.pack_start(button,True,True,0)
    password=Gtk.Entry();password.set_visibility(False);password.set_text('PasswordHiddenSentinel')
    box.pack_start(password,True,True,0);w.add(box);w.connect('destroy',Gtk.main_quit);w.show_all();Gtk.main()
else:
    import os
    env={**os.environ,'GTK_MODULES':'gail:atk-bridge','NO_AT_BRIDGE':'0'}
    # Start the accessibility bus before the GTK provider.
    Atspi.get_desktop(0)
    process=subprocess.Popen(['/usr/bin/python3',__file__,'--fixture'],env=env)
    try:
        target=None
        for _ in range(60):
            import json
            helper=sys.argv[1]
            listed=json.loads(subprocess.check_output([helper,'--list-windows']))
            target=next((n['id'] for n in listed if n['title']=='svgshot Linux Fixture'),None)
            if target:break
            time.sleep(.1)
        assert target,'GTK window did not register with AT-SPI'
        import io
        png=subprocess.check_output([helper,'--window',target,'--stdout'])
        snapshot=read_snapshot(io.BytesIO(png))
        validate(snapshot)
        import json
        data=json.dumps(snapshot)
        assert 'Visible text' in data and 'Preserve semantics' in data
        assert 'PasswordHiddenSentinel' not in data
        assert 'checked' in data
        assert png.startswith(b'\x89PNG')
        from PIL import Image
        image=Image.open(io.BytesIO(png));assert list(image.size)==snapshot['image']['size']
        def nodes(n):
            yield n
            for c in n['children']:yield from nodes(c)
        all_nodes=list(nodes(snapshot['root']))
        assert any(n['states'].get('checked')=='checked' for n in all_nodes)
        assert any(n['text'].get('lines') for n in all_nodes),'Visible text ranges missing'
        with __import__('tempfile').TemporaryDirectory() as folder:
            path=Path(folder)/'capture.png'
            subprocess.run([helper,'--window',target,'--out',str(path)],check=True)
            stored=read_snapshot(path);validate(stored)
            assert stored['root']['label']==snapshot['root']['label']
            # Explicit bitmap pairing must keep the original pixel data.
            b=snapshot['image']['source_bounds']
            paired=subprocess.check_output([helper,'--window',target,'--stdout','--bitmap',str(path),
                                           '--window-bounds',*map(str,b)])
            assert Image.open(io.BytesIO(paired)).tobytes()==image.tobytes()
            from svgshot.grab import capture_bytes
            wrapped=capture_bytes(native_helper=helper,window=target)
            validate(read_snapshot(io.BytesIO(wrapped)))
        print('Validated native Linux file/stdout/pairing/frontend, visible text, checked state and password redaction')
    finally:
        process.terminate();process.wait(timeout=5)
