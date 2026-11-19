# Lembara Profit Analyzer

Works with Etsy (CSV or API), Shopify (CSV) and manual channels such as Instagram DMs.

Small handmade shops often know their sales but not their **real profit per product**. Etsy alone takes a transaction fee, a payment processing fee, a fixed fee per order and a listing renewal per item sold, and the true cost of a candle depends on how much wax, oil and glass went into it.

This tool reads a shop's orders, products, materials and recipes from plain CSV files (or straight from an Etsy *Order Items* export) and answers:

- What did I actually keep after platform fees, materials and shipping?
- Which products make money, and how much per unit?
- Which orders **lost** money, and why?
- What needs restocking?

Optionally it writes the report to Excel or a Google Sheet, and asks Claude for a short plain-English summary based only on the numbers.

## Example (included demo shop)

```
$ etsy-profit --data data/sample

Shop report  2026-09-01 to 2026-10-31  (49 orders)
  Sales                        $1,379.50
  Platform fees                  $124.26
  Product + shipping costs       $478.11
  Net profit                     $777.13   (56.3% margin)

Orders that lost money
  ET-1045  Matchbox - Botanical         -$1.02  (fees $1.02, shipping $5.10)
  SH-1049  Matchbox - Botanical         -$0.47  (fees $0.47, shipping $5.10)
```

A $6 matchbox sold alone with free shipping loses money once the label is paid. This is the kind of insight that's easy to miss in Etsy's own dashboard.

The demo numbers come from a Google Sheets version of the same tracker, and the test suite checks that the Python version reproduces that spreadsheet's results exactly.

## How it works

| Step | Module | What it does |
|---|---|---|
| Load | `loaders.py` | Reads the CSVs, checks required columns, turns `$4.10` into numbers |
| Cost | `costing.py` | Material cost per unit → recipe cost per product (falls back to a flat unit cost when there is no recipe) |
| Fees | `fees.py` | Per-channel fees: % of order total, fixed fee once per order, fee per item sold |
| Analyze | `analysis.py` | Order profit, totals, profit by product / channel / month, restock list, losing orders |
| Import | `etsy_import.py` | Maps Etsy's Order Items CSV, counts shipping once per order, skips orders already imported, flags unknown SKUs |
| Shopify | `shopify_import.py` | Maps Shopify's Orders export, fills blank order-level fields on extra line items, skips cancelled/refunded orders |
| Etsy API | `etsy_api.py` | OAuth 2.0 with PKCE, token refresh, paginated receipts → the same order format as the CSV import |
| Export | `exporters.py` | Excel workbook or Google Sheet tabs |
| Report | `client_report.py` | Two-page HTML report for the client, printed to PDF by Edge or Chrome |
| Summary | `ai_summary.py` | Sends the computed tables (not raw customer data) to Claude with instructions to use only those numbers |

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

etsy-profit --data data/sample --excel output/report.xlsx
pytest
```

Import an Etsy export (Shop Manager → Settings → Options → Download Data → Order Items CSV):

```bash
etsy-profit --data data/sample --etsy-csv path/to/EtsySoldOrderItems.csv --etsy-shipping-cost 4.20
```

Etsy's CSV does not include what you paid for the shipping label, so `--etsy-shipping-cost` sets an average per order.

For exact order-level numbers, also pass Etsy's **Orders** CSV (same Download Data page, CSV type "Orders"):

```bash
etsy-profit --data data/sample --etsy-csv EtsySoldOrderItems.csv --etsy-orders-csv EtsySoldOrders.csv
```

The Order Items file repeats order-level values (shipping, coupons) on every line. The Orders file has one row per
order, so shipping and discounts are taken from there. Each order's total is then checked against Etsy's
`Order Total` minus `Sales Tax`, and any order that doesn't match is listed under *Data checks* in the report
instead of being silently trusted.

Import a Shopify Orders export (Orders → Export → CSV) the same way:

```bash
etsy-profit --data data/sample --shopify-csv path/to/orders_export.csv --shopify-shipping-cost 4.20
```

Cancelled and fully refunded/voided orders are skipped and listed under *Data checks*. Partial refunds and
discounts reduce revenue. A line-item discount stays on its own product; only the rest of `Discount Amount` is
treated as an order-level discount. Each order's total is checked against Shopify's `Total − Taxes − Refunded Amount`.
Dates are the shop's local date from `Created at`. The importer was tested on a real export from a Shopify
development store (17 Nov 2026, `data/sample/shopify_test_store_export.csv`).

### Pull orders straight from Etsy (Open API v3)

Etsy's order CSVs have been reported to lag about two weeks behind, so the API gives fresher numbers. You need an app registered at [developers.etsy.com](https://developers.etsy.com/documentation) (keystring + shared secret) with an `https://` redirect URL.

