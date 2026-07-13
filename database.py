import sqlite3
import hashlib
import secrets
import bcrypt
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_NAME = "teacher_system.db"

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
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
            UNIQUE(student_id, quiz_id)
        )
        """)

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
            uploaded_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
        )
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
