from dataclasses import dataclass, field

import pandas as pd

from .costing import cost_per_item
from .fees import order_line_fees
from .loaders import Shop
from .money import round_money


@dataclass
class Report:
    orders: pd.DataFrame
    summary: dict
    by_channel: pd.DataFrame
    by_product: pd.DataFrame
    by_month: pd.DataFrame
    restock: pd.DataFrame
    losing_orders: pd.DataFrame
    item_costs: pd.DataFrame
    warnings: list[str] = field(default_factory=list)


def enrich_orders(shop: Shop) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    item_costs, warnings = cost_per_item(shop.products, shop.recipes, shop.materials)
    orders = shop.orders.copy()

    unknown_skus = sorted(set(orders["sku"]) - set(shop.products["sku"]))
    if unknown_skus:
        warnings.append(f"Orders contain SKUs missing from products.csv (product cost counted as $0): {', '.join(unknown_skus)}")

    orders["product"] = orders["sku"].map(shop.products.set_index("sku")["product"]).fillna("Unknown SKU")
    for column in ("discount", "refund"):
        orders[column] = orders[column].fillna(0.0) if column in orders.columns else 0.0
    # Refunds lower the sale, but payment processors keep the fee on the original charge (see fees.py).
    orders["order_total"] = (orders["qty"] * orders["unit_price"] + orders["shipping_charged"]
                             - orders["discount"] - orders["refund"])

    fees, fee_warnings = order_line_fees(orders, shop.fees)
    warnings.extend(fee_warnings)
    orders["platform_fees"] = fees

    cost_used = orders["sku"].map(item_costs.set_index("sku")["cost_used"]).fillna(0.0)
    orders["product_cost"] = (orders["qty"] * cost_used).map(round_money)
    orders["net_profit"] = (
        orders["order_total"] - orders["platform_fees"] - orders["product_cost"] - orders["shipping_cost"]
    ).map(round_money)

    return orders, item_costs, warnings


def summarize(orders: pd.DataFrame) -> dict:
    sales = orders["order_total"].sum()
    fees = orders["platform_fees"].sum()
    costs = orders["product_cost"].sum() + orders["shipping_cost"].sum()
    profit = orders["net_profit"].sum()
    return {
        "sales": round_money(sales),
        "platform_fees": round_money(fees),
        "product_and_shipping_costs": round_money(costs),
        "net_profit": round_money(profit),
        "profit_margin": round(profit / sales, 4) if sales else 0.0,
        "orders": int(orders["order_id"].nunique()),
        "first_date": orders["date"].min().date().isoformat() if len(orders) else None,
        "last_date": orders["date"].max().date().isoformat() if len(orders) else None,
    }


def _money_columns(table: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    for column in columns:
        table[column] = table[column].map(round_money)
    return table


def by_channel(orders: pd.DataFrame) -> pd.DataFrame:
    table = (
        orders.groupby("channel", as_index=False)
        .agg(sales=("order_total", "sum"), fees=("platform_fees", "sum"), net_profit=("net_profit", "sum"))
        .sort_values("net_profit", ascending=False, ignore_index=True)
    )
    return _money_columns(table, ["sales", "fees", "net_profit"])


def by_product(orders: pd.DataFrame, products: pd.DataFrame, item_costs: pd.DataFrame) -> pd.DataFrame:
    totals = orders.groupby("sku").agg(
        units_sold=("qty", "sum"), sales=("order_total", "sum"), net_profit=("net_profit", "sum")
    )
    table = products[["sku", "product", "price", "starting_stock", "reorder_at"]].merge(
        item_costs[["sku", "cost_used", "cost_source"]], on="sku", how="left"
    )
    table = table.join(totals, on="sku").fillna({"units_sold": 0, "sales": 0.0, "net_profit": 0.0})
    table["units_sold"] = table["units_sold"].astype(int)
    table["in_stock"] = (table["starting_stock"] - table["units_sold"]).astype(int)
    table["status"] = "OK"
    table.loc[table["in_stock"] <= table["reorder_at"], "status"] = "REORDER"
    table.loc[table["in_stock"] <= 0, "status"] = "OUT OF STOCK"
    table["profit_per_unit"] = (table["net_profit"] / table["units_sold"].where(table["units_sold"] > 0)).fillna(0.0)
    table = _money_columns(table, ["sales", "net_profit", "profit_per_unit"])
    return table.sort_values("net_profit", ascending=False, ignore_index=True)


def by_month(orders: pd.DataFrame) -> pd.DataFrame:
    months = orders.assign(month=orders["date"].dt.strftime("%Y-%m"))
    table = months.groupby("month", as_index=False).agg(sales=("order_total", "sum"), net_profit=("net_profit", "sum"))
    return _money_columns(table, ["sales", "net_profit"])


def restock(product_table: pd.DataFrame) -> pd.DataFrame:
    needs = product_table[product_table["status"] != "OK"]
    return needs[["sku", "product", "in_stock", "reorder_at", "status"]].sort_values(
        "in_stock", ascending=False, ignore_index=True
    )


def losing_orders(orders: pd.DataFrame) -> pd.DataFrame:
    columns = ["date", "order_id", "channel", "product", "qty", "discount", "refund", "order_total", "platform_fees",
               "product_cost", "shipping_cost", "net_profit"]
    return orders.loc[orders["net_profit"] < 0, columns].reset_index(drop=True)


def analyze(shop: Shop) -> Report:
    orders, item_costs, warnings = enrich_orders(shop)
    product_table = by_product(orders, shop.products, item_costs)
    return Report(
        orders=orders,
        summary=summarize(orders),
        by_channel=by_channel(orders),
        by_product=product_table,
        by_month=by_month(orders),
        restock=restock(product_table),
        losing_orders=losing_orders(orders),
        item_costs=item_costs,
        warnings=warnings,
    )
