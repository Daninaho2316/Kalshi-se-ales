"""
Motor de señales Kalshi — modelo "entrar y salir" (no apuesta a vencimiento).

Idea central (confirmada por el usuario):
  1. El precio del ACTIVO real (BTC/ETH/SOL/XRP, via CF Benchmarks) se mueve
     unos segundos ANTES de que el precio del contrato de Kalshi lo refleje
     del todo. Ese margen de segundos es la ventaja: cuando el activo muestra
     un movimiento claro en una dirección, ahí se marca una "entrada" al
     contrato de Kalshi (lado YES si el activo sube, NO si baja), al precio
     de ese momento (todavia no ajustado).
  2. Desde ese precio de entrada, el bot vigila el precio del CONTRATO de
     Kalshi. Apenas venderlo ahora mismo diera un 10% de ganancia sobre lo
     invertido en la entrada, se manda la señal a Telegram AL INSTANTE
     ("entrar y salir", no esperar al cierre del ciclo — eso lo convertiria
     en apuesta).
  3. GitHub Actions solo puede programarse de forma confiable cada ~5 min,
     pero DENTRO de cada corrida el script se queda revisando ambos precios
     cada ~12 segundos durante ~4.5 minutos antes de salir (asi se aprovecha
     el margen de segundos sin depender de un servidor externo).

Nunca imprime ni transmite claves/secretos.
"""
import csv
import json
import os
import time
from datetime import datetime, timezone

from kalshi_client import KalshiClient
from telegram_notify import send_message

STATE_PATH = "state.json"
LOG_PATH = "logs/signals.csv"
CSV_FIELDS = [
    "timestamp_utc", "market_ticker", "series", "evento", "lado",
    "precio_entrada_c", "precio_actual_c", "roi_pct",
    "activo_en_entrada", "activo_ahora",
]

# Series de Kalshi a monitorear (15 min, sube/baja) -> indice CF Benchmarks
SERIES_INDEX = {
    "KXBTC15M": "BRTI",
    "KXETH15M": "ETHUSD_RTI",
    "KXSOL15M": "SOLUSD_RTI",
    "KXXRP15M": "XRPUSD_RTI",
}

# Umbral minimo de movimiento del activo (fraccion, no %) para marcar una
# "entrada" — distinto por activo porque cada uno se mueve a su propio ritmo.
MOMENTUM_THRESHOLD = {
    "KXBTC15M": 0.0004,  # 0.04%
    "KXETH15M": 0.0006,  # 0.06%
    "KXSOL15M": 0.0010,  # 0.10%
    "KXXRP15M": 0.0012,  # 0.12%
}

ROI_TARGET = 0.10          # 10% de ganancia sobre el precio de entrada
HISTORY_WINDOW = 6         # lecturas del activo que se guardan para medir el movimiento (~60-70s)
POLL_SECONDS = 12          # pausa entre lecturas dentro de una misma corrida
RUN_SECONDS = 25            # VALOR DE PRUEBA TEMPORAL, se restaura a 20700 luego de confirmar el autoencadenado


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {"underlying_history": {}, "current_ticker": {}, "positions": {}}


def save_state(state):
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, STATE_PATH)


def ensure_log_header():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    if not os.path.exists(LOG_PATH):
        with open(LOG_PATH, "w", newline="") as f:
            csv.writer(f).writerow(CSV_FIELDS)


def log_row(row: dict):
    ensure_log_header()
    with open(LOG_PATH, "a", newline="") as f:
        csv.writer(f).writerow([row.get(k, "") for k in CSV_FIELDS])


def get_active_market(client: KalshiClient, series_ticker: str):
    data = client.get_markets(series_ticker=series_ticker, status="open")
    markets = data.get("markets", [])
    if not markets:
        return None
    markets.sort(key=lambda m: m.get("close_time", ""))
    return markets[0]


def _to_cents(dollars_str):
    """Convierte un precio en formato '0.1300' (dolares) a centavos enteros (13)."""
    if dollars_str is None:
        return None
    try:
        value = float(dollars_str)
    except (TypeError, ValueError):
        return None
    return int(round(value * 100))


