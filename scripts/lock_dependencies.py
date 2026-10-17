from __future__ import annotations

from importlib import metadata
from pathlib import Path
from packaging.requirements import Requirement

root = Path(__file__).resolve().parents[1]
pending = [Requirement(line) for line in (root / "requirements.txt").read_text().splitlines() if line and not line.startswith("#")]
seen = {}
while pending:
    requirement = pending.pop()
    if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
        continue
    name = requirement.name.lower().replace("_", "-")
    if name in seen:
        continue
    dist = metadata.distribution(requirement.name)
    seen[name] = dist.version
    pending.extend(Requirement(item) for item in dist.requires or [])
(root / "requirements-lock.txt").write_text("# Actual Windows x64 build dependencies\n" + "\n".join(f"{name}=={version}" for name, version in sorted(seen.items())) + "\n", encoding="utf-8")
print(f"Locked {len(seen)} dependencies")
