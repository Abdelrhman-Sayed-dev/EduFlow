"""
database.py
ملف قاعدة البيانات - مسؤول عن إنشاء الجداول والاتصال بـ SQLite

الهيكل الهرمي:
    المرحلة (إعدادي / ثانوي)
        -> المحافظة
            -> المجموعة (تابعة لمرحلة ومحافظة معينة، وليها مشرف واحد مسؤول عنها)
                -> الطالب (تابع لمجموعة معينة)

الأدوار في النظام:
    admin      -> الأدمن العام: بيضيف المشرفين والطلاب وعنده كل الصلاحيات
    teacher    -> المدرس: بيتابع كل حاجة + جدول مواعيده الخاص
    supervisor -> المشرف: مسؤول عن مجموعة واحدة أو أكتر، وبس اللي يقدر يرفع سبورة الحصة
    student    -> الطالب: بيتابع درجاته وحضوره وسبورة مجموعته بس
"""

import sqlite3
import hashlib
import secrets
import bcrypt
from datetime import datetime, timedelta
from contextlib import contextmanager

DB_NAME = "teacher_system.db"

# المراحل الدراسية الثابتة في النظام
STAGES = ["إعدادي", "ثانوي"]

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


def gen_access_code() -> str:
    """كود دخول قصير للطالب (مثال: ST-4F92AB)"""
    return "ST-" + secrets.token_hex(3).upper()


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


def cleanup_expired_sessions(conn):
    """مسح الجلسات اللي انتهت صلاحيتها (يتنفذ بهدوء عند كل تسجيل دخول)"""
    now = datetime.utcnow().isoformat(timespec="seconds")
    try:
        conn.execute("DELETE FROM sessions WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
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
        # المستخدمين (أدمن - مدرس - مشرف) - تسجيل دخول بيوزر وباسورد
        # ---------------------------------------------------------------
        cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','teacher','supervisor')),
            full_name TEXT NOT NULL,
            phone TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

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

        # جدول الكويزات - كل كويز ممكن يكون عام أو مرتبط بمجموعة معينة
        cur.execute("""
        CREATE TABLE IF NOT EXISTS quizzes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            quiz_date TEXT,
            max_score REAL NOT NULL DEFAULT 100,
            group_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        )
        """)

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

        # جدول الحضور والغياب
        cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            session_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('present','absent','late','excused')),
            notes TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
            UNIQUE(student_id, session_date)
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

        # تعبئة المراحل الثابتة لو الجدول فاضي
        for stage_name in STAGES:
            cur.execute("INSERT OR IGNORE INTO stages (name) VALUES (?)", (stage_name,))

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
