from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
import imaplib
import email
import os
import base64
from email.header import decode_header
from bs4 import BeautifulSoup
from html import unescape
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# Load biến môi trường từ file .env
load_dotenv()

app = Flask(__name__, static_folder="../")

# ✅ Bật CORS
CORS(app, resources={r"/*": {"origins": "*"}})

# Đường dẫn tới index.html và index.js
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
INDEX_HTML_PATH = os.path.join(PARENT_DIR, "index.html")

@app.route("/")
def serve_index():
    if os.path.exists(INDEX_HTML_PATH):
        return send_file(INDEX_HTML_PATH)
    return "<h1>Không tìm thấy index.html. Vui lòng kiểm tra lại.</h1>", 404

@app.route("/index.js")
def serve_index_js():
    return send_from_directory(PARENT_DIR, "index.js")

# Chuẩn hóa text để so sánh
def normalize_text(text):
    return unescape(text).replace("\xa0", " ").strip().lower()

# Trích xuất nội dung email
def extract_email_body(msg):
    body_text, body_html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body_text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
            elif part.get_content_type() == "text/html":
                body_html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        if msg.get_content_type() == "text/plain":
            body_text = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
        elif msg.get_content_type() == "text/html":
            body_html = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")

    return body_text, body_html

# Làm sạch HTML email để hiển thị gọn trên giao diện
def clean_email_html(raw_html):
    soup = BeautifulSoup(raw_html, "html.parser")
    return str(soup.body) if soup.body else raw_html

# Encode HTML để gửi qua JSON
def encode_email_html(html):
    return base64.b64encode(html.encode("utf-8")).decode("utf-8")

# Hàm chung tìm email trong ngày và 10 phút gần nhất
def find_latest_email_within_timeframe(filter_keywords=None):
    EMAIL_MANAGER = os.getenv("EMAIL_MANAGER")
    EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(EMAIL_MANAGER, EMAIL_PASSWORD)
    mail.select("inbox")

    today = datetime.now(timezone.utc).strftime("%d-%b-%Y")
    status, email_ids = mail.search(None, f'(SINCE {today})')
    email_ids = email_ids[0].split()

    now = datetime.now(timezone.utc)
    ten_minutes_ago = now - timedelta(minutes=10)

    for email_id in reversed(email_ids):
        status, data = mail.fetch(email_id, "(RFC822)")
        raw_email = data[0][1]
        msg = email.message_from_bytes(raw_email)

        email_date = parsedate_to_datetime(msg['Date'])
        if email_date.tzinfo is None:
            email_date = email_date.replace(tzinfo=timezone.utc)

        # Bỏ qua email không nằm trong 10 phút gần nhất
        if not (ten_minutes_ago <= email_date <= now):
            continue

        body_text, body_html = extract_email_body(msg)

        print(f"📩 Kiểm tra email ID: {email_id.decode()} gửi lúc: {email_date}")

        if filter_keywords:
            lower_body_text = normalize_text(body_text)
            lower_body_html = normalize_text(body_html)

            if not any(keyword in lower_body_text or keyword in lower_body_html for keyword in filter_keywords):
                continue

        mail.logout()
        return clean_email_html(body_html)

    mail.logout()
    return None

# Quét email theo username
def find_latest_email_with_username(username):
    return find_latest_email_within_timeframe()

# Quét email theo từ khóa TV
def find_latest_email_with_tv_keyword():
    tv_keywords = ["tv", "tivi", "television"]
    return find_latest_email_within_timeframe(filter_keywords=tv_keywords)

# API scan-email
@app.route("/scan-email", methods=["POST"])
def scan_email():
    data = request.get_json()
    device_type = data.get("device_type")
    username = data.get("username", "").strip()

    email_html = None
    search_type = ""

    if device_type == "tv":
        email_html = find_latest_email_with_tv_keyword()
        search_type = "TV"
    else:
        if not username:
            return jsonify({"success": False, "message": "Thiếu tên người dùng cho thiết bị cá nhân"}), 400
        email_html = find_latest_email_with_username(username)
        search_type = f"Username ({username})"

    if email_html:
        encoded_html = encode_email_html(email_html)
        print(f"✅ Tìm thấy email ({search_type}), độ dài: {len(encoded_html)} ký tự")
        return jsonify({"success": True, "email_html": encoded_html})
    else:
        print(f"⚠️ Không tìm thấy email phù hợp ({search_type}).")
        return jsonify({"success": False, "message": f"Không tìm thấy email phù hợp ({search_type})."})

if __name__ == "__main__":
    app.run(debug=True, port=5000)
