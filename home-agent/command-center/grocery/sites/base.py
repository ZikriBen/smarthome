"""SiteAdapter interface. Every supermarket implements the same 5 operations so the shared
picker/report/compare code never needs to know which site it talks to."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..model import Product


def _accepts_size(searcher) -> bool:
    import inspect
    try:
        return "size" in inspect.signature(searcher.search).parameters
    except (TypeError, ValueError):
        return False


class SiteAdapter(ABC):
    name: str = "base"
    display_name: str = "base"
    default_step_kg: float = 0.5
    system_line_ids: tuple = ()
    robots = None              # RobotsPolicy or None (unknown / not fetched)
    crawl_delay: float = 0.0   # minimum seconds between requests for this site
    deep_size: int = 40        # page size for pack_preference items (Rami Levy size=40 verified live)

    def __init__(self, searcher=None, store: Optional[str] = None):
        self.searcher = searcher   # object with .search(term) -> list[raw]  (live or fixtures)
        self.store = store

    # 1. search: raw site records for a term (no filtering)
    def search(self, term: str, size: Optional[int] = None) -> List[dict]:
        if size is None or not _accepts_size(self.searcher):
            return self.searcher.search(term)
        return self.searcher.search(term, size=size)

    # 2. normalize: raw -> Product (pack, unit price, promo, availability, categories)
    @abstractmethod
    def normalize(self, raw: dict, expected_unit: Optional[str] = None) -> Optional[Product]:
        ...

    # 3. cart_get: current cart {product_id: qty} for the logged-in session (phase 2)
    @abstractmethod
    def cart_get(self, session, client) -> Dict[str, float]:
        ...

    # 4. cart_merge_post: merge `ours` into the cart and write it; returns a diff dict (phase 2)
    @abstractmethod
    def cart_merge_post(self, session, client, ours: Dict[str, float], policy: str = "max",
                        plan_store: Optional[str] = None, log=print) -> dict:
        ...

    # 5. checkout link (Ben opens it and pays himself; we NEVER check out)
    @property
    @abstractmethod
    def checkout_url(self) -> str:
        ...

    @abstractmethod
    def load_session(self, path: str):
        ...

    @property
    @abstractmethod
    def default_session_path(self) -> str:
        ...

    def make_live_searcher(self, client, size: int = 10):
        raise NotImplementedError
