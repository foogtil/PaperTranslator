"""Reproducible Windows builds; never include user settings or translation jobs."""
from __future__ import annotations

import importlib.metadata as metadata
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from papertranslator import __version__


def collect_licenses():
    destination = ROOT / "licenses"
    destination.mkdir(exist_ok=True)
    names = ["PyMuPDF", "rapidocr-onnxruntime", "onnxruntime", "opencv-python", "numpy", "Pillow", "httpx", "httpcore", "certifi", "pyclipper", "Shapely", "PyYAML", "six", "anyio", "idna", "h11", "sniffio", "coloredlogs", "humanfriendly", "flatbuffers", "protobuf", "sympy", "mpmath", "packaging", "tkinterdnd2", "pyinstaller"]
    toc = ROOT / "build" / f"v{__version__}" / "PaperTranslator" / "PYZ-00.toc"
    if not toc.exists():
        toc = ROOT / "build" / "PaperTranslator" / "PYZ-00.toc"
    if toc.is_file():
        module_map = metadata.packages_distributions()
        discovered = set()

        def walk(value):
            if isinstance(value, (tuple, list)):
                if len(value) > 1 and isinstance(value[0], str) and isinstance(value[1], str) and "site-packages" in value[1]:
                    discovered.update(module_map.get(value[0].split(".")[0], []))
                for child in value:
                    walk(child)
            elif isinstance(value, dict):
                for child in value.values():
                    walk(child)

        walk(ast.literal_eval(toc.read_text(encoding="utf-8")))
        names.extend(sorted(discovered))
        (destination / "bundled-packages.json").write_text(json.dumps({n: metadata.version(n) for n in sorted(discovered)}, indent=2), encoding="utf-8")
        print("Collected licenses for bundled packages: " + ", ".join(sorted(discovered)), flush=True)
    names = list(dict.fromkeys(names))
    lockfile = ROOT / "requirements-lock.txt"
    locked = {}
    if lockfile.exists():
        for line in lockfile.read_text(encoding="utf-8").splitlines():
            if "==" in line and not line.startswith("#"):
                name, version = line.split("==", 1)
                locked[name.lower().replace("_", "-")] = version
    for name in names:
        try:
            locked[name.lower().replace("_", "-")] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    lockfile.write_text("# Actual build dependencies, including bundled optional modules\n" + "\n".join(f"{n}=={v}" for n, v in sorted(locked.items())) + "\n", encoding="utf-8")
    manifest = []
    for name in names:
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        copied = []
        for record in dist.files or []:
            if not any(word in str(record).lower() for word in ("license", "copying", "notice")):
                continue
            source = Path(dist.locate_file(record))
            if not source.is_file() or source.suffix.lower() in (".pyc", ".py", ".pyd"):
                continue
            target = destination / name / str(record).replace("../", "")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(str(target.relative_to(ROOT)))
        manifest.append({"package": name, "version": dist.version, "license_files": copied,
                         "project_urls": dist.metadata.get_all("Project-URL") or [], "homepage": dist.metadata.get("Home-page", "")})
    # Python is bundled by PyInstaller, including its PSF license.
    for filename in ("LICENSE.txt", "LICENSE"):
        python_license = Path(sys.base_prefix) / filename
        if python_license.exists():
            (destination / "Python").mkdir(exist_ok=True)
            shutil.copy2(python_license, destination / "Python" / filename)
    for library in (Path(sys.base_prefix) / "tcl").glob("*/license.terms"):
        target = destination / "TclTk" / library.parent.name
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(library, target / library.name)
    for entry in manifest:
        if entry["package"] == "flatbuffers":
            manual = "licenses/flatbuffers-APACHE-2.0.txt"
            if manual not in entry["license_files"]:
                entry["license_files"].append(manual)
    (destination / "dependency-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def verify_vendor_sources():
    """Refuse to build an AGPL distribution without the exact third-party sources."""
    vendor = ROOT / "vendor-sources"
    manifest_path = vendor / "source-manifest.json"
    if not manifest_path.is_file():
        raise SystemExit("vendor-sources/source-manifest.json is missing. Run scripts/fetch_sources.py first.")
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in entries:
        path = vendor / entry["file"]
        if not path.is_file():
            raise SystemExit(f"Missing third-party source: {entry['file']}. Run scripts/fetch_sources.py first.")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != entry["sha256"]:
            raise SystemExit(f"Checksum mismatch for {entry['file']}.")
    print("Verified third-party source archives", flush=True)


def source_archive(destination):
    allowed_files = ["run.py", "pyproject.toml", "requirements.txt", "requirements-lock.txt", "README.md", "README.zh-CN.md", "DEFAULTS.md", "TESTING.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "PaperTranslator.spec", ".gitignore"]
    allowed_dirs = ["papertranslator", "scripts", "tests", "licenses", "assets", "docs"]
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in allowed_files:
            p = ROOT / name
            if p.is_file():
                archive.write(p, name)
        for directory in allowed_dirs:
            for p in (ROOT / directory).rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                    archive.write(p, p.relative_to(ROOT))
        manifest = ROOT / "vendor-sources" / "source-manifest.json"
        if manifest.is_file():
            archive.write(manifest, manifest.relative_to(ROOT))


def main():
    collect_licenses()
    if "--licenses-only" in sys.argv:
        return
    verify_vendor_sources()
    dist = ROOT / "dist" / f"v{__version__}"
    work = ROOT / "build" / f"v{__version__}"
    if "--package-only" not in sys.argv:
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(dist), "--workpath", str(work), str(ROOT / "PaperTranslator.spec")], cwd=ROOT, check=True)
    destination = ROOT / "release"
    destination.mkdir(exist_ok=True)
    source = destination / f"PaperTranslator-{__version__}-source.zip"
    source_archive(source)
    shutil.copy2(dist / "PaperTranslator-Standalone.exe", destination / "PaperTranslator-Standalone.exe")
    portable = dist / "PaperTranslator"
    for name in ("README.md", "README.zh-CN.md", "DEFAULTS.md", "TESTING.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(ROOT / name, portable / name)
    shutil.copytree(ROOT / "licenses", portable / "licenses", dirs_exist_ok=True)
    shutil.copy2(source, portable / source.name)
    # Ship the verified corresponding PyMuPDF/MuPDF and other third-party sources.
    shutil.copytree(ROOT / "vendor-sources", portable / "vendor-sources", dirs_exist_ok=True)
    shutil.make_archive(str(destination / f"PaperTranslator-{__version__}-Windows-x64"), "zip", dist, "PaperTranslator")
    import hashlib
    hashes = {}
    for path in list(destination.glob(f"*{__version__}*.zip")) + list(dist.glob("*.exe")):
        with open(path, "rb") as handle:
            hashes[path.name] = hashlib.file_digest(handle, "sha256").hexdigest()
    (destination / f"SHA256-{__version__}.json").write_text(json.dumps(hashes, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
