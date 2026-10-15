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
import urllib.error
import urllib.request
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
    "KXXRP15M": "XRPUSD_RTI",
}

# Series para las que se permiten ENTRADAS NUEVAS -- 2026-10-14 el usuario
# pidio dejar solo BTC generando señales de entrada porque ETH y XRP estaban
# rindiendo mal con la nueva estrategia (30-45c + TP/SL fijo) mientras BTC
# rendia bien. ETH y XRP se dejan en SERIES_INDEX arriba (no se quitan) para
# que cualquier posicion ya abierta en esas series siga recibiendo señales de
# salida (TP / reversion / corte) hasta que cierre -- solo se bloquean
# entradas NUEVAS en esas dos.
ENTRADAS_ACTIVAS = {"KXBTC15M"}


# Umbral minimo de movimiento del activo (fraccion, no %) para marcar una
# "entrada" — distinto por activo porque cada uno se mueve a su propio ritmo.
MOMENTUM_THRESHOLD = {
    "KXBTC15M": 0.0012,  # subido de 0.04% a 0.12% el 2026-10-15 a pedido del usuario -- con
                          # 0.04% estaba disparando ~50+ señales/dia en BTC solo; el objetivo
                          # pedido es 8-12 señales/dia de mas calidad. Es un primer ajuste a
                          # ojo (no hay datos de tick a tick guardados para simular el umbral
                          # exacto) -- se recalibra con el conteo real de señales de los
                          # proximos dias.
    "KXETH15M": 0.0006,  # 0.06%
    "KXXRP15M": 0.0012,  # 0.12% (mismo valor que se uso la primera vez que XRP estuvo activo;
                          # XRP se mueve mas brusco que BTC/ETH, por eso el umbral mas alto --
                          # a revisar con datos reales una vez que entren señales de XRP).
}

# Par de Kraken (exchange real, API publica, sin API key) para traer velas
# OHLC de 1 minuto y calcular indicadores tecnicos (EMA/RSI/ADX) -- CF
# Benchmarks solo da el valor puntual del indice, sin historial ni volumen,
# asi que para EMA/RSI/ADX hace falta una serie de velas de un exchange real.
# (Los indicadores estan desactivados como filtro de entrada por ahora -- ver
# sesgo_tecnico() -- pero se deja el par listo por si se reactivan.)
KRAKEN_PAIR = {
    "KXBTC15M": "XBTUSD",
    "KXETH15M": "ETHUSD",
    "KXXRP15M": "XRPUSD",
}

INDICATOR_CACHE_SECONDS = 30  # no hace falta pedir velas nuevas cada 8s: una
                               # vela de 1 min no cambia tanto en ese rato, y
                               # asi se cuida la API publica de Kraken

ROI_TARGET = 0.10          # BAJADO de vuelta de 20% a 10% (2026-10-13, sexto ajuste, pocas horas
                           # despues del quinto): el experimento de subir a 20% fallo fuerte en la
                           # practica real -- 24 operaciones, win rate se desplomo de ~85-87% a
                           # 25%, ROI neto promedio -5.1%. La razon: con el objetivo en 93c (el
                           # tope) para casi todas las entradas de la banda 70-80 de ask, la meta
                           # quedaba muy lejos del precio de entrada, y mientras mas lejos esta la
                           # meta mas tiempo tarda en llegar -- mas tiempo le da al precio para
                           # devolverse y pegar el corte de perdida antes de alcanzarla (5 cortes
                           # seguidos el 2026-10-13 entre 11:31 y 12:46, varios en 20-30s). El
                           # riesgo de subir la ganancia no se compensa si tumba el win rate mas
                           # rapido de lo que sube el premio -- la matematica: con corte real
                           # ~-28%, hace falta win rate > 58% para que valga la pena con meta de
                           # 20% (28/(28+20)), pero el real fue 25%. Con meta de 10% el breakeven
                           # es ~74% (28/(28+10)) y el win rate historico real (~85-87%) lo supera
                           # comodo. CONCLUSION: para esta banda de precios (70-80 ask) y esta
                           # velocidad de reversion, 10% es la meta que de verdad funciona -- si se
                           # quiere mas agresivo despues, probar con pasos chicos (ej. 12%) y mirar
                           # los resultados reales antes de subir mas, no saltar directo a 20%.

OBJETIVO_MAX_CENTS = 93    # tope duro para el precio de SALIDA (objetivo), sin importar que tan
                           # alto sea el precio de entrada. Por datos reales (662 pares del
                           # historial), un objetivo de 94c+ es cuando se pega cerca del techo y
                           # tarda mucho mas en llegar (o no llega y se fuerza el cierre sin
                           # ganancia) -- en 90-93c el cierre forzado es ~0%, el problema real
                           # empieza en 94c. Vuelto a 93 (el techo real segun los datos, no 90)
                           # ahora que el filtro de ganancia minima (ver MIN_ROOM_PCT) se encarga
                           # de descartar los casos al borde con poco margen, en vez de bajar el
                           # techo para todos.

