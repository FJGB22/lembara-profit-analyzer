import argparse
import sys
from pathlib import Path

from .analysis import analyze
from .etsy_import import merge_ready_items, read_etsy_order_items
from .exporters import write_excel
from .loaders import load_shop


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def print_report(report) -> None:
    s = report.summary
    print(f"\nShop report  {s['first_date']} to {s['last_date']}  ({s['orders']} orders)")
    print(f"  Sales                     {_money(s['sales']):>12}")
    print(f"  Platform fees             {_money(s['platform_fees']):>12}")
    print(f"  Product + shipping costs  {_money(s['product_and_shipping_costs']):>12}")
    print(f"  Net profit                {_money(s['net_profit']):>12}   ({s['profit_margin']:.1%} margin)")

    print("\nProfit by product")
    for row in report.by_product.itertuples():
        print(f"  {row.product:<28} {row.units_sold:>3} sold  {_money(row.net_profit):>10}  "
              f"({_money(row.profit_per_unit)}/unit)  {row.status}")

    if not report.losing_orders.empty:
        print("\nOrders that lost money")
        for row in report.losing_orders.itertuples():
            print(f"  {row.order_id:<8} {row.product:<26} {_money(row.net_profit):>8}  "
                  f"(fees {_money(row.platform_fees)}, shipping {_money(row.shipping_cost)})")

    for warning in report.warnings:
        print(f"\nWARNING: {warning}")


def _etsy_settings() -> tuple[str, str, str]:
    import os
    missing = [name for name in ("ETSY_KEYSTRING", "ETSY_SHARED_SECRET", "ETSY_REDIRECT_URI") if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"Set these environment variables first: {', '.join(missing)}")
    return os.environ["ETSY_KEYSTRING"], os.environ["ETSY_SHARED_SECRET"], os.environ["ETSY_REDIRECT_URI"]


def _pull_from_etsy(args, shop):
    import pandas as pd

    from .etsy_api import EtsyClient, EtsyToken, fetch_etsy_orders

    keystring, shared_secret, _ = _etsy_settings()
    if not Path(args.token_file).exists():
        raise RuntimeError(f"No Etsy token at {args.token_file}. Run etsy-profit-connect first.")
    client = EtsyClient(keystring, shared_secret, EtsyToken.load(args.token_file), token_file=args.token_file)
    since = pd.Timestamp(args.since) if args.since else None
    return fetch_etsy_orders(client, shop.orders, shop.products, since, args.etsy_shipping_cost, shop.item_names)


