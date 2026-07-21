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

    # مثلث كحلي أعلى اليسار (زاوية علوية)
    tri = w * 0.20
    c.setFillColor(NAVY)
    p = c.beginPath()
    p.moveTo(x, y + h)
    p.lineTo(x + tri, y + h)
    p.lineTo(x, y + h - tri * 1.4)
    p.close()
    c.drawPath(p, fill=1, stroke=0)

    # خط دهبي موازي لضلع المثلث العلوي
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.1)
    c.line(x + tri * 0.15, y + h - tri * 0.05, x + tri * 1.35, y + h - tri * 1.55)

    # مثلث كحلي أسفل اليمين (زاوية سفلية)
    p2 = c.beginPath()
    p2.moveTo(x + w, y)
    p2.lineTo(x + w - tri, y)
    p2.lineTo(x + w, y + tri * 1.4)
    p2.close()
    c.drawPath(p2, fill=1, stroke=0)
    c.line(x + w - tri * 0.15, y + tri * 0.05, x + w - tri * 1.35, y + tri * 1.55)

    cx = x + w / 2
    top = y + h

    # اسم الدكتور + العنوان
    _rtext(c, cx, top - h * 0.135, brand["doctor_prefix"], "Kufi-SemiBold", h * 0.052, GOLD, "center")
    _rtext(c, cx, top - h * 0.24, brand["doctor_name"], "Kufi-Bold", h * 0.115, NAVY, "center")
    _rtext(c, cx, top - h * 0.315, brand["subject"], "Kufi-Regular", h * 0.052, TEXT_MUTED, "center")

    # اسم المنصة (يسار/يمين حسب المساحة - أعلى يمين الكارت بعيد عن المثلث)
    _etext(c, x + w - w * 0.06, top - h * 0.10, brand["platform_name"], "Cairo-ExtraBold", h * 0.075, NAVY, "right")

    # خط فاصل دهبي رفيع تحت العنوان
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.line(x + w * 0.12, top - h * 0.35, x + w * 0.88, top - h * 0.35)

    # صندوق اسم الطالب
    box_y = top - h * 0.52
    box_h = h * 0.145
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.6)
    c.roundRect(x + w * 0.08, box_y - box_h + h * 0.03, w * 0.84, box_h, 3, fill=0, stroke=1)
    _rtext(c, x + w * 0.88, box_y + h * 0.052, "اسم الطالب", "Kufi-Regular", h * 0.04, GOLD, "right")
    _rtext(c, x + w * 0.88, box_y - h * 0.02, data["full_name"], "Kufi-Bold", h * 0.075, NAVY, "right")

    # فاصل أفقي رفيع
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.line(x + w * 0.08, top - h * 0.585, x + w * 0.92, top - h * 0.585)

    # صف الأكواد (كود الحضور | كود الدخول)
    codes_y1 = top - h * 0.685
    codes_y2 = top - h * 0.75
    half = w / 2
    # كود الحضور - يمين
    _rtext(c, x + w * 0.92, codes_y1, "كود الحضور", "Kufi-Regular", h * 0.042, GOLD, "right")
    _etext(c, x + w * 0.92, codes_y2, data["attendance_code"], "Cairo-Bold", h * 0.078, NAVY, "right")
    # كود الدخول - شمال
    _rtext(c, x + w * 0.46, codes_y1, "كود الدخول للمنصة", "Kufi-Regular", h * 0.042, GOLD, "right")
    _etext(c, x + w * 0.46, codes_y2, data["access_code"], "Cairo-Bold", h * 0.078, NAVY, "right")
    # فاصل رأسي بين الكودين
    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.line(cx, codes_y2 - h * 0.01, cx, codes_y1 + h * 0.04)

    _rtext(c, cx, top - h * 0.80, "كود تسجيل الدخول الخاص بك - لا تشاركه مع أي شخص",
           "Kufi-Regular", h * 0.03, TEXT_MUTED, "center")

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
    _etext(c, x + w * 0.06, y + h - bar_h * 0.42, brand["platform_name"], "Cairo-ExtraBold", h * 0.09, GOLD_LIGHT, "left")
    _rtext(c, x + w * 0.94, y + h - bar_h * 0.42, brand["doctor_name"], "Kufi-Bold", h * 0.075, CREAM, "right")

    # اسم الطالب
    _rtext(c, cx, y + h - bar_h - h * 0.16, data["full_name"], "Kufi-Bold", h * 0.115, NAVY, "center")
    _rtext(c, cx, y + h - bar_h - h * 0.245, f"{data.get('group_name','')}  •  {brand['subject']}",
           "Kufi-Regular", h * 0.045, TEXT_MUTED, "center")

    c.setStrokeColor(GOLD)
    c.setLineWidth(0.7)
    c.line(x + w * 0.1, y + h - bar_h - h * 0.29, x + w * 0.9, y + h - bar_h - h * 0.29)

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
    _etext(c, x + w * 0.05, top - h * 0.13, brand["platform_name"], "Cairo-ExtraBold", h * 0.075, NAVY, "left")
    _rtext(c, cx, top - h * 0.13, brand["doctor_name"], "Kufi-Bold", h * 0.06, GOLD, "center")

    c.setStrokeColor(LINE_LIGHT)
    c.setLineWidth(0.5)
    c.line(x + w * 0.06, top - h * 0.19, x + w * 0.94, top - h * 0.19)

    _rtext(c, cx, top - h * 0.36, data["full_name"], "Kufi-Bold", h * 0.10, NAVY, "center")
    _rtext(c, cx, top - h * 0.45, data.get("group_name", ""), "Kufi-Regular", h * 0.045, TEXT_MUTED, "center")

    box_top = top - h * 0.56
    box_h = h * 0.34
    c.setFillColor(NAVY)
    c.roundRect(x + w * 0.06, box_top - box_h, w * 0.88, box_h, 3, fill=1, stroke=0)

    half_y = box_top - box_h * 0.38
    _rtext(c, x + w * 0.86, box_top - h * 0.075, "كود الحضور", "Kufi-Regular", h * 0.032, GOLD_LIGHT, "right")
    _etext(c, x + w * 0.86, box_top - h * 0.145, data["attendance_code"], "Cairo-Bold", h * 0.065, CREAM, "right")
    _rtext(c, x + w * 0.86, box_top - h * 0.215, "كود الدخول للمنصة", "Kufi-Regular", h * 0.032, GOLD_LIGHT, "right")
    _etext(c, x + w * 0.86, box_top - h * 0.285, data["access_code"], "Cairo-Bold", h * 0.065, CREAM, "right")

    _etext(c, cx, y + h * 0.05, brand["academic_year"], "Cairo-Bold", h * 0.035, TEXT_MUTED, "center")


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
