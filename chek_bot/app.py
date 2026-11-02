import os, re, sqlite3, io, logging, hashlib
from urllib.parse import urlparse, parse_qs, urlencode
import cv2
import numpy as np
import requests
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET = os.environ.get("TARGET_CHAT_ID", "")
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
SECRET = hashlib.sha256(TOKEN.encode()).hexdigest() if TOKEN else ""
OWNER = os.environ.get("OWNER_USER_ID", "")
API = f"https://api.telegram.org/bot{TOKEN}"
DB = "/tmp/chek_bot_seen.sqlite3"

def send(chat, message):
    requests.post(API + "/sendMessage", json={"chat_id": chat, "text": message, "disable_web_page_preview": True}, timeout=20).raise_for_status()

def normalize_qr(raw):
    raw = raw.strip()
    match = re.search(r"https?://[^\s]+", raw)
    if match:
        raw = match.group(0).rstrip(".,)")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname != "ofd.soliq.uz" or parsed.path.rstrip("/") != "/check":
        return None
    query = parse_qs(parsed.query)
    if not all(query.get(k) for k in ("t", "r", "c", "s")):
        return None
    return "https://ofd.soliq.uz/check?" + urlencode({k: query[k][0] for k in ("t", "r", "c", "s")})

def decode_photo(photo):
    array = np.frombuffer(photo, dtype=np.uint8)
    img = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if img is None:
        return None
    detector = cv2.QRCodeDetector()
    value, _, _ = detector.detectAndDecode(img)
    if value:
        return normalize_qr(value)
    ok, values, _, _ = detector.detectAndDecodeMulti(img)
    if ok:
        for value in values:
            link = normalize_qr(value)
            if link:
                return link
    return None

def seen(link):
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS sent (url TEXT PRIMARY KEY)")
        if db.execute("SELECT 1 FROM sent WHERE url=?", (link,)).fetchone():
            return True
        return False

def remember(link):
    with sqlite3.connect(DB) as db:
        db.execute("INSERT OR IGNORE INTO sent (url) VALUES (?)", (link,))

@app.get("/")
def health():
    return {"status": "ok", "bot_configured": bool(TOKEN), "group_configured": bool(TARGET)}

@app.post("/webhook")
def webhook():
    if not TOKEN or request.headers.get("X-Telegram-Bot-Api-Secret-Token") != SECRET:
        abort(403)
    update = request.get_json(silent=True) or {}
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return {"ok": True}
    text = msg.get("text", "")
    if text.startswith("/id"):
        send(chat_id, f"Chat ID: {chat_id}")
        return {"ok": True}
    if chat.get("type") != "private":
        return {"ok": True}
    if text.startswith("/start"):
        send(chat_id, "Чек расмини юборинг. QR-коддан OFD ҳаволасини олиб гуруҳга юбораман.")
        return {"ok": True}
    if not OWNER or str(msg.get("from", {}).get("id")) != OWNER:
        send(chat_id, "Рухсат йўқ. /id орқали ID олинг ва OWNER_USER_ID ни Render серверига киритинг.")
        return {"ok": True}
    if not TARGET:
        send(chat_id, "Гуруҳ ҳали уланмаган. Аввал гуруҳда /id буйруғини юборинг ва TARGET_CHAT_ID ни серверга киритинг.")
        return {"ok": True}
    link = normalize_qr(text) if text else None
    if not link and msg.get("photo"):
        file_id = msg["photo"][-1]["file_id"]
        meta = requests.get(API + "/getFile", params={"file_id": file_id}, timeout=20).json()
        path = meta.get("result", {}).get("file_path")
        if path:
            data = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=30).content
            link = decode_photo(data)
    if not link:
        send(chat_id, "QR-код ўқилмади ёки OFD чеки эмас. Аниқроқ расм ёки OFD ҳаволасини юборинг.")
        return {"ok": True}
    if seen(link):
        send(chat_id, "Бу чек аввал юборилган (сервер хотираси бўйича).")
        return {"ok": True}
    try:
        send(TARGET, "🧾 Янги фискал чек\n" + link)
        remember(link)
        send(chat_id, "✅ OFD ҳаволаси гуруҳга юборилди.")
    except Exception:
        logging.exception("Send failed")
        send(chat_id, "❌ Гуруҳга юборилмади. Бот ҳуқуқлари ва TARGET_CHAT_ID ни текширинг.")
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))

# Register webhook at worker boot, without revealing bot token in logs.
if TOKEN and PUBLIC_URL:
    try:
        result = requests.post(API + "/setWebhook", json={"url": PUBLIC_URL + "/webhook", "secret_token": SECRET, "allowed_updates": ["message"]}, timeout=12)
        if not result.ok or not result.json().get("ok"):
            logging.error("Telegram webhook registration failed (HTTP %s)", result.status_code)
        else:
            logging.info("Telegram webhook registered")
    except Exception:
        logging.exception("Telegram webhook registration failed")
