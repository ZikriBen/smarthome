from .rami_levy import RamiLevyAdapter
from .shufersal import ShufersalAdapter

ADAPTERS = {"rami_levy": RamiLevyAdapter, "shufersal": ShufersalAdapter}
ALIASES = {"rami": "rami_levy", "ramilevy": "rami_levy", "rami-levy": "rami_levy", "shufersal": "shufersal",
           "שופרסל": "shufersal", "רמי לוי": "rami_levy"}


def get_adapter_class(name: str):
    key = ALIASES.get(name.lower(), name.lower())
    if key not in ADAPTERS:
        raise ValueError(f"unknown site {name!r}; known: {sorted(ADAPTERS)}")
    return ADAPTERS[key]
