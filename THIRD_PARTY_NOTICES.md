# Third-party notices

PaperTranslator's own source is provided under AGPL-3.0-only (see `LICENSE` and `NOTICE`). This notice does not replace any dependency's license. See the unmodified texts and actual versions in `licenses/`.

The repository contains source code; prebuilt Windows builds are published via GitHub Releases. If you build or redistribute a Windows executable, you are distributing a combined work that includes the components below; provide the corresponding source and notices with your build.

- **PyMuPDF / MuPDF**, Artifex and contributors: GNU AGPL v3 open-source distribution. [Project](https://github.com/pymupdf/PyMuPDF), [licensing](https://pymupdf.io/licensing). The exact source distributions for the shipped versions are attached to the GitHub Release under `vendor-sources/` (or can be re-fetched and verified with `scripts/fetch_sources.py`); the MuPDF archive includes or identifies its matching MuPDF sources and build instructions.
- **RapidOCR**, RapidAI contributors: Apache-2.0. The bundled ONNX OCR models derive from Baidu PaddleOCR and retain Apache-2.0 terms. [RapidOCR](https://github.com/RapidAI/RapidOCR), [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR). Model origins and checksums are recorded in `licenses/ocr-models.json`.
- **ONNX Runtime**, Microsoft and contributors: MIT, with additional third-party notices in its distribution.
- **OpenCV / opencv-python**: Apache-2.0 and third-party licenses; preserve both LICENSE and LICENSE-3RD-PARTY materials.
- **NumPy**: BSD-3-Clause and bundled numerical-library notices, including the license materials copied from its wheel.
- **Pillow**, **HTTPX**, **HTTP Core**, **AnyIO**, **h11**, **idna**, **six**, **PyYAML**, **pyclipper**, **coloredlogs**, **humanfriendly**, **protobuf**, **SymPy**, **mpmath**, **packaging**: their respective permissive licenses in the copied materials.
- **FlatBuffers**: Apache-2.0; license text in `licenses/flatbuffers-APACHE-2.0.txt`.
- **certifi**: Mozilla Public License 2.0 for the bundled certificate store.
- **Shapely**: BSD-3-Clause; its GEOS library carries LGPL terms. The portable `_internal` files can be replaced for library debugging / modification. [Shapely source](https://github.com/shapely/shapely), [GEOS source](https://libgeos.org/). See included GEOS notices.
- **Python**: Python Software Foundation license, included with the runtime notice materials.
- **Tcl/Tk**, **tkinterdnd2 / tkdnd**: respective upstream BSD-style / permissive licenses, including notices collected from the distribution.
- **PyInstaller**: GPL with its bootloader distribution exception; packaging does not by itself change the application's license. Its original license and exception are included.
- **Noto / Droid fallback fonts used by MuPDF**: their license texts are inside the MuPDF source archive under `resources/fonts/` (for example `han/LICENSE.txt`, `sil/OFL.txt`, `urw/OFL.txt`). No Windows proprietary font file is copied into the build. A custom font chosen by the user must have appropriate embedding rights.

DeepSeek and Xiaomi MiMo are external services, not bundled model weights. API users provide their own account credentials and comply with the selected service's terms. Neither provider sponsors this project.

Build tooling records dependency versions and copies license files without rewriting their terms. A list of upstream source links is informational; it is not a promise that every future combination of substituted dependencies has the same license conditions.
