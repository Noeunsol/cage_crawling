from ui.common import fmt_date


def test_fmt_date_normalizes_provider_dates():
    assert fmt_date("Mon, 08 Jun 2026 15:23:00 GMT") == "2026-06-08"
    assert fmt_date("2026-08-13T09:00:00") == "2026-08-13"
    assert fmt_date("2026.08.13") == "2026-08-13"
    assert fmt_date("") == "미제공"
