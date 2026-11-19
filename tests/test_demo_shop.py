"""The expected numbers come from the original Google Sheets demo (Fern & Wick Candle Co.).
The Python version must reproduce the spreadsheet exactly."""

from pathlib import Path

import pandas as pd
import pytest

from etsy_profit import analyze, load_shop
from etsy_profit.costing import recipe_costs
from etsy_profit.etsy_import import ALREADY_IMPORTED, READY, UNKNOWN_SKU, merge_ready_items, read_etsy_order_items

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


@pytest.fixture(scope="module")
def shop():
    return load_shop(SAMPLE)


@pytest.fixture(scope="module")
def report(shop):
    return analyze(shop)


def test_dashboard_totals_match_spreadsheet(report):
    s = report.summary
    assert s["sales"] == 1379.50
    assert s["platform_fees"] == 124.26
    assert s["product_and_shipping_costs"] == 478.11
    assert s["net_profit"] == 777.13
    assert round(s["profit_margin"], 3) == 0.563
    assert s["orders"] == 49


def test_recipe_costs_match_spreadsheet(shop):
    costs, warnings = recipe_costs(shop.recipes, shop.materials)
    assert costs.to_dict() == {"FW-001": 5.16, "FW-002": 5.31, "FW-003": 7.49}
    assert warnings == []


def test_single_order_line(report):
    first = report.orders.iloc[0]
    assert first["order_id"] == "ET-1001"
    assert first["order_total"] == 16.00
    assert first["platform_fees"] == 1.97
    assert first["product_cost"] == 3.40
    assert first["net_profit"] == 5.53


def test_profit_by_channel(report):
    table = report.by_channel.set_index("channel")
    assert table.loc["Etsy"].tolist() == [993.00, 110.84, 536.66]
    assert table.loc["Shopify"].tolist() == [338.50, 13.42, 199.97]
    assert table.loc["Instagram"].tolist() == [48.00, 0.00, 40.50]


def test_top_products(report):
    top = report.by_product.head(5)[["product", "units_sold", "net_profit"]]
    assert top.values.tolist() == [
        ["Cedar & Smoke Candle 8oz", 14, 202.72],
        ["Lavender Soy Candle 8oz", 15, 196.21],
        ["Candle Gift Box (3 mini)", 4, 132.47],
        ["Wax Melt Sampler (6 pack)", 12, 97.64],
        ["Citrus Grove Candle 12oz", 3, 59.26],
    ]


def test_months(report):
    assert report.by_month.values.tolist() == [["2026-09", 602.50, 328.79], ["2026-10", 777.00, 448.34]]


def test_restock(report):
    assert report.restock[["product", "in_stock", "status"]].values.tolist() == [
        ["Cedar & Smoke Candle 8oz", 6, "REORDER"],
        ["Candle Gift Box (3 mini)", 2, "REORDER"],
        ["Wick Trimmer - Brass", 0, "OUT OF STOCK"],
    ]


def test_losing_orders_are_the_matchboxes(report):
    assert report.losing_orders["order_id"].tolist() == ["ET-1045", "SH-1049"]


def test_etsy_import_statuses(shop):
    imported = read_etsy_order_items(SAMPLE / "etsy_order_items.csv", shop.orders, shop.products)
    assert imported["status"].tolist() == [READY, READY, ALREADY_IMPORTED, UNKNOWN_SKU]
    assert imported["shipping_charged"].tolist() == [4.5, 0.0, 0.0, 5.5]
    assert imported["date"].iloc[0] == pd.Timestamp("2026-11-01")


def test_etsy_shipping_cost_only_on_first_line(shop):
    imported = read_etsy_order_items(
        SAMPLE / "etsy_order_items.csv", shop.orders, shop.products, default_shipping_cost=4.2
    )
    assert imported["shipping_cost"].tolist() == [4.2, 0.0, 4.2, 4.2]


def test_merge_adds_only_ready_lines(shop):
    imported = read_etsy_order_items(SAMPLE / "etsy_order_items.csv", shop.orders, shop.products)
    merged = merge_ready_items(shop.orders, imported)
    assert len(merged) == len(shop.orders) + 2
    assert merged["order_id"].tolist()[-2:] == ["ET-2001", "ET-2001"]


def test_wrong_etsy_column_name_gives_clear_error(shop, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Date,Order ID,SKU,Quantity,Price,Order Shipping,Item Name\n")
    with pytest.raises(ValueError, match="Sale Date"):
        read_etsy_order_items(bad, shop.orders, shop.products)


def test_etsy_coupon_discount_counted_once_per_order(shop, tmp_path):
    csv = tmp_path / "coupon.csv"
    csv.write_text(
        "Sale Date,Item Name,Quantity,Price,Discount Amount,Shipping Discount,Order Shipping,Order ID,SKU\n"
        "11/03/26,Lavender Soy Candle 8oz,1,22,4.40,4.50,4.50,ET-3001,FW-001\n"
        "11/03/26,Matchbox - Botanical,1,6,4.40,4.50,4.50,ET-3001,FW-007\n"
    )
    imported = read_etsy_order_items(csv, shop.orders, shop.products)
    assert imported["discount"].tolist() == [8.90, 0.0]

    merged = merge_ready_items(shop.orders, imported)
    other = type(shop)(shop.products, shop.materials, shop.recipes, merged, shop.fees)
    order = analyze(other).orders.query("order_id == 'ET-3001'")
    assert order["order_total"].round(2).tolist() == [17.60, 6.00]


def test_discount_column_is_optional_in_orders_csv(report):
    assert (report.orders["discount"] == 0).all()


def test_unknown_channel_is_warned_not_crashed(shop):
    orders = shop.orders.copy()
    orders.loc[0, "channel"] = "TikTok Shop"
    other = type(shop)(shop.products, shop.materials, shop.recipes, orders, shop.fees)
    report = analyze(other)
    assert any("TikTok Shop" in warning for warning in report.warnings)
    assert report.orders.loc[0, "platform_fees"] == 0.0
