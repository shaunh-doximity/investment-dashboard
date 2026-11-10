"""Small local ticker map; labels are suggestions until the user verifies them."""

from __future__ import annotations

from dashboard.market_history import SECTOR_SYMBOLS


ASSET_TYPES = ("Stock", "ETF", "Foreign/OTC Stock", "Cash", "Other")
BENCHMARK_TYPES = ("U.S. sector proxy", "International/local benchmark", "Fund's own benchmark", "None")
CLASSIFICATION_SOURCES = ("Automatic", "Manual", "Fund Look-through")
VERIFICATION_STATUSES = ("Unverified", "Verified", "Needs review")
UNCLASSIFIED = "Diversified / unclassified"

SECTOR_LABELS = {symbol: f"{name} - {symbol}" for symbol, name in SECTOR_SYMBOLS.items()}
SECTOR_CHOICES = tuple(SECTOR_LABELS[s] for s in SECTOR_SYMBOLS) + (
    "Diversified / S&P 500", "Diversified / Equal-weight S&P 500",
    "Diversified / Nasdaq-100", "Industry / Semiconductors",
    "Fixed Income / Treasury", "Commodity / Gold", "Commodity / Oil",
    "Other / unclassified",
)


def local_classification(symbol: str) -> dict:
    """Return a conservative local mapping, or an explicitly unclassified record."""
    symbol = str(symbol).strip().upper()
    mapped = {
        "ADSK": ("Stock", SECTOR_LABELS["XLK"], "XLK", "U.S. sector proxy"),
        "AMZN": ("Stock", SECTOR_LABELS["XLY"], "XLY", "U.S. sector proxy"),
        "HOOD": ("Stock", SECTOR_LABELS["XLF"], "XLF", "U.S. sector proxy"),
        "LULU": ("Stock", SECTOR_LABELS["XLY"], "XLY", "U.S. sector proxy"),
        "MCD": ("Stock", SECTOR_LABELS["XLY"], "XLY", "U.S. sector proxy"),
        "TCEHY": ("Foreign/OTC Stock", "Communication Services", "XLC", "U.S. sector proxy"),
        "TTWO": ("Stock", SECTOR_LABELS["XLC"], "XLC", "U.S. sector proxy"),
        "QQQ": ("ETF", "Diversified / Nasdaq-100", "QQQ", "Fund's own benchmark"),
        "SPY": ("ETF", "Diversified / S&P 500", "SPY", "Fund's own benchmark"),
        "RSP": ("ETF", "Diversified / Equal-weight S&P 500", "RSP", "Fund's own benchmark"),
        "DIA": ("ETF", "Diversified / Dow Jones Industrial Average", "DIA", "Fund's own benchmark"),
        "IWM": ("ETF", "Diversified / Russell 2000", "IWM", "Fund's own benchmark"),
        "SMH": ("ETF", "Industry / Semiconductors", "SMH", "Fund's own benchmark"),
        "TLT": ("ETF", "Fixed Income / Treasury", "TLT", "Fund's own benchmark"),
        "GLD": ("ETF", "Commodity / Gold", "GLD", "Fund's own benchmark"),
        "USO": ("ETF", "Commodity / Oil", "USO", "Fund's own benchmark"),
    }
    for sector_symbol, label in SECTOR_LABELS.items():
        mapped.setdefault(sector_symbol, ("ETF", label, sector_symbol, "Fund's own benchmark"))
    asset, sector, benchmark, benchmark_type = mapped.get(
        symbol, ("Other", UNCLASSIFIED, "", "None")
    )
    return {
        "asset_type": asset,
        "sector": sector,
        "sector_benchmark": benchmark,
        "benchmark_type": benchmark_type,
        "classification_source": "Automatic",
        "verification_status": "Unverified",
    }


def sector_bucket(record: dict | str | None) -> str:
    """Return a sector label suitable for sector-only aggregations, if applicable."""
    sector = record if isinstance(record, str) else (record or {}).get("sector", "")
    if not sector:
        return UNCLASSIFIED
    if sector in SECTOR_LABELS.values():
        return sector
    return sector
