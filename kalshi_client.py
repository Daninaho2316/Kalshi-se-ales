"""
Cliente firmado para la API de Kalshi (CF Benchmarks passthrough + mercados).
Usa la api key de solo lectura "Dani2316" y su clave privada local.
Nunca imprime ni transmite el contenido de la clave privada.
"""
import base64
import json
import time
import urllib.request
import urllib.parse
import urllib.error

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE_URL = "https://external-api.kalshi.com"


class KalshiClient:
    def __init__(self, key_id: str, private_key_path: str):
        self.key_id = key_id
        with open(private_key_path, "rb") as f:
            self.private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, method: str, path: str) -> dict:
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        if isinstance(self.private_key, Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
            signature = self.private_key.sign(
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                hashes.SHA256(),
            )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    def _get(self, path: str, params: dict = None) -> dict:
        headers = self._sign("GET", path)
        headers["Accept"] = "application/json"
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code} on {path}: {body}") from None

    # --- CF Benchmarks real-time index values ---
    def cfbenchmarks_value(self, index_id: str) -> dict:
        return self._get("/trade-api/v2/cfbenchmarks/values", {"id": index_id})

    # --- Markets ---
    def get_markets(self, series_ticker: str = None, event_ticker: str = None, status: str = "open") -> dict:
        params = {"status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._get("/trade-api/v2/markets", params)

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/trade-api/v2/markets/{ticker}")

    def get_series_list(self, category: str = None) -> dict:
        params = {"category": category} if category else {}
        return self._get("/trade-api/v2/series", params)

    def portfolio_balance(self) -> dict:
        return self._get("/trade-api/v2/portfolio/balance")


if __name__ == "__main__":
    import sys
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config/secrets.json"
    with open(cfg_path) as f:
        cfg = json.load(f)
    client = KalshiClient(cfg["kalshi_key_id"], cfg["kalshi_private_key_path"])
    print("Probando autenticacion con /portfolio/balance ...")
    print(client.portfolio_balance())
