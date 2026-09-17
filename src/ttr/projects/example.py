"""The worked example, built automatically on a fresh install.

A clone that starts `ttr serve` against an empty projects root would otherwise
open on an empty page, with the example two copied-out commands away. So the
first start builds it: the published extract and its alias table travel inside
the package (`example_data/`), which makes this work the same from a checkout, a
`pip install git+...` and the Docker image, none of which can rely on the
repository's `docs/` being beside them.

**Offered once per projects root, and only to an empty one.** A root that already
holds projects belongs to someone monitoring real sites, and a demo project
appearing among them would be noise. Once the example has been offered, a marker
file records it, so deleting the example is final: it does not come back on the
next start. `ttr project load-example` builds it again on request.

The copies under `example_data/` must stay byte-identical to
`docs/evaluation/data/detections_20260628-0716.csv` and
`examples/back-garden/species_aliases.yaml`; `tests/test_example_project.py`
checks that.
"""

from __future__ import annotations

import os
from importlib.resources import as_file, files
from pathlib import Path
from typing import Callable, Optional

from ..logging import get_logger
from .paths import projects_root
from .registry import Registry

logger = get_logger(__name__)

EXAMPLE_NAME = "Example Site"
EXAMPLE_SITE_NAME = "Example Village, UK"
EXAMPLE_LATITUDE = 51.5
EXAMPLE_LONGITUDE = -0.1

_DATA_DIR = "example_data"
EXTRACT_RESOURCE = "detections_20260628-0716.csv"
ALIAS_RESOURCE = "species_aliases.yaml"

#: Set to any non-empty value to stop `ttr serve` building the example.
SKIP_ENV_VAR = "TTR_NO_EXAMPLE"
#: Written to the projects root once the example has been offered.
MARKER_NAME = ".example-offered"


def build_example_project(root: Optional[Path] = None):
    """Create the example project. Returns ``(manifest, project_dir, ctx, stored)``."""
    from .extract import create_from_extract

    data = files(__package__).joinpath(_DATA_DIR)
    with as_file(data.joinpath(EXTRACT_RESOURCE)) as csv_path, \
            as_file(data.joinpath(ALIAS_RESOURCE)) as alias_path:
        return create_from_extract(
            csv_path, EXAMPLE_NAME, alias_table=alias_path,
            site_name=EXAMPLE_SITE_NAME,
            latitude=EXAMPLE_LATITUDE, longitude=EXAMPLE_LONGITUDE,
            root=root)


def ensure_example_project(root: Optional[Path] = None, *,
                           notify: Optional[Callable[[str], None]] = None) -> bool:
    """Build the example if this root has never had it offered and is empty.

    Returns True when a project was built. Never raises: the example is a
    convenience, and failing to build it must not stop the server starting. A
    failure leaves no marker, so the next start tries again.
    """
    notify = notify or (lambda message: None)
    if os.environ.get(SKIP_ENV_VAR):
        return False
    try:
        root = root or projects_root()
        marker = root / MARKER_NAME
        if marker.exists():
            return False
        if Registry.load(root).entries:
            _write_marker(marker)
            return False
        notify(f"First start: building the example project {EXAMPLE_NAME!r} "
               f"from the published data...")
        _, _, _, stored = build_example_project(root)
        _write_marker(marker)
    except Exception as exc:
        logger.warning("example_project_not_built", extra={"error": repr(exc)})
        notify(f"  the example project could not be built: {exc}")
        return False
    notify(f"  {stored} alert events loaded. Delete it at any time; it will not "
           f"be rebuilt (set {SKIP_ENV_VAR}=1 to skip this step).")
    return True


def _write_marker(marker: Path) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "The example project has been offered for this projects root and will not\n"
        "be built again automatically. `ttr project load-example` rebuilds it.\n",
        encoding="utf-8")
