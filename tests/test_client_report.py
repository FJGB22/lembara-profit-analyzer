"""The client report must show the same numbers as the analysis, and must never trust text from a client's CSV."""

from pathlib import Path

import pandas as pd
import pytest

from etsy_profit import analyze, load_shop
from etsy_profit import client_report
from etsy_profit.cli import main
from etsy_profit.client_report import build_actions, build_report_html, free_shipping_finding, write_report

SAMPLE = Path(__file__).parent.parent / "data" / "sample"


@pytest.fixture(scope="module")
def shop():
    return load_shop(SAMPLE)


@pytest.fixture(scope="module")
def report(shop):
    return analyze(shop)


def test_report_shows_dashboard_numbers(report, shop):
    page = build_report_html(report, "Fern & Wick", shop.fees)
    for number in ("$1,379.50", "$124.26", "$478.11", "$777.13", "56.3%"):
        assert number in page
    assert "Three products earn 68% of the profit" in page


def test_actions_match_demo_findings(report):
    titles = [title for title, _, _ in build_actions(report)]
    assert titles == ["Free shipping cost you $107.10", "2 orders lost $1.49", "Restock 3 products"]


def test_sample_label_only_when_asked(report):
    assert "FICTIONAL SHOP" not in build_report_html(report, "Real Shop")
    assert "FICTIONAL SHOP" in build_report_html(report, "Demo", sample=True)


def test_text_from_client_files_is_escaped(shop):
    products = shop.products.copy()
    products.loc[0, "product"] = "<script>alert(1)</script>"
    tricky = type(shop)(products, shop.materials, shop.recipes, shop.orders, shop.fees)
    page = build_report_html(analyze(tricky), "<b>Shop</b>")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "<b>Shop</b>" not in page


def test_free_shipping_is_counted_per_order_not_per_line():
    orders = pd.DataFrame({
        "order_id": ["A", "A", "B"],
        "channel": ["Etsy", "Etsy", "Etsy"],
        "shipping_charged": [4.50, 0.0, 0.0],   # order A: buyer paid shipping on the first line only
        "shipping_cost": [4.20, 0.0, 5.10],
    })
    finding = free_shipping_finding(orders)
    assert finding["orders"] == 1
    assert finding["absorbed"] == 5.10


def test_no_browser_still_writes_html(report, tmp_path, monkeypatch):
    monkeypatch.setattr(client_report, "find_browser", lambda: None)
    html_file, pdf_file, problem = write_report(report, tmp_path / "r.html", "Shop")
    assert html_file.exists() and pdf_file is None
    assert "print it to PDF" in problem


def test_cli_report_option(tmp_path, capsys):
    target = tmp_path / "out" / "report.html"
    assert main(["--data", str(SAMPLE), "--report", str(target), "--shop-name", "Fern & Wick", "--no-pdf"]) == 0
    assert target.exists()
    assert "$777.13" in target.read_text(encoding="utf-8")


def test_etsy_report_keeps_trademark_notice(report, shop):
    assert "trademark of Etsy, Inc." in build_report_html(report, "Fern & Wick", shop.fees)


def test_sample_report_has_no_contact_email(report, shop):
    assert "lembara.official@gmail.com" in build_report_html(report, "Real Shop", shop.fees)
    sample = build_report_html(report, "Demo", shop.fees, sample=True)
    assert "lembara.official@gmail.com" not in sample
    assert "@gmail" not in sample
