import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatInviteLink
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler
import secrets
import string
import sys
import asyncio 
import json
import os
import re
from functools import wraps

# محاولة استيراد المكتبات المطلوبة للمهام المجدولة
try:
    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.executors.pool import ThreadPoolExecutor
    HAS_APSCHEDULER = True
except ImportError:
    HAS_APSCHEDULER = False
    print("⚠️ المكتبات المطلوبة للمهام المجدولة غير مثبتة. سيتم استخدام النظام بدون مهام مجدولة تلقائية.")

# =============================================
# إعدادات النظام
# =============================================
# الحصول على التوكن من متغير البيئة، إذا لم يوجد استخدم التوكن الافتراضي
BOT_TOKEN = os.getenv("BOT_TOKEN", "8292295559:AAHDGkvgZc70UAfWCQh8A317nHZQCxD9qq0")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1003139245858")  # القناة الرئيسية
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@SPX53")
MAIN_ADMIN = os.getenv("MAIN_ADMIN", "@SPX_47")
# تحويل ADMIN_IDS من سلسلة نصية إلى قائمة
admin_ids_str = os.getenv("ADMIN_IDS", "7591454108")
ADMIN_IDS = [int(id.strip()) for id in admin_ids_str.split(",")]

# تحديد المنطقة الزمنية بشكل صحيح
try:
    if HAS_APSCHEDULER:
        TIMEZONE = pytz.timezone("Asia/Riyadh")
    else:
        TIMEZONE = None
except Exception as e:
    print(f"⚠️ خطأ في تحديد المنطقة الزمنية: {e}")
    TIMEZONE = None

# حالات المحادثة
(
    WAITING_FOR_CODE, 
    WAITING_FOR_ADMIN_ID, 
    WAITING_FOR_CHANNEL_INFO, 
    WAITING_FOR_BUTTON_TEXT, 
    WAITING_FOR_BUTTON_RESPONSE,
    WAITING_FOR_CHANNEL_SELECTION,
    WAITING_FOR_CUSTOM_CHANNELS,
    WAITING_FOR_BATCH_DETAILS,
    WAITING_FOR_COUNT,
    WAITING_FOR_BUTTON_COMMAND,
    WAITING_FOR_BUTTON_DELETE,
    WAITING_FOR_BUTTON_EDIT
) = range(12)

