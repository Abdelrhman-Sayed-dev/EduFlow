# -*- coding: utf-8 -*-
"""
student_cards.py
منطق توليد ملف PDF لبطاقات الطلاب (لوحة تحكم الأدمن فقط).
مسؤول عن: ترتيب البطاقات في Grid احترافي على صفحات A4، علامات القص،
واستدعاء الـ Template المختار لرسم كل بطاقة. لا تعديل هنا مطلوب عند إضافة
Template جديد - فقط أضفه في card_templates.TEMPLATES.
"""
import io
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdfcanvas

from card_templates import TEMPLATES, draw_crop_marks, register_fonts

PAGE_W, PAGE_H = A4  # نقاط (points) - 595.27 x 841.89 تقريباً

MARGIN = 24          # هامش الصفحة بالنقطة
GUTTER = 20          # المسافة بين البطاقات (كافية عشان علامات القص ما تتقاطعش)
CROP_MARGIN = 14     # مساحة إضافية حوالين كل بطاقة عشان علامات القص متتقطعش مع الكارت جنبها


def _grid_for_template(template_key: str):
    """يحسب عدد الأعمدة/الصفوف ومقاس كل بطاقة حسب عدد الكروت في الصفحة"""
    cards_per_page = TEMPLATES[template_key]["cards_per_page"]
    if cards_per_page == 8:
        cols, rows = 2, 4
    elif cards_per_page == 10:
        cols, rows = 2, 5
    else:
        cols, rows = 2, 4

    usable_w = PAGE_W - 2 * MARGIN
    usable_h = PAGE_H - 2 * MARGIN
    card_w = (usable_w - (cols - 1) * GUTTER) / cols
    card_h = (usable_h - (rows - 1) * GUTTER) / rows
    return cols, rows, card_w, card_h


def generate_student_cards_pdf(students: list, template_key: str, brand: dict) -> bytes:
    """
    students: قايمة dict فيها full_name, attendance_code, access_code, group_name لكل طالب
    template_key: "A" / "B" / "C"
    brand: بيانات ثابتة (اسم الدكتور، المنصة، المادة، العام الدراسي)
    يرجع: (pdf_bytes, page_count)
    """
    if template_key not in TEMPLATES:
        template_key = "A"
    register_fonts()

    draw_fn = TEMPLATES[template_key]["fn"]
    cols, rows, card_w, card_h = _grid_for_template(template_key)
    per_page = cols * rows

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)
    c.setTitle("بطاقات الطلاب - EduFlow")

    total = len(students)
    page_count = 0
    for i, student in enumerate(students):
        pos_in_page = i % per_page
        if pos_in_page == 0:
            if i > 0:
                c.showPage()
            page_count += 1

        col = pos_in_page % cols
        row = pos_in_page // cols

        x = MARGIN + col * (card_w + GUTTER)
        # نبدأ من أعلى الصفحة نزولاً
        y = PAGE_H - MARGIN - (row + 1) * card_h - row * GUTTER

        draw_fn(c, x, y, card_w, card_h, student, brand)
        draw_crop_marks(c, x, y, card_w, card_h)

    if total == 0:
        # صفحة فاضية برسالة بسيطة بدل ملف تالف لو مفيش طلاب مطابقين للفلتر
        c.setFont("Helvetica", 12)
        c.drawCentredString(PAGE_W / 2, PAGE_H / 2, "No students matched the selected filter")
        page_count = 1

    c.save()
    buf.seek(0)
    return buf.read(), page_count


# ---------------------------------------------------------------------------
# Preview صغير لكل Template - بيتولد مرة واحدة بس ويتخزن في الذاكرة (Cache)
# عشان الأدمن يشوف شكل التصميم قبل ما يختاره، من غير ما نولّد صورة في كل طلب
# ---------------------------------------------------------------------------
_PREVIEW_CACHE = {}

_SAMPLE_STUDENT = {
    "full_name": "أحمد محمد سعيد",
    "attendance_code": "4587",
    "access_code": "ENT-9723",
    "group_name": "مجموعة 2 - ثانوي",
}


def get_template_preview_png(template_key: str, brand: dict) -> bytes:
    """يرجع صورة PNG صغيرة (كارت واحد بس) لمعاينة التصميم - بتتخزن في الذاكرة أول مرة"""
    if template_key in _PREVIEW_CACHE:
        return _PREVIEW_CACHE[template_key]

    if template_key not in TEMPLATES:
        raise ValueError("template not found")

    import pypdfium2 as pdfium  # مكتبة خفيفة، مستخدمة هنا بس (مش في توليد الـ PDF الأساسي)

    draw_fn = TEMPLATES[template_key]["fn"]
    _, _, card_w, card_h = _grid_for_template(template_key)

    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=(card_w + 20, card_h + 20))
    draw_fn(c, 10, 10, card_w, card_h, _SAMPLE_STUDENT, brand)
    c.save()
    buf.seek(0)

    pdf = pdfium.PdfDocument(buf.read())
    page = pdf[0]
    bitmap = page.render(scale=2.4)
    pil_img = bitmap.to_pil()
    out = io.BytesIO()
    pil_img.save(out, format="PNG")
    png_bytes = out.getvalue()
    _PREVIEW_CACHE[template_key] = png_bytes
    return png_bytes
