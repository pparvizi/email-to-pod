import os
import imaplib
import email
from email.header import decode_header
from gtts import gTTS
from flask import Flask, Response
import xml.etree.ElementTree as ET
import re
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from io import BytesIO

# Temporary local storage
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# Gmail
GMAIL_USER = os.environ.get("GMAIL_ADDRESS")
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://email-to-pod.onrender.com")

# Google Drive / Docs
MP3_FOLDER_ID = os.environ.get("GDRIVE_MP3_FOLDER_ID")
DOC_FOLDER_ID = os.environ.get("GDRIVE_DOC_FOLDER_ID")
RSS_FILE_NAME = "email_to_pod_feed.xml"

# Load OAuth credentials from token.json secret file in Render
with open("/opt/render/project/secrets/token.json", "r") as f:
    creds_data = json.load(f)

credentials = Credentials.from_authorized_user_info(
    creds_data,
    scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/gmail.readonly"
    ]
)

drive_service = build("drive", "v3", credentials=credentials)
doc_service = build("docs", "v1", credentials=credentials)

app = Flask(__name__)

# --- Google Drive helpers ---
def upload_to_drive(filepath, filename, folder_id, mimetype):
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(filepath, mimetype=mimetype, resumable=True)
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webContentLink"
    ).execute()
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
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]}
    ).execute()
    drive_service.files().update(fileId=doc_id, addParents=DOC_FOLDER_ID).execute()
    return doc_id

# --- Persistent RSS helpers ---
def load_existing_rss():
    try:
        results = drive_service.files().list(
            q=f"name='{RSS_FILE_NAME}' and '{MP3_FOLDER_ID}' in parents",
            spaces="drive",
            fields="files(id, name)"
        ).execute()
        files = results.get("files", [])
        if not files:
            return []

        rss_file_id = files[0]["id"]
        request = drive_service.files().get_media(fileId=rss_file_id)
        fh = BytesIO()
        downloader = request.execute()
        fh.write(downloader)
        fh.seek(0)
        xml_data = fh.read()
        root = ET.fromstring(xml_data)
        items = []
        for item in root.findall("./channel/item"):
            title = item.find("title").text
            enclosure = item.find("enclosure").attrib.get("url")
            items.append({"subject": title, "file_url": enclosure})
        return items
    except Exception as e:
        print("No existing RSS found:", e)
        return []

def save_rss_to_drive(xml_bytes):
    try:
        results = drive_service.files().list(
            q=f"name='{RSS_FILE_NAME}' and '{MP3_FOLDER_ID}' in parents",
            spaces="drive",
            fields="files(id, name)"
        ).execute()
        files = results.get("files", [])
        media = MediaFileUpload(BytesIO(xml_bytes), mimetype="application/rss+xml")
        if files:
            file_id = files[0]["id"]
            drive_service.files().update(fileId=file_id, media_body=media).execute()
        else:
            file_metadata = {"name": RSS_FILE_NAME, "parents": [MP3_FOLDER_ID]}
            file = drive_service.files().create(body=file_metadata, media_body=media).execute()
            drive_service.permissions().create(
                fileId=file["id"],
                body={"role": "reader", "type": "anyone"}
            ).execute()
    except Exception as e:
        print("Error saving RSS to Drive:", e)

# --- Email processing ---
def fetch_unread_emails():
    print("Connecting to Gmail IMAP...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, os.environ.get("GMAIL_APP_PASSWORD", ""))
    mail.select("inbox")

    status, messages = mail.search(None, '(UNSEEN)')
    email_ids = messages[0].split()
    print(f"Found {len(email_ids)} unread emails.")

    existing_entries = load_existing_rss()
    results = existing_entries.copy()

    for eid in email_ids:
        status, msg_data = mail.fetch(eid, "(RFC822)")
        if status != "OK":
            continue

        msg = email.message_from_bytes(msg_data[0][1])
        subject, encoding = decode_header(msg["Subject"])[0]
        if isinstance(subject, bytes):
            subject = subject.decode(encoding or "utf-8", errors="ignore")
        subject = subject or "No Subject"

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break
                elif ctype == "text/html" and not body:
                    html_content = part.get_payload(decode=True).decode(errors="ignore")
                    body = re.sub('<[^<]+?>', '', html_content)
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        if not body.strip():
            print(f"Skipping empty email: {subject}")
            continue

        filename = f"{eid.decode()}.mp3"
        filepath = os.path.join(AUDIO_DIR, filename)
        tts = gTTS(body[:2000])
        tts.save(filepath)

        mp3_url = upload_to_drive(filepath, filename, MP3_FOLDER_ID, "audio/mpeg")
        create_google_doc(body, subject)

        results.append({"subject": subject, "file_url": mp3_url})

    mail.logout()
    return results

# --- RSS generation ---
def generate_rss(emails):
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "Email to Pod"
    ET.SubElement(channel, "link").text = APP_URL
    ET.SubElement(channel, "description").text = "Your emails as spoken podcasts"

    for email_info in emails:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = email_info["subject"]
        ET.SubElement(item, "enclosure", url=email_info["file_url"], type="audio/mpeg")
        ET.SubElement(item, "guid").text = email_info["file_url"]

    return ET.tostring(rss, encoding="utf-8")

# --- Flask endpoints ---
@app.route("/feed")
def feed():
    emails = fetch_unread_emails()
    xml_data = generate_rss(emails)
    save_rss_to_drive(xml_data)
    return Response(xml_data, mimetype="application/rss+xml")

@app.route("/envtest")
def envtest():
    return {"GMAIL_USER": GMAIL_USER, "GMAIL_PASS_SET": bool(os.environ.get("GMAIL_APP_PASSWORD"))}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
