"""
توليد SEO ذكي — Gemini (مجاني) أو Claude كـ fallback.
"""

import json
import urllib.request
import config

# ══════════════════════════════════════════════════════════════════
#  قواعد SEO الصارمة — هاي الدستور اللي Claude لازم يطبّقه
# ══════════════════════════════════════════════════════════════════

SEO_RULES = """
═══════════════════════════════════════════
         قواعد SEO الصارمة — يُطبّق 100%
═══════════════════════════════════════════

## 1. عنوان الصفحة (meta_title)
   - العربي:    50-60 حرف بالضبط (احسب كل حرف وفراغ)
   - الإنجليزي: 50-60 حرف بالضبط
   - البنية الإلزامية: [الكلمة المفتاحية الرئيسية] + [تمييز] + [لبنان أو Lebanon]
   - مثال صحيح:  "طعام قطط رويال كانين للبالغين | لبنان" = 40 حرف ✓
   - مثال خاطئ: "أفضل طعام للقطط البالغة في لبنان مع التوصيل" = طويل جداً ✗
   - ممنوع: علامات تعجب، حروف كابيتال كلها، نقاط زائدة
   - الكلمة المفتاحية الأهم تكون في البداية دائماً

## 2. وصف الصفحة (meta_desc)
   - العربي:    145-155 حرف بالضبط (احسب كل حرف وفراغ)
   - الإنجليزي: 145-155 حرف بالضبط
   - يجب أن يحتوي على:
     ✓ الكلمة المفتاحية الرئيسية في أول 20 كلمة
     ✓ فائدة واحدة واضحة (ليس قائمة)
     ✓ دعوة للعمل في النهاية: "اطلب الآن" أو "تسوّق هلق" أو "وصّلناك لعندك"
     ✓ إشارة للتوصيل في لبنان
   - ممنوع: تكرار عنوان الصفحة، كلمات حشو ("متجرنا الرائع")

## 3. الكلمات المفتاحية (keywords)

   ### الكلمات الساخنة 🔥 — حجم بحث عالي في لبنان (استخدم منها دائماً)
   بالعربي:
     - "طعام قطط لبنان" | "متجر حيوانات أليفة لبنان"
     - "رمل قطط لبنان" | "طعام كلاب لبنان"
     - "توصيل مستلزمات حيوانات" | "شراء طعام حيوانات أليفة"
     - "أفضل طعام قطط" | "سعر طعام كلاب لبنان"
     - "مستلزمات قطط بيروت" | "pet store lebanon"
   بالإنجليزي:
     - "pet store lebanon" | "cat food lebanon"
     - "dog food lebanon delivery" | "buy cat litter lebanon"
     - "pet supplies beirut" | "online pet store lebanon"

   ### الكلمات الباردة ❄️ — حجم بحث منخفض أو منافسة عالية جداً (تجنّب)
   - "أفضل" وحدها بدون تخصص (منافسة عالية جداً)
   - "حيوانات أليفة" وحدها (عامة جداً)
   - أسماء ماركات عالمية بدون كلمة "لبنان" (Amazon يهيمن عليها)
   - كلمات إنجليزية عامة: "pet food", "cat food" بدون "lebanon"
   - "رخيص" أو "cheap" (تجذب زبائن غير مناسبين)

   ### عدد الكلمات المطلوب:
   - منتج:   10 كلمات عربية + 8 كلمات إنجليزية
   - تصنيف:  8 كلمات عربية + 6 كلمات إنجليزية
   - مدونة:  6 كلمات عربية + 5 كلمات إنجليزية
   - رئيسية: 12 كلمة عربية + 10 كلمات إنجليزية

   ### ترتيب الكلمات: من الأساخن للأبرد (الأهم أولاً)
   ### الفاصل: فاصلة عربية "،" للعربي، comma "," للإنجليزي

## 4. عنوان Open Graph (og_title)
   - 40-55 حرف
   - جذاب للمشاركة على WhatsApp وInstagram
   - يحتوي رقم أو إحصاء إن أمكن: "طعام كلاب RoyalPaw — توصيل خلال 24 ساعة"
   - أو سؤال: "قطتك تستحق الأفضل؟"

## 5. وصف Open Graph (og_description)
   - 90-110 حرف
   - محفّز للنقر، يذكر توصيل لبنان

## 6. قواعد عامة
   - لا تكرر نفس العبارة في أكثر من حقل
   - كل صفحة لها هوية SEO مختلفة (حتى لو المنتجات متشابهة)
   - لبنان تُكتب "لبنان" (لا "LB" ولا "Lebanese Republic")
   - Lebanon تُكتب "Lebanon" (لا "LB" ولا "Lebanese")
   - الأسعار لا تُذكر في العنوان (تتغير)
   - ممنوع Keyword Stuffing (تكرار نفس الكلمة أكثر من مرتين)

═══════════════════════════════════════════
"""

