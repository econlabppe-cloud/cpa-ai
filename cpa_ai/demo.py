"""נתוני הדגמה: עסק פיקטיבי עם חצי שנה של פעילות, כדי שאפשר יהיה לראות את כל המסכים בלי חיבורים ובלי מפתחות.

כל הנתונים בדויים (מספרי עוסק תקינים לפי ספרת ביקורת, אך אינם של עסקים אמיתיים). המסד זמני בזיכרון."""
from __future__ import annotations

from datetime import date

from . import banking, collab, services
from .db import Business
from .rules.checks import validate_israeli_id
from .schemas import ApprovalIn, BusinessIn, IncomeIn


def _vid(n: int) -> str:
    base = f"{n:08d}"
    for check in range(10):
        if validate_israeli_id(base + str(check)):
            return base + str(check)
    raise AssertionError


def _month_back(today: date, k: int) -> tuple[int, int]:
    m = today.year * 12 + today.month - 1 - k
    return m // 12, m % 12 + 1


def _invoice(vendor: str, vid: str, number: int, when: date, pre: float, item: str, extra: str = "") -> str:
    vat = round(pre * 0.18, 2)
    return (f'{vendor}\nח.פ {vid}\nחשבונית מס מס\' {number}\nתאריך: {when:%d/%m/%Y}\n{item}\n{extra}'
            f'סה"כ לפני מע"מ: {pre:,.2f}\nמע"מ 18%: {vat:,.2f}\nסה"כ לתשלום: {pre + vat:,.2f}\n')


def seed(SessionLocal, today: date | None = None) -> int:
    """מחזיר את מזהה העסק שנוצר."""
    today = today or date.today()
    with SessionLocal() as s:
        biz = services.create_business(s, BusinessIn(
            name="סטודיו דנה — עסק לדוגמה", vat_id="037544129", owner_name="דנה לוי", vat_frequency="bimonthly",
            advance_rate_percent=5.0), "demo")
        biz.contact_email = "dana@example.com"
        biz.tags = "עיצוב, ליווי חודשי"
        biz.internal_notes = "לקוחה לדוגמה — הערות פנימיות מוצגות לרו\"ח בלבד"
        s.commit()

        customers = ["חברת אורבן בע\"מ", "מסעדת הים", "סטארטאפ נובה", "משרד עו\"ד כהן"]
        for k in range(7, 0, -1):                      # הכנסות: אחת בחודש (בלי מספר 105 — פער במספור לדוגמה)
            y, m = _month_back(today, k)
            number = 100 + (k if k > 3 else k - 1)
            services.add_income(s, biz.id, IncomeIn(
                doc_type="tax_invoice", doc_number=str(number), customer_name=customers[k % 4],
                doc_date=date(y, m, 12), pre_vat_amount=[9000, 12500, 7800, 15400, 11000, 9600, 13200][7 - k],
                withholding_tax=300.0 if k == 6 else 0.0), "demo")

        office = _vid(51345678)
        rent = _vid(51345100)
        tel = _vid(51200047)
        fuel = _vid(51100011)
        for k in range(7, 0, -1):                       # ספק קבוע: ציוד משרדי בכל חודש
            y, m = _month_back(today, k)
            services.process_text(s, biz.id, _invoice("אופיס דיפו בע\"מ", office, 3000 + k, date(y, m, 5), 300 + 10 * k,
                                                      "נייר A4 וטונר"), "heuristic", "demo")
            services.process_text(s, biz.id, _invoice("נכסי הדר בע\"מ", rent, 700 + k, date(y, m, 1), 4000,
                                                      "דמי שכירות למשרד"), "heuristic", "demo")
        y, m = _month_back(today, 1)
        services.process_text(s, biz.id, _invoice("סלקום", tel, 8801, date(y, m, 9), 220, "חבילת סלולר ואינטרנט"),
                              "heuristic", "demo")
        services.process_text(s, biz.id, _invoice("תחנת דלק מרכז", fuel, 4455, date(y, m, 14), 12000, "דלק לצי רכבים"),
                              "heuristic", "demo")                               # מעל הסף ללא מספר הקצאה
        services.process_text(s, biz.id, _invoice("מסעדת הדגל", _vid(51300022), 55, date(y, m, 20), 900, "ארוחה עסקית",
                                                   "שולם במזומן\n"), "heuristic", "demo")
        services.process_text(s, biz.id, f"Google Ireland Ltd\nTax Invoice No. 77\n{date(y, m, 22):%d/%m/%Y}\n"
                                         "Google Workspace\nTotal $120.00\n", "heuristic", "demo")
        services.process_text(s, biz.id, _invoice("משרד פרסום דיגיטל", _vid(51400033), 912, date(y, m, 25), 2500,
                                                   "קמפיין Google Ads"), "heuristic", "demo")

        csv = "תאריך,תיאור,סכום\n"                      # תנועות בנק: חלקן יותאמו, וחלקן יחכו לקבלה
        yy, mm = _month_back(today, 1)
        csv += f"{date(yy, mm, 6):%d/%m/%Y},אופיס דיפו,{-round((300 + 10) * 1.18, 2)}\n"
        csv += f"{date(yy, mm, 11):%d/%m/%Y},סופר פארם,-185.00\n{date(yy, mm, 15):%d/%m/%Y},חניון עזריאלי,-96.00\n"
        banking.import_csv(s, biz.id, csv)

        collab.create_thread(s, biz.id, kind="question", title="מה זה החיוב של 185 ₪ בסופר פארם?",
                             body="חיוב מהבנק בלי קבלה. ציוד משרדי או הוצאה פרטית?", author="רו\"ח ישראלה",
                             role="accountant")
        collab.create_thread(s, biz.id, kind="question", title="אפשר לקזז את הרכב החדש?",
                             body="קניתי רכב לעסק. מה צריך להביא לך?", author="דנה לוי", role="client")

        y, m = _month_back(today, 6)                    # דוח מע"מ ישן שאושר והוגש
        try:
            rep = services.prepare_report(s, biz.id, "vat_period", y, m, 2.25, "demo")
            services.approve_report(s, rep.id, ApprovalIn(approver_name="רו\"ח ישראלה", license_type="cpa",
                                                          license_number="12345", attest=True), "demo")
            services.mark_submitted(s, rep.id, "DEMO-001", "demo")
        except services.Conflict:
            pass
        return s.query(Business).one().id
