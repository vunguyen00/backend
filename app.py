import os
import re
import time
import json
import string
import imaplib
import email
from datetime import datetime, timedelta, timezone
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, jsonify, request
from flask_cors import CORS
from playwright.sync_api import sync_playwright
import requests
import random
# --- Khởi tạo ứng dụng Flask và CORS ---
load_dotenv()  # Load biến môi trường từ .env
app = Flask(__name__)

CORS(app,
     supports_credentials=False,
     origins="*",  # ← Cho phép mọi nguồn
     allow_headers=["Content-Type", "Authorization", "ngrok-skip-browser-warning"],
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"]
)

if not firebase_admin._apps:
    firebase_cred_path = os.getenv('FIREBASE_CREDENTIAL_PATH')
    firebase_database_url = os.getenv('FIREBASE_DATABASE_URL')

    cred = credentials.Certificate(firebase_cred_path)
    firebase_admin.initialize_app(cred, {
        'databaseURL': firebase_database_url
    })

def generate_login_key(length=10):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

@app.route('/upload-account', methods=['POST', 'OPTIONS'])
def upload_account():
    """
    Nhận thông tin tài khoản Netflix từ client và lưu vào Firebase kèm login_key.
    """
    if request.method == 'OPTIONS':
        return '', 200

    data = request.get_json() or {}
    missing = [f for f in ('username','password','cookies') if not data.get(f)]
    if missing:
        return jsonify({"success": False, "message": "Missing: " + ",".join(missing)}), 400
    try:
        login_key = generate_login_key()
        rec = {
            'username': data['username'],
            'password': data['password'],
            'cookies':  data['cookies'],
            'register_date': data.get('register_date',''),
            'expire_date':   data.get('expire_date',''),
            'login_key': login_key
        }
        ref = db.reference('accounts').push(rec)
        aid = ref.key
        return jsonify({"success": True, "account_id": aid, "login_key": login_key}), 200
    except Exception as e:
        app.logger.error(f"upload_account error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500
    
@app.route('/get-data', methods=['GET'])
def get_data():
    """
    Lấy toàn bộ dữ liệu `accounts` và `users` từ Firebase.
    """
    try:
        accounts = db.reference('accounts').get() or {}
        users = db.reference('users').get() or {}
        return jsonify({"accounts": accounts, "users": users}), 200
    except Exception as e:
        app.logger.error(f"get_data error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/delete-account/<account_id>', methods=['DELETE','POST','OPTIONS'])
def delete_account(account_id):
    """
    Xóa tài khoản theo ID và các liên kết user tương ứng.
    """
    if request.method == 'OPTIONS':
        return '', 200
    try:
        delete_account_and_user_links(account_id)
        return jsonify({"success": True}), 200
    except Exception as e:
        app.logger.error(f"delete_account error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/update-account/<account_id>', methods=['PUT','OPTIONS'])
def update_account(account_id):
    """
    Cập nhật các trường của account (username, password, cookies, register_date, expire_date).
    """
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json() or {}
    allowed = {'username','password','cookies','register_date','expire_date'}
    updates = {k:v for k,v in data.items() if k in allowed}
    if not updates:
        return jsonify({"success": False, "message": "No valid fields"}), 400
    try:
        db.reference(f'accounts/{account_id}').update(updates)
        return jsonify({"success": True}), 200
    except Exception as e:
        app.logger.error(f"update_account error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/clear-account/<account_id>', methods=['POST','OPTIONS'])
def clear_account(account_id):
    """
    Xóa liên kết của tất cả user với account này và reset ngày đăng ký, hết hạn.
    """
    if request.method == 'OPTIONS':
        return '', 200
    try:
        users = db.reference('users').get() or {}
        for key, u in users.items():
            if u.get('account_id') == account_id:
                db.reference(f'users/{key}').delete()
        db.reference(f'accounts/{account_id}').update({'register_date':'','expire_date':''})
        return jsonify({"success": True}), 200
    except Exception as e:
        app.logger.error(f"clear_account error: {e}")
        return jsonify({"success": False, "message": str(e)}), 500

def delete_account_and_user_links(aid):
    """
    Xóa account và tất cả user liên kết với account đó.
    """
    db.reference(f'accounts/{aid}').delete()
    users = db.reference('users').get() or {}
    for key, u in users.items():
        if u.get('account_id') == aid:
            db.reference(f'users/{key}').delete()

@app.route('/warranty', methods=['POST','OPTIONS'])
def warranty():
    """
    Xác minh bảo hành bằng login_key duy nhất.
    """
    if request.method == 'OPTIONS':
        return '', 200

    data = request.get_json() or {}
    login_key = data.get('login_key')
    if not login_key:
        return jsonify({'success': False, 'message': 'Missing login_key'}), 400

    # Tìm tài khoản theo login_key
    all_accounts = db.reference('accounts').get() or {}
    matched = [(aid, acc) for aid, acc in all_accounts.items() if acc.get('login_key') == login_key]
    if not matched:
        return jsonify({'success': False, 'message': 'Không tìm thấy tài khoản với login_key này'}), 404

    aid, acc = matched[0]
    if acc.get('cookies') and check_cookie_session(acc['cookies']):
        return jsonify({'success': True, 'status': 'active', 'account_id': aid,
                        'username': acc.get('username',''), 'password': acc.get('password','')}), 200

    # Nếu cookie hết hạn → tìm tài khoản khác còn sống
    delete_account_and_user_links(aid)
    exp_date = acc.get('expire_date', '')
    remaining_days = 30
    try:
        exp_dt = datetime.strptime(exp_date, '%m/%d/%y').date()
        delta = (exp_dt - datetime.today().date()).days
        if delta > 0:
            remaining_days = delta
    except Exception:
        pass

    all_users = db.reference('users').get() or {}
    used_ids = {u.get('account_id') for u in all_users.values()}
    candidates = [new_aid for new_aid, a in all_accounts.items()
                  if new_aid not in used_ids and a.get('cookies') and new_aid != aid]

    for new_aid in candidates:
        new_acc = all_accounts[new_aid]
        if check_cookie_session(new_acc['cookies']):
            today_dt = datetime.today()
            reg = today_dt.strftime('%m/%d/%y')
            exp = (today_dt + timedelta(days=remaining_days)).strftime('%m/%d/%y')
            db.reference(f'accounts/{new_aid}').update({'register_date': reg, 'expire_date': exp})

            return jsonify({'success': True, 'status': 'replaced', 'account_id': new_aid,
                            'username': new_acc.get('username',''), 'password': new_acc.get('password','')}), 200
        else:
            delete_account_and_user_links(new_aid)

    return jsonify({'success': False, 'message': 'Không còn tài khoản thay thế khả dụng.'}), 200

def check_cookie_session(cookie_input) -> bool:
    cookies = []
    try:
        if isinstance(cookie_input, str) and cookie_input.strip().startswith('{'):
            cookie_json = json.loads(cookie_input)
            cookies = [{
                'name': c['name'], 'value': c['value'],
                'domain': c.get('domain', '.netflix.com'), 'path': c.get('path', '/'),
                'secure': c.get('secure', True), 'httpOnly': c.get('httpOnly', False),
            } for c in cookie_json.get('cookies', [])]
        elif isinstance(cookie_input, dict) and 'cookies' in cookie_input:
            cookies = [{
                'name': c['name'], 'value': c['value'],
                'domain': c.get('domain', '.netflix.com'), 'path': c.get('path', '/'),
                'secure': c.get('secure', True), 'httpOnly': c.get('httpOnly', False),
            } for c in cookie_input['cookies']]
        elif isinstance(cookie_input, str):
            for pair in cookie_input.split(';'):
                pair = pair.strip()
                if '=' not in pair: continue
                name, value = pair.split('=', 1)
                cookies.append({'name': name, 'value': value, 'domain': '.netflix.com', 'path': '/'})
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            context.add_cookies(cookies)
            page = context.new_page()
            page.goto('https://www.netflix.com/browse', timeout=25000)
            active = '/browse' in page.url
            browser.close()
            return active
    except Exception as e:
        app.logger.error(f"check_cookie_session error: {e}")
        return False

@app.route("/admin-assign-account", methods=["POST", "OPTIONS"])
def admin_assign_account():
    if request.method == 'OPTIONS':
        return '', 200

    data = request.get_json() or {}
    guest_name = data.get("guest_name")
    expire_date = data.get("expire_date") 

    if not guest_name or not expire_date:
        return jsonify({"success": False, "message": "Thiếu tên người dùng hoặc hạn dùng"}), 400

    # Tìm account còn sống và chưa bị dùng
    all_accounts = db.reference('accounts').get() or {}
    all_users = db.reference('users').get() or {}
    used_ids = {u.get('account_id') for u in all_users.values()}
    candidates = [(aid, acc) for aid, acc in all_accounts.items()
                  if aid not in used_ids and acc.get('cookies')]

    for aid, acc in candidates:
        if check_cookie_session(acc['cookies']):
            today = datetime.today()
            register_date = today.strftime('%m/%d/%y')
            exp_date_fmt = datetime.strptime(expire_date, '%Y-%m-%d').strftime('%m/%d/%y')

            # Cập nhật account ngày ĐK + hết hạn
            db.reference(f'accounts/{aid}').update({
                'register_date': register_date,
                'expire_date': exp_date_fmt
            })

            # Tạo user mới
            user_data = {'guest_name': guest_name, 'account_id': aid}
            db.reference('users').push(user_data)

            return jsonify({
                "success": True,
                "account_id": aid,
                "username": acc.get("username", "")
            }), 200
        else:
            delete_account_and_user_links(aid)

    return jsonify({"success": False, "message": "Không tìm thấy tài khoản còn hoạt động."}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
