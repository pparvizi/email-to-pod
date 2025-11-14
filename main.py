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
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
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
### FIX: Load only service account credentials
from google.oauth2.service_account import Credentials

sa_key_json_str = os.environ.get("SA_KEY_JSON")
if not sa_key_json_str:
    raise ValueError("Missing SA_KEY_JSON environment variable")

sa_info = json.loads(sa_key_json_str)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents"
]

credentials = Credentials.from_service_account_info(sa_info, scopes=SCOPES)

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

    with open(filepath, "rb") as f:
        media = MediaIoBaseUpload(f, mimetype=mimetype, resumable=False)

        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id"
        ).execute()

    # Make file public
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
        result = drive_service.files().list(
            q=f"name='{RSS_FILE_NAME}' and '{MP3_FOLDER_ID}' in parents",
            fields="files(id)"
        ).execute()

        files = result.get("files", [])
        if not files:
            return []

        file_id = files[0]["id"]
        xml_bytes = drive_service.files().get_media(fileId=file_id).execute()

        root = ET.fromstring(xml_bytes)
        items = []

        for item in root.findall("./channel/item"):
            title = item.find("title").text
            enclosure = item.find("enclosure").attrib["url"]
            items.append({"subject": title, "file_url": enclosure})

        return items

    except Exception
