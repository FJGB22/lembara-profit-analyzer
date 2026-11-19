from pathlib import Path

import pandas as pd
import pytest

from etsy_profit import analyze, load_shop
from etsy_profit.etsy_import import READY, UNKNOWN_SKU, merge_ready_items
from etsy_profit.shopify_import import read_shopify_orders

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


@pytest.fixture()
def shop():
    return load_shop(SAMPLE)


@pytest.fixture()
def imported(shop):
    return read_shopify_orders(SAMPLE / "shopify_orders_export.csv", shop.orders, shop.products, default_shipping_cost=4.2)


def test_cancelled_and_fully_refunded_orders_are_skipped(imported):
    assert "#1052" not in imported["order_id"].tolist()
    assert "#1053" not in imported["order_id"].tolist()


def test_order_level_money_only_on_first_line(imported):
    order = imported[imported["order_id"] == "#1050"]
    assert order["sku"].tolist() == ["FW-003", "FW-004"]
    assert order["qty"].tolist() == [1.0, 2.0]
    assert order["shipping_charged"].tolist() == [5.5, 0.0]
    assert order["discount"].tolist() == [5.0, 0.0]
    assert order["shipping_cost"].tolist() == [4.2, 0.0]
    assert order["date"].tolist() == [pd.Timestamp("2026-11-04"), pd.Timestamp("2026-11-04")]


def test_partial_refund_is_kept_apart_from_discount(imported):
    order = imported[imported["order_id"] == "#1051"]
    assert order["discount"].tolist() == [0.0]
    assert order["refund"].tolist() == [10.0]


def test_statuses_and_lowercase_sku_header(imported):
    assert imported["status"].tolist() == [READY, READY, READY, UNKNOWN_SKU]


def test_shopify_orders_flow_into_report(shop, imported):
    shop.orders = merge_ready_items(shop.orders, imported)
    report = analyze(shop)
    order = report.orders.query("order_id == '#1050'")
    assert order["order_total"].round(2).tolist() == [30.50, 28.00]
    assert order["platform_fees"].round(2).tolist() == [1.18, 0.81]


def test_missing_columns_named(shop, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Name,Created at\n#1,2026-11-01\n")
    with pytest.raises(ValueError, match="Lineitem quantity"):
        read_shopify_orders(bad, shop.orders, shop.products)
