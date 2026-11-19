"""Etsy refunds and cancellations, and the per-platform rule for fees on refunds (17 Nov 2026).

- Etsy Help: Order Total in the Orders CSV does not subtract fully refunded or cancelled orders.
- Etsy seller forum (not staff): Adjusted Order Total is blank when nothing changed, 0 after a full refund,
  and the total after a partial refund otherwise.
- Etsy Help: Etsy credits its transaction and processing fees back on refunds; Shopify Payments does not.
"""

from pathlib import Path

import pandas as pd
import pytest

from etsy_profit import load_shop
from etsy_profit.cli import main
from etsy_profit.etsy_import import FULLY_REFUNDED, READY, apply_etsy_orders, read_etsy_order_items, read_etsy_orders
from etsy_profit.fees import fees_returned_on_refund, order_line_fees

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


@pytest.fixture()
def shop():
    shop = load_shop(SAMPLE)
    shop.orders = shop.orders.iloc[0:0]
    return shop


def _orders_with(tmp_path, changes: dict[str, tuple[str, str]]) -> Path:
    """Sample Orders CSV with (Status, Adjusted Order Total) changed for some orders."""
    raw = pd.read_csv(SAMPLE / "etsy_orders.csv", dtype=str, keep_default_na=False)
    for order, (status, adjusted) in changes.items():
        raw.loc[raw["Order ID"] == order, ["Status", "Adjusted Order Total"]] = [status, adjusted]
    path = tmp_path / "orders.csv"
    raw.to_csv(path, index=False)
    return path


def _apply(shop, orders_path):
    items = read_etsy_order_items(SAMPLE / "etsy_order_items.csv", shop.orders, shop.products)
    return apply_etsy_orders(items, read_etsy_orders(orders_path))


def test_untouched_orders_have_no_refund(shop):
    items, warnings = _apply(shop, SAMPLE / "etsy_orders.csv")
    assert items["refund"].sum() == 0
    assert not any("refund" in w for w in warnings)


def test_full_refund_is_left_out(shop, tmp_path):
    items, warnings = _apply(shop, _orders_with(tmp_path, {"ET-1047": ("Completed", "0.00")}))
    assert items.loc[items["order_id"] == "ET-1047", "status"].tolist() == [FULLY_REFUNDED]
    assert any("1 fully refunded or cancelled Etsy order(s) left out: ET-1047" in w for w in warnings)


def test_partial_refund_is_subtracted_once_and_totals_still_agree(shop, tmp_path):
    # ET-2001: Order Total 41.22 incl. 2.72 tax; $10 refunded -> adjusted 31.22. The adjusted total includes
    # tax, so only the pre-tax share comes off sales: 10 x 38.50 / 41.22 = 9.34.
    items, warnings = _apply(shop, _orders_with(tmp_path, {"ET-2001": ("Completed", "31.22")}))
    order = items[items["order_id"] == "ET-2001"]
    assert order["refund"].tolist() == [9.34, 0.0]
    assert (order["status"] == READY).all()
    assert not any("differ from Etsy" in w for w in warnings)
    assert any("also had sales tax: ET-2001" in w for w in warnings)


def test_unusual_status_is_flagged_but_kept(shop, tmp_path):
    items, warnings = _apply(shop, _orders_with(tmp_path, {"ET-2001": ("Canceled", "")}))
    assert any("ET-2001 (Canceled)" in w for w in warnings)
    assert (items.loc[items["order_id"] == "ET-2001", "status"] == READY).all()


def test_fee_rule_follows_the_platform(shop):
    returned = fees_returned_on_refund(shop.fees)
    assert returned.to_dict() == {"Etsy": True, "Shopify": False, "Instagram": False}
    no_column = shop.fees.drop(columns=["fees_returned_on_refund"])
    assert fees_returned_on_refund(no_column).to_dict() == {"Etsy": True, "Shopify": False, "Instagram": False}

    # The same $42.50 kept after a $10 refund: Etsy charges on what was kept, Shopify on what was charged.
    orders = pd.DataFrame({"order_id": ["E", "S"], "channel": ["Etsy", "Shopify"], "qty": [1, 1],
                           "order_total": [42.50, 42.50], "refund": [10.0, 10.0]})
    fees, _ = order_line_fees(orders, shop.fees)
    assert fees.tolist() == [round(42.50 * 0.095 + 0.25 + 0.20, 2), round(52.50 * 0.029 + 0.30, 2)]


def test_cli_warns_when_only_order_items_are_given(capsys):
    assert main(["--data", str(SAMPLE), "--etsy-csv", str(SAMPLE / "etsy_order_items.csv")]) == 0
    assert "Refunds and cancellations are only in Etsy's Orders file" in capsys.readouterr().out
