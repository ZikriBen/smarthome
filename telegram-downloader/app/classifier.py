import sys
from pathlib import Path


def _add_shared_path() -> None:
    candidates = (
        # Running directly from the smarthome repository.
        Path(__file__).resolve().parents[2] / "shared",

        # Running inside Docker.
        Path("/shared"),
    )

    for candidate in candidates:
        if candidate.exists():
            path = str(candidate)

            if path not in sys.path:
                sys.path.insert(0, path)

            return

    raise RuntimeError(
        "Unable to locate shared media_common package"
    )


_add_shared_path()

from media_common.classifier import *  # noqa: F401,F403,E402
