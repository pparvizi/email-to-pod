import os
import imaplib
import email
from email.header import decode_header
from gtts import gTTS
from flask import Flask, Response
import xml.etree.ElementTree as ET
import re
import json
import threading
import time
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from io import BytesIO

# -----------------------------
# TEMP STORAGE
# -----------------------------
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# -----------------------------
# ENV VARIABLES
# -----------------------------
GMAIL_USER = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
MP3_FOLDER_ID = os.environ.get("GDRIVE_MP3_FOLDER_ID")
DOC_FOLDER_ID = os.environ.get("GDRIVE_DOC_FOLDER_ID")
RSS_FILE_NAME = "email_to_pod_feed.xml"
APP_URL = os.environ.get("RENDER_EXTERNAL_URL")

# -----------------------------
# LOAD SERVICE ACCOUNT CREDENTIALS
# -----------------------------
from google.oauth2.service_account import Credentials

sa_key_json_str = os.environ.get("SA_KEY_JSON")
if not sa_key_json_str:
    raise ValueError("Missing SA_KEY_JSON environment variable")

sa_info = json.loads(sa_key_json_str)

# Scopes needed for Drive + Docs only
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents"
]

credentials = Credentials.from_service_account_info(sa_info, scopes=SCOPES)

# Build Drive and Docs services
drive_service = build("drive", "v3", credentials=credentials)
doc_service = build("docs", "v1", credentials=credentials)

# -----------------------------
# FLASK
# -----------------------------
app = Flask(__name__)


# -----------------------------
# GOOGLE DRIVE HELPERS
# -----------------------------
def upload_to_drive(filepath, filename, folder_id, mimetype):
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(filepath, mimetype=mimetype, resumable=True)

    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()

    # Make public
    drive_service.permissions().create(
        fileId=file["id"],
        body={"role": "reader", "type": "anyone"}
    ).execute()

    return f"https://drive.google.com/uc?id={file['id']}&export=download"


def create_google_doc(text, name):
    doc = doc_service.documents().create(body={"title": name}).execute()
    doc_id = doc["documentId"]

    doc_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [
            {"insertText": {"location": {"index": 1}, "text": text}}
        ]}
    ).execute()

    drive_service.files().update(
        fileId=doc_id,
        addParents=DOC_FOLDER_ID
    ).execute()

    return doc_id


# -----------------------------
# RSS HELPERS
# -----------------------------
def load_existing_rss():
    try:
        results = drive_service.files().list(
            q=f"name='{RSS_FILE_NAME}' and '{MP3_FOLDER_ID}' in parents",
            fields="files(id)"
        ).execute()

        files = results.get("files", [])
        if not files:
            return []

        file_id = files[0]["id"]
        request = drive_service.files().get_media(fileId=file_id)
        xml_bytes = request.execute()

        root = ET.fromstring(xml_bytes)
        items = []

        for item in root.findall("./channel/item"):
            title = item.find("title").text
            enclosure = item.find("enclosure").attrib["url"]
            items.append({"subject": title, "file_url": enclosure})

        return items

    except Exception:
        return []


def save_rss_to_drive(xml_bytes):
    try:
        results = drive_service.files().list(
            q=f"name='{RSS_FILE_NAME}' and '{MP3_FOLDER_ID}' in parents",
            fields="files(id)"
        ).execute()

        files = results.get("files", [])

        # Prepare in-memory file upload object
        memfile = BytesIO(xml_bytes)
        media = MediaFileUpload(memfile, mimetype="application/rss+xml")

        if files:
            drive_service.files().update(
                fileId=files[0]["id"], media_body=media
            ).execute()

        else:
            metadata = {"name": RSS_FILE_NAME, "parents": [MP3_FOLDER_ID]}
            new_file = drive_service.files().create(
                body=metadata,
                media_body=media,
                fields="id"
            ).execute()

            drive_service.permissions().create(
                fileId=new_file["id"],
                body={"role": "reader", "type": "anyone"}
            ).execute()

    except Exception as e:
        print("RSS save error:", e)
        import traceback
        traceback.print_exc()

# -----------------------------
# EMAIL PROCESSING
# -----------------------------
def fetch_and_process_emails():
    print("DEBUG: GMAIL_USER=", GMAIL_USER)
    print("DEBUG: GMAIL_APP_PASSWORD set?", bool(GMAIL_APP_PASSWORD))
    print("IMAP: Connecting...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    status, messages = mail.search(None, '(UNSEEN)')
    email_ids = messages[0].split()
    print(f"IMAP: Found {len(email_ids)} unread")

    existing = load_existing_rss()
    results = existing.copy()

    for eid in email_ids:
        _, msg_data = mail.fetch(eid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        subject, enc = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = subject.decode(enc or "utf-8", "ignore")
        subject = subject or "No Subject"

        # Extract body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", "ignore")
                    break
                elif ctype == "text/html" and not body:
                    html = part.get_payload(decode=True).decode("utf-8", "ignore")
                    body = re.sub('<[^<]+?>', '', html)
        else:
            body = msg.get_payload(decode=True).decode("utf-8", "ignore")

        if not body.strip():
            print("Empty email skipped")
            continue

        # -----------------------
        # Generate TTS MP3
        # -----------------------
        filename = f"{eid.decode()}.mp3"
        filepath = os.path.join(AUDIO_DIR, filename)

        tts = gTTS(body[:2000])  # limit to avoid gTTS issues
        tts.save(filepath)

        # Upload MP3 + Create Google Doc
        mp3_url = upload_to_drive(filepath, filename, MP3_FOLDER_ID, "audio/mpeg")
        create_google_doc(body, subject)

        results.append({"subject": subject, "file_url": mp3_url})

    mail.logout()
    return results


# -----------------------------
# RSS GENERATION
# -----------------------------
def generate_rss(items):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "Email to Pod"
    ET.SubElement(channel, "link").text = APP_URL
    ET.SubElement(channel, "description").text = "Your emails as spoken podcasts"

    for entry in items:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = entry["subject"]
        ET.SubElement(item, "enclosure",
                      url=entry["file_url"], type="audio/mpeg")
        ET.SubElement(item, "guid").text = entry["file_url"]

    return ET.tostring(rss, encoding="utf-8")


# -----------------------------
# BACKGROUND THREAD LOOP
# -----------------------------
def background_loop():
    print("Background IMAP thread started")
    while True:
        try:
            items = fetch_and_process_emails()
            xml = generate_rss(items)
            save_rss_to_drive(xml)
            print("RSS updated")
        except Exception as e:
            print("Background loop error:", e)

        time.sleep(60)  # run every 60 seconds


# -----------------------------
# START BACKGROUND THREAD
# -----------------------------
threading.Thread(target=background_loop, daemon=True).start()


# -----------------------------
# FLASK ROUTES
# -----------------------------
@app.route("/")
def home():
    return "OK - service running"


@app.route("/feed")
def feed():
    """Podcast apps call this — returns latest RSS."""
    items = load_existing_rss()
    xml = generate_rss(items)
    return Response(xml, mimetype="application/rss+xml")


@app.route("/envtest")
def envtest():
    return {
        "GMAIL_USER": GMAIL_USER,
        "HAS_APP_PASSWORD": bool(GMAIL_APP_PASSWORD),
        "HAS_TOKEN": bool(token_json_str)
    }


# -----------------------------
# RUN SERVER
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
