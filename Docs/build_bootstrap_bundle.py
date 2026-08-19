"""Build the single-file bootstrap distributed to an isolated VDI.

Run this on the source machine after any code change:
    python Docs/build_bootstrap_bundle.py

It writes Docs/bootstrap_project.py.  That output is intentionally generated
and self-contained: copy only it to the VDI and run ``python
bootstrap_project.py`` there.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import zlib


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "Docs" / "bootstrap_project.py"

# The VDI receives application source and project documentation. Dependencies,
# demo data, and mutable runtime state are deliberately not bundled: the
# bootstrap recreates them from the locked npm manifest / requirements file.
INCLUDE_ROOTS = ("backend", "frontend", "Docs")
EXCLUDED_PARTS = {
    ".env",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".git",
    "data",
    "data_demo",
    "dist",
    "build",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".tsbuildinfo"}
EXCLUDED_FILES = {"Docs/bootstrap_project.py"}


def included(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if relative.as_posix() in EXCLUDED_FILES:
        return False
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    return path.suffix.lower() not in EXCLUDED_SUFFIXES


def collect_files() -> list[tuple[str, str, str]]:
    files: list[tuple[str, str, str]] = []
    for root_name in INCLUDE_ROOTS:
        root = ROOT / root_name
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not included(path):
                continue
            raw = path.read_bytes()
            relative = path.relative_to(ROOT).as_posix()
            files.append(
                (
                    relative,
                    hashlib.sha256(raw).hexdigest(),
                    base64.b64encode(raw).decode("ascii"),
                )
            )
    return files


RUNTIME = r'''"""Self-contained FPT RAG bootstrap for a CPU-only VDI.

Run after transferring this single file to the VDI:
    python bootstrap_project.py --destination C:\\FPT_RAG --profile gateway

The script verifies every embedded project file before writing it, recreates the
frontend, backend, and documentation, creates a Python virtual environment,
installs the VDI dependencies, runs npm ci from the committed lockfile, builds
the frontend, and runs the offline test suite. It never installs GPU/CUDA/Torch
packages or downloads local Hugging Face models.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
import urllib.request
import zipfile
import zlib

PAYLOAD = "__PAYLOAD__"
PAYLOAD_SHA256 = "__PAYLOAD_SHA256__"


def run(command: list[str], *, cwd: Path) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def require_python() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11+ is required; install it and rerun this file.")


def ensure_npm(destination: Path) -> str:
    """Return npm, downloading a verified portable Node.js LTS on Windows when
    the VDI has Python but no system Node installation."""
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm:
        return npm
    if os.name != "nt":
        raise SystemExit("npm is missing. Install Node.js LTS and rerun this file.")

    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower()
    )
    if not arch:
        raise SystemExit(f"Unsupported Windows architecture for portable Node.js: {platform.machine()}")

    print("= npm not found; downloading a portable Node.js LTS runtime.")
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=60) as response:
        releases = json.loads(response.read().decode("utf-8"))
    file_kind = f"win-{arch}-zip"
    release = next(
        (item for item in releases if item.get("lts") and file_kind in item.get("files", [])),
        None,
    )
    if release is None:
        raise SystemExit(f"No Node.js LTS release advertises {file_kind}.")

    version = release["version"]
    filename = f"node-{version}-win-{arch}.zip"
    base_url = f"https://nodejs.org/dist/{version}"
    tools_dir = destination / ".tools"
    archive_path = tools_dir / filename
    tools_dir.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(f"{base_url}/SHASUMS256.txt", timeout=60) as response:
        checksums = response.read().decode("utf-8")
    expected = next(
        (line.split()[0] for line in checksums.splitlines() if line.split()[-1] == filename),
        None,
    )
    if expected is None:
        raise SystemExit(f"Node.js checksum is missing for {filename}.")
    with urllib.request.urlopen(f"{base_url}/{filename}", timeout=180) as response:
        archive_path.write_bytes(response.read())
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"Portable Node.js checksum failed for {filename}.")

    extract_root = tools_dir.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            output = (tools_dir / member.filename).resolve()
            if output != extract_root and extract_root not in output.parents:
                raise SystemExit(f"Unsafe path in Node.js archive: {member.filename}")
        archive.extractall(tools_dir)
    extracted = tools_dir / f"node-{version}-win-{arch}"
    node_root = tools_dir / "node"
    extracted.rename(node_root)
    npm = node_root / "npm.cmd"
    if not npm.is_file():
        raise SystemExit("Portable Node.js extracted without npm.cmd.")
    return str(npm)


