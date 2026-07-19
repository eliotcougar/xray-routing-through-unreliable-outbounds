"""Assemble the static GitHub Pages artifact for Xray Route Lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xray_strategy_sim.webapp import catalog_payload, summary_payload  # noqa: E402


SITE_SOURCE = ROOT / "site"
DATA_SOURCE = ROOT / "data" / "monte-carlo"
WHITEPAPER = ROOT / "docs" / "xray-unreliable-routing.pdf"
PACKAGE_SOURCE = ROOT / "xray_strategy_sim"
STATIC_FILES = ("index.html", "styles.css", "app.js", "simulator-worker.js")
BROWSER_MODULES = ("model.py", "observatory.py", "strategies.py", "simulation.py", "webapp.py")


def write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")),
        encoding="utf-8",
    )


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