```bash
export ETSY_KEYSTRING=...  ETSY_SHARED_SECRET=...  ETSY_REDIRECT_URI=https://your-registered-callback
etsy-profit-connect                         # one time: approve access in the browser, paste the redirect URL back
etsy-profit --data data/sample --etsy-api --since 2026-11-01 --etsy-shipping-cost 4.20
```

Only read scopes are requested (`transactions_r shops_r`). The token is stored locally in `.etsy_token.json` (git-ignored) and refreshed automatically. Cancelled receipts are skipped; orders already in `orders.csv` are not imported twice.

The app's public pages (home, OAuth callback, privacy policy) live in `docs/` and are served with GitHub Pages. The callback page only shows the address to copy back; it loads no other resources and sends nothing.

*The term 'Etsy' is a trademark of Etsy, Inc. This application uses the Etsy API but is not endorsed or certified by Etsy, Inc.*

### Client report (HTML + PDF)

```bash
etsy-profit --data data/sample --report output/report.html --shop-name "Fern & Wick Candle Co." --sample-report
```

Writes a two-page, client-ready report: totals, net profit by product (chart), up to three findings to act on
(shipping the shop absorbed, orders that lost money, products to restock), profit by channel and month, the fee
rates used and how every number is calculated. The PDF is printed by Microsoft Edge or Google Chrome in headless
mode, so no extra Python package is needed. If neither browser is found (or you pass `--no-pdf`), only the HTML file
is written; open it and print to PDF. Set `LEMBARA_BROWSER` to a browser's full path to choose one.

`--sample-report` adds a "sample · fictional shop, demo data" label for portfolio use. Leave it off for real clients. Sample reports also leave out the contact email, because Upwork does not allow contact details in profiles, portfolios or proposals before a contract starts.
All text from the input files (product names, shop name) is HTML-escaped.

Optional extras:

```bash
pip install -e ".[sheets]"   # --sheet-id SHEET_ID --credentials service_account.json
pip install -e ".[ai]"       # export ANTHROPIC_API_KEY=... then add --ai
```

## Input files

| File | Columns |
|---|---|
| `products.csv` | sku, product, category, unit_cost, price, starting_stock, reorder_at |
| `materials.csv` (optional) | material, unit, pack_price, pack_size, notes |
| `recipes.csv` (optional) | sku, material, qty_per_item |
| `orders.csv` | date, order_id, channel, sku, qty, unit_price, shipping_charged, shipping_cost, discount (optional), refund (optional) |
| `fees.csv` | channel, pct_of_order_total, fixed_per_order, per_item_sold, notes, fees_returned_on_refund (optional, yes/no) |
| `item_names.csv` (optional) | item_name, sku — maps a marketplace item name (e.g. a long Etsy listing title) to your SKU |

Shops that never set SKUs still work: an imported line with a blank or unknown SKU is matched through
`item_names.csv`, then by a product with exactly the same name (case and extra spaces ignored). A name used by two
products is never guessed. Lines that still match nothing are left out and listed under *Data checks* with the item
sales they represent, so a report never looks complete when it isn't.

`data/sample` also holds example exports in the platforms' own layouts: `etsy_order_items.csv`, `etsy_orders.csv` and `shopify_orders_export.csv` (buyer fields are placeholders), plus `shopify_test_store_export.csv`, a real export from a Shopify development store with no buyer data (staff name replaced).

