# -*- coding: utf-8 -*-
"""
card_templates.py
محرك رسم بطاقات الطلاب - كل Template عبارة عن دالة بترسم كارت واحد جوه مستطيل
(x, y, w, h) على الـ canvas بتاع reportlab. عشان تضيف تصميم جديد مستقبلاً،
اعمل دالة جديدة بنفس التوقيع وسجّلها في TEMPLATES تحت من غير ما تلمس أي حاجة
تانية في منطق توليد الـ PDF.
"""
import os
import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# ---------------------------------------------------------------------------
# تسجيل الخطوط (عربي + إنجليزي) مرة واحدة بس
# ---------------------------------------------------------------------------
_FONTS_REGISTERED = False


def register_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont("Kufi-Regular", os.path.join(FONT_DIR, "NotoKufi-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Kufi-Bold", os.path.join(FONT_DIR, "NotoKufi-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("Kufi-SemiBold", os.path.join(FONT_DIR, "NotoKufi-SemiBold.ttf")))
    pdfmetrics.registerFont(TTFont("Cairo-Regular", os.path.join(FONT_DIR, "Cairo-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("Cairo-Bold", os.path.join(FONT_DIR, "Cairo-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("Cairo-ExtraBold", os.path.join(FONT_DIR, "Cairo-ExtraBold.ttf")))
    _FONTS_REGISTERED = True


def ar(text: str) -> str:
    """يجهّز أي نص عربي للرسم الصحيح (تشكيل الحروف + اتجاه RTL)"""
    if not text:
        return ""
    text = str(text)
    reshaped = arabic_reshaper.reshape(text)
    return get_display(reshaped)


# ---------------------------------------------------------------------------
# ألوان الهوية (نفس ألوان EduFlow: كحلي + دهبي)
# ---------------------------------------------------------------------------
NAVY = HexColor("#152A54")
NAVY_DARK = HexColor("#0E1D3A")
GOLD = HexColor("#C89B3C")
GOLD_LIGHT = HexColor("#E8C77A")
CREAM = HexColor("#FBFAF7")
TEXT_DARK = HexColor("#152A54")
TEXT_MUTED = HexColor("#8A8570")
LINE_LIGHT = HexColor("#E4DFD3")


def _rtext(c, x, y, text, font, size, color=TEXT_DARK, align="right"):
    """كتابة سطر عربي واحد (بعد التجهيز) - align: right/center/left"""
    c.setFont(font, size)
    c.setFillColor(color)
    txt = ar(text)
    if align == "right":
        c.drawRightString(x, y, txt)
    elif align == "center":
        c.drawCentredString(x, y, txt)
    else:
        c.drawString(x, y, txt)


def _etext(c, x, y, text, font, size, color=TEXT_DARK, align="center"):
    """كتابة سطر إنجليزي/أرقام (LTR عادي)"""
    c.setFont(font, size)
    c.setFillColor(color)
    if align == "center":
        c.drawCentredString(x, y, text)
    elif align == "right":
        c.drawRightString(x, y, text)
    else:
        c.drawString(x, y, text)


# ---------------------------------------------------------------------------
# TEMPLATE A - التصميم الكامل الفاخر (مطابق للديزاين الأصلي) - كحلي/دهبي
# مقاس مقترح: 9.2 سم × 5.7 سم (8 بطاقات في الصفحة)
# ---------------------------------------------------------------------------
def template_a(c, x, y, w, h, data, brand):
    register_fonts()

    # خلفية الكارت
    c.setFillColor(CREAM)
    c.roundRect(x, y, w, h, 4, fill=1, stroke=0)

    # إطار خفيف
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.roundRect(x + 0.7, y + 0.7, w - 1.4, h - 1.4, 3, fill=0, stroke=1)

    # مثلث كحلي أعلى اليسار (زاوية علوية) - مقاس صغير عشان ميدخلش في مساحة النصوص
    tri_w = w * 0.11
    tri_h = h * 0.17
    c.setFillColor(NAVY)
    p = c.beginPath()
    p.moveTo(x, y + h)
    p.lineTo(x + tri_w, y + h)
    p.lineTo(x, y + h - tri_h)
    p.close()
    c.drawPath(p, fill=1, stroke=0)

    # خط دهبي رفيع بالظبط على ضلع المثلث (مش مادّ برّه المثلث)
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.1)
    c.line(x + tri_w, y + h, x, y + h - tri_h)

    # مثلث كحلي أسفل اليمين (زاوية سفلية) - نفس المقاس الصغير
    p2 = c.beginPath()
    p2.moveTo(x + w, y)
    p2.lineTo(x + w - tri_w, y)
    p2.lineTo(x + w, y + tri_h)
    p2.close()
    c.drawPath(p2, fill=1, stroke=0)
    c.line(x + w - tri_w, y, x + w, y + tri_h)

    cx = x + w / 2
    top = y + h

    # اسم الدكتور + العنوان (بدون جملة المادة عشان الكارت يبقى أنضف)
    _rtext(c, cx, top - h * 0.095, brand["doctor_prefix"], "Kufi-SemiBold", h * 0.042, GOLD, "center")
    _rtext(c, cx, top - h * 0.25, brand["doctor_name"], "Kufi-Bold", h * 0.105, NAVY, "center")

    # اسم المنصة + شعارها (أعلى يمين الكارت بعيد عن المثلث)
    draw_platform_brand(c, brand["platform_name"], x + w - w * 0.06, top - h * 0.095,
                         h * 0.072, "Cairo-ExtraBold", NAVY, logo_r=h * 0.048, align="right",
                         gap=w * 0.018)

    # صندوق اسم الطالب (فراغ كافي بين تسمية "اسم الطالب" والاسم نفسه)
    label_y = top - h * 0.385
    value_y = top - h * 0.485
    box_top = top - h * 0.355
    box_bottom = top - h * 0.515
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.6)
    c.roundRect(x + w * 0.08, box_bottom, w * 0.84, box_top - box_bottom, 3, fill=0, stroke=1)
    _rtext(c, x + w * 0.88, label_y, "اسم الطالب", "Kufi-Regular", h * 0.036, GOLD, "right")
    _rtext(c, x + w * 0.88, value_y, data["full_name"], "Kufi-Bold", h * 0.072, NAVY, "right")

    # فاصل أفقي رفيع
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.line(x + w * 0.08, top - h * 0.565, x + w * 0.92, top - h * 0.565)

    # صف الأكواد (كود الحضور | كود الدخول) - فراغ كافي بين التسمية والكود الكبير تحتها
    codes_y1 = top - h * 0.645
    codes_y2 = top - h * 0.745
    # كود الحضور - يمين
    _rtext(c, x + w * 0.92, codes_y1, "كود الحضور", "Kufi-Regular", h * 0.040, GOLD, "right")
    _etext(c, x + w * 0.92, codes_y2, data["attendance_code"], "Cairo-Bold", h * 0.075, NAVY, "right")
    # كود الدخول - شمال
    _rtext(c, x + w * 0.46, codes_y1, "كود الدخول للمنصة", "Kufi-Regular", h * 0.040, GOLD, "right")
    _etext(c, x + w * 0.46, codes_y2, data["access_code"], "Cairo-Bold", h * 0.075, NAVY, "right")
    # فاصل رأسي بين الكودين
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.line(cx, codes_y2 - h * 0.015, cx, codes_y1 + h * 0.03)

    _rtext(c, cx, top - h * 0.81, "كود تسجيل الدخول الخاص بك - لا تشاركه مع أي شخص",
           "Kufi-Regular", h * 0.028, TEXT_MUTED, "center")

    # فوتر: المجموعة | العام الدراسي
    footer_y = y + h * 0.10
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.6)
    c.line(x + w * 0.08, footer_y + h * 0.06, x + w * 0.92, footer_y + h * 0.06)
    _rtext(c, x + w * 0.92, footer_y, f"المجموعة: {data.get('group_name','')}", "Kufi-Regular", h * 0.036, NAVY, "right")
    _etext(c, x + w * 0.08, footer_y, brand["academic_year"], "Cairo-Bold", h * 0.036, NAVY, "left")


# ---------------------------------------------------------------------------
# TEMPLATE B - تصميم مبسط أفقي (بار كحلي علوي) - 10 بطاقات في الصفحة
# ---------------------------------------------------------------------------
def template_b(c, x, y, w, h, data, brand):
    register_fonts()

    c.setFillColor(CREAM)
    c.roundRect(x, y, w, h, 3, fill=1, stroke=0)
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.roundRect(x, y, w, h, 3, fill=0, stroke=1)

    # الشريط الكحلي العلوي
    bar_h = h * 0.30
    c.setFillColor(NAVY)
    c.roundRect(x, y + h - bar_h, w, bar_h, 3, fill=1, stroke=0)
    c.setFillColor(NAVY)
    c.rect(x, y + h - bar_h, w, bar_h * 0.5, fill=1, stroke=0)  # يغطي الاستدارة السفلية للبار

    cx = x + w / 2
    draw_platform_brand(c, brand["platform_name"], x + w * 0.09, y + h - bar_h * 0.42,
                         h * 0.09, "Cairo-ExtraBold", GOLD_LIGHT, logo_r=h * 0.038, align="left",
                         gap=w * 0.015, ring_color=GOLD_LIGHT, mark_color=NAVY)
    _rtext(c, x + w * 0.94, y + h - bar_h * 0.42, f"{brand['doctor_prefix']} {brand['doctor_name']}", "Kufi-Bold", h * 0.062, CREAM, "right")

    # اسم الطالب
    _rtext(c, cx, y + h - bar_h - h * 0.155, data["full_name"], "Kufi-Bold", h * 0.10, NAVY, "center")
    _rtext(c, cx, y + h - bar_h - h * 0.29, data.get('group_name', ''),
           "Kufi-Regular", h * 0.04, TEXT_MUTED, "center")

    c.setStrokeColor(GOLD)
    c.setLineWidth(0.7)
    c.line(x + w * 0.1, y + h - bar_h - h * 0.335, x + w * 0.9, y + h - bar_h - h * 0.335)

    half = w / 2
    _rtext(c, x + w * 0.92, y + h * 0.20, "كود الحضور", "Kufi-Regular", h * 0.04, GOLD, "right")
    _etext(c, x + w * 0.92, y + h * 0.09, data["attendance_code"], "Cairo-Bold", h * 0.08, NAVY, "right")
    _rtext(c, x + w * 0.46, y + h * 0.20, "كود الدخول", "Kufi-Regular", h * 0.04, GOLD, "right")
    _etext(c, x + w * 0.46, y + h * 0.09, data["access_code"], "Cairo-Bold", h * 0.08, NAVY, "right")
    c.setStrokeColor(LINE_LIGHT)
    c.line(cx, y + h * 0.06, cx, y + h * 0.24)


# ---------------------------------------------------------------------------
# TEMPLATE C - تصميم مضغوط (Compact ID) - 10 بطاقات في الصفحة، بدون زخارف زوايا
# ---------------------------------------------------------------------------
def template_c(c, x, y, w, h, data, brand):
    register_fonts()

    c.setFillColor(CREAM)
    c.roundRect(x, y, w, h, 3, fill=1, stroke=0)
    c.setStrokeColor(NAVY)
    c.setLineWidth(1.1)
    c.roundRect(x, y, w, h, 3, fill=0, stroke=1)

    # شريط جانبي دهبي يمين
    strip_w = w * 0.03
    c.setFillColor(GOLD)
    c.rect(x + w - strip_w, y, strip_w, h, fill=1, stroke=0)

    cx = x + (w - strip_w) / 2
    top = y + h
    draw_platform_brand(c, brand["platform_name"], x + w * 0.075, top - h * 0.12,
                         h * 0.07, "Cairo-ExtraBold", NAVY, logo_r=h * 0.032, align="left",
                         gap=w * 0.014)
    _rtext(c, cx, top - h * 0.12, f"{brand['doctor_prefix']} {brand['doctor_name']}", "Kufi-Bold", h * 0.048, GOLD, "center")

    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.line(x + w * 0.06, top - h * 0.21, x + w * 0.94, top - h * 0.21)

    _rtext(c, cx, top - h * 0.36, data["full_name"], "Kufi-Bold", h * 0.095, NAVY, "center")
    _rtext(c, cx, top - h * 0.48, data.get("group_name", ""), "Kufi-Regular", h * 0.042, TEXT_MUTED, "center")

    box_top = top - h * 0.565
    box_h = h * 0.365
    c.setFillColor(NAVY)
    c.roundRect(x + w * 0.06, box_top - box_h, w * 0.88, box_h, 3, fill=1, stroke=0)

    _rtext(c, x + w * 0.86, box_top - h * 0.075, "كود الحضور", "Kufi-Regular", h * 0.030, GOLD_LIGHT, "right")
    _etext(c, x + w * 0.86, box_top - h * 0.16, data["attendance_code"], "Cairo-Bold", h * 0.062, CREAM, "right")
    _rtext(c, x + w * 0.86, box_top - h * 0.225, "كود الدخول للمنصة", "Kufi-Regular", h * 0.030, GOLD_LIGHT, "right")
    _etext(c, x + w * 0.86, box_top - h * 0.31, data["access_code"], "Cairo-Bold", h * 0.062, CREAM, "right")

    _etext(c, cx, y + h * 0.045, brand["academic_year"], "Cairo-Bold", h * 0.032, TEXT_MUTED, "center")


TEMPLATES = {
    "A": {"label": "تصميم فاخر (كحلي وذهبي)", "fn": template_a, "cards_per_page": 8},
    "B": {"label": "تصميم مبسط (بار علوي)", "fn": template_b, "cards_per_page": 10},
    "C": {"label": "تصميم مضغوط (ID Card)", "fn": template_c, "cards_per_page": 10},
}


def draw_crop_marks(c, x, y, w, h, length=6, offset=3):
    """علامات قص صغيرة على أركان كل بطاقة (بالنقطة - points)"""
    c.setStrokeColor(HexColor("#999999"))
    c.setLineWidth(0.4)
    corners = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    for (cx0, cy0) in corners:
        dx = -1 if cx0 == x else 1
        dy = -1 if cy0 == y else 1
        # خط أفقي
        c.line(cx0 + dx * offset, cy0, cx0 + dx * (offset + length), cy0)
        # خط رأسي
        c.line(cx0, cy0 + dy * offset, cx0, cy0 + dy * (offset + length))


# ---------------------------------------------------------------------------
# شعار المنصة (Logo) - أيقونة دائرية بسيطة بشكل "قبعة تخرج" جنب اسم EduFlow
# مرسومة بالكامل Vector (من غير أي صورة خارجية) عشان تفضل حادة في الطباعة
# ---------------------------------------------------------------------------
def draw_platform_brand(c, text, x, y, size, font, color, logo_r, align="right",
                         gap=None, ring_color=None, mark_color=None):
    """يرسم اسم المنصة (نص) + شعار المنصة جنبه، مع حساب عرض النص فعليًا
    عشان الشعار ميتصقش في النص ولا يفضل بعيد عنه بمسافة غريبة"""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if gap is None:
        gap = logo_r * 0.55
    tw = stringWidth(text, font, size)
    logo_cy = y + size * 0.35
    if align == "right":
        c.setFont(font, size)
        c.setFillColor(color)
        c.drawRightString(x, y, text)
        logo_cx = x - tw - gap - logo_r
    else:
        c.setFont(font, size)
        c.setFillColor(color)
        c.drawString(x, y, text)
        logo_cx = x - gap - logo_r
    draw_platform_logo(c, logo_cx, logo_cy, logo_r, ring_color=ring_color, mark_color=mark_color)


def draw_platform_logo(c, cx, cy, r, ring_color=None, mark_color=None):
    ring_color = ring_color or NAVY
    mark_color = mark_color or GOLD

    c.setFillColor(ring_color)
    c.circle(cx, cy, r, fill=1, stroke=0)

    dw = r * 0.62
    dh = r * 0.34
    cy_cap = cy + r * 0.16
    c.setFillColor(mark_color)
    p = c.beginPath()
    p.moveTo(cx, cy_cap + dh)
    p.lineTo(cx + dw, cy_cap)
    p.lineTo(cx, cy_cap - dh)
    p.lineTo(cx - dw, cy_cap)
    p.close()
    c.drawPath(p, fill=1, stroke=0)

    base_w = r * 0.62
    base_h = r * 0.30
    c.roundRect(cx - base_w / 2, cy_cap - dh - base_h * 0.55, base_w, base_h, base_h * 0.35, fill=1, stroke=0)

    c.setStrokeColor(mark_color)
    c.setLineWidth(max(0.6, r * 0.10))
    tx = cx + dw * 0.55
    ty0 = cy_cap + dh * 0.25
    ty1 = ty0 - r * 0.75
    c.line(tx, ty0, tx, ty1)
    c.setFillColor(mark_color)
    c.circle(tx, ty1 - r * 0.07, r * 0.11, fill=1, stroke=0)