# ══════════════════════════════════════════════════════════════════
#  الكلمات الساخنة حسب التصنيف — لحقن تلقائي بالـ prompt
# ══════════════════════════════════════════════════════════════════

HOT_KEYWORDS = {
    'قطط': 'طعام قطط لبنان، رمل قطط لبنان، مستلزمات قطط بيروت، متجر قطط لبنان',
    'كلاب': 'طعام كلاب لبنان، مستلزمات كلاب لبنان، سرير كلاب، إكسسوارات كلاب لبنان',
    'طيور': 'طعام طيور لبنان، بذور طيور لبنان، قفص طيور لبنان',
    'أسماك': 'حوض أسماك لبنان، طعام أسماك لبنان، أدوات حوض بيروت',
    'حيوانات صغيرة': 'طعام أرانب لبنان، مستلزمات أرانب، حيوانات صغيرة لبنان',
    'default': 'متجر حيوانات أليفة لبنان، pet store lebanon، مستلزمات حيوانات بيروت',
}


def _hot_keywords_for(category_ar: str) -> str:
    for key in HOT_KEYWORDS:
        if key in (category_ar or ''):
            return HOT_KEYWORDS[key]
    return HOT_KEYWORDS['default']


def _parse_json(text: str) -> dict | None:
    """يستخرج أول JSON object من النص."""
    start = text.find('{')
    end   = text.rfind('}') + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except Exception:
            pass
    return None


