from hashlib import sha256
from pathlib import Path
import sys

EXPECTED = "528bf9b9dddd6ac9bef801e4e4249bdea258ee6d2faedd99519e8f1105832374"
root = Path(__file__).resolve().parent
parts = [root / f"contest-agent-1.5.5-real-debug.tar.part{i:03d}" for i in range(1, 5)]
output = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "contest-agent-1.5.5-real-debug.tar"
if not all(part.is_file() for part in parts):
    raise SystemExit("Download all four part files into the same directory first.")
hash_value = sha256()
with output.open("xb") as target:
    for part in parts:
        with part.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hash_value.update(chunk)
                target.write(chunk)
if hash_value.hexdigest() != EXPECTED:
    raise SystemExit(f"SHA-256 mismatch; remove the invalid output: {output}")
print(f"Verified: {output}\nSHA-256: {EXPECTED}")
