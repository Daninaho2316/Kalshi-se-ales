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
import math
import os
import subprocess
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kalshi_client import KalshiClient
from telegram_notify import send_message

STATE_PATH = "state.json"
LOG_PATH = "logs/signals.csv"
CSV_FIELDS = [
    "timestamp_utc", "market_ticker", "series", "evento", "lado",
    "precio_entrada_c", "precio_actual_c", "roi_pct",
    "activo_en_entrada", "activo_ahora", "segundos_restantes_cierre",
]

# Series de Kalshi a monitorear (15 min, sube/baja) -> indice CF Benchmarks
SERIES_INDEX = {
    "KXBTC15M": "BRTI",
    "KXETH15M": "ETHUSD_RTI",
    # KXSOL15M y KXXRP15M desactivados a pedido del usuario: con las 4 series
    # activas llegaban demasiadas senales juntas y era dificil entrar a
    # todas a tiempo; se dejan solo BTC y ETH (mejor resultado promedio por
    # operacion en el rastreador).
}

# Umbral minimo de movimiento del activo (fraccion, no %) para marcar una
# "entrada" — distinto por activo porque cada uno se mueve a su propio ritmo.
MOMENTUM_THRESHOLD = {
    "KXBTC15M": 0.0004,  # 0.04%
    "KXETH15M": 0.0006,  # 0.06%
}

ROI_TARGET = 0.10          # 10% de ganancia sobre el precio de entrada
MAX_ENTRY_PRICE = 90       # no entrar si el precio ya esta tan alto que un 10% es matematicamente
                           # imposible (el contrato nunca pasa de 99-100c); 90c*1.10=99c, el limite exacto

# No entrar si el precio ya esta tan BAJO que el mercado practicamente ya
# descarto ese lado (ej. 3c = el mercado le da ~3% de probabilidad). A esos
# precios cualquier moneda de 1 centavo de diferencia es un porcentaje
# enorme (de 3c a 2c es "solo" 1 centavo pero -33%), asi que el 10% de
# ganancia y el -30% de corte de perdida se disparan por pura ruidosidad del
# precio, no por un movimiento real — y es jugar contra un consenso de
# mercado muy fuerte. Visto en datos reales: entrada en ETH a 3c, corte de
# perdida casi inmediato a 2c.
MIN_ENTRY_PRICE = 20   # subido de 10 a 20: con 13c tambien se vio un salto de -69% entre
                       # una lectura y la siguiente (contrato poco liquido = saltos grandes)

# No abrir una entrada nueva si al ciclo de 15 min le quedan menos de esto.
# Motivo (visto en los datos reales): varias perdidas fueron entradas tardias
# que no tuvieron tiempo de llegar al 10% antes de que cerrara el ciclo — eso
# es exactamente lo que el usuario no quiere (se convertiria en apuesta al
# quedarse esperando el cierre). Mejor no entrar que forzar una entrada sin
# margen para salir a tiempo.
MIN_TIME_TO_CLOSE_SECONDS = 180  # 3 minutos

# Antes, si el ciclo cerraba sin llegar al 10%, la posicion se quedaba abierta
# y se perdia el 100% de lo invertido (un perdida = como 10 ganadas). Para que
# el negocio tenga sentido, ahora SIEMPRE se sale de la posicion antes de que
# cierre el ciclo, por una de tres razones: se llego al 10% (ganancia), se
# cayo demasiado (se corta la perdida aqui en vez de dejarla llegar a -100%),
# o se esta acabando el tiempo (se cierra YA al precio que sea, nunca se deja
# vencer sin vender).
STOP_LOSS_PCT = 0.30       # cortar la perdida si el contrato cae 30% desde la entrada
FORCE_EXIT_SECONDS = 45    # si quedan <45s para el cierre y sigue abierta, cerrarla YA
MIN_HOLD_SECONDS = 30      # no avisar la toma de ganancia antes de este tiempo desde la
                           # entrada: en datos reales muchas entradas llegaban al 10% en
                           # 10-20 segundos, un tiempo imposible para que el usuario abra
                           # Kalshi y compre a mano. El corte de perdida y el cierre forzado
                           # NO se retrasan (la proteccion contra perdidas va primero).
