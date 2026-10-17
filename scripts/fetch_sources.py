"""Fetch verifiable upstream source and license materials; never execute them."""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import tarfile

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination = ROOT / "vendor-sources"
    destination.mkdir(exist_ok=True)
    manifest = []
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=90) as client:
        for name in ("PyMuPDF", "rapidocr-onnxruntime", "Shapely", "chardet"):
            version = metadata.version(name)
            response = client.get(f"https://pypi.org/pypi/{name}/{version}/json")
            response.raise_for_status()
            entries = response.json()["urls"]
            entry = next((item for item in entries if item["packagetype"] == "sdist"), None)
            if entry is None and name == "rapidocr-onnxruntime":
                entry = next(item for item in entries if item["filename"].endswith("py3-none-any.whl"))
            if entry is None:
                raise RuntimeError(f"No source distribution found: {name}")
            path = destination / entry["filename"]
            if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["digests"]["sha256"]:
                print(f"Downloading {name} {version} source", flush=True)
                with client.stream("GET", entry["url"]) as stream:
                    stream.raise_for_status()
                    with open(path, "wb") as handle:
                        for chunk in stream.iter_bytes():
                            handle.write(chunk)
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if checksum != entry["digests"]["sha256"]:
                raise RuntimeError(f"Checksum mismatch: {name}")
            manifest.append({"project": name, "version": version, "file": path.name, "sha256": checksum, "source_url": entry["url"]})
            if name == "rapidocr-onnxruntime" and path.name.endswith(".tar.gz"):
                # Copy upstream source archive license without extracting code.
                with tarfile.open(path) as archive:
                    for member in archive.getmembers():
                        if member.isfile() and member.name.rsplit("/", 1)[-1].lower() in ("license", "license.txt", "notice"):
                            target = ROOT / "licenses" / "RapidOCR" / member.name.rsplit("/", 1)[-1]
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.extractfile(member).read())
        import shapely
        version = shapely.geos_version_string.split("-")[0]
        url = f"https://download.osgeo.org/geos/geos-{version}.tar.bz2"
        path = destination / f"geos-{version}.tar.bz2"
        if not path.exists():
            print(f"Downloading GEOS {version} source", flush=True)
            with client.stream("GET", url) as stream:
                stream.raise_for_status()
                with open(path, "wb") as handle:
                    for chunk in stream.iter_bytes():
                        handle.write(chunk)
        manifest.append({"project": "GEOS", "version": version, "file": path.name, "source_url": url, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "verification": "HTTPS upstream download; local checksum, not an upstream signature"})
        import pymupdf
        version = pymupdf.mupdf_version
        url = f"https://mupdf.com/downloads/archive/mupdf-{version}-source.tar.gz"
        path = destination / f"mupdf-{version}-source.tar.gz"
        if not path.exists():
            print(f"Downloading MuPDF {version} source", flush=True)
            partial = path.with_suffix(path.suffix + ".part")
            with client.stream("GET", url) as stream:
                stream.raise_for_status()
                with open(partial, "wb") as handle:
                    for chunk in stream.iter_bytes():
                        handle.write(chunk)
            with tarfile.open(partial) as archive:
                archive.getmembers()
            os.replace(partial, path)
        manifest.append({"project": "MuPDF", "version": version, "file": path.name, "source_url": url, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "verification": "HTTPS upstream download; local checksum, not an upstream signature"})
        for name, url in [("RapidOCR-APACHE-2.0.txt", "https://raw.githubusercontent.com/RapidAI/RapidOCR/main/LICENSE"),
                          ("PaddleOCR-APACHE-2.0.txt", "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/LICENSE")]:
            response = client.get(url)
            response.raise_for_status()
            target = ROOT / "licenses" / name
            target.write_bytes(response.content)
    import rapidocr_onnxruntime
    model_root = Path(rapidocr_onnxruntime.__file__).parent / "models"
    models = [{"filename": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size,
               "origin": "PaddleOCR weights converted and redistributed by RapidOCR 1.2.3", "license": "Apache-2.0"} for p in model_root.glob("*.onnx")]
    (ROOT / "licenses" / "ocr-models.json").write_text(json.dumps(models, indent=2), encoding="utf-8")
    (destination / "source-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Upstream source materials collected", flush=True)


if __name__ == "__main__":
    main()