MIN_ROOM_PCT = ROI_TARGET  # SUBIDO de 0.08 a ROI_TARGET (2026-10-13, septimo ajuste): antes
                           # el minimo (8%) era mas bajo que la meta anunciada (10%), asi que
                           # algunas entradas pasaban el filtro pero el "objetivo" mostrado ya
                           # venia recortado por OBJETIVO_MAX_CENTS a MENOS del 10% prometido (el
                           # usuario lo noto: "precios de entrada en 81 con salida en 90 no dan
                           # muchas veces ni el 10"). Exigiendo el mismo % que ROI_TARGET, ninguna
                           # entrada que se manda promete menos de la meta completa -- las que no
                           # alcanzan se descartan en vez de mostrar un objetivo recortado.

LIMIT_SLIPPAGE_BUFFER_CENTS = 10  # un limite 2c POR DEBAJO del ask (como se
                           # probo primero) casi nunca se llena en un movimiento
                           # de momentum real: el precio normalmente SIGUE
                           # corriendo en la misma direccion, no retrocede hacia
                           # el limite -- por eso el usuario reportaba el precio
                           # "disparandose" 10c o mas en vez de bajar. La idea
                           # correcta es al reves: un limite un poco POR ENCIMA
                           # del ask, como tope de proteccion (se llena casi al
                           # toque aunque el precio ya haya corrido durante el
                           # tiempo de reaccion humana, pero nunca se paga mas de
                           # eso). BAJADO de 15 a 10 (2026-10-13, tercer ajuste):
                           # con 15 de colchon y el objetivo limitado a
                           # OBJETIVO_MAX_CENTS, muchas entradas se quedaban sin
                           # espacio real para ganar -- a veces el objetivo
                           # terminaba por DEBAJO del tope de la orden limite (ej.
                           # ask 79 -> limite 94 -> "objetivo" 87, vender mas barato
                           # de lo que pudiste haber pagado). Con 10 de colchon y el
                           # chequeo de espacio minimo (ver mas abajo) eso ya no
                           # puede pasar.
MAX_ENTRY_PRICE = 45       # CAMBIADO de 70 a 45 (2026-10-14, estrategia nueva pedida por
                           # el usuario via documento "indicaciones_bot_kalshi 2.md"). Bloqueo
                           # estricto: Ask >= 46 nunca genera señal (ya lo cubre este tope, no
                           # hace falta un chequeo aparte).
                           # ADVERTENCIA (dejada aqui a proposito): esto vuelve a la zona de ask
                           # 20-60c que HISTORICAMENTE (653 pares reales de este mismo bot) se
                           # resuelve en 32-41 segundos en promedio -- el motivo original por el
                           # que MIN_ENTRY_PRICE se habia subido de 20 a 70, y por el que el
                           # 2026-10-13 solo se bajo hasta 65, no mas abajo. El usuario vio esta
                           # advertencia explicitamente y pidio igual usar el rango del documento
                           # tal cual, asi que se implementa como esta pedido -- pero si las
                           # entradas vuelven a sentirse "demasiado rapidas para alcanzar a mano",
                           # esta es la causa mas probable.

# No entrar si el precio ya esta tan BAJO que el mercado practicamente ya
# descarto ese lado (ej. 3c = el mercado le da ~3% de probabilidad). A esos
# precios cualquier moneda de 1 centavo de diferencia es un porcentaje
# enorme, asi que el TP/SL se disparan por pura ruidosidad del precio, no
# por un movimiento real — y es jugar contra un consenso de mercado muy
# fuerte. Visto en datos reales: entrada en ETH a 3c, corte de perdida casi
# inmediato a 2c.
MIN_ENTRY_PRICE = 30   # CAMBIADO de 65 a 30 (2026-10-14, misma estrategia nueva). Banda pedida
                       # por el usuario: Ask entre $0.30 y $0.45. Ver la advertencia completa en
                       # MAX_ENTRY_PRICE.

TP_MARGEN_CENTS = 25   # Ganancia fija en CENTAVOS sobre el precio limite de entrada (no un
                       # porcentaje). Pedido explicito del usuario via documento: con entrada
                       # limite de 40c, el objetivo es 65c (40+25); con 55c, el objetivo es 80c
                       # (55+25). Reemplaza el sistema anterior (ROI_TARGET/calcular_objetivo,
                       # que segu'ia siendo un % con la comision de Kalshi restada) para esta
                       # banda de precios nueva. ROI_TARGET/calcular_objetivo se dejan en el
                       # archivo solo como respaldo por si queda alguna posicion vieja abierta
                       # con el esquema anterior.

SL_MARGEN_CENTS = 20   # Perdida fija en CENTAVOS tolerada bajo el precio limite de entrada
                       # (reemplaza STOP_LOSS_PCT, que era un %, para esta banda nueva). Con
                       # entrada limite de 40c, el corte es a 20c (40-20); con 55c, el corte es
                       # a 35c (55-20). Relacion riesgo:beneficio ~1:1.25 (arriesga 20, gana 25),
                       # pedida por el documento del usuario.

