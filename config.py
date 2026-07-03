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

# نطاق رسوم التوصيل التقريبي (وكيلني) - يستخدم بالرسائل التلقائية فقط
DELIVERY_FEE_MIN = 4
DELIVERY_FEE_MAX = 5

# رسوم التوصيل حسب المنطقة (بالدولار) — عدّلها حسب أسعارك الفعلية
DELIVERY_FEES = {
    "بيروت":               2.0,
    "المتن":               2.5,
    "بعبدا":               2.5,
    "كسروان":              3.0,
    "الشوف":               3.0,
    "عاليه":               3.0,
    "جبيل":                3.0,
    "طرابلس":              4.0,
    "البترون":             4.0,
    "زغرتا":               4.5,
    "الكورة":              4.0,
    "عكار":                5.0,
    "صيدا":                4.0,
    "صور":                 4.5,
    "النبطية":             4.5,
    "بنت جبيل":            5.0,
    "مرجعيون":             5.0,
    "زحلة":                4.0,
    "البقاع الغربي":       4.5,
    "راشيا":               5.0,
    "بعلبك - الهرمل":      5.0,
}

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
