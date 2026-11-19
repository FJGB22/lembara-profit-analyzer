"""Client-facing profit report: a printable HTML page, turned into a PDF by Edge or Chrome."""

import html
import os
import shutil
import subprocess
from pathlib import Path

import pandas as pd

from .analysis import Report

MAX_CHART_ROWS = 10
MAX_TABLE_ROWS = 8
MAX_MONTHS = 6

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
BROWSER_COMMANDS = ["msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]


def _usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "0.0%"


def _n(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _e(text) -> str:
    return html.escape(str(text))


def _short(name: str, limit: int = 30) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _period(summary: dict) -> str:
    first, last = summary.get("first_date"), summary.get("last_date")
    if not first:
        return "No orders"
    start, end = pd.Timestamp(first), pd.Timestamp(last)
    return f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}"


def find_browser() -> str | None:
    """Edge or Chrome, used only to print the HTML report to PDF. LEMBARA_BROWSER overrides the search."""
    override = os.environ.get("LEMBARA_BROWSER")
    if override:
        return override if Path(override).exists() else None
    for command in BROWSER_COMMANDS:
        found = shutil.which(command)
        if found:
            return found
    local = os.environ.get("LOCALAPPDATA")
    candidates = BROWSER_CANDIDATES + ([str(Path(local) / "Google/Chrome/Application/chrome.exe")] if local else [])
    return next((path for path in candidates if Path(path).exists()), None)


# ---------------------------------------------------------------- findings

def free_shipping_finding(orders: pd.DataFrame) -> dict | None:
    """Orders where the buyer paid no shipping but the shop paid for a label."""
    per_order = orders.groupby("order_id").agg(
        channel=("channel", "first"), charged=("shipping_charged", "sum"), paid=("shipping_cost", "sum")
    )
    absorbed = per_order[(per_order["charged"] == 0) & (per_order["paid"] > 0)]
    if absorbed.empty:
        return None
    return {
        "orders": len(absorbed),
        "absorbed": absorbed["paid"].sum(),
        "charged_total": per_order["charged"].sum(),
        "paid_total": per_order["paid"].sum(),
        "channels": sorted(absorbed["channel"].unique()),
    }


def _free_shipping_losing_orders(orders: pd.DataFrame) -> int:
    losing_ids = orders.loc[orders["net_profit"] < 0, "order_id"].unique()
    charged = orders[orders["order_id"].isin(losing_ids)].groupby("order_id")["shipping_charged"].sum()
    return int((charged == 0).sum())


def losing_finding(report: Report) -> dict | None:
    losing = report.losing_orders
    if losing.empty:
        return None
    counts = losing.groupby("product")["order_id"].nunique().sort_values(ascending=False)
    return {
        "orders": losing["order_id"].nunique(),
        "lost": losing["net_profit"].sum(),
        "product": counts.index[0],
        "product_count": int(counts.iloc[0]),
        "free_shipping": _free_shipping_losing_orders(report.orders),
    }


def restock_finding(report: Report) -> pd.DataFrame:
    needs = report.by_product[report.by_product["status"] != "OK"]
    return needs.sort_values("net_profit", ascending=False)


def build_actions(report: Report) -> list[tuple[str, str, str]]:
    actions = []
    ship = free_shipping_finding(report.orders)
    if ship:
        gap = ship["charged_total"] - ship["paid_total"]
        detail = (f"On {_n(ship['orders'], ' and '.join(ship['channels']) + ' order')} the buyer paid no shipping, "
                  f"so the shop paid {_usd(ship['absorbed'])} in labels. Across all orders, shipping charged was "
                  f"{_usd(ship['charged_total'])} against {_usd(ship['paid_total'])} paid"
                  + (f": a {_usd(abs(gap))} gap." if gap < 0 else "."))
        actions.append((f"Free shipping cost you {_usd(ship['absorbed'])}", detail,
                        "Add a free-shipping minimum or build part of shipping into item prices."))
    lose = losing_finding(report)
    if lose:
        title = (f"{lose['orders']} orders lost {_usd(abs(lose['lost']))}" if lose["orders"] > 1
                 else f"One order lost {_usd(abs(lose['lost']))}")
        product = _e(lose["product"])
        if lose["orders"] == 1:
            detail = f"It included {product}." + (" The buyer paid no shipping." if lose["free_shipping"] else "")
        else:
            share = "All of them" if lose["product_count"] >= lose["orders"] else f"{lose['product_count']} of them"
            free = ("All had free shipping." if lose["free_shipping"] == lose["orders"]
                    else f"{lose['free_shipping']} had free shipping." if lose["free_shipping"] else "")
            detail = f"{share} included {product}. {free}".strip()
        detail += " Details on page 2."
        actions.append((title, detail, "Check the price, shipping and fees on these products first."))
    needs = restock_finding(report)
    if not needs.empty:
        low = needs[needs["status"] == "REORDER"]["product"].head(3).tolist()
        out = needs[needs["status"] == "OUT OF STOCK"]["product"].head(3).tolist()
        parts = []
        if low:
            parts.append("Running low (highest earners first): " + ", ".join(_e(n) for n in low) + ".")
        if out:
            parts.append("Out of stock: " + ", ".join(_e(n) for n in out) + ".")
        detail = " ".join(parts)
        actions.append((f"Restock {_n(len(needs), 'product')}", detail, "Reorder the top earners first (list on page 2)."))
    return actions[:3]


# ---------------------------------------------------------------- pieces

def profit_chart(by_product: pd.DataFrame) -> str:
    rows = by_product[by_product["units_sold"] > 0][["product", "net_profit", "profit_per_unit", "units_sold"]]
    rows = rows.sort_values("net_profit", ascending=False).reset_index(drop=True)
    if len(rows) > MAX_CHART_ROWS:
        rest = rows.iloc[MAX_CHART_ROWS - 1:]
        other = pd.DataFrame([{
            "product": f"{len(rest)} other products", "net_profit": rest["net_profit"].sum(),
            "profit_per_unit": rest["net_profit"].sum() / rest["units_sold"].sum(), "units_sold": rest["units_sold"].sum(),
        }])
        rows = pd.concat([rows.iloc[: MAX_CHART_ROWS - 1], other], ignore_index=True)
    if rows.empty:
        return "<p class='note'>No products sold in this period.</p>"

    width, row_h, label_w, value_w = 700, 24, 200, 165
    plot_w = width - label_w - value_w - 10
    top_value = max(rows["net_profit"].max(), 0.01)
    parts = []
    for i, row in rows.iterrows():
        y = 4 + i * row_h
        bar = max(row["net_profit"], 0) / top_value * plot_w
        colour = "#2a78d6" if i < 3 and row["net_profit"] > 0 else "#9fa3a6"
        parts.append(
            f"<text x='{label_w - 10}' y='{y + 16}' text-anchor='end' class='lab'>{_e(_short(row['product']))}</text>"
            f"<rect x='{label_w}' y='{y + 4}' width='{bar:.1f}' height='16' rx='3' fill='{colour}'/>"
            f"<text x='{label_w + bar + 6:.1f}' y='{y + 16}' class='val'>{_usd(row['net_profit'])} "
            f"<tspan class='sub'>· {_usd(row['profit_per_unit'])}/unit</tspan></text>"
        )
    height = 4 + len(rows) * row_h + 6
    return (f"<svg viewBox='0 0 {width} {height}' width='{width}' role='img' aria-label='Net profit by product'>"
            f"<line x1='{label_w}' y1='4' x2='{label_w}' y2='{height - 4}' stroke='#cfcfca'/>{''.join(parts)}</svg>")


def _table(headers: list[str], rows: list[list[str]], numeric: set[int]) -> str:
    head = "".join(f"<th class='n'>{h}</th>" if i in numeric else f"<th>{h}</th>" for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f"<td class='n'>{c}</td>" if i in numeric else f"<td>{c}</td>" for i, c in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def channel_table(report: Report) -> str:
    counts = report.orders.groupby("channel")["order_id"].nunique()
    rows = [[_e(r.channel), str(int(counts.get(r.channel, 0))), _usd(r.sales), _usd(r.fees), _pct(r.fees, r.sales),
             _usd(r.net_profit), _pct(r.net_profit, r.sales)] for r in report.by_channel.itertuples()]
    return _table(["Channel", "Orders", "Sales", "Fees", "Fees % of sales", "Net profit", "Margin"], rows, {1, 2, 3, 4, 5, 6})


def month_table(report: Report) -> str:
    months = report.by_month.tail(MAX_MONTHS)
    rows = [[pd.Timestamp(r.month + "-01").strftime("%B %Y"), _usd(r.sales), _usd(r.net_profit), _pct(r.net_profit, r.sales)]
            for r in months.itertuples()]
    return _table(["Month", "Sales", "Net profit", "Margin"], rows, {1, 2, 3})


def losing_table(report: Report) -> str:
    lines = report.orders[report.orders["net_profit"] < 0].sort_values("net_profit")
    shown = lines.head(MAX_TABLE_ROWS)
    rows = [[_e(r.order_id), _e(r.channel), _e(_short(r.product, 34)), _usd(r.order_total), _usd(r.shipping_charged),
             _usd(r.shipping_cost), _usd(r.platform_fees), _usd(r.product_cost), f"<b>{_usd(r.net_profit)}</b>"]
            for r in shown.itertuples()]
    table = _table(["Order", "Channel", "Product", "Order total", "Shipping charged", "Shipping paid", "Fees",
                    "Materials", "Net"], rows, {3, 4, 5, 6, 7, 8})
    extra = len(lines) - len(shown)
    return table + (f"<div class='note'>{extra} more line(s) in the Excel report.</div>" if extra > 0 else "")


def restock_table(report: Report) -> str:
    needs = restock_finding(report)
    rows = [[_e(r.product), str(r.in_stock), str(int(r.reorder_at)), f"<b>{'Out of stock' if r.status == 'OUT OF STOCK' else 'Reorder'}</b>",
             _usd(r.net_profit), _usd(r.profit_per_unit)] for r in needs.head(MAX_TABLE_ROWS).itertuples()]
    return _table(["Product", "In stock", "Reorder at", "Status", "Net profit", "Profit / unit"], rows, {1, 2, 4, 5})


def fee_notes(fees: pd.DataFrame | None, channels=None) -> str:
    """Fee rates for the channels in this report only (a Shopify-only shop doesn't need Etsy's rates)."""
    if fees is None or fees.empty:
        return ""
    if channels is not None:
        fees = fees[fees["channel"].isin(list(channels))]
    lines = []
    for r in fees.itertuples():
        rate = f"{r.pct_of_order_total * 100:g}% of order total + {_usd(r.fixed_per_order)} per order + {_usd(r.per_item_sold)} per item"
        lines.append(f"<li><b>{_e(r.channel)}:</b> {rate}.</li>")
    return ("<ul class='m'>" + "".join(lines) + "</ul><div class='note'>Rates are set per shop and should match the "
            "seller's country and payment plan.</div>")


# ---------------------------------------------------------------- page

STYLE = """
@page { size: Letter; margin: 0.5in 0.6in; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Inter, "Segoe UI", system-ui, sans-serif; color: #1d1d1b; font-size: 10.5pt; line-height: 1.45;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.first { page-break-after: always; }
.brand { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 2px solid #1d1d1b; padding-bottom: 8px; }
.brand .name { font-weight: 700; letter-spacing: .02em; font-size: 11pt; }
.brand .tag { color: #6b6b66; font-size: 9pt; }
.demo { display: inline-block; background: #fff4d6; color: #6a4b00; border: 1px solid #f0d78a; border-radius: 4px; padding: 1px 7px; font-size: 8.5pt; font-weight: 600; }
h1 { font-size: 18pt; line-height: 1.15; margin: 18px 0 4px; letter-spacing: -.01em; }
.meta { color: #6b6b66; font-size: 9.5pt; margin-bottom: 14px; }
.kpis { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin: 6px 0 16px; }
.kpi { border: 1px solid #e3e3df; border-radius: 6px; padding: 9px 11px; }
.kpi .l, .kpi .s { color: #6b6b66; font-size: 8.5pt; }
.kpi .v { font-size: 15pt; font-weight: 700; margin-top: 1px; }
.kpi.hero { background: #f2f7fd; border-color: #c9ddf5; }
h2 { font-size: 12pt; margin: 14px 0 6px; page-break-after: avoid; }
h2 .q { font-weight: 400; color: #6b6b66; font-size: 9.5pt; }
.lab { font: 9.5pt Inter, "Segoe UI", sans-serif; fill: #1d1d1b; }
.val { font: 600 9.5pt Inter, "Segoe UI", sans-serif; fill: #1d1d1b; }
.sub { font-weight: 400; fill: #6b6b66; }
.act { border-left: 3px solid #2a78d6; padding: 5px 10px 5px 12px; font-size: 9.5pt; line-height: 1.4; background: #fafaf8; margin-bottom: 6px; }
.act b.t { display: block; font-size: 10.5pt; margin-bottom: 1px; }
table { width: 100%; border-collapse: collapse; font-size: 9.5pt; page-break-inside: avoid; }
th { text-align: left; font-weight: 600; color: #52514e; border-bottom: 1px solid #1d1d1b; padding: 5px 6px; font-size: 8.5pt; }
td { padding: 5px 6px; border-bottom: 1px solid #ecece8; }
th.n, td.n { text-align: right; font-variant-numeric: tabular-nums; }
.note { color: #6b6b66; font-size: 8.5pt; margin-top: 4px; }
.warn { border: 1px solid #f0d78a; background: #fffaf0; border-radius: 6px; padding: 6px 10px; font-size: 9pt; }
ul.m { margin: 4px 0 0; padding-left: 16px; color: #33332f; font-size: 9pt; }
ul.m li { margin-bottom: 2px; }
.foot { margin-top: 18px; border-top: 1px solid #e3e3df; padding-top: 6px; color: #8a8a84; font-size: 7.5pt; }
"""

TRADEMARK = ("The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed "
             "or certified by Etsy, Inc.")


def build_report_html(report: Report, shop_name: str, fees: pd.DataFrame | None = None, sample: bool = False,
                      contact: str = "lembara.official@gmail.com") -> str:
    s = report.summary
    shop = _e(shop_name)
    # Portfolio samples carry no email: Upwork forbids sharing contact details before a contract starts.
    contact_line = f" · {_e(contact)}" if contact and not sample else ""
    badge = " &nbsp;<span class='demo'>SAMPLE REPORT · FICTIONAL SHOP, DEMO DATA</span>" if sample else ""
    channels = ", ".join(_e(c) for c in report.by_channel["channel"])

    chart_rows = report.by_product[report.by_product["units_sold"] > 0]
    headline = "Net profit by product"
    if len(chart_rows) >= 4 and s["net_profit"] > 0:
        share = chart_rows["net_profit"].head(3).sum() / s["net_profit"]
        headline = f"Three products earn {share:.0%} of the profit"

    actions = "".join(
        f"<div class='act'><b class='t'>{title}</b>{detail}<div><b>Fix:</b> {fix}</div></div>"
        for title, detail, fix in build_actions(report)
    ) or "<p>No losing orders, unpaid shipping or low stock found in this period.</p>"

    warnings = ""
    if report.warnings:
        warnings = ("<h2>Data checks</h2><div class='warn'><ul class='m'>"
                    + "".join(f"<li>{_e(w)}</li>" for w in report.warnings) + "</ul></div>")

    losing = ("<h2>Orders that lost money</h2>" + losing_table(report)) if not report.losing_orders.empty else ""
    restock = ("<h2>Restock list</h2>" + restock_table(report)) if not restock_finding(report).empty else ""
    footer = ("Sample report built from a fictional shop's demo data to show the report format. It does not describe "
              "a real client. " if sample else "") + (TRADEMARK if "Etsy" in set(report.by_channel["channel"]) else "")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Profit report – {shop}</title><style>{STYLE}</style></head>
<body>
<section class="first">
  <div class="brand"><span class="name">LEMBARA STUDIO</span><span class="tag">Profit Analyzer · Profit report</span></div>
  <h1>{shop} made {_usd(s['net_profit'])} profit on {_usd(s['sales'])} in sales</h1>
  <div class="meta">Period: {_period(s)} · Channels: {channels} · {s['orders']} orders{badge}</div>
  <div class="kpis">
    <div class="kpi"><div class="l">Sales (incl. shipping charged)</div><div class="v">{_usd(s['sales'])}</div></div>
    <div class="kpi"><div class="l">Platform fees</div><div class="v">{_usd(s['platform_fees'])}</div></div>
    <div class="kpi"><div class="l">Materials + shipping paid</div><div class="v">{_usd(s['product_and_shipping_costs'])}</div></div>
    <div class="kpi hero"><div class="l">Net profit</div><div class="v">{_usd(s['net_profit'])}</div><div class="s">{s['profit_margin']:.1%} of sales</div></div>
  </div>
  <h2>{headline} <span class="q">· after fees, materials and shipping</span></h2>
  {profit_chart(report.by_product)}
  <div class="note">Profit per unit shown after each total. Material costs come from the shop's recipe sheet; products without a recipe use their unit cost.</div>
  <h2>What to act on</h2>
  {actions}
  <div class="foot">Prepared with Lembara Profit Analyzer{contact_line}</div>
</section>
<section>
  <div class="brand"><span class="name">LEMBARA STUDIO</span><span class="tag">{shop} · {_period(s)}</span></div>
  <h2 style="margin-top:18px">Profit by sales channel</h2>
  {channel_table(report)}
  <h2>By month</h2>
  {month_table(report)}
  {losing}
  {restock}
  {warnings}
  <h2>How these numbers are calculated</h2>
  <ul class="m">
    <li><b>Net profit</b> = order total − platform fees − material cost − shipping paid by the shop.</li>
    <li><b>Order total</b> = item price × quantity + shipping charged − discounts − refunds. Payment fees are counted on the amount charged before refunds, because the processor keeps them.</li>
    <li><b>Material cost</b> comes from the recipe sheet (materials × quantity used), or the product's unit cost when it has no recipe.</li>
    <li><b>Buyer names and addresses</b> are not stored or used.</li>
    <li><b>Not included:</b> ads, sales tax/VAT, packaging and the seller's time, unless added to the inputs.</li>
  </ul>
  <h2>Fee rates used</h2>
  {fee_notes(fees, report.by_channel["channel"])}
  <div class="foot">{footer}</div>
</section>
</body></html>"""


def write_report(report: Report, html_path: str | Path, shop_name: str, fees: pd.DataFrame | None = None,
                 sample: bool = False, pdf: bool = True) -> tuple[Path, Path | None, str | None]:
    """Write the HTML report and, when a browser is available, a PDF next to it.

    Returns (html path, pdf path or None, message explaining why there is no PDF).
    """
    html_path = Path(html_path).resolve()
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(build_report_html(report, shop_name, fees, sample), encoding="utf-8")
    if not pdf:
        return html_path, None, None

    browser = find_browser()
    if not browser:
        return html_path, None, ("No Edge or Chrome found. Open the HTML file in a browser and print it to PDF, "
                                 "or set LEMBARA_BROWSER to the browser's .exe path.")
    pdf_path = html_path.with_suffix(".pdf")
    command = [browser, "--headless", "--disable-gpu", "--no-pdf-header-footer", "--print-to-pdf-no-header",
               f"--print-to-pdf={pdf_path}", html_path.as_uri()]
    try:
        subprocess.run(command, check=True, timeout=120, capture_output=True)
    except (subprocess.SubprocessError, OSError) as error:
        return html_path, None, f"PDF printing failed ({error}). Open the HTML file and print it to PDF instead."
    if not pdf_path.exists():
        return html_path, None, "The browser did not create a PDF. Open the HTML file and print it to PDF instead."
    return html_path, pdf_path, None
