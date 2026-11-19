"""Checks added after comparing the importers with published export layouts (16 Nov 2026).

- Etsy's Orders CSV (one row per order) settles shipping and discounts that Order Items repeats on every line.
- Our order totals are cross-checked against Etsy's Order Total minus Sales Tax.
- Shopify orders whose Discount Amount equals the refund are flagged (community reports, not official).
- Files re-saved by Excel on Windows (cp1252) still load.
"""

from pathlib import Path

import pytest

from etsy_profit import load_shop
from etsy_profit.cli import main
from etsy_profit.csv_files import read_text_csv
from etsy_profit.etsy_import import apply_etsy_orders, read_etsy_order_items, read_etsy_orders
from etsy_profit.shopify_import import read_shopify_orders

SAMPLE = Path(__file__).parent.parent / "data" / "sample"
ITEMS_HEADER = ("Sale Date,Item Name,Buyer,Quantity,Price,Coupon Code,Coupon Details,Discount Amount,Shipping Discount,"
                "Order Shipping,Order Sales Tax,Item Total,Currency,Transaction ID,Listing ID,Date Paid,Date Shipped,"
                "Ship Name,Ship Address1,Ship Address2,Ship City,Ship State,Ship Zipcode,Ship Country,Order ID,"
                "Variations,Order Type,Listings Type,Payment Type,InPerson Discount,InPerson Location,VAT Paid by Buyer,SKU")
ORDERS_HEADER = "Sale Date,Order ID,Order Value,Discount Amount,Shipping Discount,Shipping,Sales Tax,Order Total"


@pytest.fixture()
def shop():
    return load_shop(SAMPLE)


def _items_file(tmp_path, discount_per_line: str) -> Path:
    path = tmp_path / "items.csv"
    rows = [
        f"11/03/26,Lavender Soy Candle 8oz,b,1,22,FALL10,10% off,{discount_per_line},0,4.5,0,22,USD,1,,,,,,,,,,US,ET-3001,,online,listing,cc,,,,FW-001",
        f"11/03/26,Cedar & Smoke Candle 8oz,b,1,24,FALL10,10% off,{discount_per_line},0,4.5,0,24,USD,2,,,,,,,,,,US,ET-3001,,online,listing,cc,,,,FW-002",
    ]
    path.write_text(ITEMS_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _orders_file(tmp_path, order_total: str) -> Path:
    path = tmp_path / "orders.csv"
    path.write_text(ORDERS_HEADER + f"\n11/03/26,ET-3001,46.00,4.60,0,4.50,1.20,{order_total}\n", encoding="utf-8")
    return path


def test_sample_orders_file_agrees_with_order_items(shop):
    items = read_etsy_order_items(SAMPLE / "etsy_order_items.csv", shop.orders, shop.products)
    merged, warnings = apply_etsy_orders(items, read_etsy_orders(SAMPLE / "etsy_orders.csv"))
    order = merged[merged["order_id"] == "ET-2001"]
    assert order["shipping_charged"].tolist() == [4.5, 0.0]
    assert warnings == []


def test_orders_file_sets_discount_once_per_order(shop, tmp_path):
    # Order Items repeats the 4.60 coupon on both lines; the Orders file says 4.60 for the whole order.
    items = read_etsy_order_items(_items_file(tmp_path, "4.60"), shop.orders, shop.products)
    merged, warnings = apply_etsy_orders(items, read_etsy_orders(_orders_file(tmp_path, "47.10")))
    assert merged["discount"].tolist() == [4.6, 0.0]
    assert warnings == []   # 22 + 24 + 4.50 - 4.60 = 45.90 = 47.10 - 1.20 sales tax


def test_mismatched_total_is_reported(shop, tmp_path):
    items = read_etsy_order_items(_items_file(tmp_path, "4.60"), shop.orders, shop.products)
    _, warnings = apply_etsy_orders(items, read_etsy_orders(_orders_file(tmp_path, "51.70")))
    assert len(warnings) == 1
    assert "ET-3001 (ours 45.90, Etsy 50.50)" in warnings[0]


def test_orders_missing_from_orders_file_are_reported(shop, tmp_path):
    items = read_etsy_order_items(SAMPLE / "etsy_order_items.csv", shop.orders, shop.products)
    _, warnings = apply_etsy_orders(items, read_etsy_orders(_orders_file(tmp_path, "47.10")))
    assert any("not in the Orders CSV" in w and "ET-2001" in w for w in warnings)


def test_orders_file_missing_column_is_named(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Order ID,Postage\nET-1,4.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="shipping -> 'Shipping'"):
        read_etsy_orders(bad)
    renamed = read_etsy_orders(bad, column_names={"shipping": "Postage", "discount_amount": "Postage"})
    assert renamed.loc["ET-1", "shipping"] == 4.5


def test_excel_cp1252_file_still_loads(tmp_path):
    path = tmp_path / "excel.csv"
    path.write_bytes("Order ID,Item Name\nET-1,Café Candle\n".encode("cp1252"))
    assert read_text_csv(path).loc[0, "Item Name"] == "Café Candle"


def _shopify_file(tmp_path, discount_code: str) -> Path:
    path = tmp_path / "shopify.csv"
    path.write_text(
        "Name,Financial Status,Created at,Shipping,Discount Code,Discount Amount,Lineitem quantity,Lineitem name,"
        "Lineitem price,Lineitem SKU,Refunded Amount,Canceled at\n"
        f"#2001,partially_refunded,2026-11-06 10:00:00 -0500,0,{discount_code},10.00,1,Cedar,24.00,FW-002,10.00,\n",
        encoding="utf-8")
    return path


def test_shopify_refund_that_looks_double_counted_is_flagged(shop, tmp_path):
    flagged = read_shopify_orders(_shopify_file(tmp_path, ""), shop.orders, shop.products)
    assert "#2001" in flagged.attrs["warnings"][0]
    with_code = read_shopify_orders(_shopify_file(tmp_path, "SAVE10"), shop.orders, shop.products)
    assert not any("Discount Amount equals Refunded Amount" in w for w in with_code.attrs["warnings"])


def test_sample_shopify_file_notes_skipped_orders_and_unknown_product(shop):
    imported = read_shopify_orders(SAMPLE / "shopify_orders_export.csv", shop.orders, shop.products)
    skipped, partial, unknown = imported.attrs["warnings"]
    assert "1 partially refunded Shopify order(s): #1051" in partial and "with tax" not in partial
    assert "2 cancelled or fully refunded" in skipped and "#1052, #1053" in skipped
    assert "1 order line(s) worth $18.00" in unknown and "Beeswax Pillar Candle (1)" in unknown


def test_cli_uses_orders_file(tmp_path, capsys):
    assert main(["--data", str(SAMPLE), "--etsy-csv", str(SAMPLE / "etsy_order_items.csv"),
                 "--etsy-orders-csv", str(SAMPLE / "etsy_orders.csv")]) == 0
    assert main(["--data", str(SAMPLE), "--etsy-orders-csv", str(SAMPLE / "etsy_orders.csv")]) == 1
    assert "used together with --etsy-csv" in capsys.readouterr().err
