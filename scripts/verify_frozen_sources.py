"""Check that a delivered executable contains the current application code."""
from pathlib import Path
import sys
import types

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

ROOT = Path(__file__).resolve().parents[1]


def normalized(code):
    return (code.co_code, tuple(normalized(c) if isinstance(c, types.CodeType) else c for c in code.co_consts),
            code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars, code.co_argcount,
            code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags, code.co_exceptiontable)


def verify(path):
    path = Path(path).resolve()
    if path.suffix == ".pyz":
        archive = ZlibArchiveReader(str(path))
    else:
        executable = CArchiveReader(str(path))
        name = next(n for n in executable.toc if n.lower().endswith(".pyz"))
        archive = executable.open_embedded_archive(name)
    mismatches = []
    for source in sorted((ROOT / "papertranslator").glob("*.py")):
        module = "papertranslator" if source.stem == "__init__" else "papertranslator." + source.stem
        current = compile(source.read_bytes(), str(source), "exec", dont_inherit=True, optimize=0)
        frozen = archive.extract(module)
        if normalized(current) != normalized(frozen):
            mismatches.append(module)
    if mismatches:
        raise RuntimeError("Frozen code differs from source: " + ", ".join(mismatches))
    print("Frozen application code matches current sources:", path.name)


if __name__ == "__main__":
    verify(sys.argv[1])
