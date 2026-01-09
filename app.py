from flask import Flask, jsonify
import os
import threading
import time

app = Flask(__name__)

# صفحة البداية
@app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Subscription Bot</title>
        <meta charset="UTF-8">
        <style>
            body {
                font-family: Arial, sans-serif;
                direction: rtl;
                text-align: center;
                padding: 50px;
                background-color: #f5f5f5;
            }
            .container {
                background-color: white;
                padding: 40px;
                border-radius: 10px;
                box-shadow: 0 0 10px rgba(0,0,0,0.1);
                max-width: 800px;
                margin: 0 auto;
            }
            h1 {
                color: #333;
            }
            .status {
                padding: 15px;
                margin: 20px 0;
                border-radius: 5px;
                font-weight: bold;
            }
            .running {
                background-color: #d4edda;
                color: #155724;
                border: 1px solid #c3e6cb;
            }
            .info {
                background-color: #d1ecf1;
                color: #0c5460;
                border: 1px solid #bee5eb;
                padding: 15px;
                margin: 20px 0;
                border-radius: 5px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 بوت إدارة الاشتراكات</h1>
            <div class="status running">
                ✅ البوت يعمل بشكل طبيعي
            </div>
            <div class="info">
                <p>📅 تاريخ البدء: """ + time.strftime("%Y-%m-%d %H:%M:%S") + """</p>
                <p>🌐 البيئة: """ + ("Render" if os.environ.get('RENDER') else "Local") + """</p>
                <p>🚦 الحالة: نشط</p>
            </div>
            <h2>📊 روابط التحقق:</h2>
            <ul style="list-style: none; padding: 0;">
                <li style="margin: 10px 0;"><a href="/health">فحص الصحة</a></li>
                <li style="margin: 10px 0;"><a href="/status">حالة النظام</a></li>
            </ul>
        </div>
    </body>
    </html>
    """

# فحص صحة النظام
@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "service": "telegram-subscription-bot",
        "environment": "render" if os.environ.get('RENDER') else "local"
    }), 200

# حالة النظام
@app.route('/status')
def status():
    return jsonify({
        "status": "running",
        "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "bot_token_set": bool(os.environ.get('BOT_TOKEN')),
        "platform": "render" if os.environ.get('RENDER') else "local",
        "python_version": os.environ.get('PYTHON_VERSION', '3.11.0')
    }), 200

# تشغيل التطبيق
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)