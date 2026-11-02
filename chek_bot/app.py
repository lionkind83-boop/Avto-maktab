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
    """Bounded QR scan suitable for Render free-tier worker."""
    img = cv2.imdecode(np.frombuffer(photo, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, "invalid_image"
    h, w = img.shape[:2]
    scale = min(1.0, 1100 / max(h, w))
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    detector = cv2.QRCodeDetector()
    # Only a few bounded passes: avoid OpenCV multi-QR scan and 2x enlargement.
    regions = [img,
               img[h//2:, :],
               img[:, w//2:],
               img[h//2:, w//2:]]
    non_ofd = False
    for region in regions:
        if min(region.shape[:2]) < 90:
            continue
        try:
            value, _, _ = detector.detectAndDecode(region)
        except cv2.error:
            continue
        if value:
            link = normalize_qr(value)
            if link:
                return link, "read"
            non_ofd = True
    return None, "non_ofd" if non_ofd else "unreadable"

def record_result(msg, status, link=""):
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS qr_results (message_id INTEGER PRIMARY KEY, status TEXT NOT NULL, url TEXT)")
        db.execute("INSERT OR REPLACE INTO qr_results (message_id,status,url) VALUES (?,?,?)", (msg["message_id"], status, link))

def report():
    with sqlite3.connect(DB) as db:
        db.execute("CREATE TABLE IF NOT EXISTS qr_results (message_id INTEGER PRIMARY KEY, status TEXT NOT NULL, url TEXT)")
        rows = db.execute("SELECT status, COUNT(*) FROM qr_results GROUP BY status").fetchall()
        failures = db.execute("SELECT message_id FROM qr_results WHERE status='unreadable' ORDER BY message_id DESC LIMIT 30").fetchall()
    counts = dict(rows)
    lines = [
        "📊 QR текширув ҳисоботи (сервер хотираси)",
        f"✅ Ўқилди: {counts.get('read', 0)}",
        f"❌ Ўқилмади: {counts.get('unreadable', 0)}",
        f"🔁 Такрорий: {counts.get('duplicate', 0)}",
        f"⚠️ Юбориш хатоси: {counts.get('send_failed', 0)}",
        f"🔎 OFD бўлмаган QR: {counts.get('non_ofd', 0)}",
    ]
    if failures:
        lines.append("Ўқилмаган хабар ID: " + ", ".join(str(x[0]) for x in failures))
    return chr(10).join(lines)

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
    if re.match(r"^/\s*id(?:@\w+)?(?:\s|$)", text, re.IGNORECASE):
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
    if text.strip().split("@")[0] == "/status":
        send(chat_id, report())
        return {"ok": True}
    link = normalize_qr(text) if text else None
    if not link and (msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith("image/")):
        file_id = msg["photo"][-1]["file_id"] if msg.get("photo") else msg["document"]["file_id"]
        meta = requests.get(API + "/getFile", params={"file_id": file_id}, timeout=20).json()
        path = meta.get("result", {}).get("file_path")
        if path:
            data = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=30).content
            link, qr_status = decode_photo(data)
    if not link:
        if msg.get("photo") or msg.get("document"):
            status = locals().get("qr_status", "unreadable")
            record_result(msg, status)
            logging.info("Receipt QR result: message_id=%s status=%s", msg.get("message_id"), status)
            if status == "non_ofd":
                send(chat_id, "⚠️ QR-код ўқилди, лекин OFD фискал чек ҳаволаси эмас. Фискал чек QR-кодини юборинг.")
            elif status == "invalid_image":
                send(chat_id, "❌ Расм файлини очиб бўлмади. JPG ёки PNG форматда юборинг.")
            else:
                send(chat_id, "❌ Расмдаги QR-кодни техник жиҳатдан ўқиб бўлмади. Чекни аниқроқ суратга олинг.")
        else:
            send(chat_id, "OFD чек ҳаволасини ёки QR-кодли чек расмини юборинг.")
        return {"ok": True}
    if seen(link):
        record_result(msg, "duplicate", link)
        send(chat_id, "Бу чек аввал юборилган (сервер хотираси бўйича).")
        return {"ok": True}
    try:
        send(TARGET, "🧾 Янги фискал чек\n" + link)
        remember(link)
        record_result(msg, "read", link)
        send(chat_id, "✅ OFD ҳаволаси гуруҳга юборилди.")
    except Exception:
        logging.exception("Send failed")
        record_result(msg, "send_failed", link)
        send(chat_id, "❌ Гуруҳга юборилмади. Бот ҳуқуқлари ва TARGET_CHAT_ID ни текширинг.")
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))

# Register webhook at worker boot, without revealing bot token in logs.
if TOKEN and PUBLIC_URL:
    try:
        probe = requests.get(API + "/getMe", timeout=12)
        logging.info("Telegram getMe: HTTP %s; reason: %s", probe.status_code, probe.json().get("description", "ok") if probe.headers.get("content-type", "").startswith("application/json") else "non-JSON response")
        result = requests.post(API + "/setWebhook", json={"url": PUBLIC_URL + "/webhook", "secret_token": SECRET, "allowed_updates": ["message"]}, timeout=12)
        if not result.ok or not result.json().get("ok"):
            logging.error("Telegram webhook registration failed (HTTP %s; reason: %s)", result.status_code, (result.json().get("description", "unknown") if result.headers.get("content-type","").startswith("application/json") else "non-JSON response"))
        else:
            logging.info("Telegram webhook registered")
    except Exception:
        logging.exception("Telegram webhook registration failed")
