"""כלי עזר לבדיקות: PDF עם עברית בשני הסגנונות, ויצירת עסק."""
import io

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

INVOICE_LINES = [
    'אופיס דיפו בע"מ', "ח.פ 513456780", "חשבונית מס מס' 10234", "תאריך: 15/07/2026",
    'סה"כ לפני מע"מ: 1,000.00', 'מע"מ 18%: 180.00', 'סה"כ לתשלום: 1,180.00',
]
_registered = False


def _font():
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont("DV", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        _registered = True


def visual(s: str) -> str:
    toks = s.split(" ")[::-1]
    return " ".join(t[::-1] if any("֐" <= c <= "׿" for c in t) else t for t in toks)


def make_pdf(lines=INVOICE_LINES, style="visual") -> bytes:
    """style=visual: כך נראה PDF תקין שנוצר ממערכת חשבוניות; style=logical: הטקסט נשמר בסדר לוגי."""
    _font()
    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.setFont("DV", 12)
    y = 800
    for ln in lines:
        c.drawString(60, y, visual(ln) if style == "visual" else ln)
        y -= 20
    c.save()
    return b.getvalue()


def blank_pdf() -> bytes:
    b = io.BytesIO()
    c = canvas.Canvas(b)
    c.showPage()
    c.save()
    return b.getvalue()


def make_image(lines=INVOICE_LINES, fmt="PNG") -> bytes:
    """צילום חשבונית מדומה: טקסט לוגי, PIL+raqm מסדר את העברית מימין לשמאל כמו על נייר."""
    from PIL import Image, ImageDraw, ImageFont
    f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    im = Image.new("RGB", (900, 60 + 54 * len(lines)), "white")
    d = ImageDraw.Draw(im)
    for i, ln in enumerate(lines):
        d.text((860 - d.textlength(ln, font=f), 20 + i * 54), ln, fill="black", font=f)
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


def scanned_pdf(lines=INVOICE_LINES) -> bytes:
    """PDF סרוק: תמונה בלבד, בלי שכבת טקסט."""
    from PIL import Image
    buf = io.BytesIO()
    Image.open(io.BytesIO(make_image(lines))).save(buf, "PDF", resolution=100)
    return buf.getvalue()
