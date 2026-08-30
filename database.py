import os
import sqlite3
import hashlib
import secrets
import bcrypt
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import contextmanager

# لازم قاعدة البيانات تتخزن في نفس الـ DATA_DIR اللي بيتحدد من متغير البيئة
# (بالظبط زي UPLOADS_DIR و VIDEOS_DIR في main.py) عشان تبقى على الـ Persistent Disk
# مش على الـ filesystem المؤقت اللي بيتمسح مع كل ديبلوي على Render.
DATA_DIR = os.environ.get("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_NAME = os.path.join(DATA_DIR, "teacher_system.db")

# المراحل الدراسية الثابتة في النظام (المرحلة الثانوية فقط بصفوفها الثلاثة)
STAGES = ["الصف الأول الثانوي", "الصف الثاني الثانوي", "الصف الثالث الثانوي"]

# محافظات مصر الـ27 - ثابتة في النظام (متعرضة كدروب داون بحث، من غير إدارة يدوية)
GOVERNORATES = [
    "القاهرة", "الجيزة", "القليوبية", "الإسكندرية", "البحيرة", "مطروح",
    "كفر الشيخ", "الدقهلية", "دمياط", "الشرقية", "بورسعيد", "الإسماعيلية",
    "السويس", "شمال سيناء", "جنوب سيناء", "المنوفية", "الغربية",
    "الفيوم", "بني سويف", "المنيا", "أسيوط", "سوهاج", "قنا",
    "الأقصر", "أسوان", "البحر الأحمر", "الوادي الجديد",
]

# مدة صلاحية جلسة الدخول (بعدها لازم تسجل دخول تاني)
SESSION_LIFETIME_DAYS = 30


def hash_password(password: str) -> str:
    """تشفير الباسورد بـ bcrypt (أأمن من sha256 البسيط)"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _legacy_sha256(password: str) -> str:
    """دالة التشفير القديمة (sha256 بسيط) - موجودة بس لدعم الحسابات القديمة قبل التحديث"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def verify_password(password: str, stored_hash: str) -> tuple[bool, bool]:
    """
    يتحقق من الباسورد، ويدعم الحسابات القديمة المشفرة بـ sha256.
    يرجع (متطابق_أم_لا, لازم_تحديث_للهاش_الجديد)
    """
    if stored_hash.startswith("$2"):  # bcrypt hash signature
        try:
            ok = bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
        except ValueError:
            ok = False
        return ok, False
    # هاش قديم (sha256) - تحقق منه وعلّم إنه محتاج تحديث
    ok = (_legacy_sha256(password) == stored_hash)
    return ok, ok


def get_first_subscription_date(conn, student_id: int):
    """
    تاريخ أول اشتراك فعلي (أول دفعة مسددة أو أول شهر اتمنح فري) للطالب - للعرض
    بس في الواجهة (زي "مشترك من...").
    بيرجع التاريخ بصيغة 'YYYY-MM-DD'، أو None لو الطالب لسه معندوش أي اشتراك مسدد.
    """
    row = conn.execute(
        """
        SELECT MIN(COALESCE(paid_date, created_at)) as first_date
        FROM payments
        WHERE student_id = ? AND (is_paid = 1 OR is_free = 1)
        """,
        (student_id,),
    ).fetchone()
    if row and row["first_date"]:
        return str(row["first_date"])[:10]
    return None


def get_paid_months(conn, student_id: int):
    """
    بيرجع قايمة الشهور (YYYY-MM) اللي الطالب مسموحله يشوف محتواها - إما لأنه
    سددها فعليًا (is_paid=1) أو اتمنحله فري لشهر معين بس (is_free=1، مختلف عن
    الفري الدائم في جدول الطلاب). بتُستخدم عشان نعرضله بس محتوى الشهور دي (لو
    فيه فجوة سداد، الشهر ده بيتخفي حتى لو فيه شهور مدفوعة بعده).
    """
    rows = conn.execute(
        "SELECT month FROM payments WHERE student_id=? AND (is_paid=1 OR is_free=1) ORDER BY month",
        (student_id,),
    ).fetchall()
    return [r["month"] for r in rows]


# ---------------------------------------------------------------------------
# نظام نقاط التفاعل (المشاركة) - يحدد "نوع الطالب" بناءً على مجموع نقاطه
# المتراكمة عبر كل الحصص: مستجيب (1-5) / فائق (5-10] / فريد (10+)
# ---------------------------------------------------------------------------
PARTICIPATION_LEVELS = {
    "responsive": "مستجيب",
    "outstanding": "فائق",
    "unique": "فريد",
}


def participation_level(total_points: int):
    """بياخد مجموع نقاط التفاعل المتراكمة للطالب ويرجع (key, label) لنوعه،
    أو (None, None) لو معندوش نقاط تفاعل مسجلة لسه"""
    if not total_points or total_points <= 0:
        return None, None
    if total_points <= 5:
        return "responsive", PARTICIPATION_LEVELS["responsive"]
    if total_points <= 10:
        return "outstanding", PARTICIPATION_LEVELS["outstanding"]
    return "unique", PARTICIPATION_LEVELS["unique"]


# ---------------------------------------------------------------------------
# حضور المشرفين - دوال حسابية مشتركة (المسافة الجغرافية وحالة الحضور)
# ---------------------------------------------------------------------------
import math as _math


def haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """يحسب المسافة بالمتر بين نقطتين جغرافيتين بمعادلة Haversine"""
    R = 6371000  # نصف قطر الأرض بالمتر
    phi1, phi2 = _math.radians(lat1), _math.radians(lat2)
    d_phi = _math.radians(lat2 - lat1)
    d_lambda = _math.radians(lon2 - lon1)
    a = (_math.sin(d_phi / 2) ** 2
         + _math.cos(phi1) * _math.cos(phi2) * _math.sin(d_lambda / 2) ** 2)
    c = 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))
    return R * c


# فرق التوقيت المحلي المستخدم في النظام (توقيت القاهرة) - بيتحدد بمنطقة زمنية
# حقيقية (مش رقم ثابت) عشان يتعامل صح مع التوقيت الصيفي: مصر رجّعت التوقيت
# الصيفي من 2023 (UTC+3 في الصيف، UTC+2 في الشتاء). رقم ثابت زي "+2" كان بيدي
# نتيجة غلط في الصيف (مواعيد الحضور بتتحسب متأخرة بساعة عن الحقيقة، فمشرف يدخل
# متأخر فعليًا كان بيتحسب حاضر لأن وقته المحسوب بيطلع قبل بداية الدوام).
# التخزين الفعلي لكل الأوقات في قاعدة البيانات لسه بتوقيت UTC زي باقي النظام.
APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE_NAME", "Africa/Cairo"))


def to_app_local_time(utc_time: datetime) -> datetime:
    return utc_time.replace(tzinfo=ZoneInfo("UTC")).astimezone(APP_TIMEZONE).replace(tzinfo=None)


