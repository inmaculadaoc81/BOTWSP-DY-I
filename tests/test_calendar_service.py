"""Tests for calendar_service.py - appointment context and event creation."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from calendar_service import (
    CalendarService,
    MADRID_TZ,
    _rental_duration_is_7_plus_days,
    _rental_payment_links,
)


class TestGetAppointmentContext:
    def setup_method(self):
        self.svc = CalendarService()

    def test_contains_schedule(self):
        result = self.svc.get_appointment_context()
        assert "Lunes a Viernes de 09:30 a 18:00" in result

    def test_contains_holidays_closed(self):
        result = self.svc.get_appointment_context()
        assert "festivos cerrados" in result

    def test_contains_confirmar_cita_format(self):
        result = self.svc.get_appointment_context()
        assert "CONFIRMAR_CITA|" in result

    def test_contains_confirmar_envio_format(self):
        result = self.svc.get_appointment_context()
        assert "CONFIRMAR_ENVIO|" in result

    def test_contains_envio_cost(self):
        result = self.svc.get_appointment_context()
        assert "15€" in result

    def test_contains_cita_vs_envio_distinction(self):
        result = self.svc.get_appointment_context()
        assert "PROTOCOLO DE CITA (cliente viene al local)" in result
        assert "PROTOCOLO DE ENVIO (mensajero recoge a domicilio)" in result

    def test_envio_does_not_request_personal_data(self):
        result = self.svc.get_appointment_context()
        assert "NO requiere recopilar ningun dato del cliente por el chat" in result

    def test_contains_current_date(self):
        result = self.svc.get_appointment_context()
        assert "Fecha actual:" in result


class TestCreateEvent:
    def setup_method(self):
        self.svc = CalendarService()

    async def test_success(self):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt1"}

        with patch.object(self.svc, "_get_service", return_value=mock_service):
            result = await self.svc.create_event(
                title="Cita: Juan - HP",
                start_iso="2026-04-01T10:00:00+02:00",
                description="Test",
                attendee_phone="34612345678",
            )
        assert result == {"id": "evt1"}

    async def test_failure_returns_none(self):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.side_effect = Exception("API error")

        with patch.object(self.svc, "_get_service", return_value=mock_service):
            result = await self.svc.create_event(
                title="Cita: Juan - HP",
                start_iso="2026-04-01T10:00:00+02:00",
            )
        assert result is None

    async def test_end_time_is_30_minutes_after_start(self):
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {"id": "evt1"}

        with patch.object(self.svc, "_get_service", return_value=mock_service):
            await self.svc.create_event(
                title="Test",
                start_iso="2026-04-01T10:00:00+02:00",
            )

        call_args = mock_service.events.return_value.insert.call_args
        body = call_args.kwargs["body"]
        start = datetime.fromisoformat(body["start"]["dateTime"])
        end = datetime.fromisoformat(body["end"]["dateTime"])
        assert (end - start).total_seconds() == 1800  # 30 minutes


class TestRentalFreeShippingLogic:
    @pytest.mark.parametrize("duracion", ["7 días", "8 días", "1 semana", "2 semanas", "1 mes"])
    def test_7_days_or_more_is_free(self, duracion):
        assert _rental_duration_is_7_plus_days(duracion) is True

    @pytest.mark.parametrize("duracion", ["1 día", "3 días", "6 días"])
    def test_less_than_7_days_is_not_free(self, duracion):
        assert _rental_duration_is_7_plus_days(duracion) is False

    @pytest.mark.parametrize("tipo_equipo", ["Windows", "Mac", "Windows - HP 15-bc"])
    def test_normal_laptops_use_caso_1_links(self, tipo_equipo):
        links = _rental_payment_links(tipo_equipo)
        assert links["rental"] == "https://sis.redsys.es/tiendaWeb/item/NDk4OzU="
        assert links["deposit"] == "https://sis.redsys.es/tiendaWeb/item/NDk4OzY="

    @pytest.mark.parametrize("tipo_equipo", ["Gaming", "Surface", "Gaming - Asus ROG Strix"])
    def test_gaming_and_surface_use_caso_2_links(self, tipo_equipo):
        links = _rental_payment_links(tipo_equipo)
        assert links["rental"] == "https://sis.redsys.es/tiendaWeb/item/NDk4Ozc="
        assert links["deposit"] == "https://sis.redsys.es/tiendaWeb/item/NDk4Ozg="
