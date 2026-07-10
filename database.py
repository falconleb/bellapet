import sqlite3
from werkzeug.security import generate_password_hash

import config


def get_db():
    """فتح اتصال جديد بقاعدة البيانات لكل طلب."""
    conn = sqlite3.connect(config.DATABASE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name_en TEXT NOT NULL,
    name_ar TEXT NOT NULL,
    icon TEXT,
    sort_order INTEGER DEFAULT 0,
    card_size TEXT DEFAULT 'small'   -- 'large' أو 'small' لتحديد حجم البطاقة بالرئيسية
);

CREATE TABLE IF NOT EXISTS subcategories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    name_en TEXT NOT NULL,
    name_ar TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    UNIQUE(category_id, slug)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    subcategory_id INTEGER REFERENCES subcategories(id),
    slug TEXT UNIQUE NOT NULL,
    name_en TEXT NOT NULL,
    name_ar TEXT NOT NULL,
    brand TEXT,
    benefit_en TEXT,                  -- جملة الفايدة الأساسية (تستخدم كعنوان بصفحة المنتج)
    benefit_ar TEXT,
    description_en TEXT,
    description_ar TEXT,
    price REAL NOT NULL,
    discount_price REAL,              -- يترك NULL لو ما في عرض
    stock_qty INTEGER DEFAULT 0,
    is_consumable INTEGER DEFAULT 0,  -- 1 = طعام/رمل، يفعّل حاسبة المدة
    consumption_grams_per_kg_day REAL,-- غرام/كغ من وزن الحيوان باليوم (للحاسبة)
    package_weight_grams REAL,        -- وزن العبوة بالغرام (للحاسبة)
    min_age_months INTEGER,
    max_age_months INTEGER,
    size_tag TEXT,                    -- small / medium / large / all
    health_tags TEXT,                 -- مفصولة بفاصلة: allergy,urinary,weight...
    is_featured INTEGER DEFAULT 0,    -- يظهر بقسم "منتجات مختارة بعناية"
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_product_images_pid_sort ON product_images(product_id, sort_order);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    area TEXT NOT NULL,
    address_note TEXT,
    status TEXT DEFAULT 'new',        -- new / confirmed / shipped / delivered / cancelled
    total REAL NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    price_at_order REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS redirects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_path TEXT UNIQUE NOT NULL,
    to_path TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS blog_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title_en TEXT NOT NULL,
    title_ar TEXT NOT NULL,
    content_en TEXT,
    content_ar TEXT,
    image TEXT,
    is_published INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_variants (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    variant_type   TEXT NOT NULL,   -- weight | color | scent | size | custom
    type_label_ar  TEXT NOT NULL,   -- "الوزن" / "اللون" / "الرائحة" / "الحجم" / مخصص
    type_label_en  TEXT NOT NULL,
    value_ar       TEXT NOT NULL,   -- "1 كغ" / "أحمر" / "لافندر"
    value_en       TEXT NOT NULL,
    price_modifier REAL DEFAULT 0,  -- +/- فوق السعر الأساسي (0 = نفس السعر)
    stock_qty      INTEGER DEFAULT 0,
    sku            TEXT,
    image_filename TEXT,            -- صورة خاصة بهذا المتغير (اختياري)
    sort_order     INTEGER DEFAULT 0,
    is_active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name_ar TEXT NOT NULL,
    name_en TEXT NOT NULL,
    description_ar TEXT,
    description_en TEXT,
    is_active INTEGER DEFAULT 1,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_products (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    product_id    INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    sort_order    INTEGER DEFAULT 0,
    PRIMARY KEY (collection_id, product_id)
);

CREATE TABLE IF NOT EXISTS product_price_tiers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id     INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    min_qty        INTEGER NOT NULL,          -- ابتداءً من هالكمية ينطبق السعر
    price_per_unit REAL    NOT NULL,          -- سعر الوحدة عند هالكمية
    label_ar       TEXT,                      -- "على القطعتين"
    label_en       TEXT,
    sort_order     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS analytics_sessions (
    id           TEXT PRIMARY KEY,
    started_at   TEXT DEFAULT (datetime('now')),
    last_seen    TEXT DEFAULT (datetime('now')),
    device_type  TEXT,   -- mobile | tablet | desktop
    os           TEXT,   -- iOS | Android | Windows | Mac | Linux
    browser      TEXT,   -- Chrome | Safari | Firefox ...
    screen       TEXT,   -- "390x844"
    language     TEXT,
    referrer     TEXT,   -- الدومين المحيل
    utm_source   TEXT,
    utm_medium   TEXT,
    utm_campaign TEXT,
    landing_page TEXT,
    page_count   INTEGER DEFAULT 0,
    converted    INTEGER DEFAULT 0,
    order_id     INTEGER
);

CREATE TABLE IF NOT EXISTS analytics_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    -- page_view | product_view | search | add_to_cart
    -- begin_checkout | purchase | cart_abandon
    page         TEXT,
    product_id   INTEGER,
    product_slug TEXT,
    search_query TEXT,
    extra        TEXT,   -- JSON للبيانات الإضافية
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_session  ON analytics_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_type     ON analytics_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_product  ON analytics_events(product_id);
CREATE INDEX IF NOT EXISTS idx_events_created  ON analytics_events(created_at);

CREATE TABLE IF NOT EXISTS ai_cache (
    cache_key  TEXT PRIMARY KEY,
    result     TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ai_style_guide (
    category_id INTEGER PRIMARY KEY,   -- 0 = global fallback
    tone_ar     TEXT NOT NULL,
    tone_en     TEXT NOT NULL,
    structure   TEXT NOT NULL,
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ai_upsell_cache (
    product_id  INTEGER PRIMARY KEY,
    ids_json    TEXT NOT NULL,          -- يخزن 6 IDs، الفلترة تصير عند التحميل
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product_desc_drafts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id   INTEGER NOT NULL,
    description_ar TEXT,
    description_en TEXT,
    benefit_ar   TEXT,
    benefit_en   TEXT,
    status       TEXT DEFAULT 'pending', -- pending | approved | rejected
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT UNIQUE NOT NULL,
    label      TEXT,
    is_active  INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    last_used  TEXT
);

CREATE TABLE IF NOT EXISTS rate_limit_log (
    key TEXT NOT NULL,
    ts  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rl_key_ts ON rate_limit_log(key, ts);

CREATE TABLE IF NOT EXISTS integration_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- مفاتيح مستخدمة:
--   n8n_order_webhook   : URL يُستدعى عند كل طلب جديد
--   n8n_status_webhook  : URL يُستدعى عند تغيير حالة طلب
--   webhook_secret      : مفتاح سري للـ incoming webhooks من n8n

CREATE TABLE IF NOT EXISTS homepage_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS category_card_images (
    category_slug TEXT PRIMARY KEY,
    filename      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS homepage_sections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id TEXT UNIQUE NOT NULL,   -- offers | featured | blog | why
    sort_order INTEGER DEFAULT 0,
    is_visible INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS content_pages (
    slug       TEXT PRIMARY KEY,  -- 'about' | 'contact' | 'returns'
    title_ar   TEXT NOT NULL DEFAULT '',
    title_en   TEXT NOT NULL DEFAULT '',
    body_ar    TEXT NOT NULL DEFAULT '',
    body_en    TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cart_promotions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_type       TEXT NOT NULL,  -- free_shipping | free_gift | social_follow
    threshold_amount REAL DEFAULT 0, -- الحد الأدنى للفاتورة لتتفعل
    title_ar         TEXT NOT NULL,
    title_en         TEXT NOT NULL,
    progress_ar      TEXT,           -- نص الشريط قبل الوصول: "أضف $X وبتاخد شحن مجاني"
    progress_en      TEXT,
    unlocked_ar      TEXT,           -- نص لما يوصل: "🎉 شحن مجاني مفعّل!"
    unlocked_en      TEXT,
    social_url       TEXT,           -- للسوشل فوللو
    is_active        INTEGER DEFAULT 1,
    sort_order       INTEGER DEFAULT 0,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS promo_gift_options (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_id    INTEGER NOT NULL REFERENCES cart_promotions(id) ON DELETE CASCADE,
    name_ar     TEXT NOT NULL,
    name_en     TEXT NOT NULL,
    is_active   INTEGER DEFAULT 1,
    sort_order  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_status_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    status     TEXT NOT NULL,
    note       TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS product_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id),
    rating     INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(order_id, product_id)
);

CREATE TABLE IF NOT EXISTS customer_perks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    phone            TEXT NOT NULL,
    customer_name    TEXT,
    perk_type        TEXT NOT NULL,   -- free_shipping | discount_pct | discount_fixed | voucher | blocked
    perk_value       TEXT,            -- رقم % أو مبلغ ثابت أو قيمة القسيمة بالدولار
    note             TEXT,            -- ملاحظة داخلية للأدمن
    expires_at       TEXT,            -- NULL = لا تنتهي
    condition_type   TEXT,            -- min_order | before_date | social | NULL = بلا شرط
    condition_value  TEXT,            -- المبلغ الأدنى أو التاريخ أو رابط/تعليمات السوشل
    created_at       TEXT DEFAULT (datetime('now')),
    UNIQUE(phone)
);

CREATE TABLE IF NOT EXISTS stock_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    phone      TEXT NOT NULL,
    name       TEXT,
    notified   INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sn_product ON stock_notifications(product_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sn_phone_product ON stock_notifications(product_id, phone);

CREATE TABLE IF NOT EXISTS automation_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ok',  -- ok | error | skipped
    summary    TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pwa_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event      TEXT NOT NULL,  -- 'installed' | 'launch'
    ua         TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint   TEXT NOT NULL UNIQUE,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    phone      TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shipping_zones (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name_ar    TEXT NOT NULL UNIQUE,
    name_en    TEXT NOT NULL DEFAULT '',
    fee        REAL NOT NULL DEFAULT 4.0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id        INTEGER NOT NULL REFERENCES products(id),
    variant_id        INTEGER REFERENCES product_variants(id),
    qty               INTEGER DEFAULT 1,
    interval_days     INTEGER NOT NULL,
    customer_name     TEXT NOT NULL,
    phone             TEXT NOT NULL,
    area              TEXT,
    notes             TEXT,
    status            TEXT DEFAULT 'active',   -- active | paused | cancelled
    current_status    TEXT DEFAULT 'pending',  -- pending | notified | confirmed | delivered | skipped
    next_renewal_date TEXT NOT NULL,
    batch_id          TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_subs_phone  ON subscriptions(phone);
CREATE INDEX IF NOT EXISTS idx_subs_status ON subscriptions(status, next_renewal_date);

CREATE TABLE IF NOT EXISTS subscription_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    renewal_date    TEXT NOT NULL,
    action          TEXT NOT NULL,  -- push_sent | whatsapp_sent | confirmed | delivered | skipped
    note            TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS seo_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_type TEXT NOT NULL,   -- 'product' | 'category' | 'blog' | 'static'
    page_id   INTEGER,         -- FK to products/categories/blog_posts (NULL for static)
    page_slug TEXT,            -- للصفحات الثابتة (home, about...)
    meta_title_ar    TEXT,
    meta_title_en    TEXT,
    meta_desc_ar     TEXT,
    meta_desc_en     TEXT,
    keywords_ar      TEXT,     -- مفصولة بفاصلة
    keywords_en      TEXT,
    og_title         TEXT,
    og_description   TEXT,
    generated_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(page_type, page_id),
    UNIQUE(page_type, page_slug)
);
"""

# الفئات الخمسة المتفق عليها: slug, name_en, name_ar, icon, sort_order, card_size
DEFAULT_CATEGORIES = [
    ("dogs", "Dogs", "كلاب", "dog", 1, "large"),
    ("cats", "Cats", "قطط", "cat", 2, "large"),
    ("birds", "Birds", "طيور", "bird", 3, "small"),
    ("fish", "Fish", "أسماك", "fish", 4, "small"),
    ("small-pets", "Small Pets", "حيوانات صغيرة", "rabbit", 5, "small"),
]

# (category_slug, slug, name_en, name_ar, sort_order)
DEFAULT_SUBCATEGORIES = [
    ("dogs",       "food",        "Food",         "أكل",          1),
    ("dogs",       "treats",      "Treats",       "مكافآت",       2),
    ("dogs",       "beds",        "Beds",         "أسرة",         3),
    ("dogs",       "accessories", "Accessories",  "إكسسوارات",    4),
    ("cats",       "food",        "Food",         "أكل",          1),
    ("cats",       "litter",      "Litter",       "رمل",          2),
    ("cats",       "toys",        "Toys",         "ألعاب",        3),
    ("cats",       "accessories", "Accessories",  "إكسسوارات",    4),
    ("birds",      "food",        "Seeds & Food", "بذور وأكل",    1),
    ("birds",      "cages",       "Cages",        "أقفاص",        2),
    ("birds",      "accessories", "Accessories",  "إكسسوارات",    3),
    ("fish",       "food",        "Food",         "أكل",          1),
    ("fish",       "aquarium",    "Aquarium",     "أدوات الحوض",  2),
    ("small-pets", "food",        "Food",         "أكل",          1),
    ("small-pets", "cages",       "Cages",        "أقفاص",        2),
    ("small-pets", "accessories", "Accessories",  "إكسسوارات",    3),
]


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    # ai suggestions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_suggestions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            type        TEXT NOT NULL,
            title       TEXT NOT NULL,
            body        TEXT NOT NULL,
            action_data TEXT,
            status      TEXT DEFAULT 'pending',
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # short links table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS short_links (
            code       TEXT PRIMARY KEY,
            target_url TEXT NOT NULL,
            label      TEXT,
            clicks     INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    # index صريح على code (PRIMARY KEY يعمل index تلقائي — هاد للتوضيح)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_short_links_code ON short_links(code)")

    # campaign archive table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS campaign_archive (
            campaign TEXT PRIMARY KEY,
            archived_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # migrations — analytics_sessions
    for col, dfn in [
        ('user_id',      'TEXT'),
        ('duration_sec', 'INTEGER DEFAULT 0'),
    ]:
        try:
            cur.execute(f'ALTER TABLE analytics_sessions ADD COLUMN {col} {dfn}')
        except Exception:
            pass
    try:
        cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_user ON analytics_sessions(user_id)')
    except Exception:
        pass

    # migrations — orders
    for col, dfn in [
        ('gift_note',       'TEXT'),
        ('review_token',    'TEXT'),
        ('tracking_number', 'TEXT'),
        ('delivery_fee',    'REAL NOT NULL DEFAULT 0'),
    ]:
        try:
            cur.execute(f'ALTER TABLE orders ADD COLUMN {col} {dfn}')
        except Exception:
            pass

    # migrations — customer_perks
    for col, definition in [
        ('condition_type',    'TEXT'),
        ('condition_value',   'TEXT'),
        ('customer_name',     'TEXT'),
        ('behavior_rating',   'INTEGER'),
        ('behavior_note',     'TEXT'),
    ]:
        try:
            cur.execute(f'ALTER TABLE customer_perks ADD COLUMN {col} {definition}')
        except Exception:
            pass

    # migrations — حقول جديدة على products
    for col, definition in [
        ('is_bundle',       'INTEGER DEFAULT 0'),
        ('bundle_note_ar',  'TEXT'),
        ('bundle_note_en',  'TEXT'),
        ('promo_type',      'TEXT'),
        ('promo_label_ar',  'TEXT'),
        ('promo_label_en',  'TEXT'),
        ('store_rating',    'INTEGER'),
        ('rating_note_ar',  'TEXT'),
        ('rating_note_en',  'TEXT'),
        ('suitable_for_ar', 'TEXT'),
        ('suitable_for_en', 'TEXT'),
        ('rating_cons_ar',  'TEXT'),
        ('rating_cons_en',  'TEXT'),
    ]:
        try:
            cur.execute(f'ALTER TABLE products ADD COLUMN {col} {definition}')
        except Exception:
            pass

    # migration: ai_style_guide — إذا الجدول لسا عنده id بدل category_id، نحوّله
    cols = [r[1] for r in cur.execute("PRAGMA table_info(ai_style_guide)").fetchall()]
    if 'id' in cols and 'category_id' not in cols:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_style_guide_new (
                category_id INTEGER PRIMARY KEY,
                tone_ar     TEXT NOT NULL,
                tone_en     TEXT NOT NULL,
                structure   TEXT NOT NULL,
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        cur.execute("""
            INSERT INTO ai_style_guide_new (category_id, tone_ar, tone_en, structure, updated_at)
            SELECT 0, tone_ar, tone_en, structure, updated_at FROM ai_style_guide LIMIT 1
        """)
        cur.execute("DROP TABLE ai_style_guide")
        cur.execute("ALTER TABLE ai_style_guide_new RENAME TO ai_style_guide")

    # seed default AI style guide (category_id=0 = global)
    cur.execute("SELECT COUNT(*) FROM ai_style_guide WHERE category_id=0")
    if cur.fetchone()[0] == 0:
        cur.execute("""INSERT INTO ai_style_guide (category_id, tone_ar, tone_en, structure) VALUES (0, ?, ?, ?)""", (
            "ودّي ومباشر، عربية بسيطة وقريبة، لا رسمية مفرطة ولا عامية ثقيلة. ابدأ بالفايدة مباشرة بدون مقدمات.",
            "Warm, clear, and direct. Professional yet friendly. No fluff or filler words.",
            """بناء كل وصف هيك بالترتيب:
1. جملة افتتاحية: الفايدة الرئيسية مباشرة (ليش هالمنتج مميز)
2. مناسب لـ: نوع الحيوان والعمر إذا ينطبق
3. مكوّن أو مواصفة بارزة واحدة فقط
4. جملة ختامية تحفيزية قصيرة
طول الوصف العربي: 60-80 كلمة
طول الوصف الإنجليزي: 50-65 كلمة
لا تذكر السعر. لا تكرر اسم المنتج أكثر من مرة."""
        ))

    # seed content pages
    for slug, title_ar, title_en in [
        ('about',   'عن متجرنا',       'About Us'),
        ('contact', 'تواصل معنا',       'Contact Us'),
        ('returns', 'سياسة الإرجاع والاستبدال', 'Return & Exchange Policy'),
    ]:
        cur.execute("INSERT OR IGNORE INTO content_pages (slug, title_ar, title_en) VALUES (?,?,?)",
                    (slug, title_ar, title_en))

    # seed default homepage sections if empty
    cur.execute("SELECT COUNT(*) FROM homepage_sections")
    if cur.fetchone()[0] == 0:
        for i, sid in enumerate(["offers", "featured", "blog", "why"]):
            cur.execute(
                "INSERT OR IGNORE INTO homepage_sections (section_id, sort_order, is_visible) VALUES (?,?,1)",
                (sid, i)
            )

    cur.execute("SELECT COUNT(*) FROM categories")
    if cur.fetchone()[0] == 0:
        cur.executemany(
            "INSERT INTO categories (slug, name_en, name_ar, icon, sort_order, card_size) "
            "VALUES (?,?,?,?,?,?)",
            DEFAULT_CATEGORIES,
        )

    cat_ids = {row["slug"]: row["id"] for row in cur.execute("SELECT id, slug FROM categories")}
    for cat_slug, slug, name_en, name_ar, sort_order in DEFAULT_SUBCATEGORIES:
        cat_id = cat_ids.get(cat_slug)
        if cat_id:
            cur.execute(
                "INSERT OR IGNORE INTO subcategories (category_id, slug, name_en, name_ar, sort_order) "
                "VALUES (?,?,?,?,?)",
                (cat_id, slug, name_en, name_ar, sort_order),
            )

    # shipping_zones — seed الأقضية اللبنانية (INSERT OR IGNORE حتى لا نمسح تعديلات الأدمن)
    cur.execute("CREATE TABLE IF NOT EXISTS shipping_zones (id INTEGER PRIMARY KEY AUTOINCREMENT, name_ar TEXT NOT NULL UNIQUE, name_en TEXT NOT NULL DEFAULT '', fee REAL NOT NULL DEFAULT 4.0, enabled INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0)")
    # migration: add name_en column if missing
    cols = [r[1] for r in cur.execute("PRAGMA table_info(shipping_zones)").fetchall()]
    if 'name_en' not in cols:
        cur.execute("ALTER TABLE shipping_zones ADD COLUMN name_en TEXT NOT NULL DEFAULT ''")
    _zones = [
        ("بيروت",           "Beirut",            4.0, 0),
        ("المتن",           "Metn",              4.0, 1),
        ("بعبدا",           "Baabda",            4.0, 2),
        ("كسروان",          "Keserwan",          4.0, 3),
        ("الشوف",           "Chouf",             4.0, 4),
        ("عاليه",           "Aley",              4.0, 5),
        ("جبيل",            "Jbeil",             4.0, 6),
        ("طرابلس",          "Tripoli",           4.0, 7),
        ("البترون",         "Batroun",           4.0, 8),
        ("زغرتا",           "Zgharta",           4.0, 9),
        ("الكورة",          "Koura",             4.0, 10),
        ("بشري",            "Bsharri",           4.0, 11),
        ("المنية - الضنية", "Miniyeh-Danniyeh",  4.0, 12),
        ("عكار",            "Akkar",             4.0, 13),
        ("صيدا",            "Saida",             4.0, 14),
        ("صور",             "Tyre",              4.0, 15),
        ("النبطية",         "Nabatieh",          4.0, 16),
        ("بنت جبيل",        "Bint Jbeil",        4.0, 17),
        ("مرجعيون",         "Marjeyoun",         4.0, 18),
        ("حاصبيا",          "Hasbaya",           4.0, 19),
        ("زحلة",            "Zahle",             4.0, 20),
        ("البقاع الغربي",   "West Bekaa",        4.0, 21),
        ("راشيا",           "Rashaya",           4.0, 22),
        ("بعلبك - الهرمل",  "Baalbek-Hermel",    4.0, 23),
    ]
    for _ar, _en, _fee, _sort in _zones:
        cur.execute("INSERT OR IGNORE INTO shipping_zones (name_ar, name_en, fee, sort_order) VALUES (?,?,?,?)", (_ar, _en, _fee, _sort))
        cur.execute("UPDATE shipping_zones SET name_en=? WHERE name_ar=? AND (name_en='' OR name_en IS NULL)", (_en, _ar))

    cur.execute("SELECT COUNT(*) FROM admin_users")
    if cur.fetchone()[0] == 0:
        cur.execute(
            "INSERT INTO admin_users (username, password_hash) VALUES (?,?)",
            ("admin", generate_password_hash("changeme123")),
        )

    for _idx in [
        "CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id)",
        "CREATE INDEX IF NOT EXISTS idx_products_active   ON products(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_products_featured ON products(is_featured)",
        "CREATE INDEX IF NOT EXISTS idx_orders_phone      ON orders(phone)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status     ON orders(status)",
    ]:
        cur.execute(_idx)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# بيانات تجريبية (Demo) فقط لمعاينة شكل التصميم على الصفحة الرئيسية وصفحات
# المنتج قبل إدخال الكاتالوج الحقيقي عبر لوحة التحكم. ممكن حذفها بأي وقت.
# ---------------------------------------------------------------------------
DEMO_PRODUCTS = [
    # category_slug, subcat, slug, name_en, name_ar, brand,
    # benefit_en, benefit_ar, desc_en, desc_ar,
    # price, discount_price, stock, is_consumable, cons_g_kg_day, pkg_g,
    # min_age_m, max_age_m, size_tag, health_tags, is_featured
    ("cats", "food", "demo-cat-food-salmon", "Adult Cat Dry Food - Salmon",
     "طعام جاف للقطط البالغة - سالمون", "Whiskas",
     "Supports a shiny coat and healthy digestion",
     "يدعم لمعان الشعر وصحة الجهاز الهضمي",
     "Complete dry food formulated for adult cats with real salmon as the first ingredient.",
     "طعام جاف كامل ومتوازن للقطط البالغة، يحتوي على سالمون حقيقي كمكون أساسي.",
     14.5, None, 24, 1, 35, 1500, 12, None, "all", "weight", 1),

    ("cats", "litter", "demo-cat-litter-tofu", "Tofu Cat Litter - Unscented",
     "رمل قطط من التوفو - بدون رائحة", "CatZen",
     "Clumps instantly and controls odor naturally",
     "يتكتل فوراً ويتحكم بالرائحة بشكل طبيعي",
     "Flushable tofu-based litter, dust-free and gentle on paws.",
     "رمل من التوفو قابل للتصريف، خفيف على مخالب القطط وخالي من الغبار.",
     9.0, 7.5, 40, 1, None, 6000, None, None, "all", "", 1),

    ("dogs", "beds", "demo-dog-bed-orthopedic", "Orthopedic Dog Bed - Medium",
     "سرير طبي للكلاب - حجم متوسط", "ComfyPaws",
     "Eases joint pressure for a deeper sleep",
     "يخفف الضغط عن المفاصل لنوم أعمق",
     "Memory-foam dog bed designed locally for joint and hip support.",
     "سرير بإسفنج ذاكري مصمم محلياً لدعم المفاصل والوركين.",
     32.0, 26.0, 10, 0, None, None, 6, None, "medium", "joint", 1),

    ("dogs", "food", "demo-dog-food-puppy", "Puppy Dry Food - Chicken",
     "طعام جاف للجراء - دجاج", "RoyalPaw",
     "Builds strong bones during early growth",
     "يبني عظاماً قوية خلال مرحلة النمو",
     "Balanced nutrition for puppies up to 12 months with DHA for brain development.",
     "تغذية متوازنة للجراء حتى عمر ١٢ شهر، تحتوي DHA لدعم نمو الدماغ.",
     16.0, None, 18, 1, 28, 2000, 1, 12, "small", "", 0),

    ("birds", "food", "demo-bird-seed-mix", "Premium Seed Mix - Small Birds",
     "خليط بذور مميز - طيور صغيرة", "FeatherFeast",
     "A balanced mix for daily energy and feather health",
     "خليط متوازن لطاقة يومية وصحة الريش",
     "Mixed seeds with added vitamins for canaries and budgies.",
     "خليط بذور مع فيتامينات مضافة للكناري والبادجي.",
     5.5, None, 30, 1, None, 500, None, None, "all", "", 0),

    ("small-pets", "food", "demo-rabbit-pellets", "Rabbit Pellets - Timothy Hay Based",
     "حبيبات أرانب - أساسها حشيشة تيموثي", "GreenNibble",
     "High fiber blend supports healthy digestion",
     "خليط غني بالألياف يدعم الهضم الصحي",
     "Pellets made from timothy hay, ideal for adult rabbits.",
     "حبيبات مصنوعة من حشيشة تيموثي، مناسبة للأرانب البالغة.",
     6.0, None, 22, 1, None, 1000, 4, None, "all", "", 0),
]

DEMO_BLOG_POSTS = [
    ("how-to-choose-cat-food", "How to Choose the Right Food for Your Cat",
     "كيف تختار الطعام المناسب لقطتك",
     "A short guide on reading ingredient labels and matching food to your cat's age.",
     "دليل سريع لقراءة مكونات الطعام واختيار ما يناسب عمر قطتك."),
    ("puppy-first-month", "Your Puppy's First Month at Home",
     "أول شهر لجروك بالبيت",
     "Practical tips for feeding, sleep, and house-training a new puppy.",
     "نصايح عملية للتغذية والنوم وتدريب الجرو الجديد بالبيت."),
]


def seed_demo_data():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM products")
    if cur.fetchone()[0] == 0:
        cat_ids = {row["slug"]: row["id"] for row in cur.execute("SELECT id, slug FROM categories")}
        for row in DEMO_PRODUCTS:
            (cat_slug, subcat_slug, slug, name_en, name_ar, brand,
             benefit_en, benefit_ar, desc_en, desc_ar,
             price, discount_price, stock, is_consumable, cons, pkg,
             min_age, max_age, size_tag, health_tags, featured) = row
            cat_id = cat_ids.get(cat_slug)
            sub_id = None
            if cat_id and subcat_slug:
                sub_row = cur.execute(
                    "SELECT id FROM subcategories WHERE category_id=? AND slug=?",
                    (cat_id, subcat_slug)
                ).fetchone()
                if sub_row:
                    sub_id = sub_row["id"]
            cur.execute(
                """INSERT INTO products
                (category_id, subcategory_id, slug, name_en, name_ar, brand,
                 benefit_en, benefit_ar, description_en, description_ar,
                 price, discount_price, stock_qty, is_consumable,
                 consumption_grams_per_kg_day, package_weight_grams,
                 min_age_months, max_age_months, size_tag, health_tags, is_featured)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cat_id, sub_id, slug, name_en, name_ar, brand,
                 benefit_en, benefit_ar, desc_en, desc_ar,
                 price, discount_price, stock, is_consumable, cons, pkg,
                 min_age, max_age, size_tag, health_tags, featured),
            )

    # صلح subcategory_id للمنتجات التجريبية القديمة اللي اتحفظت بـ NULL
    cat_ids = {row["slug"]: row["id"] for row in cur.execute("SELECT id, slug FROM categories")}
    for cat_slug, subcat_slug, prod_slug, *_ in DEMO_PRODUCTS:
        cat_id = cat_ids.get(cat_slug)
        if cat_id and subcat_slug:
            sub_row = cur.execute(
                "SELECT id FROM subcategories WHERE category_id=? AND slug=?",
                (cat_id, subcat_slug)
            ).fetchone()
            if sub_row:
                cur.execute(
                    "UPDATE products SET subcategory_id=? WHERE slug=? AND subcategory_id IS NULL",
                    (sub_row["id"], prod_slug),
                )

    cur.execute("SELECT COUNT(*) FROM blog_posts")
    if cur.fetchone()[0] == 0:
        for slug, title_en, title_ar, content_en, content_ar in DEMO_BLOG_POSTS:
            cur.execute(
                """INSERT INTO blog_posts (slug, title_en, title_ar, content_en, content_ar)
                VALUES (?,?,?,?,?)""",
                (slug, title_en, title_ar, content_en, content_ar),
            )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    seed_demo_data()
    print("تم تجهيز قاعدة البيانات وإضافة بيانات تجريبية: petstore.db")
