from pathlib import Path

import pandas as pd

from .analysis import Report

SUMMARY_LABELS = {
    "sales": "Sales",
    "platform_fees": "Platform fees",
    "product_and_shipping_costs": "Product + shipping costs",
    "net_profit": "Net profit",
    "profit_margin": "Profit margin",
    "orders": "Orders",
    "first_date": "First order",
    "last_date": "Last order",
}


def report_tables(report: Report) -> dict[str, pd.DataFrame]:
    summary = pd.DataFrame(
        [(SUMMARY_LABELS[key], value) for key, value in report.summary.items()], columns=["metric", "value"]
    )
    orders = report.orders.assign(date=report.orders["date"].dt.date)
    losing = report.losing_orders.assign(date=report.losing_orders["date"].dt.date)
    tables = {
        "Summary": summary,
        "By product": report.by_product,
        "By channel": report.by_channel,
        "By month": report.by_month,
        "Restock": report.restock,
        "Losing orders": losing,
        "Item costs": report.item_costs,
        "Orders": orders,
    }
    if report.warnings:
        tables["Warnings"] = pd.DataFrame({"warning": report.warnings})
    return tables


def write_excel(report: Report, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, table in report_tables(report).items():
            table.to_excel(writer, sheet_name=name, index=False)
            sheet = writer.sheets[name]
            sheet.freeze_panes = "A2"
            for column_cells in sheet.columns:
                width = max(len(str(cell.value)) if cell.value is not None else 0 for cell in column_cells)
                sheet.column_dimensions[column_cells[0].column_letter].width = min(max(width + 2, 10), 45)
    return path


def write_google_sheet(report: Report, spreadsheet_id: str, credentials_file: str | Path) -> str:
    import gspread

    client = gspread.service_account(filename=str(credentials_file))
    spreadsheet = client.open_by_key(spreadsheet_id)

    for name, table in report_tables(report).items():
        title = f"Report - {name}"
        try:
            worksheet = spreadsheet.worksheet(title)
            worksheet.clear()
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=title, rows=len(table) + 10, cols=len(table.columns) + 2)
        values = [list(table.columns)] + table.astype(object).where(table.notna(), "").astype(str).values.tolist()
        worksheet.update(values, value_input_option="USER_ENTERED")

    return spreadsheet.url
