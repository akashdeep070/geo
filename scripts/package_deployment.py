"""Create a clean source archive ready to import or deploy with Vercel CLI."""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = (
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "vercel.json",
    ".python-version",
    ".vercelignore",
    ".gitignore",
)
SOURCE_DIRECTORIES = ("app", "web", "examples", "scripts", "tests", ".github")
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv", ".vercel", ".git"}


def source_files():
    for name in ROOT_FILES:
        yield ROOT / name
    for name in SOURCE_DIRECTORIES:
        for path in sorted((ROOT / name).rglob("*")):
            if any(part in IGNORED_PARTS for part in path.relative_to(ROOT).parts):
                continue
            if path.name == ".env" or path.name.startswith(".env.") or path.suffix == ".pyc":
                continue
            if path.is_symlink():
                raise SystemExit(f"Cannot package a source symlink: {path.relative_to(ROOT)}")
            if path.is_file():
                yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "geo-vercel.zip")
    output = parser.parse_args().output.resolve()
    if output.suffix != ".zip" or any(
        output.is_relative_to(ROOT / folder) for folder in SOURCE_DIRECTORIES
    ):
        raise SystemExit("Choose a .zip output outside the source directories, such as dist/.")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_frontend.py"), "--check"],
        cwd=ROOT,
        check=True,
    )
    files = list(source_files())
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(ROOT).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"Created {output} ({len(files)} files, {output.stat().st_size:,} bytes)")
    print(f"SHA256: {digest}")


if __name__ == "__main__":
    main()
