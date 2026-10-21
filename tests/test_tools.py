"""Pruebas unitarias de las herramientas (determinísticas, sin LLM)."""
import pytest

from biblioteca_agent.memory import DerivacionStore, LongTermMemory
from biblioteca_agent.tools import build_tools, calcular_multa_atraso


@pytest.mark.parametrize("dias, items, esperado", [
    (0, 1, 0),      # sin atraso
    (6, 1, 0),      # no alcanza un bloque completo de 7 días
    (7, 1, 1000),   # 1 bloque x 1 ítem
    (15, 2, 4000),  # 2 bloques x 2 ítems
    (21, 3, 9000),  # 3 bloques x 3 ítems
])
def test_multa_usuario_interno(dias, items, esperado):
    assert calcular_multa_atraso(dias, items, "interno")["monto"] == esperado


def test_multa_no_aplica_a_externos():
    r = calcular_multa_atraso(30, 2, "Externo")
    assert r["aplica"] is False and r["monto"] == 0


@pytest.mark.parametrize("dias, items, tipo", [(-1, 1, "interno"), (7, 0, "interno"), (7, 1, "alumno")])
def test_multa_valida_entradas(dias, items, tipo):
    with pytest.raises(ValueError):
        calcular_multa_atraso(dias, items, tipo)


@pytest.fixture
def tools(settings, retriever):
    lt = LongTermMemory(settings.long_term_path)
    der = DerivacionStore(settings.derivaciones_path)
    return {t.name: t for t in build_tools("seb", retriever, lt, der)}, lt, der


def test_tool_calcular_multa_formatea_monto(tools):
    t, _, _ = tools
    salida = t["calcular_multa"].invoke({"dias_atraso": 15, "cantidad_items": 2, "tipo_usuario": "interno"})
    assert salida.startswith("MONTO: $4.000")


def test_tool_guardar_dato_rechaza_datos_sensibles(tools):
    t, lt, _ = tools
    assert t["guardar_dato_usuario"].invoke({"dato": "rut", "valor": "12.345.678-9"}).startswith("ERROR")
    assert lt.obtener("seb") == {}


def test_tool_registrar_derivacion_escribe_ticket(tools):
    t, _, der = tools
    salida = t["registrar_derivacion"].invoke({"consulta": "descuento en multa", "motivo": "sin respaldo"})
    assert "DER-0001" in salida
    assert der.listar()[0]["usuario"] == "seb"


def test_descripciones_de_tools_presentes(tools):
    # El LLM elige la tool leyendo su descripción: ninguna puede estar vacía.
    t, _, _ = tools
    for nombre, herramienta in t.items():
        assert len(herramienta.description) > 50, nombre
