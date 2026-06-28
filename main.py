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
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

from database import (
    get_connection, init_db, hash_password, verify_password, gen_token,
    gen_access_code, gen_temp_password, session_expiry, cleanup_expired_sessions
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

# مجلد رفع الصور (سبورة الحصص) - بيتم تخزين الصور كملفات على الـ disk
# مش base64 جوه قاعدة البيانات، عشان الداتابيز ما تكبرش وتبقى بطيئة
UPLOADS_DIR = os.environ.get("UPLOADS_DIR", os.path.join(os.environ.get("DATA_DIR", "."), "uploads"))
BOARD_IMAGES_DIR = os.path.join(UPLOADS_DIR, "board_images")
os.makedirs(BOARD_IMAGES_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


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
    name: str
    stage_id: int
    governorate_id: int
    notes: Optional[str] = None
    session_price: Optional[float] = None
    supervisor_id: Optional[int] = None
    schedule_slots: Optional[list[ScheduleSlotIn]] = None  # مواعيد المجموعة (يوم + وقت)


class StudentIn(BaseModel):
    full_name: str
    phone: Optional[str] = None
    parent_phone: Optional[str] = None
    group_id: int
    notes: Optional[str] = None


class QuizIn(BaseModel):
    title: str
    description: Optional[str] = None
    quiz_date: Optional[str] = None
    max_score: float = 100
    group_id: Optional[int] = None  # لو فاضي يبقى الكويز عام لكل المجموعات


class QuizScoreIn(BaseModel):
    student_id: int
    quiz_id: int
    score: float
    notes: Optional[str] = None


class AttendanceIn(BaseModel):
    student_id: int
    session_date: str
    status: str  # present / absent / late / excused
    notes: Optional[str] = None


class AttendanceCodeIn(BaseModel):
    access_code: str
    session_date: str
    status: str


class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class StudentLoginIn(BaseModel):
    access_code: str


class CodeLoginIn(BaseModel):
    """تسجيل دخول موحّد بالكود - يصلح للطالب أو المشرف أو المدرس"""
    access_code: str


class UserIn(BaseModel):
    username: str
    password: Optional[str] = None  # لو فاضي، النظام يولّد كلمة مرور تلقائية ويرجّعها
    full_name: str
    phone: Optional[str] = None
    role: str = "supervisor"  # supervisor أو teacher (الأدمن مينضافش من هنا)
    governorate_id: Optional[int] = None  # للمشرف بس - المحافظة المسؤول عنها


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


class UserUpdateIn(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    governorate_id: Optional[int] = None


class ScheduleIn(BaseModel):
    day_of_week: str
    start_time: str
    end_time: Optional[str] = None
    group_id: Optional[int] = None
    title: Optional[str] = None
    notes: Optional[str] = None


class BoardImageIn(BaseModel):
    group_id: int
    session_number: int
    session_date: Optional[str] = None
    image_data: str  # base64 data url
    caption: Optional[str] = None


class AssignSupervisorIn(BaseModel):
    supervisor_id: Optional[int] = None  # None = شيل المشرف من المجموعة


class BehaviorNoteIn(BaseModel):
    student_id: int
    note: str
    note_type: str = "neutral"  # positive / negative / neutral


class PaymentIn(BaseModel):
    student_id: int
    month: str  # صيغة YYYY-MM
    amount: Optional[float] = None
    is_paid: bool = False
    paid_date: Optional[str] = None
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# نظام تسجيل الدخول والصلاحيات
# ---------------------------------------------------------------------------

def get_current_session(authorization: Optional[str] = Header(None)):
    """يقرأ التوكن من الهيدر Authorization: Bearer <token> ويتأكد إنه لسه صالح"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="لازم تسجل دخول الأول")
    token = authorization.split(" ", 1)[1].strip()
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
            return {
                "type": "student", "id": student["id"], "role": "student",
                "full_name": student["full_name"], "group_id": student["group_id"]
            }


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


# ---------------------------------------------------------------------------
# الصفحة الرئيسية (الواجهة)
# ---------------------------------------------------------------------------

@app.get("/")
def serve_frontend():
    return FileResponse("frontend.html")


# ---------------------------------------------------------------------------
# تسجيل الدخول
# ---------------------------------------------------------------------------

@app.post("/api/auth/login")
def login(data: LoginIn):
    """تسجيل دخول الأدمن / المدرس / المشرف"""
    with get_connection() as conn:
        cleanup_expired_sessions(conn)
        user = conn.execute(
            "SELECT * FROM users WHERE username=?", (data.username,)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غلط")

        ok, needs_upgrade = verify_password(data.password, user["password_hash"])
        if not ok:
            raise HTTPException(status_code=401, detail="اسم المستخدم أو كلمة المرور غلط")
        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="الحساب موقوف، كلم الأدمن")

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
def login_with_code(data: CodeLoginIn):
    """
    تسجيل دخول موحّد بالكود - يصلح للطالب أو المشرف أو المدرس.
    بيدور على الكود في جدول الطلاب الأول، ولو مش موجود يدور في جدول المستخدمين (مشرف/مدرس).
    """
    code = data.access_code.strip()
    with get_connection() as conn:
        cleanup_expired_sessions(conn)

        student = conn.execute("SELECT * FROM students WHERE access_code=?", (code,)).fetchone()
        if student:
            if not student["is_active"]:
                raise HTTPException(status_code=403, detail="الحساب موقوف، كلم المشرف")
            token = gen_token()
            conn.execute(
                "INSERT INTO sessions (token, user_type, user_id, expires_at) VALUES (?, 'student', ?, ?)",
                (token, student["id"], session_expiry())
            )
            group = conn.execute("SELECT * FROM groups WHERE id=?", (student["group_id"],)).fetchone()
            return {
                "token": token, "role": "student", "full_name": student["full_name"],
                "id": student["id"], "group_id": student["group_id"],
                "group_name": group["name"] if group else None
            }

        user = conn.execute("SELECT * FROM users WHERE access_code=?", (code,)).fetchone()
        if user:
            if not user["is_active"]:
                raise HTTPException(status_code=403, detail="الحساب موقوف، كلم الأدمن")
            token = gen_token()
            conn.execute(
                "INSERT INTO sessions (token, user_type, user_id, expires_at) VALUES (?, 'user', ?, ?)",
                (token, user["id"], session_expiry())
            )
            return {
                "token": token, "role": user["role"], "full_name": user["full_name"],
                "username": user["username"], "id": user["id"]
            }

        raise HTTPException(status_code=401, detail="كود الدخول غلط")


@app.post("/api/auth/student-login")
def student_login(data: StudentLoginIn):
    """تسجيل دخول الطالب بكود الدخول الخاص بيه (للتوافق مع نسخ قديمة - استخدم /login-code الأحدث)"""
    return login_with_code(CodeLoginIn(access_code=data.access_code))


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
# المراحل - Stages (إعدادي / ثانوي) - ثابتة، قراءة فقط
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
        SELECT g.id, g.name, g.notes, g.session_price, g.stage_id, g.governorate_id,
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


@app.post("/api/groups")
def add_group(group: GroupIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO groups (name, stage_id, governorate_id, notes, session_price, supervisor_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (group.name, group.stage_id, group.governorate_id, group.notes,
                 group.session_price, group.supervisor_id)
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
            """UPDATE groups SET name=?, stage_id=?, governorate_id=?, notes=?, session_price=?, supervisor_id=?
               WHERE id=?""",
            (group.name, group.stage_id, group.governorate_id, group.notes,
             group.session_price, group.supervisor_id, group_id)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
        return {"message": "تم تعديل المجموعة"}


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

@app.get("/api/students")
def get_students(group_id: Optional[int] = None, stage_id: Optional[int] = None,
                  governorate_id: Optional[int] = None, session=Depends(get_current_session)):
    """جلب الطلاب - المشرف يشوف طلاب مجموعاته بس، والطالب يشوف بياناته بس"""
    query = """
        SELECT s.id, s.full_name, s.phone, s.parent_phone, s.notes, s.group_id, s.access_code, s.is_active,
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
        return result


@app.post("/api/students")
def add_student(student: StudentIn, session=Depends(require_roles("admin"))):
    with get_connection() as conn:
        code = gen_access_code()
        # تأكد إن الكود فريد (احتمالية تكرار شبه معدومة بس للأمان)
        while conn.execute("SELECT id FROM students WHERE access_code=?", (code,)).fetchone():
            code = gen_access_code()
        cur = conn.execute(
            """INSERT INTO students (full_name, phone, parent_phone, group_id, notes, access_code)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (student.full_name, student.phone, student.parent_phone,
             student.group_id, student.notes, code)
        )
        return {"id": cur.lastrowid, "access_code": code, "message": "تم إضافة الطالب بنجاح"}


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
    """توليد كود دخول جديد للطالب (لو الكود ضاع منه مثلاً)"""
    with get_connection() as conn:
        code = gen_access_code()
        while conn.execute("SELECT id FROM students WHERE access_code=?", (code,)).fetchone():
            code = gen_access_code()
        result = conn.execute("UPDATE students SET access_code=? WHERE id=?", (code, student_id))
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        return {"access_code": code, "message": "تم توليد كود جديد"}


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
def get_users(role: Optional[str] = None, session=Depends(require_roles("admin"))):
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
    if user.role not in ("supervisor", "teacher"):
        raise HTTPException(status_code=400, detail="الدور المسموح بيه هنا: مشرف أو مدرس بس")
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
        prefix = "SUP" if user.role == "supervisor" else "TCH"
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
            SELECT sc.*, g.name as group_name
            FROM teacher_schedule sc
            LEFT JOIN groups g ON g.id = sc.group_id
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

        rows = conn.execute(
            """SELECT bi.*, u.full_name as uploaded_by_name
               FROM board_images bi
               LEFT JOIN users u ON u.id = bi.uploaded_by
               WHERE bi.group_id=?
               ORDER BY bi.session_number DESC, bi.created_at DESC""",
            (group_id,)
        ).fetchall()
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
# الكويزات - Quizzes
# ---------------------------------------------------------------------------

@app.get("/api/quizzes")
def get_quizzes(group_id: Optional[int] = None, session=Depends(get_current_session)):
    query = """
        SELECT q.*, g.name as group_name
        FROM quizzes q
        LEFT JOIN groups g ON g.id = q.group_id
        WHERE 1=1
    """
    params = []
    if group_id:
        query += " AND (q.group_id = ? OR q.group_id IS NULL)"
        params.append(group_id)

    if session["role"] == "supervisor":
        query += " AND (q.group_id IS NULL OR q.group_id IN (SELECT id FROM groups WHERE supervisor_id=?))"
        params.append(session["id"])
    elif session["role"] == "student":
        query += " AND (q.group_id IS NULL OR q.group_id = ?)"
        params.append(session["group_id"])

    query += " ORDER BY q.quiz_date DESC, q.id DESC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


@app.post("/api/quizzes")
def add_quiz(quiz: QuizIn, session=Depends(require_roles("admin", "teacher", "supervisor"))):
    with get_connection() as conn:
        if session["role"] == "supervisor":
            if not quiz.group_id:
                raise HTTPException(status_code=403, detail="المشرف لازم يحدد مجموعته")
            assert_supervisor_owns_group(conn, session, quiz.group_id)
        cur = conn.execute(
            "INSERT INTO quizzes (title, description, quiz_date, max_score, group_id) VALUES (?, ?, ?, ?, ?)",
            (quiz.title, quiz.description, quiz.quiz_date, quiz.max_score, quiz.group_id)
        )
        return {"id": cur.lastrowid, "message": "تم إضافة الكويز بنجاح"}


@app.delete("/api/quizzes/{quiz_id}")
def delete_quiz(quiz_id: int, session=Depends(require_roles("admin", "teacher", "supervisor"))):
    with get_connection() as conn:
        quiz = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if not quiz:
            raise HTTPException(status_code=404, detail="الكويز غير موجود")
        if session["role"] == "supervisor" and quiz["group_id"]:
            assert_supervisor_owns_group(conn, session, quiz["group_id"])
        conn.execute("DELETE FROM quizzes WHERE id=?", (quiz_id,))
        return {"message": "تم حذف الكويز"}


# ---------------------------------------------------------------------------
# درجات الكويزات - Quiz Scores
# ---------------------------------------------------------------------------

@app.get("/api/quizzes/{quiz_id}/scores")
def get_quiz_scores(quiz_id: int, session=Depends(get_current_session)):
    """
    جلب طلاب الكويز مع درجاتهم.
    لو الكويز مرتبط بمجموعة معينة، يظهر طلاب المجموعة بس.
    لو الكويز عام (group_id = NULL)، يظهر كل الطلاب (أو طلاب المشرف/الطالب بس حسب الدور).
    """
    with get_connection() as conn:
        quiz = conn.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        if not quiz:
            raise HTTPException(status_code=404, detail="الكويز غير موجود")

        if session["role"] == "supervisor" and quiz["group_id"]:
            assert_supervisor_owns_group(conn, session, quiz["group_id"])

        if quiz["group_id"]:
            rows = conn.execute("""
                SELECT s.id as student_id, s.full_name, qs.score, qs.notes, qs.id as score_id
                FROM students s
                LEFT JOIN quiz_scores qs ON qs.student_id = s.id AND qs.quiz_id = ?
                WHERE s.group_id = ?
                ORDER BY s.full_name
            """, (quiz_id, quiz["group_id"])).fetchall()
        else:
            base_query = """
                SELECT s.id as student_id, s.full_name, qs.score, qs.notes, qs.id as score_id, s.group_id
                FROM students s
                LEFT JOIN quiz_scores qs ON qs.student_id = s.id AND qs.quiz_id = ?
                WHERE 1=1
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
def set_score(score: QuizScoreIn, session=Depends(require_roles("admin", "teacher", "supervisor"))):
    with get_connection() as conn:
        if session["role"] == "supervisor":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (score.student_id,)).fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="الطالب غير موجود")
            assert_supervisor_owns_group(conn, session, student["group_id"])

        conn.execute("""
            INSERT INTO quiz_scores (student_id, quiz_id, score, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, quiz_id)
            DO UPDATE SET score=excluded.score, notes=excluded.notes
        """, (score.student_id, score.quiz_id, score.score, score.notes))
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

        rows = conn.execute("""
            SELECT q.title, q.quiz_date, q.max_score, qs.score
            FROM quiz_scores qs
            JOIN quizzes q ON q.id = qs.quiz_id
            WHERE qs.student_id = ?
            ORDER BY q.quiz_date DESC
        """, (student_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# الحضور والغياب - Attendance
# ---------------------------------------------------------------------------

@app.get("/api/attendance/{session_date}")
def get_attendance_by_date(session_date: str, group_id: Optional[int] = None,
                            session=Depends(get_current_session)):
    """كل الطلاب (أو طلاب مجموعة معينة) مع حالة حضورهم في تاريخ معين"""
    with get_connection() as conn:
        if session["role"] == "supervisor" and group_id:
            assert_supervisor_owns_group(conn, session, group_id)

        query = """
            SELECT s.id as student_id, s.full_name, a.status, a.notes, a.id as attendance_id, s.group_id
            FROM students s
            LEFT JOIN attendance a ON a.student_id = s.id AND a.session_date = ?
            WHERE 1=1
        """
        params = [session_date]
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
def set_attendance(att: AttendanceIn, session=Depends(require_roles("admin", "teacher", "supervisor"))):
    with get_connection() as conn:
        if session["role"] == "supervisor":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (att.student_id,)).fetchone()
            if not student:
                raise HTTPException(status_code=404, detail="الطالب غير موجود")
            assert_supervisor_owns_group(conn, session, student["group_id"])

        conn.execute("""
            INSERT INTO attendance (student_id, session_date, status, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(student_id, session_date)
            DO UPDATE SET status=excluded.status, notes=excluded.notes
        """, (att.student_id, att.session_date, att.status, att.notes))
        return {"message": "تم حفظ الحضور"}


@app.get("/api/students/find-by-code")
def find_student_by_code(code: str, session=Depends(require_roles("admin", "teacher", "supervisor"))):
    """البحث عن طالب بكود الدخول الخاص بيه - يستخدم في تسجيل الحضور السريع"""
    with get_connection() as conn:
        student = conn.execute(
            """SELECT s.id, s.full_name, s.group_id, g.name as group_name
               FROM students s JOIN groups g ON g.id = s.group_id
               WHERE s.access_code = ?""",
            (code.strip(),)
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="مفيش طالب بالكود ده")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])
        return dict(student)


@app.post("/api/attendance/by-code")
def set_attendance_by_code(data: AttendanceCodeIn, session=Depends(require_roles("admin", "teacher", "supervisor"))):
    """تسجيل حضور سريع بكود الطالب - المشرف بيدوّر بالكود ويسجل الحالة على طول"""
    with get_connection() as conn:
        student = conn.execute(
            "SELECT id, full_name, group_id FROM students WHERE access_code=?",
            (data.access_code.strip(),)
        ).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="مفيش طالب بالكود ده")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])

        conn.execute("""
            INSERT INTO attendance (student_id, session_date, status)
            VALUES (?, ?, ?)
            ON CONFLICT(student_id, session_date)
            DO UPDATE SET status=excluded.status
        """, (student["id"], data.session_date, data.status))
        return {"message": "تم تسجيل الحضور", "student_name": student["full_name"], "student_id": student["id"]}