def compute_attendance_status(check_in_time_utc: datetime, work_start_time: str, grace_period_minutes: int):
    """
    بيحدد حالة الحضور (present/late) ودقائق التأخير بناءً على وقت الدخول
    الفعلي (server time UTC، بيتحول لتوقيت القاهرة المحلي هنا مع مراعاة
    التوقيت الصيفي) ومواعيد العمل.
    work_start_time بصيغة 'HH:MM' بتوقيت النظام المحلي (مش UTC).
    """
    local_time = to_app_local_time(check_in_time_utc)
    h, m = [int(x) for x in work_start_time.split(":")]
    scheduled_start = local_time.replace(hour=h, minute=m, second=0, microsecond=0)
    diff_minutes = int((local_time - scheduled_start).total_seconds() // 60)
    if diff_minutes <= grace_period_minutes:
        return "present", 0
    return "late", diff_minutes


def gen_token() -> str:
    return secrets.token_hex(24)


def gen_access_code(prefix: str = "ST") -> str:
    """كود دخول قصير (مثال: ST-4F92AB للطالب، SUP-4F92AB للمشرف، TCH-4F92AB للمدرس)"""
    return f"{prefix}-" + secrets.token_hex(3).upper()


def gen_numeric_code(length: int = 5) -> str:
    """كود مميز أرقام بس لكل طالب - أسهل وأسرع في الكتابة من كود تسجيل الدخول، بيستخدم في أخذ الحضور السريع"""
    return "".join(secrets.choice("0123456789") for _ in range(length))


def gen_temp_password() -> str:
    """كلمة مرور مؤقتة سهلة القراءة، تتولّد تلقائيًا للمشرف/المدرس لو الأدمن سايب خانة كلمة المرور فاضية"""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def session_expiry() -> str:
    return (datetime.utcnow() + timedelta(days=SESSION_LIFETIME_DAYS)).isoformat(timespec="seconds")


@contextmanager
def get_connection():
    """فتح اتصال بقاعدة البيانات وإغلاقه تلقائياً"""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _safe_alter(cur, sql):
    """تشغيل ALTER TABLE بأمان - يتجاهل الخطأ لو العمود موجود بالفعل"""
    try:
        cur.execute(sql)
    except Exception:
        pass


def _migrate_stages(cur):
    """
    ترحيل بيانات المراحل القديمة بعد إلغاء مرحلة الإعدادي من النظام:
    - مرحلة "إعدادي" (لو كانت موجودة من نسخة قديمة) بتتحذف بالكامل، وبالتبعية (CASCADE)
      بتتحذف كل المجموعات والطلاب وكل بياناتهم المرتبطة (درجات/حضور/واجبات/مدفوعات...).
    - مرحلة "ثانوي" العامة القديمة (لو كانت موجودة) بتتحول اسمها لـ "الصف الأول الثانوي"
      بدل ما تتحذف، عشان نحافظ على المجموعات والطلاب الموجودين فيها فعليًا.
    """
    old_prep = cur.execute("SELECT id FROM stages WHERE name=?", ("إعدادي",)).fetchone()
    if old_prep:
        cur.execute("DELETE FROM stages WHERE id=?", (old_prep["id"],))

    old_sec = cur.execute("SELECT id FROM stages WHERE name=?", ("ثانوي",)).fetchone()
    if old_sec:
        # لو "الصف الأول الثانوي" مش موجودة أصلاً، حوّل الاسم القديم لها بدل ما تتمسح بياناته
        already_exists = cur.execute(
            "SELECT id FROM stages WHERE name=?", ("الصف الأول الثانوي",)
        ).fetchone()
        if not already_exists:
            cur.execute(
                "UPDATE stages SET name=? WHERE id=?",
                ("الصف الأول الثانوي", old_sec["id"]),
            )
        else:
            # الاسم الجديد موجود بالفعل -> امسح الصف العام القديم مع بياناته
            cur.execute("DELETE FROM stages WHERE id=?", (old_sec["id"],))


def _migrate_legacy_surveys(cur):
    """
    ترحيل الاستطلاعات القديمة (من نسخة كانت بتخزن سؤال واحد بس في عمود
    surveys.question وردود الطلاب في جدول survey_responses القديم) للشكل
    الجديد اللي بيسمح بأكتر من سؤال. أي استطلاع قديم لسه معندوش صفوف في
    survey_questions بنولّدله سؤال تقييم واحد بعنوان السؤال القديم، وبننقل
    ردود الطلاب المسجلة عليه لجدولي survey_answers و survey_completions
    الجديدين. الاستطلاعات اللي اترحّلت قبل كده (وليها أسئلة بالفعل) بيتم
    تجاهلها عشان الترحيل يبقى آمن لو اتشغل أكتر من مرة.
    """
    old_surveys = cur.execute("""
        SELECT s.id, s.question FROM surveys s
        WHERE NOT EXISTS (SELECT 1 FROM survey_questions q WHERE q.survey_id = s.id)
    """).fetchall()
    for sv in old_surveys:
        cur.execute(
            "INSERT INTO survey_questions (survey_id, question_text, question_type, order_index) VALUES (?, ?, 'rating', 0)",
            (sv["id"], sv["question"] or "إيه رأيك في أداء المنصة والمتابعة معاك؟"),
        )
        question_id = cur.lastrowid

        old_responses = cur.execute(
            "SELECT student_id, rating, notes, created_at FROM survey_responses WHERE survey_id = ?",
            (sv["id"],),
        ).fetchall()
        for r in old_responses:
            cur.execute(
                """INSERT OR IGNORE INTO survey_answers
                   (survey_id, question_id, student_id, rating, answer_text, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (sv["id"], question_id, r["student_id"], r["rating"], r["notes"], r["created_at"]),
            )
            cur.execute(
                "INSERT OR IGNORE INTO survey_completions (survey_id, student_id, created_at) VALUES (?, ?, ?)",
                (sv["id"], r["student_id"], r["created_at"]),
            )


def _migrate_users_role_check(cur):
    """
    لو جدول users موجود من نسخة قديمة (قيد CHECK بتاعه لسه مش شامل head_supervisor)،
    نعيد بناء الجدول بنفس البيانات لكن بقيد CHECK جديد يسمح بالدور الجديد.
    """
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if not row or not row["sql"] or "head_supervisor" in row["sql"]:
        return  # جدول جديد أصلاً، أو لسه معملوش create

    cur.execute("ALTER TABLE users RENAME TO users_old_migrating")
    cur.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','teacher','supervisor','head_supervisor')),
            full_name TEXT NOT NULL,
            phone TEXT,
            access_code TEXT UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            governorate_id INTEGER REFERENCES governorates(id)
        )
    """)
    cur.execute("""
        INSERT INTO users (id, username, password_hash, role, full_name, phone,
                            access_code, is_active, created_at, governorate_id)
        SELECT id, username, password_hash, role, full_name, phone,
               access_code, is_active, created_at, governorate_id
        FROM users_old_migrating
    """)
    cur.execute("DROP TABLE users_old_migrating")


def _migrate_quizzes_columns(cur):
    """إضافة أعمدة الكويز الجديدة (المرحلة/رقم الحصة/الصورة/النموذج/المنشئ/النوع) على القواعد القديمة"""
    _safe_alter(cur, "ALTER TABLE quizzes ADD COLUMN stage_id INTEGER REFERENCES stages(id)")
    _safe_alter(cur, "ALTER TABLE quizzes ADD COLUMN session_number INTEGER")
    _safe_alter(cur, "ALTER TABLE quizzes ADD COLUMN image_data TEXT")
    _safe_alter(cur, "ALTER TABLE quizzes ADD COLUMN version_label TEXT")
    _safe_alter(cur, "ALTER TABLE quizzes ADD COLUMN created_by INTEGER REFERENCES users(id)")
    _safe_alter(cur, "ALTER TABLE quizzes ADD COLUMN quiz_type TEXT NOT NULL DEFAULT 'quiz'")


def _migrate_qb_questions_columns(cur):
    """
    إضافة أعمدة "سؤال بالصورة" لبنك الأسئلة على القواعد القديمة:
    - question_image: صورة السؤال والاختيارات (base64) بدل كتابتهم نص
    - answer_mode: 'text' (الوضع القديم) أو 'image' (سؤال بالصورة)
    - label_style: شكل حروف الاختيارات المعروضة للطالب - 'ar' (أ ب ج د) أو 'en' (A B C D)
    - correct_option: رقم الاختيار الصح (1-4) في وضع الصورة، بترتيب ثابت مايتلخبطش
      (عكس النص العادي، اختيارات الصورة مايصحش تتلخبط لأنها جزء من الصورة نفسها)
    """
    _safe_alter(cur, "ALTER TABLE qb_questions ADD COLUMN question_image TEXT")
    _safe_alter(cur, "ALTER TABLE qb_questions ADD COLUMN answer_mode TEXT NOT NULL DEFAULT 'text'")
    _safe_alter(cur, "ALTER TABLE qb_questions ADD COLUMN label_style TEXT NOT NULL DEFAULT 'ar'")
    _safe_alter(cur, "ALTER TABLE qb_questions ADD COLUMN correct_option INTEGER")


def _backfill_attendance_codes(cur):
    """توليد كود حضور رقمي لأي طالب قديم لسه معندوش كود (بعد إضافة العمود لأول مرة)"""
    rows = cur.execute("SELECT id FROM students WHERE attendance_code IS NULL OR attendance_code=''").fetchall()
    for row in rows:
        code = gen_numeric_code()
        while cur.execute("SELECT id FROM students WHERE attendance_code=?", (code,)).fetchone():
            code = gen_numeric_code()
        cur.execute("UPDATE students SET attendance_code=? WHERE id=?", (code, row["id"]))


def cleanup_expired_sessions(conn):
    """مسح الجلسات اللي انتهت صلاحيتها (يتنفذ بهدوء عند كل تسجيل دخول)"""
    now = datetime.utcnow().isoformat(timespec="seconds")
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# حماية من محاولات تسجيل الدخول الغلط المتكررة (brute-force protection)
# ---------------------------------------------------------------------------
LOGIN_ATTEMPT_WINDOW_MINUTES = 15   # المدة اللي بنعد فيها المحاولات الفاشلة
LOGIN_ATTEMPT_MAX = 8               # أقصى عدد محاولات فاشلة مسموح بيه في المدة دي


def is_login_blocked(conn, identifier: str) -> bool:
    """بيتأكد إن الـ identifier (يوزرنيم أو كود دخول أو IP) معملش محاولات فاشلة كتير قوي مؤخرًا"""
    # ملحوظة: عمود created_at بيتخزن بصيغة SQLite's CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS"
    # بمسافة)، فلازم الـ cutoff يتقارن بنفس الصيغة بالظبط (مش isoformat اللي بيحط "T")
    cutoff = (datetime.utcnow() - timedelta(minutes=LOGIN_ATTEMPT_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COUNT(*) as c FROM login_attempts WHERE identifier=? AND created_at >= ?",
        (identifier, cutoff)
    ).fetchone()
    return row["c"] >= LOGIN_ATTEMPT_MAX


def record_failed_login(conn, identifier: str):
    """يسجل محاولة دخول فاشلة"""
    try:
        conn.execute("INSERT INTO login_attempts (identifier) VALUES (?)", (identifier,))
    except Exception:
        pass


def clear_failed_logins(conn, identifier: str):
    """يمسح محاولات الفشل بعد نجاح تسجيل الدخول"""
    try:
        conn.execute("DELETE FROM login_attempts WHERE identifier=?", (identifier,))
    except Exception:
        pass


def cleanup_old_login_attempts(conn):
    """مسح دوري لمحاولات الدخول القديمة عشان الجدول ما يكبرش من غير داعي"""
    cutoff = (datetime.utcnow() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute("DELETE FROM login_attempts WHERE created_at < ?", (cutoff,))
    except Exception:
        pass


def init_db():
    """إنشاء كل الجداول المطلوبة لو لسه غير موجودة"""
    with get_connection() as conn:
        cur = conn.cursor()

        # جدول المراحل الدراسية (إعدادي / ثانوي)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS stages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """)

        # جدول المحافظات
        cur.execute("""
        CREATE TABLE IF NOT EXISTS governorates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """)

        # ---------------------------------------------------------------
        # المستخدمين (أدمن - مدرس - مشرف) - تسجيل دخول بيوزر وباسورد، أو بكود دخول
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','teacher','supervisor','head_supervisor')),
            full_name TEXT NOT NULL,
            phone TEXT,
            access_code TEXT UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        _safe_alter(cur, "ALTER TABLE users ADD COLUMN access_code TEXT")
        _safe_alter(cur, "ALTER TABLE users ADD COLUMN governorate_id INTEGER REFERENCES governorates(id)")

        # جلسات الدخول (توكنات بسيطة) - كل توكن له تاريخ انتهاء صلاحية
        cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_type TEXT NOT NULL CHECK(user_type IN ('user','student')),
            user_id INTEGER NOT NULL,
            expires_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        _safe_alter(cur, "ALTER TABLE sessions ADD COLUMN expires_at TEXT")

        # ---------------------------------------------------------------
        # محاولات تسجيل الدخول الفاشلة - للحماية من محاولات التخمين المتكررة (brute-force)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_identifier ON login_attempts(identifier, created_at)")

        # جدول المجموعات - كل مجموعة تابعة لمرحلة ومحافظة، وممكن يكون ليها مشرف
        cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            stage_id INTEGER NOT NULL,
            governorate_id INTEGER NOT NULL,
            notes TEXT,
            session_price REAL,
            supervisor_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (stage_id) REFERENCES stages(id) ON DELETE CASCADE,
            FOREIGN KEY (governorate_id) REFERENCES governorates(id) ON DELETE CASCADE,
            FOREIGN KEY (supervisor_id) REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE(name, stage_id, governorate_id)
        )
        """)

        # migrations: أعمدة جديدة لو الجدول قديم
        _safe_alter(cur, "ALTER TABLE groups ADD COLUMN session_price REAL")
        _safe_alter(cur, "ALTER TABLE groups ADD COLUMN monthly_fee REAL DEFAULT 0")
        _safe_alter(cur, "ALTER TABLE groups ADD COLUMN supervisor_id INTEGER REFERENCES users(id)")
        # ملحوظة: عمود supervisor_id فوق باقي في الجدول للتوافق مع نسخ قديمة من قاعدة
        # البيانات، لكن النظام بقى مايستخدموش؛ المشرف المسؤول بقى بيتسجل في جدول
        # group_supervisors تحت عشان تقدر تعيّن أكتر من مشرف لنفس المجموعة.
        # عمود session_price كان متوقف الاستخدام، وبقى دلوقتي مستخدم في حساب مديونية
        # الغياب التلقائية (absence_debts تحت): قيمة الحصة الواحدة لطلاب المجموعة دي.

        # ---------------------------------------------------------------
        # جدول ربط المجموعات بالمشرفين - Many-to-Many
        # (ممكن يبقى للمجموعة الواحدة أكتر من مشرف مسؤول عنها)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_supervisors (
            group_id INTEGER NOT NULL,
            supervisor_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (group_id, supervisor_id),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (supervisor_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_group_supervisors_supervisor ON group_supervisors(supervisor_id)")

        # ترحيل تلقائي: أي مجموعة كان ليها مشرف واحد مسجل في العمود القديم groups.supervisor_id
        # (من نسخة سابقة من النظام) بننقله لجدول group_supervisors الجديد لو لسه مش موجود فيه
        cur.execute("""
            INSERT OR IGNORE INTO group_supervisors (group_id, supervisor_id)
            SELECT id, supervisor_id FROM groups WHERE supervisor_id IS NOT NULL
        """)

        # جدول الطلاب - كل طالب تابع لمجموعة معينة + كود دخول خاص بيه
        cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            phone TEXT,
            parent_phone TEXT,
            group_id INTEGER NOT NULL,
            notes TEXT,
            access_code TEXT UNIQUE,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        )
        """)
        _safe_alter(cur, "ALTER TABLE students ADD COLUMN access_code TEXT")
        _safe_alter(cur, "ALTER TABLE students ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        _safe_alter(cur, "ALTER TABLE students ADD COLUMN attendance_code TEXT")
        _safe_alter(cur, "ALTER TABLE students ADD COLUMN device_id TEXT")
        # طالب "فري" - معفى من سداد الاشتراك الشهري، بيقدر يشوف المحتوى زي أي طالب
        # مسدد بالظبط، بس بيتعرض في تقرير الاشتراكات بشكل مميز (فري) مش ضمن المسددين فلوس
        _safe_alter(cur, "ALTER TABLE students ADD COLUMN is_free INTEGER NOT NULL DEFAULT 0")
        _backfill_attendance_codes(cur)

        # ---------------------------------------------------------------
        # الملاحظات السلوكية - يكتبها المشرف، تظهر للمدرس والأدمن بس (مش الطالب)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS behavior_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            author_id INTEGER,
            note_type TEXT NOT NULL DEFAULT 'neutral' CHECK(note_type IN ('positive','negative','neutral')),
            note TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL
        )
        """)

        # ---------------------------------------------------------------
        # طلبات الطلاب - الطالب يقدم طلب (إذن حضور في معاد آخر / مشكلة / شرح)
        # ويوصل لمشرف مجموعته يرد عليه ويغير حالته
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS student_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            request_type TEXT NOT NULL CHECK(request_type IN ('attendance_change','issue','explanation','other')),
            details TEXT,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','in_progress','resolved')),
            supervisor_reply TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_student_requests_group ON student_requests(group_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_student_requests_student ON student_requests(student_id)")

        # ---------------------------------------------------------------
        # المدفوعات - سجل شهري لكل طالب (دفع/متبقي)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            amount REAL,
            is_paid INTEGER NOT NULL DEFAULT 0,
            paid_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(student_id, month)
        )
        """)
        # فري خاص بشهر معين بس (مختلف عن students.is_free اللي بيبقى فري دائم في
        # كل الشهور) - لو الطالب فري في شهر معين، بيشوف محتوى الشهر ده بس من
        # غير سداد، من غير ما يأثر على باقي الشهور
        _safe_alter(cur, "ALTER TABLE payments ADD COLUMN is_free INTEGER NOT NULL DEFAULT 0")

        # ---------------------------------------------------------------
        # Exception (خصم/سعر مختلف عن سعر الباقة) + رسوم الحصص التي غابها الطالب
        # ميزتان اختياريتان بالكامل - لو مش مستخدمتين، amount = base_price زي ما كان قبل كده بالظبط
        # - base_price: السعر الأساسي المعتمد فعليًا لهذه الدفعة (سعر المجموعة، أو
        #   قيمة الـ Exception لو اتفعلت). بيتسجل وقت الحفظ عشان التقرير يفضل صحيح
        #   حتى لو سعر المجموعة اتغير بعدين.
        # - exception_amount: لو مش NULL، يبقى معناه إن الأدمن فعّل Exception وحدد
        #   قيمة اشتراك فعلية مختلفة عن سعر الباقة (بتحل محل base_price مش بتتضاف ليه)
        # - absence_sessions / absence_session_price: عدد حصص الغياب وسعر الحصة
        # - absence_fee: رسوم الغياب المحسوبة = absence_sessions × absence_session_price
        # - amount (العمود الأصلي) فضل بمعنى "إجمالي المطلوب" = base_price + absence_fee
        # ---------------------------------------------------------------
        _safe_alter(cur, "ALTER TABLE payments ADD COLUMN base_price REAL")
        _safe_alter(cur, "ALTER TABLE payments ADD COLUMN exception_amount REAL")
        _safe_alter(cur, "ALTER TABLE payments ADD COLUMN absence_sessions INTEGER NOT NULL DEFAULT 0")
        _safe_alter(cur, "ALTER TABLE payments ADD COLUMN absence_session_price REAL NOT NULL DEFAULT 0")
        _safe_alter(cur, "ALTER TABLE payments ADD COLUMN absence_fee REAL NOT NULL DEFAULT 0")
        # ترحيل السجلات القديمة: base_price كانت مش موجودة قبل كده، فبنملاها من amount
        # (اللي كانت هي سعر الاشتراك بالظبط قبل إضافة الـ Exception ورسوم الغياب)
        cur.execute("UPDATE payments SET base_price = amount WHERE base_price IS NULL")

        # ---------------------------------------------------------------
        # نظام الاشتراك بالحصص - نظام منفصل تمامًا عن الاشتراك الشهري (payments).
        # الأدمن/المشرف يسجل إن الطالب دفع مبلغ معين مقابل حصة أو أكتر بعينها،
        # وده بيفتح الحصص دي بس للطالب - مش المحتوى كله زي الاشتراك الشهري.
        # النظامان بيشتغلوا مع بعض بشكل تراكمي (طالب ممكن يكون مشترك شهريًا
        # وبرضه يشتري حصص إضافية، أو يكون مش مشترك شهريًا خالص وبيشتري حصص بس).
        #
        # session_purchases: "فاتورة" الشراء - طالب + مبلغ + تاريخ + حالة + مين سجلها
        # session_purchase_items: الحصص المحددة اللي اتغطت بالفاتورة دي (شهر + رقم حصة)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS session_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            amount REAL,
            purchase_date TEXT NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')),
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_session_purchases_student ON session_purchases(student_id, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_session_purchases_group ON session_purchases(group_id)")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS session_purchase_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            session_number INTEGER NOT NULL,
            FOREIGN KEY (purchase_id) REFERENCES session_purchases(id) ON DELETE CASCADE,
            UNIQUE(purchase_id, month, session_number)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_session_purchase_items_purchase ON session_purchase_items(purchase_id)")

        # ---------------------------------------------------------------
        # مديونيات الغياب (Absence Debts) - Business Rule بسيطة مبنية فوق
        # جدول الحضور (attendance) الموجود بالفعل، من غير ما تعمل أي نظام
        # حضور جديد:
        #   - أول ما يتسجل غياب (attendance.status='absent') لطالب في حصة معينة،
        #     بيتسجل تلقائيًا سطر هنا بقيمة = سعر الحصة (groups.session_price)
        #     وحالته "مش مسدد".
        #   - وجود أي سطر هنا "مش مسدد" لطالب = المنصة مقفولة عليه (يقدر يدخل
        #     لحسابه بس مش هيقدر يشوف أي محتوى) لحد ما الأدمن/المشرف يسجل
        #     إنه سدد قيمة الحصة دي (is_paid=1).
        #   - نظام منفصل تمامًا عن الاشتراك الشهري (payments) وعن رسوم الغياب
        #     اليدوية اللي بتتضاف لفاتورة الشهر (payments.absence_fee) - ده
        #     تلقائي بالكامل ومربوط بكل حصة غياب لوحدها.
        #   - UNIQUE(student_id, session_date, session_number) بنفس مفتاح
        #     جدول attendance عشان يفضل سطر واحد بالظبط لكل حصة غياب.
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS absence_debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            session_number INTEGER NOT NULL DEFAULT 1,
            amount REAL NOT NULL DEFAULT 0,
            is_paid INTEGER NOT NULL DEFAULT 0,
            paid_date TEXT,
            paid_by INTEGER,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (paid_by) REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE(student_id, session_date, session_number)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_absence_debts_student ON absence_debts(student_id, is_paid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_absence_debts_group ON absence_debts(group_id, is_paid)")

        # ---------------------------------------------------------------
        # دفتر حسابات يدوي (Manual Ledger) - جدول بسيط زي شيت إكسل، منفصل
        # تمامًا عن أي نظام مالي تاني في المشروع (مديونيات الغياب، الاشتراك
        # الشهري، الاشتراك بالحصص). مفيش أي ربط تلقائي هنا - كل سطر بيتكتب
        # يدويًا: اسم (نص حر، مش لازم يكون طالب مسجل في النظام)، نوع
        # (دفع/عليه)، مبلغ، وملاحظة. الهدف منه إنه دفتر سريع للتقييد اليومي
        # وطباعة تقرير بيه.
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS manual_ledger_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            student_name TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL DEFAULT 0,
            entry_type TEXT NOT NULL DEFAULT 'paid',
            notes TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_manual_ledger_date ON manual_ledger_entries(entry_date)")

        # جدول الكويزات - كويز عام على مستوى المرحلة الدراسية (بيشوفه كل مشرفي المرحلة)
        # أو مرتبط بمجموعة معينة (النظام القديم، لسه متاح للتوافق)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            quiz_date TEXT,
            max_score REAL NOT NULL DEFAULT 100,
            group_id INTEGER,
            stage_id INTEGER,
            session_number INTEGER,
            image_data TEXT,
            version_label TEXT,
            created_by INTEGER,
            quiz_type TEXT NOT NULL DEFAULT 'quiz',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (stage_id) REFERENCES stages(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        _migrate_quizzes_columns(cur)

        # جدول درجات الكويزات (طالب - كويز - درجة)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS quiz_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            quiz_id INTEGER NOT NULL,
            score REAL NOT NULL,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'present',
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
            UNIQUE(student_id, quiz_id)
        )
        """)
        # عمود حالة الطالب في الامتحان/الكويز: present (حاضر وأدى) أو absent (متغيب عن الأداء).
        # الطالب المتغيب بتتسجل درجته صفر تلقائيًا وتنقص من نسبة الامتحانات (40%) في التقييم التراكمي.
        _safe_alter(cur, "ALTER TABLE quiz_scores ADD COLUMN status TEXT NOT NULL DEFAULT 'present'")

        # جدول الحضور والغياب - بيسمح بأكتر من حصة لنفس الطالب في نفس اليوم
        cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            session_number INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL CHECK(status IN ('present','absent','late','excused')),
            notes TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(student_id, session_date, session_number)
        )
        """)
        _safe_alter(cur, "ALTER TABLE attendance ADD COLUMN session_number INTEGER NOT NULL DEFAULT 1")

        # ---------------------------------------------------------------
        # جدول تفاعل الطالب (المشاركة) - المشرف بيسجل كل حصة هل الطالب
        # جاوب/اتفاعل مع المستر وبيدي نقاط من 1 لـ 5. مجموع النقاط المتراكم
        # عبر الحصص بيحدد "نوع الطالب": مستجيب (1-5) / فائق (5-10) / فريد (10+)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS participation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            session_number INTEGER NOT NULL DEFAULT 1,
            points INTEGER NOT NULL CHECK(points BETWEEN 1 AND 5),
            notes TEXT,
            author_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (author_id) REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE(student_id, session_date, session_number)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_participation_student ON participation(student_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_participation_group ON participation(group_id, session_date)")

        # ---------------------------------------------------------------
        # جدول الواجبات - واجب لكل حصة مجموعة
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_number INTEGER NOT NULL DEFAULT 1,
            session_date TEXT,
            description TEXT NOT NULL,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE(group_id, session_number)
        )
        """)

        # جدول متابعة تسليم الواجبات لكل طالب
        cur.execute("""
        CREATE TABLE IF NOT EXISTS homework_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            homework_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            done INTEGER,
            notes TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (homework_id) REFERENCES homework(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(homework_id, student_id)
        )
        """)

        # ---------------------------------------------------------------
        # جدول مواعيد المدرس (جدول الحصص الخاص بالمدرس)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS teacher_schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT,
            group_id INTEGER,
            title TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
        )
        """)

        # ---------------------------------------------------------------
        # سبورة الحصة - صور شرح كل حصة، خاصة بكل مجموعة لوحدها
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS board_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            session_number INTEGER NOT NULL,
            session_date TEXT,
            image_data TEXT NOT NULL,
            caption TEXT,
            uploaded_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)

        # ---------------------------------------------------------------
        # فيديوهات المجموعة - المشرف بيرفع فيديو لمجموعة معينة، وكل طلاب
        # المجموعة يقدروا يتفرجوا عليه (بدون تنزيل) - الملف نفسه بيتخزن على
        # الـ disk برا مجلد الـ uploads العام، وبيتبث عن طريق endpoint فيه تحقق صلاحيات
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS group_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            file_path TEXT NOT NULL,
            file_size INTEGER,
            mime_type TEXT,
            session_number INTEGER,
            uploaded_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        _safe_alter(cur, "ALTER TABLE group_videos ADD COLUMN session_number INTEGER")
        # نوع الفيديو: upload (ملف اترفع فعليًا وموجود على الديسك) أو link (رابط
        # خارجي زي يوتيوب/جوجل درايف وغيره). لو link، عمود file_path بيتخزن فيه
        # سترينج فاضي (عشان نحافظ على NOT NULL من غير ما نغيّر بنية الجدول)
        # وعمود external_url هو اللي فيه الرابط الفعلي.
        _safe_alter(cur, "ALTER TABLE group_videos ADD COLUMN video_type TEXT DEFAULT 'upload'")
        _safe_alter(cur, "ALTER TABLE group_videos ADD COLUMN external_url TEXT")

        # ---------------------------------------------------------------
        # دعم أكتر من مزود فيديو (Multi Video Provider) - إضافة على النظام
        # الحالي من غير أي كسر: video_type فضل زي ما هو ('upload' أو 'link')
        # لكل الفيديوهات القديمة، وده بيخليها تشتغل بالظبط زي ما كانت.
        # لأي مزود جديد بيتحط له video_type مستقل (مثلاً 'bunny') عشان طبقة
        # التشغيل تعرف تفرّق بينه وبين 'link' العادي (يوتيوب/جوجل درايف...).
        #
        # provider: اسم المزود بشكل صريح - مش شرط يتملى للفيديوهات القديمة
        # (upload/link) عشان منحتاجش نعمل Backfill ليها، وهي أصلاً بتتعرف من
        # video_type + external_url زي ما كانت بالظبط. للمزودات الجديدة
        # (زي bunny) provider بيتملى إجباريًا وقت الإضافة.
        # provider_video_id: المعرف الخاص بالفيديو عند المزود (مثلاً Bunny
        # Video GUID) - بديل لـ external_url في حالة المزودات اللي مش رابط
        # عادي وبتحتاج توليد رابط تشغيل موقّع (Signed URL) في كل مرة.
        # ---------------------------------------------------------------
        _safe_alter(cur, "ALTER TABLE group_videos ADD COLUMN provider TEXT")
        _safe_alter(cur, "ALTER TABLE group_videos ADD COLUMN provider_video_id TEXT")

        # ---------------------------------------------------------------
        # ربط فيديو واحد بأكتر من مجموعة - بدل ما الفيديو يترفع لمجموعة واحدة
        # بس، ده بيسمح إن نفس ملف الفيديو (اترفع مرة واحدة) يتحدد له كذا مجموعة،
        # ولكل مجموعة رقم حصة مستقل (ممكن يكون مختلف حسب تقدم كل مجموعة)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS video_group_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            session_number INTEGER,
            FOREIGN KEY (video_id) REFERENCES group_videos(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            UNIQUE(video_id, group_id)
        )
        """)
        # ترحيل الفيديوهات القديمة (اللي كانت مربوطة بعمود group_id مباشرة) لجدول
        # الربط الجديد، عشان تفضل شغالة زي ما هي من غير ما نخسر أي بيانات
        cur.execute("""
            INSERT OR IGNORE INTO video_group_links (video_id, group_id, session_number)
            SELECT id, group_id, session_number FROM group_videos WHERE group_id IS NOT NULL
        """)

        # ---------------------------------------------------------------
        # الإشعارات - كل عملية يعملها المشرف بتوصل للطالب (درجة/واجب/سبورة...)
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_type TEXT NOT NULL DEFAULT 'student' CHECK(user_type IN ('student','user')),
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_type, user_id, is_read)")

        # ---------------------------------------------------------------
        # استطلاعات رأي الطلاب - Surveys
        # الأدمن بيكتب استطلاع فيه أكتر من سؤال (كل سؤال إما تقييم بالنجوم 1-5
        # أو سؤال مفتوح بيجاوب عليه الطالب بالنص)، وبيحدد يبعته لكل الطلاب
        # النشطين أو لمجموعات معينة بس. الطالب بيوصله إشعار وبيجاوب مرة واحدة.
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS surveys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'استطلاع رأي عن أداء المنصة',
            question TEXT NOT NULL DEFAULT 'إيه رأيك في أداء المنصة والمتابعة معاك؟',
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        # ملحوظة: عمود "question" فوق باقي في الجدول للتوافق مع نسخ قديمة، لكن
        # النظام بقى مايستخدمهوش؛ أسئلة الاستطلاع بقت في جدول survey_questions
        # تحت عشان يسمح بأكتر من سؤال لكل استطلاع.
        _safe_alter(cur, "ALTER TABLE surveys ADD COLUMN target_all_groups INTEGER NOT NULL DEFAULT 1")

        # لو الاستطلاع مش مستهدف كل المجموعات (target_all_groups = 0)، المجموعات
        # المستهدفة بتتسجل هنا (ممكن أكتر من مجموعة لنفس الاستطلاع)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS survey_groups (
            survey_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            PRIMARY KEY (survey_id, group_id),
            FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_survey_groups_group ON survey_groups(group_id)")

        # أسئلة كل استطلاع - بالترتيب اللي الأدمن كتبها بيه
        cur.execute("""
        CREATE TABLE IF NOT EXISTS survey_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            survey_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            question_type TEXT NOT NULL DEFAULT 'rating' CHECK(question_type IN ('rating', 'text')),
            order_index INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_survey_questions_survey ON survey_questions(survey_id, order_index)")

        # رد الطالب على سؤال معين - لو السؤال تقييم بيتسجل rating، لو مفتوح بيتسجل answer_text
        cur.execute("""
        CREATE TABLE IF NOT EXISTS survey_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            survey_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            rating INTEGER CHECK(rating IS NULL OR rating BETWEEN 1 AND 5),
            answer_text TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES survey_questions(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(question_id, student_id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_survey_answers_survey ON survey_answers(survey_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_survey_answers_question ON survey_answers(question_id)")

        # علامة إن الطالب خلّص كل أسئلة الاستطلاع (بيتسجل مرة واحدة بعد ما يبعت كل إجاباته)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS survey_completions (
            survey_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (survey_id, student_id),
            FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_survey_completions_survey ON survey_completions(survey_id)")

        # جدول قديم (نسخة سابقة كانت بتسجل رد واحد بس لكل استطلاع) - باقي للتوافق
        # مع نسخ قديمة من قاعدة البيانات، لكن مش مستخدم في الكود الجديد
        cur.execute("""
        CREATE TABLE IF NOT EXISTS survey_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            survey_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (survey_id) REFERENCES surveys(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(survey_id, student_id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_survey_responses_survey ON survey_responses(survey_id)")

        _migrate_legacy_surveys(cur)

        # ---------------------------------------------------------------
        # بنك الأسئلة - Question Bank
        # كل سؤال تابع لمرحلة دراسية + مُصنّف بـ "الباب" و"الدرس"، وله إجابة
        # صحيحة واحدة + 3 إجابات خاطئة + تفسير يظهر للطالب لو جاوب غلط.
        # الأدمن بيرفع الأسئلة دفعة واحدة عن طريق شيت إكسيل (endpoint رفع خاص).
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS qb_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage_id INTEGER NOT NULL,
            chapter TEXT NOT NULL,
            lesson TEXT NOT NULL,
            question_text TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            wrong_answer_1 TEXT NOT NULL,
            wrong_answer_2 TEXT NOT NULL,
            wrong_answer_3 TEXT NOT NULL,
            explanation TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (stage_id) REFERENCES stages(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qb_questions_filter ON qb_questions(stage_id, chapter, lesson)")
        _migrate_qb_questions_columns(cur)

        # سجل إجابات الطلاب على أسئلة بنك الأسئلة - كل محاولة بسجل مستقل
        # (بيسمح بمحاولات متكررة لنفس السؤال) عشان نقدر نحلل نقط الضعف المشتركة
        cur.execute("""
        CREATE TABLE IF NOT EXISTS qb_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_answer TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            answered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES qb_questions(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qb_answers_question ON qb_answers(question_id, is_correct)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qb_answers_student ON qb_answers(student_id, question_id)")

        # مفضلة الطالب في بنك الأسئلة
        cur.execute("""
        CREATE TABLE IF NOT EXISTS qb_favorites (
            student_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (student_id, question_id),
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES qb_questions(id) ON DELETE CASCADE
        )
        """)

        # أسئلة "هحلها لاحقًا" - عشان الطالب يرجعلها تاني
        cur.execute("""
        CREATE TABLE IF NOT EXISTS qb_solve_later (
            student_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (student_id, question_id),
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES qb_questions(id) ON DELETE CASCADE
        )
        """)

        # ---------------------------------------------------------------
        # سجل الأنشطة (Activity Log) - بيسجل كل عمليات تسجيل الدخول/الخروج
        # وأهم الإجراءات (رفع سبورة، أخذ حضور، رصد درجة...) عشان الأدمن
        # يقدر يراجع مين عمل إيه وإمتى
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_type TEXT NOT NULL CHECK(actor_type IN ('user','student')),
            actor_id INTEGER,
            actor_name TEXT,
            actor_role TEXT,
            action TEXT NOT NULL,
            description TEXT,
            group_id INTEGER,
            ip_address TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE SET NULL
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_role ON activity_log(actor_role)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_created ON activity_log(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_action ON activity_log(action)")

        # ---------------------------------------------------------------
        # الامتحانات الإلكترونية الآمنة - Online Exams (منفصل عن جدول quizzes
        # القديم اللي بيرصد درجات يدوي بس). النظام ده كامل الأمان من السيرفر:
        # ترتيب الأسئلة، السؤال الحالي، الوقت المتبقي، والتصحيح كله بيتحدد
        # وبيتحقق منه في الباك إند فقط - الفرونت إند بيعرض بس وبيبعت الإجابات.
        # مشرف المشرفين أو الأدمن هو اللي بيرفع الامتحان لمرحلة دراسية كاملة،
        # وبيوصل لكل طلاب المرحلة (بنفس منطق جدول quizzes الحالي stage_id).
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_exams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            stage_id INTEGER NOT NULL,
            duration_minutes INTEGER NOT NULL,
            max_violations INTEGER NOT NULL DEFAULT 3,
            shuffle_questions INTEGER NOT NULL DEFAULT 1,
            shuffle_options INTEGER NOT NULL DEFAULT 1,
            show_result_immediately INTEGER NOT NULL DEFAULT 1,
            start_at TEXT,
            end_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (stage_id) REFERENCES stages(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_online_exams_stage ON online_exams(stage_id, is_active)")

        # أسئلة الامتحان - إجابة صحيحة واحدة + 3 غلط (زي بنك الأسئلة بالظبط)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_exam_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            correct_answer TEXT NOT NULL,
            wrong_answer_1 TEXT NOT NULL,
            wrong_answer_2 TEXT NOT NULL,
            wrong_answer_3 TEXT NOT NULL,
            explanation TEXT,
            order_index INTEGER NOT NULL DEFAULT 0,
            points REAL NOT NULL DEFAULT 1,
            FOREIGN KEY (exam_id) REFERENCES online_exams(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_online_exam_questions_exam ON online_exam_questions(exam_id, order_index)")

        # محاولة الطالب - محاولة واحدة لكل طالب لكل امتحان. ترتيب الأسئلة
        # (question_order) بيتخزن مرة واحدة وقت البدء وثابت طول المحاولة،
        # والمهلة (expires_at) بتتحسب من وقت السيرفر وقت البدء ومبتتغيرش.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_exam_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'in_progress' CHECK(status IN ('in_progress','submitted','terminated')),
            question_order TEXT NOT NULL,
            current_index INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            violations_count INTEGER NOT NULL DEFAULT 0,
            ended_reason TEXT,
            submitted_at TEXT,
            score REAL,
            total_points REAL,
            FOREIGN KEY (exam_id) REFERENCES online_exams(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(exam_id, student_id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_online_exam_attempts_student ON online_exam_attempts(student_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_online_exam_attempts_exam ON online_exam_attempts(exam_id)")

        # إجابات الطالب في المحاولة - سؤال واحد لكل صف، بتتسجل فورًا لحظة
        # ما يدوس "التالي" عشان لو حصل قطع نت أو ريفريش يكمل من غير ما يخسر حاجة
        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_exam_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            question_id INTEGER NOT NULL,
            selected_answer TEXT,
            is_correct INTEGER NOT NULL DEFAULT 0,
            answered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (attempt_id) REFERENCES online_exam_attempts(id) ON DELETE CASCADE,
            FOREIGN KEY (question_id) REFERENCES online_exam_questions(id) ON DELETE CASCADE,
            UNIQUE(attempt_id, question_id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_online_exam_answers_attempt ON online_exam_answers(attempt_id)")

        # سجل مخالفات المراقبة (خروج من التبويب/تصغير/خروج فُل سكرين/محاولة
        # فتح Developer Tools/محاولة نسخ...) - لكل محاولة امتحان على حدة
        cur.execute("""
        CREATE TABLE IF NOT EXISTS online_exam_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER NOT NULL,
            violation_type TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (attempt_id) REFERENCES online_exam_attempts(id) ON DELETE CASCADE
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_online_exam_violations_attempt ON online_exam_violations(attempt_id)")

        # ---------------------------------------------------------------
        # حضور وانصراف المشرفين (Supervisor Attendance) - نظام مستقل تمامًا
        # عن حضور الطلاب (جدول attendance فوق). كل عمليات القرار (المسافة،
        # الوقت، الحالة) بتتحسب في الباك إند فقط (main.py) والجدول ده بيخزن
        # النتيجة النهائية بس.
        # ---------------------------------------------------------------

        # إعدادات مكان العمل والمواعيد - صف واحد ثابت (id=1)، قابل للتعديل من الأدمن
        cur.execute("""
        CREATE TABLE IF NOT EXISTS supervisor_attendance_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            work_latitude REAL,
            work_longitude REAL,
            allowed_radius_meters INTEGER NOT NULL DEFAULT 100,
            max_gps_accuracy_meters INTEGER NOT NULL DEFAULT 50,
            work_start_time TEXT NOT NULL DEFAULT '09:00',
            work_end_time TEXT NOT NULL DEFAULT '17:00',
            grace_period_minutes INTEGER NOT NULL DEFAULT 15,
            updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # صف الإعدادات الافتراضي لازم يكون موجود دايمًا عشان نقدر نعمل عليه UPDATE بسيطة
        cur.execute("INSERT OR IGNORE INTO supervisor_attendance_settings (id) VALUES (1)")

        # جدول مواعيد كل يوم في الأسبوع - المعاد مش لازم يكون ثابت لكل الأيام
        # (مثلاً يوم ممكن يبدأ الساعة 9 ويوم تاني يبدأ الساعة 2، ويوم ممكن يكون
        # إجازة أصلًا). day_of_week بيتبع Python's date.weekday(): الإثنين=0 ...
        # الأحد=6. لو يوم مسجل is_working_day=0 مفيش تأخير بيتحسب فيه خالص.
        cur.execute("""
        CREATE TABLE IF NOT EXISTS supervisor_attendance_weekly_schedule (
            day_of_week INTEGER PRIMARY KEY CHECK (day_of_week BETWEEN 0 AND 6),
            is_working_day INTEGER NOT NULL DEFAULT 1,
            work_start_time TEXT NOT NULL DEFAULT '09:00',
            updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)
        # تعبئة الأيام السبعة بالإعداد العام الحالي كقيمة افتراضية لو الجدول فاضي
        # (أول مرة الجدول ده بيتعمل - مايكررش فوق البيانات الموجودة بعد كده)
        default_start_row = cur.execute(
            "SELECT work_start_time FROM supervisor_attendance_settings WHERE id=1"
        ).fetchone()
        default_start = default_start_row["work_start_time"] if default_start_row else "09:00"
        for dow in range(7):
            cur.execute(
                "INSERT OR IGNORE INTO supervisor_attendance_weekly_schedule (day_of_week, is_working_day, work_start_time) VALUES (?, 1, ?)",
                (dow, default_start)
            )

        # سجل حضور/انصراف المشرفين - صف واحد لكل مشرف لكل يوم
        cur.execute("""
        CREATE TABLE IF NOT EXISTS supervisor_attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            supervisor_id INTEGER NOT NULL,
            attendance_date TEXT NOT NULL,

            check_in TEXT,
            check_in_latitude REAL,
            check_in_longitude REAL,
            check_in_accuracy REAL,
            check_in_distance REAL,

            check_out TEXT,
            check_out_latitude REAL,
            check_out_longitude REAL,
            check_out_accuracy REAL,
            check_out_distance REAL,

            status TEXT NOT NULL DEFAULT 'incomplete' CHECK(status IN ('present','late','absent','excused','incomplete')),
            late_minutes INTEGER NOT NULL DEFAULT 0,
            working_minutes INTEGER,

            notes TEXT,

            modified_by_admin INTEGER REFERENCES users(id) ON DELETE SET NULL,
            modified_at TEXT,
            modification_reason TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (supervisor_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(supervisor_id, attendance_date)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_supervisor_attendance_supervisor ON supervisor_attendance(supervisor_id, attendance_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_supervisor_attendance_date ON supervisor_attendance(attendance_date)")

        # ---------------------------------------------------------------
        # التقويم - أحداث (حصص/امتحانات/مراجعات) تظهر لمجموعة بعينها أو لمرحلة
        # دراسية كاملة. المشرف بيضيف لمجموعاته بس، والأدمن/مشرف المشرفين
        # يقدروا يضيفوا لمرحلة كاملة كمان.
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS calendar_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            event_type TEXT NOT NULL DEFAULT 'session' CHECK(event_type IN ('session','exam','review','other')),
            event_date TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            group_id INTEGER,
            stage_id INTEGER,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (stage_id) REFERENCES stages(id) ON DELETE CASCADE,
            FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(event_date)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_group ON calendar_events(group_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_stage ON calendar_events(stage_id)")

        # ترحيل قيد الأدوار القديم عشان يسمح بدور head_supervisor الجديد
        _migrate_users_role_check(cur)

        # ترحيل المراحل القديمة (حذف الإعدادي بالكامل + تحويل الثانوي العامة لأول صف)
        _migrate_stages(cur)

        # تعبئة المراحل الثابتة لو الجدول فاضي
        for stage_name in STAGES:
            cur.execute("INSERT OR IGNORE INTO stages (name) VALUES (?)", (stage_name,))

        # تعبئة المحافظات الـ27 الثابتة لو الجدول فاضي (مفيش إدارة يدوية ليها)
        for gov_name in GOVERNORATES:
            cur.execute("INSERT OR IGNORE INTO governorates (name) VALUES (?)", (gov_name,))

        # إنشاء حساب أدمن افتراضي لو مفيش ولا أدمن في النظام
        admin_exists = cur.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
        if not admin_exists:
            cur.execute(
                "INSERT INTO users (username, password_hash, role, full_name) VALUES (?, ?, 'admin', ?)",
                ("admin", hash_password("admin123"), "الأدمن العام")
            )

        conn.commit()


if __name__ == "__main__":
    init_db()
    print("تم إنشاء قاعدة البيانات والجداول بنجاح ✅")
    print("بيانات دخول الأدمن الافتراضية -> username: admin | password: admin123")
