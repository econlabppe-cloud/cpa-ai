"""בדיקת בריאות הספרים: פערים בהמספור, כפילויות, תאריכים עתידיים, אינדיקציות לבדיקה."""
from datetime import date, timedelta

from cpa_ai import health, services
from cpa_ai.schemas import IncomeIn

TODAY = date(2026, 10, 13)


def inc(s, biz, when, amount, number=None, customer="לקוח א", doc_type="tax_invoice", wh=0.0):
    return services.add_income(s, biz.id, IncomeIn(doc_type=doc_type, doc_number=number, customer_name=customer,
                                                   doc_date=when, pre_vat_amount=amount, withholding_tax=wh), "t")


def codes(r):
    return {i["code"] for i in r["issues"]}


def test_clean_books_score_100(env):
    s, biz = env
    for n, d in enumerate((date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1)), start=100):
        inc(s, biz, d, 1000, str(n), customer=f"לקוח {n}")
    r = health.books_health(s, biz, 2026, TODAY)
    assert r["issues"] == [] and r["score"] == 100


def test_gap_in_income_invoice_numbers(env):
    s, biz = env
    for n in (100, 101, 104, 105):
        inc(s, biz, date(2026, 3, 1), 500, str(n), customer=f"c{n}")
    r = health.books_health(s, biz, 2026, TODAY)
    gap = next(i for i in r["issues"] if i["code"] == "INCOME_NUMBER_GAP")
    assert gap["severity"] == "high" and gap["detail_data"]["missing"] == [102, 103]


def test_credit_notes_have_their_own_sequence(env):
    s, biz = env
    for n in (1, 2, 3):
        inc(s, biz, date(2026, 3, 1), 500, str(n), customer=f"a{n}")
    inc(s, biz, date(2026, 3, 2), 100, "900", customer="z", doc_type="credit_invoice")
    assert "INCOME_NUMBER_GAP" not in codes(health.books_health(s, biz, 2026, TODAY))


def test_duplicate_and_missing_numbers(env):
    s, biz = env
    inc(s, biz, date(2026, 3, 1), 500, "7", customer="a")
    inc(s, biz, date(2026, 3, 2), 600, "7", customer="b")
    inc(s, biz, date(2026, 3, 3), 700, None, customer="c")
    r = health.books_health(s, biz, 2026, TODAY)
    assert {"INCOME_NUMBER_DUPLICATE", "INCOME_NUMBER_MISSING"} <= codes(r)
    assert r["score"] < 100


def test_future_dated_document(env):
    s, biz = env
    inc(s, biz, TODAY + timedelta(days=40), 500, "1")
    r = health.books_health(s, biz, 2026, TODAY)
    assert "FUTURE_DATE" in codes(r)


def test_withholding_certificates_reminder(env):
    s, biz = env
    inc(s, biz, date(2026, 3, 1), 10_000, "1", customer="חברה בע\"מ", wh=1_500)
    r = health.books_health(s, biz, 2026, TODAY)
    w = next(i for i in r["issues"] if i["code"] == "WITHHOLDING_CERTIFICATES")
    assert w["detail_data"]["total"] == 1_500 and w["detail_data"]["customers"] == ["חברה בע\"מ"]


def test_customer_dependence_and_loss(env):
    s, biz = env
    for n in range(1, 5):
        inc(s, biz, date(2026, n, 1), 10_000, str(n), customer="לקוח יחיד")
    inc(s, biz, date(2026, 5, 1), 1_000, "5", customer="אחר")
    r = health.books_health(s, biz, 2026, TODAY)
    assert "CUSTOMER_DEPENDENCE" in codes(r)
    assert next(i for i in r["issues"] if i["code"] == "CUSTOMER_DEPENDENCE")["detail_data"]["share"] > 0.9
    assert "LOSS_YEAR" not in codes(r)


def test_heuristic_flag_and_disclaimer(env):
    s, biz = env
    r = health.books_health(s, biz, 2026, TODAY)
    assert "אינדיקציות" in r["disclaimer"] and "אינן כללי חוק" in r["disclaimer"]


def test_serious_findings_become_a_home_task(env):
    from cpa_ai import home
    s, biz = env
    for n in (100, 103):
        inc(s, biz, date(2026, 10, 1), 500, str(n), customer=f"c{n}")
    tasks = {t["id"]: t for t in home.build_home(s, biz, TODAY)["tasks"]}
    assert "books_health" in tasks and "פער" in tasks["books_health"]["detail"]
    clean = {t["id"] for t in home.build_home(env[0], env[1], date(2027, 6, 1))["tasks"]}
    assert "books_health" not in clean  # השנה הנוכחית ריקה
