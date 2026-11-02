# Telegram OFD chek bot

Deploy on Render with build command `pip install -r chek_bot/requirements.txt` and start command `gunicorn --chdir chek_bot app:app --bind 0.0.0.0:$PORT --workers 1`.

Set BOT_TOKEN in Render Environment, then register Telegram webhook to `https://YOUR-RENDER-URL/webhook` using Telegram Bot API setWebhook. Optional WEBHOOK_SECRET must match secret_token at setWebhook. In group send /id, then set TARGET_CHAT_ID. Photos and OFD links sent privately to bot are forwarded to group. Do not share tokens.

Limitations: QR only, no OCR for amounts. Duplicate history uses ephemeral /tmp sqlite and resets on redeploy. A durable database is required for permanent duplicate prevention. Do not use free sleeping service for guaranteed 24/7 operation.
