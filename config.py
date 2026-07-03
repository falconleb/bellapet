import os, secrets as _secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATABASE = os.path.join(BASE_DIR, 'petstore.db')

_secret = os.environ.get('SECRET_KEY')
if not _secret:
    _secret = _secrets.token_hex(32)
    print('[WARNING] SECRET_KEY not set in environment — using random key. Sessions will reset on every restart. Set SECRET_KEY in your .env file for production.')
SECRET_KEY = _secret

# رقم واتساب المتجر (بصيغة دولية بدون + أو 00، مثال: 9613xxxxxx)
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '96181680729')

SITE_NAME_EN = "Bella Pet"
SITE_NAME_AR = "بيلا بت"

CURRENCY_SYMBOL = "$"

# رسوم التوصيل الثابتة (بالدولار) — تُطبَّق على جميع المناطق
FLAT_DELIVERY_FEE = 4.0
DELIVERY_FEE_MIN  = 4
DELIVERY_FEE_MAX  = 4

# أيام التوصيل الافتراضية
DELIVERY_DAYS_MIN = 2
DELIVERY_DAYS_MAX = 4

# للتوافق مع كود الكارت القديم — كل المناطق بنفس السعر
DELIVERY_FEES = {area: FLAT_DELIVERY_FEE for area in [
    "بيروت","المتن","بعبدا","كسروان","الشوف","عاليه","جبيل",
    "طرابلس","البترون","زغرتا","الكورة","عكار","صيدا","صور",
    "النبطية","بنت جبيل","مرجعيون","زحلة","البقاع الغربي","راشيا","بعلبك - الهرمل",
]}

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_SECURE   = os.environ.get('HTTPS', '') == '1'  # True لما تشغّل بـ HTTPS

DEFAULT_LANGUAGE = "en"  # اللغة الافتراضية للموقع
SUPPORTED_LANGUAGES = ("en", "ar")

UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'img', 'products')
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5MB حد أقصى لرفع الصور

# Anthropic API — للمستشار الذكي (اختياري)
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')

# Gemini API — لتوليد SEO والوصف (مجاني)
# احصل على مفتاحك من: https://aistudio.google.com/app/apikey
GEMINI_API_KEY   = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL     = 'gemini-2.0-flash'

# Groq API — للمستشار الذكي والبحث
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
GROQ_MODEL   = 'llama-3.3-70b-versatile'
