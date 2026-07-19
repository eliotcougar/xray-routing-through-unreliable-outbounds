"""Assemble the static GitHub Pages artifact for Xray Route Lab."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xray_strategy_sim.webapp import catalog_payload, summary_payload  # noqa: E402


SITE_SOURCE = ROOT / "site"
DATA_SOURCE = ROOT / "data" / "monte-carlo"
WHITEPAPER = ROOT / "docs" / "xray-unreliable-routing.pdf"
PACKAGE_SOURCE = ROOT / "xray_strategy_sim"
STATIC_FILES = ("index.html", "styles.css", "app.js", "simulator-worker.js")
BROWSER_MODULES = ("model.py", "observatory.py", "strategies.py", "simulation.py", "webapp.py")
PYODIDE_VERSION = "314.0.2"
PYODIDE_CORE_ARCHIVE = f"pyodide-core-{PYODIDE_VERSION}.tar.bz2"
PYODIDE_CORE_URL = (
    "https://github.com/pyodide/pyodide/releases/download/"
    f"{PYODIDE_VERSION}/{PYODIDE_CORE_ARCHIVE}"
)
PYODIDE_CORE_SHA256 = "86e3d5e0cbd39b1def1e424b3f1abdcc9edc66ae200fa5280ae8825bf71799ec"
PYODIDE_RUNTIME_FILES = (
    "pyodide.mjs",
    "pyodide.asm.mjs",
    "pyodide.asm.wasm",
    "pyodide-lock.json",
    "python_stdlib.zip",
)


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        encoding="utf-8",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_pyodide_core(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / PYODIDE_CORE_ARCHIVE
    if archive.is_file() and file_sha256(archive) == PYODIDE_CORE_SHA256:
        return archive

    partial = archive.with_suffix(archive.suffix + ".part")
    request = Request(PYODIDE_CORE_URL, headers={"User-Agent": "xray-route-lab-pages-builder/1.0"})
    with urlopen(request) as response, partial.open("wb") as destination:
        shutil.copyfileobj(response, destination)
    actual_hash = file_sha256(partial)
    if actual_hash != PYODIDE_CORE_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"Pyodide core checksum mismatch: expected {PYODIDE_CORE_SHA256}, got {actual_hash}"
        )
    partial.replace(archive)
    return archive


def install_pyodide_runtime(output: Path) -> None:
    archive = download_pyodide_core(ROOT / "build" / "cache")
    runtime_dir = output / "pyodide"
    runtime_dir.mkdir(parents=True)
    with tarfile.open(archive, "r:bz2") as bundle:
        for filename in PYODIDE_RUNTIME_FILES:
            member_name = f"pyodide/{filename}"
            member = bundle.getmember(member_name)
            source = bundle.extractfile(member)
            if source is None:
                raise RuntimeError(f"Pyodide runtime member is not a file: {member_name}")
            with source, (runtime_dir / filename).open("wb") as destination:
                shutil.copyfileobj(source, destination)


def build(output: Path) -> None:
    output = output.resolve()
    build_root = (ROOT / "build").resolve()
    try:
        output.relative_to(build_root)
    except ValueError as error:
        raise SystemExit(f"Pages output must stay under {build_root}") from error

    if output.exists():
        shutil.rmtree(output)
    data_dir = output / "data"
    package_dir = output / "python" / "xray_strategy_sim"
    data_dir.mkdir(parents=True)
    package_dir.mkdir(parents=True)

    for filename in STATIC_FILES:
        shutil.copy2(SITE_SOURCE / filename, output / filename)
    for filename in BROWSER_MODULES:
        shutil.copy2(PACKAGE_SOURCE / filename, package_dir / filename)
    (package_dir / "__init__.py").write_text(
        '"""Minimal package marker for the browser simulator."""\n',
        encoding="utf-8",
    )

    write_json(data_dir / "catalog.json", catalog_payload())
    write_json(data_dir / "summary.json", summary_payload(DATA_SOURCE))
    install_pyodide_runtime(output)
    shutil.copy2(WHITEPAPER, output / WHITEPAPER.name)
    (output / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Built GitHub Pages artifact: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build" / "pages")
    args = parser.parse_args()
    build(args.output)


if __name__ == "__main__":
    main()