def contract_prices(market: dict):
    """Devuelve (yes_bid, yes_ask, no_bid, no_ask) en centavos. Kalshi manda
    estos precios como strings en dolares (ej. 'yes_bid_dollars': '0.1300'),
    no como enteros en centavos; con respaldo si falta algun campo
    (YES + NO = 100 centavos siempre)."""
    yes_bid = _to_cents(market.get("yes_bid_dollars"))
    yes_ask = _to_cents(market.get("yes_ask_dollars"))
    no_bid = _to_cents(market.get("no_bid_dollars"))
    no_ask = _to_cents(market.get("no_ask_dollars"))
    if no_bid is None and yes_ask is not None:
        no_bid = 100 - yes_ask
    if no_ask is None and yes_bid is not None:
        no_ask = 100 - yes_bid
    if yes_bid is None and no_ask is not None:
        yes_bid = 100 - no_ask
    if yes_ask is None and no_bid is not None:
        yes_ask = 100 - no_bid
    return yes_bid, yes_ask, no_bid, no_ask


def send_signal(bot_token, chat_id, *, series, ticker, lado, entrada, actual, roi):
    activo = series.replace("KX", "").replace("15M", "")
    direccion = "UP" if lado == "yes" else "DOWN"
    texto = (
        f"<b>SEÑAL KALSHI — {activo}</b>\n"
        f"Mercado: {ticker}\n"
        f"Lado: {direccion}\n"
        f"Entrada: {entrada}¢  →  Ahora: {actual}¢\n"
        f"Ganancia sobre lo invertido: <b>{roi*100:.1f}%</b>\n"
        f"Vender/cerrar ahora."
    )
    try:
        send_message(bot_token, chat_id, texto)
    except Exception as e:
        print(f"[WARN] No se pudo enviar Telegram: {e}")


def send_entry_signal(bot_token, chat_id, *, series, ticker, lado, entry_price, underlying_price):
    activo = series.replace("KX", "").replace("15M", "")
    direccion = "UP" if lado == "yes" else "DOWN"
    objetivo = int(round(entry_price * 1.10))
    texto = (
        f"<b>ENTRADA KALSHI — {activo}</b>\n"
        f"Mercado: {ticker}\n"
        f"Lado: {direccion}\n"
        f"Precio de entrada ahora mismo: <b>{entry_price}¢</b>\n"
        f"Precio del activo: {underlying_price}\n"
        f"Te aviso de nuevo cuando llegue a ~{objetivo}¢ (10% de ganancia)."
    )
    try:
        send_message(bot_token, chat_id, texto)
    except Exception as e:
        print(f"[WARN] No se pudo enviar Telegram (entrada): {e}")


def archive_unsignaled(state, series, old_ticker):
    """Si cambia el ticker del ciclo (cerro el mercado de 15 min) y habia una
    posicion abierta que nunca llego al 10%, se registra para el historial
    (no es una señal, es solo dato para el futuro dashboard)."""
    pos = state["positions"].pop(old_ticker, None)
    if pos and not pos.get("signaled"):
        log_row({
            "timestamp_utc": now_iso(),
            "market_ticker": old_ticker,
            "series": series,
            "evento": "ciclo_cerrado_sin_señal",
            "lado": pos.get("side", ""),
            "precio_entrada_c": pos.get("entry_price", ""),
            "precio_actual_c": "",
            "roi_pct": "",
            "activo_en_entrada": pos.get("underlying_entry", ""),
            "activo_ahora": "",
        })