def connect(argv: list[str] | None = None) -> int:
    """One-time login: the seller approves access in the browser, the token is saved locally."""
    import secrets

    from .etsy_api import EtsyApiError, authorization_url, code_from_redirect, exchange_code, make_pkce_pair

    parser = argparse.ArgumentParser(prog="etsy-profit-connect", description="Connect an Etsy shop (one time).")
    parser.add_argument("--token-file", default=".etsy_token.json")
    args = parser.parse_args(argv)

    try:
        keystring, _, redirect_uri = _etsy_settings()
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    verifier, challenge = make_pkce_pair()
    state = secrets.token_urlsafe(16)
    print("1. Open this link while logged in to the Etsy shop and click Allow access:\n")
    print(authorization_url(keystring, redirect_uri, state, challenge))
    print("\n2. Etsy sends you to your redirect page (it may show an error page; that's fine).")
    redirect = input("   Paste the full address from the browser bar here: ")

    try:
        code = code_from_redirect(redirect, state)
        token = exchange_code(keystring, redirect_uri, code, verifier)
    except EtsyApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    token.save(args.token_file)
    print(f"\nConnected (Etsy user {token.user_id}). Token saved to {args.token_file}. Keep this file private.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="etsy-profit", description="Real profit per product for small Etsy shops.")
    parser.add_argument("--data", default="data/sample", help="folder with products/materials/recipes/orders/fees CSVs")
    parser.add_argument("--etsy-csv", help="Etsy 'Order Items' CSV to add to the orders")
    parser.add_argument("--etsy-orders-csv",
                        help="Etsy 'Orders' CSV (one row per order) to use with --etsy-csv: exact shipping and "
                             "discounts per order, plus a check against Etsy's own order totals")
    parser.add_argument("--shopify-csv", help="Shopify Orders export CSV to add to the orders")
    parser.add_argument("--shopify-shipping-cost", type=float, default=0.0,
                        help="your average shipping label cost per imported Shopify order")
    parser.add_argument("--etsy-api", action="store_true",
                        help="pull orders straight from Etsy (run etsy-profit-connect once first)")
    parser.add_argument("--since", help="with --etsy-api: only orders created on or after this date (YYYY-MM-DD)")
    parser.add_argument("--token-file", default=".etsy_token.json", help="where etsy-profit-connect saved the token")
    parser.add_argument("--etsy-shipping-cost", type=float, default=0.0,
                        help="your average shipping label cost per imported Etsy order (Etsy does not report it)")
    parser.add_argument("--excel", help="write the full report to this .xlsx file")
    parser.add_argument("--sheet-id", help="write the report into this Google Sheet (needs --credentials)")
    parser.add_argument("--credentials", help="Google service account JSON file for --sheet-id")
    parser.add_argument("--ai", action="store_true", help="add a plain-English summary written by Claude")
    parser.add_argument("--report", help="write a client-ready report to this .html file (and a PDF next to it)")
    parser.add_argument("--shop-name", default="Your shop", help="shop name shown on the --report")
    parser.add_argument("--sample-report", action="store_true",
                        help="mark the --report as a sample made from demo data (for portfolios)")
    parser.add_argument("--no-pdf", action="store_true", help="with --report: write only the HTML file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        shop = load_shop(args.data)
    except (FileNotFoundError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    import_warnings: list[str] = []

    if args.etsy_orders_csv and not args.etsy_csv:
        print("Error: --etsy-orders-csv is used together with --etsy-csv (the Order Items file)", file=sys.stderr)
        return 1

    if args.etsy_csv:
        imported = read_etsy_order_items(args.etsy_csv, shop.orders, shop.products,
                                         default_shipping_cost=args.etsy_shipping_cost, item_names=shop.item_names)
        import_warnings.extend(imported.attrs.get("warnings", []))
        if args.etsy_orders_csv:
            from .etsy_import import apply_etsy_orders, read_etsy_orders
            try:
                imported, warnings = apply_etsy_orders(imported, read_etsy_orders(args.etsy_orders_csv))
            except ValueError as error:
                print(f"Error: {error}", file=sys.stderr)
                return 1
            import_warnings.extend(warnings)
        else:
            import_warnings.append("Only the Etsy Order Items file was used. Refunds and cancellations are only in "
                                   "Etsy's Orders file, so refunded or cancelled Etsy orders may be counted as sales. "
                                   "Add --etsy-orders-csv to catch them.")
        print("\nEtsy import")
        for status, count in imported["status"].value_counts().items():
            print(f"  {status}: {count} line(s)")
        shop.orders = merge_ready_items(shop.orders, imported)

    if args.shopify_csv:
        from .shopify_import import read_shopify_orders
        try:
            imported = read_shopify_orders(args.shopify_csv, shop.orders, shop.products,
                                           default_shipping_cost=args.shopify_shipping_cost,
                                           item_names=shop.item_names)
        except ValueError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        import_warnings.extend(imported.attrs.get("warnings", []))
        print("\nShopify import")
        for status, count in imported["status"].value_counts().items():
            print(f"  {status}: {count} line(s)")
        shop.orders = merge_ready_items(shop.orders, imported)

    if args.etsy_api:
        try:
            imported = _pull_from_etsy(args, shop)
        except (RuntimeError, OSError, KeyError) as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        import_warnings.extend(imported.attrs.get("warnings", []))
        print("\nEtsy API")
        for status, count in imported["status"].value_counts().items():
            print(f"  {status}: {count} line(s)")
        shop.orders = merge_ready_items(shop.orders, imported)

    report = analyze(shop)
    report.warnings.extend(import_warnings)
    print_report(report)

    if args.excel:
        print(f"\nExcel report: {write_excel(report, args.excel)}")

    if args.report:
        from .client_report import write_report
        html_file, pdf_file, problem = write_report(report, args.report, args.shop_name, fees=shop.fees,
                                                    sample=args.sample_report, pdf=not args.no_pdf)
        print(f"\nReport (HTML): {html_file}")
        if pdf_file:
            print(f"Report (PDF):  {pdf_file}")
        elif problem:
            print(f"No PDF: {problem}")

    if args.sheet_id:
        if not args.credentials:
            print("Error: --sheet-id needs --credentials", file=sys.stderr)
            return 1
        from .exporters import write_google_sheet
        print(f"\nGoogle Sheet updated: {write_google_sheet(report, args.sheet_id, Path(args.credentials))}")

    if args.ai:
        from .ai_summary import write_ai_summary
        try:
            print("\nAI summary\n" + write_ai_summary(report))
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
