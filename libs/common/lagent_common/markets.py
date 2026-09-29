"""Where a seller sells: country -> name + currency. Listing Agent writes listings, researches prices
and reads voice commands for this market. Keep in sync with apps/web/lib/markets.ts."""
from __future__ import annotations

from dataclasses import dataclass

# code: (country name, currency, currency symbol)
MARKETS: dict[str, tuple[str, str, str]] = {
    "PK": ("Pakistan", "PKR", "Rs."),
    "US": ("United States", "USD", "$"),
    "GB": ("United Kingdom", "GBP", "£"),
    "CA": ("Canada", "CAD", "CA$"),
    "AU": ("Australia", "AUD", "A$"),
    "NZ": ("New Zealand", "NZD", "NZ$"),
    "IE": ("Ireland", "EUR", "€"),
    "DE": ("Germany", "EUR", "€"),
    "FR": ("France", "EUR", "€"),
    "IT": ("Italy", "EUR", "€"),
    "ES": ("Spain", "EUR", "€"),
    "NL": ("Netherlands", "EUR", "€"),
    "AE": ("United Arab Emirates", "AED", "AED"),
    "SA": ("Saudi Arabia", "SAR", "SAR"),
    "QA": ("Qatar", "QAR", "QAR"),
    "IN": ("India", "INR", "₹"),
    "BD": ("Bangladesh", "BDT", "Tk"),
    "LK": ("Sri Lanka", "LKR", "Rs."),
    "MY": ("Malaysia", "MYR", "RM"),
    "SG": ("Singapore", "SGD", "S$"),
    "ZA": ("South Africa", "ZAR", "R"),
    "TR": ("Türkiye", "TRY", "₺"),
}
DEFAULT = "PK"


@dataclass(frozen=True)
class Market:
    country: str = DEFAULT      # ISO 3166 code, e.g. "PK"
    currency: str = "PKR"       # ISO 4217 code

    @property
    def country_name(self) -> str:
        return MARKETS.get(self.country, MARKETS[DEFAULT])[0]

    @property
    def symbol(self) -> str:
        for _, cur, sym in MARKETS.values():
            if cur == self.currency:
                return sym
        return self.currency

    def money(self, amount: float) -> str:
        whole = float(amount).is_integer()
        decimals = 0 if self.currency in ("PKR", "INR", "BDT", "LKR") or amount >= 1000 or whole else 2
        sep = "" if self.symbol in ("$", "£", "€", "₹", "₺", "CA$", "A$", "NZ$", "S$") else " "
        return f"{self.symbol}{sep}{amount:,.{decimals}f}"

    @staticmethod
    def of(data: dict | None) -> "Market":
        data = data or {}
        country = str(data.get("country") or DEFAULT).upper()
        if country not in MARKETS:
            country = DEFAULT
        currency = str(data.get("currency") or MARKETS[country][1]).upper()
        if currency not in {c for _, c, _ in MARKETS.values()}:
            currency = MARKETS[country][1]
        return Market(country, currency)


def currency_for(country: str) -> str:
    return MARKETS.get(country.upper(), MARKETS[DEFAULT])[1]


CURRENCIES = sorted({c for _, c, _ in MARKETS.values()})
