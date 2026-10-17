from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

exe = Path(sys.argv[1]).resolve()
report = Path(sys.argv[2]).resolve()
report.parent.mkdir(parents=True, exist_ok=True)
environment = os.environ.copy()
environment.pop("PYTHONPATH", None)
environment.pop("PYTHONHOME", None)
windows = Path(environment.get("WINDIR", "C:/Windows"))
environment["PATH"] = str(windows / "System32") + os.pathsep + str(windows)
process = subprocess.run([str(exe), "--self-test", str(report)], cwd=report.parent, env=environment, timeout=90)
if not report.exists():
    raise RuntimeError(f"Executable did not write a self-test report; exit={process.returncode}")
result = json.loads(report.read_text(encoding="utf-8"))
print(json.dumps({"exe": exe.name, "exit": process.returncode, "report": result}, ensure_ascii=False))
raise SystemExit(0 if result.get("ok") and process.returncode == 0 else 1)
