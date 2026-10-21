from .rami_levy import RamiLevyAdapter
from .shufersal import ShufersalAdapter
from .super_hist import SuperHistAdapter

ADAPTERS = {"rami_levy": RamiLevyAdapter, "shufersal": ShufersalAdapter, "super_hist": SuperHistAdapter}
ALIASES = {"rami": "rami_levy", "ramilevy": "rami_levy", "rami-levy": "rami_levy", "shufersal": "shufersal",
           "שופרסל": "shufersal", "רמי לוי": "rami_levy",
           "superhist": "super_hist", "super-hist": "super_hist", "hist": "super_hist", "הסופר החברתי": "super_hist"}


def get_adapter_class(name: str):
    key = ALIASES.get(name.lower(), name.lower())
    if key not in ADAPTERS:
        raise ValueError(f"unknown site {name!r}; known: {sorted(ADAPTERS)}")
    return ADAPTERS[key]
