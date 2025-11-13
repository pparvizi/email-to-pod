import os
import imaplib
import email
from email.header import decode_header
from gtts import gTTS
from flask import Flask, Response
import xml.etree.ElementTree as ET
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload
import re
import io
import json

# Gmail
GMAIL_USER = os.environ.get("GMAIL_ADDRESS")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD")
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")

# Google Drive / Docs
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # JSON key
MP3_FOLDER_ID = os.environ.get("GDRIVE_MP3_FOLDER_ID")
DOC_FOLDER_ID = os.environ.get("GDRIVE_DOC_FOLDER_ID")
RSS_FILE_NAME = "email_to_pod_feed.json"  # JSON storing processed emails

# Authenticate service account
credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents"
    ]
)
drive_service = build("drive", "v3", credentials=credentials)
docs_service = build("docs", "v1", credentials=credentials)

app = Flask(__name__)

def download_json_file_from_drive(name, folder_id):
    """Return JSON data from Drive file, or empty list if not found."""
    query = f"name='{name}' and '{folder_id}' in parents and trashed=false"
    results = drive_service.files().list(q=query, fields="files(id)").execute()
    files = results.get("files", [])
    if not files:
        return [], None
    file_id = files[0]["id"]
    fh = io.BytesIO()
    request = drive_service.files().get_media(fileId=file_id)
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    data = json.load(fh)
    return data, file_id

def upload_json_to_drive(data, name, folder_id, existing_file_id=None):
    """Upload JSON file to Drive, overwrite if exists."""
    fh = io.BytesIO()
    fh.write(json.dumps(data).encode("utf-8"))
    fh.seek(0)
    media = MediaIoBaseUpload(fh, mimetype="application/json", resumable=True)
    if existing_file_id:
        file = drive_service.files().update(fileId=existing_file_id, media_body=media).execute()
    else:
        file_metadata = {"name": name, "parents": [folder_id]}
        file = drive_service.files().create(body=file_metadata, media_body=media).execute()
    return file.get("id")

def upload_to_drive(filepath, filename, folder_id, mimetype):
    """Upload a file to Google Drive and make it public."""
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(filepath, mimetype=mimetype, resumable=True)
    file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id"
    ).execute()
    drive_service.permissions().create(
        fileId=file["id"],
        body={"role": "reader", "type": "anyone"}
    ).execute()
    return f"https://drive.google.com/uc?id={file['id']}&export=download"

def create_google_doc(text, name):
    """Create a Google Doc and move it to the specified folder."""
    doc = docs_service.documents().create(body={"title": name}).execute()
    doc_id = doc["documentId"]
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]}
    ).execute()
    drive_service.files().update(fileId=doc_id, addParents=DOC_FOLDER_ID).execute()
    return doc_id

def fetch_new_emails(processed_ids):
    """Fetch unread Gmail emails not in processed_ids, return list of dicts."""
    print("Connecting to Gmail IMAP...")
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_USER, GMAIL_PASS)
    mail.select("inbox")
    status, messages = mail.search(None, "(UNSEEN)")
    email_ids = messages[0].split()
    print(f"Found {len(email_ids)} unread emails.")

    new_emails = []
    for eid in email_ids:
        eid_str = eid.decode()
        if eid_str in processed_ids:
            continue
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
                    body = re.sub("<[^<]+?>", "", html_content)
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        if not body.strip():
            print(f"Skipping empty email: {subject}")
            continue

        # Create MP3
        filename = f"{eid_str}.mp3"
        filepath = f"/tmp/{filename}"
        tts = gTTS(body[:2000])
        tts.save(filepath)

        mp3_url = upload_to_drive(filepath, filename, MP3_FOLDER_ID, "audio/mpeg")
        create_google_doc(body, subject)

        new_emails.append({"id": eid_str, "subject": subject, "file_url": mp3_url})

    mail.logout()
    return new_emails

def generate_rss(emails):
    """Generate RSS feed XML."""
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
    # Load previously processed emails
    processed_emails, rss_file_id = download_json_file_from_drive(RSS_FILE_NAME, MP3_FOLDER_ID)
    processed_ids = {e["id"] for e in processed_emails}

    # Fetch new emails
    new_emails = fetch_new_emails(processed_ids)

    # Update JSON record
    all_emails = processed_emails + new_emails
    upload_json_to_drive(all_emails, RSS_FILE_NAME, MP3_FOLDER_ID, existing_file_id=rss_file_id)

    xml_data = generate_rss(all_emails)
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