# =============================================
# نظام التسجيل
# =============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('subscription_bot.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# =============================================
# ديكوراتور إعادة المحاولة
# =============================================
def retry_async(max_retries=3, delay=1.0):
    """ديكوراتور لإعادة المحاولة للدوال غير المتزامنة"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = delay * (2 ** attempt)  # exponential backoff
                        logger.warning(f"⚠️ محاولة {attempt + 1} فشلت، إعادة المحاولة بعد {wait_time} ثانية: {e}")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"❌ فشلت جميع {max_retries} محاولات: {e}")
            raise last_exception
        return wrapper
    return decorator

# =============================================
# نظام إدارة قاعدة البيانات - الإصدار المحدث
# =============================================
class SubscriptionManagementSystem:
    def __init__(self):
        self.setup_database()
        self.application = None
        self.setup_scheduler()
        
    def set_application(self, application):
        """تعيين تطبيق البوت للمهام المجدولة"""
        self.application = application
        
    def setup_database(self):
        """إعداد قاعدة البيانات"""
        self.conn = sqlite3.connect('subscriptions.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        
        # جدول الأكواد
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                duration_days INTEGER NOT NULL,
                price REAL DEFAULT 0,
                is_used BOOLEAN DEFAULT FALSE,
                used_by INTEGER,
                used_at TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT,
                batch_id TEXT,
                channels TEXT DEFAULT '[]',
                excluded_channels TEXT DEFAULT '[]',
                apply_to_all_channels BOOLEAN DEFAULT TRUE,
                max_uses INTEGER DEFAULT 1,
                current_uses INTEGER DEFAULT 0,
                is_active BOOLEAN DEFAULT TRUE,
                is_trial BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # جدول المشتركين
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                code_used TEXT NOT NULL,
                subscribed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                notified INTEGER DEFAULT 0,
                channels TEXT DEFAULT '[]',
                excluded_channels TEXT DEFAULT '[]',
                apply_to_all_channels BOOLEAN DEFAULT TRUE,
                invite_links TEXT DEFAULT '[]',
                last_notification TEXT,
                is_trial BOOLEAN DEFAULT FALSE,
                trial_used BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # جدول المشرفين
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                added_by INTEGER,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                permissions TEXT DEFAULT 'all',
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        
        # جدول القنوات
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS additional_channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT UNIQUE NOT NULL,
                channel_username TEXT,
                channel_name TEXT,
                added_by INTEGER,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN DEFAULT TRUE,
                channel_type TEXT DEFAULT 'premium',
                require_subscription BOOLEAN DEFAULT TRUE,
                is_main_channel BOOLEAN DEFAULT FALSE
            )
        ''')
        
        # جدول الأزرار الديناميكية
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS dynamic_buttons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                button_text TEXT NOT NULL,
                button_command TEXT UNIQUE NOT NULL,
                button_response TEXT NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # إضافة المشرف الأساسي إذا لم يكن موجوداً
        self.cursor.execute('''
            INSERT OR IGNORE INTO admins (user_id, username, first_name, last_name, added_by, permissions, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (ADMIN_IDS[0], "SPX_47", "المشرف", "الرئيسي", ADMIN_IDS[0], "all", True))
        
        # إضافة القناة الرئيسية إذا لم تكن موجودة
        self.cursor.execute('''
            INSERT OR IGNORE INTO additional_channels 
            (channel_id, channel_username, channel_name, added_by, is_active, is_main_channel)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (CHANNEL_ID, CHANNEL_USERNAME, "القناة الرئيسية", ADMIN_IDS[0], True, True))
        
        # التحقق من الأعمدة المفقودة وإضافتها
        self.add_missing_columns()
        
        self.conn.commit()
        logger.info("✅ تم إعداد قاعدة البيانات بنجاح")

    def add_missing_columns(self):
        """إضافة الأعمدة المفقودة إذا كانت غير موجودة"""
        try:
            self.cursor.execute("PRAGMA table_info(subscribers)")
            columns = [column[1] for column in self.cursor.fetchall()]
            
            missing_columns = []
            
            if 'invite_links' not in columns:
                self.cursor.execute('ALTER TABLE subscribers ADD COLUMN invite_links TEXT DEFAULT "[]"')
                missing_columns.append('invite_links')
            
            if 'last_notification' not in columns:
                self.cursor.execute('ALTER TABLE subscribers ADD COLUMN last_notification TEXT')
                missing_columns.append('last_notification')
            
            if 'is_trial' not in columns:
                self.cursor.execute('ALTER TABLE subscribers ADD COLUMN is_trial BOOLEAN DEFAULT FALSE')
                missing_columns.append('is_trial')
                
            if 'trial_used' not in columns:
                self.cursor.execute('ALTER TABLE subscribers ADD COLUMN trial_used BOOLEAN DEFAULT FALSE')
                missing_columns.append('trial_used')
            
            # التحقق من جدول codes
            self.cursor.execute("PRAGMA table_info(codes)")
            code_columns = [column[1] for column in self.cursor.fetchall()]
            
            if 'is_trial' not in code_columns:
                self.cursor.execute('ALTER TABLE codes ADD COLUMN is_trial BOOLEAN DEFAULT FALSE')
                missing_columns.append('codes.is_trial')
            
            # التحقق من جدول additional_channels
            self.cursor.execute("PRAGMA table_info(additional_channels)")
            channel_columns = [column[1] for column in self.cursor.fetchall()]
            
            if 'is_main_channel' not in channel_columns:
                self.cursor.execute('ALTER TABLE additional_channels ADD COLUMN is_main_channel BOOLEAN DEFAULT FALSE')
                missing_columns.append('additional_channels.is_main_channel')
            
            if missing_columns:
                logger.info(f"✅ تم إضافة الأعمدة المفقودة: {missing_columns}")
                
            self.conn.commit()
        except Exception as e:
            logger.error(f"❌ خطأ في إضافة الأعمدة المفقودة: {e}")

    def setup_scheduler(self):
        """إعداد المهام المجدولة"""
        if not HAS_APSCHEDULER:
            logger.warning("⚠️ APScheduler غير مثبت، سيتم استخدام النظام بدون مهام مجدولة تلقائية")
            self.scheduler = None
            return
            
        try:
            executors = {
                'default': ThreadPoolExecutor(1)
            }
            self.scheduler = BackgroundScheduler(executors=executors, timezone=TIMEZONE)
            
            # مهمة التحقق من الاشتراكات المنتهية كل يوم في الساعة 10 صباحاً
            self.scheduler.add_job(
                self.check_expired_subscriptions_wrapper,
                'cron',
                hour=10,
                minute=0,
                id='check_expired_subscriptions'
            )
            
            # مهمة إرسال تنبيهات قبل انتهاء الاشتراك بيوم
            self.scheduler.add_job(
                self.send_expiry_notifications_wrapper,
                'cron', 
                hour=9,
                minute=0,
                id='send_expiry_notifications'
            )
            
            # مهمة التحقق من انتهاء الفترات التجريبية
            self.scheduler.add_job(
                self.check_expired_trials_wrapper,
                'cron',
                hour=11,
                minute=0,
                id='check_expired_trials'
            )
            
            self.scheduler.start()
            logger.info("✅ تم إعداد المهام المجدولة")
            
        except Exception as e:
            logger.error(f"❌ خطأ في إعداد المهام المجدولة: {e}")
            self.scheduler = None

    async def safe_send_message(self, bot, chat_id, text, reply_markup=None):
        """إرسال رسالة آمن مع إعادة المحاولة"""
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال رسالة لـ {chat_id}: {e}")
            raise

    @retry_async(max_retries=3, delay=1.0)
    async def remove_user_from_additional_channels_async(self, bot, user_id, invite_links_json):
        """إخراج المستخدم من القنوات الإضافية فقط (ليس من القناة الرئيسية) - إصدار محسن"""
        try:
            if not invite_links_json:
                return
                
            invite_links = json.loads(invite_links_json)
            
            for link_info in invite_links:
                try:
                    channel_id = link_info.get('channel_id')
                    # التحقق إذا كانت هذه القناة الرئيسية
                    if channel_id == CHANNEL_ID:
                        logger.info(f"⏭️ تخطي إخراج المستخدم {user_id} من القناة الرئيسية")
                        continue
                    
                    if channel_id:
                        await bot.ban_chat_member(
                            chat_id=channel_id,
                            user_id=user_id
                        )
                        logger.info(f"✅ تم إخراج المستخدم {user_id} من القناة الإضافية {channel_id}")
                        
                        # الانتظار قليلاً لتجنب حظر API
                        await asyncio.sleep(0.5)
                        
                except Exception as e:
                    logger.error(f"❌ خطأ في إخراج المستخدم {user_id} من القناة {link_info.get('channel_id')}: {e}")
                    continue  # الاستمرار مع القنوات الأخرى
                    
        except Exception as e:
            logger.error(f"❌ خطأ في إخراج المستخدم من القنوات الإضافية: {e}")
            raise  # لإعادة المحاولة

    async def check_expired_subscriptions_async(self):
        """التحقق من الاشتراكات المنتهية (إصدار async محسن)"""
        try:
            logger.info("🔄 بدء التحقق من الاشتراكات المنتهية...")
            cursor = self.get_cursor()
            current_time = datetime.now().isoformat()
            
            # الحصول على المشتركين الذين انتهت اشتراكاتهم
            cursor.execute('''
                SELECT user_id, username, first_name, code_used, expires_at, invite_links, is_trial
                FROM subscribers 
                WHERE expires_at < ? AND is_active = TRUE
            ''', (current_time,))
            
            expired_subscribers = cursor.fetchall()
            
            if not expired_subscribers:
                logger.info("✅ لا توجد اشتراكات منتهية")
                cursor.close()
                return
            
            logger.info(f"🔄 معالجة {len(expired_subscribers)} مشترك منتهي الاشتراك")
            
            processed_count = 0
            error_count = 0
            
            for subscriber in expired_subscribers:
                user_id, username, first_name, code_used, expires_at, invite_links_json, is_trial = subscriber
                
                try:
                    # تحديث حالة المشترك
                    cursor.execute('''
                        UPDATE subscribers 
                        SET is_active = FALSE 
                        WHERE user_id = ?
                    ''', (user_id,))
                    
                    # إخراج المستخدم من القنوات الإضافية فقط (ليس من القناة الرئيسية)
                    if self.application and self.application.bot:
                        await self.remove_user_from_additional_channels_async(self.application.bot, user_id, invite_links_json)
                    
                    # إرسال رسالة للمستخدم باستخدام الإرسال الآمن
                    if self.application and self.application.bot:
                        try:
                            trial_text = "تجريبية" if is_trial else "عادية"
                            message_text = f"❌ انتهت صلاحية اشتراكك {trial_text}\n\n🎫 الكود: {code_used}\n📅 انتهى في: {expires_at.split()[0]}\n\nللاستمرار في الوصول للقنوات المميزة، يرجى تجديد الاشتراك."
                            
                            await self.safe_send_message(
                                self.application.bot,
                                user_id,
                                message_text
                            )
                        except Exception as e:
                            logger.error(f"❌ لا يمكن إرسال رسالة للمستخدم {user_id}: {e}")
                            error_count += 1
                    
                    processed_count += 1
                    logger.info(f"✅ تم تعطيل اشتراك المستخدم {user_id} ({username})")
                    
                    # إضافة تأخير لتجنب الضغط على API
                    await asyncio.sleep(1.0)
                    
                except Exception as e:
                    logger.error(f"❌ خطأ في معالجة المستخدم {user_id}: {e}")
                    error_count += 1
            
            self.conn.commit()
            cursor.close()
            
            logger.info(f"✅ تم معالجة {processed_count} مشترك، {error_count} أخطاء")
            
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من الاشتراكات المنتهية: {e}")

    def check_expired_subscriptions_wrapper(self):
        """غلاف للدالة غير المتزامنة للاستخدام مع المهام المجدولة"""
        try:
            if self.application:
                # إنشاء حلقة أحداث جديدة وتشغيل المهمة
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.check_expired_subscriptions_async())
                finally:
                    loop.close()
            else:
                logger.warning("⚠️ تطبيق البوت غير معين للمهام المجدولة")
        except Exception as e:
            logger.error(f"❌ خطأ في غلاف التحقق من الاشتراكات المنتهية: {e}")

    async def check_expired_trials_async(self):
        """التحقق من انتهاء الفترات التجريبية (إصدار async)"""
        try:
            cursor = self.get_cursor()
            current_time = datetime.now().isoformat()
            
            # الحصول على الفترات التجريبية المنتهية
            cursor.execute('''
                SELECT user_id, username, first_name, code_used, expires_at, invite_links
                FROM subscribers 
                WHERE expires_at < ? AND is_active = TRUE AND is_trial = TRUE
            ''', (current_time,))
            
            expired_trials = cursor.fetchall()
            
            if not expired_trials:
                logger.info("✅ لا توجد فترات تجريبية منتهية")
                return
            
            logger.info(f"🔄 معالجة {len(expired_trials)} فترة تجريبية منتهية")
            
            for trial in expired_trials:
                user_id, username, first_name, code_used, expires_at, invite_links_json = trial
                
                try:
                    # تحديث حالة المشترك
                    cursor.execute('''
                        UPDATE subscribers 
                        SET is_active = FALSE, trial_used = TRUE
                        WHERE user_id = ?
                    ''', (user_id,))
                    
                    # إخراج المستخدم من القنوات الإضافية فقط
                    if self.application and self.application.bot:
                        await self.remove_user_from_additional_channels_async(self.application.bot, user_id, invite_links_json)
                    
                    # إرسال رسالة للمستخدم
                    if self.application and self.application.bot:
                        try:
                            await self.safe_send_message(
                                self.application.bot,
                                user_id,
                                f"⏰ انتهت الفترة التجريبية الخاصة بك\n\n🎫 الكود: {code_used}\n📅 انتهى في: {expires_at.split()[0]}\n\nللاستمرار في الوصول للقنوات المميزة، يرجى شراء اشتراك."
                            )
                        except Exception as e:
                            logger.error(f"❌ لا يمكن إرسال رسالة للمستخدم {user_id}: {e}")
                    
                    logger.info(f"✅ تم إنهاء الفترة التجريبية للمستخدم {user_id} ({username})")
                    
                except Exception as e:
                    logger.error(f"❌ خطأ في معالجة المستخدم {user_id}: {e}")
            
            self.conn.commit()
            cursor.close()
            
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من الفترات التجريبية المنتهية: {e}")

    def check_expired_trials_wrapper(self):
        """غلاف للدالة غير المتزامنة للاستخدام مع المهام المجدولة"""
        try:
            if self.application:
                # إنشاء حلقة أحداث جديدة وتشغيل المهمة
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.check_expired_trials_async())
                finally:
                    loop.close()
            else:
                logger.warning("⚠️ تطبيق البوت غير معين للمهام المجدولة")
        except Exception as e:
            logger.error(f"❌ خطأ في غلاف التحقق من الفترات التجريبية: {e}")

    async def send_expiry_notifications_async(self):
        """إرسال تنبيهات قبل انتهاء الاشتراك بيوم (إصدار async)"""
        try:
            cursor = self.get_cursor()
            tomorrow = (datetime.now() + timedelta(days=1)).isoformat()
            today = datetime.now().isoformat()
            
            # الحصول على المشتركين الذين ستنتهي اشتراكاتهم خلال 24 ساعة
            cursor.execute('''
                SELECT user_id, username, first_name, code_used, expires_at, last_notification, is_trial
                FROM subscribers 
                WHERE expires_at BETWEEN ? AND ? 
                AND is_active = TRUE
                AND (last_notification IS NULL OR last_notification < ?)
            ''', (today, tomorrow, today))
            
            expiring_subscribers = cursor.fetchall()
            
            if not expiring_subscribers:
                logger.info("✅ لا توجد اشتراكات قريبة من الانتهاء")
                return
            
            logger.info(f"🔄 إرسال تنبيهات لـ {len(expiring_subscribers)} مشترك")
            
            for subscriber in expiring_subscribers:
                user_id, username, first_name, code_used, expires_at, last_notification, is_trial = subscriber
                
                try:
                    if self.application and self.application.bot:
                        trial_text = "تجريبية" if is_trial else ""
                        # إرسال رسالة تنبيه
                        await self.safe_send_message(
                            self.application.bot,
                            user_id,
                            f"⚠️ تنبيه: اشتراكك {trial_text} سينتهي قريباً!\n\n🎫 الكود: {code_used}\n📅 ينتهي في: {expires_at.split()[0]}\n⏰ المتبقي: أقل من 24 ساعة\n\nللتجديد، يرجى التواصل مع الإدارة."
                        )
                    
                    # تحديث وقت آخر إشعار
                    cursor.execute('''
                        UPDATE subscribers 
                        SET last_notification = ? 
                        WHERE user_id = ?
                    ''', (datetime.now().isoformat(), user_id))
                    
                    logger.info(f"✅ تم إرسال تنبيه للمستخدم {user_id}")
                    
                except Exception as e:
                    logger.error(f"❌ خطأ في إرسال تنبيه للمستخدم {user_id}: {e}")
            
            self.conn.commit()
            cursor.close()
            
        except Exception as e:
            logger.error(f"❌ خطأ في إرسال التنبيهات: {e}")

    def send_expiry_notifications_wrapper(self):
        """غلاف للدالة غير المتزامنة للاستخدام مع المهام المجدولة"""
        try:
            if self.application:
                # إنشاء حلقة أحداث جديدة وتشغيل المهمة
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.send_expiry_notifications_async())
                finally:
                    loop.close()
            else:
                logger.warning("⚠️ تطبيق البوت غير معين للمهام المجدولة")
        except Exception as e:
            logger.error(f"❌ خطأ في غلاف إرسال التنبيهات: {e}")

    def get_cursor(self):
        """الحصول على مؤشر جديد"""
        return self.conn.cursor()

    def is_admin(self, user_id):
        """التحقق إذا كان المستخدم مشرفاً"""
        try:
            cursor = self.get_cursor()
            cursor.execute('SELECT id FROM admins WHERE user_id = ? AND is_active = TRUE', (user_id,))
            result = cursor.fetchone() is not None
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من المشرف: {e}")
            return False

    def add_admin(self, user_id, username, first_name, last_name, added_by):
        """إضافة مشرف جديد"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO admins (user_id, username, first_name, last_name, added_by, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name, added_by, True))
            self.conn.commit()
            cursor.close()
            return True, "✅ تم إضافة المشرف بنجاح"
        except Exception as e:
            return False, f"❌ خطأ في إضافة المشرف: {e}"

    def remove_admin(self, user_id):
        """إزالة مشرف"""
        try:
            if user_id == ADMIN_IDS[0]:
                return False, "❌ لا يمكن حذف المشرف الرئيسي"
            
            cursor = self.get_cursor()
            cursor.execute('DELETE FROM admins WHERE user_id = ?', (user_id,))
            self.conn.commit()
            cursor.close()
            return True, "✅ تم حذف المشرف بنجاح"
        except Exception as e:
            return False, f"❌ خطأ في حذف المشرف: {e}"

    def get_all_admins(self):
        """الحصول على جميع المشرفين"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT user_id, username, first_name, last_name, added_at 
                FROM admins WHERE is_active = TRUE ORDER BY added_at DESC
            ''')
            result = cursor.fetchall()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على المشرفين: {e}")
            return []

    def generate_code(self, length=12):
        """إنشاء كود عشوائي"""
        alphabet = string.ascii_uppercase + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def create_subscription_code(self, duration_days, price=0.0, created_by="system", batch_id=None, channels=None, excluded_channels=None, apply_to_all_channels=True, max_uses=1, is_trial=False):
        """إنشاء كود اشتراك جديد"""
        code = self.generate_code()
        expires_at = (datetime.now() + timedelta(days=30)).isoformat()
        
        channels_json = json.dumps(channels or [])
        excluded_json = json.dumps(excluded_channels or [])
        
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                INSERT INTO codes (code, duration_days, price, created_by, expires_at, batch_id, channels, excluded_channels, apply_to_all_channels, max_uses, is_active, is_trial)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (code, duration_days, price, created_by, expires_at, batch_id, channels_json, excluded_json, apply_to_all_channels, max_uses, True, is_trial))
            self.conn.commit()
            cursor.close()
            
            logger.info(f"✅ تم إنشاء كود جديد: {code} لمدة {duration_days} يوم")
            return True, code
        except Exception as e:
            logger.error(f"❌ خطأ في إنشاء الكود: {e}")
            return False, f"❌ خطأ في إنشاء الكود: {e}"

    def create_multiple_codes(self, count, duration_days, price=0.0, created_by="system", batch_id=None, channels=None, excluded_channels=None, apply_to_all_channels=True, max_uses=1):
        """إنشاء عدة أكواد دفعة واحدة"""
        batch_id = batch_id or f"BATCH_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        codes = []
        failed_codes = []
        
        for i in range(count):
            success, code = self.create_subscription_code(duration_days, price, created_by, batch_id, channels, excluded_channels, apply_to_all_channels, max_uses)
            if success:
                codes.append(code)
            else:
                failed_codes.append(code)
        
        return batch_id, codes, failed_codes

    def create_batch_codes(self, count, duration_days, price=0.0, created_by="system", batch_id=None, channels=None, excluded_channels=None, apply_to_all_channels=True, max_uses=1):
        """إنشاء دفعة أكواد"""
        batch_id = batch_id or f"BATCH_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        codes = []
        
        for i in range(count):
            success, code = self.create_subscription_code(duration_days, price, created_by, batch_id, channels, excluded_channels, apply_to_all_channels, max_uses)
            if success:
                codes.append(code)
        
        return batch_id, codes

    def validate_channel_id(self, channel_id):
        """التحقق من صحة معرف القناة"""
        try:
            channel_str = str(channel_id).strip()
            
            if channel_str.startswith('@'):
                return channel_str
            elif channel_str.startswith('-100'):
                return channel_str
            elif channel_str.isdigit():
                return f"-100{channel_str}"
            else:
                return channel_str
        except Exception as e:
            logger.error(f"❌ خطأ في التحقق من معرف القناة: {e}")
            return None

    def add_additional_channel(self, channel_id, channel_username, channel_name, added_by, channel_type="premium", require_subscription=True, is_main_channel=False):
        """إضافة قناة إضافية"""
        try:
            validated_channel_id = self.validate_channel_id(channel_id)
            if not validated_channel_id:
                return False, "❌ معرف القناة غير صحيح"
            
            cursor = self.get_cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO additional_channels 
                (channel_id, channel_username, channel_name, added_by, is_active, channel_type, require_subscription, is_main_channel)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (validated_channel_id, channel_username, channel_name, added_by, True, channel_type, require_subscription, is_main_channel))
            self.conn.commit()
            cursor.close()
            return True, f"✅ تم إضافة القناة بنجاح! المعرف: {validated_channel_id}"
        except Exception as e:
            return False, f"❌ خطأ في إضافة القناة: {e}"

    def get_active_channels(self):
        """الحصول على القنوات النشطة"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT id, channel_id, channel_username, channel_name, added_at, is_active, is_main_channel
                FROM additional_channels 
                WHERE is_active = TRUE
                ORDER BY is_main_channel DESC, added_at DESC
            ''')
            result = cursor.fetchall()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على القنوات: {e}")
            return []

    async def activate_trial_subscription(self, user_id, username, first_name, last_name, context=None):
        """تفعيل فترة تجريبية تلقائية لمدة 48 ساعة"""
        try:
            cursor = self.get_cursor()
            
            # التحقق إذا كان المستخدم قد استخدم الفترة التجريبية مسبقاً
            cursor.execute('''
                SELECT id FROM subscribers 
                WHERE user_id = ? AND trial_used = TRUE
            ''', (user_id,))
            
            if cursor.fetchone():
                return False, "❌ لقد استخدمت الفترة التجريبية مسبقاً ولا يمكنك استخدامها مرة أخرى.", []

            # التحقق إذا كان المستخدم لديه اشتراك فعال حالياً
            cursor.execute('''
                SELECT id FROM subscribers 
                WHERE user_id = ? AND is_active = TRUE
            ''', (user_id,))
            
            if cursor.fetchone():
                return False, "❌ لديك اشتراك فعال حالياً، لا يمكنك استخدام الفترة التجريبية.", []

            # إنشاء كود تجريبي
            trial_code = f"TRIAL_{self.generate_code(8)}"
            trial_duration = 2  # يومين (48 ساعة)
            subscription_expires = datetime.now() + timedelta(days=trial_duration)
            
            # الحصول على القنوات الإضافية (غير الرئيسية)
            additional_channels = self.get_additional_channels_only()
            
            # إنشاء روابط الدعوة للقنوات الإضافية
            invite_links = []
            if context and context.bot:
                try:
                    for channel in additional_channels:
                        try:
                            # إنشاء رابط دعوة قصير لمدة 48 ساعة واستخدام واحد
                            invite_link = await context.bot.create_chat_invite_link(
                                chat_id=channel['id'],
                                expire_date=datetime.now() + timedelta(hours=48),
                                member_limit=1,
                                creates_join_request=False
                            )
                            
                            invite_links.append({
                                'channel_id': channel['id'],
                                'channel_name': channel['name'],
                                'channel_username': channel['username'],
                                'invite_link': invite_link.invite_link
                            })
                            
                            logger.info(f"✅ تم إنشاء رابط دعوة تجريبي للقناة: {channel['name']}")
                            
                        except Exception as e:
                            logger.error(f"❌ خطأ في إنشاء رابط دعوة تجريبي للقناة {channel['id']}: {e}")
                            # إذا فشل إنشاء الرابط، نستخدم رابط القناة المباشر
                            if channel['username']:
                                invite_links.append({
                                    'channel_id': channel['id'],
                                    'channel_name': channel['name'],
                                    'channel_username': channel['username'],
                                    'invite_link': f"https://t.me/{channel['username'].replace('@', '')}"
                                })
                except Exception as e:
                    logger.error(f"❌ خطأ في إنشاء روابط الدعوة التجريبية: {e}")

            # حفظ الاشتراك التجريبي
            cursor.execute('''
                INSERT INTO subscribers 
                (user_id, username, first_name, last_name, code_used, expires_at, is_active, 
                 channels, excluded_channels, apply_to_all_channels, invite_links, is_trial, trial_used)
                VALUES (?, ?, ?, ?, ?, ?, TRUE, ?, ?, ?, ?, TRUE, FALSE)
            ''', (user_id, username, first_name, last_name, trial_code, 
                  subscription_expires.isoformat(), json.dumps([]), json.dumps([]), 
                  True, json.dumps(invite_links)))

            # حفظ الكود التجريبي
            cursor.execute('''
                INSERT INTO codes (code, duration_days, price, created_by, is_trial, is_active)
                VALUES (?, ?, ?, ?, TRUE, FALSE)
            ''', (trial_code, trial_duration, 0.0, "system"))

            self.conn.commit()
            cursor.close()

            message = f"✅ تم تفعيل الفترة التجريبية بنجاح!\n\n🎫 الكود: {trial_code}\n⏰ المدة: 48 ساعة\n📅 ينتهي في: {subscription_expires.strftime('%Y-%m-%d %H:%M')}\n\n⚠️ هذه الفترة تجريبية ولا يمكن استخدامها مرة أخرى"

            return True, message, invite_links

        except Exception as e:
            logger.error(f"❌ خطأ في تفعيل الفترة التجريبية: {e}")
            return False, f"❌ حدث خطأ أثناء تفعيل الفترة التجريبية: {str(e)}", []

    def get_additional_channels_only(self):
        """الحصول على القنوات الإضافية فقط (غير الرئيسية)"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT channel_id, channel_username, channel_name 
                FROM additional_channels 
                WHERE is_active = TRUE AND is_main_channel = FALSE
            ''')
            
            result = cursor.fetchall()
            cursor.close()
            
            channels = []
            for row in result:
                channels.append({
                    'id': row[0],
                    'username': row[1],
                    'name': row[2]
                })
            return channels
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على القنوات الإضافية: {e}")
            return []

    def get_main_channel_info(self):
        """الحصول على معلومات القناة الرئيسية"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT channel_id, channel_username, channel_name 
                FROM additional_channels 
                WHERE is_main_channel = TRUE AND is_active = TRUE
            ''')
            
            result = cursor.fetchone()
            cursor.close()
            
            if result:
                return {
                    'id': result[0],
                    'username': result[1],
                    'name': result[2]
                }
            return None
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على معلومات القناة الرئيسية: {e}")
            return None

    async def use_code(self, code, user_id, username, first_name, last_name, context=None):
        """استخدام كود اشتراك - محدث لاستبعاد القناة الرئيسية من الإخراج"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT id, duration_days, is_used, expires_at, channels, excluded_channels, apply_to_all_channels, max_uses, current_uses, is_trial
                FROM codes 
                WHERE code = ? AND is_used = FALSE AND is_active = TRUE
            ''', (code,))
            
            result = cursor.fetchone()
            
            if not result:
                cursor.close()
                return False, "❌ الكود غير صالح أو منتهي الصلاحية", []

            code_id, duration_days, is_used, expires_at, channels_json, excluded_json, apply_to_all, max_uses, current_uses, is_trial = result

            if expires_at:
                expires_date = datetime.fromisoformat(expires_at)
                if expires_date < datetime.now():
                    cursor.close()
                    return False, "❌ الكود منتهي الصلاحية", []

            if current_uses >= max_uses:
                cursor.execute('UPDATE codes SET is_used = TRUE WHERE id = ?', (code_id,))
                self.conn.commit()
                cursor.close()
                return False, "❌ تم استخدام هذا الكود للعدد الأقصى المسموح", []

            subscription_expires = datetime.now() + timedelta(days=duration_days)
            
            channels = json.loads(channels_json) if channels_json else []
            excluded_channels = json.loads(excluded_json) if excluded_json else []

            cursor.execute('''
                UPDATE codes 
                SET current_uses = current_uses + 1,
                    is_used = CASE WHEN current_uses + 1 >= max_uses THEN TRUE ELSE FALSE END
                WHERE id = ?
            ''', (code_id,))

            # إنشاء روابط الدعوة للقنوات الإضافية فقط
            invite_links = []
            if context and context.bot:
                try:
                    # الحصول على القنوات الإضافية التي يجب إضافة المستخدم إليها
                    target_channels = []
                    if apply_to_all:
                        all_channels = self.get_additional_channels_only()  # القنوات الإضافية فقط
                        for channel in all_channels:
                            channel_info = {
                                'id': channel['id'],
                                'username': channel['username'],
                                'name': channel['name']
                            }
                            if channel_info['id'] not in excluded_channels:
                                target_channels.append(channel_info)
                    else:
                        for channel_id in channels:
                            channel_info = self.get_channel_by_id(channel_id)
                            if channel_info and channel_id != CHANNEL_ID:  # استبعاد القناة الرئيسية
                                target_channels.append(channel_info)
                    
                    # إنشاء روابط دعوة قصيرة لكل قناة إضافية
                    for channel in target_channels:
                        try:
                            # إنشاء رابط دعوة قصير
                            invite_link = await context.bot.create_chat_invite_link(
                                chat_id=channel['id'],
                                expire_date=datetime.now() + timedelta(days=duration_days),
                                member_limit=1,
                                creates_join_request=False
                            )
                            
                            invite_links.append({
                                'channel_id': channel['id'],
                                'channel_name': channel['name'],
                                'channel_username': channel['username'],
                                'invite_link': invite_link.invite_link
                            })
                            
                            logger.info(f"✅ تم إنشاء رابط دعوة للقناة الإضافية: {channel['name']}")
                            
                        except Exception as e:
                            logger.error(f"❌ خطأ في إنشاء رابط دعوة للقناة {channel['id']}: {e}")
                            # إذا فشل إنشاء الرابط، نستخدم رابط القناة المباشر
                            if channel['username']:
                                invite_links.append({
                                    'channel_id': channel['id'],
                                    'channel_name': channel['name'],
                                    'channel_username': channel['username'],
                                    'invite_link': f"https://t.me/{channel['username'].replace('@', '')}"
                                })
                except Exception as e:
                    logger.error(f"❌ خطأ في إنشاء روابط الدعوة: {e}")

            cursor.execute('''
                INSERT OR REPLACE INTO subscribers 
                (user_id, username, first_name, last_name, code_used, expires_at, is_active, channels, excluded_channels, apply_to_all_channels, invite_links, last_notification, is_trial, trial_used)
                VALUES (?, ?, ?, ?, ?, ?, TRUE, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name, code, 
                  subscription_expires.isoformat(), json.dumps(channels), json.dumps(excluded_channels), 
                  apply_to_all, json.dumps(invite_links), None, is_trial, is_trial))

            self.conn.commit()
            cursor.close()

            trial_text = "تجريبية" if is_trial else "عادية"
            message = f"✅ تم تفعيل الاشتراك {trial_text} بنجاح!\n\n🎫 الكود: {code}\n⏰ المدة: {duration_days} يوم\n📅 ينتهي في: {subscription_expires.strftime('%Y-%m-%d %H:%M')}"

            return True, message, invite_links

        except Exception as e:
            logger.error(f"❌ خطأ في استخدام الكود: {e}")
            return False, f"❌ حدث خطأ أثناء تفعيل الكود: {str(e)}", []

    def get_channel_by_id(self, channel_id):
        """الحصول على معلومات القناة بواسطة المعرف"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT channel_id, channel_username, channel_name 
                FROM additional_channels 
                WHERE channel_id = ? AND is_active = TRUE
            ''', (channel_id,))
            
            result = cursor.fetchone()
            cursor.close()
            
            if result:
                return {
                    'id': result[0],
                    'username': result[1],
                    'name': result[2]
                }
            return None
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على معلومات القناة: {e}")
            return None

    def get_subscription_info(self, user_id):
        """الحصول على معلومات اشتراك المستخدم"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT code_used, subscribed_at, expires_at, is_active, invite_links, is_trial
                FROM subscribers 
                WHERE user_id = ?
            ''', (user_id,))
            
            result = cursor.fetchone()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على معلومات الاشتراك: {e}")
            return None

    def get_available_codes(self):
        """الحصول على الأكواد المتاحة"""
        try:
            current_time = datetime.now().isoformat()
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT code, duration_days, price, created_at, expires_at, is_trial
                FROM codes 
                WHERE is_used = FALSE AND is_active = TRUE AND (expires_at IS NULL OR expires_at > ?)
                ORDER BY created_at DESC
            ''', (current_time,))
            
            result = cursor.fetchall()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على الأكواد المتاحة: {e}")
            return []

    def get_all_subscribers(self):
        """الحصول على جميع المشتركين"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                SELECT user_id, username, first_name, last_name, code_used, 
                       subscribed_at, expires_at, is_active, is_trial
                FROM subscribers 
                ORDER BY subscribed_at DESC
            ''')
            
            result = cursor.fetchall()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على المشتركين: {e}")
            return []

    def get_system_stats(self):
        """الحصول على إحصائيات النظام"""
        try:
            cursor = self.get_cursor()
            
            cursor.execute('SELECT COUNT(*) FROM subscribers WHERE is_active = TRUE')
            active_subscribers = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM codes WHERE is_used = FALSE AND is_active = TRUE')
            available_codes = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM codes WHERE is_used = TRUE')
            used_codes = cursor.fetchone()[0]
            
            cursor.execute('SELECT COUNT(*) FROM additional_channels WHERE is_active = TRUE')
            active_channels = cursor.fetchone()[0]
            
            cursor.execute('SELECT SUM(price) FROM codes WHERE is_used = TRUE')
            total_revenue = cursor.fetchone()[0] or 0
            
            cursor.close()
            
            return {
                'active_subscribers': active_subscribers,
                'available_codes': available_codes,
                'used_codes': used_codes,
                'active_channels': active_channels,
                'total_revenue': total_revenue
            }
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على إحصائيات النظام: {e}")
            return {}

    def add_dynamic_button(self, button_text, button_command, button_response, created_by):
        """إضافة زر ديناميكي جديد"""
        try:
            cursor = self.get_cursor()
            cursor.execute('''
                INSERT INTO dynamic_buttons (button_text, button_command, button_response, created_by, is_active)
                VALUES (?, ?, ?, ?, ?)
            ''', (button_text, button_command, button_response, created_by, True))
            self.conn.commit()
            cursor.close()
            return True, "✅ تم إضافة الزر بنجاح"
        except sqlite3.IntegrityError:
            return False, "❌ هذا الأمر موجود مسبقاً"
        except Exception as e:
            return False, f"❌ خطأ في إضافة الزر: {str(e)}"

    def get_dynamic_buttons(self):
        """الحصول على الأزرار الديناميكية"""
        try:
            cursor = self.get_cursor()
            cursor.execute('SELECT * FROM dynamic_buttons WHERE is_active = TRUE ORDER BY created_at DESC')
            result = cursor.fetchall()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على الأزرار: {e}")
            return []

    def get_all_dynamic_buttons(self):
        """الحصول على جميع الأزرار بما فيها المعطلة"""
        try:
            cursor = self.get_cursor()
            cursor.execute('SELECT * FROM dynamic_buttons ORDER BY created_at DESC')
            result = cursor.fetchall()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على الأزرار: {e}")
            return []

    def delete_dynamic_button(self, button_command):
        """حذف زر ديناميكي"""
        try:
            cursor = self.get_cursor()
            cursor.execute('DELETE FROM dynamic_buttons WHERE button_command = ?', (button_command,))
            self.conn.commit()
            cursor.close()
            return True, "✅ تم حذف الزر بنجاح"
        except Exception as e:
            return False, f"❌ خطأ في حذف الزر: {str(e)}"

    def toggle_dynamic_button(self, button_command):
        """تفعيل/تعطيل زر ديناميكي"""
        try:
            cursor = self.get_cursor()
            # الحصول على الحالة الحالية
            cursor.execute('SELECT is_active FROM dynamic_buttons WHERE button_command = ?', (button_command,))
            result = cursor.fetchone()
            
            if not result:
                return False, "❌ الزر غير موجود"
            
            current_state = result[0]
            new_state = not current_state
            
            cursor.execute('UPDATE dynamic_buttons SET is_active = ? WHERE button_command = ?', (new_state, button_command))
            self.conn.commit()
            cursor.close()
            
            status = "مفعل" if new_state else "معطل"
            return True, f"✅ تم {status} الزر بنجاح"
        except Exception as e:
            return False, f"❌ خطأ في تعديل حالة الزر: {str(e)}"

    def edit_dynamic_button(self, button_command, new_text=None, new_response=None):
        """تعديل زر ديناميكي"""
        try:
            cursor = self.get_cursor()
            
            if new_text and new_response:
                cursor.execute('''
                    UPDATE dynamic_buttons 
                    SET button_text = ?, button_response = ? 
                    WHERE button_command = ?
                ''', (new_text, new_response, button_command))
            elif new_text:
                cursor.execute('''
                    UPDATE dynamic_buttons 
                    SET button_text = ? 
                    WHERE button_command = ?
                ''', (new_text, button_command))
            elif new_response:
                cursor.execute('''
                    UPDATE dynamic_buttons 
                    SET button_response = ? 
                    WHERE button_command = ?
                ''', (new_response, button_command))
            
            self.conn.commit()
            cursor.close()
            return True, "✅ تم تعديل الزر بنجاح"
        except Exception as e:
            return False, f"❌ خطأ في تعديل الزر: {str(e)}"

    def get_dynamic_button_by_command(self, button_command):
        """الحصول على معلومات زر بواسطة الأمر"""
        try:
            cursor = self.get_cursor()
            cursor.execute('SELECT * FROM dynamic_buttons WHERE button_command = ?', (button_command,))
            result = cursor.fetchone()
            cursor.close()
            return result
        except Exception as e:
            logger.error(f"❌ خطأ في الحصول على معلومات الزر: {e}")
            return None

    def is_valid_code_format(self, text):
        """التحقق من صحة تنسيق الكود"""
        pattern = r'^[A-Z0-9]{12}$'
        return re.match(pattern, text) is not None

    def find_code_in_text(self, text):
        """البحث عن كود في النص"""
        pattern = r'[A-Z0-9]{12}'
        matches = re.findall(pattern, text.upper())
        
        for match in matches:
            if self.is_valid_code_format(match):
                cursor = self.get_cursor()
                cursor.execute('SELECT id FROM codes WHERE code = ? AND is_used = FALSE AND is_active = TRUE', (match,))
                result = cursor.fetchone() is not None
                cursor.close()
                
                if result:
                    return match
        
        return None

# =============================================
# بوت التلجرام - الإصدار المحدث والمصحح
# =============================================
class TelegramSubscriptionBot:
    def __init__(self, system):
        self.token = BOT_TOKEN
        self.channel_username = CHANNEL_USERNAME
        self.main_admin = MAIN_ADMIN
        self.system = system
        self.application = None
        self.user_data = {}
        
    def setup_handlers(self, application):
        """إعداد معالجات الأوامر - تم التصحيح"""
        self.application = application
        
        # الأوامر الأساسية
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("use", self.use_code_command))
        application.add_handler(CommandHandler("mysubscription", self.my_subscription))
        application.add_handler(CommandHandler("channels", self.list_channels))
        application.add_handler(CommandHandler("trial", self.trial_command))
        application.add_handler(CommandHandler("mainchannel", self.main_channel_command))
        
        # أوامر المشرفين
        application.add_handler(CommandHandler("createcode", self.create_code))
        application.add_handler(CommandHandler("createbatch", self.create_batch))
        application.add_handler(CommandHandler("codes", self.list_codes))
        application.add_handler(CommandHandler("subscribers", self.list_subscribers))
        application.add_handler(CommandHandler("stats", self.show_stats_command))
        application.add_handler(CommandHandler("addadmin", self.add_admin))
        application.add_handler(CommandHandler("removeadmin", self.remove_admin))
        application.add_handler(CommandHandler("admins", self.list_admins))
        application.add_handler(CommandHandler("addchannel", self.add_channel))
        application.add_handler(CommandHandler("channelslist", self.admin_channels_list))
        
        # الأوامر الجديدة للإدارة
        application.add_handler(CommandHandler("checkexpired", self.check_expired_manually))
        application.add_handler(CommandHandler("sendnotifications", self.send_notifications_manually))
        
        # الأمر الجديد لإنشاء عدة أكواد
        application.add_handler(CommandHandler("createmultiple", self.create_multiple_codes))
        
        # نظام الأزرار الديناميكية المتكامل
        application.add_handler(CommandHandler("buttons", self.list_buttons))
        application.add_handler(CommandHandler("addbutton", self.add_button_start))
        application.add_handler(CommandHandler("deletebutton", self.delete_button_start))
        application.add_handler(CommandHandler("editbutton", self.edit_button_start))
        application.add_handler(CommandHandler("togglebutton", self.toggle_button))
        
        # معالجات المحادثة للأزرار
        conversation_handler = ConversationHandler(
            entry_points=[
                CommandHandler('addbutton', self.add_button_start),
                CommandHandler('deletebutton', self.delete_button_start),
                CommandHandler('editbutton', self.edit_button_start)
            ],
            states={
                WAITING_FOR_BUTTON_TEXT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_button_text)
                ],
                WAITING_FOR_BUTTON_COMMAND: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_button_command)
                ],
                WAITING_FOR_BUTTON_RESPONSE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_button_response)
                ],
                WAITING_FOR_BUTTON_DELETE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.delete_button_confirm)
                ],
                WAITING_FOR_BUTTON_EDIT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_button_choice)
                ]
            },
            fallbacks=[CommandHandler('cancel', self.cancel_operation)],
            allow_reentry=True
        )
        
        application.add_handler(conversation_handler)
        
        # معالجات الاستعلامات
        application.add_handler(CallbackQueryHandler(self.handle_callback))
        
        # معالج الرسائل العامة
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        
        # معالج الأخطاء
        application.add_error_handler(self.error_handler)
        
        # إعداد معالجات الأزرار الديناميكية
        self.setup_dynamic_handlers(application)

    def setup_dynamic_handlers(self, application):
        """إعداد معالجات الأزرار الديناميكية - تم التصحيح"""
        try:
            buttons = self.system.get_dynamic_buttons()
            for button in buttons:
                command = button[2]  # button_command
                # إضافة معالج لكل زر ديناميكي
                application.add_handler(CommandHandler(command, self.handle_dynamic_command))
        except Exception as e:
            logger.error(f"❌ خطأ في إعداد معالجات الأزرار الديناميكية: {e}")

    async def handle_dynamic_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة الأوامر الديناميكية"""
        try:
            command = update.message.text.split()[0][1:]  # إزالة /
            button = None
            
            buttons = self.system.get_dynamic_buttons()
            for btn in buttons:
                if btn[2] == command:
                    button = btn
                    break
            
            if button:
                # إضافة زر الرجوع إلى القائمة الرئيسية
                keyboard = [
                    [InlineKeyboardButton("🔙 الرجوع إلى القائمة الرئيسية", callback_data="main_back")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await update.message.reply_text(button[3], reply_markup=reply_markup)
            else:
                await update.message.reply_text("❌ هذا الأمر غير متوفر حالياً")
                
        except Exception as e:
            logger.error(f"❌ خطأ في معالجة الأمر الديناميكي: {e}")
            await update.message.reply_text("❌ حدث خطأ في معالجة الأمر")

    # نظام إدارة الأزرار الديناميكية - البدء
    async def add_button_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء عملية إضافة زر جديد"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return ConversationHandler.END
        
        await update.message.reply_text(
            "🎯 **إضافة زر ديناميكي جديد**\n\n"
            "أرسل نص الزر الذي سيظهر للمستخدمين:\n\n"
            "💡 مثال: \"📞 تواصل مع الإدارة\""
        )
        return WAITING_FOR_BUTTON_TEXT

    async def add_button_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """استقبال نص الزر"""
        context.user_data['button_text'] = update.message.text
        
        await update.message.reply_text(
            "✅ **تم حفظ نص الزر**\n\n"
            "الآن أرسل الأمر الذي سيتم استخدامه للزر (بدون /):\n\n"
            "💡 مثال: \"contact\"\n"
            "⚠️ يجب أن يكون الأمر بالإنجليزية فقط ولا يحتوي على مسافات"
        )
        return WAITING_FOR_BUTTON_COMMAND

    async def add_button_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """استقبال أمر الزر"""
        command = update.message.text.strip().lower()
        
        # التحقق من صحة الأمر
        if not re.match(r'^[a-z0-9_]+$', command):
            await update.message.reply_text(
                "❌ **أمر غير صالح**\n\n"
                "يجب أن يحتوي الأمر على:\n"
                "• أحرف إنجليزية صغيرة فقط\n"
                "• أرقام\n"
                "• شرطة سفلية (_)\n"
                "• بدون مسافات أو رموز خاصة\n\n"
                "أعد إرسال الأمر:"
            )
            return WAITING_FOR_BUTTON_COMMAND
        
        # التحقق من عدم وجود الأمر مسبقاً
        existing_button = self.system.get_dynamic_button_by_command(command)
        if existing_button:
            await update.message.reply_text(
                "❌ **هذا الأمر موجود مسبقاً**\n\n"
                "الرجاء اختيار أمر آخر:"
            )
            return WAITING_FOR_BUTTON_COMMAND
        
        context.user_data['button_command'] = command
        
        await update.message.reply_text(
            "✅ **تم حفظ أمر الزر**\n\n"
            "الآن أرسل الرد الذي سيظهر عندما يضغط المستخدم على الزر:\n\n"
            "💡 يمكنك استخدام تنسيق HTML البسيط مثل:\n"
            "<b>نص عريض</b>\n"
            "<i>نص مائل</i>\n"
            "<code>نص كود</code>"
        )
        return WAITING_FOR_BUTTON_RESPONSE

    async def add_button_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """استقبال رد الزر وإضافته"""
        button_text = context.user_data['button_text']
        button_command = context.user_data['button_command']
        button_response = update.message.text
        
        success, message = self.system.add_dynamic_button(
            button_text, button_command, button_response, update.effective_user.id
        )
        
        if success:
            # إعادة تحميل المعالجات لتشمل الزر الجديد
            self.setup_dynamic_handlers(self.application)
            
            keyboard = [
                [InlineKeyboardButton("🔙 الرجوع إلى القائمة الرئيسية", callback_data="main_back")],
                [InlineKeyboardButton("🎯 إدارة الأزرار", callback_data="admin_manage_buttons")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ **تم إضافة الزر بنجاح**\n\n"
                f"📝 النص: {button_text}\n"
                f"⚡ الأمر: /{button_command}\n"
                f"📨 الرد: {button_response[:50]}...\n\n"
                f"يمكنك الآن استخدام الأمر /{button_command} في أي مكان",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(f"❌ {message}")
        
        # تنظيف البيانات المؤقتة
        context.user_data.clear()
        return ConversationHandler.END

    async def delete_button_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء عملية حذف زر"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return ConversationHandler.END
        
        buttons = self.system.get_all_dynamic_buttons()
        
        if not buttons:
            await update.message.reply_text("📭 لا توجد أزرار متاحة للحذف")
            return ConversationHandler.END
        
        # عرض قائمة الأزرار المتاحة للحذف
        text = "🗑️ **حذف زر ديناميكي**\n\n"
        text += "الأزرار المتاحة:\n\n"
        
        for i, button in enumerate(buttons, 1):
            status = "🟢" if button[4] else "🔴"
            text += f"{i}. {status} /{button[2]} - {button[1]}\n"
        
        text += "\nأرسل الأمر الذي تريد حذفه (بدون /):"
        
        await update.message.reply_text(text)
        return WAITING_FOR_BUTTON_DELETE

    async def delete_button_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """تأكيد حذف الزر"""
        command = update.message.text.strip().lower()
        
        # التحقق من وجود الزر
        button = self.system.get_dynamic_button_by_command(command)
        if not button:
            await update.message.reply_text(
                "❌ **الزر غير موجود**\n\n"
                "الرجاء إرسال أمر صحيح:"
            )
            return WAITING_FOR_BUTTON_DELETE
        
        # حذف الزر
        success, message = self.system.delete_dynamic_button(command)
        
        if success:
            # إعادة تحميل المعالجات
            self.setup_dynamic_handlers(self.application)
            
            keyboard = [
                [InlineKeyboardButton("🔙 الرجوع إلى القائمة الرئيسية", callback_data="main_back")],
                [InlineKeyboardButton("🎯 إدارة الأزرار", callback_data="admin_manage_buttons")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"✅ **تم حذف الزر بنجاح**\n\n"
                f"الأمر: /{command}\n"
                f"النص: {button[1]}",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text(f"❌ {message}")
        
        return ConversationHandler.END

    async def edit_button_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء عملية تعديل زر"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return ConversationHandler.END
        
        buttons = self.system.get_all_dynamic_buttons()
        
        if not buttons:
            await update.message.reply_text("📭 لا توجد أزرار متاحة للتعديل")
            return ConversationHandler.END
        
        # عرض قائمة الأزرار المتاحة للتعديل
        text = "✏️ **تعديل زر ديناميكي**\n\n"
        text += "الأزرار المتاحة:\n\n"
        
        for i, button in enumerate(buttons, 1):
            status = "🟢" if button[4] else "🔴"
            text += f"{i}. {status} /{button[2]} - {button[1]}\n"
        
        text += "\nأرسل الأمر الذي تريد تعديله (بدون /):"
        
        await update.message.reply_text(text)
        return WAITING_FOR_BUTTON_EDIT

    async def edit_button_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """اختيار ما سيتم تعديله في الزر"""
        command = update.message.text.strip().lower()
        
        # التحقق من وجود الزر
        button = self.system.get_dynamic_button_by_command(command)
        if not button:
            await update.message.reply_text(
                "❌ **الزر غير موجود**\n\n"
                "الرجاء إرسال أمر صحيح:"
            )
            return WAITING_FOR_BUTTON_EDIT
        
        context.user_data['edit_command'] = command
        
        keyboard = [
            [InlineKeyboardButton("📝 تعديل النص", callback_data=f"edit_text_{command}")],
            [InlineKeyboardButton("📨 تعديل الرد", callback_data=f"edit_response_{command}")],
            [InlineKeyboardButton("📝📨 تعديل النص والرد", callback_data=f"edit_both_{command}")],
            [InlineKeyboardButton("🔙 إلغاء", callback_data="admin_manage_buttons")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"✏️ **تعديل الزر:** /{command}\n\n"
            f"📝 النص الحالي: {button[1]}\n"
            f"📨 الرد الحالي: {button[3][:50]}...\n\n"
            f"اختر ما تريد تعديله:",
            reply_markup=reply_markup
        )
        
        return ConversationHandler.END

    async def toggle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """تفعيل/تعطيل زر"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        if not context.args:
            await update.message.reply_text(
                "❌ يرجى إدخال أمر الزر\n"
                "استخدم: /togglebutton [أمر_الزر]\n\n"
                "💡 مثال: /togglebutton contact"
            )
            return
        
        command = context.args[0].lower()
        success, message = self.system.toggle_dynamic_button(command)
        
        if success:
            # إعادة تحميل المعالجات
            self.setup_dynamic_handlers(self.application)
        
        await update.message.reply_text(message)

    async def list_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض جميع الأزرار الديناميكية"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        buttons = self.system.get_all_dynamic_buttons()
        
        if not buttons:
            await update.message.reply_text("📭 لا توجد أزرار ديناميكية مضافة")
            return
        
        text = "🎯 **جميع الأزرار الديناميكية**\n\n"
        
        active_count = 0
        inactive_count = 0
        
        for i, button in enumerate(buttons, 1):
            status = "🟢 مفعل" if button[4] else "🔴 معطل"
            if button[4]:
                active_count += 1
            else:
                inactive_count += 1
                
            text += f"{i}. {status}\n"
            text += f"   📝 النص: {button[1]}\n"
            text += f"   ⚡ الأمر: /{button[2]}\n"
            text += f"   📨 الرد: {button[3][:30]}...\n"
            text += f"   📅 الإضافة: {button[6].split()[0]}\n\n"
        
        text += f"📊 الإحصائيات:\n"
        text += f"• 🟢 الأزرار المفعلة: {active_count}\n"
        text += f"• 🔴 الأزرار المعطلة: {inactive_count}\n"
        text += f"• 📋 المجموع: {len(buttons)}\n\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ إضافة زر", callback_data="admin_add_button")],
            [InlineKeyboardButton("🗑️ حذف زر", callback_data="admin_delete_button")],
            [InlineKeyboardButton("✏️ تعديل زر", callback_data="admin_edit_button")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(text, reply_markup=reply_markup)

    async def cancel_operation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إلغاء العملية الحالية"""
        context.user_data.clear()
        await update.message.reply_text("❌ تم إلغاء العملية")
        return ConversationHandler.END

    async def trial_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """تفعيل الفترة التجريبية"""
        user = update.effective_user
        
        processing_msg = await update.message.reply_text("⏳ جاري تفعيل الفترة التجريبية...")
        
        success, message, invite_links = await self.system.activate_trial_subscription(
            user.id, user.username, user.first_name, user.last_name, context
        )
        
        await processing_msg.delete()
        await update.message.reply_text(message)
        
        # إرسال روابط الدعوة للقنوات الإضافية إذا كانت موجودة
        if success and invite_links:
            links_text = "🔗 روابط الانضمام إلى القنوات المميزة:\n\n"
            for link_info in invite_links:
                links_text += f"📢 {link_info['channel_name']}\n{link_info['invite_link']}\n\n"
            
            links_text += "⚠️ ملاحظة: هذه الروابط صالحة لمدة 48 ساعة فقط"
            
            await update.message.reply_text(links_text)

    async def main_channel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """رابط القناة الرئيسية"""
        main_channel = self.system.get_main_channel_info()
        
        if not main_channel:
            await update.message.reply_text("❌ لم يتم العثور على معلومات القناة الرئيسية")
            return
        
        channel_link = f"https://t.me/{main_channel['username'].replace('@', '')}" if main_channel['username'] else f"https://t.me/c/{main_channel['id'].replace('-100', '')}"
        
        text = f"""
📢 **القناة الرئيسية**

{main_channel['name']}

🔗 الرابط: {channel_link}

👉 انضم الآن للبقاء على اطلاع بآخر المستجدات!
        """
        
        keyboard = [
            [InlineKeyboardButton("📢 انضم للقناة الرئيسية", url=channel_link)],
            [InlineKeyboardButton("🔙 الرجوع للقائمة", callback_data="main_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(text, reply_markup=reply_markup)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """بدء المحادثة مع الأزرار الجديدة"""
        user = update.effective_user
        
        # الحصول على الأزرار الديناميكية النشطة
        dynamic_buttons = self.system.get_dynamic_buttons()
        
        keyboard = [
            [InlineKeyboardButton("🎫 تفعيل كود اشتراك", callback_data="user_activate_code")],
            [InlineKeyboardButton("🆓 فترة تجريبية مجانية", callback_data="user_trial")],
            [InlineKeyboardButton("📢 القناة الرئيسية", callback_data="user_main_channel")],
            [InlineKeyboardButton("📋 معلومات اشتراكي", callback_data="user_my_subscription")],
            [InlineKeyboardButton("🛠️ الأوامر المتاحة", callback_data="main_help")]
        ]
        
        # إضافة الأزرار الديناميكية
        for button in dynamic_buttons:
            keyboard.append([InlineKeyboardButton(button[1], callback_data=f"dynamic_{button[2]}")])
        
        if self.system.is_admin(user.id):
            keyboard.append([InlineKeyboardButton("👑 لوحة المشرفين", callback_data="admin_dashboard")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = f"""
مرحباً {user.first_name}!

أهلاً بك في صالة روبوت سباكس 🤖

مميزات النظام الاحترافية:
🧠 تحليل بالذكاء الاصطناعي
⚡ تنفيذ فوري للإشارات
📊 مراقبة مستمرة من خبراء التحليل
🎯 دقة عالية في الأداء
🔄 تحديث وتطوير مستمر
📈 تحليلات تنبؤية متقدمة
✨ مؤشرات حصرية للمشتركين
🎓 دورات تدريبية متخصصة
🔒 أداء ثابت وموثوق
🤖 تداول آلي بالكامل
👨‍💼 إشراف مباشر من محترفين

انظم إلى قناتنا الرئيسية:
📢 @SPX53

لطلب الدعم او الاشتراك:
📩 @SPX_47

أو من خلال موقعنا الإلكتروني:
🌐 جاري التنفيذ 🚧

اختر من الخيارات أدناه للبدء:
        """
        
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة استعلامات الأزرار مع الإضافات الجديدة"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user = query.from_user
        
        try:
            if data == "user_activate_code":
                await self.activate_code_menu(query, context)
            elif data == "user_trial":
                await self.activate_trial_callback(query, context)
            elif data == "user_main_channel":
                await self.show_main_channel_callback(query, context)
            elif data == "user_my_subscription":
                await self.show_user_subscription(query, context)
            elif data == "user_list_channels":
                await self.show_available_channels(query, context)
            elif data == "main_help":
                await self.show_help_menu(query, context)
            elif data == "admin_dashboard":
                if self.system.is_admin(user.id):
                    await self.show_admin_dashboard(query, context)
                else:
                    await query.edit_message_text("❌ ليس لديك صلاحية الوصول إلى لوحة المشرفين")
            elif data == "admin_stats":
                if self.system.is_admin(user.id):
                    await self.show_detailed_stats(query, context)
                else:
                    await query.edit_message_text("❌ ليس لديك صلاحية الوصول إلى الإحصائيات")
            elif data == "admin_create_code":
                if self.system.is_admin(user.id):
                    await self.show_create_code_menu(query, context)
            elif data == "admin_list_codes":
                if self.system.is_admin(user.id):
                    await self.show_codes_list(query, context)
            elif data == "admin_list_subs":
                if self.system.is_admin(user.id):
                    await self.show_subscribers_list(query, context)
            elif data == "admin_manage_channels":
                if self.system.is_admin(user.id):
                    await self.show_channels_management(query, context)
            elif data == "admin_check_expired":
                if self.system.is_admin(user.id):
                    await self.check_expired_manually_callback(query, context)
            elif data == "admin_send_notifications":
                if self.system.is_admin(user.id):
                    await self.send_notifications_manually_callback(query, context)
            elif data == "admin_create_multiple":
                if self.system.is_admin(user.id):
                    await self.create_multiple_codes_callback(query, context)
            elif data == "admin_manage_buttons":
                if self.system.is_admin(user.id):
                    await self.show_buttons_management(query, context)
            elif data == "admin_add_button":
                if self.system.is_admin(user.id):
                    await self.add_button_start_callback(query, context)
            elif data == "admin_delete_button":
                if self.system.is_admin(user.id):
                    await self.delete_button_start_callback(query, context)
            elif data == "admin_edit_button":
                if self.system.is_admin(user.id):
                    await self.edit_button_start_callback(query, context)
            elif data.startswith("edit_text_") or data.startswith("edit_response_") or data.startswith("edit_both_"):
                if self.system.is_admin(user.id):
                    await self.handle_edit_callback(query, context, data)
            elif data == "main_back":
                await self.show_main_menu(query, context)
            elif data.startswith("dynamic_"):
                command = data.replace("dynamic_", "")
                button = self.system.get_dynamic_button_by_command(command)
                if button:
                    keyboard = [
                        [InlineKeyboardButton("🔙 الرجوع إلى القائمة الرئيسية", callback_data="main_back")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(button[3], reply_markup=reply_markup)
                
        except Exception as e:
            logger.error(f"❌ خطأ في معالجة الاستعلام: {e}")
            await query.edit_message_text("❌ حدث خطأ في معالجة الطلب")

    async def show_buttons_management(self, query, context):
        """عرض لوحة إدارة الأزرار"""
        buttons = self.system.get_all_dynamic_buttons()
        
        active_count = len([b for b in buttons if b[4]])
        inactive_count = len([b for b in buttons if not b[4]])
        
        text = f"""
🎯 **إدارة الأزرار الديناميكية**

📊 الإحصائيات:
• 🟢 الأزرار المفعلة: {active_count}
• 🔴 الأزرار المعطلة: {inactive_count}
• 📋 المجموع: {len(buttons)}

🛠️ الأدوات المتاحة:
        """
        
        keyboard = [
            [InlineKeyboardButton("➕ إضافة زر جديد", callback_data="admin_add_button")],
            [InlineKeyboardButton("📋 عرض جميع الأزرار", callback_data="admin_list_buttons")],
            [InlineKeyboardButton("🗑️ حذف زر", callback_data="admin_delete_button")],
            [InlineKeyboardButton("✏️ تعديل زر", callback_data="admin_edit_button")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def add_button_start_callback(self, query, context):
        """بدء إضافة زر من خلال الاستعلام"""
        await query.edit_message_text(
            "🎯 **إضافة زر ديناميكي جديد**\n\n"
            "أرسل نص الزر الذي سيظهر للمستخدمين:\n\n"
            "💡 مثال: \"📞 تواصل مع الإدارة\"\n\n"
            "أو اضغط إلغاء للرجوع."
        )

    async def delete_button_start_callback(self, query, context):
        """بدء حذف زر من خلال الاستعلام"""
        buttons = self.system.get_all_dynamic_buttons()
        
        if not buttons:
            await query.edit_message_text("📭 لا توجد أزرار متاحة للحذف")
            return
        
        text = "🗑️ **حذف زر ديناميكي**\n\n"
        text += "الأزرار المتاحة:\n\n"
        
        for i, button in enumerate(buttons, 1):
            status = "🟢" if button[4] else "🔴"
            text += f"{i}. {status} /{button[2]} - {button[1]}\n"
        
        text += "\nأرسل الأمر الذي تريد حذفه (بدون /) في المحادثة."
        
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage_buttons")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def edit_button_start_callback(self, query, context):
        """بديل لتعديل زر من خلال الاستعلام"""
        buttons = self.system.get_all_dynamic_buttons()
        
        if not buttons:
            await query.edit_message_text("📭 لا توجد أزرار متاحة للتعديل")
            return
        
        text = "✏️ **تعديل زر ديناميكي**\n\n"
        text += "الأزرار المتاحة:\n\n"
        
        for i, button in enumerate(buttons, 1):
            status = "🟢" if button[4] else "🔴"
            text += f"{i}. {status} /{button[2]} - {button[1]}\n"
        
        text += "\nأرسل الأمر الذي تريد تعديله (بدون /) في المحادثة."
        
        keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage_buttons")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def handle_edit_callback(self, query, context, data):
        """معالجة استعلامات التعديل"""
        parts = data.split('_')
        action = parts[1]  # text, response, both
        command = parts[2]  # command name
        
        context.user_data['edit_command'] = command
        context.user_data['edit_action'] = action
        
        if action == 'text':
            await query.edit_message_text(
                f"✏️ **تعديل نص الزر:** /{command}\n\n"
                f"أرسل النص الجديد للزر:"
            )
        elif action == 'response':
            await query.edit_message_text(
                f"✏️ **تعديل رد الزر:** /{command}\n\n"
                f"أرسل الرد الجديد للزر:"
            )
        elif action == 'both':
            await query.edit_message_text(
                f"✏️ **تعديل النص والرد للزر:** /{command}\n\n"
                f"أرسل النص الجديد للزر:"
            )

    async def activate_trial_callback(self, query, context):
        """تفعيل الفترة التجريبية من خلال الاستعلام"""
        user = query.from_user
        
        await query.edit_message_text("⏳ جاري تفعيل الفترة التجريبية...")
        
        success, message, invite_links = await self.system.activate_trial_subscription(
            user.id, user.username, user.first_name, user.last_name, context
        )
        
        if success:
            # إرسال روابط الدعوة للقنوات الإضافية إذا كانت موجودة
            if invite_links:
                links_text = "🔗 روابط الانضمام إلى القنوات المميزة:\n\n"
                for link_info in invite_links:
                    links_text += f"📢 {link_info['channel_name']}\n{link_info['invite_link']}\n\n"
                
                links_text += "⚠️ ملاحظة: هذه الروابط صالحة لمدة 48 ساعة فقط"
                
                await query.edit_message_text(f"{message}\n\n{links_text}")
            else:
                await query.edit_message_text(message)
        else:
            await query.edit_message_text(message)

    async def show_main_channel_callback(self, query, context):
        """عرض رابط القناة الرئيسية من خلال الاستعلام"""
        main_channel = self.system.get_main_channel_info()
        
        if not main_channel:
            await query.edit_message_text("❌ لم يتم العثور على معلومات القناة الرئيسية")
            return
        
        channel_link = f"https://t.me/{main_channel['username'].replace('@', '')}" if main_channel['username'] else f"https://t.me/c/{main_channel['id'].replace('-100', '')}"
        
        text = f"""
📢 **القناة الرئيسية**

{main_channel['name']}

🔗 الرابط: {channel_link}

👉 انضم الآن للبقاء على اطلاع بآخر المستجدات!

💡 ملاحظة: القناة الرئيسية مجانية ومتاحة للجميع، وستبقى عضويتك فيها دائماً حتى بعد انتهاء الاشتراكات.
        """
        
        keyboard = [
            [InlineKeyboardButton("📢 انضم للقناة الرئيسية", url=channel_link)],
            [InlineKeyboardButton("🆓 فترة تجريبية", callback_data="user_trial")],
            [InlineKeyboardButton("🔙 الرجوع للقائمة", callback_data="main_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_main_menu(self, query, context):
        """عرض القائمة الرئيسية"""
        user = query.from_user
        
        # الحصول على الأزرار الديناميكية النشطة
        dynamic_buttons = self.system.get_dynamic_buttons()
        
        keyboard = [
            [InlineKeyboardButton("🎫 تفعيل كود اشتراك", callback_data="user_activate_code")],
            [InlineKeyboardButton("🆓 فترة تجريبية مجانية", callback_data="user_trial")],
            [InlineKeyboardButton("📢 القناة الرئيسية", callback_data="user_main_channel")],
            [InlineKeyboardButton("📋 معلومات اشتراكي", callback_data="user_my_subscription")],
            [InlineKeyboardButton("🛠️ الأوامر المتاحة", callback_data="main_help")]
        ]
        
        # إضافة الأزرار الديناميكية
        for button in dynamic_buttons:
            keyboard.append([InlineKeyboardButton(button[1], callback_data=f"dynamic_{button[2]}")])
        
        if self.system.is_admin(user.id):
            keyboard.append([InlineKeyboardButton("👑 لوحة المشرفين", callback_data="admin_dashboard")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = f"""
القائمة الرئيسية

مرحباً {user.first_name}!

اختر من الخيارات:
        """
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def activate_code_menu(self, query, context):
        """قائمة تفعيل الكود"""
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = """
🎫 تفعيل كود اشتراك

يمكنك تفعيل الاشتراك بطريقتين:

1. استخدم الأمر:
   /use [الكود]

2. أو أرسل الكود مباشرة:
   فقط اكتب الكود وأرسله

💡 مثال: ABCDEF123456

✅ بعد التفعيل، سيتم إرسال روابط الانضمام إلى القنوات لك تلقائياً.
        """
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_user_subscription(self, query, context):
        """عرض معلومات اشتراك المستخدم"""
        user = query.from_user
        subscription_info = self.system.get_subscription_info(user.id)
        
        if not subscription_info or not subscription_info[3]:  # is_active
            keyboard = [
                [InlineKeyboardButton("🎫 تفعيل كود", callback_data="user_activate_code")],
                [InlineKeyboardButton("🆓 فترة تجريبية", callback_data="user_trial")],
                [InlineKeyboardButton("🔙 رجوع", callback_data="main_back")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            text = """
❌ لا يوجد اشتراك فعال

للاستفادة من خدماتنا:
1. احصل على كود اشتراك من الإدارة
2. أو استخدم الفترة التجريبية المجانية
3. أرسل الكود مباشرة أو استخدم الأمر /use

💡 يمكنك إرسال الكود مباشرة دون استخدام الأمر
            """
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        
        code_used, subscribed_at, expires_at, is_active, invite_links_json, is_trial = subscription_info
        
        trial_text = "تجريبية" if is_trial else "عادية"
        
        text = f"""
📋 معلومات اشتراكك

🎫 الكود المستخدم: {code_used}
📅 تاريخ البدء: {subscribed_at.split()[0]}
⏰ تاريخ الانتهاء: {expires_at.split()[0]}
🔰 الحالة: {'🟢 نشط' if is_active else '🔴 منتهي'}
🎯 النوع: {trial_text}
        """
        
        # عرض روابط الدعوة إذا كانت موجودة
        if invite_links_json:
            invite_links = json.loads(invite_links_json)
            if invite_links:
                text += "\n🔗 روابط القنوات:\n"
                for link_info in invite_links:
                    text += f"• {link_info['channel_name']}: {link_info['invite_link']}\n"
        
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_available_channels(self, query, context):
        """عرض القنوات المتاحة"""
        channels = self.system.get_active_channels()
        
        if not channels:
            text = "📭 لا توجد قنوات متاحة حالياً"
        else:
            text = "📢 القنوات المتاحة:\n\n"
            for i, channel in enumerate(channels, 1):
                channel_id, username, name, added_at, is_active, is_main_channel = channel[1:7]
                main_text = " (رئيسية)" if is_main_channel else ""
                text += f"{i}. {name}{main_text}"
                if username:
                    text += f" - {username}"
                text += "\n"
        
        keyboard = [
            [InlineKeyboardButton("🎫 تفعيل كود", callback_data="user_activate_code")],
            [InlineKeyboardButton("🆓 فترة تجريبية", callback_data="user_trial")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_help_menu(self, query, context):
        """عرض قائمة المساعدة"""
        user = query.from_user
        
        text = """
🛠️ أوامر البوت المتاحة:

👤 للمستخدمين:
• /start - بدء استخدام البوت
• /use [الكود] - تفعيل كود اشتراك
• /trial - تفعيل فترة تجريبية مجانية (48 ساعة)
• /mainchannel - رابط القناة الرئيسية
• /mysubscription - عرض معلومات الاشتراك
• /channels - عرض القنوات المتاحة
• /help - عرض هذه الرسالة

🎯 **الميزةوات الجديدة:**
• 🆓 فترة تجريبية مجانية لمدة 48 ساعة
• 📢 البقاء في القناة الرئيسية دائماً
• 🔗 إخراج من القنوات المميزة فقط عند انتهاء الاشتراك

💡 ملاحظة: يمكنك إرسال الكود مباشرة دون استخدام الأمر /use
        """
        
        if self.system.is_admin(user.id):
            text += """

👑 للمشرفين:
• /createcode [المدة] [السعر] - إنشاء كود جديد
• /createmultiple [العدد] [المدة] [السعر] - إنشاء عدة أكواد دفعة واحدة
• /createbatch [عدد] [مدة] [سعر] - إنشاء دفعة أكواد
• /codes - عرض الأكواد المتاحة
• /subscribers - عرض المشتركين
• /stats - إحصائيات النظام
• /addadmin [معرف] - إضافة مشرف
• /admins - عرض المشرفين
• /addchannel [معرف] [معرف_عام] [اسم] - إضافة قناة
• /channelslist - عرض القنوات
• /buttons - عرض الأزرار
• /addbutton [نص] [أمر] [رد] - إضافة زر
• /checkexpired - التحقق من الاشتراكات المنتهية يدوياً
• /sendnotifications - إرسال التنبيهات يدوياً
            """
        
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_admin_dashboard(self, query, context):
        """لوحة تحكم المشرفين"""
        stats = self.system.get_system_stats()
        
        text = f"""
👑 لوحة تحكم المشرفين

📊 الإحصائيات:
• 👥 المشتركين النشطين: {stats.get('active_subscribers', 0)}
• 🎫 الأكواد المتاحة: {stats.get('available_codes', 0)}
• ✅ الأكواد المستخدمة: {stats.get('used_codes', 0)}
• 📢 القنوات النشطة: {stats.get('active_channels', 0)}
• 💰 الإيرادات: ${stats.get('total_revenue', 0):.2f}

🛠️ أدوات الإدارة:
        """
        
        keyboard = [
            [
                InlineKeyboardButton("🎫 إنشاء كود", callback_data="admin_create_code"),
                InlineKeyboardButton("📦 إنشاء دفعة", callback_data="admin_create_batch")
            ],
            [
                InlineKeyboardButton("🎫 إنشاء عدة أكواد", callback_data="admin_create_multiple"),
                InlineKeyboardButton("📋 الأكواد", callback_data="admin_list_codes")
            ],
            [
                InlineKeyboardButton("👥 المشتركين", callback_data="admin_list_subs"),
                InlineKeyboardButton("📢 إدارة القنوات", callback_data="admin_manage_channels")
            ],
            [
                InlineKeyboardButton("🎯 إدارة الأزرار", callback_data="admin_manage_buttons"),
                InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")
            ],
            [
                InlineKeyboardButton("🔄 فحص المنتهية", callback_data="admin_check_expired"),
                InlineKeyboardButton("🔔 إرسال تنبيهات", callback_data="admin_send_notifications")
            ],
            [InlineKeyboardButton("🔙 رجوع", callback_data="main_back")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_detailed_stats(self, query, context):
        """عرض إحصائيات مفصلة"""
        stats = self.system.get_system_stats()
        
        text = f"""
📊 إحصائيات النظام التفصيلية

👥 المشتركين:
• 🟢 النشطين: {stats.get('active_subscribers', 0)}

🎫 الأكواد:
• 📭 المتاحة: {stats.get('available_codes', 0)}
• ✅ المستخدمة: {stats.get('used_codes', 0)}

📢 القنوات:
• 🔗 النشطة: {stats.get('active_channels', 0)}

💰 المالية:
• 💵 الإيرادات: ${stats.get('total_revenue', 0):.2f}

🔄 آخر تحديث: {datetime.now().strftime('%Y-%m-%d %H:%M')}
        """
        
        keyboard = [
            [InlineKeyboardButton("🔄 تحديث", callback_data="admin_stats")],
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_create_code_menu(self, query, context):
        """عرض قائمة إنشاء كود"""
        text = """
🎫 إنشاء كود جديد

استخدم الأمر:
/createcode [المدة] [السعر]

مثال:
/createcode 30 10.00
        """
        
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_codes_list(self, query, context):
        """عرض قائمة الأكواد"""
        codes = self.system.get_available_codes()
        
        if not codes:
            text = "📭 لا توجد أكواد متاحة حالياً"
        else:
            text = "🎫 الأكواد المتاحة:\n\n"
            for i, code in enumerate(codes[:10], 1):
                code_text, duration, price, created_at, expires_at, is_trial = code
                trial_text = " (تجريبي)" if is_trial else ""
                text += f"{i}. {code_text} - {duration} يوم - ${price:.2f}{trial_text}\n"
        
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_subscribers_list(self, query, context):
        """عرض قائمة المشتركين"""
        subscribers = self.system.get_all_subscribers()
        
        if not subscribers:
            text = "📭 لا توجد مشتركين حالياً"
        else:
            text = "👥 قائمة المشتركين:\n\n"
            for i, sub in enumerate(subscribers[:10], 1):
                user_id, username, first_name, last_name, code_used, subscribed_at, expires_at, is_active, is_trial = sub
                name = f"{first_name} {last_name}" if first_name and last_name else username
                status = "🟢" if is_active else "🔴"
                trial_text = " (تجريبي)" if is_trial else ""
                text += f"{i}. {status} {name}{trial_text}\n"
        
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def show_channels_management(self, query, context):
        """عرض إدارة القنوات"""
        channels = self.system.get_active_channels()
        
        text = "📢 إدارة القنوات\n\n"
        text += "استخدم الأمر:\n"
        text += "/addchannel [معرف] [معرف_عام] [اسم]\n\n"
        text += "مثال:\n"
        text += "/addchannel -100123456789 @channel_name اسم_القناة\n\n"
        text += f"عدد القنوات النشطة: {len(channels)}"
        
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def check_expired_manually_callback(self, query, context):
        """التحقق من الاشتراكات المنتهية من خلال الاستعلام"""
        await query.edit_message_text("🔄 جاري التحقق من الاشتراكات المنتهية...")
        try:
            await self.system.check_expired_subscriptions_async()
            await query.edit_message_text("✅ تم الانتهاء من فحص الاشتراكات المنتهية")
        except Exception as e:
            await query.edit_message_text(f"❌ حدث خطأ أثناء الفحص: {e}")

    async def send_notifications_manually_callback(self, query, context):
        """إرسال التنبيهات من خلال الاستعلام"""
        await query.edit_message_text("🔄 جاري إرسال التنبيهات...")
        try:
            await self.system.send_expiry_notifications_async()
            await query.edit_message_text("✅ تم إرسال التنبيهات بنجاح")
        except Exception as e:
            await query.edit_message_text(f"❌ حدث خطأ أثناء إرسال التنبيهات: {e}")

    async def create_multiple_codes_callback(self, query, context):
        """بدء إنشاء عدة أكواد من خلال الاستعلام"""
        text = """
🎫 إنشاء عدة أكواد دفعة واحدة

أرسل عدد الأكواد التي تريد إنشاءها:

💡 مثال: 10
(سيتم إنشاء 10 أكواد بنفس المواصفات)

أو اضغط إلغاء للرجوع.
        """
        
        keyboard = [
            [InlineKeyboardButton("🔙 رجوع", callback_data="admin_dashboard")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text, reply_markup=reply_markup)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض رسالة المساعدة المحدثة"""
        user = update.effective_user
        
        text = """
🛠️ أوامر البوت المتاحة:

👤 للمستخدمين:
• /start - بدء استخدام البوت
• /use [الكود] - تفعيل كود اشتراك
• /trial - تفعيل فترة تجريبية مجانية (48 ساعة)
• /mainchannel - رابط القناة الرئيسية
• /mysubscription - عرض معلومات الاشتراك
• /channels - عرض القنوات المتاحة
• /help - عرض هذه الرسالة

🎯 **الميزووات الجديدة:**
• 🆓 فترة تجريبية مجانية لمدة 48 ساعة
• 📢 البقاء في القناة الرئيسية دائماً
• 🔗 إخراج من القنوات المميزة فقط عند انتهاء الاشتراك

💡 ملاحظة: يمكنك إرسال الكود مباشرة دون استخدام الأمر /use
        """
        
        if self.system.is_admin(user.id):
            text += """

👑 للمشرفين:
• /createcode [المدة] [السعر] - إنشاء كود جديد
• /createmultiple [العدد] [المدة] [السعر] - إنشاء عدة أكواد دفعة واحدة
• /createbatch [عدد] [مدة] [سعر] - إنشاء دفعة أكواد
• /codes - عرض الأكواد المتاحة
• /subscribers - عرض المشتركين
• /stats - إحصائيات النظام
• /addadmin [معرف] - إضافة مشرف
• /admins - عرض المشرفين
• /addchannel [معرف] [معرف_عام] [اسم] - إضافة قناة
• /channelslist - عرض القنوات
• /buttons - عرض الأزرار
• /addbutton [نص] [أمر] [رد] - إضافة زر
• /checkexpired - التحقق من الاشتراكات المنتهية يدوياً
• /sendnotifications - إرسال التنبيهات يدوياً
            """
        
        await update.message.reply_text(text)

    async def use_code_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """استخدام كود عبر الأمر"""
        if not context.args:
            await update.message.reply_text("❌ يرجى إدخال الكود\nاستخدم: /use [الكود]")
            return
        
        code = context.args[0].upper()
        user = update.effective_user
        
        processing_msg = await update.message.reply_text("⏳ جاري تفعيل الكود...")
        
        success, message, invite_links = await self.system.use_code(
            code, user.id, user.username, user.first_name, user.last_name, context
        )
        
        await processing_msg.delete()
        await update.message.reply_text(message)
        
        # إرسال روابط الدعوة إذا كانت موجودة
        if success and invite_links:
            links_text = "🔗 روابط الانضمام إلى القنوات:\n\n"
            for link_info in invite_links:
                links_text += f"📢 {link_info['channel_name']}\n{link_info['invite_link']}\n\n"
            
            await update.message.reply_text(links_text)

    async def my_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض الاشتراك عبر الأمر"""
        user = update.effective_user
        subscription_info = self.system.get_subscription_info(user.id)
        
        if not subscription_info or not subscription_info[3]:
            await update.message.reply_text("❌ ليس لديك اشتراك فعال")
            return
        
        code_used, subscribed_at, expires_at, is_active, invite_links_json, is_trial = subscription_info
        
        trial_text = "تجريبية" if is_trial else "عادية"
        
        text = f"""
📋 معلومات اشتراكك

🎫 الكود المستخدم: {code_used}
📅 تاريخ البدء: {subscribed_at.split()[0]}
⏰ تاريخ الانتهاء: {expires_at.split()[0]}
🔰 الحالة: {'🟢 نشط' if is_active else '🔴 منتهي'}
🎯 النوع: {trial_text}
        """
        
        # عرض روابط الدعوة إذا كانت موجودة
        if invite_links_json:
            invite_links = json.loads(invite_links_json)
            if invite_links:
                text += "\n🔗 روابط القنوات:\n"
                for link_info in invite_links:
                    text += f"• {link_info['channel_name']}: {link_info['invite_link']}\n"
        
        await update.message.reply_text(text)

    async def list_channels(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض القنوات عبر الأمر"""
        channels = self.system.get_active_channels()
        
        if not channels:
            await update.message.reply_text("📭 لا توجد قنوات متاحة حالياً")
            return
        
        text = "📢 القنوات المتاحة:\n\n"
        for i, channel in enumerate(channels, 1):
            channel_id, username, name, added_at, is_active, is_main_channel = channel[1:7]
            main_text = " (رئيسية)" if is_main_channel else ""
            text += f"{i}. {name}{main_text}"
            if username:
                text += f" - {username}"
            text += "\n"
        
        await update.message.reply_text(text)

    async def create_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إنشاء كود جديد"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text("❌ صيغة غير صحيحة\nاستخدم: /createcode [المدة] [السعر]")
            return
        
        try:
            duration = int(context.args[0])
            price = float(context.args[1])
            
            success, code = self.system.create_subscription_code(duration, price, update.effective_user.id)
            
            if success:
                await update.message.reply_text(f"✅ تم إنشاء الكود: {code}\n⏰ المدة: {duration} يوم\n💰 السعر: ${price:.2f}")
            else:
                await update.message.reply_text(f"❌ {code}")
                
        except ValueError:
            await update.message.reply_text("❌ يرجى إدخال أرقام صحيحة")

    async def create_batch(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إنشاء دفعة أكواد"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        if len(context.args) < 3:
            await update.message.reply_text("❌ صيغة غير صحيحة\nاستخدم: /createbatch [العدد] [المدة] [السعر]")
            return
        
        try:
            count = int(context.args[0])
            duration = int(context.args[1])
            price = float(context.args[2])
            
            if count > 100:
                await update.message.reply_text("❌ الحد الأقصى للدفعة هو 100 كود")
                return
            
            batch_id, codes = self.system.create_batch_codes(count, duration, price, update.effective_user.id)
            
            filename = f"batch_{batch_id}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(f"دفعة الأكواد: {batch_id}\n")
                f.write(f"العدد: {count} كود\n")
                f.write(f"المدة: {duration} يوم\n")
                f.write(f"السعر: ${price:.2f}\n")
                f.write(f"التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
                f.write("=" * 40 + "\n\n")
                
                for i, code in enumerate(codes, 1):
                    f.write(f"{i}. {code}\n")
            
            await update.message.reply_document(
                document=open(filename, 'rb'),
                caption=f"✅ تم إنشاء دفعة الأكواد\n\n📦 المعرف: {batch_id}\n🔢 العدد: {count} كود\n⏰ المدة: {duration} يوم\n💰 السعر: ${price:.2f}"
            )
            
            os.remove(filename)
            
        except ValueError:
            await update.message.reply_text("❌ يرجى إدخال أرقام صحيحة")

    async def list_codes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض الأكواد المتاحة"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        codes = self.system.get_available_codes()
        
        if not codes:
            await update.message.reply_text("📭 لا توجد أكواد متاحة حالياً")
            return
        
        text = "🎫 الأكواد المتاحة:\n\n"
        for i, code in enumerate(codes[:10], 1):
            code_text, duration, price, created_at, expires_at, is_trial = code
            trial_text = " (تجريبي)" if is_trial else ""
            text += f"{i}. {code_text} - {duration} يوم - ${price:.2f}{trial_text}\n"
        
        await update.message.reply_text(text)

    async def list_subscribers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض المشتركين"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        subscribers = self.system.get_all_subscribers()
        
        if not subscribers:
            await update.message.reply_text("📭 لا توجد مشتركين حالياً")
            return
        
        text = "👥 قائمة المشتركين:\n\n"
        for i, sub in enumerate(subscribers[:10], 1):
            user_id, username, first_name, last_name, code_used, subscribed_at, expires_at, is_active, is_trial = sub
            name = f"{first_name} {last_name}" if first_name and last_name else username
            status = "🟢" if is_active else "🔴"
            trial_text = " (تجريبي)" if is_trial else ""
            text += f"{i}. {status} {name} (ID: {user_id}){trial_text}\n"
        
        await update.message.reply_text(text)

    async def show_stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض الإحصائيات عبر الأمر"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        stats = self.system.get_system_stats()
        
        text = f"""
📊 إحصائيات النظام

👥 المشتركين النشطين: {stats.get('active_subscribers', 0)}
🎫 الأكواد المتاحة: {stats.get('available_codes', 0)}
✅ الأكواد المستخدمة: {stats.get('used_codes', 0)}
📢 القنوات النشطة: {stats.get('active_channels', 0)}
💰 الإيرادات: ${stats.get('total_revenue', 0):.2f}
        """
        
        await update.message.reply_text(text)

    async def add_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إضافة مشرف"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        if not context.args:
            await update.message.reply_text("❌ يرجى إدخال معرف المستخدم\nاستخدم: /addadmin [معرف_المستخدم]")
            return
        
        try:
            new_admin_id = int(context.args[0])
            success, message = self.system.add_admin(new_admin_id, "", "", "", update.effective_user.id)
            await update.message.reply_text(message)
        except ValueError:
            await update.message.reply_text("❌ يرجى إدخال معرف صحيح")

    async def remove_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إزالة مشرف"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        if not context.args:
            await update.message.reply_text("❌ يرجى إدخال معرف المستخدم\nاستخدم: /removeadmin [معرف_المستخدم]")
            return
        
        try:
            admin_id = int(context.args[0])
            success, message = self.system.remove_admin(admin_id)
            await update.message.reply_text(message)
        except ValueError:
            await update.message.reply_text("❌ يرجى إدخال معرف صحيح")

    async def list_admins(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض المشرفين"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        admins = self.system.get_all_admins()
        
        if not admins:
            await update.message.reply_text("📭 لا توجد مشرفين مضافين حالياً")
            return
        
        text = "👑 قائمة المشرفين:\n\n"
        for i, admin in enumerate(admins, 1):
            user_id, username, first_name, last_name, added_at = admin
            name = f"{first_name} {last_name}" if first_name and last_name else username
            text += f"{i}. {name} (ID: {user_id})\n"
        
        await update.message.reply_text(text)

    async def add_channel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إضافة قناة جديدة"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        if len(context.args) < 3:
            await update.message.reply_text("❌ صيغة غير صحيحة\nاستخدم: /addchannel [معرف_القناة] [معرف_المستخدم] [اسم_القناة]")
            return
        
        channel_id = context.args[0]
        channel_username = context.args[1]
        channel_name = ' '.join(context.args[2:])

        success, message = self.system.add_additional_channel(
            channel_id, channel_username, channel_name, update.effective_user.id
        )

        await update.message.reply_text(message)

    async def admin_channels_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """عرض قائمة القنوات للمشرفين"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        channels = self.system.get_active_channels()
        
        if not channels:
            await update.message.reply_text("📭 لا توجد قنوات مضافين حالياً")
            return
        
        text = "📢 قائمة القنوات:\n\n"
        for i, channel in enumerate(channels, 1):
            channel_id, username, name, added_at, is_active, is_main_channel = channel[1:7]
            main_text = " (رئيسية)" if is_main_channel else ""
            text += f"{i}. {name}{main_text}\n"
            text += f"   🆔 {channel_id}\n"
            if username:
                text += f"   🔗 {username}\n"
            text += "\n"
        
        await update.message.reply_text(text)

    async def check_expired_manually(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """التحقق من الاشتراكات المنتهية يدوياً"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        processing_msg = await update.message.reply_text("🔄 جاري التحقق من الاشتراكات المنتهية...")
        
        try:
            await self.system.check_expired_subscriptions_async()
            await processing_msg.edit_text("✅ تم الانتهاء من فحص الاشتراكات المنتهية")
        except Exception as e:
            await processing_msg.edit_text(f"❌ حدث خطأ أثناء الفحص: {e}")

    async def send_notifications_manually(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إرسال التنبيهات يدوياً"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        processing_msg = await update.message.reply_text("🔄 جاري إرسال التنبيهات...")
        
        try:
            await self.system.send_expiry_notifications_async()
            await processing_msg.edit_text("✅ تم إرسال التنبيهات بنجاح")
        except Exception as e:
            await processing_msg.edit_text(f"❌ حدث خطأ أثناء إرسال التنبيهات: {e}")

    async def create_multiple_codes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """إنشاء عدة أكواد دفعة واحدة"""
        if not self.system.is_admin(update.effective_user.id):
            await update.message.reply_text("❌ ليس لديك صلاحية هذا الأمر")
            return
        
        if len(context.args) < 3:
            await update.message.reply_text(
                "❌ صيغة غير صحيحة\n"
                "استخدم: /createmultiple [العدد] [المدة] [السعر]\n\n"
                "💡 مثال: /createmultiple 10 30 5.00\n"
                "(سيتم إنشاء 10 أكواد، مدة 30 يوم، سعر 5.00 لكل كود)"
            )
            return
        
        try:
            count = int(context.args[0])
            duration = int(context.args[1])
            price = float(context.args[2])
            
            if count <= 0:
                await update.message.reply_text("❌ يرجى إدخال عدد صحيح موجب")
                return
                
            if count > 1000:
                await update.message.reply_text("❌ الحد الأقصى للعدد هو 1000 كود")
                return
            
            processing_msg = await update.message.reply_text(f"🔄 جاري إنشاء {count} كود...")
            
            # إنشاء الأكواد
            batch_id, codes, failed_codes = self.system.create_multiple_codes(
                count, duration, price, update.effective_user.id
            )
            
            success_count = len(codes)
            failed_count = len(failed_codes)
            
            # إنشاء ملف نصي يحتوي على الأكواد
            filename = f"multiple_codes_{batch_id}.txt"
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(f"دفعة الأكواد: {batch_id}\n")
                f.write(f"العدد: {count} كود\n")
                f.write(f"العدد الناجح: {success_count} كود\n")
                f.write(f"العدد الفاشل: {failed_count} كود\n")
                f.write(f"المدة: {duration} يوم\n")
                f.write(f"السعر: ${price:.2f} لكل كود\n")
                f.write(f"الإيراد المتوقع: ${price * success_count:.2f}\n")
                f.write(f"التاريخ: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
                f.write("=" * 50 + "\n\n")
                
                for i, code in enumerate(codes, 1):
                    f.write(f"{i}. {code}\n")
            
            # إرسال الملف مع تقرير
            caption = f"""
✅ تم إنشاء {success_count} كود بنجاح من أصل {count}

📦 المعرف: {batch_id}
🔢 العدد: {count} كود
⏰ المدة: {duration} يوم
💰 السعر: ${price:.2f} لكل كود
💵 الإيراد المتوقع: ${price * success_count:.2f}

📋 الأكواد المرفوعة في الملف
            """
            
            if failed_count > 0:
                caption += f"\n⚠️ فشل إنشاء {failed_count} كود"
            
            await processing_msg.delete()
            await update.message.reply_document(
                document=open(filename, 'rb'),
                caption=caption
            )
            
            # حذف الملف المؤقت
            os.remove(filename)
            
        except ValueError:
            await update.message.reply_text("❌ يرجى إدخال أرقام صحيحة")
        except Exception as e:
            await update.message.reply_text(f"❌ حدث خطأ أثناء إنشاء الأكواد: {e}")

    @retry_async(max_retries=2, delay=0.5)
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة الرسائل العامة - تم التطوير للتعرف التلقائي على الأكواد"""
        if not update.message:
            return
            
        text = update.message.text.strip()
        
        # تجاهل الرسائل التي تبدأ ب / لأنها أوامر
        if text.startswith('/'):
            return
        
        # التحقق إذا كان النص عبارة عن كود صالح
        code = self.system.find_code_in_text(text)
        
        if code:
            # تم العثور على كود صالح، نقوم بتفعيله
            user = update.effective_user
            
            processing_msg = await update.message.reply_text("⏳ جاري تفعيل الكود...")
            
            try:
                success, message, invite_links = await self.system.use_code(
                    code, user.id, user.username, user.first_name, user.last_name, context
                )
                
                await processing_msg.delete()
                await self.system.safe_send_message(
                    context.bot,
                    update.message.chat_id,
                    message
                )
                
                # إرسال روابط الدعوة إذا كانت موجودة
                if success and invite_links:
                    links_text = "🔗 روابط الانضمام إلى القنوات:\n\n"
                    for link_info in invite_links:
                        links_text += f"📢 {link_info['channel_name']}\n{link_info['invite_link']}\n\n"
                    
                    await self.system.safe_send_message(
                        context.bot,
                        update.message.chat_id,
                        links_text
                    )
                    
            except Exception as e:
                logger.error(f"❌ خطأ في معالجة الكود: {e}")
                await processing_msg.delete()
                await self.system.safe_send_message(
                    context.bot,
                    update.message.chat_id,
                    "❌ حدث خطأ أثناء تفعيل الكود، يرجى المحاولة مرة أخرى"
                )
        else:
            # إذا لم يكن كوداً، نرسل رسالة المساعدة
            await self.system.safe_send_message(
                context.bot,
                update.message.chat_id,
                "🤖 بوت إدارة الاشتراكات\n\n"
                "💡 استخدم /start لرؤية الأوامر المتاحة\n\n"
                "🎫 يمكنك الآن إرسال الكود مباشرة دون استخدام الأمر /use"
            )

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """معالجة الأخطاء"""
        logger.error(f"❌ خطأ في البوت: {context.error}", exc_info=context.error)

    def run_bot(self):
        """تشغيل البوت مع إعدادات HTTP محسنة - تم التصحيح"""
        try:
            # إعدادات HTTP محسنة
            from telegram.request import HTTPXRequest
            
            # استخدام HTTPXRequest مع إعدادات محسنة
            request = HTTPXRequest(
                connection_pool_size=50,  # زيادة حجم pool الاتصالات
                read_timeout=60.0,
                write_timeout=60.0,
                connect_timeout=60.0,
                pool_timeout=120.0
            )
            
            application = Application.builder().token(self.token).request(request).build()
            self.setup_handlers(application)
            self.system.set_application(application)
            
            logger.info("🚀 بدء تشغيل بوت إدارة الاشتراكات...")
            print("=" * 60)
            print("🤖 بوت إدارة اشتراكات ")
            print("✅ الإصدار المطور - نظام الفترات التجريبية والبقاء في القناة الرئيسية")
            print("=" * 60)
            
            # إعدادات Polling محسنة
            application.run_polling(
                poll_interval=2.0,  # زيادة الفترة بين الاستعلامات
                timeout=60,
                drop_pending_updates=True,
                allowed_updates=['message', 'callback_query']
            )
            
        except Exception as e:
            logger.error(f"❌ خطأ في تشغيل البوت: {e}")
            print(f"❌ فشل تشغيل البوت: {e}")

    async def run_bot_async(self):
        """تشغيل البوت بشكل غير متزامن (للاستخدام مع Render)"""
        try:
            from telegram.request import HTTPXRequest
            
            request = HTTPXRequest(
                connection_pool_size=50,
                read_timeout=60.0,
                write_timeout=60.0,
                connect_timeout=60.0,
                pool_timeout=120.0
            )
            
            application = Application.builder().token(self.token).request(request).build()
            self.setup_handlers(application)
            self.system.set_application(application)
            
            logger.info("🚀 بدء تشغيل بوت إدارة الاشتراكات على Render...")
            print("=" * 60)
            print("🤖 بوت إدارة اشتراكات ")
            print("✅ الإصدار المعدل للعمل على Render")
            print("=" * 60)
            
            await application.initialize()
            await application.start()
            
            # البقاء قيد التشغيل
            while True:
                await asyncio.sleep(3600)  # الانتظار لمدة ساعة
            
        except Exception as e:
            logger.error(f"❌ خطأ في تشغيل البوت: {e}")
            print(f"❌ فشل تشغيل البوت: {e}")

# =============================================
# الدالة الرئيسية المعدلة للعمل على Render
# =============================================
def main():
    """الدالة الرئيسية المعدلة للتشغيل على Render"""
    print("🚀 نظام إدارة مشتركين ")
    print("✅ الإصدار المعدل للعمل على Render")
    print("=" * 60)
    
    # التحقق من وجود التوكن
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ لم يتم تعيين BOT_TOKEN")
        print("⚠️ يرجى تعيين متغير البيئة BOT_TOKEN في Render")
        return
    
    try:
        # إنشاء مثيل النظام
        system = SubscriptionManagementSystem()
        print("✅ تم تهيئة نظام الإدارة بنجاح")
        
        # إنشاء وتشغيل البوت
        bot = TelegramSubscriptionBot(system)
        
        # استخدام run_bot_async إذا كان على Render، وإلا run_bot العادي
        import asyncio
        asyncio.run(bot.run_bot_async())
            
    except Exception as e:
        print(f"❌ فشل تشغيل البوت: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()