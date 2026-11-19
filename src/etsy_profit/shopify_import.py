"""Import Shopify's Orders export (Orders → Export → CSV).

Column names follow Shopify Help "Exporting orders" (checked 12 Nov 2026) and a real export from the
fern-and-wick-test development store (17 Nov 2026). Extra line items of an order appear on separate rows
with most order-level fields left blank.
"""

from pathlib import Path

import pandas as pd

from .etsy_import import mark_import_status
from .csv_files import read_text_csv
from .money import to_number

SHOPIFY_COLUMNS = {
    "order_id": "Name",
    "date": "Created at",
    "sku": "Lineitem SKU",
    "qty": "Lineitem quantity",
    "unit_price": "Lineitem price",
    "item_name": "Lineitem name",
}

# Optional. Each field lists the names seen for it; the first one present in the file is used.
# The real 2026 export says "Cancelled at" while Shopify Help spells it "Canceled at".
ORDER_LEVEL_COLUMNS = {
    "shipping_charged": ["Shipping"],
    "discount_amount": ["Discount Amount"],
    "line_discount": ["Lineitem discount"],
    "refunded": ["Refunded Amount"],
    "financial_status": ["Financial Status"],
    "canceled_at": ["Cancelled at", "Canceled at"],
    "discount_code": ["Discount Code"],
    "total": ["Total"],
    "taxes": ["Taxes"],
}
ORDER_LEVEL_FIELDS = ["date", "financial_status", "canceled_at", "total", "taxes"]

SKIPPED_FINANCIAL_STATUSES = {"refunded", "voided"}


def _shop_local_dates(values: pd.Series) -> pd.Series:
    """'2026-11-16 21:30:00 -0500' -> 2026-11-16. The date the shop saw, not the UTC date."""
    without_offset = values.str.replace(r"\s*(?:[+-]\d{2}:?\d{2}|Z)$", "", regex=True)
    return pd.to_datetime(without_offset, format="mixed").dt.normalize()


def _first_present(raw: pd.DataFrame, names: list[str]) -> pd.Series:
    for name in names:
        if name.lower() in raw.columns:
            return raw[name.lower()].str.strip()
    return pd.Series("", index=raw.index)