VENTANA_ENTRADA_MAX_MINUTOS = 7  # Solo se abren entradas nuevas durante los primeros 7 minutos
                       # de la vela de 15 min (minuto 0:00 a 6:59). Pedido del usuario via
                       # documento: la segunda mitad de la vela (minuto 7:00 en adelante) se
                       # bloquea por completo para entradas nuevas, sin importar que tan buena
                       # se vea la condicion de precio -- la volatilidad y el manejo del spread
                       # de los creadores de mercado en los minutos finales de la vela baja la
                       # tasa de acierto. Esto NO afecta las salidas (cobrar/corte/cierre
                       # forzado siguen funcionando siempre en una posicion ya abierta).

REGLA_ORO_MARGEN_CENTS = 2  # Si el precio ya paso de entrada+2c ANTES de que el usuario alcance
                       # a poner la orden limite, el mensaje le avisa que cancele en vez de
                       # perseguir el precio (pedido del documento, "Regla de Oro").

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
STOP_LOSS_PCT = 0.20       # BAJADO de 30% a 20% (2026-10-12). DEJO DE USARSE para
                           # entradas nuevas desde el 2026-10-14 (ver SL_MARGEN_CENTS, corte fijo
                           # en centavos pedido por el usuario) -- se deja aqui solo como
                           # respaldo por si queda alguna posicion vieja abierta con el esquema
                           # anterior.
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
POLL_SECONDS = 5           # pausa entre lecturas dentro de una misma corrida (bajado de 12 a 8,
                           # y de 8 a 5 el 2026-10-15 a pedido del usuario para recortar la
                           # latencia entre el movimiento real del precio y el aviso. El bot ya
                           # no hace ninguna llamada de red extra antes de mandar el Telegram de
                           # entrada/salida (eso se desactivo antes, ver sesgo_tecnico() mas
                           # abajo) -- el unico retraso real que queda es este, el tiempo entre
                           # una lectura de precio y la siguiente.

REVERSION_CONFIRM_TICKS = 3  # Pedido del usuario (2026-10-15): la condicion de reversion
                              # (el activo moviendose en contra) tiene que mantenerse activa
                              # 3 lecturas seguidas antes de avisar "SALIR" -- antes se avisaba
                              # con un solo tick, lo que a veces agarraba un simple parpadeo de
                              # un segundo, no un retroceso real.

REVERSION_ZONA_MUERTA_MIN_PCT = 6   # Pedido del usuario (2026-10-15): si la perdida flotante
REVERSION_ZONA_MUERTA_MAX_PCT = 20  # de la posicion esta entre -6% y -20%, NO avisar reversion --
                                     # se deja que el precio busque el TP normal o el corte fijo.
                                     # Evita avisar "SALIR" justo en el fondo de un latigazo del
                                     # mercado que se puede recuperar. Por fuera de ese rango
                                     # (perdida chica, menos de 6%, o perdida ya grande, mas de
                                     # 20%) la reversion si puede avisar normal.

REVERSO_CONFIRM_READINGS = 2  # Pedido del usuario (2026-10-14): en vez de esperar a que el
                           # PRECIO DEL CONTRATO caiga los SL_MARGEN_CENTS completos para avisar
                           # "cortar perdida", vigilar el ACTIVO (igual que para detectar la
                           # entrada) y avisar "SALIR" apenas se detecte que el activo se esta
                           # moviendo EN CONTRA de la posicion -- antes de que el precio del
                           # contrato alcance a caer tanto. Ventana mas corta que
                           # ENTRY_CONFIRM_READINGS (2 lecturas, ~16s, contra 4 lecturas, ~32s) a
                           # proposito: para abrir una entrada hace falta confirmar bien porque
                           # arriesga plata nueva, pero para salir de una posicion YA abierta
                           # conviene reaccionar mas rapido que confirmar de mas -- el costo de
                           # una falsa alarma (salir un poco antes de tiempo) es mucho menor que
                           # el de quedarse esperando el corte de perdida fijo. El corte fijo en
                           # centavos (SL_MARGEN_CENTS) se deja activo como respaldo, por si esta
                           # señal de reversion no alcanza a dispararse a tiempo.

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


_klines_cache = {}  # series -> (timestamp_local, closes, highs, lows)


