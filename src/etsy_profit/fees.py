import pandas as pd

from .money import round_money


YES = {"yes", "y", "true", "1"}


def fees_returned_on_refund(fees: pd.DataFrame) -> pd.Series:
    """Per channel: does the platform give its fees back when the seller refunds?

    Read from the optional fees.csv column `fees_returned_on_refund` (yes/no). Without it, only Etsy does.
    """
    if "fees_returned_on_refund" in fees.columns:
        values = fees["fees_returned_on_refund"].astype(str).str.strip().str.lower().isin(YES)
    else:
        values = fees["channel"].eq("Etsy")
    return pd.Series(values.values, index=fees["channel"])


def order_line_fees(orders: pd.DataFrame, fees: pd.DataFrame) -> tuple[pd.Series, list[str]]:
    warnings = []
    rates = fees.set_index("channel")

    unknown = sorted(set(orders["channel"]) - set(rates.index))
    if unknown:
        warnings.append(f"No fee settings for channel(s) {', '.join(unknown)}; their fees are counted as $0")

    pct = orders["channel"].map(rates["pct_of_order_total"]).fillna(0.0)
    fixed = orders["channel"].map(rates["fixed_per_order"]).fillna(0.0)
    per_item = orders["channel"].map(rates["per_item_sold"]).fillna(0.0)

    first_line_of_order = ~orders["order_id"].duplicated(keep="first")
    # Refunds and fees differ per platform. Etsy Help: transaction and processing fees on a refund are
    # credited back (proportionally), so the fee follows the amount kept. Shopify staff: Shopify Payments keeps
    # the original processing fee, so the fee stays on the amount charged before the refund.
    refund = orders["refund"].fillna(0.0) if "refund" in orders.columns else pd.Series(0.0, index=orders.index)
    returned = orders["channel"].map(fees_returned_on_refund(fees)).fillna(False).astype(bool)
    fee_base = orders["order_total"] + refund.where(~returned, 0.0)
    raw = fee_base * pct + fixed.where(first_line_of_order, 0.0) + orders["qty"] * per_item

    return raw.map(round_money).rename("platform_fees"), warnings
