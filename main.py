import os
import time
import imaplib
import email
from email.header import decode_header
from gtts import gTTS
from flask import Flask, Response
from feedgen.feed import FeedGenerator

# ===== Configuration =====
EMAIL = os.environ.get("GMAIL_ADDRESS")
PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
MAIL_SERVER = "imap.gmail.com"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 300))  # seconds

# Storage paths
AUDIO_DIR = "audio"
FEED_FILE = "feed.xml"
os.makedirs(AUDIO_DIR, exist_ok=True)

# ===== RSS Feed Setup =====
fg = FeedGenerator()
fg.load_extension('podcast')
fg.title("Email to Pod")
fg.link(href="https://your-render-app-name.onrender.com/feed")
fg.description("A private podcast of your emails as audio.")

app = Flask(__name__)

# ===== Helper functions =====
def fetch_emails():
    mail = imaplib.IMAP4_SSL(MAIL_SERVER)
    mail.login(EMAIL, PASSWORD)
    mail.select("inbox")

    status, messages = mail.search(None, 'UNSEEN')
    mail_ids = messages[0].split()
    new_items = []

    for num in mail_ids:
        status, data = mail.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(data[0][1])

        subject, encoding = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = subject.decode(encoding or "utf-8", errors="ignore")
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

        if body.strip():
            filename = f"{AUDIO_DIR}/{int(time.time())}.mp3"
            tts = gTTS(text=f"Subject: {subject}. {body}", lang="en")
            tts.save(filename)

            fe = fg.add_entry()
            fe.id(filename)
            fe.title(subject)
            fe.enclosure(f"https://your-render-app-name.onrender.com/{filename}", 0, "audio/mpeg")
            fe.pubDate(time.strftime("%a, %d %b %Y %H:%M:%S %z"))
            new_items.append(subject)

    mail.logout()
    if new_items:
        fg.rss_file(FEED_FILE)
    return new_items

@app.route("/feed")
def feed():
    with open(FEED_FILE, "rb") as f:
        return Response(f.read(), mimetype="application/rss+xml")

@app.route(f"/{AUDIO_DIR}/<path:filename>")
def serve_audio(filename):
    with open(f"{AUDIO_DIR}/{filename}", "rb") as f:
        return Response(f.read(), mimetype="audio/mpeg")

def background_loop():
    while True:
        print("Checking for new emails...")
        new = fetch_emails()
        if new:
            print(f"Processed: {new}")
        time.sleep(CHECK_INTERVAL)

# ===== Start background loop =====
import threading
threading.Thread(target=background_loop, daemon=True).start()

# ===== Flask main =====
@app.route("/")
def index():
    return "Email-to-Pod is running. Subscribe to /feed"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
