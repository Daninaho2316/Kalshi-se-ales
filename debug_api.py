"""Script temporal de diagnostico: imprime la forma real de las respuestas
de la API de Kalshi (CF Benchmarks y Markets) para ajustar los nombres de
los campos en signal_engine.py. No imprime ninguna clave/secreto."""
import json
import os
from kalshi_client import KalshiClient

client = KalshiClient(os.environ["KALSHI_KEY_ID"], os.environ["KALSHI_PRIVATE_KEY_PATH"])

print("=== cfbenchmarks_value(BRTI) ===")
try:
    print(json.dumps(client.cfbenchmarks_value("BRTI"), indent=2))
except Exception as e:
    print(f"ERROR: {e}")

print("=== get_markets(KXBTC15M) ===")
try:
    data = client.get_markets(series_ticker="KXBTC15M", status="open")
    markets = data.get("markets", [])
    print(f"cantidad de mercados abiertos: {len(markets)}")
    if markets:
        print(json.dumps(markets[0], indent=2))
except Exception as e:
    print(f"ERROR: {e}")
