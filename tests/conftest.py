import pytest

from cpa_ai import services
from cpa_ai.db import make_sessionmaker
from cpa_ai.schemas import BusinessIn


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """כל בדיקה כותבת קבצי מקור לתיקייה זמנית משלה."""
    monkeypatch.setenv("CPA_AI_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def env():
    Session = make_sessionmaker("sqlite://")
    s = Session()
    biz = services.create_business(s, BusinessIn(name="סטודיו", vat_id="037544129", owner_name="דנה"), "t")
    return s, biz
