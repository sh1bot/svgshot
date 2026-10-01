"""Build the dependency-light Linux capture zipapp (not the SVG converter)."""
from pathlib import Path
import tempfile
import shutil
import zipapp
import sys
root=Path(__file__).resolve().parents[1]
out=Path(sys.argv[1]);out.parent.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as temp:
    folder=Path(temp);package=folder/'svgshot';package.mkdir()
    for name in ['__init__.py','grab.py','snapshot.py','schema.py','linux_capture.py']:
        shutil.copy2(root/'svgshot'/name,package/name)
    zipapp.create_archive(folder,target=out,main='svgshot.grab:main',compressed=True)