def decode_files() -> list[tuple[str, str, str]]:
    packed = base64.b64decode(PAYLOAD.encode("ascii"))
    if hashlib.sha256(packed).hexdigest() != PAYLOAD_SHA256:
        raise SystemExit("Bootstrap payload checksum failed; transfer the file again.")
    return __import__("json").loads(zlib.decompress(packed).decode("utf-8"))


def write_sources(destination: Path) -> None:
    for relative, expected_hash, encoded in decode_files():
        raw = base64.b64decode(encoded.encode("ascii"))
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise SystemExit(f"Embedded-file checksum failed: {relative}")
        output = destination / Path(relative)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(raw)


def configure_env(backend: Path, profile: str) -> None:
    env_file = backend / ".env"
    if env_file.exists():
        print("= backend/.env already exists; leaving it unchanged.")
        return
    template = backend / f".env.vdi.{profile}.example"
    shutil.copyfile(template, env_file)
    print(f"= Created backend/.env from {template.name}.")
    if profile == "gateway":
        print("! Edit backend/.env: set the gateway URLs and OPENAI_API_KEY before starting the API.")


def write_launchers(destination: Path) -> None:
    """Create Windows launchers that support either system or portable Node."""
    (destination / "start-backend.cmd").write_text(
        '@echo off\n'
        'cd /d "%~dp0backend"\n'
        '".venv\\Scripts\\python.exe" -m uvicorn api.main:app --host 127.0.0.1 --port 8000\n',
        encoding="utf-8",
        newline="\r\n",
    )
    (destination / "start-frontend.cmd").write_text(
        '@echo off\n'
        'cd /d "%~dp0frontend"\n'
        'if exist "%~dp0.tools\\node\\npm.cmd" (\n'
        '  call "%~dp0.tools\\node\\npm.cmd" run dev -- --host 127.0.0.1\n'
        ') else (\n'
        '  call npm run dev -- --host 127.0.0.1\n'
        ')\n',
        encoding="utf-8",
        newline="\r\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Recreate and install the FPT RAG system on a CPU-only VDI.")
    parser.add_argument("--destination", type=Path, default=Path.cwd() / "FPT_RAG")
    parser.add_argument("--profile", choices=("gateway", "offline"), default="gateway")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-frontend-build", action="store_true")
    parser.add_argument("--extract-only", action="store_true", help="write and verify sources without installing dependencies")
    args = parser.parse_args()

    require_python()
    destination = args.destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"Destination is not empty: {destination}. Choose an empty directory.")
    destination.mkdir(parents=True, exist_ok=True)
    write_sources(destination)
    if args.extract_only:
        print(f"Sources extracted and verified at {destination}.")
        return 0

    npm = ensure_npm(destination)

    backend = destination / "backend"
    frontend = destination / "frontend"
    venv = backend / ".venv"
    python = venv_python(venv)
    run([sys.executable, "-m", "venv", str(venv)], cwd=backend)
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], cwd=backend)
    run([str(python), "-m", "pip", "install", "-r", "requirements-vdi-minimal.txt"], cwd=backend)
    configure_env(backend, args.profile)
    run([npm, "ci"], cwd=frontend)
    if not args.skip_frontend_build:
        run([npm, "run", "build"], cwd=frontend)
    if not args.skip_tests:
        run([str(python), "tests/run_all.py"], cwd=backend)
    write_launchers(destination)

    print("\nBootstrap complete.")
    print(f"Start the API:      {destination / 'start-backend.cmd'}")
    print(f"Start the frontend: {destination / 'start-frontend.cmd'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def main() -> None:
    manifest = collect_files()
    packed = zlib.compress(json.dumps(manifest, separators=(",", ":")).encode("utf-8"), level=9)
    payload = base64.b64encode(packed).decode("ascii")
    script = RUNTIME.replace("__PAYLOAD__", payload).replace(
        "__PAYLOAD_SHA256__", hashlib.sha256(packed).hexdigest()
    )
    OUTPUT.write_text(script, encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(manifest)} source files ({OUTPUT.stat().st_size:,} bytes).")


if __name__ == "__main__":
    main()


