from pathlib import Path

import pandas as pd

from .csv_files import read_text_csv
from .money import to_number

DEFAULT_ETSY_COLUMNS = {
    "date": "Sale Date",
    "order_id": "Order ID",
    "sku": "SKU",
    "qty": "Quantity",
    "unit_price": "Price",
    "shipping_charged": "Order Shipping",
    "item_name": "Item Name",
}

OPTIONAL_ETSY_COLUMNS = {
    "discount_amount": "Discount Amount",
    "shipping_discount": "Shipping Discount",
}

READY = "Ready"
ALREADY_IMPORTED = "Already in orders - skip"
UNKNOWN_SKU = "SKU not in products - add it first"
FULLY_REFUNDED = "Fully refunded or cancelled - left out"


def read_etsy_order_items(
    path: str | Path,
    existing_orders: pd.DataFrame,
    products: pd.DataFrame,
    column_names: dict[str, str] | None = None,
    default_shipping_cost: float = 0.0,
    item_names: pd.DataFrame | None = None,
) -> pd.DataFrame:
    overrides = column_names or {}
    names = {field: overrides.get(field, column) for field, column in DEFAULT_ETSY_COLUMNS.items()}
    optional = {field: overrides.get(field, column) for field, column in OPTIONAL_ETSY_COLUMNS.items()}
    raw = read_text_csv(path)
    raw.columns = [column.strip() for column in raw.columns]

    missing = [f"{field} -> '{column}'" for field, column in names.items() if column not in raw.columns]
    if missing:
        raise ValueError(
            "Etsy CSV is missing expected columns: " + "; ".join(missing)
            + ". Pass the real column names with column_names={...}."
        )

    items = pd.DataFrame({field: raw[column].str.strip() for field, column in names.items()})
    for field, column in optional.items():
        items[field] = raw[column].str.strip() if column in raw.columns else "0"
    items = items[items["order_id"] != ""].reset_index(drop=True)

    items["date"] = pd.to_datetime(items["date"], format="mixed", dayfirst=False)
    items["qty"] = to_number(items["qty"])
    items["unit_price"] = to_number(items["unit_price"])
    items["shipping_charged"] = to_number(items["shipping_charged"])
    items["discount"] = to_number(items["discount_amount"]) + to_number(items["shipping_discount"])
    repeat_line = items["order_id"].duplicated(keep="first")
    items.loc[repeat_line, "shipping_charged"] = 0.0
    items.loc[repeat_line, "discount"] = 0.0
    items["channel"] = "Etsy"
    items["shipping_cost"] = float(default_shipping_cost)
    items.loc[repeat_line, "shipping_cost"] = 0.0

    return mark_import_status(items, existing_orders, products, item_names)


# Etsy's order-level export (Settings → Options → Download Data → Orders). Column names from Etsy Help's
# 1099-K article (Discount Amount, Shipping Discount, Shipping, Sales Tax, Order Total) and Coupler.io's list
# (Order ID). Shops outside the US may see other names (e.g. "Delivery City"), so every name can be overridden.
DEFAULT_ETSY_ORDER_COLUMNS = {
    "order_id": "Order ID",
    "shipping": "Shipping",
    "discount_amount": "Discount Amount",
    "shipping_discount": "Shipping Discount",
    "sales_tax": "Sales Tax",
    "order_total": "Order Total",
    # Refunds: blank = no change, 0 = fully refunded or cancelled, otherwise the total after a partial refund
    # (Etsy seller forum, not Etsy staff). Etsy Help: Order Total itself never subtracts refunds.
    "adjusted_order_total": "Adjusted Order Total",
    "status": "Status",
}
REQUIRED_ETSY_ORDER_FIELDS = ("order_id", "shipping", "discount_amount")


