"""Pure calculations based only on entered positions and provider quotes."""


def holding_metrics(holding: dict, quote: dict | None = None) -> dict[str, float | None]:
    shares = float(holding["shares"])
    average_cost = float(holding["average_cost"])
    cost_basis = shares * average_cost
    price = quote.get("price") if quote else None
    change = quote.get("change") if quote else None
    market_value = shares * float(price) if price is not None else None
    return {
        "cost_basis": cost_basis,
        "market_value": market_value,
        "unrealized_pl": market_value - cost_basis if market_value is not None else None,
        "daily_pl": shares * float(change) if change is not None else None,
    }


def portfolio_totals(
    holdings: list[dict], quotes: dict[str, dict] | None = None, cash: float = 0
) -> dict[str, float | int | None]:
    quotes = quotes or {}
    metrics = [holding_metrics(item, quotes.get(item["symbol"].upper())) for item in holdings]
    missing_prices = sum(item["market_value"] is None for item in metrics)
    missing_changes = sum(item["daily_pl"] is None for item in metrics)
    invested_value = sum(
        float(item["market_value"]) for item in metrics if item["market_value"] is not None
    )
    total_value = float(cash) + invested_value
    return {
        "cost_basis": sum(float(item["cost_basis"]) for item in metrics),
        "invested_value": invested_value,
        "total_value": total_value if missing_prices == 0 else None,
        "unrealized_pl": (
            sum(float(item["unrealized_pl"]) for item in metrics)
            if missing_prices == 0 else None
        ),
        "daily_pl": (
            sum(float(item["daily_pl"]) for item in metrics)
            if missing_changes == 0 else None
        ),
        "cash_allocation": cash / total_value if missing_prices == 0 and total_value > 0 else None,
        "missing_prices": missing_prices,
        "missing_changes": missing_changes,
    }
