"""
backend.py
الباك اند الرئيسي للسيستم - مبني على FastAPI
يوفر API كامل لإدارة:
  المراحل - المحافظات - المجموعات - الطلاب - الكويزات - الدرجات - الحضور
  + نظام تسجيل دخول بـ 3 أدوار (أدمن - مدرس - مشرف) + دخول الطالب بكود خاص
  + المشرفين (تعيين كل مشرف على مجموعة/مجموعات معينة بس)
  + جدول مواعيد المدرس
  + سبورة الحصة (صور شرح كل حصة، خاصة بكل مجموعة)
"""

import os
import base64
import uuid
import asyncio
import calendar
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header, Depends, Request, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional

from database import (
    get_connection, init_db, hash_password, verify_password, gen_token,
    gen_access_code, gen_numeric_code, gen_temp_password, session_expiry, cleanup_expired_sessions,
    is_login_blocked, record_failed_login, clear_failed_logins, cleanup_old_login_attempts,
    get_first_subscription_date, get_paid_months
)

app = FastAPI(title="منصة المدرس - نظام إدارة الطلاب والمجموعات")

# ---------------------------------------------------------------------------
# CORS: في الإنتاج حدد دومين موقعك في متغير البيئة ALLOWED_ORIGINS
# مثال: ALLOWED_ORIGINS=https://myteacher-platform.com
# لو متغير البيئة مش موجود، بيفتح للكل (مناسب للتجربة بس مش للإنتاج)
# ---------------------------------------------------------------------------
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_env.split(",")] if _allowed_origins_env != "*" else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOWED_ORIGINS != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# تشغيل قاعدة البيانات أول ما السيرفر يبدأ
init_db()

# ---------------------------------------------------------------------------
# تحذيرات أمان بتظهر في اللوجز عند تشغيل السيرفر - عشان محدش ينسى يقفل الثغرات
# دي قبل ما يعمل نشر فعلي (production)
# ---------------------------------------------------------------------------
def _print_startup_security_warnings():
    if ALLOWED_ORIGINS == ["*"]:
        print("=" * 70)
        print("⚠️  تحذير أمان: CORS مفتوح لأي دومين (ALLOWED_ORIGINS=*).")
        print("   حدد دومين موقعك الحقيقي في متغير البيئة ALLOWED_ORIGINS قبل الإنتاج.")
        print("=" * 70)
    try:
        with get_connection() as conn:
            admin = conn.execute(
                "SELECT password_hash FROM users WHERE username='admin' AND role='admin'"
            ).fetchone()
            if admin and verify_password("admin123", admin["password_hash"])[0]:
                print("=" * 70)
                print("🚨 تحذير أمان خطير: حساب الأدمن لسه بالباسورد الافتراضي (admin123)!")
                print("   غيّره فورًا من إعدادات الحساب قبل ما تنشر السيستم فعليًا.")
                print("=" * 70)
    except Exception:
        pass


_print_startup_security_warnings()

# ---------------------------------------------------------------------------
# نسخ احتياطي دوري تلقائي (بديل Cron Job، لأن Render مش بيسمح لخدمات الـ Cron
# بالوصول للـ Persistent Disk - فالحل إننا نشغّل الباك أب كـ background task
# جوه نفس السيرفيس اللي شغال عليه السيرفر، عشان يقدر يوصل لنفس الديسك)
# تقدر تتحكم في عدد الساعات بين كل نسخة عن طريق متغير البيئة BACKUP_INTERVAL_HOURS
# ---------------------------------------------------------------------------
from backup import run_backup

BACKUP_INTERVAL_HOURS = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))


async def _periodic_backup_task():
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_HOURS * 3600)
        try:
            run_backup()
        except Exception as e:
            print(f"⚠️ فشل النسخ الاحتياطي الدوري: {e}")


@app.on_event("startup")
async def _start_periodic_backup():
    asyncio.create_task(_periodic_backup_task())
    print(f"✅ تم تفعيل النسخ الاحتياطي التلقائي كل {BACKUP_INTERVAL_HOURS} ساعة")

