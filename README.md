# Telegram Subscription Bot for Render

هذا بوت Telegram لإدارة الاشتراكات يعمل على منصة Render.

## المميزات

- نظام إدارة الاشتراكات
- أكواد تفعيل ذات صلاحية محددة
- فترة تجريبية مجانية (48 ساعة)
- نظام الأزرار الديناميكية
- إدارة المشرفين
- إدارة القنوات المتعددة
- إشعارات انتهاء الصلاحية التلقائية

## التثبيت على Render

1. انسخ هذا المشروع إلى حسابك على GitHub
2. سجل الدخول إلى [render.com](https://render.com)
3. اضغط على **New +** واختر **Background Worker**
4. اختر مستودع GitHub الخاص بك
5. أدخل المعلومات التالية:
   - **Name**: subscription-bot-worker
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
6. أضف متغيرات البيئة:
   - `BOT_TOKEN`: توكن بوت Telegram الخاص بك
   - `CHANNEL_ID`: معرف القناة الرئيسية (مثال: -1001234567890)
   - `CHANNEL_USERNAME`: اسم المستخدم للقناة (مثال: @channel_name)
   - `MAIN_ADMIN`: المشرف الرئيسي (مثال: @admin_username)
   - `ADMIN_IDS`: معرفات المشرفين مفصولة بفواصل (مثال: 123456789,987654321)
7. اختر **Free** Plan
8. اضغط **Create Background Worker**

## متغيرات البيئة المطلوبة

```env
BOT_TOKEN=your_bot_token_here
CHANNEL_ID=-1001234567890
CHANNEL_USERNAME=@channel_name
MAIN_ADMIN=@admin_username
ADMIN_IDS=123456789