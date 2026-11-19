from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .csv_files import read_text_csv
from .money import to_number

REQUIRED_COLUMNS = {
    "products": ["sku", "product", "unit_cost", "price", "starting_stock", "reorder_at"],
    "materials": ["material", "pack_price", "pack_size"],
    "recipes": ["sku", "material", "qty_per_item"],
    "orders": ["date", "order_id", "channel", "sku", "qty", "unit_price", "shipping_charged", "shipping_cost"],
    "fees": ["channel", "pct_of_order_total", "fixed_per_order", "per_item_sold"],
}

NUMBER_COLUMNS = {
    "products": ["unit_cost", "price", "starting_stock", "reorder_at"],
    "materials": ["pack_price", "pack_size"],
    "recipes": ["qty_per_item"],
    "orders": ["qty", "unit_price", "shipping_charged", "shipping_cost", "discount", "refund"],
    "fees": ["pct_of_order_total", "fixed_per_order", "per_item_sold"],
}

OPTIONAL_FILES = {"materials", "recipes"}
OPTIONAL_COLUMNS = {"orders": {"discount": "0", "refund": "0"}}


@dataclass
class Shop:
    products: pd.DataFrame
    materials: pd.DataFrame
    recipes: pd.DataFrame
    orders: pd.DataFrame
    fees: pd.DataFrame
    item_names: pd.DataFrame | None = None  # optional item_names.csv: marketplace item name -> sku


def _empty_table(name: str) -> pd.DataFrame:
    return pd.DataFrame(columns=REQUIRED_COLUMNS[name])


def _read_table(data_dir: Path, name: str) -> pd.DataFrame:
    path = data_dir / f"{name}.csv"
    if not path.exists():
        if name in OPTIONAL_FILES:
            return _empty_table(name)
        raise FileNotFoundError(f"Missing {path}. Expected columns: {', '.join(REQUIRED_COLUMNS[name])}")

    table = read_text_csv(path)
    table.columns = [column.strip().lower() for column in table.columns]

    missing = [column for column in REQUIRED_COLUMNS[name] if column not in table.columns]
    if missing:
        raise ValueError(f"{path.name} is missing columns: {', '.join(missing)}")

    for column, default in OPTIONAL_COLUMNS.get(name, {}).items():
        if column not in table.columns:
            table[column] = default

    for column in NUMBER_COLUMNS[name]:
        try:
            table[column] = to_number(table[column])
        except ValueError as error:
            raise ValueError(f"{path.name}, column '{column}' has a value that is not a number") from error

    for column in ("sku", "material", "channel", "order_id"):
        if column in table.columns:
            table[column] = table[column].str.strip()

    return table


def load_shop(data_dir: str | Path) -> Shop:
    data_dir = Path(data_dir)
    tables = {name: _read_table(data_dir, name) for name in REQUIRED_COLUMNS}
    tables["orders"]["date"] = pd.to_datetime(tables["orders"]["date"], errors="raise")
    return Shop(**tables, item_names=_read_item_names(data_dir))


def _read_item_names(data_dir: Path) -> pd.DataFrame | None:
    path = data_dir / "item_names.csv"
    if not path.exists():
        return None
    table = read_text_csv(path)
    table.columns = [column.strip().lower() for column in table.columns]
    missing = [column for column in ("item_name", "sku") if column not in table.columns]
    if missing:
        raise ValueError(f"{path.name} is missing columns: {', '.join(missing)}")
    return table.assign(item_name=table["item_name"].str.strip(), sku=table["sku"].str.strip())
