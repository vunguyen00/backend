from flask import Flask, jsonify
from flask_cors import CORS
import imaplib
import email
import os
import re
import time
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Load biến môi trường từ file .env
load_dotenv()

EMAIL_MANAGER = os.getenv("EMAIL_MANAGER")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

def extract_email_body(msg):
    """Trích xuất nội dung email (Text & HTML)."""
    body_text, body_html = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                if content_type == "text/plain":
                    body_text += payload.decode(charset, errors="ignore")
                elif content_type == "text/html":
                    body_html += payload.decode(charset, errors="ignore")
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        content_type = msg.get_content_type()
        if payload:
            if content_type == "text/plain":
                body_text = payload.decode(charset, errors="ignore")
            elif content_type == "text/html":
                body_html = payload.decode(charset, errors="ignore")
    return body_text, body_html

def extract_otp_from_text(text):
    """
    Tìm kiếm mã PIN/OTP/... dựa trên:
    1) Regex "từ khóa" + tối đa 30 ký tự + (4-6 chữ số)
    2) Nếu không tìm thấy, duyệt tất cả các số 4-6 chữ số và kiểm tra
       100 ký tự trước và sau để xem có từ khóa liên quan không.
    """

    # Danh sách từ khóa có thể mở rộng tùy ý
    # "mã pin", "pin", "mã otp", "otp", "mã truy cập", "mã xác minh", "verification code", "code"
    # Regex dưới đây: 
    # - (?i) là flag ignore case
    # - (?: ... ) là nhóm không capture
    # - \D{0,30} là 0-30 ký tự không phải số, giúp bắt trường hợp "Mã PIN của hồ sơ: 6868"
    direct_pattern = r'(?i)(?:mã\s*pin|pin|mã\s*otp|otp|mã\s*truy\s*cập|mã\s*xác\s*minh|verification\s*code|code)\D{0,30}([0-9]{4,6})'
    match = re.search(direct_pattern, text)
    if match:
        return match.group(1)

    # Nếu không tìm thấy theo pattern trên, fallback:
    # Tìm tất cả số 4-6 chữ số, xem trong 100 ký tự trước-sau có từ khóa không
    for match in re.finditer(r'\b\d{4,6}\b', text):
        start, end = match.span()
        context = text[max(0, start-100):min(len(text), end+100)]
        if re.search(
            r'(?i)(?:mã\s*pin|pin|mã\s*otp|otp|mã\s*truy\s*cập|mã\s*xác\s*minh|verification\s*code|code)',
            context
        ):
            return match.group()
    return None

def scan_first_email():
    """
    Quét email đầu tiên (mới nhất) trong hộp thư và thực hiện:
      - Lần quét 1: Tìm mã (PIN/OTP/...) dựa trên mẫu định dạng.
      - Nếu không tìm thấy, kiểm tra xem email có chứa link "Nhận mã" hay không.
    """
    mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    mail.login(EMAIL_MANAGER, EMAIL_PASSWORD)
    mail.select("inbox")
    
    status, email_ids_data = mail.search(None, "ALL")
    email_ids = email_ids_data[0].split()
    if not email_ids:
        mail.logout()
        return None

    # Lấy email mới nhất
    first_email_id = email_ids[-1]
    status, data = mail.fetch(first_email_id, "(RFC822)")
    raw_email = data[0][1]
    msg = email.message_from_bytes(raw_email)
    body_text, body_html = extract_email_body(msg)

    # Nếu body_text trống mà body_html có nội dung, chuyển HTML sang text
    if not body_text and body_html:
        body_text = BeautifulSoup(body_html, "html.parser").get_text()

    # Dùng BeautifulSoup để hỗ trợ tìm link "Nhận mã"
    soup = BeautifulSoup(body_html, "html.parser") if body_html else None

    result = {}
    otp = extract_otp_from_text(body_text) if body_text else None
    if otp:
        result["otp"] = otp

    # Nếu không tìm thấy mã, kiểm tra link "Nhận mã"
    if not result.get("otp") and soup:
        link_tag = soup.find("a", string=re.compile("Nhận mã", re.IGNORECASE))
        if link_tag:
            result["click_required"] = True
            result["receive_link"] = link_tag.get("href")
    
    mail.logout()
    return result

@app.route("/scan-email", methods=["POST"])
def scan_email():
    try:
        print("📩 Bắt đầu quét email đầu tiên...")
        # Lần quét đầu tiên: Kiểm tra mã (PIN/OTP/...) trong email
        result = scan_first_email()
        if result and "otp" in result:
            print("✅ Mã được tìm thấy trong lần quét đầu tiên.")
            return jsonify({"success": True, "otp": result["otp"]})
        
        # Nếu không có mã mà có link "Nhận mã"
        if result and result.get("click_required"):
            receive_link = result["receive_link"]
            print(f"🖱 Đang nhấn vào link 'Nhận mã': {receive_link}")
            try:
                requests.get(receive_link)  # Nhấn vào link "Nhận mã"
                print("✅ Đã nhấn vào link. Chờ 30 giây để nhận mã...")
                time.sleep(30)
                # Quét lại email đầu tiên sau khi chờ
                result2 = scan_first_email()
                if result2 and "otp" in result2:
                    print("✅ Mã được tìm thấy sau khi nhấn link.")
                    return jsonify({"success": True, "otp": result2["otp"]})
                else:
                    return jsonify({"success": False, "message": "Không tìm thấy mã sau khi nhấn link."})
            except Exception as e:
                print(f"❌ Lỗi khi nhấn vào link: {str(e)}")
                return jsonify({"success": False, "message": f"Lỗi khi nhấn vào link: {str(e)}"})
        
        return jsonify({"success": False, "message": "Không tìm thấy mã (PIN/OTP) trong email."})
    except Exception as e:
        print(f"❌ Lỗi xử lý: {str(e)}")
        return jsonify({"success": False, "message": "Lỗi server"}), 500

if __name__ == "__main__":
    app.run(debug=True, port=5000)