def read_shopify_orders(path: str | Path, existing_orders: pd.DataFrame, products: pd.DataFrame,
                        default_shipping_cost: float = 0.0, item_names: pd.DataFrame | None = None) -> pd.DataFrame:
    raw = read_text_csv(path)
    raw.columns = [column.strip().lower() for column in raw.columns]

    missing = [column for column in SHOPIFY_COLUMNS.values() if column.lower() not in raw.columns]
    if missing:
        raise ValueError(f"Shopify CSV is missing expected columns: {', '.join(missing)}")

    items = pd.DataFrame({field: raw[column.lower()].str.strip() for field, column in SHOPIFY_COLUMNS.items()})
    for field, names in ORDER_LEVEL_COLUMNS.items():
        items[field] = _first_present(raw, names)
    items = items[items["order_id"] != ""].reset_index(drop=True)

    # Extra line items leave order-level fields blank: copy them down from the order's first row.
    for field in ORDER_LEVEL_FIELDS:
        items[field] = items[field].replace("", pd.NA).groupby(items["order_id"]).transform("first").fillna("")

    skipped = (items["canceled_at"] != "") | items["financial_status"].str.lower().isin(SKIPPED_FINANCIAL_STATUSES)
    warnings = []
    skipped_orders = list(dict.fromkeys(items.loc[skipped, "order_id"]))
    if skipped_orders:
        warnings.append(f"{len(skipped_orders)} cancelled or fully refunded Shopify order(s) left out: "
                        f"{', '.join(skipped_orders)}. If they were paid by card through Shopify Payments, "
                        "the processing fee on the original charge is usually not returned, and that cost is "
                        "not in this report.")
    items = items[~skipped].reset_index(drop=True)
    first_line = ~items["order_id"].duplicated(keep="first")

    # Not official Shopify guidance, only community reports: "Discount Amount" can already include a refund.
    # Then subtracting both counts the refund twice. Flag those orders instead of guessing.
    heads = items[first_line]
    disc, refund = to_number(heads["discount_amount"]), to_number(heads["refunded"])
    suspicious = heads.loc[(refund > 0) & ((disc - refund).abs() < 0.005) & (heads["discount_code"] == ""), "order_id"]
    if not suspicious.empty:
        warnings.append("Shopify order(s) where Discount Amount equals Refunded Amount and no discount code was used: "
                        f"{', '.join(suspicious)}. The refund may be counted twice; check these orders in Shopify.")

    items["date"] = _shop_local_dates(items["date"])
    items["qty"] = to_number(items["qty"])
    items["unit_price"] = to_number(items["unit_price"])
    items["shipping_charged"] = to_number(items["shipping_charged"]).where(first_line, 0.0)
    # "Refunded Amount" includes the refunded tax, while Taxes and Total keep their original values
    # (fern-and-wick-test export, 17 Nov: $22.00 item + $1.95 tax refunded = 23.95). Our sales exclude tax,
    # so only the pre-tax share of the refund is subtracted, split in proportion to the order's tax.
    refunded = to_number(items["refunded"])
    total, taxes = to_number(items["total"]), to_number(items["taxes"])
    pre_tax_share = ((total - taxes) / total).where((taxes > 0) & (total > 0), 1.0)
    items["refund"] = (refunded * pre_tax_share).round(2).where(first_line, 0.0)
    partial = items.loc[first_line & (refunded > 0), "order_id"]
    if not partial.empty:
        message = (f"{len(partial)} partially refunded Shopify order(s): {', '.join(partial)}. The export doesn't say "
                   "which item was refunded, so each refund is taken from the order's first line and the item still "
                   "counts as sold: order totals are right, but profit per product for these orders may be shifted.")
        taxed_refunds = items.loc[first_line & (refunded > 0) & (taxes > 0), "order_id"]
        if not taxed_refunds.empty:
            message += (f" Order(s) with tax ({', '.join(taxed_refunds)}): Shopify's Refunded Amount includes refunded "
                        "tax, so the refund was split into item and tax in proportion to the order. Exact for "
                        "returned items; check refunds that left tax untouched.")
        warnings.append(message)

    # In the real export "Discount Amount" (first row) already contains the line-item discounts, so the
    # line discount stays on its own product and only the rest is treated as an order-level discount.
    line_discount = to_number(items["line_discount"])
    order_discount = to_number(items["discount_amount"]).where(first_line, 0.0)
    remainder = order_discount - line_discount.groupby(items["order_id"]).transform("sum").where(first_line, 0.0)
    uneven = items.loc[first_line & (remainder < -0.005), "order_id"]
    if not uneven.empty:
        warnings.append("Shopify order(s) where line-item discounts are larger than Discount Amount: "
                        f"{', '.join(uneven)}. Only the line-item discounts were used; check these orders in Shopify.")
    items["discount"] = line_discount + remainder.clip(lower=0.0)

    # Cross-check: our order total should equal Shopify's Total minus Taxes minus Refunded Amount.
    has_total = items["total"] != ""
    if has_total.any():
        lines = items[has_total]
        ours = (lines["qty"] * lines["unit_price"] + lines["shipping_charged"] - lines["discount"]
                - lines["refund"]).groupby(lines["order_id"], sort=False).sum()
        heads = lines[~lines["order_id"].duplicated()].set_index("order_id")
        refund_kept_out = lines.loc[~lines["order_id"].duplicated(), ["order_id", "refund"]].set_index("order_id")["refund"]
        theirs = (to_number(heads["total"]) - to_number(heads["taxes"]) - refund_kept_out).reindex(ours.index)
        off = ours[(ours - theirs).abs() > 0.01]
        if not off.empty:
            sample = ", ".join(f"{order} (ours {ours[order]:.2f}, Shopify {theirs[order]:.2f})" for order in off.index[:5])
            warnings.append(f"{len(off)} Shopify order total(s) differ from Total minus Taxes minus the refund. "
                            f"Check how this shop's export counts discounts, tax or refunds: {sample}")

    items["channel"] = "Shopify"
    items["shipping_cost"] = pd.Series(float(default_shipping_cost), index=items.index).where(first_line, 0.0)

    result = mark_import_status(items, existing_orders, products, item_names)
    result.attrs["warnings"] = warnings + result.attrs["warnings"]
    return result