def poll_once(client, state, bot_token, chat_id):
    for series, index_id in SERIES_INDEX.items():
        try:
            cf = client.cfbenchmarks_value(index_id)
            payload = cf.get("data", {}).get("payload", [])
            if not payload:
                raise ValueError("payload vacio en la respuesta de CF Benchmarks")
            underlying_price = float(payload[-1]["value"])
        except Exception as e:
            print(f"[WARN] {series}: no se pudo leer precio del activo ({e})")
            continue

        hist = state["underlying_history"].setdefault(series, [])
        hist.append(underlying_price)
        if len(hist) > HISTORY_WINDOW:
            del hist[0: len(hist) - HISTORY_WINDOW]

        try:
            market = get_active_market(client, series)
        except Exception as e:
            print(f"[WARN] {series}: no se pudo leer el mercado ({e})")
            continue
        if not market:
            continue

        ticker = market["ticker"]
        prev_ticker = state["current_ticker"].get(series)
        if prev_ticker and prev_ticker != ticker:
            archive_unsignaled(state, series, prev_ticker)
            state["underlying_history"][series] = [underlying_price]
            hist = state["underlying_history"][series]
        state["current_ticker"][series] = ticker

        yes_bid, yes_ask, no_bid, no_ask = contract_prices(market)
        position = state["positions"].get(ticker)

        if position is None:
            if len(hist) >= 2:
                base = hist[0]
                if base:
                    pct_move = (hist[-1] - base) / base
                    threshold = MOMENTUM_THRESHOLD.get(series, 0.0005)
                    if abs(pct_move) >= threshold:
                        if pct_move > 0 and yes_ask is not None:
                            state["positions"][ticker] = {
                                "side": "yes",
                                "entry_price": yes_ask,
                                "entry_time": now_iso(),
                                "underlying_entry": underlying_price,
                                "series": series,
                                "signaled": False,
                            }
                            send_entry_signal(
                                bot_token, chat_id, series=series, ticker=ticker,
                                lado="yes", entry_price=yes_ask, underlying_price=underlying_price,
                            )
                            log_row({
                                "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                                "evento": "entrada_detectada", "lado": "yes",
                                "precio_entrada_c": yes_ask, "precio_actual_c": "", "roi_pct": "",
                                "activo_en_entrada": underlying_price, "activo_ahora": "",
                            })
                        elif pct_move < 0 and no_ask is not None:
                            state["positions"][ticker] = {
                                "side": "no",
                                "entry_price": no_ask,
                                "entry_time": now_iso(),
                                "underlying_entry": underlying_price,
                                "series": series,
                                "signaled": False,
                            }
                            send_entry_signal(
                                bot_token, chat_id, series=series, ticker=ticker,
                                lado="no", entry_price=no_ask, underlying_price=underlying_price,
                            )
                            log_row({
                                "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                                "evento": "entrada_detectada", "lado": "no",
                                "precio_entrada_c": no_ask, "precio_actual_c": "", "roi_pct": "",
                                "activo_en_entrada": underlying_price, "activo_ahora": "",
                            })
        elif not position.get("signaled"):
            side = position["side"]
            entry_price = position["entry_price"]
            current_sell = yes_bid if side == "yes" else no_bid
            if entry_price is not None and current_sell is not None and entry_price > 0:
                roi = (current_sell - entry_price) / entry_price
                if roi >= ROI_TARGET:
                    send_signal(
                        bot_token, chat_id,
                        series=series, ticker=ticker, lado=side,
                        entrada=entry_price, actual=current_sell, roi=roi,
                    )
                    log_row({
                        "timestamp_utc": now_iso(),
                        "market_ticker": ticker,
                        "series": series,
                        "evento": "señal_venta",
                        "lado": side,
                        "precio_entrada_c": entry_price,
                        "precio_actual_c": current_sell,
                        "roi_pct": round(roi * 100, 2),
                        "activo_en_entrada": position["underlying_entry"],
                        "activo_ahora": underlying_price,
                    })
                    position["signaled"] = True


def main():
    key_id = os.environ["KALSHI_KEY_ID"]
    key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]

    client = KalshiClient(key_id, key_path)
    state = load_state()
    ensure_log_header()

    start = time.monotonic()
    iteration = 0
    while time.monotonic() - start < RUN_SECONDS:
        iteration += 1
        try:
            poll_once(client, state, bot_token, chat_id)
        except Exception as e:
            print(f"[WARN] Error en iteracion {iteration}: {e}")
        save_state(state)
        elapsed = time.monotonic() - start
        remaining = RUN_SECONDS - elapsed
        if remaining <= 0:
            break
        time.sleep(min(POLL_SECONDS, remaining))

    print(f"Corrida terminada: {iteration} lecturas.")


if __name__ == "__main__":
    main()
