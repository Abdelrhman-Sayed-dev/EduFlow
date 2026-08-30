"""
fix_duplicate_sessions.py
==========================
سكريبت لمرة واحدة لتصحيح مشكلة "رقم الحصة بيتكرر بتاريخ مختلف" (نفس رقم
الحصة، مثلاً 1، بيظهر أكتر من مرة بتواريخ مختلفة لنفس المجموعة، بسبب إن
حد نسي يغيّر رقم الحصة يدويًا لما أخد غياب حصة جديدة).

اللي بيعمله:
  لكل مجموعة، بيجمع كل التواريخ الحقيقية اللي اتسجل فيها أي نشاط (حضور /
  تفاعل / واجب / كويز / صور سبورة) ويرتبهم زمنيًا، وبعدين بيرقمهم من جديد
  بالترتيب: 1, 2, 3... من غير تكرار، وبيحدّث كل الجداول المرتبطة (الحضور،
  التفاعل، الواجبات، صور السبورة، الكويزات) عشان تفضل متزامنة مع بعضها.

  فيديوهات المجموعة (video_group_links) معندهاش تاريخ خاص بيها، فبيتحدث
  رقمها بس لو الرقم القديم مش متلخبط (يعني معروف بيقصد تاريخ واحد بس)،
  غير كده بيتسجل في التقرير كـ"يحتاج مراجعة يدوية" ومايتلمسش خالص.

الاستخدام:
    python fix_duplicate_sessions.py            # وضع المعاينة (Dry run) - يطبع التقرير بس، مايغيرش حاجة
    python fix_duplicate_sessions.py --apply     # ينفذ التغييرات فعليًا (بعد ما ياخد نسخة احتياطية أول حاجة)
    python fix_duplicate_sessions.py --apply --group-id 5   # يصلح مجموعة واحدة بس

ملاحظات أمان:
  - قبل أي تنفيذ فعلي (--apply)، بياخد نسخة احتياطية تلقائية من قاعدة
    البيانات في backups/ بتاريخ ووقت التشغيل، بنفس أسلوب backup.py.
  - من غير --apply السكريبت بس بيطبع اللي هيحصل، مايعملش أي تعديل.
  - شغّله في وقت هدوء (مفيش حد بياخد غياب في نفس اللحظة) لتفادي أي تعارض.
"""

import os
import sys
import shutil
import sqlite3
import argparse
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_NAME = os.path.join(DATA_DIR, "teacher_system.db")
BACKUPS_DIR = os.path.join(DATA_DIR, "backups")

# الجداول اللي ليها تاريخ خاص بيها ونقدر نطابقها بالتاريخ مباشرة
DATED_TABLES = [
    # (اسم الجدول, عمود المجموعة, عمود التاريخ, هل محتاج JOIN مع students)
    ("participation", "group_id", "session_date", False),
    ("homework", "group_id", "session_date", False),
    ("board_images", "group_id", "session_date", False),
    ("quizzes", "group_id", "quiz_date", False),
    # مديونيات الغياب (Business Rule الغياب) - نفس مفتاح جدول attendance
    # بالظبط (طالب + تاريخ + رقم حصة)، فلازم تترقّم مع باقي الجداول عشان
    # تفضل مطابقة لسجل الحضور اللي اتولدت منه
    ("absence_debts", "group_id", "session_date", False),
]


def backup_db():
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = os.path.join(BACKUPS_DIR, f"pre_session_fix_{stamp}.db")
    shutil.copy2(DB_NAME, target)
    print(f"✅ اتاخدت نسخة احتياطية قبل التعديل: {target}")
    return target


def get_all_group_ids(conn, only_group_id=None):
    if only_group_id:
        return [only_group_id]
    rows = conn.execute("SELECT id FROM groups ORDER BY id").fetchall()
    return [r["id"] for r in rows]


def collect_dates_for_group(conn, group_id):
    """بيرجع dict: {date: set(old_numbers used on this date across any table)}"""
    date_to_numbers = {}

    def add(date, number):
        if not date or number is None:
            return
        date_to_numbers.setdefault(date, set()).add(number)

    # الحضور - مربوط بالطالب مش بالمجموعة مباشرة
    rows = conn.execute(
        """SELECT DISTINCT a.session_date, a.session_number
           FROM attendance a JOIN students s ON s.id = a.student_id
           WHERE s.group_id=?""",
        (group_id,),
    ).fetchall()
    for r in rows:
        add(r["session_date"], r["session_number"])

    for table, gcol, dcol, _ in DATED_TABLES:
        rows = conn.execute(
            f"SELECT DISTINCT {dcol} as d, session_number as n FROM {table} WHERE {gcol}=?",
            (group_id,),
        ).fetchall()
        for r in rows:
            add(r["d"], r["n"])

    return date_to_numbers


def collect_old_number_to_dates(date_to_numbers):
    """بيعكس الماب: لكل رقم قديم، إيه التواريخ اللي اتسجل بيها (لاكتشاف الالتباس)"""
    number_to_dates = {}
    for date, numbers in date_to_numbers.items():
        for n in numbers:
            number_to_dates.setdefault(n, set()).add(date)
    return number_to_dates


