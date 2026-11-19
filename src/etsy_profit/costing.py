import pandas as pd

from .money import round_money


def material_unit_costs(materials: pd.DataFrame) -> pd.Series:
    pack_size = materials["pack_size"].where(materials["pack_size"] > 0)
    unit_costs = (materials["pack_price"] / pack_size).fillna(0.0)
    return pd.Series(unit_costs.values, index=materials["material"], name="cost_per_unit")


def recipe_costs(recipes: pd.DataFrame, materials: pd.DataFrame) -> tuple[pd.Series, list[str]]:
    warnings = []
    if recipes.empty:
        return pd.Series(dtype=float, name="recipe_cost"), warnings

    unit_costs = material_unit_costs(materials)
    lines = recipes.copy()
    lines["cost_per_unit"] = lines["material"].map(unit_costs)

    unknown = sorted(lines.loc[lines["cost_per_unit"].isna(), "material"].unique())
    if unknown:
        warnings.append(f"Recipes use materials missing from materials.csv (counted as $0): {', '.join(unknown)}")

    lines["line_cost"] = lines["qty_per_item"] * lines["cost_per_unit"].fillna(0.0)
    totals = lines.groupby("sku")["line_cost"].sum().map(round_money)
    totals.name = "recipe_cost"
    return totals, warnings


def cost_per_item(products: pd.DataFrame, recipes: pd.DataFrame, materials: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    from_recipes, warnings = recipe_costs(recipes, materials)

    costs = products[["sku", "product", "unit_cost"]].copy()
    costs["recipe_cost"] = costs["sku"].map(from_recipes).fillna(0.0)
    costs["cost_used"] = costs["recipe_cost"].where(costs["recipe_cost"] > 0, costs["unit_cost"])
    costs["cost_source"] = costs["recipe_cost"].gt(0).map({True: "recipe", False: "unit cost"})

    unknown_recipe_skus = sorted(set(recipes["sku"]) - set(products["sku"]))
    if unknown_recipe_skus:
        warnings.append(f"Recipes exist for SKUs not in products.csv: {', '.join(unknown_recipe_skus)}")

    return costs, warnings
