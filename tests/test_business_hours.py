"""Tests for main.get_outside_hours_message / is_within_business_hours."""
from datetime import datetime
from zoneinfo import ZoneInfo

import main


def _madrid(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Madrid"))


class TestGetOutsideHoursMessage:
    def test_friday_before_opening_says_today_not_monday(self, mocker):
        """Regression: viernes 9:08 AM (abre a las 9:30) no debe decir 'cerrado hasta el lunes'."""
        friday_early = _madrid(2026, 7, 17, 9, 8)  # 2026-07-17 is a Friday
        assert friday_early.weekday() == 4
        mocker.patch("main._get_madrid_now", return_value=friday_early)

        msg = main.get_outside_hours_message()

        assert "hoy" in msg
        assert "lunes" not in msg.lower()

    def test_friday_after_closing_says_monday(self, mocker):
        """Viernes despues de cerrar (18:00) si debe avisar que esta cerrado hasta el lunes."""
        friday_evening = _madrid(2026, 7, 17, 19, 0)
        mocker.patch("main._get_madrid_now", return_value=friday_evening)

        msg = main.get_outside_hours_message()

        assert "lunes" in msg.lower()

    def test_saturday_says_monday(self, mocker):
        saturday = _madrid(2026, 7, 18, 12, 0)
        assert saturday.weekday() == 5
        mocker.patch("main._get_madrid_now", return_value=saturday)

        msg = main.get_outside_hours_message()

        assert "lunes" in msg.lower()

    def test_sunday_says_monday(self, mocker):
        sunday = _madrid(2026, 7, 19, 12, 0)
        assert sunday.weekday() == 6
        mocker.patch("main._get_madrid_now", return_value=sunday)

        msg = main.get_outside_hours_message()

        assert "lunes" in msg.lower()

    def test_weekday_evening_says_tomorrow_not_monday(self, mocker):
        """Un martes por la noche, el siguiente dia habil es manana (miercoles), no lunes."""
        tuesday_evening = _madrid(2026, 7, 14, 19, 0)
        assert tuesday_evening.weekday() == 1
        mocker.patch("main._get_madrid_now", return_value=tuesday_evening)

        msg = main.get_outside_hours_message()

        assert "mañana" in msg
        assert "lunes" not in msg.lower()
