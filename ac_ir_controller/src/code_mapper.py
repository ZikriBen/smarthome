import json
import logging
from pathlib import Path
from typing import Dict, Any

log = logging.getLogger(__name__)


def load_codes(path: str | Path) -> Dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    # quick sanity checks
    if "off" not in data:
        raise ValueError("codes file missing 'off' key")
    for mode in ("heat", "cool"):
        if mode not in data:
            raise ValueError(f"codes file missing '{mode}'")
    log.info("Loaded AC IR codes from %s", path)
    return data

def normalize(mode: str, fan: str, temperature: int):
    normalize_mode = (mode).strip().lower()
    normalize_fan = (fan).strip().lower()
    normalize_temperature = int(temperature)

    return normalize_mode, normalize_fan, normalize_temperature

def lookup_ir(codes: Dict[str, Any], mode: str, fan: str, temperature: int) -> str:
    mode, fan, temperature = normalize(mode, fan, temperature)

    # Any payload mentioning off => OFF code
    if "off" in mode:
        return codes["off"]

    try:
        return codes[mode][fan][str(temperature)]
    except KeyError as e:
        raise ValueError(f"no code for {mode}/{fan}/{temperature}") from e
