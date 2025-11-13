import os
import imaplib
import email
from email.header import decode_header
from gtts import gTTS
from flask import Flask, Response
import xml.etree.ElementTree as ET

AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

GMAIL_USER = os.environ.get("GMAIL_ADDRESS")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

app = Flask(__name__)

def fetch_unread_emails():
    print("Connecting to Gmail IMAP...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASS)
    mail.select("inbox")

    status, messages = mail.search(None, '(UNSEEN)')
    email_ids = messages[0].split()
    print(f"Found {len(email_ids)} unread emails.")

    results = []
    for eid in email_ids:
        status, msg_data = mail.fetch(eid, "(RFC822)")
        if status != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])
        subject, encoding = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = subject.decode(encoding or "utf-8", errors="ignore")
        subject = subject or "No Subject"


        
        # Extract plain text body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break
                elif content_type == "text/html" and not body:
                    # fallback to HTML if no plain text yet
                    html_content = part.get_payload(decode=True).decode(errors="ignore")
                    # optional: strip HTML tags simply
                    import re
                    body = re.sub('<[^<]+?>', '', html_content)
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")
        
        # Skip empty bodies
        if not body.strip():
            print(f"Skipping email '{subject}' because body is empty")
            continue

        # Convert to MP3
        filename = f"{eid.decode()}.mp3"
        filepath = os.path.join(AUDIO_DIR, filename)
        if not os.path.exists(filepath):
            print(f"Creating audio for: {subject}")
            tts = gTTS(body[:2000])  # limit length for speed
            tts.save(filepath)
        results.append({"subject": subject, "file": filename})
    mail.logout()
    return results

def generate_rss(emails):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Email to Pod"
    ET.SubElement(channel, "link").text = APP_URL
    ET.SubElement(channel, "description").text = "Your emails as spoken podcasts"

    for email_info in emails:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = email_info["subject"]
        ET.SubElement(item, "enclosure", url=f"{APP_URL}/audio/{email_info['file']}", type="audio/mpeg")
        ET.SubElement(item, "guid").text = f"{APP_URL}/audio/{email_info['file']}"
    return ET.tostring(rss, encoding="utf-8")

@app.route("/feed")
def feed():
    files = [f for f in os.listdir(AUDIO_DIR) if f.endswith(".mp3")]
    emails = [{"subject": f.split('.')[0], "file": f} for f in files]
    xml_data = generate_rss(emails)
    return Response(xml_data, mimetype="application/rss+xml")

@app.route("/audio/<path:filename>")
def audio(filename):
    path = os.path.join(AUDIO_DIR, filename)
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        return Response(data, mimetype="audio/mpeg")
    return "Not found", 404

@app.route("/envtest")
def envtest():
    user = os.environ.get("GMAIL_USER")
    pw_set = bool(os.environ.get("GMAIL_PASS"))
    return {
        "GMAIL_USER": user,
        "GMAIL_PASS_SET": pw_set
    }

if __name__ == "__main__":
    new_emails = fetch_unread_emails()
    print(f"Generated {len(new_emails)} new MP3 files.")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
