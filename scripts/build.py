"""Generate the standalone fold and a deterministic, locally verifiable manifest."""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0-draft"
GENERATED = {
    "close_call_fold.py": ("Python fold", "python"),
}
ARTIFACTS = (
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "README.md",
    "LICENSE",
    "NOTICE",
    "close-call-game.md",
    "contest.json",
    "close_call_fold.py",
    "scripts/build.py",
    "scripts/verify.py",
    "examples/sample-season.jsonl",
    "examples/sample-season.expected.json",
    "tests/test_fold.py",
    "tests/test_package.py",
)


def extract_block(document: str, heading: str, language: str) -> bytes:
    sections = document.split(f"\n## {heading}\n")
    if len(sections) != 2:
        raise ValueError(f"document: expected one section titled {heading!r}")
    section = sections[1].split("\n## ", 1)[0]
    matches = re.findall(rf"^```{language}\n(.*?)^```\s*$", section, re.M | re.S)
    if len(matches) != 1:
        raise ValueError(f"document: expected one {language} block in {heading!r}")
    return matches[0].encode("utf-8")


def outputs(root: Path) -> dict[str, bytes]:
    document = (root / "close-call-game.md").read_text(encoding="utf-8")
    generated = {
        path: extract_block(document, heading, language)
        for path, (heading, language) in GENERATED.items()
    }
    files = {}
    for path in sorted(ARTIFACTS):
        data = generated[path] if path in generated else (root / path).read_bytes()
        files[path] = {
            "url": path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    manifest = {
        "schema_version": 1,
        "package": "technocore-close-call",
        "version": VERSION,
        "status": "draft",
        "entrypoint": "close-call-game.md",
        "url_resolution": "Artifact URLs are relative to this manifest's URL.",
        "files": files,
    }
    generated["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    return generated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Refuse stale outputs without changing files")
    mode.add_argument("--archive", action="store_true", help="Also create a ZIP containing only package artifacts")
    args = parser.parse_args()
    try:
        expected = outputs(ROOT)
        stale = []
        for name, data in expected.items():
            path = ROOT / name
            if args.check:
                if not path.exists() or path.read_bytes() != data:
                    stale.append(name)
            else:
                path.write_bytes(data)
        if stale:
            raise ValueError("stale generated files: " + ", ".join(stale))
        if args.archive:
            destination = ROOT / "dist" / f"technocore-close-call-{VERSION}.zip"
            destination.parent.mkdir(exist_ok=True)
            with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in sorted((*ARTIFACTS, "manifest.json")):
                    info = zipfile.ZipInfo(f"technocore-close-call/{name}", date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    archive.writestr(info, (ROOT / name).read_bytes())
            print(f"Created {destination.relative_to(ROOT)}")
    except (OSError, ValueError, KeyError) as error:
        print(f"build: {error}", file=sys.stderr)
        return 1
    print("Generated files are current." if args.check else "Generated fold and manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
