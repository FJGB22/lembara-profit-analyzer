"""Tests against a real Shopify Orders export (fern-and-wick-test development store, 16–17 Nov 2026).

The ten test orders were made on purpose: order discount (#1002), partial refund (#1005), full refund
(#1006), cancelled (#1007), a line-item discount (#1008), an automatic 10% product discount on 3 units plus
New York sales tax (#1009) and a taxed order with one item returned, tax refunded with it (#1010).
Staff name replaced; the only buyer is a made-up "Test Buyer NY" at a placeholder address.
"""

import csv
from pathlib import Path

import pandas as pd
import pytest

from etsy_profit import analyze, load_shop
from etsy_profit.client_report import build_report_html
from etsy_profit.etsy_import import READY, merge_ready_items
from etsy_profit.shopify_import import read_shopify_orders

SAMPLE = Path(__file__).parent.parent / "data" / "sample"
EXPORT = SAMPLE / "shopify_test_store_export.csv"

# Totals as shown in Shopify admin, minus refunds.
EXPECTED_TOTALS = {"#1001": 26.50, "#1002": 58.50, "#1003": 6.00, "#1004": 48.00, "#1005": 42.50, "#1008": 59.60,
                   "#1009": 87.30,   # 44 + 3 x 14 - 4.20 automatic discount + 5.50 shipping; tax 7.75 left out
                   "#1010": 32.00}   # 24 + 8 shipping; the returned $22 item and its $1.95 tax left out


@pytest.fixture()
def shop():
    shop = load_shop(SAMPLE)
    shop.orders = shop.orders.iloc[0:0]  # only the test store's orders
    return shop


def _import(shop, path=EXPORT):
    return read_shopify_orders(path, shop.orders, shop.products)


def _edited_export(tmp_path, old: str, new: str) -> Path:
    text = EXPORT.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "edited.csv"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def _report(shop, imported):
    shop.orders = merge_ready_items(shop.orders, imported)
    return analyze(shop)


def test_refunded_and_cancelled_orders_are_left_out_and_noted(shop):
    imported = _import(shop)
    assert sorted(imported["order_id"].unique()) == sorted(EXPECTED_TOTALS)
    assert (imported["status"] == READY).all()
    skipped, taxed = imported.attrs["warnings"]
    assert "#1006, #1007" in skipped
    assert "2 partially refunded Shopify order(s): #1005, #1010" in taxed
    assert "Order(s) with tax (#1010)" in taxed


def test_order_totals_match_shopify(shop):
    report = _report(shop, _import(shop))
    totals = report.orders.groupby("order_id")["order_total"].sum().round(2).to_dict()
    assert totals == EXPECTED_TOTALS
    assert report.summary["sales"] == 360.40
    assert report.summary["orders"] == 8


def test_partial_refund_lowers_sales_but_not_the_fee_base(shop):
    imported = _import(shop)
    order = imported[imported["order_id"] == "#1005"]
    assert order[["discount", "refund"]].values.tolist() == [[0.0, 10.0]]
    report = _report(shop, imported)
    fee = report.orders.loc[report.orders["order_id"] == "#1005", "platform_fees"].tolist()
    assert fee == [1.82]  # 2.9% of $52.50 charged + $0.30, not of $42.50 kept


def test_line_item_discount_stays_on_its_product(shop):
    imported = _import(shop)
    order = imported[imported["order_id"] == "#1008"].set_index("sku")
    assert order["discount"].to_dict() == {"FW-002": 2.40, "FW-001": 0.0, "FW-008": 0.0}


def test_line_item_discount_not_moved_to_first_row(shop, tmp_path):
    # Same order #1008, but the discounted Cedar candle becomes the second row instead of the first:
    # swap only the line-item columns of its first two rows.
    with EXPORT.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    header = rows[0]
    first_row = next(i for i, row in enumerate(rows) if row[0] == "#1008")
    line_columns = [i for i, name in enumerate(header) if name.lower().startswith("lineitem")]
    first, second = rows[first_row], rows[first_row + 1]
    for i in line_columns:
        first[i], second[i] = second[i], first[i]
    path = tmp_path / "swapped.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerows(rows)
    order = _import(shop, path).query("order_id == '#1008'")
    assert order["sku"].tolist() == ["FW-001", "FW-002", "FW-008"]
    assert order["discount"].tolist() == [0.0, 2.40, 0.0]


def test_line_item_discount_is_for_the_whole_line(shop):
    # Shopify answered the open question: 10% off 3 x $14 shows Lineitem discount 4.20, not 1.40 per unit.
    order = _import(shop).query("order_id == '#1009'").set_index("sku")
    assert order["discount"].to_dict() == {"FW-001": 0.0, "FW-004": 4.20}


def test_refund_counts_only_its_pre_tax_share(shop):
    # #1010: Refunded Amount 23.95 = $22.00 item + $1.95 tax. Only the $22.00 lowers sales.
    order = _import(shop).query("order_id == '#1010'")
    assert order["refund"].tolist() == [22.00, 0.0]


def test_cancelled_order_skipped_even_when_still_marked_paid(shop, tmp_path):
    path = _edited_export(tmp_path, "#1007,,refunded,", "#1007,,paid,")
    assert "#1007" not in _import(shop, path)["order_id"].tolist()


def test_date_is_the_shops_local_date(shop, tmp_path):
    path = _edited_export(tmp_path, "2026-11-16 04:11:14 -0500,1,Lavender",
                          "2026-11-16 21:30:00 -0500,1,Lavender")
    order = _import(shop, path).query("order_id == '#1001'")
    assert order["date"].tolist() == [pd.Timestamp("2026-11-16")]


def test_total_that_does_not_add_up_is_flagged(shop, tmp_path):
    path = _edited_export(tmp_path, "USD,22.00,4.50,0.00,26.50,", "USD,22.00,4.50,0.00,30.00,")
    warnings = _import(shop, path).attrs["warnings"]
    assert any("#1001 (ours 26.50, Shopify 30.00)" in warning for warning in warnings)


def test_low_stock_wick_trimmer_flagged(shop):
    report = _report(shop, _import(shop))
    trimmer = report.by_product.set_index("sku").loc["FW-008"]
    assert (trimmer["in_stock"], trimmer["status"]) == (2, "REORDER")


def test_shopify_only_report_has_no_etsy_text(shop):
    page = build_report_html(_report(shop, _import(shop)), "Test store", shop.fees)
    assert "Etsy" not in page
    assert "<b>Shopify:</b> 2.9%" in page
