import os, re, sqlite3, io, logging, hashlib, time, base64, csv
from urllib.parse import urlparse, parse_qs, urlencode
import cv2
import numpy as np
import zxingcpp
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
DB = os.environ.get("SQLITE_PATH", "/tmp/chek_bot_seen.sqlite3")

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
    """Decode small, tilted thermal-receipt QR codes using several bounded passes."""
    if len(photo) > 12_000_000:
        return None, "invalid_image"
    img = cv2.imdecode(np.frombuffer(photo, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, "invalid_image"
    cv2.setNumThreads(1)
    h, w = img.shape[:2]
    if h * w > 8_000_000:
        scale = (8_000_000 / (h * w)) ** .5
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]
    detector = cv2.QRCodeDetector()
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    regions = [gray]
    # A full image can contain large amounts of unrelated text; isolate QR candidates.
    scale = min(1.0, 1100 / max(h, w))
    small = cv2.resize(gray, None, fx=scale, fy=scale) if scale < 1 else gray
    mask = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY_INV, 31, 8)
    contours, _ = cv2.findContours(cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)),
        cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for contour in contours:
        x, y, cw, ch = cv2.boundingRect(contour)
        if 35 < min(cw, ch) and .58 < cw / ch < 1.7 and cw * ch < 250000:
            boxes.append((cw * ch, x, y, cw, ch))
    for _, x, y, cw, ch in sorted(boxes, reverse=True)[:15]:
        cx, cy = (x + cw / 2) / scale, (y + ch / 2) / scale
        radius = max(cw, ch) / scale * .95
        x0, x1 = max(0, int(cx-radius)), min(w, int(cx+radius))
        y0, y1 = max(0, int(cy-radius)), min(h, int(cy+radius))
        if min(x1-x0, y1-y0) >= 65:
            regions.append(gray[y0:y1, x0:x1])
    # Different receipt positions and photographic perspective.
    for x0, y0, x1, y1 in ((0,.45,.55,1),(.3,.45,.8,1),(.45,.4,1,1),
                            (0,.2,.65,.9),(.25,.25,.85,.95)):
        regions.append(gray[int(h*y0):int(h*y1),int(w*x0):int(w*x1)])
    deadline = time.monotonic() + 22
    non_ofd = False
    for region in regions:
        if time.monotonic() > deadline:
            break
        rh, rw = region.shape
        if min(rh, rw) < 55:
            continue
        if max(rh, rw) > 1300:
            s = 1300 / max(rh, rw)
            region = cv2.resize(region, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        frames = [region, clahe.apply(region)]
        if max(region.shape) < 850:
            frames += [cv2.resize(frame, None, fx=2, fy=2,
                                  interpolation=cv2.INTER_CUBIC) for frame in frames[:]]
        for frame in frames:
            if time.monotonic() > deadline:
                break
            variants = [frame, cv2.GaussianBlur(frame, (3, 3), 0)]
            if max(frame.shape) <= 1500:
                variants.append(cv2.adaptiveThreshold(frame, 255,
                    cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 35, 7))
                variants.append(cv2.threshold(frame, 0, 255,
                    cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1])
            for variant in variants:
                if time.monotonic() > deadline:
                    break
                try:
                    results = zxingcpp.read_barcodes(variant,
                        formats=zxingcpp.BarcodeFormat.QRCode,
                        try_rotate=True, try_downscale=False, try_invert=True)
                    for result in results:
                        link = normalize_qr(result.text)
                        if link:
                            return link, "read"
                        non_ofd = True
                    if not results and max(variant.shape) < 1400:
                        value, _, _ = detector.detectAndDecode(variant)
                        if value:
                            link = normalize_qr(value)
                            if link:
                                return link, "read"
                            non_ofd = True
                except Exception:
                    logging.exception("QR decoder error")
    logging.info("QR scan finished: regions=%s, candidates=%s, non_ofd=%s",
                 len(regions), len(boxes), non_ofd)
    return None, "non_ofd" if non_ofd else "unreadable"

def init_results(db):
    db.execute("""CREATE TABLE IF NOT EXISTS qr_results
        (message_id INTEGER PRIMARY KEY, status TEXT NOT NULL, url TEXT,
         file_id TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(qr_results)")}
    if "file_id" not in columns:
        db.execute("ALTER TABLE qr_results ADD COLUMN file_id TEXT")
    if "updated_at" not in columns:
        db.execute("ALTER TABLE qr_results ADD COLUMN updated_at TEXT")

STORE_URL = os.environ.get("RECEIPT_STORE_URL", "")
STORE_SECRET = os.environ.get("RECEIPT_STORE_SECRET", "")

def store(action, **data):
    if not STORE_URL or not STORE_SECRET:
        raise RuntimeError("Persistent receipt database is not configured")
    response = requests.post(STORE_URL, headers={"x-bot-secret": STORE_SECRET},
                             json={"action": action, **data}, timeout=15)
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        raise RuntimeError("Persistent receipt database returned an error")
    return result.get("data", [])

def record_result(msg, status, link="", file_id=None):
    store("record", message_id=msg["message_id"], status=status,
          url=link or None, file_id=file_id)
    if file_id:
        try:
            photo = fetch_telegram_image(file_id)
            if len(photo) <= 8_000_000:
                store("image", message_id=msg["message_id"],
                      base64=base64.b64encode(photo).decode("ascii"))
            else:
                logging.warning("Receipt photo exceeds archive upload limit")
        except Exception:
            logging.exception("Receipt image archive upload failed: message_id=%s", msg["message_id"])

def get_unread():
    return [(row["message_id"], row.get("file_id")) for row in store("unread")]

def fetch_telegram_image(file_id):
    meta = requests.get(API + "/getFile", params={"file_id": file_id}, timeout=12)
    meta.raise_for_status()
    path = meta.json().get("result", {}).get("file_path")
    if not path:
        raise ValueError("Telegram did not return a file path")
    response = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=18)
    response.raise_for_status()
    if len(response.content) > 12_000_000:
        raise ValueError("Image too large")
    return response.content

def retry_unread(chat_id):
    rows = get_unread()
    if not rows:
        send(chat_id, "✅ Қайта текшириладиган ўқилмаган чек йўқ.")
        return
    updated = 0
    for message_id, file_id in rows[:5]:
        if not file_id:
            continue
        try:
            link, status = decode_photo(fetch_telegram_image(file_id))
            if link:
                if seen(link):
                    status = "duplicate"
                else:
                    # Retry is audit-only: never post an OFD link to the group.
                    status = "manual_review"
            record_result({"message_id": message_id}, status, link or "", file_id)
            updated += 1
        except Exception:
            logging.exception("Retry failed: message_id=%s", message_id)
    send(chat_id, f"🔄 Қайта текширилди: {updated}/{min(len(rows),5)}. Қолганлари учун /retry ни яна юборинг.\n/status — натижалар.")

def report():
    rows = store("report")
    counts = {}
    failures = []
    for row in rows:
        status = row["status"]
        counts[status] = counts.get(status, 0) + 1
        if status in ("unreadable", "manual_review"):
            failures.append(row["message_id"])
    lines = [
        "📊 QR текширув ҳисоботи (Supabase доимий база)",
        f"✅ Ўқилди: {counts.get('read', 0)}",
        f"📝 Қўлда текшириш керак: {counts.get('manual_review', 0)}",
        f"❌ Ўқилмади (эски): {counts.get('unreadable', 0)}",
        f"🔁 Такрорий: {counts.get('duplicate', 0)}",
        f"⚠️ Юбориш хатоси: {counts.get('send_failed', 0)}",
        f"🔎 OFD бўлмаган QR: {counts.get('non_ofd', 0)}",
    ]
    if failures:
        lines.append("Ўқилмаган хабар ID: " + ", ".join(str(x) for x in failures[-30:]))
    return chr(10).join(lines)

def seen(link):
    return bool(store("seen", url=link))

def remember(link):
    pass  # Successful receipts are persisted by record_result.

@app.get("/")
def health():
    return {"status": "ok", "bot_configured": bool(TOKEN), "group_configured": bool(TARGET), "persistent_database_configured": bool(STORE_URL and STORE_SECRET)}

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
    if text.strip().split("@")[0] == "/export":
        rows = store("report")
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["message_id", "status", "ofd_url", "image_path", "updated_at"])
        for row in rows:
            writer.writerow([row.get("message_id"), row.get("status"),
                             row.get("url"), row.get("image_path"), row.get("updated_at")])
        response = requests.post(API + "/sendDocument",
            data={"chat_id": chat_id, "caption": "📊 Чеклар реестри (Google Sheets ва Excel учун CSV)"},
            files={"document": ("cheklar_reyestri.csv", output.getvalue().encode("utf-8-sig"), "text/csv")},
            timeout=30)
        response.raise_for_status()
        return {"ok": True}
    if text.strip().split("@")[0] == "/status":
        send(chat_id, report())
        return {"ok": True}
    if text.strip().split("@")[0] == "/review":
        rows = [row for row in store("report") if row.get("status") == "manual_review"]
        if not rows:
            send(chat_id, "✅ Қўлда текшириладиган чеклар йўқ.")
        else:
            send(chat_id, "📝 Қўлда текшириладиган чек ID лари:\\n" +
                 "\\n".join(str(row["message_id"]) for row in rows[-30:]) +
                 "\\nOFD тасдиқланмагунча бухгалтерияда тасдиқланган деб ҳисобламанг.")
        return {"ok": True}
    if text.strip().split("@")[0] == "/unread":
        rows = get_unread()
        if not rows:
            send(chat_id, "✅ Ўқилмаган чеклар йўқ.")
        else:
            send(chat_id, "❌ Ўқилмаган чек хабар ID лари:\n" +
                 "\n".join(f"• {mid}" + ("" if fid else " (қайта юбориш керак)") for mid, fid in rows) +
                 "\n\nҚайта текшириш: /retry")
        return {"ok": True}
    if text.strip().split("@")[0] == "/retry":
        retry_unread(chat_id)
        return {"ok": True}
    link = normalize_qr(text) if text else None
    if not link and (msg.get("photo") or (msg.get("document") or {}).get("mime_type", "").startswith("image/")):
        file_id = msg["photo"][-1]["file_id"] if msg.get("photo") else msg["document"]["file_id"]
        try:
            link, qr_status = decode_photo(fetch_telegram_image(file_id))
        except Exception:
            logging.exception("Image download or scan failed")
            qr_status = "unreadable"
    if not link:
        if msg.get("photo") or msg.get("document"):
            status = locals().get("qr_status", "unreadable")
            record_result(msg, status, file_id=locals().get("file_id"))
            logging.info("Receipt QR result: message_id=%s status=%s", msg.get("message_id"), status)
            if status == "non_ofd":
                send(chat_id, "⚠️ QR-код ўқилди, лекин OFD фискал чек ҳаволаси эмас. Фискал чек QR-кодини юборинг.")
            elif status == "invalid_image":
                send(chat_id, "❌ Расм файлини очиб бўлмади. JPG ёки PNG форматда юборинг.")
            else:
                send(chat_id, "📝 QR-код ўқилмади. Чек базага сақланди ва қўлда текшириш рўйхатига қўшилди. /review")
        else:
            send(chat_id, "OFD чек ҳаволасини ёки QR-кодли чек расмини юборинг.")
        return {"ok": True}
    if seen(link):
        record_result(msg, "duplicate", link, locals().get("file_id"))
        send(chat_id, "Бу чек аввал қайд этилган. OFD ҳаволаси гуруҳга қайта юборилмади.")
        return {"ok": True}
    try:
        send(TARGET, "🧾 Янги фискал чек\n" + link)
        remember(link)
        record_result(msg, "read", link, locals().get("file_id"))
        send(chat_id, "✅ OFD ҳаволаси гуруҳга юборилди.")
    except Exception:
        logging.exception("Send failed")
        record_result(msg, "send_failed", link, locals().get("file_id"))
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
