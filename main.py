import os
import imaplib
import email
from email.header import decode_header
from gtts import gTTS
from flask import Flask, Response
import xml.etree.ElementTree as ET
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import re

# Temporary local storage
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# Gmail
GMAIL_USER = os.environ.get("GMAIL_ADDRESS")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

# Google Drive
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # path to JSON key
MP3_FOLDER_ID = os.environ.get("GDRIVE_MP3_FOLDER_ID")               # Drive folder for MP3s
DOC_FOLDER_ID = os.environ.get("GDRIVE_DOC_FOLDER_ID")               # Drive folder for Docs

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=["https://www.googleapis.com/auth/drive"]
)
drive_service = build("drive", "v3", credentials=credentials)

app = Flask(__name__)

def upload_to_drive(filepath, filename, folder_id, mimetype):
    """Upload a file to Google Drive."""
    file_metadata = {
        "name": filename,
        "parents": [folder_id]
    }
    media = MediaFileUpload(filepath, mimetype=mimetype, resumable=True)
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webContentLink"
    ).execute()
    # Make the file public
    drive_service.permissions().create(
        fileId=file["id"],
        body={"role": "reader", "type": "anyone"}
    ).execute()
    # Return public link
    return f"https://drive.google.com/uc?id={file['id']}&export=download"

def create_google_doc(text, name):
    """Create a Google Doc with the email text."""
    doc_service = build("docs", "v1", credentials=credentials)
    doc = doc_service.documents().create(body={"title": name}).execute()
    doc_id = doc["documentId"]
    doc_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]}
    ).execute()
    # Move to folder
    drive_service.files().update(fileId=doc_id, addParents=DOC_FOLDER_ID).execute()
    return doc_id

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

        # Convert to MP3
        filename = f"{eid.decode()}.mp3"
        filepath = os.path.join(AUDIO_DIR, filename)
        tts = gTTS(body[:2000])
        tts.save(filepath)

        # Upload to Google Drive
        mp3_url = upload_to_drive(filepath, filename, MP3_FOLDER_ID, "audio/mpeg")
        create_google_doc(body, f"{subject}")  # store text in Docs

        results.append({"subject": subject, "file_url": mp3_url})
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
        ET.SubElement(item, "enclosure", url=email_info["file_url"], type="audio/mpeg")
        ET.SubElement(item, "guid").text = email_info["file_url"]
    return ET.tostring(rss, encoding="utf-8")

@app.route("/feed")
def feed():
    emails = fetch_unread_emails()
    xml_data = generate_rss(emails)
    return Response(xml_data, mimetype="application/rss+xml")

@app.route("/envtest")
def envtest():
    return {
        "GMAIL_USER": GMAIL_USER,
        "GMAIL_PASS_SET": bool(GMAIL_PASS)
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