# مجلد رفع الصور (سبورة الحصص) - بيتم تخزين الصور كملفات على الـ disk
# مش base64 جوه قاعدة البيانات، عشان الداتابيز ما تكبرش وتبقى بطيئة
UPLOADS_DIR = os.environ.get("UPLOADS_DIR", os.path.join(os.environ.get("DATA_DIR", "."), "uploads"))
BOARD_IMAGES_DIR = os.path.join(UPLOADS_DIR, "board_images")
os.makedirs(BOARD_IMAGES_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")

# مجلد فيديوهات الطلاب - عمدًا برّا مجلد /uploads العام (اللي متاح لأي حد بالرابط من غير تسجيل دخول)
# عشان الفيديوهات متتفتحش غير من خلال endpoint فيه تحقق صلاحيات (مشرف المجموعة / الطالب نفسه / الأدمن)
VIDEOS_DIR = os.environ.get("VIDEOS_DIR", os.path.join(os.environ.get("DATA_DIR", "."), "private_videos"))
os.makedirs(VIDEOS_DIR, exist_ok=True)
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
MAX_VIDEO_SIZE_BYTES = 2 * 1024 * 1024 * 1024  # 2 جيجا حد أقصى للفيديو الواحد


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class GovernorateIn(BaseModel):
    name: str


class ScheduleSlotIn(BaseModel):
    day_of_week: str
    start_time: str
    end_time: Optional[str] = None


class GroupIn(BaseModel):
    name: str = Field(..., max_length=150)
    stage_id: int
    governorate_id: int
    notes: Optional[str] = Field(None, max_length=2000)
    session_price: Optional[float] = None
    monthly_fee: Optional[float] = None
    supervisor_id: Optional[int] = None
    schedule_slots: Optional[list[ScheduleSlotIn]] = None  # مواعيد المجموعة (يوم + وقت)


class StudentIn(BaseModel):
    full_name: str = Field(..., max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    parent_phone: Optional[str] = Field(None, max_length=30)
    group_id: int
    notes: Optional[str] = Field(None, max_length=2000)


class BulkStudentIn(BaseModel):
    full_name: str = Field(..., max_length=150)
    group_id: int
    phone: Optional[str] = Field(None, max_length=30)
    parent_phone: Optional[str] = Field(None, max_length=30)
    notes: Optional[str] = Field(None, max_length=2000)


class BulkStudentsRequest(BaseModel):
    students: list[BulkStudentIn]


class QuizIn(BaseModel):
    title: str = Field(..., max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    quiz_date: Optional[str] = None
    max_score: float = 100
    group_id: Optional[int] = None  # نظام قديم للتوافق - كويز خاص بمجموعة واحدة
    stage_id: Optional[int] = None  # كويز عام على مستوى مرحلة كاملة (النظام الجديد)
    session_number: Optional[int] = None
    image_data: Optional[str] = None  # صورة الكويز/الامتحان (base64)
    version_label: Optional[str] = Field(None, max_length=100)  # اسم النموذج لو فيه أكتر من نموذج امتحان
    quiz_type: str = "quiz"  # "quiz" كويز عادي أو "exam" امتحان شامل


class NotificationOut(BaseModel):
    id: int
    title: str
    body: Optional[str] = None
    is_read: bool
    created_at: str


class HomeworkIn(BaseModel):
    group_id: int
    session_number: int
    session_date: Optional[str] = None
    description: str = Field(..., max_length=3000)


class HomeworkSubmissionIn(BaseModel):
    student_id: int
    done: Optional[bool] = None
    notes: Optional[str] = Field(None, max_length=2000)


class QuizScoreIn(BaseModel):
    student_id: int
    quiz_id: int
    score: float
    notes: Optional[str] = Field(None, max_length=2000)


class AttendanceIn(BaseModel):
    student_id: int
    session_date: str
    status: str  # present / absent / late / excused
    notes: Optional[str] = Field(None, max_length=2000)
    session_number: int = 1  # رقم الحصة في نفس اليوم (لو في أكتر من حصة)


class AttendanceCodeIn(BaseModel):
    access_code: str
    session_date: str
    status: str
    session_number: int = 1


class LoginIn(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., max_length=200)


class StudentLoginIn(BaseModel):
    access_code: str = Field(..., max_length=50)


class CodeLoginIn(BaseModel):
    """تسجيل دخول موحّد بالكود - يصلح للطالب أو المشرف أو المدرس"""
    access_code: str = Field(..., max_length=50)
    device_id: Optional[str] = Field(None, max_length=100)


class UserIn(BaseModel):
    username: str = Field(..., max_length=100)
    password: Optional[str] = Field(None, min_length=6, max_length=200)  # لو فاضي، النظام يولّد كلمة مرور تلقائية ويرجّعها
    full_name: str = Field(..., max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    role: str = "supervisor"  # supervisor أو teacher (الأدمن مينضافش من هنا)
    governorate_id: Optional[int] = None  # للمشرف بس - المحافظة المسؤول عنها


class ChangePasswordIn(BaseModel):
    current_password: str = Field(..., max_length=200)
    new_password: str = Field(..., min_length=6, max_length=200)


class UserUpdateIn(BaseModel):
    full_name: Optional[str] = Field(None, max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    password: Optional[str] = Field(None, min_length=6, max_length=200)
    is_active: Optional[bool] = None
    governorate_id: Optional[int] = None


class ScheduleIn(BaseModel):
    day_of_week: str
    start_time: str
    end_time: Optional[str] = None
    group_id: Optional[int] = None
    title: Optional[str] = Field(None, max_length=200)
    notes: Optional[str] = Field(None, max_length=2000)


class BoardImageIn(BaseModel):
    group_id: int
    session_number: int
    session_date: Optional[str] = None
    image_data: str  # base64 data url
    caption: Optional[str] = Field(None, max_length=500)


class AssignSupervisorIn(BaseModel):
    supervisor_id: Optional[int] = None  # None = شيل المشرف من المجموعة


class BehaviorNoteIn(BaseModel):
    student_id: int
    note: str = Field(..., max_length=2000)
    note_type: str = "neutral"  # positive / negative / neutral


class PaymentIn(BaseModel):
    student_id: int
    month: str  # صيغة YYYY-MM
    amount: Optional[float] = None
    is_paid: bool = False
    paid_date: Optional[str] = None
    notes: Optional[str] = Field(None, max_length=2000)


class StudentRequestIn(BaseModel):
    request_type: str  # attendance_change / issue / explanation / other
    details: Optional[str] = Field(None, max_length=2000)


class StudentRequestStatusIn(BaseModel):
    status: str  # pending / in_progress / resolved
    supervisor_reply: Optional[str] = Field(None, max_length=2000)


# ---------------------------------------------------------------------------
# نظام تسجيل الدخول والصلاحيات
# ---------------------------------------------------------------------------

def current_month_str():
    """الشهر الحالي بصيغة YYYY-MM - نفس الصيغة المستخدمة في جدول المدفوعات"""
    return datetime.utcnow().strftime("%Y-%m")


def is_student_subscribed(conn, student_id: int) -> bool:
    """هل الطالب سدد اشتراك الشهر الحالي؟ - ده اللي بيتحكم في السماح بمشاهدة المحتوى من عدمه"""
    row = conn.execute(
        "SELECT is_paid FROM payments WHERE student_id=? AND month=?",
        (student_id, current_month_str())
    ).fetchone()
    return bool(row and row["is_paid"])


def student_content_cutoff(session) -> Optional[str]:
    """
    فلتر عام (Global Filter): تاريخ أول اشتراك للطالب صاحب الجلسة الحالية.
    - لو الجلسة طالب: بيرجع تاريخ أول اشتراك (أو None لو مفيش اشتراك مسدد أصلاً).
    - لو الجلسة مش طالب (أدمن/مشرف/مدرس): بيرجع None يعني من غير أي فلترة تاريخ،
      لأن الشرط ده خاص بتجربة الطالب نفسه عند دخوله للمنصة.
    """
    if session.get("role") == "student":
        return session.get("subscription_since")
    return None


def _resolve_session_by_token(token: Optional[str]):
    """المنطق المشترك لقراءة الجلسة من التوكن - مستخدم في get_current_session
    وفي get_session_for_media (اللي بتقبل التوكن من الـ query string كمان)"""
    if not token:
        raise HTTPException(status_code=401, detail="لازم تسجل دخول الأول")
    with get_connection() as conn:
        sess = conn.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        if not sess:
            raise HTTPException(status_code=401, detail="الجلسة منتهية، سجل دخول تاني")

        if sess["expires_at"] and sess["expires_at"] < datetime.utcnow().isoformat(timespec="seconds"):
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            raise HTTPException(status_code=401, detail="انتهت صلاحية الجلسة، سجل دخول تاني")

        if sess["user_type"] == "user":
            user = conn.execute("SELECT * FROM users WHERE id=?", (sess["user_id"],)).fetchone()
            if not user or not user["is_active"]:
                raise HTTPException(status_code=401, detail="الحساب غير مفعّل")
            return {
                "type": "user", "id": user["id"], "role": user["role"],
                "full_name": user["full_name"], "username": user["username"]
            }
        else:
            student = conn.execute("SELECT * FROM students WHERE id=?", (sess["user_id"],)).fetchone()
            if not student or not student["is_active"]:
                raise HTTPException(status_code=401, detail="الحساب غير مفعّل")
            # لازم يكون سدد اشتراك الشهر الحالي عشان يقدر يستخدم أي جزء من المنصة -
            # ده تحقق مركزي بيغطي كل الـ endpoints تلقائيًا من غير ما نعدل كل واحدة لوحدها
            if not is_student_subscribed(conn, student["id"]):
                raise HTTPException(status_code=402, detail="يجب سداد الاشتراك الشهري لمشاهدة المحتوى")
            # تاريخ أول اشتراك للطالب - نقطة البداية العامة لعرض أي محتوى له في المنصة
            # (فلتر عام Global Filter بيتطبق في كل الـ endpoints اللي بترجع بيانات مؤرخة)
            subscription_since = get_first_subscription_date(conn, student["id"])
            return {
                "type": "student", "id": student["id"], "role": "student",
                "full_name": student["full_name"], "group_id": student["group_id"],
                "subscription_since": subscription_since
            }


def get_current_session(authorization: Optional[str] = Header(None)):
    """يقرأ التوكن من الهيدر Authorization: Bearer <token> ويتأكد إنه لسه صالح"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="لازم تسجل دخول الأول")
    token = authorization.split(" ", 1)[1].strip()
    return _resolve_session_by_token(token)


def get_session_for_media(request: Request, token: Optional[str] = Query(None)):
    """زي get_current_session بالظبط، بس بتقبل التوكن من الـ query string كمان -
    ده ضروري لعناصر <video>/<img> لأن المتصفح بيطلبها مباشرة من غير ما نقدر
    نضيف هيدر Authorization مخصص عليها"""
    authorization = request.headers.get("authorization")
    if authorization and authorization.startswith("Bearer "):
        tok = authorization.split(" ", 1)[1].strip()
    else:
        tok = token
    return _resolve_session_by_token(tok)


def require_roles(*roles):
    """Dependency factory: يسمح بالدخول بس للأدوار المحددة"""
    def checker(session=Depends(get_current_session)):
        if session["role"] not in roles:
            raise HTTPException(status_code=403, detail="مفيش صلاحية للوصول لده")
        return session
    return checker


def supervised_group_ids(conn, supervisor_id):
    rows = conn.execute("SELECT id FROM groups WHERE supervisor_id=?", (supervisor_id,)).fetchall()
    return [r["id"] for r in rows]


def assert_supervisor_owns_group(conn, session, group_id):
    """يتأكد إن المشرف بيتعامل مع مجموعته بس"""
    if session["role"] == "supervisor":
        grp = conn.execute("SELECT supervisor_id FROM groups WHERE id=?", (group_id,)).fetchone()
        if not grp or grp["supervisor_id"] != session["id"]:
            raise HTTPException(status_code=403, detail="مش مسموح لك تتعامل مع مجموعة غير مجموعتك")


def create_notification(conn, student_id: int, title: str, body: str = None):
    """إنشاء إشعار جديد للطالب - بيتنادى تلقائي بعد أي عملية مشرف تخص الطالب"""
    try:
        conn.execute(
            "INSERT INTO notifications (user_type, user_id, title, body) VALUES ('student', ?, ?, ?)",
            (student_id, title, body)
        )
    except Exception:
        pass


def create_user_notification(conn, user_id: int, title: str, body: str = None):
    """إنشاء إشعار جديد لمستخدم (مشرف/مدرس/أدمن) - مثلاً لما طالب يقدم طلب جديد"""
    try:
        conn.execute(
            "INSERT INTO notifications (user_type, user_id, title, body) VALUES ('user', ?, ?, ?)",
            (user_id, title, body)
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# تذكير سداد الاشتراك الشهري
# قبل ما الشهر يخلص بعدد أيام معين، أي طالب لسه مسددش اشتراك الشهر الحالي
# بيوصله إشعار تذكير (مرة واحدة بس لكل شهر) عشان يسدد قبل ما المحتوى يتحجب عنه
# ---------------------------------------------------------------------------
PAYMENT_REMINDER_DAYS_BEFORE = int(os.environ.get("PAYMENT_REMINDER_DAYS_BEFORE", "5"))
REMINDER_CHECK_INTERVAL_SECONDS = int(os.environ.get("REMINDER_CHECK_INTERVAL_SECONDS", str(6 * 60 * 60)))


def days_left_in_month(now: Optional[datetime] = None) -> int:
    """عدد الأيام المتبقية على نهاية الشهر الحالي (بتوقيت UTC)"""
    now = now or datetime.utcnow()
    last_day = calendar.monthrange(now.year, now.month)[1]
    return last_day - now.day


def _payment_reminder_title(month: str) -> str:
    return f"تذكير: قرّب سداد اشتراك {month}"


def send_subscription_payment_reminders(force: bool = False) -> int:
    """
    يفحص كل الطلاب النشطين اللي لسه مسددوش اشتراك الشهر الحالي، ولو باقي على نهاية
    الشهر PAYMENT_REMINDER_DAYS_BEFORE يوم أو أقل، يبعتلهم إشعار تذكير بالسداد.
    كل طالب بياخد إشعار واحد بس لكل شهر (بيتأكد إن مفيش إشعار بنفس العنوان اتبعتله قبل كده).
    force=True بتتجاهل شرط عدد الأيام المتبقية (مفيد للتجربة اليدوية من الأدمن).
    """
    remaining = days_left_in_month()
    if not force and remaining > PAYMENT_REMINDER_DAYS_BEFORE:
        return 0

    month = current_month_str()
    title = _payment_reminder_title(month)
    sent = 0
    with get_connection() as conn:
        students = conn.execute("SELECT id, full_name FROM students WHERE is_active=1").fetchall()
        for st in students:
            if is_student_subscribed(conn, st["id"]):
                continue
            already_sent = conn.execute(
                "SELECT id FROM notifications WHERE user_type='student' AND user_id=? AND title=?",
                (st["id"], title)
            ).fetchone()
            if already_sent:
                continue
            create_notification(
                conn, st["id"], title,
                f"باقي {remaining} يوم على نهاية الشهر ولسه مسددتش اشتراك شهر {month}. "
                f"سدد بسرعة عشان محتوى المنصة يفضل متاح ليك من غير أي انقطاع."
            )
            sent += 1
        conn.commit()
    return sent


async def _payment_reminder_background_loop():
    """Loop خلفي بيشتغل طول ما السيرفر شغال، وبيفحص كل REMINDER_CHECK_INTERVAL_SECONDS
    لو فيه طلاب محتاجين إشعار تذكير سداد"""
    while True:
        try:
            sent = send_subscription_payment_reminders()
            if sent:
                print(f"📩 تم إرسال {sent} إشعار تذكير بسداد الاشتراك")
        except Exception as e:
            print(f"⚠️ خطأ أثناء إرسال تذكيرات السداد: {e}")
        await asyncio.sleep(REMINDER_CHECK_INTERVAL_SECONDS)


@app.on_event("startup")
async def _start_payment_reminder_job():
    asyncio.create_task(_payment_reminder_background_loop())


# ---------------------------------------------------------------------------
# الصفحة الرئيسية (الواجهة)
# ---------------------------------------------------------------------------

@app.get("/")
def serve_frontend():
    return FileResponse("frontend.html")


# ---------------------------------------------------------------------------
# تسجيل الدخول
# ---------------------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """بيجيب IP بتاع الطلب - بياخد بالحسبان إنه ممكن يكون وراء proxy (زي Render)"""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/auth/login")
def login(data: LoginIn, request: Request):
    """تسجيل دخول الأدمن / المدرس / المشرف"""
    ip_key = f"ip:{_client_ip(request)}"
    user_key = f"user:{data.username.strip().lower()}"
    with get_connection() as conn:
        cleanup_expired_sessions(conn)
        cleanup_old_login_attempts(conn)

        if is_login_blocked(conn, ip_key) or is_login_blocked(conn, user_key):
            raise HTTPException(status_code=429, detail="محاولات دخول كتير غلط، استنى شوية وحاول تاني")

        user = conn.execute(
            "SELECT * FROM users WHERE username=?", (data.username,)
        ).fetchone()
        if not user:
            record_failed_login(conn, ip_key)
            record_failed_login(conn, user_key)
            conn.commit()  # لازم commit قبل الـ raise، لأن get_connection بيعمل rollback عند أي Exception
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غلط")

        ok, needs_upgrade = verify_password(data.password, user["password_hash"])
        if not ok:
            record_failed_login(conn, ip_key)
            record_failed_login(conn, user_key)
            conn.commit()  # لازم commit قبل الـ raise، لأن get_connection بيعمل rollback عند أي Exception
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غلط")
        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="الحساب موقوف، كلم الأدمن")

        clear_failed_logins(conn, ip_key)
        clear_failed_logins(conn, user_key)

        # لو الحساب لسه بالتشفير القديم (sha256)، حدّثه تلقائي لـ bcrypt دلوقتي
        if needs_upgrade:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(data.password), user["id"]))

        token = gen_token()
        conn.execute(
            "INSERT INTO sessions (token, user_type, user_id, expires_at) VALUES (?, 'user', ?, ?)",
            (token, user["id"], session_expiry())
        )
        return {
            "token": token,
            "role": user["role"],
            "full_name": user["full_name"],
            "username": user["username"],
            "id": user["id"]
        }


@app.post("/api/auth/login-code")
def login_with_code(data: CodeLoginIn, request: Request = None):
    """
    تسجيل دخول موحّد بالكود - يصلح للطالب أو المشرف أو المدرس.
    بيدور على الكود في جدول الطلاب الأول، ولو مش موجود يدور في جدول المستخدمين (مشرف/مدرس).
    """
    code = data.access_code.strip()
    ip_key = f"ip:{_client_ip(request)}" if request is not None else None
    code_key = f"code:{code.lower()}"
    with get_connection() as conn:
        cleanup_expired_sessions(conn)
        cleanup_old_login_attempts(conn)

        if is_login_blocked(conn, code_key) or (ip_key and is_login_blocked(conn, ip_key)):
            raise HTTPException(status_code=429, detail="محاولات دخول كتير غلط، استنى شوية وحاول تاني")

        student = conn.execute("SELECT * FROM students WHERE access_code=?", (code,)).fetchone()
        if student:
            if not student["is_active"]:
                raise HTTPException(status_code=403, detail="الحساب موقوف، كلم المشرف")

            incoming_device = (data.device_id or "").strip()
            if incoming_device:
                bound_device = student["device_id"]
                if not bound_device:
                    # أول مرة يتسجل بيها دخول بالكود ده - نربط الكود بالجهاز ده
                    conn.execute("UPDATE students SET device_id=? WHERE id=?", (incoming_device, student["id"]))
                elif bound_device != incoming_device:
                    # الكود متربط بجهاز تاني بالفعل - نمنع الدخول من جهاز مختلف
                    record_failed_login(conn, code_key)
                    conn.commit()
                    raise HTTPException(
                        status_code=403,
                        detail="الكود ده مسجّل بالفعل على جهاز تاني. لو الجهاز اتغيّر، كلم المشرف يعمل reset لجهازك"
                    )

            clear_failed_logins(conn, code_key)
            if ip_key:
                clear_failed_logins(conn, ip_key)
            # منع أكتر من شخص يدخل بنفس الكود في نفس الوقت: أي تسجيل دخول جديد بالكود ده
            # بيلغي أي جلسة سابقة مفتوحة لنفس الطالب (يعني لو صاحب الكود بعته لصاحبه، دخول
            # صاحبه هيطلّع صاحب الكود الأصلي تلقائيًا من الجلسة القديمة بتاعته)
            conn.execute("DELETE FROM sessions WHERE user_type='student' AND user_id=?", (student["id"],))
            token = gen_token()
            conn.execute(
                "INSERT INTO sessions (token, user_type, user_id, expires_at) VALUES (?, 'student', ?, ?)",
                (token, student["id"], session_expiry())
            )
            group = conn.execute("SELECT * FROM groups WHERE id=?", (student["group_id"],)).fetchone()
            return {
                "token": token, "role": "student", "full_name": student["full_name"],
                "id": student["id"], "group_id": student["group_id"],
                "group_name": group["name"] if group else None,
                "subscription_active": is_student_subscribed(conn, student["id"]),
                "subscription_since": get_first_subscription_date(conn, student["id"]),
            }

        user = conn.execute("SELECT * FROM users WHERE access_code=?", (code,)).fetchone()
        if user:
            if not user["is_active"]:
                raise HTTPException(status_code=403, detail="الحساب موقوف، كلم الأدمن")
            clear_failed_logins(conn, code_key)
            if ip_key:
                clear_failed_logins(conn, ip_key)
            token = gen_token()
            conn.execute(
                "INSERT INTO sessions (token, user_type, user_id, expires_at) VALUES (?, 'user', ?, ?)",
                (token, user["id"], session_expiry())
            )
            return {
                "token": token, "role": user["role"], "full_name": user["full_name"],
                "username": user["username"], "id": user["id"]
            }

        record_failed_login(conn, code_key)
        if ip_key:
            record_failed_login(conn, ip_key)
        conn.commit()  # لازم commit قبل الـ raise، لأن get_connection بيعمل rollback عند أي Exception
        raise HTTPException(status_code=401, detail="كود الدخول غلط")


@app.post("/api/auth/student-login")
def student_login(data: StudentLoginIn, request: Request):
    """تسجيل دخول الطالب بكود الدخول الخاص بيه (للتوافق مع نسخ قديمة - استخدم /login-code الأحدث)"""
    return login_with_code(CodeLoginIn(access_code=data.access_code), request)


@app.post("/api/auth/logout")
def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
        with get_connection() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    return {"message": "تم تسجيل الخروج"}


@app.get("/api/auth/me")
def me(session=Depends(get_current_session)):
    return session


@app.put("/api/auth/change-password")
def change_my_password(data: ChangePasswordIn, session=Depends(get_current_session)):
    """
    يسمح للمستخدم (أدمن/مدرس/مشرف) بتغيير كلمة مروره بنفسه.
    الطالب مش له كلمة مرور (بيدخل بكود)، فمينفعش يستخدم ده.
    """
    if session["type"] != "user":
        raise HTTPException(status_code=403, detail="الطالب بيدخل بكود مش بكلمة مرور")

    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session["id"],)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="المستخدم غير موجود")

        ok, _ = verify_password(data.current_password, user["password_hash"])
        if not ok:
            raise HTTPException(status_code=401, detail="كلمة المرور الحالية غلط")

        if len(data.new_password) < 6:
            raise HTTPException(status_code=400, detail="كلمة المرور الجديدة لازم تكون 6 حروف/أرقام على الأقل")

        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(data.new_password), session["id"])
        )
        return {"message": "تم تغيير كلمة المرور بنجاح"}


# ---------------------------------------------------------------------------
# المراحل - Stages (الصف الأول / الثاني / الثالث الثانوي) - ثابتة، قراءة فقط
# ---------------------------------------------------------------------------

@app.get("/api/stages")
def get_stages(session=Depends(get_current_session)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM stages ORDER BY id").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# المحافظات - Governorates
# ---------------------------------------------------------------------------

@app.get("/api/governorates")
def get_governorates(session=Depends(get_current_session)):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM governorates ORDER BY name").fetchall()
        return [dict(r) for r in rows]


@app.post("/api/governorates")
def add_governorate(gov: GovernorateIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM governorates WHERE name=?", (gov.name,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="المحافظة دي موجودة بالفعل")
        cur = conn.execute("INSERT INTO governorates (name) VALUES (?)", (gov.name,))
        return {"id": cur.lastrowid, "message": "تم إضافة المحافظة بنجاح"}


@app.delete("/api/governorates/{gov_id}")
def delete_governorate(gov_id: int, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        result = conn.execute("DELETE FROM governorates WHERE id=?", (gov_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="المحافظة غير موجودة")
        return {"message": "تم حذف المحافظة"}


# ---------------------------------------------------------------------------
# المجموعات - Groups (تابعة لمرحلة ومحافظة، وممكن ليها مشرف)
# ---------------------------------------------------------------------------

@app.get("/api/groups")
def get_groups(stage_id: Optional[int] = None, governorate_id: Optional[int] = None,
               session=Depends(get_current_session)):
    """جلب المجموعات - المشرف يشوف مجموعاته بس، والطالب يشوف مجموعته بس"""
    query = """
        SELECT g.id, g.name, g.notes, g.session_price, g.monthly_fee, g.stage_id, g.governorate_id,
               g.supervisor_id, g.created_at, st.name as stage_name, gov.name as governorate_name,
               u.full_name as supervisor_name,
               (SELECT COUNT(*) FROM students s WHERE s.group_id = g.id) as students_count
        FROM groups g
        JOIN stages st ON st.id = g.stage_id
        JOIN governorates gov ON gov.id = g.governorate_id
        LEFT JOIN users u ON u.id = g.supervisor_id
        WHERE 1=1
    """
    params = []
    if stage_id:
        query += " AND g.stage_id = ?"
        params.append(stage_id)
    if governorate_id:
        query += " AND g.governorate_id = ?"
        params.append(governorate_id)

    if session["role"] == "supervisor":
        query += " AND g.supervisor_id = ?"
        params.append(session["id"])
    elif session["role"] == "student":
        query += " AND g.id = ?"
        params.append(session["group_id"])

    query += " ORDER BY st.name, gov.name, g.name"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/groups/{group_id}/info")
def get_group_info(group_id: int, session=Depends(get_current_session)):
    """بيانات مجموعة معينة + بيانات المشرف (اسمه ورقمه) + مواعيد المجموعة"""
    with get_connection() as conn:
        group = conn.execute("""
            SELECT g.id, g.name, g.notes, g.session_price, g.monthly_fee, g.supervisor_id,
                   st.name as stage_name, gov.name as governorate_name,
                   u.full_name as supervisor_name, u.phone as supervisor_phone
            FROM groups g
            JOIN stages st ON st.id = g.stage_id
            JOIN governorates gov ON gov.id = g.governorate_id
            LEFT JOIN users u ON u.id = g.supervisor_id
            WHERE g.id = ?
        """, (group_id,)).fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")

        # مواعيد المجموعة من جدول المواعيد
        schedule = conn.execute("""
            SELECT day_of_week, start_time, end_time, title
            FROM teacher_schedule
            WHERE group_id = ?
            ORDER BY day_of_week, start_time
        """, (group_id,)).fetchall()

        result = dict(group)
        result["schedule"] = [dict(s) for s in schedule]
        return result


@app.post("/api/groups")
def add_group(group: GroupIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO groups (name, stage_id, governorate_id, notes, session_price, monthly_fee, supervisor_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (group.name, group.stage_id, group.governorate_id, group.notes,
                 group.session_price, group.monthly_fee, group.supervisor_id)
            )
        except Exception:
            raise HTTPException(status_code=400, detail="المجموعة دي موجودة بالفعل في نفس المرحلة والمحافظة")

        group_id = cur.lastrowid

        # إضافة مواعيد المجموعة (لو حددها الأدمن) - تتسجل في جدول المواعيد العام تلقائيًا
        if group.schedule_slots:
            for slot in group.schedule_slots:
                conn.execute(
                    """INSERT INTO teacher_schedule (day_of_week, start_time, end_time, group_id, title)
                       VALUES (?, ?, ?, ?, ?)""",
                    (slot.day_of_week, slot.start_time, slot.end_time, group_id, f"حصة {group.name}")
                )

        return {"id": group_id, "message": "تم إضافة المجموعة بنجاح"}


@app.put("/api/groups/{group_id}")
def update_group(group_id: int, group: GroupIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        result = conn.execute(
            """UPDATE groups SET name=?, stage_id=?, governorate_id=?, notes=?, session_price=?, monthly_fee=?, supervisor_id=?
               WHERE id=?""",
            (group.name, group.stage_id, group.governorate_id, group.notes,
             group.session_price, group.monthly_fee, group.supervisor_id, group_id)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")

        # لو الأدمن بعت مواعيد للمجموعة، نمسح القديم المرتبط بيها ونسجل الجديد
        # (عشان تظهر صح في "جدول المواعيد" عند المدرس وباقي الأدوار)
        if group.schedule_slots is not None:
            conn.execute("DELETE FROM teacher_schedule WHERE group_id=?", (group_id,))
            for slot in group.schedule_slots:
                conn.execute(
                    """INSERT INTO teacher_schedule (day_of_week, start_time, end_time, group_id, title)
                       VALUES (?, ?, ?, ?, ?)""",
                    (slot.day_of_week, slot.start_time, slot.end_time, group_id, f"حصة {group.name}")
                )
        return {"message": "تم تعديل المجموعة"}


class MonthlyFeeIn(BaseModel):
    monthly_fee: float


@app.put("/api/groups/{group_id}/monthly-fee")
def set_group_monthly_fee(group_id: int, payload: MonthlyFeeIn, session=Depends(require_roles("admin"))):
    """تحديد قيمة الاشتراك الشهري لكل طلاب المجموعة دفعة واحدة"""
    with get_connection() as conn:
        result = conn.execute("UPDATE groups SET monthly_fee=? WHERE id=?", (payload.monthly_fee, group_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
        return {"message": "تم تحديث قيمة الاشتراك"}


@app.put("/api/groups/{group_id}/supervisor")
def assign_supervisor(group_id: int, data: AssignSupervisorIn, session=Depends(require_roles("admin"))):
    """تعيين أو تغيير المشرف المسؤول عن مجموعة معينة"""
    with get_connection() as conn:
        if data.supervisor_id is not None:
            sup = conn.execute(
                "SELECT id FROM users WHERE id=? AND role='supervisor'", (data.supervisor_id,)
            ).fetchone()
            if not sup:
                raise HTTPException(status_code=404, detail="المشرف غير موجود")
        result = conn.execute(
            "UPDATE groups SET supervisor_id=? WHERE id=?", (data.supervisor_id, group_id)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
        return {"message": "تم تحديث المشرف المسؤول عن المجموعة"}


@app.delete("/api/groups/{group_id}")
def delete_group(group_id: int, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        result = conn.execute("DELETE FROM groups WHERE id=?", (group_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
        return {"message": "تم حذف المجموعة (وكل طلابها)"}


# ---------------------------------------------------------------------------
# الطلاب - Students
# ---------------------------------------------------------------------------

@app.get("/api/students/search")
def search_students(q: str = "", session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    """البحث عن طالب بالاسم أو الـ ID أو كود الحضور - يرجع بياناته + كل درجاته في الكويزات + حالة الاشتراك"""
    with get_connection() as conn:
        query = """
            SELECT s.id, s.full_name, s.phone, s.parent_phone, s.group_id,
                   g.name as group_name, st.name as stage_name, gov.name as governorate_name
            FROM students s
            JOIN groups g ON g.id = s.group_id
            JOIN stages st ON st.id = g.stage_id
            JOIN governorates gov ON gov.id = g.governorate_id
            WHERE (s.full_name LIKE ? OR CAST(s.id AS TEXT) = ? OR s.attendance_code = ?)
        """
        params = [f"%{q}%", q.strip(), q.strip()]
        if session["role"] == "supervisor":
            query += " AND g.supervisor_id = ?"
            params.append(session["id"])
        query += " ORDER BY s.full_name LIMIT 20"

        students = conn.execute(query, params).fetchall()
        result = []
        for s in students:
            sd = dict(s)
            # جلب كل درجات الطالب
            scores = conn.execute("""
                SELECT q.title, q.quiz_date, q.max_score, qs.score, qs.id as score_id
                FROM quiz_scores qs
                JOIN quizzes q ON q.id = qs.quiz_id
                WHERE qs.student_id = ?
                ORDER BY q.quiz_date DESC
            """, (s["id"],)).fetchall()
            sd["scores"] = [dict(sc) for sc in scores]
            # ملخص الحضور
            att = conn.execute("""
                SELECT status, COUNT(*) as cnt FROM attendance
                WHERE student_id = ? GROUP BY status
            """, (s["id"],)).fetchall()
            att_summary = {r["status"]: r["cnt"] for r in att}
            sd["attendance_summary"] = att_summary
            # حالة اشتراك الشهر الحالي
            sd["subscription_active"] = is_student_subscribed(conn, s["id"])
            # سجل الاشتراكات الشهرية (تاريخ السداد لكل شهر)
            payments_rows = conn.execute(
                "SELECT month, is_paid, paid_date, amount FROM payments WHERE student_id=? ORDER BY month DESC",
                (s["id"],)
            ).fetchall()
            sd["payments"] = [dict(p) for p in payments_rows]
            result.append(sd)
        return result


@app.get("/api/students")
def get_students(group_id: Optional[int] = None, stage_id: Optional[int] = None,
                  governorate_id: Optional[int] = None, session=Depends(get_current_session)):
    """جلب الطلاب - المشرف يشوف طلاب مجموعاته بس، والطالب يشوف بياناته بس"""
    query = """
        SELECT s.id, s.full_name, s.phone, s.parent_phone, s.notes, s.group_id, s.access_code,
               s.attendance_code, s.is_active,
               g.name as group_name, st.name as stage_name, gov.name as governorate_name
        FROM students s
        JOIN groups g ON g.id = s.group_id
        JOIN stages st ON st.id = g.stage_id
        JOIN governorates gov ON gov.id = g.governorate_id
        WHERE 1=1
    """
    params = []
    if group_id:
        query += " AND s.group_id = ?"
        params.append(group_id)
    if stage_id:
        query += " AND g.stage_id = ?"
        params.append(stage_id)
    if governorate_id:
        query += " AND g.governorate_id = ?"
        params.append(governorate_id)

    if session["role"] == "supervisor":
        query += " AND g.supervisor_id = ?"
        params.append(session["id"])
    elif session["role"] == "student":
        query += " AND s.id = ?"
        params.append(session["id"])

    query += " ORDER BY s.full_name"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        result = [dict(r) for r in rows]
        # الطالب ميشوفش أكواد دخول زمايله، وبردو ميشوفش حتى كوده نفسه في القايمة العامة
        if session["role"] == "student":
            for r in result:
                r.pop("access_code", None)
                r.pop("attendance_code", None)
        return result


@app.post("/api/students")
def add_student(student: StudentIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM students WHERE group_id=? AND is_active=1 AND LOWER(TRIM(full_name))=LOWER(TRIM(?))",
            (student.group_id, student.full_name)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="فيه طالب بنفس الاسم مسجل بالفعل في نفس المجموعة")

        code = gen_access_code()
        # تأكد إن الكود فريد (احتمالية تكرار شبه معدومة بس للأمان)
        while conn.execute("SELECT id FROM students WHERE access_code=?", (code,)).fetchone():
            code = gen_access_code()
        att_code = gen_numeric_code()
        while conn.execute("SELECT id FROM students WHERE attendance_code=?", (att_code,)).fetchone():
            att_code = gen_numeric_code()
        cur = conn.execute(
            """INSERT INTO students (full_name, phone, parent_phone, group_id, notes, access_code, attendance_code)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (student.full_name, student.phone, student.parent_phone,
             student.group_id, student.notes, code, att_code)
        )
        return {"id": cur.lastrowid, "access_code": code, "attendance_code": att_code, "message": "تم إضافة الطالب بنجاح"}


@app.post("/api/students/bulk")
def bulk_add_students(payload: BulkStudentsRequest, session=Depends(require_roles("admin"))):
    """استيراد مجموعة طلاب دفعة واحدة (من ملف CSV بيتقرا في الفرونت إند وبيتبعت هنا كـ JSON).
    بيرجع تقرير: مين اتضاف بنجاح (مع الأكواد بتاعته) ومين فشل ولية"""
    with get_connection() as conn:
        created, errors = [], []
        seen_in_batch = set()  # (group_id, اسم بعد التطبيع) عشان نمسك تكرار جوه نفس الملف
        for idx, s in enumerate(payload.students, start=1):
            name = (s.full_name or "").strip()
            if not name:
                errors.append({"row": idx, "name": s.full_name or "", "error": "الاسم مطلوب"})
                continue
            grp = conn.execute("SELECT id FROM groups WHERE id=?", (s.group_id,)).fetchone()
            if not grp:
                errors.append({"row": idx, "name": name, "error": "المجموعة غير موجودة"})
                continue

            dedup_key = (s.group_id, name.strip().lower())
            if dedup_key in seen_in_batch:
                errors.append({"row": idx, "name": name, "error": "اسم مكرر أكتر من مرة في نفس الملف"})
                continue
            existing = conn.execute(
                "SELECT id FROM students WHERE group_id=? AND is_active=1 AND LOWER(TRIM(full_name))=LOWER(TRIM(?))",
                (s.group_id, name)
            ).fetchone()
            if existing:
                errors.append({"row": idx, "name": name, "error": "طالب بنفس الاسم موجود بالفعل في نفس المجموعة"})
                continue

            try:
                code = gen_access_code()
                while conn.execute("SELECT id FROM students WHERE access_code=?", (code,)).fetchone():
                    code = gen_access_code()
                att_code = gen_numeric_code()
                while conn.execute("SELECT id FROM students WHERE attendance_code=?", (att_code,)).fetchone():
                    att_code = gen_numeric_code()
                cur = conn.execute(
                    """INSERT INTO students (full_name, phone, parent_phone, group_id, notes, access_code, attendance_code)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (name, (s.phone or "").strip() or None, (s.parent_phone or "").strip() or None,
                     s.group_id, (s.notes or "").strip() or None, code, att_code)
                )
                seen_in_batch.add(dedup_key)
                created.append({"id": cur.lastrowid, "full_name": name, "access_code": code, "attendance_code": att_code})
            except Exception:
                errors.append({"row": idx, "name": name, "error": "حصلت مشكلة أثناء الإضافة"})

        return {"created_count": len(created), "created": created, "errors": errors}


@app.put("/api/students/{student_id}")
def update_student(student_id: int, student: StudentIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        result = conn.execute(
            """UPDATE students SET full_name=?, phone=?, parent_phone=?, group_id=?, notes=? WHERE id=?""",
            (student.full_name, student.phone, student.parent_phone,
             student.group_id, student.notes, student_id)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        return {"message": "تم تعديل بيانات الطالب"}


@app.put("/api/students/{student_id}/reset-code")
def reset_student_code(student_id: int, session=Depends(require_roles("admin"))):
    """توليد كود دخول جديد للطالب (لو الكود ضاع منه مثلاً) - وبيلغي ربط الجهاز القديم كمان"""
    with get_connection() as conn:
        code = gen_access_code()
        while conn.execute("SELECT id FROM students WHERE access_code=?", (code,)).fetchone():
            code = gen_access_code()
        result = conn.execute("UPDATE students SET access_code=?, device_id=NULL WHERE id=?", (code, student_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        conn.execute("DELETE FROM sessions WHERE user_type='student' AND user_id=?", (student_id,))
        return {"access_code": code, "message": "تم توليد كود جديد"}


@app.put("/api/students/{student_id}/reset-device")
def reset_student_device(student_id: int, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    """فك ربط كود الطالب بالجهاز القديم - يُستخدم لو الطالب غيّر تليفونه ومش قادر يدخل"""
    with get_connection() as conn:
        student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])
        conn.execute("UPDATE students SET device_id=NULL WHERE id=?", (student_id,))
        conn.execute("DELETE FROM sessions WHERE user_type='student' AND user_id=?", (student_id,))
        return {"message": "تم إلغاء ربط الجهاز - الطالب يقدر يدخل من أي جهاز دلوقتي"}


@app.put("/api/students/{student_id}/reset-attendance-code")
def reset_student_attendance_code(student_id: int, session=Depends(require_roles("admin"))):
    """توليد كود حضور رقمي جديد للطالب (لو ضاع منه مثلاً)"""
    with get_connection() as conn:
        att_code = gen_numeric_code()
        while conn.execute("SELECT id FROM students WHERE attendance_code=?", (att_code,)).fetchone():
            att_code = gen_numeric_code()
        result = conn.execute("UPDATE students SET attendance_code=? WHERE id=?", (att_code, student_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        return {"attendance_code": att_code, "message": "تم توليد كود حضور جديد"}


@app.put("/api/students/{student_id}/toggle-active")
def toggle_student_active(student_id: int, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        student = conn.execute("SELECT is_active FROM students WHERE id=?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        new_state = 0 if student["is_active"] else 1
        conn.execute("UPDATE students SET is_active=? WHERE id=?", (new_state, student_id))
        return {"is_active": bool(new_state), "message": "تم تحديث حالة الحساب"}


@app.delete("/api/students/{student_id}")
def delete_student(student_id: int, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        result = conn.execute("DELETE FROM students WHERE id=?", (student_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        return {"message": "تم حذف الطالب"}


# ---------------------------------------------------------------------------
# المشرفين والمدرسين - Users management (الأدمن بس)
# ---------------------------------------------------------------------------

@app.get("/api/users")
def get_users(role: Optional[str] = None, session=Depends(require_roles("admin", "head_supervisor"))):
    query = """
        SELECT u.id, u.username, u.full_name, u.phone, u.role, u.access_code, u.is_active, u.created_at,
               u.governorate_id, gov.name as governorate_name
        FROM users u
        LEFT JOIN governorates gov ON gov.id = u.governorate_id
        WHERE u.role != 'admin'
    """
    params = []
    if role:
        query += " AND u.role = ?"
        params.append(role)
    query += " ORDER BY u.full_name"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d["role"] == "supervisor":
                groups = conn.execute(
                    "SELECT id, name FROM groups WHERE supervisor_id=?", (d["id"],)
                ).fetchall()
                d["groups"] = [dict(g) for g in groups]
            result.append(d)
        return result


@app.post("/api/users")
def add_user(user: UserIn, session=Depends(require_roles("admin"))):
    if user.role not in ("supervisor", "teacher", "head_supervisor"):
        raise HTTPException(status_code=400, detail="الدور المسموح بيه هنا: مشرف أو مدرس أو مشرف مشرفين بس")
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username=?", (user.username,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="اسم المستخدم ده مستخدم قبل كده")

        # لو الأدمن سايب خانة كلمة المرور فاضية، نولّد كلمة مرور تلقائية ونرجّعها
        generated_password = None
        password = user.password
        if not password:
            generated_password = gen_temp_password()
            password = generated_password

        # كود دخول سريع (بديل ليوزر وباسورد) - مفيد للمشرفين اللي مش مرتاحين للتعامل مع باسورد
        prefix = "SUP" if user.role == "supervisor" else ("HSV" if user.role == "head_supervisor" else "TCH")
        code = gen_access_code(prefix)
        while conn.execute("SELECT id FROM users WHERE access_code=?", (code,)).fetchone():
            code = gen_access_code(prefix)

        gov_id = user.governorate_id if user.role == "supervisor" else None
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, full_name, phone, access_code, governorate_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user.username, hash_password(password), user.role, user.full_name, user.phone, code, gov_id)
        )
        role_label = "المشرف" if user.role == "supervisor" else "المدرس"
        response = {"id": cur.lastrowid, "access_code": code, "message": f"تم إضافة {role_label} بنجاح"}
        if generated_password:
            response["generated_password"] = generated_password
        return response


@app.put("/api/users/{user_id}/reset-code")
def reset_user_code(user_id: int, session=Depends(require_roles("admin"))):
    """توليد كود دخول جديد للمشرف/المدرس (لو الكود ضاع منه مثلاً)"""
    with get_connection() as conn:
        user = conn.execute("SELECT role FROM users WHERE id=? AND role != 'admin'", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="المستخدم غير موجود")
        prefix = "SUP" if user["role"] == "supervisor" else "TCH"
        code = gen_access_code(prefix)
        while conn.execute("SELECT id FROM users WHERE access_code=?", (code,)).fetchone():
            code = gen_access_code(prefix)
        conn.execute("UPDATE users SET access_code=? WHERE id=?", (code, user_id))
        return {"access_code": code, "message": "تم توليد كود جديد"}


@app.put("/api/users/{user_id}")
def update_user(user_id: int, data: UserUpdateIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=? AND role != 'admin'", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="المستخدم غير موجود")

        full_name = data.full_name if data.full_name is not None else user["full_name"]
        phone = data.phone if data.phone is not None else user["phone"]
        is_active = int(data.is_active) if data.is_active is not None else user["is_active"]
        password_hash = hash_password(data.password) if data.password else user["password_hash"]
        governorate_id = data.governorate_id if data.governorate_id is not None else user["governorate_id"]

        conn.execute(
            "UPDATE users SET full_name=?, phone=?, is_active=?, password_hash=?, governorate_id=? WHERE id=?",
            (full_name, phone, is_active, password_hash, governorate_id, user_id)
        )
        return {"message": "تم تعديل البيانات"}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        user = conn.execute("SELECT id FROM users WHERE id=? AND role != 'admin'", (user_id,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="المستخدم غير موجود")
        conn.execute("UPDATE groups SET supervisor_id=NULL WHERE supervisor_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        return {"message": "تم حذف المستخدم"}


# ---------------------------------------------------------------------------
# جدول مواعيد المدرس - Teacher Schedule
# ---------------------------------------------------------------------------

@app.get("/api/schedule")
def get_schedule(session=Depends(get_current_session)):
    """كل الأدوار تقدر تشوف الجدول (متابعة)، التعديل للمدرس والأدمن بس"""
    with get_connection() as conn:
        query = """
            SELECT sc.*, g.name as group_name, st.name as stage_name
            FROM teacher_schedule sc
            LEFT JOIN groups g ON g.id = sc.group_id
            LEFT JOIN stages st ON st.id = g.stage_id
            WHERE 1=1
        """
        params = []
        if session["role"] == "supervisor":
            query += " AND (sc.group_id IS NULL OR sc.group_id IN (SELECT id FROM groups WHERE supervisor_id=?))"
            params.append(session["id"])
        elif session["role"] == "student":
            query += " AND (sc.group_id IS NULL OR sc.group_id = ?)"
            params.append(session["group_id"])

        rows = conn.execute(query, params).fetchall()
        order = {"السبت": 0, "الأحد": 1, "الإثنين": 2, "الثلاثاء": 3, "الأربعاء": 4, "الخميس": 5, "الجمعة": 6}
        items = [dict(r) for r in rows]
        items.sort(key=lambda x: (order.get(x["day_of_week"], 99), x["start_time"]))
        return items


@app.post("/api/schedule")
def add_schedule(item: ScheduleIn, session=Depends(require_roles("admin", "teacher"))):
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO teacher_schedule (day_of_week, start_time, end_time, group_id, title, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item.day_of_week, item.start_time, item.end_time, item.group_id, item.title, item.notes)
        )
        return {"id": cur.lastrowid, "message": "تم إضافة الموعد للجدول"}


@app.put("/api/schedule/{item_id}")
def update_schedule(item_id: int, item: ScheduleIn, session=Depends(require_roles("admin", "teacher"))):
    with get_connection() as conn:
        result = conn.execute(
            """UPDATE teacher_schedule SET day_of_week=?, start_time=?, end_time=?, group_id=?, title=?, notes=?
               WHERE id=?""",
            (item.day_of_week, item.start_time, item.end_time, item.group_id, item.title, item.notes, item_id)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="الموعد غير موجود")
        return {"message": "تم تعديل الموعد"}


@app.delete("/api/schedule/{item_id}")
def delete_schedule(item_id: int, session=Depends(require_roles("admin", "teacher"))):
    with get_connection() as conn:
        result = conn.execute("DELETE FROM teacher_schedule WHERE id=?", (item_id,))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="الموعد غير موجود")
        return {"message": "تم حذف الموعد"}


# ---------------------------------------------------------------------------
# سبورة الحصة - Board Images (خاصة بكل مجموعة لوحدها، المشرف بس بيرفعها)
# ---------------------------------------------------------------------------

def _save_base64_image(data_url: str, directory: str) -> str:
    """
    يفك تشفير صورة base64 (data URL) ويحفظها كملف على الـ disk،
    ويرجع المسار اللي يقدر المتصفح يستخدمه مباشرة في <img src="...">.
    """
    try:
        header, b64data = data_url.split(",", 1)
        ext = "png"
        if "image/jpeg" in header or "image/jpg" in header:
            ext = "jpg"
        elif "image/webp" in header:
            ext = "webp"
        elif "image/gif" in header:
            ext = "gif"
        raw = base64.b64decode(b64data)
    except Exception:
        raise HTTPException(status_code=400, detail="صيغة الصورة غير صحيحة")

    if len(raw) > 8 * 1024 * 1024:  # حد أقصى 8MB للصورة
        raise HTTPException(status_code=400, detail="حجم الصورة كبير جدًا (الحد الأقصى 8 ميجا)")

    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(directory, filename)
    with open(filepath, "wb") as f:
        f.write(raw)

    rel_path = os.path.relpath(filepath, UPLOADS_DIR).replace("\\", "/")
    return f"/uploads/{rel_path}"


@app.get("/api/board-images")
def get_board_images(group_id: int, session=Depends(get_current_session)):
    """جلب صور السبورة لمجموعة معينة - كل دور حسب صلاحياته على المجموعة دي"""
    with get_connection() as conn:
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, group_id)
        elif session["role"] == "student":
            if session["group_id"] != group_id:
                raise HTTPException(status_code=403, detail="تقدر تشوف سبورة مجموعتك بس")

        query = """SELECT bi.*, u.full_name as uploaded_by_name
                   FROM board_images bi
                   LEFT JOIN users u ON u.id = bi.uploaded_by
                   WHERE bi.group_id=?"""
        params = [group_id]
        # فلتر عام: إخفاء صور سبورة الحصص اللي تاريخها قبل تاريخ أول اشتراك الطالب
        cutoff = student_content_cutoff(session)
        if cutoff:
            query += " AND (bi.session_date IS NULL OR bi.session_date >= ?)"
            params.append(cutoff)
        query += " ORDER BY bi.session_number DESC, bi.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/board-images")
def add_board_image(data: BoardImageIn, session=Depends(require_roles("supervisor", "admin"))):
    """رفع صورة سبورة لحصة معينة - المشرف يرفع لمجموعته بس - بتُحفظ كملف على الـ disk"""
    with get_connection() as conn:
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, data.group_id)

        image_url = _save_base64_image(data.image_data, BOARD_IMAGES_DIR)

        cur = conn.execute(
            """INSERT INTO board_images (group_id, session_number, session_date, image_data, caption, uploaded_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (data.group_id, data.session_number, data.session_date, image_url,
             data.caption, session["id"])
        )
        group_students = conn.execute(
            "SELECT id FROM students WHERE group_id=? AND is_active=1", (data.group_id,)
        ).fetchall()
        for st in group_students:
            create_notification(
                conn, st["id"], "تم رفع صور السبورة 🖼️",
                f"صور شرح الحصة رقم {data.session_number} بقت متاحة"
            )
        return {"id": cur.lastrowid, "message": "تم رفع صورة السبورة بنجاح"}


@app.delete("/api/board-images/{image_id}")
def delete_board_image(image_id: int, session=Depends(require_roles("supervisor", "admin"))):
    with get_connection() as conn:
        img = conn.execute("SELECT * FROM board_images WHERE id=?", (image_id,)).fetchone()
        if not img:
            raise HTTPException(status_code=404, detail="الصورة غير موجودة")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, img["group_id"])
        conn.execute("DELETE FROM board_images WHERE id=?", (image_id,))

        # حذف الملف الفعلي من الـ disk لو موجود
        if img["image_data"] and img["image_data"].startswith("/uploads/"):
            file_path = os.path.join(UPLOADS_DIR, img["image_data"][len("/uploads/"):])
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                pass

        return {"message": "تم حذف الصورة"}


# ---------------------------------------------------------------------------
# فيديوهات المجموعة - المشرف بيرفع فيديو لمجموعة معينة، وكل طلاب المجموعة
# يقدروا يتفرجوا عليه (بث محمي بصلاحيات + بدون رابط تحميل مباشر)
# ---------------------------------------------------------------------------

def assert_can_access_group_media(conn, session, group_id: int):
    """يتأكد إن صاحب الجلسة مسموح له يشوف فيديوهات المجموعة دي:
    - أي طالب في المجموعة نفسها
    - المشرف المسؤول عن المجموعة
    - الأدمن / مشرف المشرفين / المدرس"""
    if session["role"] == "student":
        if session.get("group_id") != group_id:
            raise HTTPException(status_code=403, detail="مش مسموح لك تشوف فيديوهات مجموعة تانية")
        return
    if session["role"] == "supervisor":
        assert_supervisor_owns_group(conn, session, group_id)
        return
    if session["role"] in ("admin", "head_supervisor", "teacher"):
        return
    raise HTTPException(status_code=403, detail="مفيش صلاحية للوصول لده")


@app.get("/api/groups/{group_id}/videos")
def list_group_videos(group_id: int, session=Depends(get_current_session)):
    with get_connection() as conn:
        assert_can_access_group_media(conn, session, group_id)
        query = """
            SELECT gv.id, gv.group_id, gv.title, gv.description, gv.file_size, gv.mime_type,
                   gv.created_at, u.full_name as uploaded_by_name
            FROM group_videos gv
            LEFT JOIN users u ON u.id = gv.uploaded_by
            WHERE gv.group_id = ?
        """
        params = [group_id]
        # فلتر عام: إخفاء الفيديوهات اللي اترفعت قبل تاريخ أول اشتراك الطالب
        cutoff = student_content_cutoff(session)
        if cutoff:
            query += " AND gv.created_at >= ?"
            params.append(cutoff)
        query += " ORDER BY gv.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# المحتوى مقسّم بالشهور والحصص (سبورة + فيديوهات + واجبات + كويزات + حضور)
# - الطالب بيشوف بس الشهور اللي دفعها فعليًا (لو فيه فجوة سداد، الشهر ده
#   بيتخفي حتى لو فيه شهور مدفوعة بعده)
# - الأدمن/المشرف/المدرس بيشوفوا كل الشهور من غير فلترة، إلا لو حددوا
#   student_id عشان يعاينوا اللي هيبان فعلاً لطالب معين
# ---------------------------------------------------------------------------

ARABIC_MONTHS = {
    "01": "يناير", "02": "فبراير", "03": "مارس", "04": "أبريل",
    "05": "مايو", "06": "يونيو", "07": "يوليو", "08": "أغسطس",
    "09": "سبتمبر", "10": "أكتوبر", "11": "نوفمبر", "12": "ديسمبر",
}

NO_DATE_KEY = "بدون_تاريخ"


def _month_label(month_key: str) -> str:
    """بيحول 'YYYY-MM' لاسم شهر عربي، مثال: '2026-10' -> 'أكتوبر 2026'"""
    try:
        year, mo = month_key.split("-")
        return f"{ARABIC_MONTHS.get(mo, mo)} {year}"
    except Exception:
        return month_key


@app.get("/api/groups/{group_id}/content-by-month")
def get_group_content_by_month(
    group_id: int,
    student_id: Optional[int] = None,
    session=Depends(get_current_session),
):
    """
    بيرجع محتوى المجموعة كله مقسّم بالشهر ثم بالحصة جوه كل شهر (شكل أكورديون
    جاهز للواجهة): { months: [ { month, label, sessions: [...], videos: [...] } ] }
    """
    with get_connection() as conn:
        # تحديد صلاحية الوصول + تحديد الطالب اللي هنفلتر شهوره حسب سداده
        if session["role"] == "student":
            if session.get("group_id") != group_id:
                raise HTTPException(status_code=403, detail="مش مسموح لك تشوف محتوى مجموعة تانية")
            effective_student_id = session["id"]
            paid_months = set(get_paid_months(conn, effective_student_id))
        elif session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, group_id)
            effective_student_id = student_id
            paid_months = set(get_paid_months(conn, effective_student_id)) if effective_student_id else None
        elif session["role"] in ("admin", "head_supervisor", "teacher"):
            effective_student_id = student_id
            paid_months = set(get_paid_months(conn, effective_student_id)) if effective_student_id else None
        else:
            raise HTTPException(status_code=403, detail="مفيش صلاحية للوصول لده")

        group = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
        stage_id = group["stage_id"]

        months: dict = {}

        def bucket(month_key: Optional[str]):
            key = month_key or NO_DATE_KEY
            if key not in months:
                months[key] = {
                    "month": month_key,
                    "label": _month_label(month_key) if month_key else "عناصر بدون تاريخ محدد",
                    "sessions": {},
                    "videos": [],
                    "quizzes_no_session": [],
                }
            return months[key]

        def session_bucket(month_dict, session_number: int):
            key = str(session_number)
            if key not in month_dict["sessions"]:
                month_dict["sessions"][key] = {
                    "session_number": session_number,
                    "board_images": [],
                    "homework": None,
                    "quizzes": [],
                    "attendance": None,
                }
            return month_dict["sessions"][key]

        # ---------------- سبورة الحصص ----------------
        rows = conn.execute(
            """SELECT bi.*, u.full_name as uploaded_by_name
               FROM board_images bi LEFT JOIN users u ON u.id = bi.uploaded_by
               WHERE bi.group_id=? ORDER BY bi.session_number, bi.created_at""",
            (group_id,)
        ).fetchall()
        for r in rows:
            m = bucket(r["session_date"][:7] if r["session_date"] else None)
            s = session_bucket(m, r["session_number"])
            s["board_images"].append(dict(r))

        # ---------------- الفيديوهات (مالهاش رقم حصة، بتتبوّب بالشهر بس) ----------------
        rows = conn.execute(
            """SELECT gv.id, gv.group_id, gv.title, gv.description, gv.file_size,
                      gv.mime_type, gv.created_at, u.full_name as uploaded_by_name
               FROM group_videos gv LEFT JOIN users u ON u.id = gv.uploaded_by
               WHERE gv.group_id=? ORDER BY gv.created_at""",
            (group_id,)
        ).fetchall()
        for r in rows:
            m = bucket(r["created_at"][:7] if r["created_at"] else None)
            m["videos"].append(dict(r))

        # ---------------- الواجبات ----------------
        rows = conn.execute(
            "SELECT * FROM homework WHERE group_id=? ORDER BY session_number", (group_id,)
        ).fetchall()
        submissions_by_hw = {}
        if effective_student_id:
            sub_rows = conn.execute(
                "SELECT * FROM homework_submissions WHERE student_id=?", (effective_student_id,)
            ).fetchall()
            submissions_by_hw = {r["homework_id"]: dict(r) for r in sub_rows}
        for r in rows:
            m = bucket(r["session_date"][:7] if r["session_date"] else None)
            s = session_bucket(m, r["session_number"])
            hw = dict(r)
            if effective_student_id:
                hw["submission"] = submissions_by_hw.get(r["id"])
            s["homework"] = hw

        # ---------------- الكويزات (خاصة بالمجموعة أو المرحلة أو عامة) ----------------
        rows = conn.execute(
            """SELECT q.* FROM quizzes q
               WHERE (q.group_id IS NULL AND q.stage_id IS NULL)
                  OR q.group_id = ? OR q.stage_id = ?
               ORDER BY q.session_number, q.quiz_date""",
            (group_id, stage_id)
        ).fetchall()
        scores_by_quiz = {}
        if effective_student_id:
            sc_rows = conn.execute(
                "SELECT * FROM quiz_scores WHERE student_id=?", (effective_student_id,)
            ).fetchall()
            scores_by_quiz = {r["quiz_id"]: dict(r) for r in sc_rows}
        for r in rows:
            m = bucket(r["quiz_date"][:7] if r["quiz_date"] else None)
            q = dict(r)
            if effective_student_id:
                q["my_score"] = scores_by_quiz.get(r["id"])
            if r["session_number"] is not None:
                s = session_bucket(m, r["session_number"])
                s["quizzes"].append(q)
            else:
                m["quizzes_no_session"].append(q)

        # ---------------- الحضور (لو محدد طالب) ----------------
        if effective_student_id:
            rows = conn.execute(
                "SELECT * FROM attendance WHERE student_id=? ORDER BY session_date, session_number",
                (effective_student_id,)
            ).fetchall()
            for r in rows:
                m = bucket(r["session_date"][:7] if r["session_date"] else None)
                s = session_bucket(m, r["session_number"])
                s["attendance"] = dict(r)

        # ---------------- فلترة الشهور: يظهر بس الشهر اللي اتسدد فعليًا ----------------
        # (لو فيه فجوة سداد، الشهر ده بيتخفي حتى لو فيه شهور مدفوعة بعده)
        # عناصر "بدون تاريخ" بتفضل ظاهرة دايمًا لأننا مش نقدر نحكم عليها بشهر معين
        if paid_months is not None:
            filtered = {k: v for k, v in months.items() if k in paid_months}
            if NO_DATE_KEY in months:
                filtered[NO_DATE_KEY] = months[NO_DATE_KEY]
            months = filtered

        dated_keys = sorted(k for k in months.keys() if k != NO_DATE_KEY)
        ordered_keys = dated_keys + ([NO_DATE_KEY] if NO_DATE_KEY in months else [])

        result_months = []
        for k in ordered_keys:
            md = months[k]
            md["sessions"] = [
                md["sessions"][sk] for sk in sorted(md["sessions"].keys(), key=lambda x: int(x))
            ]
            result_months.append(md)

        return {
            "group_id": group_id,
            "group_name": group["name"],
            "paid_months": sorted(paid_months) if paid_months is not None else None,
            "months": result_months,
        }


@app.post("/api/groups/{group_id}/videos")
async def upload_group_video(
    group_id: int,
    title: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    session=Depends(require_roles("admin", "head_supervisor", "supervisor", "teacher")),
):
    with get_connection() as conn:
        assert_can_access_group_media(conn, session, group_id)

        ext = os.path.splitext(file.filename or "")[1].lower()
        if ext not in ALLOWED_VIDEO_EXTENSIONS:
            raise HTTPException(status_code=400, detail="صيغة الفيديو غير مدعومة")

        stored_name = f"{uuid.uuid4().hex}{ext}"
        dest_path = os.path.join(VIDEOS_DIR, stored_name)

        size = 0
        try:
            with open(dest_path, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_VIDEO_SIZE_BYTES:
                        out.close()
                        os.remove(dest_path)
                        raise HTTPException(status_code=413, detail="حجم الفيديو أكبر من الحد المسموح (500 ميجا)")
                    out.write(chunk)
        except HTTPException:
            raise
        except Exception:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise HTTPException(status_code=500, detail="حصلت مشكلة أثناء رفع الفيديو")

        cur = conn.execute("""
            INSERT INTO group_videos (group_id, title, description, file_path, file_size, mime_type, uploaded_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (group_id, title.strip(), (description or "").strip() or None, stored_name, size,
              file.content_type or "video/mp4", session["id"]))

        # إشعار كل طلاب المجموعة إن فيه فيديو جديد
        student_ids = [r["id"] for r in conn.execute("SELECT id FROM students WHERE group_id=? AND is_active=1", (group_id,)).fetchall()]
        for sid in student_ids:
            create_notification(conn, sid, "فيديو جديد", f"المشرف رفع فيديو جديد: {title.strip()}")

        return {"id": cur.lastrowid, "message": "تم رفع الفيديو بنجاح"}


@app.delete("/api/videos/{video_id}")
def delete_group_video(video_id: int, session=Depends(require_roles("admin", "head_supervisor", "supervisor", "teacher"))):
    with get_connection() as conn:
        vid = conn.execute("SELECT * FROM group_videos WHERE id=?", (video_id,)).fetchone()
        if not vid:
            raise HTTPException(status_code=404, detail="الفيديو غير موجود")
        assert_can_access_group_media(conn, session, vid["group_id"])
        conn.execute("DELETE FROM group_videos WHERE id=?", (video_id,))
        file_path = os.path.join(VIDEOS_DIR, vid["file_path"])
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        return {"message": "تم حذف الفيديو"}


@app.get("/api/videos/{video_id}/stream")
def stream_group_video(video_id: int, request: Request, session=Depends(get_session_for_media)):
    """بث الفيديو مع دعم Range requests (ضروري عشان المستخدم يقدر يتقدم/يرجع في فيديو طويل).
    الفيديو مش متاح كملف عام قابل للتنزيل - بيتبث بس من خلال الـ endpoint ده بعد التحقق من الصلاحية،
    وبـ Content-Disposition: inline (مش attachment) عشان يتشغل جوه المشغل مباشرة."""
    with get_connection() as conn:
        vid = conn.execute("SELECT * FROM group_videos WHERE id=?", (video_id,)).fetchone()
        if not vid:
            raise HTTPException(status_code=404, detail="الفيديو غير موجود")
        assert_can_access_group_media(conn, session, vid["group_id"])

        # فلتر عام: منع الطالب من بث فيديو اترفع قبل تاريخ أول اشتراك ليه، حتى لو
        # حاول يوصله مباشرة بالرابط (مش بس إخفاءه من القايمة)
        cutoff = student_content_cutoff(session)
        if cutoff and vid["created_at"] and vid["created_at"] < cutoff:
            raise HTTPException(status_code=404, detail="الفيديو غير موجود")

        file_path = os.path.join(VIDEOS_DIR, vid["file_path"])
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="ملف الفيديو مفقود")

        file_size = os.path.getsize(file_path)
        mime_type = vid["mime_type"] or "video/mp4"
        range_header = request.headers.get("range")

        base_headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": "inline",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }

        if range_header:
            try:
                range_val = range_header.strip().split("=")[1]
                start_str, end_str = range_val.split("-")
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
                end = min(end, file_size - 1)
            except Exception:
                start, end = 0, file_size - 1

            chunk_size = end - start + 1

            def iter_range():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = chunk_size
                    while remaining > 0:
                        data = f.read(min(1024 * 1024, remaining))
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            headers = {
                **base_headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(chunk_size),
            }
            return StreamingResponse(iter_range(), status_code=206, media_type=mime_type, headers=headers)

        def iter_full():
            with open(file_path, "rb") as f:
                while True:
                    data = f.read(1024 * 1024)
                    if not data:
                        break
                    yield data

        headers = {**base_headers, "Content-Length": str(file_size)}
        return StreamingResponse(iter_full(), media_type=mime_type, headers=headers)


# ---------------------------------------------------------------------------
# الكويزات - Quizzes
# ---------------------------------------------------------------------------

@app.get("/api/quizzes")
def get_quizzes(group_id: Optional[int] = None, stage_id: Optional[int] = None,
                 session=Depends(get_current_session)):
    query = """
        SELECT q.*, g.name as group_name, st.name as stage_name,
               u.full_name as created_by_name
        FROM quizzes q
        LEFT JOIN groups g ON g.id = q.group_id
        LEFT JOIN stages st ON st.id = q.stage_id
        LEFT JOIN users u ON u.id = q.created_by
        WHERE 1=1
    """
    params = []
    if group_id:
        query += " AND (q.group_id = ? OR q.group_id IS NULL)"
        params.append(group_id)
    if stage_id:
        query += " AND (q.stage_id = ? OR q.stage_id IS NULL)"
        params.append(stage_id)

    with get_connection() as conn:
        if session["role"] == "supervisor":
            my_stage_ids = [r["stage_id"] for r in conn.execute(
                "SELECT DISTINCT stage_id FROM groups WHERE supervisor_id=?", (session["id"],)
            ).fetchall()]
            my_stage_ids = my_stage_ids or [-1]
            placeholders = ",".join("?" * len(my_stage_ids))
            query += f""" AND (
                (q.group_id IS NULL AND q.stage_id IS NULL)
                OR q.group_id IN (SELECT id FROM groups WHERE supervisor_id=?)
                OR q.stage_id IN ({placeholders})
            )"""
            params.append(session["id"])
            params.extend(my_stage_ids)
        elif session["role"] == "student":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (session["id"],)).fetchone()
            student_stage = None
            if student:
                grp = conn.execute("SELECT stage_id FROM groups WHERE id=?", (student["group_id"],)).fetchone()
                student_stage = grp["stage_id"] if grp else None
            query += """ AND (
                (q.group_id IS NULL AND q.stage_id IS NULL)
                OR q.group_id = ?
                OR q.stage_id = ?
            )"""
            params.append(session.get("group_id"))
            params.append(student_stage)

        # فلتر عام: إخفاء أي كويز تاريخه قبل تاريخ أول اشتراك الطالب
        cutoff = student_content_cutoff(session)
        if cutoff:
            query += " AND (q.quiz_date IS NULL OR q.quiz_date >= ?)"
            params.append(cutoff)

        query += " ORDER BY q.quiz_date DESC, q.id DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/quizzes")
def add_quiz(quiz: QuizIn, session=Depends(require_roles("admin", "head_supervisor"))):
    """إنشاء كويز/امتحان شامل جديد - مشرف المشرفين أو الأدمن بس، وبيوصل تلقائي لكل مشرفي المرحلة"""
    if not quiz.stage_id and not quiz.group_id:
        raise HTTPException(status_code=400, detail="لازم تحدد المرحلة الدراسية للكويز")
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO quizzes (title, description, quiz_date, max_score, group_id,
                                  stage_id, session_number, image_data, version_label, created_by, quiz_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (quiz.title, quiz.description, quiz.quiz_date, quiz.max_score, quiz.group_id,
              quiz.stage_id, quiz.session_number, quiz.image_data, quiz.version_label, session["id"], quiz.quiz_type))
        return {"id": cur.lastrowid, "message": "تم إضافة الكويز بنجاح، ووصل لكل مشرفي المرحلة"}


@app.put("/api/quizzes/{quiz_id}")
def update_quiz(quiz_id: int, quiz: QuizIn, session=Depends(require_roles("admin", "head_supervisor"))):
    with get_connection() as conn:
        existing = conn.execute("SELECT id FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="الكويز غير موجود")
        conn.execute("""
            UPDATE quizzes SET title=?, description=?, quiz_date=?, max_score=?, group_id=?,
                                stage_id=?, session_number=?, image_data=COALESCE(?, image_data),
                                version_label=?, quiz_type=?
            WHERE id=?
        """, (quiz.title, quiz.description, quiz.quiz_date, quiz.max_score, quiz.group_id,
              quiz.stage_id, quiz.session_number, quiz.image_data, quiz.version_label, quiz.quiz_type, quiz_id))
        return {"message": "تم تعديل الكويز"}


@app.delete("/api/quizzes/{quiz_id}")
def delete_quiz(quiz_id: int, session=Depends(require_roles("admin", "head_supervisor"))):
    with get_connection() as conn:
        quiz = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if not quiz:
            raise HTTPException(status_code=404, detail="الكويز غير موجود")
        conn.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
        return {"message": "تم حذف الكويز"}


# ---------------------------------------------------------------------------
# درجات الكويزات - Quiz Scores
# ---------------------------------------------------------------------------

@app.get("/api/quizzes/{quiz_id}/scores")
def get_quiz_scores(quiz_id: int, session=Depends(get_current_session)):
    """
    جلب طلاب الكويز مع درجاتهم.
    - كويز مرحلة (stage_id): المشرف يشوف طلاب مجموعته بس، الأدمن/مشرف المشرفين يشوفوا كل طلاب المرحلة.
    - كويز مجموعة قديم (group_id): يظهر طلاب المجموعة بس.
    - كويز عام (مفيهوش لا مجموعة ولا مرحلة): كل الطلاب (أو حسب دور المستخدم).
    """
    with get_connection() as conn:
        quiz = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if not quiz:
            raise HTTPException(status_code=404, detail="الكويز غير موجود")

        # فلتر عام: الطالب ميقدرش يشوف كويز تاريخه قبل تاريخ أول اشتراك ليه
        cutoff = student_content_cutoff(session)
        if cutoff and quiz["quiz_date"] and quiz["quiz_date"] < cutoff:
            raise HTTPException(status_code=404, detail="الكويز غير موجود")

        if session["role"] == "supervisor" and quiz["group_id"]:
            assert_supervisor_owns_group(conn, session, quiz["group_id"])

        if quiz["group_id"]:
            rows = conn.execute("""
                SELECT s.id as student_id, s.full_name, s.attendance_code, qs.score, qs.notes, qs.id as score_id
                FROM students s
                LEFT JOIN quiz_scores qs ON qs.student_id = s.id AND qs.quiz_id = ?
                WHERE s.group_id = ? AND s.is_active=1
                ORDER BY s.full_name
            """, (quiz_id, quiz["group_id"])).fetchall()
        elif quiz["stage_id"]:
            base_query = """
                SELECT s.id as student_id, s.full_name, s.attendance_code, qs.score, qs.notes, qs.id as score_id,
                       s.group_id, g.name as group_name
                FROM students s
                JOIN groups g ON g.id = s.group_id
                LEFT JOIN quiz_scores qs ON qs.student_id = s.id AND qs.quiz_id = ?
                WHERE g.stage_id = ? AND s.is_active=1
            """
            params = [quiz_id, quiz["stage_id"]]
            if session["role"] == "supervisor":
                base_query += " AND s.group_id IN (SELECT id FROM groups WHERE supervisor_id=?)"
                params.append(session["id"])
            elif session["role"] == "student":
                base_query += " AND s.id = ?"
                params.append(session["id"])
            base_query += " ORDER BY g.name, s.full_name"
            rows = conn.execute(base_query, params).fetchall()
        else:
            base_query = """
                SELECT s.id as student_id, s.full_name, s.attendance_code, qs.score, qs.notes, qs.id as score_id,
                       s.group_id, g.name as group_name
                FROM students s
                JOIN groups g ON g.id = s.group_id
                LEFT JOIN quiz_scores qs ON qs.student_id = s.id AND qs.quiz_id = ?
                WHERE s.is_active=1
            """
            params = [quiz_id]
            if session["role"] == "supervisor":
                base_query += " AND s.group_id IN (SELECT id FROM groups WHERE supervisor_id=?)"
                params.append(session["id"])
            elif session["role"] == "student":
                base_query += " AND s.id = ?"
                params.append(session["id"])
            base_query += " ORDER BY s.full_name"
            rows = conn.execute(base_query, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/scores")
def set_score(score: QuizScoreIn, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        quiz = conn.execute("SELECT * FROM quizzes WHERE id=?", (score.quiz_id,)).fetchone()
        if not quiz:
            raise HTTPException(status_code=404, detail="الكويز غير موجود")
        student = conn.execute("SELECT group_id FROM students WHERE id=?", (score.student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")

        if session["role"] == "supervisor":
            # المشرف يسجل درجات طلاب مجموعته بس
            assert_supervisor_owns_group(conn, session, student["group_id"])
            if quiz["group_id"] and quiz["group_id"] != student["group_id"]:
                raise HTTPException(status_code=403, detail="الكويز ده مش لمجموعتك")
            if quiz["stage_id"]:
                grp = conn.execute("SELECT stage_id FROM groups WHERE id=?", (student["group_id"],)).fetchone()
                if not grp or grp["stage_id"] != quiz["stage_id"]:
                    raise HTTPException(status_code=403, detail="الكويز ده مش لمرحلة مجموعتك")

        is_new = conn.execute(
            "SELECT id FROM quiz_scores WHERE student_id=? AND quiz_id=?", (score.student_id, score.quiz_id)
        ).fetchone() is None

        conn.execute("""
            INSERT INTO quiz_scores (student_id, quiz_id, score, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, quiz_id)
            DO UPDATE SET score=excluded.score, notes=excluded.notes
        """, (score.student_id, score.quiz_id, score.score, score.notes))

        create_notification(
            conn, score.student_id,
            "تمت إضافة درجة جديدة ✅" if is_new else "تم تعديل درجتك 📝",
            f"{quiz['title']}: {score.score} / {quiz['max_score']}"
        )
        return {"message": "تم حفظ الدرجة"}


@app.get("/api/students/{student_id}/scores")
def get_student_scores(student_id: int, session=Depends(get_current_session)):
    with get_connection() as conn:
        if session["role"] == "student" and session["id"] != student_id:
            raise HTTPException(status_code=403, detail="تقدر تشوف درجاتك بس")
        if session["role"] == "supervisor":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (student_id,)).fetchone()
            if student:
                assert_supervisor_owns_group(conn, session, student["group_id"])

        query = """
            SELECT q.title, q.quiz_date, q.max_score, qs.score
            FROM quiz_scores qs
            JOIN quizzes q ON q.id = qs.quiz_id
            WHERE qs.student_id = ?
        """
        params = [student_id]
        # فلتر عام: إخفاء أي درجة كويز تاريخه قبل تاريخ أول اشتراك الطالب
        cutoff = student_content_cutoff(session)
        if cutoff:
            query += " AND (q.quiz_date IS NULL OR q.quiz_date >= ?)"
            params.append(cutoff)
        query += " ORDER BY q.quiz_date DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# الحضور والغياب - Attendance
# ---------------------------------------------------------------------------

@app.get("/api/attendance/{session_date}")
def get_attendance_by_date(session_date: str, group_id: Optional[int] = None,
                            session_number: int = 1,
                            session=Depends(get_current_session)):
    """كل الطلاب (أو طلاب مجموعة معينة) مع حالة حضورهم في تاريخ وحصة معينة"""
    with get_connection() as conn:
        if session["role"] == "supervisor" and group_id:
            assert_supervisor_owns_group(conn, session, group_id)

        # فلتر عام: الطالب ميقدرش يشوف حضور تاريخه قبل تاريخ أول اشتراك ليه
        cutoff = student_content_cutoff(session)
        if cutoff and session_date < cutoff:
            return []

        query = """
            SELECT s.id as student_id, s.full_name, s.attendance_code, s.phone, s.parent_phone,
                   a.status, a.notes, a.id as attendance_id, a.session_number, s.group_id,
                   COALESCE(p.is_paid, 0) as subscription_paid
            FROM students s
            LEFT JOIN attendance a ON a.student_id = s.id
                AND a.session_date = ?
                AND a.session_number = ?
            LEFT JOIN payments p ON p.student_id = s.id AND p.month = ?
            WHERE 1=1
        """
        params = [session_date, session_number, current_month_str()]
        if group_id:
            query += " AND s.group_id = ?"
            params.append(group_id)

        if session["role"] == "supervisor":
            query += " AND s.group_id IN (SELECT id FROM groups WHERE supervisor_id=?)"
            params.append(session["id"])
        elif session["role"] == "student":
            query += " AND s.id = ?"
            params.append(session["id"])

        query += " ORDER BY s.full_name"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/attendance")
def set_attendance(att: AttendanceIn, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        if session["role"] == "supervisor":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (att.student_id,)).fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="الطالب غير موجود")
            assert_supervisor_owns_group(conn, session, student["group_id"])

        conn.execute("""
            INSERT INTO attendance (student_id, session_date, session_number, status, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(student_id, session_date, session_number)
            DO UPDATE SET status=excluded.status, notes=COALESCE(excluded.notes, attendance.notes)
        """, (att.student_id, att.session_date, att.session_number, att.status, att.notes))
        return {"message": "تم حفظ الحضور"}


@app.get("/api/students/find-by-code")
def find_student_by_code(code: str, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    """
    البحث عن طالب بكود الدخول الخاص بيه - يستخدم في تسجيل الحضور السريع، وبيرجع
    بروفايل كامل (الاسم - الأرقام - المجموعة - حالة الاشتراك - تواريخ السداد)
    عشان يظهر للمشرف/الأدمن وهو بياخد الحضور
    """
    with get_connection() as conn:
        student = conn.execute(
            """SELECT s.id, s.full_name, s.phone, s.parent_phone, s.group_id,
                      s.attendance_code, s.access_code,
                      g.name as group_name, st.name as stage_name, gov.name as governorate_name
               FROM students s
               JOIN groups g ON g.id = s.group_id
               JOIN stages st ON st.id = g.stage_id
               JOIN governorates gov ON gov.id = g.governorate_id
               WHERE s.attendance_code = ? OR s.access_code = ?""",
            (code.strip(), code.strip())
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="مفيش طالب بالكود ده")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])

        sd = dict(student)
        sd["subscription_active"] = is_student_subscribed(conn, student["id"])
        payments_rows = conn.execute(
            "SELECT month, is_paid, paid_date, amount FROM payments WHERE student_id=? ORDER BY month DESC",
            (student["id"],)
        ).fetchall()
        sd["payments"] = [dict(p) for p in payments_rows]
        return sd


@app.post("/api/attendance/by-code")
def set_attendance_by_code(data: AttendanceCodeIn, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    """تسجيل حضور سريع بكود الطالب - المشرف بيدوّر بالكود ويسجل الحالة على طول"""
    with get_connection() as conn:
        student = conn.execute(
            "SELECT id, full_name, group_id FROM students WHERE attendance_code=? OR access_code=?",
            (data.access_code.strip(), data.access_code.strip())
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="مفيش طالب بالكود ده")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])

        conn.execute("""
            INSERT INTO attendance (student_id, session_date, session_number, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, session_date, session_number)
            DO UPDATE SET status=excluded.status
        """, (student["id"], data.session_date, data.session_number, data.status))
        subscription_paid = is_student_subscribed(conn, student["id"])
        return {
            "message": "تم تسجيل الحضور", "student_name": student["full_name"], "student_id": student["id"],
            "subscription_paid": subscription_paid
        }


@app.get("/api/students/{student_id}/attendance")
def get_student_attendance(student_id: int, session=Depends(get_current_session)):
    with get_connection() as conn:
        if session["role"] == "student" and session["id"] != student_id:
            raise HTTPException(status_code=403, detail="تقدر تشوف حضورك بس")
        if session["role"] == "supervisor":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (student_id,)).fetchone()
            if student:
                assert_supervisor_owns_group(conn, session, student["group_id"])

        query = "SELECT session_date, status, notes FROM attendance WHERE student_id = ?"
        params = [student_id]
        # فلتر عام: إخفاء سجلات الحضور اللي تاريخها قبل تاريخ أول اشتراك الطالب
        cutoff = student_content_cutoff(session)
        if cutoff:
            query += " AND session_date >= ?"
            params.append(cutoff)
        query += " ORDER BY session_date DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# الملاحظات السلوكية - Behavior Notes (المشرف بيكتبها، تظهر للمدرس والأدمن بس)
# ---------------------------------------------------------------------------

@app.get("/api/students/{student_id}/behavior-notes")
def get_behavior_notes(student_id: int, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    """ملحوظة: الطالب ميقدرش يشوف ملاحظاته السلوكية - دي بين المشرف والمدرس والأدمن بس"""
    with get_connection() as conn:
        student = conn.execute("SELECT group_id FROM students WHERE id=?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])

        rows = conn.execute("""
            SELECT bn.*, u.full_name as author_name
            FROM behavior_notes bn
            LEFT JOIN users u ON u.id = bn.author_id
            WHERE bn.student_id = ?
            ORDER BY bn.created_at DESC
        """, (student_id,)).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/behavior-notes")
def add_behavior_note(data: BehaviorNoteIn, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    if data.note_type not in ("positive", "negative", "neutral"):
        raise HTTPException(status_code=400, detail="نوع الملاحظة غير صحيح")
    with get_connection() as conn:
        student = conn.execute("SELECT group_id FROM students WHERE id=?", (data.student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])

        cur = conn.execute(
            "INSERT INTO behavior_notes (student_id, author_id, note_type, note) VALUES (?, ?, ?, ?)",
            (data.student_id, session["id"], data.note_type, data.note)
        )
        return {"id": cur.lastrowid, "message": "تم إضافة الملاحظة بنجاح"}


@app.delete("/api/behavior-notes/{note_id}")
def delete_behavior_note(note_id: int, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        note = conn.execute("SELECT * FROM behavior_notes WHERE id=?", (note_id,)).fetchone()
        if not note:
            raise HTTPException(status_code=404, detail="الملاحظة غير موجودة")
        if session["role"] == "supervisor":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (note["student_id"],)).fetchone()
            if student:
                assert_supervisor_owns_group(conn, session, student["group_id"])
        conn.execute("DELETE FROM behavior_notes WHERE id=?", (note_id,))
        return {"message": "تم حذف الملاحظة"}


# ---------------------------------------------------------------------------
# المدفوعات - Payments (سجل شهري لكل طالب)
# ---------------------------------------------------------------------------

@app.get("/api/students/{student_id}/payments")
def get_student_payments(student_id: int, session=Depends(get_current_session)):
    with get_connection() as conn:
        student = conn.execute("SELECT group_id FROM students WHERE id=?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])
        elif session["role"] == "student" and session["id"] != student_id:
            raise HTTPException(status_code=403, detail="تقدر تشوف مدفوعاتك بس")

        query = "SELECT * FROM payments WHERE student_id=?"
        params = [student_id]
        # فلتر عام: إخفاء أي سجل دفع لشهر قبل شهر أول اشتراك الطالب
        cutoff = student_content_cutoff(session)
        if cutoff:
            query += " AND month >= ?"
            params.append(cutoff[:7])  # مقارنة بصيغة الشهر YYYY-MM
        query += " ORDER BY month DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def _build_ranking_message(rank_score, total_score, rank_att, total_att, trend):
    """
    بيبني رسالة تحفيزية واحدة بسيطة وواضحة حسب حالة الطالب - دايمًا إيجابية
    ومحفزة حتى لو الترتيب متأخر، ومن غير ما نكدّس أكتر من جملة فوق بعض.
    """
    if rank_score and total_score and total_score >= 1:
        pct = rank_score / total_score  # كل ما قل كل ما كان الترتيب أحسن
    else:
        pct = None

    good_attendance = bool(rank_att and total_att and (rank_att / total_att) <= 0.3)

    # عنده درجات كويزات كفاية نحسب بيها ترتيب
    if pct is not None:
        if rank_score == 1:
            return "🏆 أنت الأول في مجموعتك!", "أداء رائع، استمر بنفس المستوى وحافظ على مكانك في القمة."
        if rank_score <= 3:
            return "🥈 من ضمن أفضل 3 في مجموعتك!", "أنت قريب جدًا من القمة، شوية تركيز كمان وهتوصل للمركز الأول."
        if trend == "up":
            return "📈 في تحسن ملحوظ!", "لاحظنا إن أداءك بيتحسن عن الفترة اللي فاتت، استمر بنفس الحماس ده."
        if pct <= 0.5:
            return "👍 أداء كويس ومتقدم", "أنت في النص الأفضل من مجموعتك، استمر في المذاكرة بانتظام وهتتقدم أكتر."
        return "🌱 كل رحلة بتبدأ بخطوة", "ركّز على مراجعة الأجزاء اللي بتواجه فيها صعوبة وحاول تحل كويزات أكتر، كل مذاكرة بتفرق."

    # لسه معندهوش درجات كويزات كفاية - نرجّع رسالة بسيطة واحدة بس
    if good_attendance:
        return "🎯 حضورك ممتاز من الأول!", "استمر على الالتزام ده، وأول ما تاخد أول كويز هتقدر تشوف ترتيبك في الدرجات كمان."
    return "👋 لسه بدايتك", "ذاكر كويس واحضر بانتظام، وهتقدر تشوف ترتيبك أول ما تاخد أول كويز."


@app.get("/api/students/{student_id}/ranking")
def get_student_ranking(student_id: int, session=Depends(get_current_session)):
    """
    ترتيب الطالب داخل مجموعته (بالدرجات والحضور) + رسالة تحفيزية.
    بيرجع ترتيب الطالب بس (مش قائمة كل الطلاب) عشان محدش يتحرج من ترتيبه قدام زمايله،
    وبيرجع أفضل 3 طلاب بالاسم كـ"لوحة شرف" تحفيزية.
    """
    with get_connection() as conn:
        student = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])
        elif session["role"] == "student" and session["id"] != student_id:
            raise HTTPException(status_code=403, detail="تقدر تشوف ترتيبك بس")

        group_id = student["group_id"]

        # متوسط درجات كل طالب نشط في المجموعة (لازم يكون عنده كويز واحد على الأقل بدرجة)
        score_rows = conn.execute("""
            SELECT s.id, s.full_name, AVG(qs.score * 100.0 / q.max_score) as avg_score_percent
            FROM students s
            JOIN quiz_scores qs ON qs.student_id = s.id
            JOIN quizzes q ON q.id = qs.quiz_id AND q.max_score > 0
            WHERE s.group_id = ? AND s.is_active = 1
            GROUP BY s.id
            ORDER BY avg_score_percent DESC
        """, (group_id,)).fetchall()
        score_rows = [dict(r) for r in score_rows]

        # نسبة حضور كل طالب نشط في المجموعة
        att_rows = conn.execute("""
            SELECT s.id,
                   SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as attendance_rate
            FROM students s
            JOIN attendance a ON a.student_id = s.id
            WHERE s.group_id = ? AND s.is_active = 1
            GROUP BY s.id
            ORDER BY attendance_rate DESC
        """, (group_id,)).fetchall()
        att_rows = [dict(r) for r in att_rows]

        total_score = len(score_rows)
        total_att = len(att_rows)
        rank_score = next((i + 1 for i, r in enumerate(score_rows) if r["id"] == student_id), None)
        rank_att = next((i + 1 for i, r in enumerate(att_rows) if r["id"] == student_id), None)

        my_avg_score = next((r["avg_score_percent"] for r in score_rows if r["id"] == student_id), None)
        my_attendance_rate = next((r["attendance_rate"] for r in att_rows if r["id"] == student_id), None)

        # اتجاه التحسن: مقارنة متوسط آخر نص كويزات بأول نص كويزات الطالب أخذها
        my_scores = conn.execute("""
            SELECT (qs.score * 100.0 / q.max_score) as pct
            FROM quiz_scores qs JOIN quizzes q ON q.id = qs.quiz_id AND q.max_score > 0
            WHERE qs.student_id = ?
            ORDER BY q.quiz_date, q.id
        """, (student_id,)).fetchall()
        trend = None
        if len(my_scores) >= 4:
            vals = [r["pct"] for r in my_scores]
            mid = len(vals) // 2
            first_half_avg = sum(vals[:mid]) / mid
            second_half_avg = sum(vals[mid:]) / (len(vals) - mid)
            if second_half_avg - first_half_avg >= 3:
                trend = "up"
            elif first_half_avg - second_half_avg >= 3:
                trend = "down"

        top3 = [{"full_name": r["full_name"], "avg_score_percent": round(r["avg_score_percent"], 1)} for r in score_rows[:3]]

        title, body = _build_ranking_message(rank_score, total_score, rank_att, total_att, trend)

        return {
            "rank_by_score": rank_score, "total_with_scores": total_score,
            "rank_by_attendance": rank_att, "total_with_attendance": total_att,
            "my_avg_score_percent": round(my_avg_score, 1) if my_avg_score is not None else None,
            "my_attendance_rate": round(my_attendance_rate, 1) if my_attendance_rate is not None else None,
            "trend": trend,
            "top3": top3,
            "message_title": title, "message_body": body,
        }


@app.get("/api/payments")
def get_payments(group_id: Optional[int] = None, month: Optional[str] = None,
                  session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    """جلب حالة المدفوعات لشهر معين، لمجموعة معينة أو كل الطلاب"""
    with get_connection() as conn:
        if session["role"] == "supervisor" and group_id:
            assert_supervisor_owns_group(conn, session, group_id)

        query = """
            SELECT s.id as student_id, s.full_name, s.group_id, g.name as group_name, g.monthly_fee as group_monthly_fee,
                   p.id as payment_id, p.amount, p.is_paid, p.paid_date, p.notes
            FROM students s
            JOIN groups g ON g.id = s.group_id
            LEFT JOIN payments p ON p.student_id = s.id AND p.month = ?
            WHERE 1=1
        """
        params = [month or ""]
        if group_id:
            query += " AND s.group_id = ?"
            params.append(group_id)
        if session["role"] == "supervisor":
            query += " AND s.group_id IN (SELECT id FROM groups WHERE supervisor_id=?)"
            params.append(session["id"])
        query += " ORDER BY s.full_name"

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/payments")
def set_payment(data: PaymentIn, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        student = conn.execute("SELECT group_id FROM students WHERE id=?", (data.student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])

        amount = data.amount
        if amount is None:
            grp = conn.execute("SELECT monthly_fee FROM groups WHERE id=?", (student["group_id"],)).fetchone()
            amount = grp["monthly_fee"] if grp else None

        paid_date = data.paid_date
        if data.is_paid and not paid_date:
            from datetime import date
            paid_date = date.today().isoformat()

        conn.execute("""
            INSERT INTO payments (student_id, month, amount, is_paid, paid_date, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id, month)
            DO UPDATE SET amount=excluded.amount, is_paid=excluded.is_paid,
                          paid_date=excluded.paid_date, notes=excluded.notes
        """, (data.student_id, data.month, amount, int(data.is_paid), paid_date, data.notes))
        return {"message": "تم حفظ بيانات الدفع"}


@app.post("/api/payments/send-reminders")
def trigger_payment_reminders(force: bool = Query(False),
                               session=Depends(require_roles("admin"))):
    """
    تشغيل يدوي لفحص وإرسال تذكيرات سداد الاشتراك (بالإضافة للفحص التلقائي الدوري).
    force=true بيتجاهل شرط قرب نهاية الشهر ويبعت التذكير لكل غير المسددين فورًا (مفيد للتجربة).
    """
    sent = send_subscription_payment_reminders(force=force)
    return {"message": f"تم إرسال {sent} إشعار تذكير سداد", "sent": sent}


# ---------------------------------------------------------------------------
# التقرير الشهري لكل طالب - Monthly Report (حضور + درجات + مدفوعات)
# ---------------------------------------------------------------------------

@app.get("/api/students/{student_id}/monthly-report")
def get_monthly_report(student_id: int, month: str, session=Depends(get_current_session)):
    """
    month بصيغة YYYY-MM
    يرجع: بيانات الطالب + ملخص الحضور + درجات الكويزات + حالة الدفع لنفس الشهر
    """
    with get_connection() as conn:
        student = conn.execute("""
            SELECT s.*, g.name as group_name, st.name as stage_name, gov.name as governorate_name,
                   u.full_name as supervisor_name, u.phone as supervisor_phone
            FROM students s
            JOIN groups g ON g.id = s.group_id
            JOIN stages st ON st.id = g.stage_id
            JOIN governorates gov ON gov.id = g.governorate_id
            LEFT JOIN users u ON u.id = g.supervisor_id
            WHERE s.id = ?
        """, (student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")

        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])
        elif session["role"] == "student":
            raise HTTPException(status_code=403, detail="التقرير الشهري متاح للمدرس والمشرف والأدمن بس")

        attendance_rows = conn.execute("""
            SELECT session_date, session_number, status FROM attendance
            WHERE student_id = ? AND session_date LIKE ?
            ORDER BY session_date, session_number
        """, (student_id, f"{month}%")).fetchall()
        attendance = [dict(r) for r in attendance_rows]
        att_summary = {"present": 0, "absent": 0, "late": 0, "excused": 0}
        for a in attendance:
            if a["status"] in att_summary:
                att_summary[a["status"]] += 1

        scores_rows = conn.execute("""
            SELECT q.title, q.quiz_date, q.max_score, qs.score
            FROM quiz_scores qs
            JOIN quizzes q ON q.id = qs.quiz_id
            WHERE qs.student_id = ? AND q.quiz_date LIKE ?
            ORDER BY q.quiz_date
        """, (student_id, f"{month}%")).fetchall()
        scores = [dict(r) for r in scores_rows]
        avg_pct = None
        if scores:
            pcts = [ (s["score"]/s["max_score"]*100) for s in scores if s["score"] is not None and s["max_score"] ]
            if pcts:
                avg_pct = round(sum(pcts)/len(pcts), 1)

        payment = conn.execute(
            "SELECT * FROM payments WHERE student_id=? AND month=?", (student_id, month)
        ).fetchone()

        return {
            "student": dict(student),
            "month": month,
            "attendance": attendance,
            "attendance_summary": att_summary,
            "scores": scores,
            "average_percentage": avg_pct,
            "payment": dict(payment) if payment else None,
        }


# ---------------------------------------------------------------------------
# الواجبات - Homework
# ---------------------------------------------------------------------------

@app.get("/api/homework")
def get_homework(group_id: Optional[int] = None, session=Depends(get_current_session)):
    """جلب الواجبات مع عدد المسلّمين لكل مجموعة"""
    with get_connection() as conn:
        query = """
            SELECT h.id, h.group_id, h.session_number, h.session_date, h.description,
                   g.name as group_name,
                   (SELECT COUNT(*) FROM homework_submissions hs WHERE hs.homework_id=h.id AND hs.done=1) as done_count,
                   (SELECT COUNT(*) FROM students s WHERE s.group_id=h.group_id AND s.is_active=1) as total_count
            FROM homework h
            JOIN groups g ON g.id = h.group_id
            WHERE 1=1
        """
        params = []
        if group_id:
            query += " AND h.group_id = ?"
            params.append(group_id)
        if session["role"] == "supervisor":
            query += " AND g.supervisor_id = ?"
            params.append(session["id"])
        elif session["role"] == "student":
            query += " AND h.group_id = ?"
            params.append(session.get("group_id"))
        # فلتر عام: إخفاء الواجبات اللي تاريخها قبل تاريخ أول اشتراك الطالب
        cutoff = student_content_cutoff(session)
        if cutoff:
            query += " AND (h.session_date IS NULL OR h.session_date >= ?)"
            params.append(cutoff)
        query += " ORDER BY h.session_number DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/homework")
def add_homework(data: HomeworkIn, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, data.group_id)
        try:
            cur = conn.execute(
                "INSERT INTO homework (group_id, session_number, session_date, description, created_by) VALUES (?,?,?,?,?)",
                (data.group_id, data.session_number, data.session_date, data.description, session["id"])
            )
            hw_id = cur.lastrowid
            # إنشاء سجلات تسليم لكل طلاب المجموعة تلقائياً
            students = conn.execute(
                "SELECT id FROM students WHERE group_id=? AND is_active=1", (data.group_id,)
            ).fetchall()
            for s in students:
                conn.execute(
                    "INSERT OR IGNORE INTO homework_submissions (homework_id, student_id) VALUES (?,?)",
                    (hw_id, s["id"])
                )
            return {"id": hw_id, "message": "تم إضافة الواجب"}
        except Exception:
            raise HTTPException(status_code=400, detail="في واجب موجود بالفعل لنفس الحصة دي")


@app.put("/api/homework/{hw_id}")
def update_homework(hw_id: int, data: HomeworkIn, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, data.group_id)
        conn.execute(
            "UPDATE homework SET description=?, session_date=? WHERE id=?",
            (data.description, data.session_date, hw_id)
        )
        return {"message": "تم تعديل الواجب"}


@app.delete("/api/homework/{hw_id}")
def delete_homework(hw_id: int, session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        conn.execute("DELETE FROM homework WHERE id=?", (hw_id,))
        return {"message": "تم حذف الواجب"}


@app.get("/api/homework/{hw_id}/submissions")
def get_homework_submissions(hw_id: int, session=Depends(require_roles("admin", "head_supervisor", "supervisor", "student"))):
    """جلب حالة تسليم الواجب لكل طلاب المجموعة (الطالب بيشوف حالته هو بس)"""
    with get_connection() as conn:
        hw = conn.execute("SELECT group_id, session_date FROM homework WHERE id=?", (hw_id,)).fetchone()
        if not hw:
            raise HTTPException(status_code=404, detail="الواجب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, hw["group_id"])
        if session["role"] == "student":
            # فلتر عام: الطالب ميقدرش يشوف واجب تاريخه قبل تاريخ أول اشتراك ليه
            cutoff = student_content_cutoff(session)
            if cutoff and hw["session_date"] and hw["session_date"] < cutoff:
                raise HTTPException(status_code=404, detail="الواجب غير موجود")
            # الطالب يشوف حالة تسليمه هو بس، ومن مجموعته هو بس
            if session.get("group_id") != hw["group_id"]:
                raise HTTPException(status_code=403, detail="مش مسموح لك تشوف واجبات مجموعة تانية")
            rows = conn.execute("""
                SELECT s.id as student_id, s.full_name,
                       hs.done, hs.notes
                FROM students s
                LEFT JOIN homework_submissions hs ON hs.student_id=s.id AND hs.homework_id=?
                WHERE s.id=? AND s.is_active=1
            """, (hw_id, session["id"])).fetchall()
            return [dict(r) for r in rows]
        rows = conn.execute("""
            SELECT s.id as student_id, s.full_name,
                   hs.done, hs.notes
            FROM students s
            LEFT JOIN homework_submissions hs ON hs.student_id=s.id AND hs.homework_id=?
            WHERE s.group_id=? AND s.is_active=1
            ORDER BY s.full_name
        """, (hw_id, hw["group_id"])).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/homework/{hw_id}/submissions")
def save_homework_submission(hw_id: int, data: HomeworkSubmissionIn,
                              session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    with get_connection() as conn:
        hw = conn.execute("SELECT group_id, session_number FROM homework WHERE id=?", (hw_id,)).fetchone()
        if not hw:
            raise HTTPException(status_code=404, detail="الواجب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, hw["group_id"])
        # جلب السجل الحالي
        existing = conn.execute(
            "SELECT * FROM homework_submissions WHERE homework_id=? AND student_id=?",
            (hw_id, data.student_id)
        ).fetchone()
        if existing:
            done_val = data.done if data.done is not None else existing["done"]
            notes_val = data.notes if data.notes is not None else existing["notes"]
            conn.execute(
                "UPDATE homework_submissions SET done=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE homework_id=? AND student_id=?",
                (done_val, notes_val, hw_id, data.student_id)
            )
        else:
            conn.execute(
                "INSERT INTO homework_submissions (homework_id, student_id, done, notes) VALUES (?,?,?,?)",
                (hw_id, data.student_id, data.done, data.notes)
            )
        create_notification(
            conn, data.student_id,
            "تم تسليم الواجب ✅" if data.done else "لسه ملسلمتش الواجب ❌",
            f"الحصة رقم {hw['session_number']}"
        )
        return {"message": "تم الحفظ"}


# ---------------------------------------------------------------------------
# الإشعارات - Notifications (للطالب حالياً)
# ---------------------------------------------------------------------------

@app.get("/api/notifications")
def get_notifications(session=Depends(get_current_session)):
    user_type = "student" if session["role"] == "student" else "user"
    cutoff = student_content_cutoff(session)
    with get_connection() as conn:
        query = """SELECT id, title, body, is_read, created_at FROM notifications
                   WHERE user_type=? AND user_id=?"""
        params = [user_type, session["id"]]
        if cutoff:
            query += " AND created_at >= ?"
            params.append(cutoff)
        query += " ORDER BY created_at DESC LIMIT 50"
        rows = conn.execute(query, params).fetchall()

        unread_query = "SELECT COUNT(*) as c FROM notifications WHERE user_type=? AND user_id=? AND is_read=0"
        unread_params = [user_type, session["id"]]
        if cutoff:
            unread_query += " AND created_at >= ?"
            unread_params.append(cutoff)
        unread = conn.execute(unread_query, unread_params).fetchone()["c"]
        return {"items": [dict(r) for r in rows], "unread_count": unread}


@app.put("/api/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int, session=Depends(get_current_session)):
    user_type = "student" if session["role"] == "student" else "user"
    with get_connection() as conn:
        conn.execute(
            "UPDATE notifications SET is_read=1 WHERE id=? AND user_type=? AND user_id=?",
            (notif_id, user_type, session["id"])
        )
        return {"message": "تم التعليم كمقروء"}


@app.put("/api/notifications/read-all")
def mark_all_notifications_read(session=Depends(get_current_session)):
    user_type = "student" if session["role"] == "student" else "user"
    with get_connection() as conn:
        conn.execute(
            "UPDATE notifications SET is_read=1 WHERE user_type=? AND user_id=? AND is_read=0",
            (user_type, session["id"])
        )
        return {"message": "تم تعليم كل الإشعارات كمقروءة"}


# ---------------------------------------------------------------------------
# طلبات الطلاب - Student Requests
# الطالب يقدم طلب (إذن حضور في معاد آخر / مواجهة مشكلة / طلب شرح) ويوصل لمشرف مجموعته
# ---------------------------------------------------------------------------

REQUEST_TYPE_LABELS = {
    "attendance_change": "إذن حضور المحاضرة في معاد آخر",
    "issue": "مواجهة مشكلة",
    "explanation": "طلب شرح جزء أو مسألة",
    "other": "طلب آخر",
}
REQUEST_STATUS_LABELS = {
    "pending": "قيد الانتظار",
    "in_progress": "جاري المتابعة",
    "resolved": "تم الحل",
}


@app.post("/api/student-requests")
def create_student_request(data: StudentRequestIn, session=Depends(get_current_session)):
    """الطالب بيقدم طلب جديد (إذن حضور في معاد آخر / مواجهة مشكلة / طلب شرح...)"""
    if session["type"] != "student":
        raise HTTPException(status_code=403, detail="الطلبات دي مخصصة للطلاب بس")
    if data.request_type not in REQUEST_TYPE_LABELS:
        raise HTTPException(status_code=400, detail="نوع الطلب غير معروف")
    if not session.get("group_id"):
        raise HTTPException(status_code=400, detail="معندكش مجموعة لسه، كلم الأدمن")

    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO student_requests (student_id, group_id, request_type, details)
               VALUES (?, ?, ?, ?)""",
            (session["id"], session["group_id"], data.request_type, (data.details or "").strip() or None)
        )
        request_id = cur.lastrowid

        group = conn.execute("SELECT * FROM groups WHERE id=?", (session["group_id"],)).fetchone()
        if group and group["supervisor_id"]:
            create_user_notification(
                conn, group["supervisor_id"],
                f"طلب جديد من الطالب {session['full_name']}",
                REQUEST_TYPE_LABELS.get(data.request_type, data.request_type)
            )
        return {"message": "تم إرسال طلبك لمشرف مجموعتك", "id": request_id}


@app.get("/api/student-requests")
def get_student_requests(status: Optional[str] = None, group_id: Optional[int] = None, session=Depends(get_current_session)):
    """
    عرض الطلبات:
    - الطالب: يشوف طلباته هو بس
    - المشرف: يشوف طلبات مجموعته/مجموعاته بس
    - الأدمن/مشرف المشرفين: يشوف كل الطلبات
    """
    with get_connection() as conn:
        query = """
            SELECT sr.*, s.full_name AS student_name, g.name AS group_name
            FROM student_requests sr
            JOIN students s ON s.id = sr.student_id
            JOIN groups g ON g.id = sr.group_id
            WHERE 1=1
        """
        params = []

        if session["role"] == "student":
            query += " AND sr.student_id = ?"
            params.append(session["id"])
        elif session["role"] == "supervisor":
            group_ids = supervised_group_ids(conn, session["id"])
            if not group_ids:
                return []
            placeholders = ",".join("?" * len(group_ids))
            query += f" AND sr.group_id IN ({placeholders})"
            params.extend(group_ids)
        elif session["role"] not in ("admin", "head_supervisor"):
            raise HTTPException(status_code=403, detail="مفيش صلاحية للوصول لده")

        if status:
            query += " AND sr.status = ?"
            params.append(status)
        if group_id:
            query += " AND sr.group_id = ?"
            params.append(group_id)

        query += " ORDER BY sr.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.put("/api/student-requests/{request_id}")
def update_student_request(request_id: int, data: StudentRequestStatusIn,
                            session=Depends(require_roles("admin", "head_supervisor", "supervisor"))):
    """المشرف يرد على طلب الطالب ويغيّر حالته (قيد الانتظار / جاري المتابعة / تم الحل)"""
    if data.status not in REQUEST_STATUS_LABELS:
        raise HTTPException(status_code=400, detail="حالة الطلب غير معروفة")

    with get_connection() as conn:
        req = conn.execute("SELECT * FROM student_requests WHERE id=?", (request_id,)).fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="الطلب غير موجود")

        assert_supervisor_owns_group(conn, session, req["group_id"])

        resolved_at = datetime.utcnow().isoformat(timespec="seconds") if data.status == "resolved" else None
        conn.execute(
            """UPDATE student_requests SET status=?, supervisor_reply=?, resolved_at=?
               WHERE id=?""",
            (data.status, (data.supervisor_reply or "").strip() or None, resolved_at, request_id)
        )

        title = f"تحديث على طلبك: {REQUEST_TYPE_LABELS.get(req['request_type'], req['request_type'])}"
        body = f"الحالة: {REQUEST_STATUS_LABELS.get(data.status, data.status)}"
        if data.supervisor_reply:
            body += f" - {data.supervisor_reply}"
        create_notification(conn, req["student_id"], title, body)

        return {"message": "تم تحديث الطلب"}


# ---------------------------------------------------------------------------
# إحصائيات لوحة المدرس/الأدمن - Executive Dashboard Overview
# ---------------------------------------------------------------------------

@app.get("/api/stats/overview")
def get_stats_overview(session=Depends(require_roles("admin", "teacher", "head_supervisor"))):
    """
    إندبوينت واحد بيجمع كل البيانات اللازمة للوحة المدرس التنفيذية:
    إجماليات، أداء المجموعات، أفضل الطلاب، مقارنة بالمراحل، واتجاه آخر الكويزات.
    للأدمن والمدرس بس (عرض فقط، من غير أي تعديل).
    """
    with get_connection() as conn:
        groups_count = conn.execute("SELECT COUNT(*) c FROM groups").fetchone()["c"]
        students_count = conn.execute("SELECT COUNT(*) c FROM students WHERE is_active=1").fetchone()["c"]
        quizzes_count = conn.execute("SELECT COUNT(*) c FROM quizzes").fetchone()["c"]

        att_row = conn.execute("""
            SELECT
              SUM(CASE WHEN status IN ('present','late') THEN 1 ELSE 0 END) as present_c,
              COUNT(*) as total_c
            FROM attendance
        """).fetchone()
        avg_attendance_rate = round((att_row["present_c"] / att_row["total_c"]) * 100, 1) if att_row["total_c"] else None

        score_row = conn.execute("""
            SELECT AVG(qs.score * 100.0 / q.max_score) as avg_pct
            FROM quiz_scores qs JOIN quizzes q ON q.id = qs.quiz_id
            WHERE q.max_score > 0
        """).fetchone()
        avg_score_percent = round(score_row["avg_pct"], 1) if score_row["avg_pct"] is not None else None

        # أداء كل مجموعة: متوسط الدرجات + نسبة الحضور + عدد الطلاب
        groups_overview = conn.execute("""
            SELECT g.id, g.name, st.name as stage_name, gov.name as governorate_name,
                   u.full_name as supervisor_name,
                   (SELECT COUNT(*) FROM students s WHERE s.group_id=g.id AND s.is_active=1) as students_count,
                   (SELECT AVG(qs.score * 100.0 / q.max_score)
                      FROM quiz_scores qs JOIN quizzes q ON q.id=qs.quiz_id
                      JOIN students s2 ON s2.id=qs.student_id
                      WHERE s2.group_id=g.id AND q.max_score>0) as avg_score_percent,
                   (SELECT (SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)*100.0/COUNT(*))
                      FROM attendance a JOIN students s3 ON s3.id=a.student_id
                      WHERE s3.group_id=g.id) as attendance_rate
            FROM groups g
            JOIN stages st ON st.id=g.stage_id
            JOIN governorates gov ON gov.id=g.governorate_id
            LEFT JOIN users u ON u.id=g.supervisor_id
            ORDER BY avg_score_percent DESC NULLS LAST
        """).fetchall()
        groups_overview = [dict(r) for r in groups_overview]
        for g in groups_overview:
            g["avg_score_percent"] = round(g["avg_score_percent"], 1) if g["avg_score_percent"] is not None else None
            g["attendance_rate"] = round(g["attendance_rate"], 1) if g["attendance_rate"] is not None else None

        # أفضل 10 طلاب حسب متوسط الدرجات (لازم يكون عنده درجة واحدة على الأقل)
        top_students = conn.execute("""
            SELECT s.id, s.full_name, g.name as group_name, st.name as stage_name,
                   AVG(qs.score * 100.0 / q.max_score) as avg_score_percent,
                   COUNT(qs.id) as quizzes_taken
            FROM students s
            JOIN quiz_scores qs ON qs.student_id=s.id
            JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            JOIN groups g ON g.id=s.group_id
            JOIN stages st ON st.id=g.stage_id
            WHERE s.is_active=1
            GROUP BY s.id
            ORDER BY avg_score_percent DESC
            LIMIT 10
        """).fetchall()
        top_students = [dict(r) for r in top_students]
        for s in top_students:
            s["avg_score_percent"] = round(s["avg_score_percent"], 1)

        # مقارنة المراحل الدراسية (تجميع المجموعات حسب المرحلة)
        stage_breakdown = conn.execute("""
            SELECT st.id as stage_id, st.name as stage_name,
                   COUNT(DISTINCT g.id) as groups_count,
                   (SELECT COUNT(*) FROM students s WHERE s.group_id IN
                      (SELECT id FROM groups WHERE stage_id=st.id) AND s.is_active=1) as students_count,
                   (SELECT AVG(qs.score * 100.0 / q.max_score)
                      FROM quiz_scores qs JOIN quizzes q ON q.id=qs.quiz_id
                      JOIN students s2 ON s2.id=qs.student_id
                      JOIN groups g2 ON g2.id=s2.group_id
                      WHERE g2.stage_id=st.id AND q.max_score>0) as avg_score_percent
            FROM stages st
            LEFT JOIN groups g ON g.stage_id=st.id
            GROUP BY st.id
            HAVING groups_count > 0
            ORDER BY st.name
        """).fetchall()
        stage_breakdown = [dict(r) for r in stage_breakdown]
        for sb in stage_breakdown:
            sb["avg_score_percent"] = round(sb["avg_score_percent"], 1) if sb["avg_score_percent"] is not None else None

        # اتجاه آخر 10 كويزات (متوسط الدرجة لكل كويز) عشان رسم بياني بسيط
        score_trend = conn.execute("""
            SELECT q.id, q.title, q.quiz_date,
                   AVG(qs.score * 100.0 / q.max_score) as avg_score_percent
            FROM quizzes q
            JOIN quiz_scores qs ON qs.quiz_id=q.id
            WHERE q.max_score>0
            GROUP BY q.id
            ORDER BY q.quiz_date DESC, q.id DESC
            LIMIT 10
        """).fetchall()
        score_trend = [dict(r) for r in score_trend][::-1]
        for t in score_trend:
            t["avg_score_percent"] = round(t["avg_score_percent"], 1) if t["avg_score_percent"] is not None else None

        # أكثر الطلاب غيابًا - ما يظهرش إلا اللي غاب 3 حصص أو أكتر
        most_absent_students = conn.execute("""
            SELECT s.id, s.full_name, g.name as group_name,
                   SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END) as absent_count
            FROM students s
            JOIN attendance a ON a.student_id=s.id
            JOIN groups g ON g.id=s.group_id
            WHERE s.is_active=1
            GROUP BY s.id
            HAVING absent_count >= 3
            ORDER BY absent_count DESC
            LIMIT 10
        """).fetchall()
        most_absent_students = [dict(r) for r in most_absent_students]

        return {
            "totals": {
                "groups": groups_count,
                "students": students_count,
                "quizzes": quizzes_count,
                "avg_attendance_rate": avg_attendance_rate,
                "avg_score_percent": avg_score_percent,
            },
            "groups_overview": groups_overview,
            "top_students": top_students,
            "most_absent_students": most_absent_students,
            "stage_breakdown": stage_breakdown,
            "score_trend": score_trend,
        }


def _date_clause(column, date_from, date_to, params):
    """بيبني شرط التاريخ (من/لحد) ويضيف الـ params المطلوبة بنفس الترتيب"""
    clause = ""
    if date_from:
        clause += f" AND {column} >= ?"
        params.append(date_from)
    if date_to:
        clause += f" AND {column} <= ?"
        params.append(date_to)
    return clause


@app.get("/api/stats/stage-overview")
def get_stage_overview(stage_id: int, governorate_id: Optional[int] = None,
                        date_from: Optional[str] = None, date_to: Optional[str] = None,
                        session=Depends(require_roles("admin", "teacher", "head_supervisor"))):
    """
    نظرة عامة على سنة دراسية كاملة (مرحلة) - مع إمكانية التصفية بمحافظة وفترة زمنية.
    كل المقارنات والترتيبات هنا محصورة داخل نفس السنة الدراسية المختارة فقط.
    """
    with get_connection() as conn:
        stage = conn.execute("SELECT id, name FROM stages WHERE id=?", (stage_id,)).fetchone()
        if not stage:
            raise HTTPException(status_code=404, detail="المرحلة غير موجودة")

        gov_filter_sql = " AND g.governorate_id = ?" if governorate_id else ""

        def base_params():
            p = [stage_id]
            if governorate_id:
                p.append(governorate_id)
            return p

        # ---- إجماليات السنة الدراسية ----
        groups_count = conn.execute(
            f"SELECT COUNT(*) c FROM groups g WHERE g.stage_id=?{gov_filter_sql}", base_params()
        ).fetchone()["c"]
        students_count = conn.execute(
            f"""SELECT COUNT(*) c FROM students s JOIN groups g ON g.id=s.group_id
                WHERE g.stage_id=?{gov_filter_sql} AND s.is_active=1""", base_params()
        ).fetchone()["c"]

        att_params = base_params()
        att_clause = _date_clause("a.session_date", date_from, date_to, att_params)
        att_row = conn.execute(f"""
            SELECT SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END) as present_c, COUNT(*) as total_c
            FROM attendance a JOIN students s ON s.id=a.student_id JOIN groups g ON g.id=s.group_id
            WHERE g.stage_id=?{gov_filter_sql}{att_clause}
        """, att_params).fetchone()
        attendance_rate = round(att_row["present_c"]*100.0/att_row["total_c"], 1) if att_row["total_c"] else None

        score_params = base_params()
        score_clause = _date_clause("q.quiz_date", date_from, date_to, score_params)
        score_row = conn.execute(f"""
            SELECT AVG(qs.score*100.0/q.max_score) as avg_pct
            FROM quiz_scores qs JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            JOIN students s ON s.id=qs.student_id JOIN groups g ON g.id=s.group_id
            WHERE g.stage_id=?{gov_filter_sql}{score_clause}
        """, score_params).fetchone()
        avg_score_percent = round(score_row["avg_pct"], 1) if score_row["avg_pct"] is not None else None

        hw_params = base_params()
        hw_clause = _date_clause("h.session_date", date_from, date_to, hw_params)
        hw_row = conn.execute(f"""
            SELECT SUM(CASE WHEN hs.done=1 THEN 1 ELSE 0 END) as done_c, COUNT(*) as total_c
            FROM homework_submissions hs JOIN homework h ON h.id=hs.homework_id JOIN groups g ON g.id=h.group_id
            WHERE g.stage_id=?{gov_filter_sql}{hw_clause}
        """, hw_params).fetchone()
        commitment_rate = round(hw_row["done_c"]*100.0/hw_row["total_c"], 1) if hw_row["total_c"] else None

        # ---- توزيع المحافظات داخل السنة الدراسية (من غير فلتر المحافظة، عشان تبان كل المحافظات) ----
        gov_score_params = [stage_id]
        gov_score_clause = _date_clause("q.quiz_date", date_from, date_to, gov_score_params)
        gov_att_params = [stage_id]
        gov_att_clause = _date_clause("a.session_date", date_from, date_to, gov_att_params)
        governorates_breakdown = conn.execute(f"""
            SELECT gov.id as governorate_id, gov.name as governorate_name,
                   COUNT(DISTINCT g.id) as groups_count,
                   (SELECT COUNT(*) FROM students s WHERE s.group_id IN
                      (SELECT id FROM groups WHERE stage_id=? AND governorate_id=gov.id) AND s.is_active=1) as students_count,
                   (SELECT AVG(qs.score*100.0/q.max_score) FROM quiz_scores qs
                      JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
                      JOIN students s2 ON s2.id=qs.student_id JOIN groups g2 ON g2.id=s2.group_id
                      WHERE g2.stage_id=? AND g2.governorate_id=gov.id{gov_score_clause}) as avg_score_percent,
                   (SELECT SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)*100.0/COUNT(*)
                      FROM attendance a JOIN students s3 ON s3.id=a.student_id JOIN groups g3 ON g3.id=s3.group_id
                      WHERE g3.stage_id=? AND g3.governorate_id=gov.id{gov_att_clause}) as attendance_rate
            FROM governorates gov
            JOIN groups g ON g.stage_id=? AND g.governorate_id=gov.id
            GROUP BY gov.id
            ORDER BY gov.name
        """, [stage_id, stage_id] + gov_score_params[1:] + [stage_id] + gov_att_params[1:] + [stage_id]).fetchall()
        governorates_breakdown = [dict(r) for r in governorates_breakdown]
        for gb in governorates_breakdown:
            gb["avg_score_percent"] = round(gb["avg_score_percent"], 1) if gb["avg_score_percent"] is not None else None
            gb["attendance_rate"] = round(gb["attendance_rate"], 1) if gb["attendance_rate"] is not None else None

        # ---- ترتيب المجموعات (كل المؤشرات سوا، الفرونت بيرتب حسب اللي محتاجه) ----
        g_score_params = []
        g_score_clause = _date_clause("q.quiz_date", date_from, date_to, g_score_params)
        g_att_params = []
        g_att_clause = _date_clause("a.session_date", date_from, date_to, g_att_params)
        g_hw_params = []
        g_hw_clause = _date_clause("h.session_date", date_from, date_to, g_hw_params)
        outer_params = base_params()

        groups_ranking = conn.execute(f"""
            SELECT g.id, g.name, gov.name as governorate_name, u.full_name as supervisor_name,
                   (SELECT COUNT(*) FROM students s WHERE s.group_id=g.id AND s.is_active=1) as students_count,
                   (SELECT AVG(qs.score*100.0/q.max_score) FROM quiz_scores qs
                      JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
                      JOIN students s2 ON s2.id=qs.student_id
                      WHERE s2.group_id=g.id{g_score_clause}) as avg_score_percent,
                   (SELECT SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)*100.0/COUNT(*)
                      FROM attendance a JOIN students s3 ON s3.id=a.student_id
                      WHERE s3.group_id=g.id{g_att_clause}) as attendance_rate,
                   (SELECT SUM(CASE WHEN hs.done=1 THEN 1 ELSE 0 END)*100.0/COUNT(*)
                      FROM homework_submissions hs JOIN homework h ON h.id=hs.homework_id
                      WHERE h.group_id=g.id{g_hw_clause}) as commitment_rate
            FROM groups g
            JOIN governorates gov ON gov.id=g.governorate_id
            LEFT JOIN users u ON u.id=g.supervisor_id
            WHERE g.stage_id=?{gov_filter_sql}
            ORDER BY avg_score_percent DESC NULLS LAST
        """, g_score_params + g_att_params + g_hw_params + outer_params).fetchall()
        groups_ranking = [dict(r) for r in groups_ranking]
        for g in groups_ranking:
            g["avg_score_percent"] = round(g["avg_score_percent"], 1) if g["avg_score_percent"] is not None else None
            g["attendance_rate"] = round(g["attendance_rate"], 1) if g["attendance_rate"] is not None else None
            g["commitment_rate"] = round(g["commitment_rate"], 1) if g["commitment_rate"] is not None else None

        # ---- ترتيب الطلاب (أفضل / أكثر التزامًا / أكثر غيابًا) ----
        s_score_params = base_params()
        s_score_clause = _date_clause("q.quiz_date", date_from, date_to, s_score_params)
        top_students = conn.execute(f"""
            SELECT s.id, s.full_name, g.name as group_name, gov.name as governorate_name,
                   AVG(qs.score*100.0/q.max_score) as avg_score_percent, COUNT(qs.id) as quizzes_taken
            FROM students s
            JOIN quiz_scores qs ON qs.student_id=s.id
            JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            JOIN groups g ON g.id=s.group_id
            JOIN governorates gov ON gov.id=g.governorate_id
            WHERE g.stage_id=?{gov_filter_sql} AND s.is_active=1{s_score_clause}
            GROUP BY s.id ORDER BY avg_score_percent DESC LIMIT 10
        """, s_score_params).fetchall()
        top_students = [dict(r) for r in top_students]
        for s in top_students:
            s["avg_score_percent"] = round(s["avg_score_percent"], 1)

        s_att_params = base_params()
        s_att_clause = _date_clause("a.session_date", date_from, date_to, s_att_params)
        students_attendance = conn.execute(f"""
            SELECT s.id, s.full_name, g.name as group_name, gov.name as governorate_name,
                   SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)*100.0/COUNT(*) as attendance_rate,
                   SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END) as absent_count,
                   COUNT(*) as records_count
            FROM students s
            JOIN attendance a ON a.student_id=s.id
            JOIN groups g ON g.id=s.group_id
            JOIN governorates gov ON gov.id=g.governorate_id
            WHERE g.stage_id=?{gov_filter_sql} AND s.is_active=1{s_att_clause}
            GROUP BY s.id
        """, s_att_params).fetchall()
        students_attendance = [dict(r) for r in students_attendance]
        for s in students_attendance:
            s["attendance_rate"] = round(s["attendance_rate"], 1)
        most_committed_students = sorted(students_attendance, key=lambda x: x["attendance_rate"], reverse=True)[:10]
        most_absent_students = sorted(
            [s for s in students_attendance if s["absent_count"] >= 3],
            key=lambda x: x["absent_count"], reverse=True
        )[:10]

        # ---- اتجاه متوسط الدرجات لآخر 10 كويزات داخل السنة الدراسية ----
        t_score_params = base_params()
        t_score_clause = _date_clause("q.quiz_date", date_from, date_to, t_score_params)
        score_trend = conn.execute(f"""
            SELECT q.id, q.title, q.quiz_date, AVG(qs.score*100.0/q.max_score) as avg_score_percent
            FROM quizzes q
            JOIN quiz_scores qs ON qs.quiz_id=q.id
            JOIN students s ON s.id=qs.student_id
            JOIN groups g ON g.id=s.group_id
            WHERE g.stage_id=?{gov_filter_sql} AND q.max_score>0{t_score_clause}
            GROUP BY q.id ORDER BY q.quiz_date DESC, q.id DESC LIMIT 10
        """, t_score_params).fetchall()
        score_trend = [dict(r) for r in score_trend][::-1]
        for t in score_trend:
            t["avg_score_percent"] = round(t["avg_score_percent"], 1) if t["avg_score_percent"] is not None else None

        return {
            "stage": {"id": stage["id"], "name": stage["name"]},
            "filters_applied": {
                "governorate_id": governorate_id, "date_from": date_from, "date_to": date_to
            },
            "totals": {
                "groups": groups_count, "students": students_count,
                "attendance_rate": attendance_rate, "commitment_rate": commitment_rate,
                "avg_score_percent": avg_score_percent,
            },
            "governorates_breakdown": governorates_breakdown,
            "groups_ranking": groups_ranking,
            "top_students": top_students,
            "most_committed_students": most_committed_students,
            "most_absent_students": most_absent_students,
            "score_trend": score_trend,
        }


@app.get("/api/stats/group-detail")
def get_group_detail(group_id: int, date_from: Optional[str] = None, date_to: Optional[str] = None,
                      session=Depends(require_roles("admin", "teacher", "head_supervisor"))):
    """تفاصيل أداء مجموعة واحدة + ترتيبها بين باقي مجموعات نفس السنة الدراسية"""
    with get_connection() as conn:
        group = conn.execute("""
            SELECT g.id, g.name, g.stage_id, st.name as stage_name, gov.name as governorate_name,
                   u.full_name as supervisor_name,
                   (SELECT COUNT(*) FROM students s WHERE s.group_id=g.id AND s.is_active=1) as students_count
            FROM groups g
            JOIN stages st ON st.id=g.stage_id
            JOIN governorates gov ON gov.id=g.governorate_id
            LEFT JOIN users u ON u.id=g.supervisor_id
            WHERE g.id=?
        """, (group_id,)).fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
        group = dict(group)
        stage_id = group["stage_id"]

        score_params = [group_id]
        score_clause = _date_clause("q.quiz_date", date_from, date_to, score_params)
        score_row = conn.execute(f"""
            SELECT AVG(qs.score*100.0/q.max_score) as avg_pct
            FROM quiz_scores qs JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            JOIN students s ON s.id=qs.student_id
            WHERE s.group_id=?{score_clause}
        """, score_params).fetchone()
        avg_score_percent = round(score_row["avg_pct"], 1) if score_row["avg_pct"] is not None else None

        att_params = [group_id]
        att_clause = _date_clause("a.session_date", date_from, date_to, att_params)
        att_row = conn.execute(f"""
            SELECT SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END) as present_c, COUNT(*) as total_c
            FROM attendance a JOIN students s ON s.id=a.student_id
            WHERE s.group_id=?{att_clause}
        """, att_params).fetchone()
        attendance_rate = round(att_row["present_c"]*100.0/att_row["total_c"], 1) if att_row["total_c"] else None

        hw_params = [group_id]
        hw_clause = _date_clause("h.session_date", date_from, date_to, hw_params)
        hw_row = conn.execute(f"""
            SELECT SUM(CASE WHEN hs.done=1 THEN 1 ELSE 0 END) as done_c, COUNT(*) as total_c
            FROM homework_submissions hs JOIN homework h ON h.id=hs.homework_id
            WHERE h.group_id=?{hw_clause}
        """, hw_params).fetchone()
        commitment_rate = round(hw_row["done_c"]*100.0/hw_row["total_c"], 1) if hw_row["total_c"] else None

        # ترتيب المجموعة بين باقي مجموعات نفس السنة الدراسية (حسب متوسط الدرجات)
        rank_score_params = []
        rank_score_clause = _date_clause("q.quiz_date", date_from, date_to, rank_score_params)
        all_groups_scores = conn.execute(f"""
            SELECT g.id,
                   (SELECT AVG(qs.score*100.0/q.max_score) FROM quiz_scores qs
                      JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
                      JOIN students s2 ON s2.id=qs.student_id
                      WHERE s2.group_id=g.id{rank_score_clause}) as avg_score_percent
            FROM groups g WHERE g.stage_id=?
        """, rank_score_params + [stage_id]).fetchall()
        scored = [dict(r) for r in all_groups_scores if r["avg_score_percent"] is not None]
        scored.sort(key=lambda x: x["avg_score_percent"], reverse=True)
        rank_position = next((i+1 for i, g in enumerate(scored) if g["id"] == group_id), None)

        # اتجاه الدرجات عبر آخر الكويزات للمجموعة دي بس
        trend_params = [group_id]
        trend_clause = _date_clause("q.quiz_date", date_from, date_to, trend_params)
        score_trend = conn.execute(f"""
            SELECT q.id, q.title, q.quiz_date, AVG(qs.score*100.0/q.max_score) as avg_score_percent
            FROM quizzes q JOIN quiz_scores qs ON qs.quiz_id=q.id
            JOIN students s ON s.id=qs.student_id
            WHERE s.group_id=? AND q.max_score>0{trend_clause}
            GROUP BY q.id ORDER BY q.quiz_date DESC, q.id DESC LIMIT 10
        """, trend_params).fetchall()
        score_trend = [dict(r) for r in score_trend][::-1]
        for t in score_trend:
            t["avg_score_percent"] = round(t["avg_score_percent"], 1) if t["avg_score_percent"] is not None else None

        # طلاب المجموعة: أفضل أداء + الأكثر التزامًا (حضورًا) + الأكثر غيابًا
        ts_params = [group_id]
        ts_clause = _date_clause("q.quiz_date", date_from, date_to, ts_params)
        top_students = conn.execute(f"""
            SELECT s.id, s.full_name, AVG(qs.score*100.0/q.max_score) as avg_score_percent, COUNT(qs.id) as quizzes_taken
            FROM students s JOIN quiz_scores qs ON qs.student_id=s.id
            JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            WHERE s.group_id=? AND s.is_active=1{ts_clause}
            GROUP BY s.id ORDER BY avg_score_percent DESC LIMIT 10
        """, ts_params).fetchall()
        top_students = [dict(r) for r in top_students]
        for s in top_students:
            s["avg_score_percent"] = round(s["avg_score_percent"], 1)

        att_students_params = [group_id]
        att_students_clause = _date_clause("a.session_date", date_from, date_to, att_students_params)
        students_attendance = conn.execute(f"""
            SELECT s.id, s.full_name, SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)*100.0/COUNT(*) as attendance_rate,
                   SUM(CASE WHEN a.status='absent' THEN 1 ELSE 0 END) as absent_count,
                   COUNT(*) as records_count
            FROM students s JOIN attendance a ON a.student_id=s.id
            WHERE s.group_id=? AND s.is_active=1{att_students_clause}
            GROUP BY s.id
        """, att_students_params).fetchall()
        students_attendance = [dict(r) for r in students_attendance]
        for s in students_attendance:
            s["attendance_rate"] = round(s["attendance_rate"], 1)
        most_committed_students = sorted(students_attendance, key=lambda x: x["attendance_rate"], reverse=True)[:10]
        most_absent_students = sorted(
            [s for s in students_attendance if s["absent_count"] >= 3],
            key=lambda x: x["absent_count"], reverse=True
        )[:10]

        return {
            "group": group,
            "totals": {
                "avg_score_percent": avg_score_percent, "attendance_rate": attendance_rate,
                "commitment_rate": commitment_rate,
            },
            "rank": {"position": rank_position, "total_groups": len(scored)},
            "score_trend": score_trend,
            "top_students": top_students,
            "most_committed_students": most_committed_students,
            "most_absent_students": most_absent_students,
        }


# ---------------------------------------------------------------------------
# Ranking متقدم — Advanced Multi-Dimension Rankings
# ---------------------------------------------------------------------------

import math

def _safe_round(val, ndigits=1):
    return round(val, ndigits) if val is not None else None

def _stddev(values):
    """حساب الانحراف المعياري (مقياس الاستقرار)"""
    n = len(values)
    if n < 2: return 0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(variance)


@app.get("/api/stats/rankings")
def get_rankings(stage_id: int, governorate_id: Optional[int] = None,
                 date_from: Optional[str] = None, date_to: Optional[str] = None,
                 pass_threshold: int = 50,
                 session=Depends(require_roles("admin", "teacher", "head_supervisor"))):
    """
    Ranking متقدم لكل الأبعاد — الطلاب والمجموعات داخل نفس السنة الدراسية فقط.
    يرجع ترتيبات منفصلة لكل بُعد.
    """
    gov_sql = " AND g.governorate_id=?" if governorate_id else ""

    def bp():
        p = [stage_id]
        if governorate_id: p.append(governorate_id)
        return p

    with get_connection() as conn:
        # ======= جمع كل درجات الطلاب مرتبة بالتاريخ (لحساب التحسن) =======
        sc_params = bp()
        sc_clause = _date_clause("q.quiz_date", date_from, date_to, sc_params)
        raw_scores = conn.execute(f"""
            SELECT s.id as sid, s.full_name, g.name as group_name, gov.name as governorate_name,
                   q.quiz_date, qs.score * 100.0 / q.max_score as pct
            FROM quiz_scores qs
            JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            JOIN students s ON s.id=qs.student_id AND s.is_active=1
            JOIN groups g ON g.id=s.group_id
            JOIN governorates gov ON gov.id=g.governorate_id
            WHERE g.stage_id=?{gov_sql}{sc_clause}
            ORDER BY q.quiz_date ASC, q.id ASC
        """, sc_params).fetchall()

        # Group by student
        from collections import defaultdict
        student_scores = defaultdict(list)
        student_meta = {}
        for r in raw_scores:
            student_scores[r["sid"]].append(r["pct"])
            if r["sid"] not in student_meta:
                student_meta[r["sid"]] = {"id": r["sid"], "full_name": r["full_name"],
                                           "group_name": r["group_name"], "governorate_name": r["governorate_name"]}

        # ======= حضور الطلاب =======
        att_params = bp()
        att_clause = _date_clause("a.session_date", date_from, date_to, att_params)
        att_rows = conn.execute(f"""
            SELECT s.id as sid, s.full_name, g.name as group_name, gov.name as governorate_name,
                   SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)*100.0/COUNT(*) as rate,
                   COUNT(*) as total
            FROM attendance a
            JOIN students s ON s.id=a.student_id AND s.is_active=1
            JOIN groups g ON g.id=s.group_id
            JOIN governorates gov ON gov.id=g.governorate_id
            WHERE g.stage_id=?{gov_sql}{att_clause}
            GROUP BY s.id HAVING total >= 1
        """, att_params).fetchall()

        # ======= واجبات الطلاب =======
        hw_params = bp()
        hw_clause = _date_clause("h.session_date", date_from, date_to, hw_params)
        hw_rows = conn.execute(f"""
            SELECT s.id as sid, s.full_name, g.name as group_name, gov.name as governorate_name,
                   SUM(CASE WHEN hs.done=1 THEN 1 ELSE 0 END)*100.0/COUNT(*) as rate,
                   COUNT(*) as total
            FROM homework_submissions hs
            JOIN homework h ON h.id=hs.homework_id
            JOIN students s ON s.id=hs.student_id AND s.is_active=1
            JOIN groups g ON g.id=s.group_id
            JOIN governorates gov ON gov.id=g.governorate_id
            WHERE g.stage_id=?{gov_sql}{hw_clause}
            GROUP BY s.id HAVING total >= 1
        """, hw_params).fetchall()

        # ======= حساب ترتيبات الطلاب =======
        def make_student(sid, extra):
            m = student_meta.get(sid, {})
            return {**m, **extra}

        # 1. أفضل امتحانات (متوسط الدرجات)
        exam_rank = sorted(
            [{"id": sid, "full_name": m["full_name"], "group_name": m["group_name"],
              "governorate_name": m["governorate_name"],
              "value": round(sum(scores)/len(scores), 1), "quizzes": len(scores), "detail": f"{len(scores)} كويز"}
             for sid, scores in student_scores.items() for m in [student_meta[sid]]],
            key=lambda x: x["value"], reverse=True
        )[:15]

        # 2. أفضل حضور
        att_rank = sorted(
            [{"id": r["sid"], "full_name": r["full_name"], "group_name": r["group_name"],
              "governorate_name": r["governorate_name"],
              "value": round(r["rate"], 1), "detail": f"{r['total']} جلسة"}
             for r in att_rows],
            key=lambda x: x["value"], reverse=True
        )[:15]

        # 3. أفضل التزام بالواجبات
        hw_rank = sorted(
            [{"id": r["sid"], "full_name": r["full_name"], "group_name": r["group_name"],
              "governorate_name": r["governorate_name"],
              "value": round(r["rate"], 1), "detail": f"{r['total']} واجب"}
             for r in hw_rows],
            key=lambda x: x["value"], reverse=True
        )[:15]

        # 4. أكثر تحسناً (فرق متوسط النصف الثاني - النصف الأول، لازم ≥ 2 كويز)
        improvement_list = []
        for sid, scores in student_scores.items():
            if len(scores) < 2: continue
            half = len(scores) // 2
            first_avg = sum(scores[:half]) / half
            second_avg = sum(scores[half:]) / (len(scores) - half)
            delta = second_avg - first_avg
            m = student_meta[sid]
            improvement_list.append({
                "id": sid, "full_name": m["full_name"], "group_name": m["group_name"],
                "governorate_name": m["governorate_name"],
                "value": round(delta, 1), "detail": f"من {round(first_avg,1)}% ← {round(second_avg,1)}%",
                "quizzes": len(scores)
            })
        improvement_rank = sorted(improvement_list, key=lambda x: x["value"], reverse=True)[:15]

        # 5. مؤشر الالتزام المركب (متوسط حضور + واجبات)
        att_by_sid = {r["sid"]: r["rate"] for r in att_rows}
        hw_by_sid = {r["sid"]: r["rate"] for r in hw_rows}
        commitment_list = []
        all_sids = set(att_by_sid) | set(hw_by_sid)
        for sid in all_sids:
            parts = [v for v in [att_by_sid.get(sid), hw_by_sid.get(sid)] if v is not None]
            if not parts: continue
            score = sum(parts) / len(parts)
            m = student_meta.get(sid)
            if not m: continue
            commitment_list.append({
                "id": sid, "full_name": m["full_name"], "group_name": m["group_name"],
                "governorate_name": m["governorate_name"],
                "value": round(score, 1),
                "detail": f"حضور {round(att_by_sid.get(sid,0),0):.0f}% · واجبات {round(hw_by_sid.get(sid,0),0):.0f}%"
            })
        commitment_rank = sorted(commitment_list, key=lambda x: x["value"], reverse=True)[:15]

        # ======= ترتيبات المجموعات =======
        # جمع درجات المجموعات مرتبة بالوقت (لحساب التحسن والاستقرار)
        grp_sc_params = bp()
        grp_sc_clause = _date_clause("q.quiz_date", date_from, date_to, grp_sc_params)
        grp_raw = conn.execute(f"""
            SELECT g.id as gid, g.name as gname, gov.name as govname, q.quiz_date,
                   AVG(qs.score*100.0/q.max_score) as avg_pct
            FROM quiz_scores qs
            JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            JOIN students s ON s.id=qs.student_id AND s.is_active=1
            JOIN groups g ON g.id=s.group_id
            JOIN governorates gov ON gov.id=g.governorate_id
            WHERE g.stage_id=?{gov_sql}{grp_sc_clause}
            GROUP BY g.id, q.id
            ORDER BY q.quiz_date ASC, q.id ASC
        """, grp_sc_params).fetchall()

        grp_scores = defaultdict(list)
        grp_meta = {}
        for r in grp_raw:
            grp_scores[r["gid"]].append(r["avg_pct"])
            if r["gid"] not in grp_meta:
                grp_meta[r["gid"]] = {"id": r["gid"], "name": r["gname"], "governorate_name": r["govname"]}

        # جمع بيانات الحضور للمجموعات
        grp_att_params = bp()
        grp_att_clause = _date_clause("a.session_date", date_from, date_to, grp_att_params)
        grp_att_rows = conn.execute(f"""
            SELECT g.id as gid, g.name as gname, gov.name as govname,
                   SUM(CASE WHEN a.status IN ('present','late') THEN 1 ELSE 0 END)*100.0/COUNT(*) as rate,
                   COUNT(*) as total
            FROM attendance a
            JOIN students s ON s.id=a.student_id AND s.is_active=1
            JOIN groups g ON g.id=s.group_id
            JOIN governorates gov ON gov.id=g.governorate_id
            WHERE g.stage_id=?{gov_sql}{grp_att_clause}
            GROUP BY g.id HAVING total >= 1
        """, grp_att_params).fetchall()

        # نسبة النجاح للمجموعات (≥ threshold)
        grp_pass_params = bp()
        grp_pass_clause = _date_clause("q.quiz_date", date_from, date_to, grp_pass_params)
        grp_pass_rows = conn.execute(f"""
            SELECT g.id as gid,
                   SUM(CASE WHEN qs.score*100.0/q.max_score >= ? THEN 1 ELSE 0 END)*100.0/COUNT(*) as pass_rate,
                   COUNT(*) as total
            FROM quiz_scores qs
            JOIN quizzes q ON q.id=qs.quiz_id AND q.max_score>0
            JOIN students s ON s.id=qs.student_id AND s.is_active=1
            JOIN groups g ON g.id=s.group_id
            WHERE g.stage_id=?{gov_sql}{grp_pass_clause}
            GROUP BY g.id HAVING total >= 1
        """, [pass_threshold] + grp_pass_params).fetchall()
        pass_by_gid = {r["gid"]: round(r["pass_rate"], 1) for r in grp_pass_rows}

        # ترتيب المجموعات — التحسن
        grp_improvement = []
        for gid, scores in grp_scores.items():
            if len(scores) < 2: continue
            half = len(scores) // 2
            first_avg = sum(scores[:half]) / half
            second_avg = sum(scores[half:]) / (len(scores) - half)
            delta = second_avg - first_avg
            m = grp_meta[gid]
            grp_improvement.append({**m, "value": round(delta, 1),
                "detail": f"من {round(first_avg,1)}% ← {round(second_avg,1)}%", "quizzes": len(scores)})
        grp_improvement_rank = sorted(grp_improvement, key=lambda x: x["value"], reverse=True)[:15]

        # ترتيب المجموعات — الاستقرار (أقل انحراف معياري = أكثر استقرارًا)
        grp_stability = []
        for gid, scores in grp_scores.items():
            if len(scores) < 2: continue
            std = _stddev(scores)
            avg = sum(scores) / len(scores)
            m = grp_meta[gid]
            grp_stability.append({**m, "value": round(avg, 1), "stability_std": round(std, 1),
                "detail": f"انحراف ±{round(std,1)} عن {round(avg,1)}%", "quizzes": len(scores)})
        grp_stability_rank = sorted(grp_stability, key=lambda x: x["stability_std"])[:15]

        # ترتيب المجموعات — نسبة النجاح
        grp_pass_rank = []
        for r in grp_att_rows:  # استخدم نفس المجموعات اللي عندها بيانات
            gid = r["gid"]
            m = grp_meta.get(gid, {"id": gid, "name": r["gname"], "governorate_name": r["govname"]})
            if gid in pass_by_gid:
                grp_pass_rank.append({**m, "value": pass_by_gid[gid],
                    "detail": f"نسبة الطلاب فوق {pass_threshold}%"})
        grp_pass_rank = sorted(grp_pass_rank, key=lambda x: x["value"], reverse=True)[:15]

        # ترتيب المجموعات — أقل غياب (= أعلى حضور)
        grp_att_rank = sorted([{
            **grp_meta.get(r["gid"], {"id": r["gid"], "name": r["gname"], "governorate_name": r["govname"]}),
            "value": round(r["rate"], 1), "detail": f"{r['total']} سجل حضور"
        } for r in grp_att_rows], key=lambda x: x["value"], reverse=True)[:15]

        return {
            "stage_id": stage_id,
            "pass_threshold": pass_threshold,
            "students": {
                "by_exams": exam_rank,
                "by_attendance": att_rank,
                "by_homework": hw_rank,
                "by_improvement": improvement_rank,
                "by_commitment": commitment_rank,
            },
            "groups": {
                "by_improvement": grp_improvement_rank,
                "by_stability": grp_stability_rank,
                "by_pass_rate": grp_pass_rank,
                "by_attendance": grp_att_rank,
            }
        }


# ---------------------------------------------------------------------------
# تشغيل السيرفر مباشرة
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # ملحوظة: reload=True اتشال لأنه للتطوير بس - مينفعش يتشغل بيه في الإنتاج
    uvicorn.run("backend:app", host="0.0.0.0", port=port)