def get_klines(pair, interval=1, retries=2):
    """Trae velas OHLC publicas de Kraken (sin API key) para un par tipo
    XBTUSD. interval en minutos. Devuelve (closes, highs, lows) como listas,
    de la mas vieja a la mas nueva."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    ultimo_error = None
    for intento in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "kalshi-signal-bot"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if data.get("error"):
                raise ValueError(str(data["error"]))
            result = data["result"]
            clave = next(k for k in result if k != "last")
            velas = result[clave]
            closes = [float(v[4]) for v in velas]
            highs = [float(v[2]) for v in velas]
            lows = [float(v[3]) for v in velas]
            return closes, highs, lows
        except Exception as e:
            ultimo_error = e
            time.sleep(1)
    raise ultimo_error


def _ema(valores, periodo):
    if len(valores) < periodo:
        return None
    k = 2 / (periodo + 1)
    e = sum(valores[:periodo]) / periodo
    for v in valores[periodo:]:
        e = v * k + e * (1 - k)
    return e


def _rsi(valores, periodo=14):
    if len(valores) < periodo + 1:
        return None
    deltas = [valores[i] - valores[i - 1] for i in range(1, len(valores))]
    ganancias = [d if d > 0 else 0 for d in deltas]
    perdidas = [-d if d < 0 else 0 for d in deltas]
    avg_g = sum(ganancias[:periodo]) / periodo
    avg_p = sum(perdidas[:periodo]) / periodo
    for i in range(periodo, len(ganancias)):
        avg_g = (avg_g * (periodo - 1) + ganancias[i]) / periodo
        avg_p = (avg_p * (periodo - 1) + perdidas[i]) / periodo
    if avg_p == 0:
        return 100.0
    rs = avg_g / avg_p
    return 100 - (100 / (1 + rs))


def _adx(highs, lows, closes, periodo=14):
    if len(closes) < periodo * 2:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(closes)):
        subida = highs[i] - highs[i - 1]
        bajada = lows[i - 1] - lows[i]
        plus_dm.append(subida if (subida > bajada and subida > 0) else 0)
        minus_dm.append(bajada if (bajada > subida and bajada > 0) else 0)
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)

    def suavizar(serie, periodo):
        s = [sum(serie[:periodo])]
        for v in serie[periodo:]:
            s.append(s[-1] - (s[-1] / periodo) + v)
        return s

    tr_s = suavizar(trs, periodo)
    plus_s = suavizar(plus_dm, periodo)
    minus_s = suavizar(minus_dm, periodo)
    plus_di = [100 * p / t if t else 0 for p, t in zip(plus_s, tr_s)]
    minus_di = [100 * m / t if t else 0 for m, t in zip(minus_s, tr_s)]
    dx = [100 * abs(p - m) / (p + m) if (p + m) else 0 for p, m in zip(plus_di, minus_di)]
    if len(dx) < periodo:
        return None
    adx_val = sum(dx[:periodo]) / periodo
    for d in dx[periodo:]:
        adx_val = (adx_val * (periodo - 1) + d) / periodo
    return adx_val


def sesgo_tecnico(series):
    """Lee velas reales de 1 min del exchange (Kraken) y calcula EMA 10/55/200,
    RSI 14 y ADX 14 -- la misma lectura que usan traders tipo TradingLatino
    para confirmar si de verdad hay tendencia o el mercado esta sin rumbo.
    Devuelve (sesgo, detalle) donde sesgo es 'sube' / 'baja' / 'neutral', y
    detalle es un dict con los valores (o {} si no se pudo calcular).
    Con cache de INDICATOR_CACHE_SECONDS para no golpear la API en cada
    lectura de 8s."""
    ahora = time.time()
    cache = _klines_cache.get(series)
    if cache and (ahora - cache[0]) < INDICATOR_CACHE_SECONDS:
        closes, highs, lows = cache[1], cache[2], cache[3]
    else:
        pair = KRAKEN_PAIR.get(series)
        if not pair:
            return "neutral", {}
        try:
            closes, highs, lows = get_klines(pair, interval=1)
        except Exception as e:
            print(f"[WARN] {series}: no se pudieron leer velas de Kraken para indicadores ({e})")
            if cache:
                closes, highs, lows = cache[1], cache[2], cache[3]
            else:
                return "neutral", {}
        else:
            _klines_cache[series] = (ahora, closes, highs, lows)

    if len(closes) < 60:
        return "neutral", {}

    precio = closes[-1]
    ema10 = _ema(closes, 10)
    ema55 = _ema(closes, 55)
    ema200 = _ema(closes, 200) if len(closes) >= 200 else None
    r = _rsi(closes, 14)
    a = _adx(highs, lows, closes, 14)
    detalle = {"ema10": ema10, "ema55": ema55, "ema200": ema200, "rsi": r, "adx": a}

    puntos = 0
    if ema10 is not None and ema55 is not None:
        puntos += 1 if ema10 > ema55 else (-1 if ema10 < ema55 else 0)
    if ema200 is not None:
        puntos += 1 if precio > ema200 else (-1 if precio < ema200 else 0)
    if r is not None:
        puntos += 1 if r > 55 else (-1 if r < 45 else 0)

    # ADX mide fuerza de tendencia, no direccion: si esta bajo, el mercado
    # esta lateral y no conviene confiar en la lectura de los otros aunque
    # den algo -- es la misma logica "sin ventaja clara" del panel del
    # usuario cuando el ADX viene bajo.
    fuerza_ok = a is not None and a >= 18
    if not fuerza_ok:
        return "neutral", detalle
    if puntos >= 2:
        return "sube", detalle
    elif puntos <= -2:
        return "baja", detalle
    return "neutral", detalle


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


def costo_comision_cents(price_cents):
    # Formula publicada por Kalshi para la comision de ejecucion: 0.07 *
    # precio * (1 - precio) en dolares, con el precio en dolares (0 a 1).
    # Convertida a centavos (que es como se maneja todo en este archivo):
    # 0.0007 * precio_centavos * (100 - precio_centavos). Se paga una vez
    # al COMPRAR y otra vez al VENDER (son dos operaciones distintas), y es
    # mas alta cuanto mas cerca del centro (50c) este el precio.
    return 0.0007 * price_cents * (100 - price_cents)


def calcular_objetivo(entry_price):
    # UNICA fuente de verdad para el precio de SALIDA (objetivo) de una
    # entrada -- la usan tanto el mensaje de Telegram (send_entry_signal)
    # como el chequeo real de "ya se puede cobrar" en poll_once(). Antes
    # estaban separados: el mensaje mostraba este numero capado a
    # OBJETIVO_MAX_CENTS, pero el chequeo de salida comparaba contra
    # entry_price*(1+ROI_TARGET) SIN el tope -- dos numeros distintos. El
    # usuario lo noto (entrada 81 -> "salida 90" que no llegaba ni al 10%
    # real en otros casos del mismo rango). Ahora hay un solo numero: se
    # calcula una vez al abrir la posicion, se muestra en el mensaje, y es
    # EXACTAMENTE el mismo que dispara el aviso de "cobrar".
    #
    # 2026-10-13 (noveno ajuste): el usuario detecto que el 10% "de papel"
    # (solo diferencia de precios) no es el 10% real que termina en el
    # bolsillo -- Kalshi cobra comision al comprar Y al vender, y esa
    # comision se come una parte grande de la ganancia ("10% es imposible
    # por que mas lo que quita Kalshi pierde uno mucho"). Ahora el objetivo
    # se calcula para que la ganancia NETA (ya restando ambas comisiones)
    # sea la que alcance ROI_TARGET, no la diferencia bruta de precios.
    comision_entrada = costo_comision_cents(entry_price)
    objetivo = entry_price + 1
    while objetivo < 99:
        comision_salida = costo_comision_cents(objetivo)
        ganancia_pct_probando = (objetivo - entry_price - comision_entrada - comision_salida) / entry_price
        if ganancia_pct_probando >= ROI_TARGET:
            break
        objetivo += 1
    return min(OBJETIVO_MAX_CENTS, objetivo)


def ganancia_neta_pct(entry_price, objetivo):
    # Ganancia real, en porcentaje sobre lo invertido, DESPUES de restar la
    # comision de Kalshi en la entrada y en la salida. Esto es lo que
    # importa de verdad (lo que queda en el bolsillo), no la diferencia
    # bruta de precios.
    comision_entrada = costo_comision_cents(entry_price)
    comision_salida = costo_comision_cents(objetivo)
    return (objetivo - entry_price - comision_entrada - comision_salida) / entry_price


def send_entry_signal(bot_token, chat_id, *, series, ticker, lado, entry_price, ask_actual, underlying_price, remaining_s=None, sesgo=None, sesgo_detalle=None):
    activo = series.replace("KX", "").replace("15M", "")
    if lado == "yes":
        direccion = "SUBE"
        cabecera = "🟢⬆️🟢⬆️🟢⬆️"
    else:
        direccion = "BAJA"
        cabecera = "🔴⬇️🔴⬇️🔴⬇️"
    # entry_price aqui es el precio LIMITE (ask + colchon de seguridad) -- lo
    # mismo que se guarda en la posicion. TP/SL fijos en centavos pedidos por
    # el usuario el 2026-10-14 (documento "indicaciones_bot_kalshi 2.md"):
    # reemplazan el calculo por % que se usaba antes para esta banda de
    # precios. ganancia_neta_pct() se deja solo como linea informativa (ya
    # con la comision de Kalshi restada), no decide nada.
    objetivo = min(99, entry_price + TP_MARGEN_CENTS)
    corte = max(1, entry_price - SL_MARGEN_CENTS)
    ganancia_real_pct = ganancia_neta_pct(entry_price, objetivo) * 100
    regla_oro_limite = entry_price + REGLA_ORO_MARGEN_CENTS
    vela_texto = ""
    if remaining_s is not None:
        transcurrido_s = max(0, 900 - remaining_s)
        vela_texto = f"Vela actual: {int(transcurrido_s // 60)}:{int(transcurrido_s % 60):02d} / 15:00\n"
    linea_tecnica = ""
    if sesgo_detalle:
        r = sesgo_detalle.get("rsi")
        a = sesgo_detalle.get("adx")
        e10 = sesgo_detalle.get("ema10")
        e55 = sesgo_detalle.get("ema55")
        if r is not None and a is not None and e10 is not None and e55 is not None:
            # OJO: nunca meter "<" o ">" sueltos en un mensaje de Telegram con
            # parse_mode=HTML -- Telegram intenta leerlo como una etiqueta HTML
            # y si no la reconoce (como "<EMA55") rechaza el mensaje ENTERO con
            # un error 400 que el bot solo loggea (no se nota en Telegram, el
            # mensaje de ENTRADA simplemente nunca llega). Por eso texto plano,
            # nada de simbolos < >.
            stack = "EMA10 sobre EMA55" if e10 > e55 else "EMA10 bajo EMA55"
            linea_tecnica = f"Sesgo técnico: {sesgo} (RSI {r:.0f}, ADX {a:.0f}, {stack})\n"
    texto = (
        f"{cabecera}\n"
        f"<b>ENTRADA KALSHI — {activo}</b>\n"
        f"{cabecera}\n"
        f"Mercado: 15 min\n"
        f"Lado: {direccion}\n"
        f"Precio actual (ask): {ask_actual}¢\n"
        f"👉 Pon ORDEN LÍMITE a: <b>{entry_price}¢</b> (tope maximo -- normalmente se llena mas cerca del ask, nunca pagas mas de esto)\n"
        f"💰 TOMAR GANANCIA (TP): <b>{objetivo}¢</b> (+{TP_MARGEN_CENTS}¢)\n"
        f"🛑 CORTAR PÉRDIDA (SL): <b>{corte}¢</b> (-{SL_MARGEN_CENTS}¢)\n"
        f"Ganancia real estimada (ya con comision de Kalshi): <b>{ganancia_real_pct:.1f}%</b>\n"
        f"⚠️ Regla de oro: si el precio ya pasó de {regla_oro_limite}¢ antes de poner tu orden, CANCELA -- no persigas el precio.\n"
        f"{vela_texto}"
        f"{linea_tecnica}"
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


def send_reversal_exit_signal(bot_token, chat_id, *, series, ticker, lado, entrada, actual, roi):
    # Pedido del usuario (2026-10-14): avisar SALIR en cuanto se detecta que
    # el ACTIVO se esta moviendo en contra de la posicion, sin esperar a que
    # el precio del contrato llegue al corte de perdida fijo (SL_MARGEN_CENTS).
    # Puede dispararse con ganancia, perdida chica, o perdida grande -- el
    # texto se adapta segun el signo de roi.
    activo = series.replace("KX", "").replace("15M", "")
    direccion = "SUBE" if lado == "yes" else "BAJA"
    etiqueta = "Ganancia" if roi >= 0 else "Pérdida"
    texto = (
        f"<b>POSIBLE RETROCESO — {activo}</b>\n"
        f"Mercado: 15 min\n"
        f"Lado: {direccion}\n"
        f"Precio de ENTRADA: <b>{entrada}¢</b>\n"
        f"Precio de SALIDA (ahora): <b>{actual}¢</b>\n"
        f"{etiqueta}: <b>{roi*100:.1f}%</b>\n"
        f"🟡🟡🟡 <b>EL ACTIVO EMPEZÓ A MOVERSE EN CONTRA</b> 🟡🟡🟡\n"
        f"🚨⚠️🚨 <b>SALIR SALIR SALIR</b> 🚨⚠️🚨\n"
        f"(se detecto antes de llegar al corte de perdida fijo -- mejor salir ahora)\n"
        f"Enviado: {hora_atlanta()} (hora Atlanta)"
    )
    try:
        send_message(bot_token, chat_id, texto)
    except Exception as e:
        print(f"[WARN] No se pudo enviar Telegram (reversion): {e}")


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
            # no alcanzaria a llegar a la ganancia y se saldria sin señal de salida
            # (exactamente la "apuesta" que se quiere evitar).
            too_late = remaining_s is not None and remaining_s < MIN_TIME_TO_CLOSE_SECONDS
            # Ventana de tiempo pedida por el usuario (documento 2026-10-14): solo
            # entrar en los primeros VENTANA_ENTRADA_MAX_MINUTOS de la vela de 15
            # min. La vela dura 900s, asi que minutos transcurridos = (900 -
            # remaining_s) / 60. Si remaining_s no se pudo leer, no se bloquea por
            # esto (igual que too_late).
            fuera_ventana_tiempo = (
                remaining_s is not None
                and (900 - remaining_s) / 60 >= VENTANA_ENTRADA_MAX_MINUTOS
            )
            # Antes se evaluaba la entrada con solo 2 lecturas del activo (tan poco
            # como 8 segundos de historial, un solo "tick"). Eso disparaba la entrada
            # sobre un movimiento instantaneo que Kalshi alcanza a corregir casi al
            # toque -- para cuando el usuario abria la app ya no quedaba nada que
            # agarrar. Ahora se exige la ventana completa de HISTORY_WINDOW lecturas
            # (~45-50s con POLL_SECONDS=8) para confirmar que el movimiento es
            # sostenido, no un parpadeo de un segundo -- una entrada real que lleva
            # medio minuto formandose tiene mas chance real de seguir viva cuando el
            # usuario entra a mano, en vez de ya haber desaparecido.
            if series in ENTRADAS_ACTIVAS and len(hist) >= ENTRY_CONFIRM_READINGS and not too_late and not fuera_ventana_tiempo and not en_pausa_de_entradas():
                base = hist[-ENTRY_CONFIRM_READINGS]
                if base:
                    pct_move = (hist[-1] - base) / base
                    threshold = MOMENTUM_THRESHOLD.get(series, 0.0005)
                    if abs(pct_move) >= threshold:
                        # Los indicadores tecnicos (EMA/RSI/ADX via Kraken) se
                        # desactivaron como filtro de entrada: la llamada de red a
                        # Kraken quedaba DENTRO del mismo ciclo que vigila los
                        # precios, asi que cuando Kraken tardaba o fallaba, todo el
                        # bot se atrasaba (no solo esa entrada -- tambien el
                        # monitoreo de las posiciones abiertas). El usuario reporto
                        # las señales mas atrasadas justo despues de este deploy.
                        # Las funciones sesgo_tecnico()/get_klines() se dejan en el
                        # archivo por si se retoma esto mas adelante hechas de forma
                        # que no bloqueen el ciclo principal (ej. en un hilo aparte).
                        sesgo, sesgo_detalle = None, {}

                        if pct_move > 0 and yes_ask is not None and MIN_ENTRY_PRICE <= yes_ask <= MAX_ENTRY_PRICE:
                            if yes_ask_jump is not None and yes_ask_jump > MAX_ASK_JUMP_CENTS:
                                print(f"[INFO] {series}: entrada SUBE descartada, el ask salto {yes_ask_jump}c entre lecturas (> {MAX_ASK_JUMP_CENTS}c)")
                            else:
                                limite_yes = min(99, yes_ask + LIMIT_SLIPPAGE_BUFFER_CENTS)
                                # TP/SL fijos en centavos (documento del usuario, 2026-10-14):
                                # reemplaza el calculo por % (calcular_objetivo/MIN_ROOM_PCT) para
                                # esta banda de precios. ganancia_neta_pct() se sigue usando solo
                                # para MOSTRAR la ganancia real estimada ya con comision -- no
                                # descarta la entrada, es informativo.
                                objetivo_yes = min(99, limite_yes + TP_MARGEN_CENTS)
                                corte_yes = max(1, limite_yes - SL_MARGEN_CENTS)
                                state["positions"][ticker] = {
                                    "side": "yes",
                                    "entry_price": limite_yes,
                                    "objetivo": objetivo_yes,
                                    "corte": corte_yes,
                                    "entry_time": now_iso(),
                                    "underlying_entry": underlying_price,
                                    "series": series,
                                    "signaled": False,
                                }
                                send_entry_signal(
                                    bot_token, chat_id, series=series, ticker=ticker,
                                    lado="yes", entry_price=limite_yes, ask_actual=yes_ask, underlying_price=underlying_price,
                                    remaining_s=remaining_s, sesgo=sesgo, sesgo_detalle=sesgo_detalle,
                                )
                                log_row({
                                    "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                                    "evento": "entrada_detectada", "lado": "yes",
                                    "precio_entrada_c": limite_yes, "precio_actual_c": "", "roi_pct": "",
                                    "activo_en_entrada": underlying_price, "activo_ahora": "",
                                    "segundos_restantes_cierre": remaining_s,
                                })
                        elif pct_move < 0 and no_ask is not None and MIN_ENTRY_PRICE <= no_ask <= MAX_ENTRY_PRICE:
                            if no_ask_jump is not None and no_ask_jump > MAX_ASK_JUMP_CENTS:
                                print(f"[INFO] {series}: entrada BAJA descartada, el ask salto {no_ask_jump}c entre lecturas (> {MAX_ASK_JUMP_CENTS}c)")
                            else:
                                limite_no = min(99, no_ask + LIMIT_SLIPPAGE_BUFFER_CENTS)
                                objetivo_no = min(99, limite_no + TP_MARGEN_CENTS)
                                corte_no = max(1, limite_no - SL_MARGEN_CENTS)
                                state["positions"][ticker] = {
                                    "side": "no",
                                    "entry_price": limite_no,
                                    "objetivo": objetivo_no,
                                    "corte": corte_no,
                                    "entry_time": now_iso(),
                                    "underlying_entry": underlying_price,
                                    "series": series,
                                    "signaled": False,
                                }
                                send_entry_signal(
                                    bot_token, chat_id, series=series, ticker=ticker,
                                    lado="no", entry_price=limite_no, ask_actual=no_ask, underlying_price=underlying_price,
                                    remaining_s=remaining_s, sesgo=sesgo, sesgo_detalle=sesgo_detalle,
                                )
                                log_row({
                                    "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                                    "evento": "entrada_detectada", "lado": "no",
                                    "precio_entrada_c": limite_no, "precio_actual_c": "", "roi_pct": "",
                                    "activo_en_entrada": underlying_price, "activo_ahora": "",
                                    "segundos_restantes_cierre": remaining_s,
                                })
        elif not position.get("signaled"):
            side = position["side"]
            entry_price = position["entry_price"]
            # objetivo guardado al abrir la posicion -- el MISMO numero que se
            # le mostro al usuario en el mensaje de ENTRADA (ver
            # calcular_objetivo()). Si la posicion es de antes de este arreglo
            # (sin "objetivo" guardado en el estado), se recalcula aqui como
            # respaldo para no romper posiciones ya abiertas.
            objetivo = position.get("objetivo") or calcular_objetivo(entry_price)
            # corte guardado al abrir la posicion -- fijo en centavos
            # (SL_MARGEN_CENTS), pedido por el usuario el 2026-10-14. Respaldo con
            # STOP_LOSS_PCT solo por si queda alguna posicion vieja sin "corte"
            # guardado (abierta antes de este cambio).
            corte = position.get("corte") or max(1, entry_price - SL_MARGEN_CENTS)
            current_sell = yes_bid if side == "yes" else no_bid
            if entry_price is not None and current_sell is not None and entry_price > 0:
                roi = (current_sell - entry_price) / entry_price

                # Cambios pedidos por el usuario (2026-10-15), tras ver que la señal de
                # reversion estaba saliendo justo en el fondo de latigazos del mercado que
                # despues se recuperaban:
                #   1) Zona muerta: con perdida flotante entre -REVERSION_ZONA_MUERTA_MIN_PCT%
                #      y -REVERSION_ZONA_MUERTA_MAX_PCT%, NO avisar reversion -- se deja que el
                #      precio busque el TP o el corte fijo en vez de vender ahi.
                #   2) Confirmacion por 3 lecturas: la condicion de "activo moviendose en
                #      contra" tiene que repetirse REVERSION_CONFIRM_TICKS veces seguidas (no
                #      solo una vez) antes de mandar el aviso -- se cuenta con un contador que
                #      vive en la posicion misma (position["reversion_streak"]).
                roi_pct_actual = roi * 100
                en_zona_muerta_reversion = -REVERSION_ZONA_MUERTA_MAX_PCT <= roi_pct_actual <= -REVERSION_ZONA_MUERTA_MIN_PCT
                momentum_en_contra = (
                    len(hist) >= REVERSO_CONFIRM_READINGS
                    and hist[-REVERSO_CONFIRM_READINGS]
                    and (
                        (side == "yes" and (hist[-1] - hist[-REVERSO_CONFIRM_READINGS]) / hist[-REVERSO_CONFIRM_READINGS] <= -MOMENTUM_THRESHOLD.get(series, 0.0005))
                        or (side == "no" and (hist[-1] - hist[-REVERSO_CONFIRM_READINGS]) / hist[-REVERSO_CONFIRM_READINGS] >= MOMENTUM_THRESHOLD.get(series, 0.0005))
                    )
                )
                if momentum_en_contra and not en_zona_muerta_reversion:
                    position["reversion_streak"] = position.get("reversion_streak", 0) + 1
                else:
                    position["reversion_streak"] = 0

                if current_sell >= objetivo and seconds_since(position.get("entry_time")) >= MIN_HOLD_SECONDS:
                    # Antes esto comparaba roi >= ROI_TARGET directamente, SIN el
                    # tope de OBJETIVO_MAX_CENTS -- asi el bot podia esperar un
                    # precio mas alto del que le habia mostrado al usuario como
                    # "objetivo" (ej. objetivo mostrado 93c, pero el chequeo real
                    # esperaba hasta 99c si el ROI_TARGET crudo lo pedia). El
                    # usuario lo detecto ("no dan ni el 10"). Ahora el disparo
                    # usa el MISMO numero que se mostro, sin excepcion.
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

                elif position["reversion_streak"] >= REVERSION_CONFIRM_TICKS:
                    # Pedido del usuario (2026-10-14): no esperar a que el precio del
                    # CONTRATO caiga hasta "corte" (SL_MARGEN_CENTS) -- vigilar el
                    # ACTIVO (la misma logica que detecta la entrada, pero con una
                    # ventana mas corta, REVERSO_CONFIRM_READINGS) y avisar SALIR en
                    # cuanto el activo se mueva en contra de la posicion, ya confirmado
                    # por REVERSION_CONFIRM_TICKS lecturas seguidas y fuera de la zona
                    # muerta (ver arriba). Puede dispararse con la posicion todavia en
                    # ganancia (no llego al objetivo) o ya en perdida chica -- lo que
                    # importa es que el activo ya esta yendo para el otro lado.
                    send_reversal_exit_signal(
                        bot_token, chat_id,
                        series=series, ticker=ticker, lado=side,
                        entrada=entry_price, actual=current_sell, roi=roi,
                    )
                    log_row({
                        "timestamp_utc": now_iso(), "market_ticker": ticker, "series": series,
                        "evento": "señal_reversion", "lado": side,
                        "precio_entrada_c": entry_price, "precio_actual_c": current_sell,
                        "roi_pct": round(roi * 100, 2),
                        "activo_en_entrada": position["underlying_entry"], "activo_ahora": underlying_price,
                    })
                    position["signaled"] = True

                elif current_sell <= corte:
                    # se cayo demasiado -> cortar la perdida aqui, nunca dejarla llegar a -100%.
                    # Antes esto comparaba roi <= -STOP_LOSS_PCT (un %); ahora compara contra
                    # el corte fijo en centavos guardado al abrir la posicion (ver SL_MARGEN_CENTS),
                    # el MISMO numero que se le mostro al usuario en el mensaje de entrada. Esta
                    # comparacion se deja como RESPALDO -- normalmente la señal de reversion (ver
                    # arriba) deberia dispararse primero, antes de que el precio llegue tan lejos
                    # (o la zona muerta la esta deteniendo a proposito, ver arriba).
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