def read_etsy_orders(path: str | Path, column_names: dict[str, str] | None = None) -> pd.DataFrame:
    """One row per order with Etsy's own order-level shipping, discounts and totals."""
    overrides = column_names or {}
    names = {field: overrides.get(field, column) for field, column in DEFAULT_ETSY_ORDER_COLUMNS.items()}
    raw = read_text_csv(path)
    raw.columns = [column.strip() for column in raw.columns]

    missing = [f"{field} -> '{names[field]}'" for field in REQUIRED_ETSY_ORDER_FIELDS if names[field] not in raw.columns]
    if missing:
        raise ValueError("Etsy Orders CSV is missing expected columns: " + "; ".join(missing)
                         + ". Pass the real column names with column_names={...}.")

    orders = pd.DataFrame({"order_id": raw[names["order_id"]].str.strip()})
    status = names["status"]
    orders["status"] = raw[status].str.strip() if status in raw.columns else ""
    for field in ("shipping", "discount_amount", "shipping_discount", "sales_tax", "order_total",
                  "adjusted_order_total"):
        column = names[field]
        if column not in raw.columns:
            orders[field] = float("nan")
            continue
        values = to_number(raw[column])
        # A blank cell must stay blank: for Adjusted Order Total, blank means "no refund" but 0 means
        # "fully refunded", and to_number would turn both into 0.
        orders[field] = values.where(raw[column].fillna("").str.strip() != "")
    for field in ("shipping", "discount_amount", "shipping_discount"):
        orders[field] = orders[field].fillna(0.0)
    orders = orders[orders["order_id"] != ""].drop_duplicates("order_id")
    return orders.set_index("order_id")


