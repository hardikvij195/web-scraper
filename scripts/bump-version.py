"""Bump VERSION: 1.0.1 -> 1.0.2 ... 1.0.9 -> 1.1.0 ... 1.9.9 -> 2.0.0 (base-10 rollover,
user rule 2026-08-26). Run before committing agent code: `python scripts/bump-version.py`.
The CRM compares each machine's reported version against main's VERSION."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "VERSION"
major, minor, patch = (int(x) for x in p.read_text(encoding="utf-8").strip().split("."))
patch += 1
if patch > 9:
    patch, minor = 0, minor + 1
if minor > 9:
    minor, major = 0, major + 1
new = f"{major}.{minor}.{patch}"
p.write_text(new + "\n", encoding="utf-8")
print(new)
