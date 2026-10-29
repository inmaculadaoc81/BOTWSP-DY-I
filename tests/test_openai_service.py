"""Tests for openai_service.py - _strip_filler pure function."""
from openai_service import _strip_filler


class TestStripFiller:
    def test_removes_si_necesitas(self):
        text = "Tu cita esta lista. Si necesitas algo mas, no dudes en escribirnos."
        result = _strip_filler(text)
        assert "Si necesitas" not in result
        assert result == "Tu cita esta lista."

    def test_removes_no_dudes(self):
        text = "Listo! No dudes en contactarnos."
        result = _strip_filler(text)
        assert "No dudes" not in result
        assert "Listo" in result

    def test_removes_estoy_aqui(self):
        text = "El equipo esta en reparacion. Estoy aquí para ayudarte."
        result = _strip_filler(text)
        assert "Estoy aquí" not in result

    def test_removes_algo_mas(self):
        text = "Tu equipo esta listo. ¿Algo más en lo que pueda ayudarte?"
        result = _strip_filler(text)
        assert "Algo más" not in result

    def test_no_filler_unchanged(self):
        text = "Tu equipo esta en reparacion."
        result = _strip_filler(text)
        assert result == text

    def test_adds_period_when_missing(self):
        text = "Tu equipo esta listo. Si necesitas algo mas, dime"
        result = _strip_filler(text)
        assert result.endswith(".")

    def test_preserves_exclamation(self):
        text = "Perfecto!"
        result = _strip_filler(text)
        assert result == "Perfecto!"

    def test_preserves_question_mark(self):
        text = "Cual es el modelo?"
        result = _strip_filler(text)
        assert result == "Cual es el modelo?"

    def test_removes_quedo_a_tu_disposicion(self):
        text = "El diagnostico es gratuito. Quedo a tu disposición para lo que necesites."
        result = _strip_filler(text)
        assert "Quedo a tu disposición" not in result


class TestEnsureIndependenceLine:
    BLOCK = (
        "Lo bueno es que trabajamos con total transparencia:\n\n"
        "✅ Diagnóstico *GRATUITO* con un técnico\n"
        "✅ Presupuesto en *24-48h* sin compromiso\n"
        "✅ Solo pagas si la reparación se realiza con éxito\n"
        "✅ Garantía de *6 meses* en cada reparación\n\n"
        "Puedes traerlo directamente al local."
    )

    def test_inserts_after_benefits_block(self):
        from openai_service import _ensure_independence_line, INDEPENDENCE_LINE
        result = _ensure_independence_line(self.BLOCK)
        assert f"✅ Garantía de *6 meses* en cada reparación\n{INDEPENDENCE_LINE}\n\nPuedes" in result

    def test_inserts_after_last_check_line(self):
        from openai_service import _ensure_independence_line, INDEPENDENCE_LINE
        text = self.BLOCK.replace("reparación\n\n", "reparación\n✅ +1.100 reseñas\n\n")
        assert f"✅ +1.100 reseñas\n{INDEPENDENCE_LINE}" in _ensure_independence_line(text)

    def test_no_change_if_already_present(self):
        from openai_service import _ensure_independence_line
        text = self.BLOCK + "\nSomos un servicio técnico independiente."
        assert _ensure_independence_line(text) == text

    def test_no_change_without_block(self):
        from openai_service import _ensure_independence_line
        assert _ensure_independence_line("Hola, ¿en qué te ayudo?") == "Hola, ¿en qué te ayudo?"
