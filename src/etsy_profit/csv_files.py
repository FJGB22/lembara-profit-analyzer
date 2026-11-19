from pathlib import Path

import pandas as pd


def read_text_csv(path: str | Path) -> pd.DataFrame:
    """Read a CSV as text. Exports are UTF-8, but a file re-saved in Excel on Windows is often cp1252."""
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except UnicodeDecodeError:
        return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp1252")
