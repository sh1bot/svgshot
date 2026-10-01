"""Compile the native helper with its source commit, without runtime sidecars."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('output', type=Path)
parser.add_argument('--arch', choices=['x86_64', 'arm64'])
parser.add_argument('--commit')
args = parser.parse_args()
commit = args.commit or subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
if not re.fullmatch(r'[0-9a-f]{40}', commit):
    parser.error('Commit must be a full Git SHA')
args.output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as folder:
    version = Path(folder)/'BuildVersion.swift'
    version.write_text(f'let captureBuildCommit = "{commit}"\n')
    command = ['swiftc', '-parse-as-library', '-O', '-D', 'SVGSHOT_BUILD_VERSION',
               '-I', str(root/'capture/macos')]
    if args.arch:
        command += ['-target', f'{args.arch}-apple-macos14.0']
    subprocess.run(command+[str(root/'capture/macos/main.swift'), str(version),
                            '-o', str(args.output)], check=True)
