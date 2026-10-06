"""Diagnostico puntual: por que no se abren posiciones si el activo se mueve.
Imprime solo los campos de precio relevantes del mercado (sin dump completo)."""
import os
from kalshi_client import KalshiClient

client = KalshiClient(os.environ["KALSHI_KEY_ID"], os.environ["KALSHI_PRIVATE_KEY_PATH"])

for series in ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]:
    data = client.get_markets(series_ticker=series, status="open")
    markets = data.get("markets", [])
    print(f"=== {series}: {len(markets)} mercados abiertos ===")
    if markets:
        m = markets[0]
        print("ticker:", m.get("ticker"))
        print("close_time:", m.get("close_time"))
        for k in ["yes_bid", "yes_ask", "no_bid", "no_ask", "last_price",
                   "yes_bid_dollars", "yes_ask_dollars"]:
            print(f"  {k} = {m.get(k)!r}")
        print("  TODAS LAS CLAVES:", sorted(m.keys()))
