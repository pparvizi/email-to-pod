import os
import base64
import time
import requests
from flask import Flask, Response
from gtts import gTTS
import xml.etree.ElementTree as ET

# Configuration
GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
ACCESS_TOKEN = os.environ.get("GMAIL_ACCESS_TOKEN")
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

app = Flask(__name__)
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

def get_emails():
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    params = {"labelIds": ["INBOX"], "q": "is:unread"}
    r = requests.get(GMAIL_API_URL, headers=headers, params=params)
    if r.status_code != 200:
        print("Error fetching emails:", r.text)
        return []

    data = r.json().get("messages", [])
    emails = []
    for msg in data:
        msg_id = msg["id"]
        detail = requests.get(f"{GMAIL_API_URL}/{msg_id}", headers=headers).json()
        snippet = detail.get("snippet", "")
        headers_list = detail.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers_list if h["name"] == "Subject"), "No Subject")

        # Convert to audio
        filename = f"{msg_id}.mp3"
        filepath = os.path.join(AUDIO_DIR, filename)
        if not os.path.exists(filepath):
            tts = gTTS(snippet)
            tts.save(filepath)
            emails.append({"subject": subject, "file": filename})

    return emails

def generate_rss(emails):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Email to Pod"
    ET.SubElement(channel, "link").text = APP_URL
    ET.SubElement(channel, "description").text = "Emails converted to podcast audio"

    for email in emails:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = email["subject"]
        ET.SubElement(item, "enclosure", url=f"{APP_URL}/audio/{email['file']}", type="audio/mpeg")
        ET.SubElement(item, "guid").text = f"{APP_URL}/audio/{email['file']}"

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

if __name__ == "__main__":
    print("Checking for new emails...")
    emails = get_emails()
    if emails:
        print(f"Created {len(emails)} new MP3s.")
    else:
        print("No new unread emails found.")
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
