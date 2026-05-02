"""Export the ``/cplugapi/v1/*`` OpenAPI spec to a JSON file.

Used by CI (Track 05 T22) so the Rust desktop client can codegen
schemas. Standalone — does not boot the WebUI; mounts only the
cplugapi router on a fresh FastAPI app.

Usage::

    python scripts/export_cplugapi_openapi.py [output_path]

Defaults to ``cplugapi-openapi.json`` in the current working directory.
"""

from __future__ import annotations

import json
import sys
import types
from collections import OrderedDict
from pathlib import Path

# Make the repo root importable when the script is invoked from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _install_stubs() -> None:
    progress = types.ModuleType("modules.progress")
    progress.pending_tasks = OrderedDict()  # type: ignore[attr-defined]
    progress.current_task = None  # type: ignore[attr-defined]
    progress.finished_tasks = []  # type: ignore[attr-defined]
    sys.modules["modules.progress"] = progress

    shared = types.ModuleType("modules.shared")

    class _State:
        def interrupt(self) -> None:
            pass

    shared.state = _State()  # type: ignore[attr-defined]
    sys.modules["modules.shared"] = shared


def main() -> int:
    _install_stubs()

    from fastapi import FastAPI

    from modules.cplugapi import setup_cplugapi

    app = FastAPI(title="ControlPlugin WebUI fork — cplugapi/v1")
    setup_cplugapi(app)

    output = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("cplugapi-openapi.json")
    output.write_text(json.dumps(app.openapi(), indent=2), encoding="utf-8")
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
