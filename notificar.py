"""
Envío de mensajes a Telegram y/o WhatsApp.

Telegram (recomendado):
  TELEGRAM_TOKEN=   token del bot (te lo da @BotFather)
  TELEGRAM_CHAT_ID= tu ID de chat (ejecuta: python notificar.py --chat-id)

WhatsApp (opcional, mediante el servicio gratuito CallMeBot):
  WHATSAPP_PHONE=   tu número con prefijo, p. ej. +34600111222
  WHATSAPP_APIKEY=  la clave que te envía CallMeBot (ver README)

Prueba:  python notificar.py "Hola, esto es una prueba"
"""
import os
import sys
import json
import urllib.parse
import urllib.request

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "").strip()
WHATSAPP_APIKEY = os.getenv("WHATSAPP_APIKEY", "").strip()

TELEGRAM_API = os.getenv("TELEGRAM_API", "https://api.telegram.org")
CALLMEBOT_API = os.getenv("CALLMEBOT_API", "https://api.callmebot.com/whatsapp.php")


def _split(text: str, size: int) -> list[str]:
    """Parte un texto largo en trozos, cortando por saltos de línea."""
    parts = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text.strip():
        parts.append(text)
    return parts


def send_telegram(text: str) -> None:
    url = f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/sendMessage"
    for part in _split(text, 4000):
        body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": part,
                           "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
            if not res.get("ok"):
                raise RuntimeError(res.get("description", "error desconocido"))


def send_whatsapp(text: str) -> None:
    for part in _split(text, 1500):
        q = urllib.parse.urlencode({"phone": WHATSAPP_PHONE, "text": part, "apikey": WHATSAPP_APIKEY})
        with urllib.request.urlopen(f"{CALLMEBOT_API}?{q}", timeout=60) as r:
            body = r.read().decode(errors="replace")
            if r.status != 200 or "error" in body.lower()[:200]:
                raise RuntimeError(body[:200])


def channels() -> list[str]:
    out = []
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        out.append("Telegram")
    if WHATSAPP_PHONE and WHATSAPP_APIKEY:
        out.append("WhatsApp")
    return out


def send(text: str) -> list[str]:
    """Envía por todos los canales configurados. Devuelve mensajes de resultado."""
    results = []
    if not channels():
        return ["⚠️  No hay Telegram ni WhatsApp configurado en .env: no se ha enviado nada."]
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        try:
            send_telegram(text)
            results.append("✅ Mensaje enviado por Telegram.")
        except Exception as e:
            results.append(f"❌ Telegram ha fallado: {e}")
    if WHATSAPP_PHONE and WHATSAPP_APIKEY:
        try:
            send_whatsapp(text)
            results.append("✅ Mensaje enviado por WhatsApp.")
        except Exception as e:
            results.append(f"❌ WhatsApp ha fallado: {e}")
    return results


def find_chat_id() -> None:
    """Muestra el chat_id de quien haya escrito últimamente al bot."""
    if not TELEGRAM_TOKEN:
        print("Pon primero TELEGRAM_TOKEN en .env.")
        return
    with urllib.request.urlopen(f"{TELEGRAM_API}/bot{TELEGRAM_TOKEN}/getUpdates", timeout=30) as r:
        res = json.load(r)
    chats = {}
    for u in res.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or ""
    if not chats:
        print("No hay mensajes. Abre tu bot en Telegram, pulsa Iniciar, escríbele algo "
              "y vuelve a ejecutar este comando.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


if __name__ == "__main__":
    if "--chat-id" in sys.argv:
        find_chat_id()
    else:
        for line in send(" ".join(sys.argv[1:]) or "✅ Prueba de mensaje desde el script de tutorías."):
            print(line)