@app.get("/api/students/{student_id}/attendance")
def get_student_attendance(student_id: int, session=Depends(get_current_session)):
    with get_connection() as conn:
        if session["role"] == "student" and session["id"] != student_id:
            raise HTTPException(status_code=403, detail="تقدر تشوف حضورك بس")
        if session["role"] == "supervisor":
            student = conn.execute("SELECT group_id FROM students WHERE id=?", (student_id,)).fetchone()
            if student:
                assert_supervisor_owns_group(conn, session, student["group_id"])

        rows = conn.execute("""
            SELECT session_date, status, notes FROM attendance
            WHERE student_id = ? ORDER BY session_date DESC
        """, (student_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# الملاحظات السلوكية - Behavior Notes (المشرف بيكتبها، تظهر للمدرس والأدمن بس)
# ---------------------------------------------------------------------------

@app.get("/api/students/{student_id}/behavior-notes")
def get_behavior_notes(student_id: int, session=Depends(require_roles("admin", "teacher", "supervisor"))):
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
def add_behavior_note(data: BehaviorNoteIn, session=Depends(require_roles("admin", "teacher", "supervisor"))):
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
def delete_behavior_note(note_id: int, session=Depends(require_roles("admin", "teacher", "supervisor"))):
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

        rows = conn.execute(
            "SELECT * FROM payments WHERE student_id=? ORDER BY month DESC", (student_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/payments")
def get_payments(group_id: Optional[int] = None, month: Optional[str] = None,
                  session=Depends(require_roles("admin", "teacher", "supervisor"))):
    """جلب حالة المدفوعات لشهر معين، لمجموعة معينة أو كل الطلاب"""
    with get_connection() as conn:
        if session["role"] == "supervisor" and group_id:
            assert_supervisor_owns_group(conn, session, group_id)

        query = """
            SELECT s.id as student_id, s.full_name, s.group_id, g.name as group_name,
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
def set_payment(data: PaymentIn, session=Depends(require_roles("admin", "teacher", "supervisor"))):
    with get_connection() as conn:
        student = conn.execute("SELECT group_id FROM students WHERE id=?", (data.student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")
        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])

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
        """, (data.student_id, data.month, data.amount, int(data.is_paid), paid_date, data.notes))
        return {"message": "تم حفظ بيانات الدفع"}


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
            SELECT s.*, g.name as group_name, st.name as stage_name, gov.name as governorate_name
            FROM students s
            JOIN groups g ON g.id = s.group_id
            JOIN stages st ON st.id = g.stage_id
            JOIN governorates gov ON gov.id = g.governorate_id
            WHERE s.id = ?
        """, (student_id,)).fetchone()
        if not student:
            raise HTTPException(status_code=404, detail="الطالب غير موجود")

        if session["role"] == "supervisor":
            assert_supervisor_owns_group(conn, session, student["group_id"])
        elif session["role"] == "student":
            raise HTTPException(status_code=403, detail="التقرير الشهري متاح للمدرس والمشرف والأدمن بس")

        attendance_rows = conn.execute("""
            SELECT session_date, status FROM attendance
            WHERE student_id = ? AND session_date LIKE ?
            ORDER BY session_date
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
# تشغيل السيرفر مباشرة
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # ملحوظة: reload=True اتشال لأنه للتطوير بس - مينفعش يتشغل بيه في الإنتاج
    uvicorn.run("backend:app", host="0.0.0.0", port=port)
