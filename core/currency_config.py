"""
Currency Configuration for CLM System
Default currency: CLP (Chilean Pesos)
Changeable to: EUR, USD
"""

CURRENCY_DEFAULT = "CLP"

CURRENCY_SYMBOLS = {
    "CLP": "$",
    "EUR": "€",
    "USD": "$",
}

CURRENCY_NAMES = {
    "CLP": "Pesos Chilenos",
    "EUR": "Euros",
    "USD": "Dólares",
}

# Exchange rates relative to CLP
EXCHANGE_RATES = {
    "CLP": 1.0,        # Base
    "USD": 900.0,      # 1 USD ≈ 900 CLP
    "EUR": 1000.0,     # 1 EUR ≈ 1000 CLP
}

# Supported currencies
SUPPORTED_CURRENCIES = ["CLP", "USD", "EUR"]


def get_currency_config(currency=None):
    """
    Get currency configuration.

    Args:
        currency (str): Currency code (CLP, USD, EUR). Defaults to CURRENCY_DEFAULT.

    Returns:
        dict: Currency configuration with symbol, name, and exchange rate.
    """
    if currency is None:
        currency = CURRENCY_DEFAULT

    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported currency: {currency}. Supported: {SUPPORTED_CURRENCIES}")

    return {
        "code": currency,
        "symbol": CURRENCY_SYMBOLS[currency],
        "name": CURRENCY_NAMES[currency],
        "exchange_rate": EXCHANGE_RATES[currency],
    }


def convert_to_currency(amount_clp, from_currency="CLP", to_currency="CLP"):
    """
    Convert amount between currencies.

    Args:
        amount_clp (float): Amount in CLP (base currency)
        from_currency (str): Source currency code
        to_currency (str): Target currency code

    Returns:
        float: Converted amount
    """
    if from_currency not in SUPPORTED_CURRENCIES or to_currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported currency pair: {from_currency} -> {to_currency}")

    # Convert from source to CLP first
    amount_in_clp = amount_clp * EXCHANGE_RATES[from_currency]

    # Then convert from CLP to target
    converted = amount_in_clp / EXCHANGE_RATES[to_currency]

    return converted
