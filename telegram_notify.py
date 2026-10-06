"""Envio simple de mensajes via Telegram Bot API (sin librerias externas)."""
import json
import urllib.request
import urllib.parse


def send_message(bot_token: str, chat_id: str, text: str) -> dict:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_updates(bot_token: str) -> dict:
    """Util para descubrir el chat_id: el usuario le escribe al bot y se lee aqui."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2:
        # modo descubrimiento: python3 telegram_notify.py <bot_token>
        result = get_updates(sys.argv[1])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif len(sys.argv) == 4:
        # modo prueba: python3 telegram_notify.py <bot_token> <chat_id> "mensaje"
        result = send_message(sys.argv[1], sys.argv[2], sys.argv[3])
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("Uso: python3 telegram_notify.py <bot_token>  (para ver chat_id)")
        print("  o: python3 telegram_notify.py <bot_token> <chat_id> <mensaje>  (para probar)")
