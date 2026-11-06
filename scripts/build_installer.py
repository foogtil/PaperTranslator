"""Build a per-user Windows installer from the complete portable payload."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from papertranslator import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compiler', type=Path, default=ROOT/'build/tools/innosetup/ISCC.exe')
    parser.add_argument('--payload', type=Path, default=ROOT/f'dist/v{__version__}/PaperTranslator')
    parser.add_argument('--output', type=Path, default=ROOT/'release')
    args = parser.parse_args()
    compiler = args.compiler.resolve()
    payload, output = args.payload.resolve(), args.output.resolve()
    if not compiler.is_file():
        raise SystemExit('Install Inno Setup 7 and pass --compiler path/to/ISCC.exe')
    for name in ('PaperTranslator.exe', '_internal', 'LICENSE', 'NOTICE', 'vendor-sources', f'PaperTranslator-{__version__}-source.zip'):
        if not (payload/name).exists(): raise SystemExit('Incomplete payload: '+name)
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(compiler), f'/DAppVersion={__version__}', f'/DPayloadDir={payload}',
                    f'/DOutputPath={output}', str(ROOT/'scripts/installer.iss')], check=True, cwd=ROOT)
    installer = output/f'PaperTranslator-{__version__}-Setup.exe'
    with installer.open('rb') as f: digest=hashlib.file_digest(f,'sha256').hexdigest()
    manifest=output/f'SHA256-{__version__}.json'
    hashes=json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
    hashes[installer.name]=digest
    manifest.write_text(json.dumps(hashes,indent=2),encoding='utf-8')
    print('Installer:',installer)


if __name__ == '__main__': main()
