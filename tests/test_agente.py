"""Pruebas de orquestación del agente con un LLM falso (sin red).

Cada lista de respuestas simula, en orden, lo que contestaría el LLM:
primero el planificador y luego el agente (llamadas a tools y respuesta final).
"""
import json

from langchain_core.messages import AIMessage

from biblioteca_agent.agent import BibliotecaAgent, parse_plan
from conftest import tool_call

SIN_PLAN = AIMessage(content='{"requiere_plan": false, "motivo": "pregunta única", "pasos": []}')


def test_parse_plan_tolera_texto_extra_y_json_invalido():
    p = parse_plan('Claro: {"requiere_plan": true, "motivo": "dos temas", "pasos": '
                   '[{"descripcion": "a"}, {"descripcion": "b"}]} listo')
    assert p["requiere_plan"] and len(p["pasos"]) == 2
    assert parse_plan("no es json")["requiere_plan"] is False
    # Un plan de un solo paso no justifica planificar: se usa ReAct.
    assert parse_plan('{"requiere_plan": true, "pasos": [{"descripcion": "a"}]}')["requiere_plan"] is False


def test_modo_react_usa_tool_de_calculo_y_guarda_memoria(settings, retriever, fake_llm):
    llm = fake_llm([
        SIN_PLAN,
        tool_call("calcular_multa", {"dias_atraso": 15, "cantidad_items": 2, "tipo_usuario": "interno"}),
        AIMessage(content="Según el Artículo 18°, tu multa es de $4.000."),
    ])
    agente = BibliotecaAgent(settings, llm, retriever, usuario="seb", session_id="s1")
    turno = agente.responder("Soy alumno, devolví 2 libros con 15 días de atraso, ¿cuánto pago?")

    assert turno.modo == "react"
    assert turno.tools[0]["tool"] == "calcular_multa"
    assert turno.tools[0]["output"].startswith("MONTO: $4.000")
    assert [m.content for m in agente.historial_completo()][-1] == turno.respuesta
    traza = json.loads(settings.traces_path.read_text(encoding="utf-8").splitlines()[-1])
    assert traza["modo"] == "react" and traza["usuario"] == "seb"


def test_memoria_largo_plazo_cruza_sesiones(settings, retriever, fake_llm):
    llm = fake_llm([
        SIN_PLAN,
        tool_call("guardar_dato_usuario", {"dato": "tipo_usuario", "valor": "interno"}),
        AIMessage(content="Anotado, eres usuario interno."),
    ])
    BibliotecaAgent(settings, llm, retriever, "seb", "sesion_1").responder("Soy alumno de Duoc")

    # Nueva conversación: el corto plazo parte vacío, pero el largo plazo se mantiene.
    nuevo = BibliotecaAgent(settings, fake_llm([]), retriever, "seb", "sesion_2")
    assert nuevo.memory.load_memory_variables({})["chat_history"] == []
    assert "tipo_usuario: interno" in nuevo._datos_usuario()
    # Otro usuario no ve esos datos.
    assert "interno" not in BibliotecaAgent(settings, fake_llm([]), retriever, "otro", "s3")._datos_usuario()


def test_modo_plan_se_detiene_si_falta_un_dato(settings, retriever, fake_llm):
    plan = {"requiere_plan": True, "motivo": "pérdida y multa", "pasos": [
        {"descripcion": "Buscar la norma sobre pérdida de material", "herramienta": "buscar_reglamento"},
        {"descripcion": "Calcular la multa por atraso", "herramienta": "calcular_multa"},
        {"descripcion": "Registrar derivación si corresponde", "herramienta": "registrar_derivacion"},
    ]}
    llm = fake_llm([
        AIMessage(content=json.dumps(plan)),
        # Paso 1: busca en el reglamento y resume.
        tool_call("buscar_reglamento", {"consulta": "pérdida de material"}),
        AIMessage(content="Art. 22°: reponer el material en 20 días hábiles."),
        # Paso 2: no sabe los días de atraso -> condición cambiante.
        AIMessage(content="FALTA_DATO: ¿Cuántos días de atraso tiene el otro libro?"),
        # Síntesis final.
        AIMessage(content="Por la pérdida debes reponer el libro (Art. 22°). ¿Cuántos días de atraso tiene el otro?"),
    ])
    agente = BibliotecaAgent(settings, llm, retriever, "seb", "s1")
    turno = agente.responder("Perdí un libro y tengo otro atrasado, ¿qué hago y cuánto pago?")

    assert turno.modo == "plan"
    assert [p["estado"] for p in turno.pasos] == ["completado", "bloqueado", "omitido"]
    assert turno.pasos[0]["tools"][0]["tool"] == "buscar_reglamento"
    assert turno.respuesta.endswith("¿Cuántos días de atraso tiene el otro?")


def test_modo_plan_completo(settings, retriever, fake_llm):
    plan = {"requiere_plan": True, "motivo": "norma + cálculo", "pasos": [
        {"descripcion": "Buscar la multa por atraso", "herramienta": "buscar_reglamento"},
        {"descripcion": "Calcular la multa para 14 días y 1 ítem", "herramienta": "calcular_multa"},
    ]}
    llm = fake_llm([
        AIMessage(content=json.dumps(plan)),
        AIMessage(content="Art. 18°: $1.000 por cada 7 días, por ítem, solo internos."),
        tool_call("calcular_multa", {"dias_atraso": 14, "cantidad_items": 1, "tipo_usuario": "interno"}),
        AIMessage(content="Multa: $2.000."),
        AIMessage(content="Según el Artículo 18°, debes pagar $2.000."),
    ])
    turno = BibliotecaAgent(settings, llm, retriever, "seb", "s1").responder("¿Cuál es la regla y cuánto pago por 14 días?")
    assert [p["estado"] for p in turno.pasos] == ["completado", "completado"]
    assert turno.pasos[1]["tools"][0]["output"].startswith("MONTO: $2.000")