HISTORY_WINDOW = 6         # lecturas del activo que se guardan para medir el movimiento (~60-70s)
ENTRY_CONFIRM_READINGS = 4 # cuantas de esas lecturas (las mas recientes) se usan para confirmar
                           # una entrada (~24-32s con POLL_SECONDS=8). Antes se usaba el buffer
                           # completo (HISTORY_WINDOW=6, ~45-50s): eso evito el problema de "no
                           # da chance ni de entrar" (señal sobre un parpadeo de 1 tick), pero el
                           # usuario reporto el efecto contrario -- para cuando ve la alerta el
                           # precio ya se habia movido ~10 centavos mas (una tendencia confirmada
                           # tiende a seguir un poco despues de confirmarse). Con una ventana mas
                           # corta se avisa un poco antes en el mismo movimiento, con menos
                           # recorrido ya "gastado" para cuando llega la alerta -- a seguir
                           # ajustando segun la diferencia real que vea el usuario.
POLL_SECONDS = 8           # pausa entre lecturas dentro de una misma corrida (bajado de 12 a 8
                           # para reaccionar mas rapido al corte de perdida)

# Pausa de ENTRADAS nuevas durante estos ratos del dia (hora NY/Atlanta, la
# misma zona que usa hora_atlanta()). El usuario reporto que las entradas
# que salen en estos horarios (apertura de mercados ~9-10:30am, almuerzo
# ~1-2pm) suelen ser de peor calidad. Esto NO afecta las SALIDAS -- cobrar,
# corte de perdida y cierre forzado siguen funcionando siempre, una posicion
# ya abierta nunca se queda atascada por esto.
PAUSA_ENTRADAS_NY = [
    (9, 0, 10, 30),   # 9:00am - 10:30am hora NY
    (13, 0, 14, 0),   # 1:00pm - 2:00pm hora NY
]

MAX_ASK_JUMP_CENTS = 8     # si el ask del contrato (yes_ask o no_ask, segun el lado que se
                           # vaya a comprar) salta mas de esto en centavos entre una lectura
                           # y la siguiente (~8s de diferencia con POLL_SECONDS), se descarta
                           # esa entrada: un salto asi de grande en tan poco tiempo suele ser
                           # un problema de datos o liquidez, no un precio real al que se
                           # pueda comprar.

# Cada cuanto se hace un "git push" de respaldo aunque no haya pasado nada
# nuevo (ademas del push inmediato que se hace apenas se registra una
# entrada/salida/cierre). Antes el log solo se subia a GitHub al terminar
# toda la corrida larga (~5h45min), asi que el rastreador se quedaba horas
# desactualizado aun cuando Telegram ya habia avisado la señal. Con esto el
# archivo logs/signals.csv en GitHub queda al dia en minutos, no en horas.
PUSH_INTERVAL_SECONDS = 180  # respaldo cada 3 min aunque no haya señales nuevas
RUN_SECONDS = 20700         # ~5h45min: casi todo el limite de 6h de un job de GitHub Actions. Al terminar, el propio workflow se vuelve a lanzar (ver signals.yml), asi el bot queda vigilando casi sin pausas en vez de depender de que el cron de GitHub despierte a tiempo (confirmado: a veces tarda 20-25 min en vez de 5).


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_ATLANTA_TZ = ZoneInfo("America/New_York")


def hora_atlanta():
    """Hora exacta de envio en hora de Atlanta (EDT/EST, se ajusta sola),
    para que el usuario vea en el propio mensaje de Telegram que tan
    "fresca" es la señal al momento de leerla -- sin esto no habia forma
    de saber si una alerta tenia 2 segundos o 3 minutos de antiguedad."""
    return datetime.now(timezone.utc).astimezone(_ATLANTA_TZ).strftime("%I:%M:%S %p").lstrip("0")


def en_pausa_de_entradas():
    """True si la hora actual en NY/Atlanta cae dentro de alguna de las
    ventanas de PAUSA_ENTRADAS_NY. Solo bloquea ENTRADAS nuevas -- las
    salidas de posiciones ya abiertas no llaman a esta funcion."""
    ahora = datetime.now(timezone.utc).astimezone(_ATLANTA_TZ)
    minutos_ahora = ahora.hour * 60 + ahora.minute
    for h1, m1, h2, m2 in PAUSA_ENTRADAS_NY:
        inicio = h1 * 60 + m1
        fin = h2 * 60 + m2
        if inicio <= minutos_ahora < fin:
            return True
    return False


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    return {"underlying_history": {}, "current_ticker": {}, "positions": {}, "last_ask": {}}


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


_LOG_EVENT_COUNT = 0  # se incrementa cada vez que se escribe una fila nueva en el log


def log_row(row: dict):
    global _LOG_EVENT_COUNT
    ensure_log_header()
    with open(LOG_PATH, "a", newline="") as f:
        csv.writer(f).writerow([row.get(k, "") for k in CSV_FIELDS])
    _LOG_EVENT_COUNT += 1


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