def plan_group(conn, group_id):
    date_to_numbers = collect_dates_for_group(conn, group_id)
    if not date_to_numbers:
        return None

    sorted_dates = sorted(date_to_numbers.keys())
    new_number_by_date = {d: i + 1 for i, d in enumerate(sorted_dates)}

    number_to_dates = collect_old_number_to_dates(date_to_numbers)
    # الأرقام القديمة اللي كانت متلخبطة (نفس الرقم استُخدم لأكتر من تاريخ حقيقي)
    ambiguous_numbers = {n: dates for n, dates in number_to_dates.items() if len(dates) > 1}
    # الأرقام القديمة الآمنة (بتشاور على تاريخ واحد بس) - نقدر نستخدمها لتحديث
    # الجداول اللي مالهاش تاريخ خاص بيها زي فيديوهات المجموعة
    safe_old_to_new = {
        n: new_number_by_date[next(iter(dates))]
        for n, dates in number_to_dates.items() if len(dates) == 1
    }

    return {
        "group_id": group_id,
        "sorted_dates": sorted_dates,
        "new_number_by_date": new_number_by_date,
        "ambiguous_numbers": ambiguous_numbers,
        "safe_old_to_new": safe_old_to_new,
    }


def apply_plan(conn, plan, report_lines):
    group_id = plan["group_id"]

    for date, new_number in plan["new_number_by_date"].items():
        conn.execute(
            """UPDATE attendance SET session_number=?
               WHERE session_date=? AND student_id IN (SELECT id FROM students WHERE group_id=?)""",
            (new_number, date, group_id),
        )
        for table, gcol, dcol, _ in DATED_TABLES:
            try:
                conn.execute(
                    f"UPDATE {table} SET session_number=? WHERE {dcol}=? AND {gcol}=?",
                    (new_number, date, group_id),
                )
            except sqlite3.IntegrityError as e:
                report_lines.append(
                    f"  ⚠️ تعارض أثناء تحديث {table} للتاريخ {date} (مجموعة {group_id}): {e} - محتاج مراجعة يدوية"
                )

    # فيديوهات المجموعة - تحديث بس للأرقام غير الملتبسة
    for old_number, new_number in plan["safe_old_to_new"].items():
        if old_number == new_number:
            continue
        conn.execute(
            "UPDATE video_group_links SET session_number=? WHERE group_id=? AND session_number=?",
            (new_number, group_id, old_number),
        )


def main():
    parser = argparse.ArgumentParser(description="تصحيح تكرار أرقام الحصص لكل مجموعة")
    parser.add_argument("--apply", action="store_true", help="نفّذ التعديلات فعليًا (غير كده معاينة بس)")
    parser.add_argument("--group-id", type=int, default=None, help="صلّح مجموعة واحدة بس بالـ id بتاعها")
    args = parser.parse_args()

    if not os.path.exists(DB_NAME):
        print(f"❌ ملف قاعدة البيانات مش موجود: {DB_NAME}")
        sys.exit(1)

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    group_ids = get_all_group_ids(conn, args.group_id)
    report_lines = []
    total_changed_groups = 0

    for group_id in group_ids:
        plan = plan_group(conn, group_id)
        if not plan:
            continue

        group_row = conn.execute("SELECT name FROM groups WHERE id=?", (group_id,)).fetchone()
        group_name = group_row["name"] if group_row else f"#{group_id}"

        # هل فيه أي حاجة هتتغير أصلاً؟ (يعني في الأقل رقم واحد قديم مش مطابق للرقم الجديد)
        old_number_to_dates = collect_old_number_to_dates(collect_dates_for_group(conn, group_id))
        needs_change = any(
            new_n != old_n
            for old_n, dates in old_number_to_dates.items()
            for d in dates
            for new_n in [plan["new_number_by_date"][d]]
        )
        if not needs_change and not plan["ambiguous_numbers"]:
            continue

        total_changed_groups += 1
        report_lines.append(f"\n📚 مجموعة: {group_name} (id={group_id})")
        report_lines.append(f"   عدد الحصص الحقيقية المكتشفة: {len(plan['sorted_dates'])}")
        for date in plan["sorted_dates"]:
            report_lines.append(f"     - {date}  →  حصة رقم {plan['new_number_by_date'][date]}")

        if plan["ambiguous_numbers"]:
            report_lines.append("   ⚠️ أرقام كانت متكررة على أكتر من تاريخ (هتتصلح حسب الجداول اللي فيها تاريخ):")
            for old_n, dates in plan["ambiguous_numbers"].items():
                report_lines.append(f"     - رقم {old_n} كان مستخدم في: {', '.join(sorted(dates))}")
            report_lines.append(
                "     ملحوظة: فيديوهات المجموعة (لو فيه) المرتبطة بالأرقام دي مش هتتحدث تلقائي "
                "لأنها معندهاش تاريخ يوضح تقصد أنهي حصة بالظبط - محتاجة مراجعة يدوية."
            )

        if args.apply:
            apply_plan(conn, plan, report_lines)

    print("\n".join(report_lines) if report_lines else "مفيش أي تكرار في أرقام الحصص - كل حاجة سليمة ✅")

    if not group_ids or total_changed_groups == 0:
        conn.close()
        return

    if args.apply:
        backup_path = None  # هنعمل الباك أب قبل الكوميت فعليًا - شوف تحت
        conn.rollback()  # نلغي أي تعديل حصل في الاتصال الحالي، ونعمل باك أب الأول
        conn.close()
        backup_db()
        # دلوقتي ننفذ فعليًا بعد ما اطمأنينا إن فيه نسخة احتياطية
        conn = sqlite3.connect(DB_NAME)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        for group_id in group_ids:
            plan = plan_group(conn, group_id)
            if plan:
                apply_plan(conn, plan, [])
        conn.commit()
        conn.close()
        print(f"\n✅ تم تنفيذ التصحيح فعليًا على {total_changed_groups} مجموعة.")
    else:
        conn.close()
        print(f"\nℹ️ ده وضع المعاينة بس (مفيش حاجة اتغيرت). لو الكلام فوق صح، شغّل السكريبت تاني بـ --apply عشان ينفّذ فعليًا.")


if __name__ == "__main__":
    main()