def apply_etsy_orders(items: pd.DataFrame, etsy_orders: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Replace the per-line guesses with Etsy's order-level shipping and discounts, then cross-check totals.

    Order Items repeats order-level values on every line, and whether "Discount Amount" there is per order
    or per item has not been confirmed. The Orders file settles it: one row per order.
    """
    items = items.copy()
    warnings = []
    first_line = ~items["order_id"].duplicated(keep="first")
    known = items["order_id"].isin(etsy_orders.index)

    shipping = items["order_id"].map(etsy_orders["shipping"])
    discount = items["order_id"].map(etsy_orders["discount_amount"] + etsy_orders["shipping_discount"])
    items.loc[known, "shipping_charged"] = shipping[known].where(first_line[known], 0.0)
    items.loc[known, "discount"] = discount[known].where(first_line[known], 0.0)

    # Refunds and cancellations, from Adjusted Order Total.
    adjusted = etsy_orders["adjusted_order_total"]
    has_adjustment = adjusted.notna()
    fully_refunded = etsy_orders.index[has_adjustment & (adjusted.abs() < 0.005)]
    refund = (etsy_orders["order_total"] - adjusted).where(has_adjustment & (adjusted.abs() >= 0.005), 0.0).clip(lower=0.0)
    # The adjusted total includes tax, our sales do not: keep only the pre-tax share of the refund, in proportion
    # to the order's tax (the same rule as Shopify, where a returned item's tax was refunded with it).
    tax = etsy_orders["sales_tax"].fillna(0.0)
    total = etsy_orders["order_total"]
    pre_tax_share = ((total - tax) / total).where((tax > 0) & (total > 0), 1.0)
    refund = (refund * pre_tax_share).round(2)
    items["refund"] = items["order_id"].map(refund).fillna(0.0).where(first_line, 0.0)
    if "status" in items.columns:
        items.loc[items["order_id"].isin(fully_refunded), "status"] = FULLY_REFUNDED
    if len(fully_refunded):
        warnings.append(f"{len(fully_refunded)} fully refunded or cancelled Etsy order(s) left out: "
                        f"{', '.join(fully_refunded[:10])}. Etsy credits back its fees on refunds, so no fee is counted.")
    partial = refund[refund > 0]
    taxed = partial.index[etsy_orders.loc[partial.index, "sales_tax"].fillna(0.0) > 0]
    if len(taxed):
        warnings.append(f"{len(taxed)} partially refunded Etsy order(s) also had sales tax: {', '.join(taxed[:10])}. "
                        "The refund was split into item and tax in proportion to the order; check these in Etsy.")
    odd = etsy_orders.index[~etsy_orders["status"].str.lower().isin(["", "completed"])]
    if len(odd):
        listed = ", ".join(f"{order} ({etsy_orders.at[order, 'status']})" for order in odd[:10])
        warnings.append(f"{len(odd)} Etsy order(s) have a status other than Completed and were kept as sales. "
                        f"Check them in Etsy: {listed}")

    missing = sorted(items.loc[~known, "order_id"].unique())
    if missing:
        warnings.append(f"{len(missing)} Etsy order(s) are not in the Orders CSV, so their shipping and discounts "
                        f"come from the Order Items file: {', '.join(missing[:10])}")

    # Cross-check: our order total should equal Etsy's Order Total (after any refund) minus sales tax.
    checkable = etsy_orders.dropna(subset=["order_total"]).drop(index=fully_refunded)
    if not checkable.empty:
        lines = items[items["order_id"].isin(checkable.index)]
        ours = (lines["qty"] * lines["unit_price"] + lines["shipping_charged"] - lines["discount"]
                - lines["refund"]).groupby(lines["order_id"]).sum()
        theirs = (checkable["order_total"] - checkable["sales_tax"].fillna(0.0) - refund).reindex(ours.index)
        off = ours[(ours - theirs).abs() > 0.01]
        if not off.empty:
            sample = ", ".join(f"{order} (ours {ours[order]:.2f}, Etsy {theirs[order]:.2f})" for order in off.index[:5])
            warnings.append(f"{len(off)} Etsy order total(s) differ from Etsy's Order Total minus Sales Tax. "
                            f"Check how this shop's export counts discounts or tax before trusting these orders: {sample}")
    return items, warnings


IMPORT_COLUMNS = ["date", "order_id", "channel", "sku", "qty", "unit_price", "shipping_charged",
                  "shipping_cost", "discount", "refund", "item_name", "status"]


def _name_key(names: pd.Series) -> pd.Series:
    """'  Lavender  Soy candle ' -> 'lavender soy candle', so small spacing/case differences still match."""
    return names.fillna("").astype(str).str.strip().str.lower().str.split().str.join(" ")


def match_skus(items: pd.DataFrame, products: pd.DataFrame,
               item_names: pd.DataFrame | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Fill in the SKU for lines whose SKU is blank or unknown.

    Many small shops never set SKUs. Order of matching: the SKU itself, then the shop's item_names.csv
    (marketplace item name -> SKU, for long listing titles), then a product with exactly the same name.
    """
    items = items.copy()
    items["sku"] = items["sku"].fillna("").astype(str).str.strip()
    warnings = []
    unknown = ~items["sku"].isin(products["sku"])
    keys = _name_key(items["item_name"])

    lookups = []
    if item_names is not None and not item_names.empty:
        lookups.append(("item_names.csv", item_names.assign(key=_name_key(item_names["item_name"]))))
    lookups.append(("product name", products.assign(key=_name_key(products["product"]))))

    matched_by = {}
    for source, table in lookups:
        table = table[table["key"] != ""]
        unique = table.drop_duplicates("key", keep=False)  # a name used for two SKUs is not a match
        mapping = unique.set_index("key")["sku"]
        hit = unknown & keys.isin(mapping.index)
        items.loc[hit, "sku"] = keys[hit].map(mapping)
        matched_by[source] = int(hit.sum())
        unknown = ~items["sku"].isin(products["sku"])

    for source, count in matched_by.items():
        if count:
            warnings.append(f"{count} order line(s) had no known SKU and were matched by {source}. "
                            "Check that the products are the right ones.")

    left = items[unknown]
    if not left.empty:
        lost = (left["qty"] * left["unit_price"]).sum()
        names = left.assign(name=left["item_name"].replace("", "(no name)")).groupby("name")["qty"].sum()
        listed = ", ".join(f"{name} ({qty:g})" for name, qty in names.sort_values(ascending=False).head(8).items())
        warnings.append(f"{len(left)} order line(s) worth ${lost:,.2f} in item sales match no product and are left "
                        f"out of this report. Add their SKU to products.csv or map the item name in item_names.csv: "
                        f"{listed}")
    return items, warnings


def mark_import_status(items: pd.DataFrame, existing_orders: pd.DataFrame, products: pd.DataFrame,
                       item_names: pd.DataFrame | None = None) -> pd.DataFrame:
    items = items.copy()
    if "refund" not in items.columns:
        items["refund"] = 0.0
    fresh = ~items["order_id"].isin(existing_orders["order_id"])
    matched, warnings = match_skus(items[fresh], products, item_names)
    items.loc[fresh, "sku"] = matched["sku"]
    items["status"] = READY
    items.loc[~items["sku"].isin(products["sku"]), "status"] = UNKNOWN_SKU
    items.loc[~fresh, "status"] = ALREADY_IMPORTED
    result = items[IMPORT_COLUMNS].reset_index(drop=True)
    result.attrs["warnings"] = warnings
    return result


def merge_ready_items(orders: pd.DataFrame, imported: pd.DataFrame) -> pd.DataFrame:
    ready = imported[imported["status"] == READY].drop(columns=["item_name", "status"])
    merged = pd.concat([orders, ready], ignore_index=True)
    for column in ("discount", "refund"):
        merged[column] = merged[column].fillna(0.0) if column in merged.columns else 0.0
    return merged.sort_values("date", kind="stable", ignore_index=True)