def seconds_since(iso_str):
    """Segundos transcurridos desde un timestamp guardado con now_iso().
    0 si no se puede leer (nunca bloquea por un dato faltante)."""
    if not iso_str:
        return 0
    try:
        then = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds()
    except Exception:
        return 0


def seconds_until_close(market: dict):
    """Segundos que faltan para que cierre el ciclo de 15 min. None si no se
    puede leer/parsear close_time (en ese caso no se bloquea la entrada)."""
    close_time = market.get("close_time")
    if not close_time:
        return None
    try:
        value = close_time.replace("Z", "+00:00")
        close_dt = datetime.fromisoformat(value)
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        return (close_dt - datetime.now(timezone.utc)).total_seconds()
    except Exception:
        return None


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
    direccion = "SUBE" if lado == "yes" else "BAJA"
    texto = (
        f"<b>SALIDA KALSHI — {activo}</b>\n"
        f"Mercado: 15 min\n"
        f"Lado: {direccion}\n"
        f"Precio de ENTRADA: <b>{entrada}¢</b>\n"
        f"Precio de SALIDA (ahora): <b>{actual}¢</b>\n"
        f"Ganancia sobre lo invertido: <b>{roi*100:.1f}%</b>\n"
        f"🚨🚨🚨 <b>COBRAR COBRAR COBRAR</b> 🚨🚨🚨\n"
        f"💰💵🤑💵💰\n"
        f"Enviado: {hora_atlanta()} (hora Atlanta)"
    )
    try:
        send_message(bot_token, chat_id, texto)
    except Exception as e:
        print(f"[WARN] No se pudo enviar Telegram: {e}")


def send_entry_signal(bot_token, chat_id, *, series, ticker, lado, entry_price, underlying_price):
    activo = series.replace("KX", "").replace("15M", "")
    if lado == "yes":
        direccion = "SUBE"
        cabecera = "🟢⬆️🟢⬆️🟢⬆️"
    else:
        direccion = "BAJA"
        cabecera = "🔴⬇️🔴⬇️🔴⬇️"
    objetivo = min(99, max(entry_price + 1, math.ceil(entry_price * 1.10)))
    texto = (
        f"{cabecera}\n"
        f"<b>ENTRADA KALSHI — {activo}</b>\n"
        f"{cabecera}\n"
        f"Mercado: 15 min\n"
        f"Lado: {direccion}\n"
        f"Precio de ENTRADA: <b>{entry_price}¢</b>\n"
        f"Precio de SALIDA (objetivo, 10%): <b>{objetivo}¢</b>\n"
        f"Enviado: {hora_atlanta()} (hora Atlanta)"
    )
    try:
        send_message(bot_token, chat_id, texto)
    except Exception as e:
        print(f"[WARN] No se pudo enviar Telegram (entrada): {e}")


def send_stop_loss_signal(bot_token, chat_id, *, series, ticker, lado, entrada, actual, roi):
    activo = series.replace("KX", "").replace("15M", "")
    direccion = "SUBE" if lado == "yes" else "BAJA"
    texto = (
        f"<b>CORTE DE PÉRDIDA — {activo}</b>\n"
        f"Mercado: 15 min\n"
        f"Lado: {direccion}\n"
        f"Precio de ENTRADA: <b>{entrada}¢</b>\n"
        f"Precio de SALIDA (ahora): <b>{actual}¢</b>\n"
        f"Pérdida: <b>{roi*100:.1f}%</b>\n"
        f"🔴🔴🔴 <b>ADVERTENCIA</b> 🔴🔴🔴\n"
        f"🚨⚠️🚨 <b>SALIR SALIR SALIR</b> 🚨⚠️🚨\n"
        f"Enviado: {hora_atlanta()} (hora Atlanta)"
    )
    try:
        send_message(bot_token, chat_id, texto)
    except Exception as e:
        print(f"[WARN] No se pudo enviar Telegram (corte de perdida): {e}")