## Limitations

- Fee rates in `data/sample/fees.csv` are an example (US Etsy rates at the time of writing). Check Etsy's current fee page; Offsite Ads, currency conversion and regulatory fees are not included.
- Etsy's Order Items column names are based on a published export layout and have not yet been checked against every account's CSV. If a column is named differently, the tool says which one and you can pass the real name.
- Without the Orders CSV, coupon discounts (`Discount Amount` + `Shipping Discount`) from Order Items are subtracted once per order, on the assumption that they are order-level values repeated on each line. Pass `--etsy-orders-csv` to use Etsy's order-level numbers and get a total check instead.
- Etsy Orders CSV column names come from Etsy Help (Discount Amount, Shipping Discount, Shipping, Sales Tax, Order Total) and a third-party list (Order ID). Shops outside the US may see different names; `read_etsy_orders(..., column_names={...})` accepts the real ones.
- Shopify: community reports (not Shopify staff) say `Discount Amount` can already include a refund. Orders where Discount Amount equals Refunded Amount with no discount code are flagged for a manual check rather than corrected automatically. In the development-store export a $10 partial refund did *not* appear in `Discount Amount`.
- Refunds and fees depend on the platform, set per channel in `fees.csv` (`fees_returned_on_refund`, yes/no; without the column only Etsy is "yes"). Etsy Help says Etsy credits its transaction and processing fees back on refunds, so Etsy fees follow the amount kept. Shopify staff (community forum, citing the Shopify Payments terms) say the original processing fee is not returned, so Shopify fees stay on the amount charged. Fully refunded or cancelled orders are left out and listed under *Data checks*.
- Etsy refunds come from the Orders CSV only. Etsy Help says `Order Total` never subtracts refunds; `Adjusted Order Total` is blank when nothing changed, 0 after a full refund, and the total after a partial refund (Etsy seller forum, not staff). With only the Order Items file, refunded or cancelled Etsy orders cannot be seen, and the report says so. Partial refunds on orders with sales tax may include refunded tax and are flagged. Orders whose `Status` is not "Completed" are flagged but kept.
- Shopify refunds: `Refunded Amount` includes refunded tax while `Taxes` and `Total` keep their original values (development-store export, 17 Nov 2026). Only the pre-tax share of a refund lowers sales, split in proportion to the order's tax; exact when a returned item's tax was refunded with it. The order export does not say which item was refunded, so the refund is taken from the order's first line and the item still counts as sold: order totals are right, but profit per product on refunded orders can be shifted. Such orders are listed under *Data checks*.
- `Lineitem discount` is the discount for the whole line (10% off 3 × $14 shows 4.20), confirmed in the development-store export.
- Fees are calculated on order totals without sales tax. Card processors charge on the full amount including tax, so fees for taxed orders are slightly low.
- Development-store limits: no card payments (orders were marked as paid manually), shipping labels or real buyers. A CSV from a real shop is still the strongest test.
- CSV files are read as UTF-8 (with or without BOM) and fall back to Windows cp1252, the encoding Excel often uses when a file is re-saved.
- Etsy's order CSVs have been reported to lag about two weeks behind the current date (since July 2025). For up-to-date numbers, the next step is pulling orders from the Etsy Open API.
- US payment processing (3% + $0.25) is also charged on sales tax collected; sales tax isn't modelled, so fees on taxed orders are slightly understated.
- Stock is calculated as starting stock minus units sold. Restocks aren't tracked yet.
- The Google Sheets export and the Claude summary are optional and are not covered by the automated tests (they need credentials).
- The Etsy API module is tested against a fake server built from Etsy's published OpenAPI spec, not yet against a live shop. Order dates use UTC.
- Shopify: in the real export, `Discount Amount` already includes line-item discounts, so the two are never added together.
- Etsy's `discount_amt` excludes free-shipping coupons; those orders should already show reduced `total_shipping_cost`, but this hasn't been checked on live data.
