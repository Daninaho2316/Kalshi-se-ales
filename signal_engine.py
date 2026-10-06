"""
Motor de señales Kalshi (BTC/ETH/SOL/XRP, mercados de 15 min).
Compara el precio real (CF Benchmarks via Kalshi) contra el strike del
mercado activo y manda alertas a Telegram cuando hay divergencia + momentum.

Diseñado para correr cada 2-5 min desde GitHub Actions (o manualmente).
Config via variables de entorno:
  KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import csv
import json
import os
import time
from datetime import datetime, timezone

from kalshi_client import KalshiClient
from telegram_notify import send_message

STATE_PATH = "state.json"
CSV_PATH = "logs/signals.csv"

# series Kalshi -> indice CF Benchmarks
SERIES_TO_INDEX = {
    "KXBTC15M": "BRTI",
    "KXETH15M": "ETHUSD_RTI",
    "KXSOL15M": "SOLUSD_RTI",
    "KXXRP15M": "XRPUSD_RTI",
}

# umbral de divergencia % para disparar señal (punto de partida, calibrar con backtesting)
THRESHOLD_PCT = {
    "KXBTC15M": 0.04,
    "KXETH15M": 0.06,
    "KXSOL15M": 0.10,
    "KXXRP15M": 0.12,
}

MOMENTUM_WINDOW = 5          # lecturas a mantener por serie
MIN_MINUTES_LEFT = 2.0       # no disparar señal si falta menos de esto para el cierre


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"momentum": {}, "sent_signals": {}, "pending_results": {}}


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def ensure_csv():
    os.makedirs("logs", exist_ok=True)
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow(
                ["timestamp_utc", "market_ticker", "series", "direction",
                 "price_at_signal", "strike", "pct_diff", "minutes_left",
                 "resultado"]
            )


def log_signal(row: list):
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow(row)


def mark_result(market_ticker: str, resultado: str):
    """Reescribe la fila correspondiente con el resultado final (yes/no settled)."""
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH) as f:
        rows = list(csv.reader(f))
    for row in rows[1:]:
        if row[1] == market_ticker and row[8] == "":
            row[8] = resultado
    with open(CSV_PATH, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def nearest_open_market(client: KalshiClient, series_ticker: str) -> dict | None:
    data = client.get_markets(series_ticker=series_ticker, status="open")
    markets = data.get("markets", [])
    if not markets:
        return None
    markets.sort(key=lambda m: m.get("close_time", ""))
    return markets[0]


def extract_live_price(cfb_response: dict) -> float | None:
    payload = cfb_response.get("data", {}).get("payload", {})
    # el passthrough devuelve el objeto de CF Benchmarks tal cual; el valor
    # suele venir en payload["value"] o payload["price"] segun el indice
    for key in ("value", "price", "index_value"):
        if key in payload:
            return float(payload[key])
    return None


def parse_close_time(market: dict) -> float:
    ct = market.get("close_time")
    dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
    return dt.timestamp()


def run():
    key_id = os.environ["KALSHI_KEY_ID"]
    pkey_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    client = KalshiClient(key_id, pkey_path)
    state = load_state()
    ensure_csv()
    now = time.time()

    for series_ticker, index_id in SERIES_TO_INDEX.items():
        try:
            market = nearest_open_market(client, series_ticker)
            if not market:
                continue
            strike = market.get("floor_strike") or market.get("cap_strike")
            if strike is None:
                continue
            close_ts = parse_close_time(market)
            minutes_left = (close_ts - now) / 60.0
            if minutes_left <= MIN_MINUTES_LEFT:
                continue

            cfb = client.cfbenchmarks_value(index_id)
            price = extract_live_price(cfb)
            if price is None:
                continue

            mom = state["momentum"].setdefault(series_ticker, [])
            mom.append({"t": now, "price": price})
            state["momentum"][series_ticker] = mom[-MOMENTUM_WINDOW:]

            pct_diff = (price - strike) / strike * 100.0
            threshold = THRESHOLD_PCT.get(series_ticker, 0.05)

            direction = None
            if abs(pct_diff) >= threshold and len(mom) >= 3:
                trend_up = mom[-1]["price"] > mom[0]["price"]
                if pct_diff > 0 and trend_up:
                    direction = "LONG"
                elif pct_diff < 0 and not trend_up:
                    direction = "SHORT"

            ticker = market["ticker"]
            dedupe_key = f"{ticker}:{direction}"
            if direction and dedupe_key not in state["sent_signals"]:
                emoji = "🟢" if direction == "LONG" else "🔴"
                asset = series_ticker.replace("KX", "").replace("15M", "")
                msg = (
                    f"{emoji} <b>{asset} 15min — Señal {direction}</b>\n"
                    f"Precio real: {price:,.2f}\n"
                    f"Strike: {strike:,.2f}\n"
                    f"Divergencia: {pct_diff:+.3f}%\n"
                    f"Cierra en: {minutes_left:.1f} min\n"
                    f"Mercado: {ticker}"
                )
                send_message(bot_token, chat_id, msg)
                state["sent_signals"][dedupe_key] = True
                state["pending_results"][ticker] = {
                    "series": series_ticker,
                    "direction": direction,
                    "close_ts": close_ts,
                }
                log_signal([
                    datetime.now(timezone.utc).isoformat(), ticker, series_ticker,
                    direction, f"{price:.4f}", f"{strike:.4f}", f"{pct_diff:.4f}",
                    f"{minutes_left:.1f}", "",
                ])
        except Exception as e:
            print(f"[WARN] {series_ticker}: {e}")

    # revisar resultados de señales cuyo mercado ya cerro
    still_pending = {}
    for ticker, info in state["pending_results"].items():
        if now < info["close_ts"] + 90:  # dar 90s de margen para que Kalshi liquide
            still_pending[ticker] = info
            continue
        try:
            m = client.get_market(ticker)
            market_data = m.get("market", m)
            status = market_data.get("status")
            if status in ("settled", "finalized", "closed"):
                result = market_data.get("result", "")  # "yes" / "no"
                acerto = (
                    (info["direction"] == "LONG" and result == "yes")
                    or (info["direction"] == "SHORT" and result == "no")
                )
                resultado = "ACIERTO" if acerto else "FALLO"
                mark_result(ticker, resultado)
                icon = "✅" if acerto else "❌"
                send_message(
                    bot_token, chat_id,
                    f"{icon} Resultado {ticker}: {resultado} (señal {info['direction']}, settle={result})",
                )
            else:
                still_pending[ticker] = info
        except Exception as e:
            print(f"[WARN] revisando resultado {ticker}: {e}")
            still_pending[ticker] = info
    state["pending_results"] = still_pending

    save_state(state)


if __name__ == "__main__":
    run()