def send_forced_exit_signal(bot_token, chat_id, *, series, ticker, lado, entrada, actual, roi):
    activo = series.replace("KX", "").replace("15M", "")
    direccion = "SUBE" if lado == "yes" else "BAJA"
    if roi >= 0:
        cierre = (
            f"🚨🚨🚨 <b>COBRAR COBRAR COBRAR</b> 🚨🚨🚨\n"
            f"💰💵🤑💵💰"
        )
    else:
        cierre = (
            f"🔴🔴🔴 <b>ADVERTENCIA</b> 🔴🔴🔴\n"
            f"🚨⚠️🚨 <b>SALIR SALIR SALIR</b> 🚨⚠️🚨"
        )
    texto = (
        f"<b>CIERRE OBLIGATORIO — {activo}</b>\n"
        f"Mercado: 15 min\n"
        f"Lado: {direccion}\n"
        f"Precio de ENTRADA: <b>{entrada}¢</b>\n"
        f"Precio de SALIDA (ahora): <b>{actual}¢</b>\n"
        f"Resultado: <b>{roi*100:+.1f}%</b>\n"
        f"Se acaba el tiempo del ciclo.\n"
        f"{cierre}\n"
        f"Enviado: {hora_atlanta()} (hora Atlanta)"
    )
    try:
        send_message(bot_token, chat_id, texto)
    except Exception as e:
        print(f"[WARN] No se pudo enviar Telegram (cierre forzado): {e}")


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


