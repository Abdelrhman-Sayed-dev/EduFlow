"""
backup.py
سكريبت بسيط لعمل نسخة احتياطية من قاعدة البيانات وملفات الصور المرفوعة.
شغّله بشكل دوري (مثلاً Cron Job يوميًا) عشان تضمن إنك مش هتخسر بيانات.

تشغيل:
    python backup.py

بينتج مجلد باسم backups/ فيه نسخة بتاريخ ووقت النسخ.
"""

import shutil
import os
from datetime import datetime

DB_NAME = "teacher_system.db"
UPLOADS_DIR = "uploads"
BACKUPS_DIR = "backups"


def run_backup():
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target_dir = os.path.join(BACKUPS_DIR, stamp)
    os.makedirs(target_dir, exist_ok=True)

    if os.path.exists(DB_NAME):
        shutil.copy2(DB_NAME, os.path.join(target_dir, DB_NAME))
        print(f"تم نسخ قاعدة البيانات -> {target_dir}/{DB_NAME}")
    else:
        print("تحذير: ملف قاعدة البيانات غير موجود")

    if os.path.isdir(UPLOADS_DIR):
        shutil.copytree(UPLOADS_DIR, os.path.join(target_dir, UPLOADS_DIR))
        print(f"تم نسخ مجلد الصور -> {target_dir}/{UPLOADS_DIR}")

    print("تمت النسخة الاحتياطية بنجاح ✅")


if __name__ == "__main__":
    run_backup()
