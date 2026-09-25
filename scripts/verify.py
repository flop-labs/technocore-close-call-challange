"""Check package bytes against manifest.json; trust its hash via the referee separately."""

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def verify_manifest(root: Path, manifest: dict) -> list[str]:
    if manifest.get("schema_version") != 1 or manifest.get("package") != "technocore-close-call":
        raise ValueError("manifest: unsupported schema or package")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("manifest: expected a nonempty files mapping")
    failures = []
    root = root.resolve()
    for name, record in files.items():
        if not isinstance(name, str) or not isinstance(record, dict):
            raise ValueError("manifest: malformed file record")
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                or relative.as_posix() != name or name in {".", "manifest.json"}):
            raise ValueError(f"manifest: unsafe artifact path {name!r}")
        path = root.joinpath(*relative.parts).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"manifest: artifact escapes package: {name}")
        digest = record.get("sha256")
        size = record.get("bytes")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or size < 0 or record.get("url") != name):
            raise ValueError(f"manifest: invalid hash, size, or relative URL for {name}")
        try:
            data = path.read_bytes()
        except OSError:
            failures.append(f"{name}: missing or unreadable")
            continue
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            failures.append(f"{name}: hash or size mismatch")
    return failures


def main() -> int:
    try:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest: expected an object")
        failures = verify_manifest(ROOT, manifest)
        if failures:
            raise ValueError("\n".join(failures))
    except (OSError, ValueError) as error:
        print(f"verify: {error}", file=sys.stderr)
        return 1
    print(f"Verified {len(manifest['files'])} artifacts against manifest.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