def git_sync(reason: str):
    """Guarda (git add/commit/push) el estado y el log de señales AHORA
    mismo, en vez de esperar a que termine toda la corrida larga. Si algo
    falla (red, permisos, lo que sea) solo avisa por consola y sigue — nunca
    debe tumbar el bot."""
    try:
        subprocess.run(["git", "config", "user.name", "kalshi-signal-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", STATE_PATH, LOG_PATH], check=True)
        nothing_to_commit = subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0
        if nothing_to_commit:
            return
        subprocess.run(
            ["git", "commit", "-m", f"actualiza estado y señales ({reason}) [skip ci]"],
            check=True,
        )
        subprocess.run(["git", "push"], check=True)
        print(f"[OK] git push realizado ({reason})")
    except Exception as e:
        print(f"[WARN] No se pudo sincronizar con git ({reason}): {e}")


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
            state.setdefault("last_ask", {}).pop(series, None)
        state["current_ticker"][series] = ticker

        yes_bid, yes_ask, no_bid, no_ask = contract_prices(market)
        position = state["positions"].get(ticker)
        remaining_s = seconds_until_close(market)

        # Salto del ask entre esta lectura y la anterior de la misma serie,
        # para el filtro MAX_ASK_JUMP_CENTS (Fix: entradas sobre datos malos
        # o ilíquidos). Se actualiza SIEMPRE, haya o no posicion abierta, para
        # que la proxima lectura se compare contra esta.
        last_ask_series = state.setdefault("last_ask", {}).setdefault(series, {})
        prev_yes_ask = last_ask_series.get("yes")
        prev_no_ask = last_ask_series.get("no")
        yes_ask_jump = abs(yes_ask - prev_yes_ask) if (yes_ask is not None and prev_yes_ask is not None) else None
        no_ask_jump = abs(no_ask - prev_no_ask) if (no_ask is not None and prev_no_ask is not None) else None
        if yes_ask is not None:
            last_ask_series["yes"] = yes_ask
        if no_ask is not None:
            last_ask_series["no"] = no_ask

        if position is None:
            # Si al ciclo le queda muy poco tiempo, no abrir una entrada nueva:
            # no alcanzaria a llegar al 10% y se saldria sin señal de salida
            # (exactamente la "apuesta" que se quiere evitar).
            too_late = remaining_s is not None and remaining_s < MIN_TIME_TO_CLOSE_SECONDS
            # Antes se evaluaba la entrada con solo 2 lecturas del activo (tan poco
            # como 8 segundos de historial, un solo "tick"). Eso disparaba la entrada
            # sobre un movimiento instantaneo que Kalshi alcanza a corregir casi al
            # toque -- para cuando el usuario abria la app ya no quedaba nada que
            # agarrar. Ahora se exige la ventana completa de HISTORY_WINDOW lecturas
            # (~45-50s con POLL_SECONDS=8) para confirmar que el movimiento es
            # sostenido, no un parpadeo de un segundo -- una entrada real que lleva
            # medio minuto formandose tiene mas chance real de seguir viva cuando el
            # usuario entra a mano, en vez de ya haber desaparecido.
            if len(hist) >= ENTRY_CONFIRM_READINGS and not too_late and not en_pausa_de_entradas():
                base = hist[-ENTRY_CONFIRM_READINGS]
                if base:
                    pct_move = (hist[-1] - base) / base
                    threshold = MOMENTUM_THRESHOLD.get(series, 0.0005)
                    if abs(pct_move) >= threshold:
                        if pct_move > 0 and yes_ask is not None and MIN_ENTRY_PRICE <= yes_ask <= MAX_ENTRY_PRICE:
                            if yes_ask_jump is not None and yes_ask_jump > MAX_ASK_JUMP_CENTS:
                                print(f"[INFO] {series}: entrada SUBE descartada, el ask salto {yes_ask_jump}c entre lecturas (> {MAX_ASK_JUMP_CENTS}c)")
                            else:
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
                                    "segundos_restantes_cierre": remaining_s,
                                })
                        elif pct_move < 0 and no_ask is not None and MIN_ENTRY_PRICE <= no_ask <= MAX_ENTRY_PRICE:
                            if no_ask_jump is not None and no_ask_jump > MAX_ASK_JUMP_CENTS:
                                print(f"[INFO] {series}: entrada BAJA descartada, el ask salto {no_ask_jump}c entre lecturas (> {MAX_ASK_JUMP_CENTS}c)")
                            else:
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
                                    "segundos_restantes_cierre": remaining_s,
                                })
        elif not position.get("signaled"):
            side = position["side"]
            entry_price = position["entry_price"]
            current_sell = yes_bid if side == "yes" else no_bid
            if entry_price is not None and current_sell is not None and entry_price > 0:
                roi = (current_sell - entry_price) / entry_price

                if roi >= ROI_TARGET and seconds_since(position.get("entry_time")) >= MIN_HOLD_SECONDS:
                    # objetivo de ganancia alcanzado Y ya paso el tiempo minimo desde
                    # la entrada (si no, se espera al siguiente poll sin marcar nada,
                    # para darle chance real al usuario de haber entrado en Kalshi)
                    send_signal(
                        bot_token, chat_id,
                        series=series, ticker=ticker, lado=side,
                        entrada=entry_price, actual=current_sell, roi=roi,
                    )
                    log_row({
                        "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                        "evento": "señal_venta", "lado": side,
                        "precio_entrada_c": entry_price, "precio_actual_c": current_sell,
                        "roi_pct": round(roi * 100, 2),
                        "activo_en_entrada": position["underlying_entry"], "activo_ahora": underlying_price,
                    })
                    position["signaled"] = True

                elif roi <= -STOP_LOSS_PCT:
                    # se cayo demasiado -> cortar la perdida aqui, nunca dejarla llegar a -100%
                    send_stop_loss_signal(
                        bot_token, chat_id,
                        series=series, ticker=ticker, lado=side,
                        entrada=entry_price, actual=current_sell, roi=roi,
                    )
                    log_row({
                        "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                        "evento": "señal_corte_perdida", "lado": side,
                        "precio_entrada_c": entry_price, "precio_actual_c": current_sell,
                        "roi_pct": round(roi * 100, 2),
                        "activo_en_entrada": position["underlying_entry"], "activo_ahora": underlying_price,
                    })
                    position["signaled"] = True

                elif remaining_s is not None and remaining_s <= FORCE_EXIT_SECONDS:
                    # se acaba el tiempo del ciclo -> cerrar YA al precio que sea,
                    # nunca dejar la posicion sin vender (eso seria la "apuesta" que no se quiere)
                    send_forced_exit_signal(
                        bot_token, chat_id,
                        series=series, ticker=ticker, lado=side,
                        entrada=entry_price, actual=current_sell, roi=roi,
                    )
                    log_row({
                        "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                        "evento": "cierre_forzado", "lado": side,
                        "precio_entrada_c": entry_price, "precio_actual_c": current_sell,
                        "roi_pct": round(roi * 100, 2),
                        "activo_en_entrada": position["underlying_entry"], "activo_ahora": underlying_price,
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
    last_push = start
    iteration = 0
    while time.monotonic() - start < RUN_SECONDS:
        iteration += 1
        events_before = _LOG_EVENT_COUNT
        try:
            poll_once(client, state, bot_token, chat_id)
        except Exception as e:
            print(f"[WARN] Error en iteracion {iteration}: {e}")
        save_state(state)

        now = time.monotonic()
        if _LOG_EVENT_COUNT != events_before:
            # hubo una entrada/salida/cierre de ciclo -> subirlo a GitHub YA,
            # no esperar a que termine toda la corrida larga.
            git_sync("señal nueva")
            last_push = now
        elif now - last_push >= PUSH_INTERVAL_SECONDS:
            git_sync("respaldo periodico")
            last_push = now

        elapsed = now - start
        remaining = RUN_SECONDS - elapsed
        if remaining <= 0:
            break
        time.sleep(min(POLL_SECONDS, remaining))

    git_sync("fin de la corrida")
    print(f"Corrida terminada: {iteration} lecturas.")


if __name__ == "__main__":
    main()
