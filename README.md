# Kalshi Signal Bot — Nelson

Motor de señales para los mercados de 15 min de BTC/ETH/SOL/XRP en Kalshi.
Compara el precio real (via CF Benchmarks, el mismo índice que usa Kalshi para liquidar)
contra el strike del mercado activo, y manda alertas a Telegram cuando hay
suficiente divergencia + momentum. Corre solo, cada 5 min, en GitHub Actions
(no depende de tu Mac ni de esta conversación).

## Archivos
- `kalshi_client.py` — cliente firmado del API de Kalshi (lectura).
- `telegram_notify.py` — envío de mensajes a tu Telegram.
- `signal_engine.py` — lógica de señales (umbrales en `THRESHOLD_PCT`, ajustables).
- `test_connection.py` — prueba de conexión manual.
- `.github/workflows/signals.yml` — el cron que lo corre cada 5 min en la nube.
- `config/secrets.json` — **NO se sube a GitHub** (está en .gitignore). Solo para pruebas locales.

## Paso 1 — Probar la conexión (en tu Mac, Terminal normal, no el puente de Claude)
```
cd ~/Documents/kalshi-signals
python3 -m pip install --user -r requirements.txt
python3 test_connection.py
```
Si ves tu balance y un precio de BTC, funciona.

## Paso 2 — Crear el bot de Telegram
1. En Telegram, busca @BotFather → `/newbot` → sigue los pasos → te da un **token**.
2. Escríbele un mensaje cualquiera a tu bot nuevo.
3. Corre: `python3 telegram_notify.py <TU_TOKEN>` — en la respuesta busca `"chat":{"id": ...}` → ese número es tu `chat_id`.

## Paso 3 — Subir el proyecto a GitHub
```
cd ~/Documents/kalshi-signals
git init
git add .
git commit -m "kalshi signal bot v1"
git branch -M main
git remote add origin https://github.com/<TU_USUARIO>/kalshi-signals.git
git push -u origin main
```

## Paso 4 — Configurar los secretos en GitHub (tú, nunca Claude)
En el repo: Settings → Secrets and variables → Actions → "New repository secret". Crea estos 4:
- `KALSHI_KEY_ID` → `015ac6bd-fb44-4097-8c75-0f6b5639953f`
- `KALSHI_PRIVATE_KEY` → pega el contenido completo del archivo `.pem` (ábrelo con TextEdit, copia todo incluyendo las líneas `-----BEGIN/END-----`)
- `TELEGRAM_BOT_TOKEN` → el token del paso 2
- `TELEGRAM_CHAT_ID` → el chat_id del paso 2

## Paso 5 — Activarlo
Ve a la pestaña "Actions" del repo → habilita los workflows si te lo pide → el cron ya queda corriendo cada 5 min solo. También puedes correrlo manualmente ahí con "Run workflow".

## Calibración / backtesting
Cada señal queda en `logs/signals.csv` con el resultado (ACIERTO/FALLO) una vez el
mercado liquida. Los umbrales de divergencia (`THRESHOLD_PCT` en `signal_engine.py`)
son un punto de partida — hay que ajustarlos con los datos reales que se vayan
acumulando ahí.

## Nota sobre Temperatura diaria
Los mercados de temperatura (24 ciudades) no tienen un índice de precio en vivo como
las criptos — se liquidan contra la temperatura real medida, no contra un feed
continuo. No están incluidos en la lógica de señales v1; se puede diseñar algo
distinto para ellos más adelante si quieres.
