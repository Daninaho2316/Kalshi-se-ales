"""Prueba rapida de conexion: correlo en tu Terminal real (no desde el puente de Claude),
porque necesita salida directa a internet hacia Kalshi.

Uso:
  cd ~/Documents/kalshi-signals
  python3 -m pip install --user -r requirements.txt
  python3 test_connection.py
"""
import json
from kalshi_client import KalshiClient

with open("config/secrets.json") as f:
    cfg = json.load(f)

client = KalshiClient(cfg["kalshi_key_id"], cfg["kalshi_private_key_path"])

print("== Balance de tu cuenta (solo lectura) ==")
print(client.portfolio_balance())

print("\n== Precio real BTC (CF Benchmarks BRTI) ==")
print(client.cfbenchmarks_value("BRTI"))

print("\n== Mercado BTC 15min abierto mas cercano ==")
data = client.get_markets(series_ticker="KXBTC15M", status="open")
markets = data.get("markets", [])
if markets:
    m = sorted(markets, key=lambda x: x.get("close_time", ""))[0]
    print(json.dumps(m, indent=2, ensure_ascii=False))
else:
    print("No se encontraron mercados abiertos de KXBTC15M (revisar el series_ticker)")
