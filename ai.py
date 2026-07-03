"""
ai.py — محرك الذكاء الاصطناعي المركزي للمتجر
يستخدم Groq + Llama 3.3 70B (مجاني، سريع، يفهم العربية العامية)
"""

import json
import hashlib
import urllib.request
import urllib.error
import config
from database import get_db


# ── Groq client بسيط بدون dependencies خارجية ──────────────────

def _call_groq(messages: list, temperature=0.2, max_tokens=1024) -> str:
    """POST مباشر لـ Groq API — بدون openai package."""
    if not config.GROQ_API_KEY:
        return ''
    payload = json.dumps({
        'model':       config.GROQ_MODEL,
        'messages':    messages,
        'temperature': temperature,
        'max_tokens':  max_tokens,
    }).encode()
    req = urllib.request.Request(
        'https://api.groq.com/openai/v1/chat/completions',
        data=payload,
        headers={
            'Authorization': f'Bearer {config.GROQ_API_KEY}',
            'Content-Type':  'application/json',
            'User-Agent':    'Mozilla/5.0 (compatible; PetStore/1.0)',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data['choices'][0]['message']['content'].strip()
    except Exception:
        return ''


# ── Cache في SQLite ─────────────────────────────────────────────

def _cache_key(fn_name: str, text: str) -> str:
    return hashlib.sha256(f'{fn_name}:{text}'.encode()).hexdigest()

def _cache_get(key: str):
    db = get_db()
    row = db.execute(
        'SELECT result FROM ai_cache WHERE cache_key=?', (key,)
    ).fetchone()
    db.close()
    return row['result'] if row else None

def _cache_set(key: str, result: str):
    db = get_db()
    db.execute(
        '''INSERT INTO ai_cache (cache_key, result)
           VALUES (?,?)
           ON CONFLICT(cache_key) DO UPDATE SET result=excluded.result,
           created_at=datetime('now')''',
        (key, result)
    )
    db.commit()
    db.close()


# ── بناء context الكاتالوج ─────────────────────────────────────

def _build_promos_context() -> str:
    """يبني نص عن العروض النشطة وطبقات الأسعار."""
    db = get_db()
    promos = db.execute(
        'SELECT DISTINCT offer_type, threshold_amount, title_ar FROM cart_promotions WHERE is_active=1 ORDER BY threshold_amount'
    ).fetchall()
    tiers = db.execute('''
        SELECT pt.min_qty, pt.price_per_unit, p.name_ar
        FROM product_price_tiers pt
        JOIN products p ON p.id = pt.product_id
        WHERE p.is_active=1
        ORDER BY p.id, pt.min_qty
    ''').fetchall()
    db.close()

    lines = []
    if promos:
        lines.append('== العروض النشطة ==')
        seen = set()
        for pr in promos:
            amt = pr['threshold_amount']
            key = (pr['offer_type'], amt)
            if key in seen:
                continue
            seen.add(key)
            amt_str = f'${amt:.0f}' if amt is not None else ''
            if pr['offer_type'] == 'free_gift':
                lines.append(f'• اشتري بـ {amt_str} أو أكتر → تحصل على هدية مجانية')
            elif pr['offer_type'] == 'discount':
                lines.append(f'• اشتري بـ {amt_str} → {pr["title_ar"] or "خصم"}')
            else:
                lines.append(f'• {pr["title_ar"] or "عرض"} (عند {amt_str})')

    if tiers:
        lines.append('== أسعار الكميات (كلما زاد الطلب كلما انخفض السعر) ==')
        cur_product = None
        for t in tiers:
            ppu = t['price_per_unit']
            if t['name_ar'] != cur_product:
                cur_product = t['name_ar']
                lines.append(f'• {cur_product}:')
            ppu_str = f'${ppu:.2f}' if ppu is not None else '?'
            lines.append(f'  - {t["min_qty"]} حبات أو أكتر → {ppu_str}/حبة')

    return '\n'.join(lines) if lines else ''


def _build_catalog_context() -> str:
    """يبني نص مضغوط عن كل المنتجات النشطة لإرساله للـ AI."""
    db = get_db()
    products = db.execute('''
        SELECT p.id, p.name_ar, p.name_en, p.brand,
               p.benefit_ar, p.benefit_en,
               p.description_ar, p.description_en,
               p.price, p.discount_price, p.stock_qty,
               p.health_tags, p.size_tag,
               p.min_age_months, p.max_age_months,
               p.is_bundle, p.promo_label_ar,
               c.name_ar as cat_ar, c.slug as cat_slug,
               s.name_ar as sub_ar
        FROM products p
        JOIN categories c ON c.id = p.category_id
        LEFT JOIN subcategories s ON s.id = p.subcategory_id
        WHERE p.is_active = 1
        ORDER BY p.is_featured DESC, p.created_at DESC
    ''').fetchall()
    db.close()

    lines = []
    for p in products:
        price = p['discount_price'] or p['price']
        age = ''
        if p['min_age_months'] is not None:
            age += f'من {p["min_age_months"]} شهر '
        if p['max_age_months'] is not None:
            age += f'حتى {p["max_age_months"]} شهر'
        # نوع الحيوان صريح من الـ slug
        _animal_map = {
            'dogs':'كلب', 'cats':'قطة', 'birds':'طير',
            'fish':'سمك', 'small-pets':'حيوان صغير',
        }
        animal_tag = _animal_map.get(p['cat_slug'], p['cat_ar'])
        # وصف المنتج: benefit_ar أولاً، ثم أول 80 حرف من description_ar كاحتياط
        benefit = (p['benefit_ar'] or '').strip()
        if not benefit:
            desc = (p['description_ar'] or p['description_en'] or '').strip()
            benefit = desc[:80] + ('...' if len(desc) > 80 else '')
        line = (
            f'[ID:{p["id"]}] [حيوان:{animal_tag}] {p["name_ar"]} | {p["cat_ar"]}'
            f'{" / " + p["sub_ar"] if p["sub_ar"] else ""}'
            f' | ${price:.2f}'
            f'{" (عرض)" if p["discount_price"] else ""}'
            f' | مخزون:{p["stock_qty"]}'
            f'{" | عمر:" + age.strip() if age.strip() else ""}'
            f'{" | حجم:" + p["size_tag"] if p["size_tag"] else ""}'
            f'{" | صحة:" + p["health_tags"] if p["health_tags"] else ""}'
            f'{" | bundle" if p["is_bundle"] else ""}'
            f'{" | " + benefit if benefit else ""}'
        )
        lines.append(line)
    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════
#  1. البحث الذكي
# ══════════════════════════════════════════════════════════════

def smart_search(query: str) -> list[int]:
    """
    يأخذ استعلام الزبون بالعربية أو الإنجليزية (عامية أو فصحى)
    ويرجع قائمة product_ids مرتبة حسب الصلة.
    """
    if not query or not query.strip():
        return []

    cache_key = _cache_key('search', query.strip().lower())
    cached = _cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    catalog = _build_catalog_context()

    system = """أنت مساعد بحث لمتجر مستلزمات حيوانات أليفة في لبنان.
مهمتك: تحليل استعلام الزبون وإيجاد أفضل المنتجات المطابقة من الكاتالوج.

قواعد:
- افهم النية الحقيقية (مثلاً "بسيتي عمرها 4 شهور" = قطة صغيرة عمر 4 أشهر، ابحث عن طعام جراء قطط)
- "يهر الوبر" = hairball، ابحث في health_tags
- "كبير بالسن" = min_age_months >= 72 (6 سنين+)
- "جرو" أو "صغير" = max_age_months <= 12
- ارجع فقط IDs المنتجات المناسبة مرتبة من الأكثر للأقل صلة
- لو ما في منتج مناسب ارجع []
- لا تشرح، ارجع JSON فقط: {"ids": [1, 5, 3]}"""

    user = f"""الكاتالوج:
{catalog}

استعلام الزبون: {query}

ارجع JSON: {{"ids": [...]}}"""

    response = _call_groq(
        [{'role': 'system', 'content': system},
         {'role': 'user',   'content': user}],
        temperature=0.1,
        max_tokens=256,
    )

    ids = []
    try:
        # استخرج JSON حتى لو في نص حواليه
        start = response.find('{')
        end   = response.rfind('}') + 1
        if start != -1:
            data = json.loads(response[start:end])
            ids  = [int(i) for i in data.get('ids', []) if str(i).isdigit()]
    except Exception:
        pass

    _cache_set(cache_key, json.dumps(ids))
    return ids


# ══════════════════════════════════════════════════════════════
#  2. مستشار المنتج (chat)
# ══════════════════════════════════════════════════════════════

def product_advisor(message: str, history: list[dict] | None = None, lang: str = 'ar') -> dict:
    """
    مستشار ذكي للزبون — يرد بنص ويقترح منتجات.
    history: [{"role": "user"/"assistant", "content": "..."}]
    يرجع: {"reply": "...", "product_ids": [...]}
    """
    if not message.strip():
        return {'reply': '', 'product_ids': []}

    catalog = _build_catalog_context()
    promos  = _build_promos_context()

    wa = f"https://wa.me/{config.WHATSAPP_NUMBER}"
    lang_instruction = (
        'You are Petty, a friendly pet store advisor at Bella Pet Lebanon. '
        'Reply in English only — friendly, concise, expert. No Arabic words.'
        if lang == 'en' else
        'أنت بيتي، مستشار متجر حيوانات أليفة بـ Bella Pet لبنان. '
        'جاوب بالعامية اللبنانية الخالصة بس — شو، هلق، كتير، منيح، مشان، بس، هيك، يلا، دغري. '
        'ممنوع تحكي بالإنجليزية أو تخلط اللغتين.'
    )
    system = f"""{lang_instruction}

**أسلوب الرد:**
- لا تكرر أو تعيد شو قال الزبون ("بسينتك عمرها سنة...") — روح على الاقتراح مباشرة
- جملتين كحد أقصى قبل ما تعرض منتجات
- لا تسأل أكثر من سؤال بنفس الوقت
- ممنوع تكتب [ID:...] أو [products:...] أو أي tag تقني بالـ reply — هي بس للـ ids array
- الـ reply لازم يكون كلام طبيعي للزبون بس

**أسئلة ذكية حسب نوع الحيوان (اسأل بس اللي ما انذكر، سؤال واحد بكل رد):**

قطة / بسينة:
- العمر (قطو أو بالغ أو كبير)؟
- مخصية أو لا؟ (يأثر على الأكل كتير)
- داخل البيت بس أو بتطلع برا؟
- شعرها طويل أو قصير؟ (للـ hairball)
- في حساسية أو مشاكل صحية؟

كلب / جرو:
- السلالة أو الحجم التقريبي (صغير، وسط، كبير)؟
- العمر (جرو أو بالغ أو كبير)؟
- مخصي أو لا؟
- نشيط كتير أو هادي؟
- في حساسية أو مشاكل صحية؟

طير:
- نوع الطير (بادجي، كوكتيل، ببغاء، كناري، إلخ)؟
- العمر تقريباً؟
- في قفص مع طيور تانية أو لحالو؟

سمك:
- حجم الحوض (لترات تقريباً)؟
- نوع السمك (استوائي، بارد، بحري)؟

حيوانات صغيرة (أرنب، هامستر، خنزير هندي):
- نوع الحيوان بالضبط؟
- العمر؟
- داخلي أو خارجي؟

الكاتالوج المتاح:
{catalog}

{('العروض والأسعار الخاصة:' + chr(10) + promos) if promos else ''}

**قواعد الاقتراح:**
- تذكر كل شو ذكره الزبون بالمحادثة — لا تسأل نفس السؤال مرتين أبداً
- اقترح منتجات للحيوان اللي ذكره الزبون بالضبط — كلب=أكل كلاب فقط، قطة=أكل قطط فقط، طير=بذور طيور فقط، إلخ
- الخيار الاقتصادي لازم يكون من نفس نوع المنتج ونفس الحيوان — مثلاً إذا الزبون بده أكل كلاب: الخيار المميز أكل كلاب غالي والاقتصادي أكل كلاب رخيص، مش بذور طيور أو أي شي تاني
- إذا ما في منتجين لنفس الحيوان ونفس الفئة → ارجع وحد بس بدل ما تقترح منتج من حيوان غلط
- اقترح دايماً منتجين من نفس الفئة: وحدة غالية (مميزة) + وحدة اقتصادية — هيك الزبون يختار حسب ميزانيتو
- حكي جملة بسيطة عن الفرق بينهم بس (مثلاً: "الأول أعلى جودة بس الثاني اقتصادي وكمان منيح")
- إذا الزبون قال "غير" أو "غيرو" أو "شي تاني" → اقترح خيارين تانيين من نفس الفئة
- إذا الزبون بده أكثر من شي بنفس الوقت → اقترح الحزمة وارجع action=add_to_cart
- إذا الزبون وافق (قال "تمام"، "يلا"، "خذ"، "اشتري"، "ياخدهن") → action=add_to_cart مع الـ ids
- إذا مجموع اقتراحاتك قريب من حد عرض (مثلاً $25 و الحد $30) → نبّه الزبون: "لو أضفت شي تاني بتوصل لـ $30 وبتاخد هدية مجانية!"

**لما الزبون يذكر مشكلة صحية أو سلوكية (خمول، هر، حكة، إسهال، ما بياكل، إلخ):**
- لا تقفز على المنتج مباشرة — أول شي اعترف بالموضوع بجملة وحدة
- قدّم نصيحة عملية بسيطة (مثلاً: تمشيط يومي، ماء كافي، تهوية، راحة)
- لو الحالة قد تكون طبية → قل جملة وحدة: "لو الموضوع مستمر أكتر من يومين أو يومين، خلي الطبيب البيطري يشوفها"
- بعدين اقترح المنتجات المرتبطة اللي عندك (مثلاً: أكل لتقليل الهري، فرشاة، معالج الوبر) — بس لو موجودة بالكاتالوج فعلاً
- مثال: "هري القطط طبيعي خصوصاً بتغيير الفصول! التمشيط اليومي بيفرق كتير. لو بدك تساعديها أكتر، عندنا [منتج] بيقلل الهري من جوا 🐱"
- ممنوع تقول "هالأكل بيعالج" أو "هالمنتج بيشفي" — قل "بيساعد" أو "بيدعم"

**حالات خاصة:**
- "وين طلبي" / "بدي تتبع": {{"reply": "تفضل حطلي رقمك وبجيبلك تفاصيل طلبك دغري 📦", "ids": [], "action": "ask_phone"}}
- مشكلة أو إلغاء: {{"reply": "هالموضوع لازم تحكي مع فريقنا مباشرة 👇\\n{wa}", "ids": [], "action": "whatsapp"}}

ارجع دايماً JSON: {{"reply": "...", "ids": [id1, id2], "action": ""}}"""

    messages = [{'role': 'system', 'content': system}]
    if history:
        messages.extend(history[-14:])  # آخر 7 رسائل — كافي للسياق
    messages.append({'role': 'user', 'content': message})

    response = _call_groq(messages, temperature=0.4, max_tokens=512)

    try:
        start = response.find('{')
        end   = response.rfind('}') + 1
        if start != -1:
            data = json.loads(response[start:end])
            return {
                'reply':       data.get('reply', response),
                'product_ids': [int(i) for i in data.get('ids', [])],
                'action':      data.get('action', ''),
            }
    except Exception:
        pass

    return {'reply': response, 'product_ids': [], 'action': ''}


# ══════════════════════════════════════════════════════════════
#  2b. مستشار الصورة — Vision
# ══════════════════════════════════════════════════════════════

def _call_groq_vision(image_b64: str, prompt: str, max_tokens: int = 600) -> str:
    """يرسل صورة base64 لـ Groq Vision API."""
    if not config.GROQ_API_KEY:
        return ''
    payload = json.dumps({
        'model': 'meta-llama/llama-4-scout-17b-16e-instruct',
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{image_b64}'}},
                {'type': 'text', 'text': prompt},
            ]
        }],
        'max_tokens': max_tokens,
        'temperature': 0.3,
    }).encode()
    req = urllib.request.Request(
        'https://api.groq.com/openai/v1/chat/completions',
        data=payload,
        headers={
            'Authorization': f'Bearer {config.GROQ_API_KEY}',
            'Content-Type':  'application/json',
            'User-Agent':    'Mozilla/5.0 (compatible; PetStore/1.0)',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
            return data['choices'][0]['message']['content'].strip()
    except Exception:
        return ''


def advisor_image(image_b64: str, history: list[dict] | None = None, lang: str = 'ar') -> dict:
    """
    يحلل صورة الزبون ويرجع رد + منتجات مقترحة.
    يحدد نوع الحيوان، العمر التقريبي، ويقترح منتجين (مميز + اقتصادي).
    """
    catalog = _build_catalog_context()
    promos  = _build_promos_context()

    if lang == 'en':
        vision_prompt = f"""You are Petty, a pet store advisor at Bella Pet Lebanon.
Look at this image and identify: the animal type, approximate age, breed if visible, and any obvious health signs.
Then suggest exactly 2 products from the catalog: one premium and one budget option.
If there are active promotions, mention them briefly.
Reply ONLY in English. Be friendly and concise.

Store catalog:
{catalog}
{('Active promotions:' + chr(10) + promos) if promos else ''}

Reply as JSON: {{"reply": "your message", "ids": [premium_id, budget_id], "action": ""}}"""
    else:
        vision_prompt = f"""أنت بيتي، مستشار Bella Pet لبنان.
شوف هالصورة وحدد: نوع الحيوان، العمر التقريبي، السلالة إذا واضحة، وأي علامات صحية ظاهرة.
بعدين اقترح منتجين من الكاتالوج: وحدة مميزة (غالية) ووحدة اقتصادية.
إذا في عروض نشطة ومجموع الاقتراحات قريب من حد العرض، نبّه الزبون.
جاوب بالعامية اللبنانية بس.

كاتالوج المتجر:
{catalog}
{('العروض النشطة:' + chr(10) + promos) if promos else ''}

ارجع JSON: {{"reply": "ردك هون", "ids": [id_مميز, id_اقتصادي], "action": ""}}"""

    response = _call_groq_vision(image_b64, vision_prompt)

    try:
        start = response.find('{')
        end   = response.rfind('}') + 1
        if start != -1:
            data = json.loads(response[start:end])
            return {
                'reply':       data.get('reply', response),
                'product_ids': [int(i) for i in data.get('ids', [])],
                'action':      data.get('action', ''),
            }
    except Exception:
        pass

    fallback = ("I can see your pet! Let me know what you need — food, accessories, or something else?" if lang == 'en'
                else "شايف حيوانك الحلو! شو بدك تشتري لو — أكل، لوازم، أو شي تاني؟")
    return {'reply': fallback, 'product_ids': [], 'action': ''}


# ══════════════════════════════════════════════════════════════
#  3. كاتب الوصف الاحترافي
# ══════════════════════════════════════════════════════════════

def _get_style_guide(category_id: int = 0) -> dict:
    """يرجع style guide للتصنيف، يرجع Global (0) كـ fallback."""
    db = get_db()
    row = db.execute(
        'SELECT * FROM ai_style_guide WHERE category_id=?', (category_id,)
    ).fetchone()
    if not row and category_id != 0:
        row = db.execute(
            'SELECT * FROM ai_style_guide WHERE category_id=0'
        ).fetchone()
    db.close()
    if row:
        return {'tone_ar': row['tone_ar'], 'tone_en': row['tone_en'], 'structure': row['structure']}
    return {'tone_ar': '', 'tone_en': '', 'structure': ''}


def generate_description(product_id: int) -> dict:
    """
    يولّد وصف احترافي ويحفظه كـ draft للمراجعة — لا يُنشر تلقائياً.
    الأدمن يراجع ويعتمد من صفحة ai-describe.
    """
    db = get_db()
    p = db.execute('''
        SELECT p.*, c.id as cat_id, c.name_ar as cat_ar, c.name_en as cat_en,
               s.name_ar as sub_ar, s.name_en as sub_en
        FROM products p
        JOIN categories c ON c.id = p.category_id
        LEFT JOIN subcategories s ON s.id = p.subcategory_id
        WHERE p.id = ?
    ''', (product_id,)).fetchone()
    db.close()

    if not p:
        return {'ok': False, 'error': 'منتج غير موجود'}

    # style guide خاص بالتصنيف أو global
    guide = _get_style_guide(p['cat_id'])

    product_info = f"""اسم المنتج (عربي): {p['name_ar']}
اسم المنتج (إنجليزي): {p['name_en']}
التصنيف: {p['cat_ar']} / {p['sub_ar'] or ''}
الماركة: {p['brand'] or 'غير محدد'}
الفئة العمرية: {f"من {p['min_age_months']} شهر" if p['min_age_months'] else ''} {f"حتى {p['max_age_months']} شهر" if p['max_age_months'] else ''}
الحجم: {p['size_tag'] or ''}
الوسوم الصحية: {p['health_tags'] or ''}
الوصف الحالي (للاستئناس فقط): {p['description_ar'] or 'لا يوجد'}
"""

    system = f"""أنت كاتب محتوى محترف لمتجر مستلزمات حيوانات أليفة في لبنان.

أسلوب الكتابة (عربي): {guide['tone_ar']}
أسلوب الكتابة (إنجليزي): {guide['tone_en']}

بنية الوصف:
{guide['structure']}

مهم: تكيّف مع طبيعة المنتج — منتجات الأكل تركّز على التغذية والمكونات، الإكسسوارات تركّز على المتانة والمقاسات، المستلزمات الصحية على الفائدة الطبية.

ارجع JSON فقط:
{{
  "description_ar": "الوصف العربي",
  "description_en": "English description",
  "benefit_ar": "جملة فايدة قصيرة (أقل من 10 كلمات)",
  "benefit_en": "Short benefit (under 8 words)"
}}"""

    response = _call_groq(
        [{'role': 'system', 'content': system},
         {'role': 'user',   'content': f'اكتب وصفاً لهذا المنتج:\n{product_info}'}],
        temperature=0.35,
        max_tokens=600,
    )

    try:
        start = response.find('{')
        end   = response.rfind('}') + 1
        data  = json.loads(response[start:end])

        # حفظ كـ draft — لا ننشر مباشرة
        db = get_db()
        db.execute('''INSERT INTO product_desc_drafts
                      (product_id, description_ar, description_en, benefit_ar, benefit_en, status)
                      VALUES (?,?,?,?,?,'pending')''',
                   (product_id, data['description_ar'], data['description_en'],
                    data['benefit_ar'], data['benefit_en']))
        draft_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
        db.commit()
        db.close()
        return {'ok': True, 'draft_id': draft_id, **data}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'raw': response}


def approve_description(draft_id: int) -> bool:
    """ينشر الـ draft المعتمد إلى جدول المنتجات."""
    db = get_db()
    draft = db.execute(
        'SELECT * FROM product_desc_drafts WHERE id=? AND status="pending"', (draft_id,)
    ).fetchone()
    if not draft:
        db.close()
        return False
    db.execute('''UPDATE products
                  SET description_ar=?, description_en=?,
                      benefit_ar=?,     benefit_en=?
                  WHERE id=?''',
               (draft['description_ar'], draft['description_en'],
                draft['benefit_ar'],     draft['benefit_en'],
                draft['product_id']))
    db.execute("UPDATE product_desc_drafts SET status='approved' WHERE id=?", (draft_id,))
    db.commit()
    db.close()
    return True


def reject_description(draft_id: int):
    db = get_db()
    db.execute("UPDATE product_desc_drafts SET status='rejected' WHERE id=?", (draft_id,))
    db.commit()
    db.close()


def update_style_guide(category_id: int, tone_ar: str, tone_en: str, structure: str):
    db = get_db()
    db.execute('''INSERT INTO ai_style_guide (category_id, tone_ar, tone_en, structure)
                  VALUES (?,?,?,?)
                  ON CONFLICT(category_id) DO UPDATE SET
                    tone_ar=excluded.tone_ar,
                    tone_en=excluded.tone_en,
                    structure=excluded.structure,
                    updated_at=datetime('now')''',
               (category_id, tone_ar, tone_en, structure))
    db.commit()
    db.close()


# ══════════════════════════════════════════════════════════════
#  4. Upsell ذكي
# ══════════════════════════════════════════════════════════════

def get_upsell(product_id: int) -> list[int]:
    """
    يقترح 6 منتجات تكميلية ويخزنها — الفلترة بالمخزون تصير في الـ API endpoint.
    هيك نضمن cache سريع + مخزون دقيق عند التحميل.
    """
    from datetime import datetime, timedelta
    db = get_db()
    cached = db.execute(
        "SELECT ids_json, updated_at FROM ai_upsell_cache WHERE product_id=?",
        (product_id,)
    ).fetchone()

    if cached:
        updated = datetime.fromisoformat(cached['updated_at'])
        if datetime.now() - updated < timedelta(hours=24):
            db.close()
            try:
                return json.loads(cached['ids_json'])
            except Exception:
                pass

    catalog = _build_catalog_context()
    p = db.execute(
        'SELECT name_ar, name_en FROM products WHERE id=?', (product_id,)
    ).fetchone()
    db.close()

    if not p:
        return []

    system = """أنت مساعد upsell لمتجر حيوانات أليفة.
مهمتك: اقترح 6 منتجات تكمّل المنتج الحالي أو يحتاجها صاحب الحيوان معه.
قواعد:
- لا تكرر المنتج نفسه
- اختر منتجات تُستخدم معاً (أكل + وعاء + مكمل)، أو نفس الفئة العمرية، أو تكمّل الاحتياج
- اقترح 6 (ستة) لأن بعضها قد يكون منتهي المخزون وقت العرض
- ارجع JSON فقط: {"ids": [id1, id2, id3, id4, id5, id6]}"""

    response = _call_groq(
        [{'role': 'system', 'content': system},
         {'role': 'user',
          'content': f'الكاتالوج:\n{catalog}\n\nالمنتج الحالي: {p["name_ar"]} (ID:{product_id})\nاقترح 6 منتجات تكميلية.'}],
        temperature=0.2, max_tokens=160,
    )

    ids = []
    try:
        start = response.find('{')
        end   = response.rfind('}') + 1
        ids   = [int(i) for i in json.loads(response[start:end]).get('ids', [])
                 if str(i).isdigit() and int(i) != product_id][:6]
    except Exception:
        pass

    db = get_db()
    db.execute('''INSERT INTO ai_upsell_cache (product_id, ids_json)
                  VALUES (?,?)
                  ON CONFLICT(product_id) DO UPDATE SET
                    ids_json=excluded.ids_json, updated_at=datetime('now')''',
               (product_id, json.dumps(ids)))
    db.commit()
    db.close()
    return ids


# ══════════════════════════════════════════════════════════════
#  5. تنبيهات المخزون الذكية
# ══════════════════════════════════════════════════════════════

def inventory_alerts(fire_webhooks: bool = False) -> list[dict]:
    """
    يحسب سرعة المبيعات بـ Moving Average آخر 14 يوم (أدق من المتوسط البسيط).
    fire_webhooks=True يُستخدم من cron job فقط لتجنب إرسال webhook مكرر.
    """
    db = get_db()

    # Moving Average: نأخذ آخر 14 يوم — يعكس الزخم الحالي للسوق
    sales = db.execute('''
        SELECT oi.product_id,
               p.name_ar, p.slug, p.stock_qty,
               SUM(oi.qty) as sold_14d
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.created_at >= date('now','-14 days')
          AND o.status NOT IN ('cancelled')
          AND p.is_active = 1
        GROUP BY oi.product_id
        HAVING sold_14d > 0
    ''').fetchall()

    out_of_stock = db.execute('''
        SELECT id, name_ar, slug, stock_qty
        FROM products WHERE is_active=1 AND stock_qty <= 0
    ''').fetchall()

    # webhook URL للإشعارات
    webhook_row = db.execute(
        "SELECT value FROM integration_settings WHERE key='n8n_stock_webhook'"
    ).fetchone()
    webhook_url = webhook_row['value'] if webhook_row else None

    db.close()

    alerts = []

    for p in out_of_stock:
        alerts.append({
            'product_id': p['id'], 'name_ar': p['name_ar'],
            'slug': p['slug'],     'stock_qty': p['stock_qty'],
            'sold_14d': 0,         'days_left': 0,
            'level': 'out',        'label': 'نفد المخزون',
        })

    out_ids = {p['id'] for p in out_of_stock}

    for s in sales:
        if s['product_id'] in out_ids:
            continue
        daily_rate = s['sold_14d'] / 14   # Moving Average 14 يوم
        days_left  = int(s['stock_qty'] / daily_rate) if daily_rate > 0 else 999

        if days_left > 30:
            continue

        level = 'critical' if days_left <= 7 else 'warning'
        label = f'يكفي {days_left} يوم'

        alerts.append({
            'product_id': s['product_id'], 'name_ar': s['name_ar'],
            'slug':       s['slug'],       'stock_qty': s['stock_qty'],
            'sold_14d':   s['sold_14d'],   'days_left': days_left,
            'level':      level,           'label': label,
        })

    alerts.sort(key=lambda x: x['days_left'])

    # إرسال webhook للحرجين — فقط عند الطلب الصريح (cron)
    if fire_webhooks and webhook_url:
        critical = [a for a in alerts if a['level'] in ('out', 'critical')]
        if critical:
            import threading
            def _fire():
                try:
                    import urllib.request, json as _json
                    body = _json.dumps({'event': 'low_stock', 'items': critical}).encode()
                    req  = urllib.request.Request(
                        webhook_url, data=body,
                        headers={'Content-Type': 'application/json'},
                        method='POST'
                    )
                    urllib.request.urlopen(req, timeout=5)
                except Exception:
                    pass
            threading.Thread(target=_fire, daemon=True).start()

    return alerts


# ══════════════════════════════════════════════════════════════
#  6. بناء الحملات الإعلانية
# ══════════════════════════════════════════════════════════════

def campaign_ideas() -> str:
    """
    يحلل بيانات نملكها 100% (سلل متروكة + multi-visit) لاقتراح حملات دقيقة.
    """
    db = get_db()

    # 1. منتجات أُضيفت للسلة بدون شراء (High-Intent — الأهم)
    abandoned_cart = db.execute('''
        SELECT p.name_ar, p.price, p.discount_price, COUNT(*) as cnt
        FROM analytics_events e
        JOIN products p ON p.id = e.product_id
        WHERE e.event_type = 'add_to_cart'
          AND e.session_id NOT IN (
            SELECT DISTINCT session_id FROM analytics_events
            WHERE event_type = 'purchase'
          )
          AND e.created_at >= date('now','-14 days')
        GROUP BY e.product_id
        ORDER BY cnt DESC LIMIT 5
    ''').fetchall()

    # 2. منتجات شافها نفس الشخص أكثر من مرتين في نفس الجلسة (نية شراء عالية جداً)
    multi_visit = db.execute('''
        SELECT p.name_ar, COUNT(*) as sessions
        FROM (
            SELECT session_id, product_id, COUNT(*) as views
            FROM analytics_events
            WHERE event_type = 'product_view'
              AND created_at >= date('now','-14 days')
            GROUP BY session_id, product_id
            HAVING views >= 2
        ) mv
        JOIN products p ON p.id = mv.product_id
        GROUP BY mv.product_id
        ORDER BY sessions DESC LIMIT 5
    ''').fetchall()

    # 3. منتجات مباعة بقوة (بيانات حقيقية من الطلبات)
    bestsellers = db.execute('''
        SELECT p.name_ar, SUM(oi.qty) as sold
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.created_at >= date('now','-30 days')
          AND o.status NOT IN ('cancelled')
        GROUP BY oi.product_id ORDER BY sold DESC LIMIT 5
    ''').fetchall()

    # 4. منتجات قرب تنفد (فرصة urgency)
    alerts   = inventory_alerts()
    low_stock = [(a['name_ar'], a['days_left']) for a in alerts
                 if a['level'] in ('critical', 'warning')][:3]

    db.close()

    def fmt(rows, key='name_ar', extra=None):
        if not rows:
            return 'لا يوجد بيانات'
        parts = []
        for r in rows:
            s = r[key]
            if extra and r[extra]:
                s += f' ({r[extra]})'
            parts.append(s)
        return ', '.join(parts)

    context = f"""بيانات المتجر (مضمونة 100% من قاعدة بياناتنا، آخر 14-30 يوم):

🛒 أُضيفوا للسلة ولم يُشتروا (High-Intent):
{chr(10).join(f"  • {r['name_ar']} — {r['cnt']} مرة (سعر: ${r['discount_price'] or r['price']})" for r in abandoned_cart) or '  لا يوجد'}

👁️ نفس الشخص فتح المنتج مرتين أو أكثر (نية شراء عالية جداً):
{chr(10).join(f"  • {r['name_ar']} — {r['sessions']} جلسة" for r in multi_visit) or '  لا يوجد'}

🏆 الأكثر مبيعاً هالشهر:
{fmt(bestsellers, 'name_ar', 'sold')}

⚠️ منتجات قرب تنفد (فرصة Urgency):
{chr(10).join(f"  • {n} ({d} يوم متبقي)" for n, d in low_stock) or '  لا يوجد'}"""

    cache_key = _cache_key('campaigns', context[:300])
    cached    = _cache_get(cache_key)
    if cached:
        return cached

    response = _call_groq([
        {'role': 'system', 'content': '''أنت مستشار تسويق رقمي لمتجر حيوانات أليفة في لبنان.
بناءً على بيانات المتجر، اقترح 3 حملات إعلانية محددة وقابلة للتنفيذ.
لكل حملة:
- العنوان
- الهدف (زيادة مبيعات منتج معين / استهداف منطقة / إعادة استهداف)
- المنتج أو المنتجات المقترحة
- الجمهور المستهدف
- وقت أفضل للنشر
كن محدداً وعملياً. بالعربية.'''},
        {'role': 'user', 'content': context},
    ], temperature=0.4, max_tokens=600)

    if response:
        _cache_set(cache_key, response)
    return response or 'تعذّر توليد الحملات، حاول لاحقاً.'


# ══════════════════════════════════════════════════════════════
#  7. تحليل الطلبات (Insights للأدمن)
# ══════════════════════════════════════════════════════════════

def analyze_orders() -> str:
    """يحلل آخر 30 يوم من الطلبات ويعطي insights للأدمن."""
    db = get_db()
    orders = db.execute('''
        SELECT o.id, o.area, o.total, o.status, o.created_at,
               GROUP_CONCAT(p.name_ar || ' x' || oi.qty) as items
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        WHERE o.created_at >= date('now', '-30 days')
        GROUP BY o.id
        ORDER BY o.created_at DESC
    ''').fetchall()
    db.close()

    if not orders:
        return 'لا يوجد طلبات خلال الـ 30 يوم الماضية.'

    summary = '\n'.join([
        f'طلب#{o["id"]} | {o["area"]} | ${o["total"]:.2f} | {o["status"]} | {o["items"]}'
        for o in orders
    ])

    cache_key = _cache_key('analyze', summary[:200])
    cached = _cache_get(cache_key)
    if cached:
        return cached

    response = _call_groq([
        {'role': 'system', 'content': 'أنت محلل بيانات لمتجر حيوانات أليفة. حلّل الطلبات وأعط insights مفيدة للأدمن باللغة العربية. كن مختصراً ومباشراً.'},
        {'role': 'user',   'content': f'طلبات آخر 30 يوم:\n{summary}\n\nأعطني: أكثر المنتجات مبيعاً، أكثر المناطق طلباً، أي أنماط مثيرة للاهتمام، وتوصية واحدة.'},
    ], temperature=0.3, max_tokens=400)

    if response:
        _cache_set(cache_key, response)
    return response or 'تعذّر التحليل، حاول لاحقاً.'


# ══════════════════════════════════════════════════════════════
#  8. مولّد المقترحات — كل شي يمر على الأدمن قبل التنفيذ
# ══════════════════════════════════════════════════════════════

def generate_suggestions() -> int:
    """
    يحلل المتجر ويولّد مقترحات جديدة بالـ DB.
    لا ينفّذ أي شي — الأدمن يقرر.
    يرجع عدد المقترحات الجديدة.
    """
    # ── المرحلة 1: اجمع البيانات وأغلق DB قبل أي AI call ───────
    db = get_db()

    slow = db.execute("""
        SELECT p.id, p.name_ar, p.price, p.stock_qty,
               COALESCE(SUM(oi.qty),0) as sold_14d
        FROM products p
        LEFT JOIN order_items oi ON oi.product_id=p.id
        LEFT JOIN orders o ON o.id=oi.order_id
            AND o.created_at >= date('now','-14 days')
            AND o.status NOT IN ('cancelled')
        WHERE p.is_active=1 AND p.stock_qty >= 10 AND p.discount_price IS NULL
        GROUP BY p.id
        HAVING sold_14d <= 2
        ORDER BY p.stock_qty DESC LIMIT 5
    """).fetchall()

    slow_existing = {
        r[0] for r in db.execute(
            "SELECT action_data FROM ai_suggestions WHERE type='promo' AND status='pending'"
        ).fetchall()
        if r[0]
    }

    dormant = db.execute("""
        SELECT o.customer_name, o.phone, MAX(o.created_at) as last_order,
               COUNT(DISTINCT o.id) as order_count,
               GROUP_CONCAT(DISTINCT p.name_ar) as products
        FROM orders o
        JOIN order_items oi ON oi.order_id=o.id
        JOIN products p ON p.id=oi.product_id
        WHERE o.status='delivered'
          AND o.created_at < date('now','-30 days')
          AND o.phone NOT IN (
            SELECT phone FROM orders WHERE created_at >= date('now','-30 days')
          )
        GROUP BY o.phone
        ORDER BY last_order DESC LIMIT 5
    """).fetchall()

    dormant_phones = {
        json.loads(r[0]).get('phone','') for r in db.execute(
            "SELECT action_data FROM ai_suggestions WHERE type='reactivate' AND status='pending'"
        ).fetchall() if r[0]
    }

    reviews_summary = db.execute("""
        SELECT p.name_ar, AVG(pr.rating) as avg_r, COUNT(*) as cnt
        FROM product_reviews pr
        JOIN products p ON p.id=pr.product_id
        GROUP BY pr.product_id
        HAVING cnt >= 1
        ORDER BY cnt DESC LIMIT 10
    """).fetchall()

    has_review_insight = db.execute(
        "SELECT id FROM ai_suggestions WHERE type='review_insight' AND status='pending'"
    ).fetchone()

    days_map = {}
    for c in dormant:
        row = db.execute(
            "SELECT CAST(julianday('now') - julianday(?) AS INTEGER) as d", (c['last_order'],)
        ).fetchone()
        days_map[c['phone']] = row['d'] if row else 0

    db.close()  # أغلق DB قبل أي AI call

    # ── المرحلة 2: AI calls (بدون DB مفتوح) ────────────────────
    to_insert = []

    for p in slow:
        pid = p['id']
        if any(f'"product_id": {pid}' in s for s in slow_existing):
            continue
        suggested_price = round(p['price'] * 0.85, 2)
        reason = _call_groq([
            {'role': 'system', 'content': 'أنت مستشار تسويق. اكتب جملة واحدة فقط (أقل من 20 كلمة) لماذا يجب خفض سعر هذا المنتج.'},
            {'role': 'user',   'content': f'المنتج: {p["name_ar"]} | سعر: ${p["price"]} | مخزون: {p["stock_qty"]} | مبيع 14 يوم: {p["sold_14d"]}'},
        ], temperature=0.3, max_tokens=60)
        to_insert.append(('promo',
            f'⬇️ خفّض سعر "{p["name_ar"]}"',
            f'{reason or "مخزون عالٍ ومبيعات بطيئة."}\n\n'
            f'السعر الحالي: ${p["price"]:.2f} | المقترح: ${suggested_price:.2f} (خصم 15%)\n'
            f'المخزون: {p["stock_qty"]} قطعة | مبيع آخر 14 يوم: {p["sold_14d"]}',
            json.dumps({'product_id': pid, 'discount_price': suggested_price})))

    for c in dormant:
        if c['phone'] in dormant_phones:
            continue
        days_ago = days_map.get(c['phone'], 0)
        msg = _call_groq([
            {'role': 'system', 'content': 'اكتب رسالة واتساب قصيرة ودية بالعربية اللبنانية لإعادة استهداف زبون. جملتان فقط.'},
            {'role': 'user',   'content': f'الزبون: {c["customer_name"]} | غاب: {days_ago} يوم | اشترى: {c["products"]}'},
        ], temperature=0.5, max_tokens=100)
        wa_msg = msg or 'أهلاً، وحشتنا! في منتجات جديدة تناسب حيوانك 🐾'
        to_insert.append(('reactivate',
            f'📱 راسل "{c["customer_name"]}" — غايب {days_ago} يوم',
            f'آخر طلب: {c["last_order"][:10]} | طلبات سابقة: {c["order_count"]}\n'
            f'اشترى: {c["products"]}\n\n💬 الرسالة المقترحة:\n{wa_msg}',
            json.dumps({'phone': c['phone'], 'name': c['customer_name'], 'wa_message': wa_msg})))

    if reviews_summary and not has_review_insight:
        data = '\n'.join([f'{r["name_ar"]}: {r["avg_r"]:.1f}★ ({r["cnt"]} تقييم)' for r in reviews_summary])
        insight = _call_groq([
            {'role': 'system', 'content': 'حلّل تقييمات المنتجات وأعط 3 ملاحظات مفيدة للأدمن. كن مختصراً.'},
            {'role': 'user',   'content': data},
        ], temperature=0.3, max_tokens=200)
        if insight:
            to_insert.append(('review_insight', '⭐ تحليل تقييمات المنتجات', insight, None))

    # ── المرحلة 3: أدخل النتائج بـ DB جديد ──────────────────────
    if to_insert:
        db = get_db()
        for row in to_insert:
            db.execute(
                "INSERT INTO ai_suggestions (type, title, body, action_data) VALUES (?,?,?,?)",
                row
            )
        db.commit()
        db.close()

    return len(to_insert)
