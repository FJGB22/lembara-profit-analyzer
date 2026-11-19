"""Shops without SKUs: order lines are matched by item name instead of being silently dropped."""

import csv
from pathlib import Path

import pandas as pd
import pytest

from etsy_profit import analyze, load_shop
from etsy_profit.etsy_import import READY, UNKNOWN_SKU, match_skus, merge_ready_items
from etsy_profit.loaders import _read_item_names
from etsy_profit.shopify_import import read_shopify_orders

SAMPLE = Path(__file__).parent.parent / "data" / "sample"
EXPORT = SAMPLE / "shopify_test_store_export.csv"


@pytest.fixture()
def shop():
    shop = load_shop(SAMPLE)
    shop.orders = shop.orders.iloc[0:0]
    return shop


def _lines(names, skus=None):
    return pd.DataFrame({"item_name": names, "sku": skus or [""] * len(names),
                         "qty": [1.0] * len(names), "unit_price": [10.0] * len(names)})


def _export_without_skus(tmp_path) -> Path:
    with EXPORT.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    sku = [name.lower() for name in rows[0]].index("lineitem sku")
    for row in rows[1:]:
        row[sku] = ""
    path = tmp_path / "no_skus.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        csv.writer(file).writerows(rows)
    return path


def test_shopify_export_without_skus_gives_the_same_report(shop, tmp_path):
    imported = read_shopify_orders(_export_without_skus(tmp_path), shop.orders, shop.products)
    assert (imported["status"] == READY).all()
    assert any("13 order line(s) had no known SKU and were matched by product name" in w
               for w in imported.attrs["warnings"])
    shop.orders = merge_ready_items(shop.orders, imported)
    assert analyze(shop).summary["sales"] == 360.40


def test_name_match_ignores_case_and_extra_spaces(shop):
    matched, _ = match_skus(_lines(["  lavender soy   CANDLE 8oz "]), shop.products)
    assert matched["sku"].tolist() == ["FW-001"]


def test_item_names_file_maps_long_listing_titles(shop):
    title = "Lavender Soy Candle | Hand Poured 8oz Amber Jar | Gift for Her"
    mapping = pd.DataFrame({"item_name": [title], "sku": ["FW-001"]})
    matched, warnings = match_skus(_lines([title]), shop.products, mapping)
    assert matched["sku"].tolist() == ["FW-001"]
    assert "matched by item_names.csv" in warnings[0]


def test_existing_sku_is_never_overwritten(shop):
    matched, warnings = match_skus(_lines(["Lavender Soy Candle 8oz"], ["FW-002"]), shop.products)
    assert matched["sku"].tolist() == ["FW-002"]
    assert warnings == []


def test_name_used_by_two_products_is_not_guessed(shop):
    products = pd.concat([shop.products, shop.products.iloc[[0]].assign(sku="FW-099")], ignore_index=True)
    matched, warnings = match_skus(_lines(["Lavender Soy Candle 8oz"]), products)
    assert matched["sku"].tolist() == [""]
    assert "left out of this report" in warnings[0]


def test_unmatched_lines_report_lost_sales(shop):
    matched, warnings = match_skus(_lines(["Shipping protection", "Mystery box", "Mystery box"]), shop.products)
    assert matched["sku"].tolist() == ["", "", ""]
    assert "3 order line(s) worth $30.00" in warnings[0]
    assert "Mystery box (2), Shipping protection (1)" in warnings[0]


def test_unmatched_lines_stay_out_of_the_report(shop, tmp_path):
    path = tmp_path / "one.csv"
    path.write_text("Name,Created at,Lineitem quantity,Lineitem name,Lineitem price,Lineitem sku,Total,Taxes\n"
                    "#5001,2026-11-10 10:00:00 -0500,1,Mystery box,12.00,,12.00,0.00\n", encoding="utf-8")
    imported = read_shopify_orders(path, shop.orders, shop.products)
    assert imported["status"].tolist() == [UNKNOWN_SKU]


def test_item_names_file_is_loaded_and_checked(tmp_path):
    assert _read_item_names(tmp_path) is None
    (tmp_path / "item_names.csv").write_text("item_name,sku\n Big Candle ,FW-001\n", encoding="utf-8")
    assert _read_item_names(tmp_path).to_dict("records") == [{"item_name": "Big Candle", "sku": "FW-001"}]
    (tmp_path / "item_names.csv").write_text("name,sku\nBig Candle,FW-001\n", encoding="utf-8")
    with pytest.raises(ValueError, match="item_name"):
        _read_item_names(tmp_path)
