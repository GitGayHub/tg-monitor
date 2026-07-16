"""Unit tests for near-duplicate detection and min_price floor (no file I/O)."""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m
from app import is_recently_sent, description_similarity, parse_price
from cfg import DEFAULT_CONFIG, ConfigManager


def reset_sent():
    m.sent_offers = {}
    m.sent_by_seller = {}


def remember(offer_id, price, description, seller=None):
    """In-memory only — do not touch sent_offers.json on disk."""
    record = {
        "offer_id": offer_id,
        "price": price,
        "description": description,
        "timestamp": time.time(),
        "seller": seller,
    }
    m.sent_offers[offer_id] = record
    if seller:
        key = seller.strip().lower()
        m.sent_by_seller.setdefault(key, []).append(record)


def main():
    d1 = (
        "⭐️99 СКИНОВ⭐️ГАРАНТИЯ⭐️ОМЕГА⭐️ДУШЕГУБ⭐️Take The L⭐️OLD PVE"
        "⭐️5 exclusives⭐️70 кирок⭐️, Продажа, скины 94"
    )
    d2 = (
        "⭐️99 СКИНОВ⭐️ГАРАНТИЯ⭐️ОМЕГА⭐️ДУШЕГУБ⭐️Take The L⭐️OLD PVE"
        "⭐️Blue Squire⭐️5 exclusives⭐️, Продажа, скины 99"
    )

    # 1) Screenshot pair similarity
    sim = description_similarity(d1, d2)
    print(f"similarity chapsedshop pair: {sim:.3f}")
    assert sim >= 0.75, sim

    # 2) Near-duplicate same seller blocked after first send
    reset_sent()
    remember("72231496", 597.56, d1, "chapsedshop")
    blocked = is_recently_sent("72231199", "chapsedshop", d2, 597.56)
    print(f"near-dup blocked: {blocked}")
    assert blocked is True

    # 3) Different seller with same text is allowed
    reset_sent()
    remember("1", 500.0, d1, "sellerA")
    allowed = is_recently_sent("2", "sellerB", d1, 500.0)
    print(f"different seller allowed: {not allowed}")
    assert allowed is False

    # 4) Exact offer id blocked
    reset_sent()
    remember("99", 200.0, "test offer", "x")
    assert is_recently_sent("99", "x", "test offer", 200.0) is True
    print("exact id blocked: True")

    # 5) Significant price drop (>=10%) allows re-notify of similar listing
    reset_sent()
    remember("10", 1000.0, d1, "chapsedshop")
    allowed_drop = is_recently_sent("11", "chapsedshop", d2, 850.0)  # 15%
    print(f"15% price drop allowed: {not allowed_drop}")
    assert allowed_drop is False

    # 6) Minor price drop still blocked
    blocked_small = is_recently_sent("12", "chapsedshop", d2, 950.0)  # 5%
    print(f"5% drop blocked: {blocked_small}")
    assert blocked_small is True

    # 7) Clearly different inventory same seller — allowed
    reset_sent()
    remember(
        "20",
        600.0,
        "Black Knight OG STW full access mail included 200 skins",
        "sellerZ",
    )
    other_desc = "WONDER OLD PVE PLAYSTATION ONLY 223 SKINS tournament farm"
    other_sim = description_similarity(
        "Black Knight OG STW full access mail included 200 skins", other_desc
    )
    other_blocked = is_recently_sent("21", "sellerZ", other_desc, 600.0)
    print(f"different inventory sim={other_sim:.3f} blocked={other_blocked}")
    assert other_sim < 0.55
    assert other_blocked is False

    # 8) min_price default and parse
    assert DEFAULT_CONFIG["min_price"] == 111
    cm = ConfigManager.__new__(ConfigManager)
    cm.data = {}
    assert cm.min_price == 111
    cm.data = {"min_price": 111}
    assert cm.min_price == 111

    assert parse_price("1.22₽") == 1.22
    assert parse_price("1.22₽") < 111
    assert parse_price("597.56₽") >= 111
    assert parse_price("111₽") >= 111
    assert parse_price("110.99₽") < 111
    print(f"fake 1.22 filtered by min_price: {parse_price('1.22₽') < cm.min_price}")

    # 9) Same price + moderate similarity still blocked
    reset_sent()
    remember(
        "a1",
        400.0,
        "OLD PVE Omega Take The L 80 skins guarantee exclusive",
        "spamSeller",
    )
    d_mod = "OLD PVE Omega Take The L 82 skins guarantee exclusive pack"
    sim_mod = description_similarity(
        "OLD PVE Omega Take The L 80 skins guarantee exclusive", d_mod
    )
    print(f"moderate sim same price: {sim_mod:.3f}")
    assert sim_mod >= 0.55
    assert is_recently_sent("a2", "spamSeller", d_mod, 400.0) is True

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
