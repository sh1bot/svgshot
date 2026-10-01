"""Live GTK/AT-SPI/X11 smoke in a private CI desktop session."""
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gi
gi.require_version('Gtk','3.0')
gi.require_version('Atspi','2.0')
from gi.repository import Gtk,Atspi
from svgshot.linux_capture import capture,windows
from svgshot.schema import validate
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
            target=next((key for key,app,n in windows(Atspi) if n.get_name()=='svgshot Linux Fixture'),None)
            if target:break
            time.sleep(.1)
        assert target,'GTK window did not register with AT-SPI'
        png,snapshot=capture(window=target)
        validate(snapshot)
        import json
        data=json.dumps(snapshot)
        assert 'Visible text' in data and 'Preserve semantics' in data
        assert 'PasswordHiddenSentinel' not in data
        assert 'checked' in data
        assert png.startswith(b'\x89PNG')
        print('Validated live Linux accessibility, bitmap, checked state and password redaction')
    finally:
        process.terminate();process.wait(timeout=5)