def _call_gemini(prompt: str) -> dict | None:
    """Gemini 2.0 Flash — مجاني، بدون مكتبات خارجية."""
    if not config.GEMINI_API_KEY:
        return None
    try:
        url  = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}")
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 900, "temperature": 0.3},
        }).encode()
        req  = urllib.request.Request(url, data=body,
               headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return _parse_json(text)
    except Exception:
        pass
    return None


def _call_claude(prompt: str) -> dict | None:
    """Claude — fallback لو ما في Gemini key."""
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_json(msg.content[0].text.strip())
    except Exception:
        pass
    return None


def _call_ai(prompt: str) -> dict | None:
    """يجرب Gemini أول، بعدين Claude كـ fallback."""
    return _call_gemini(prompt) or _call_claude(prompt)


# ══════════════════════════════════════════════════════════════════
#  دوال التوليد
# ══════════════════════════════════════════════════════════════════

def generate_product_seo(product: dict, category_name_ar: str) -> dict | None:
    name_ar    = product.get('name_ar', '')
    name_en    = product.get('name_en', '')
    brand      = product.get('brand', '') or ''
    benefit_ar = product.get('benefit_ar', '') or ''
    benefit_en = product.get('benefit_en', '') or ''
    desc_ar    = (product.get('description_ar', '') or '')[:250]
    hot_kw     = _hot_keywords_for(category_name_ar)

    prompt = f"""{SEO_RULES}

الآن ولّد بيانات SEO للمنتج التالي مع الالتزام الكامل بالقواعد أعلاه:

المنتج:
  الاسم بالعربي:    {name_ar}
  الاسم بالإنجليزي: {name_en}
  الماركة:          {brand}
  التصنيف:          {category_name_ar}
  الفائدة (عربي):   {benefit_ar}
  الفائدة (إنجليزي): {benefit_en}
  الوصف:            {desc_ar}

كلمات ساخنة للتصنيف يجب دمجها: {hot_kw}

⚠️ تحقق من عدد الحروف قبل الإرسال:
  - meta_title_ar:  50-60 حرف
  - meta_title_en:  50-60 حرف
  - meta_desc_ar:   145-155 حرف
  - meta_desc_en:   145-155 حرف
  - keywords_ar:    10 كلمات مفصولة بـ "،"
  - keywords_en:    8 كلمات مفصولة بـ ","
  - og_title:       40-55 حرف
  - og_description: 90-110 حرف

أجب بـ JSON فقط بدون أي نص قبله أو بعده:
{{
  "meta_title_ar": "...",
  "meta_title_en": "...",
  "meta_desc_ar": "...",
  "meta_desc_en": "...",
  "keywords_ar": "...",
  "keywords_en": "...",
  "og_title": "...",
  "og_description": "..."
}}"""

    return _call_ai(prompt)


def generate_category_seo(category: dict) -> dict | None:
    name_ar = category.get('name_ar', '')
    name_en = category.get('name_en', '')
    hot_kw  = _hot_keywords_for(name_ar)

    prompt = f"""{SEO_RULES}

ولّد بيانات SEO لصفحة تصنيف في متجر حيوانات أليفة بلبنان:

التصنيف: {name_ar} ({name_en})
المتجر:  يبيع طعام، إكسسوارات، رمل، أدوات — توصيل لكل لبنان.

كلمات ساخنة للتصنيف يجب دمجها: {hot_kw}

⚠️ تحقق من عدد الحروف:
  - meta_title_ar:  50-60 حرف
  - meta_title_en:  50-60 حرف
  - meta_desc_ar:   145-155 حرف
  - meta_desc_en:   145-155 حرف
  - keywords_ar:    8 كلمات مفصولة بـ "،"
  - keywords_en:    6 كلمات مفصولة بـ ","
  - og_title:       40-55 حرف
  - og_description: 90-110 حرف

JSON فقط:
{{
  "meta_title_ar": "...",
  "meta_title_en": "...",
  "meta_desc_ar": "...",
  "meta_desc_en": "...",
  "keywords_ar": "...",
  "keywords_en": "...",
  "og_title": "...",
  "og_description": "..."
}}"""

    return _call_ai(prompt)


def generate_blog_seo(post: dict) -> dict | None:
    title_ar   = post.get('title_ar', '')
    title_en   = post.get('title_en', '')
    content_ar = (post.get('content_ar', '') or '')[:300]

    prompt = f"""{SEO_RULES}

ولّد بيانات SEO لمقالة في مدونة متجر حيوانات أليفة بلبنان:

عنوان (عربي):    {title_ar}
عنوان (إنجليزي): {title_en}
مقتطف المحتوى:   {content_ar}

⚠️ تحقق من عدد الحروف:
  - meta_title_ar:  50-60 حرف (اجعله سؤالاً أو وعداً)
  - meta_title_en:  50-60 حرف
  - meta_desc_ar:   145-155 حرف
  - meta_desc_en:   145-155 حرف
  - keywords_ar:    6 كلمات مفصولة بـ "،"
  - keywords_en:    5 كلمات مفصولة بـ ","
  - og_title:       40-55 حرف
  - og_description: 90-110 حرف

JSON فقط:
{{
  "meta_title_ar": "...",
  "meta_title_en": "...",
  "meta_desc_ar": "...",
  "meta_desc_en": "...",
  "keywords_ar": "...",
  "keywords_en": "...",
  "og_title": "...",
  "og_description": "..."
}}"""

    return _call_ai(prompt)


def generate_static_seo(page_slug: str) -> dict | None:
    pages = {
        'home': {
            'ar': 'الصفحة الرئيسية — متجر مستلزمات الحيوانات الأليفة الأول في لبنان. طعام، رمل، إكسسوارات، ألعاب للقطط والكلاب والطيور والأسماك. توصيل سريع.',
            'en': 'Homepage — Lebanon\'s online pet store for cats, dogs, birds and fish. Food, litter, accessories and toys. Fast delivery across Lebanon.',
        },
    }
    page = pages.get(page_slug, {'ar': page_slug, 'en': page_slug})

    prompt = f"""{SEO_RULES}

ولّد بيانات SEO للصفحة الرئيسية لمتجر حيوانات أليفة في لبنان:

وصف الصفحة (عربي):    {page['ar']}
وصف الصفحة (إنجليزي): {page['en']}

كلمات ساخنة يجب دمجها:
  متجر حيوانات أليفة لبنان، pet store lebanon، طعام قطط لبنان،
  طعام كلاب لبنان، مستلزمات حيوانات بيروت، توصيل حيوانات أليفة لبنان

⚠️ تحقق من عدد الحروف:
  - meta_title_ar:  50-60 حرف
  - meta_title_en:  50-60 حرف
  - meta_desc_ar:   145-155 حرف
  - meta_desc_en:   145-155 حرف
  - keywords_ar:    12 كلمة مفصولة بـ "،"
  - keywords_en:    10 كلمات مفصولة بـ ","
  - og_title:       40-55 حرف (جذاب للمشاركة)
  - og_description: 90-110 حرف

JSON فقط:
{{
  "meta_title_ar": "...",
  "meta_title_en": "...",
  "meta_desc_ar": "...",
  "meta_desc_en": "...",
  "keywords_ar": "...",
  "keywords_en": "...",
  "og_title": "...",
  "og_description": "..."
}}"""

    return _call_ai(prompt)
