"""Tests for intent_classifier.py - with mocked OpenAI client."""
import os
import pytest
from unittest.mock import patch, AsyncMock

from openai import AsyncOpenAI

from intent_classifier import classify_intent, IntentResult
from tests.conftest import make_completion


class TestClassifyIntent:
    @pytest.fixture
    def client(self):
        c = AsyncMock()
        return c

    def _setup_response(self, client, json_str):
        client.chat.completions.create.return_value = make_completion(json_str)

    async def test_repair_lookup(self, client):
        self._setup_response(client, '{"needs_repair_lookup": true, "needs_prices": false, "wants_appointment": false, "brand": null}')
        result = await classify_intent(client, "como va mi reparacion")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is False

    async def test_prices(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": true, "wants_appointment": false, "brand": null}')
        result = await classify_intent(client, "cuanto cuesta reparar una pantalla")
        assert result.needs_prices is True

    async def test_appointment(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": true, "brand": null}')
        result = await classify_intent(client, "quiero agendar una cita")
        assert result.wants_appointment is True

    async def test_valid_brand(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": "dyson"}')
        result = await classify_intent(client, "tengo un dyson que no funciona")
        assert result.brand == "dyson"

    async def test_unknown_brand_ignored(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": "nokia"}')
        result = await classify_intent(client, "tengo un nokia")
        assert result.brand is None

    async def test_brand_case_insensitive(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": "DYSON"}')
        result = await classify_intent(client, "mi DYSON")
        assert result.brand == "dyson"

    async def test_needs_human(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "needs_human": true, "brand": null}')
        result = await classify_intent(client, "quiero hablar con un tecnico")
        assert result.needs_human is True

    async def test_multiple_intents(self, client):
        self._setup_response(client, '{"needs_repair_lookup": true, "needs_prices": true, "wants_appointment": true, "brand": "hp"}')
        result = await classify_intent(client, "cuanto cuesta y como va mi hp, quiero cita")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is True
        assert result.wants_appointment is True
        assert result.brand == "hp"

    async def test_api_failure_returns_fail_open(self, client):
        client.chat.completions.create.side_effect = Exception("API error")
        result = await classify_intent(client, "hola")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is True
        assert result.brand is None

    async def test_invalid_json_returns_fail_open(self, client):
        self._setup_response(client, "not valid json")
        result = await classify_intent(client, "hola")
        assert result.needs_repair_lookup is True
        assert result.needs_prices is True

    async def test_passes_history(self, client):
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "brand": null}')
        history = [
            {"role": "user", "content": "hola"},
            {"role": "assistant", "content": "bienvenido"},
            {"role": "user", "content": "tengo un problema"},
        ]
        await classify_intent(client, "con mi portatil", history=history)

        call_args = client.chat.completions.create.call_args
        messages = call_args.kwargs["messages"]
        # system + last 2 history + user message = 4 messages
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert messages[-1]["content"] == "con mi portatil"

    async def test_prompt_instructs_not_to_treat_ambiguity_as_needs_human(self, client):
        """Regression guard: ambiguity/uncertainty must not be conflated with an
        explicit request for a human agent in the classifier's own instructions."""
        self._setup_response(client, '{"needs_repair_lookup": false, "needs_prices": false, "wants_appointment": false, "needs_human": false, "brand": null}')
        await classify_intent(client, "algo ambiguo")

        system_content = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
        assert "ambiguedad" in system_content.lower() or "ambigüedad" in system_content.lower()
        assert "no es una peticion de humano" in system_content.lower() or "no es una petición de humano" in system_content.lower()


class TestClassifyIntentLive:
    """
    Live regression tests against the real OpenAI model, using the two
    real customer messages that previously caused a premature handoff
    (Thermomix error code, ambiguous VHS/Video8 tape description).

    Skipped automatically when no OPENAI_API_KEY is configured.
    """

    _api_key = os.getenv("OPENAI_API_KEY", "")

    pytestmark = pytest.mark.skipif(
        not _api_key or not _api_key.startswith("sk-"),
        reason="requires a real OPENAI_API_KEY (conftest.py sets a fake 'test-key' placeholder for other tests)",
    )

    @pytest.fixture
    def real_client(self):
        return AsyncOpenAI(api_key=self._api_key)

    async def test_thermomix_error_code_does_not_trigger_needs_human(self, real_client):
        history = [
            {"role": "user", "content": "Hola, Me aparece en la pantalla de mi Thermomix el error C347, que puedo hacer?"},
            {"role": "assistant", "content": "¿Podrías indicarme el modelo exacto de tu Thermomix? Por ejemplo, TM21, TM31, TM5, TM6 o TM7."},
        ]
        result = await classify_intent(real_client, "TM6", history=history)
        assert result.needs_human is False

    async def test_ambiguous_tape_description_does_not_trigger_needs_human(self, real_client):
        message = (
            "Por las fotos que he enviado no se ve, de que tipo son? Creo que si. Es "
            "una cinta VHS de 240 min (puede que tenga menos minutos grabados). En caso "
            "de que este dañada, serian dos cintas Video8 de 60 min."
        )
        result = await classify_intent(real_client, message)
        assert result.needs_human is False

    async def test_thermomix_dislodged_blades_does_not_trigger_needs_human(self, real_client):
        message = (
            "Tengo una Thermomix tm31 y dos vasos, no me funciona. Se han "
            "desbasculado las cuchillas"
        )
        result = await classify_intent(real_client, message)
        assert result.needs_human is False

    async def test_macbook_reinstall_error_does_not_trigger_needs_human(self, real_client):
        message = (
            "Hola Fatima Quisiera reinstalar macOS a mi MacBook Pro 2010. Borre el "
            "disco y no tenia copia. Quisiera instalarlo nuevamente. No me deja "
            "hacerlo manualmente porque me sale error"
        )
        result = await classify_intent(real_client, message)
        assert result.needs_human is False
