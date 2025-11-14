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

# --- Temporary local storage ---
AUDIO_DIR = "/tmp/audio"
os.makedirs(AUDIO_DIR, exist_ok=True)

# --- Environment variables ---
GMAIL_USER = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")  # Only if using app password for IMAP
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://email-to-pod.onrender.com")
MP3_FOLDER_ID = os.environ.get("GDRIVE_MP3_FOLDER_ID")
DOC_FOLDER_ID = os.environ.get("GDRIVE_DOC_FOLDER_ID")
RSS_FILE_NAME = "email_to_pod_feed.xml"

# --- Load OAuth token.json secret file ---
TOKEN_JSON_PATH = os.environ.get("TOKEN_JSON_FILE")
with open(TOKEN_JSON_PATH, "r") as f:
    token_data = json.load(f)

credentials = Credentials.from_authorized_user_info(
    token_data,
    scopes=[
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/gmail.readonly"
    ]
)

drive_service = build("drive", "v3", credentials=credentials)
doc_service = build("docs", "v1", credentials=credentials)

# --- Flask app ---
app = Flask(__name__)

# --- Google Drive helpers ---
def upload_to_drive(filepath, filename, folder_id, mimetype):
    file_metadata = {"name": filename, "parents": [folder_id]}
    media = MediaFileUpload(filepath, mimetype=mimetype, resumable=True)
    file = drive_service.files().create(
        body=file_metadata, media_body=media, fields="id, webContentLink"
    ).execute()
    drive_service.permissions().create(
        fileId=file["id"], body={"role": "reader", "type": "anyone"}
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

# --- RSS helpers ---
def load_existing_rss():
    try:
        results = drive_service.files().list(
            q=f"name='{RSS_FILE_NAME}' and '{MP3_FOLDER_ID}' in parents",
            spaces="drive",
            fields="files(id, name)"
        ).execute()
        files = results.get("fi
