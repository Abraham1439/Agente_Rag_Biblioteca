"""Interfaz Streamlit del Asistente de la Biblioteca Duoc UC.

Ejecutar con:  streamlit run app.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from biblioteca_agent.agent import BibliotecaAgent, build_llm  # noqa: E402
from biblioteca_agent.config import Settings  # noqa: E402
from biblioteca_agent.memory import DerivacionStore, LongTermMemory  # noqa: E402
from biblioteca_agent.rag import ReglamentoRetriever  # noqa: E402

st.set_page_config(page_title="Asistente Biblioteca Duoc UC", page_icon="📚", layout="wide")

ICONO_ESTADO = {"completado": "✅", "bloqueado": "⛔", "omitido": "⏭️", "pendiente": "⏳"}


@st.cache_resource(show_spinner="Cargando índice del reglamento...")
def recursos():
    settings = Settings()
    return settings, build_llm(settings), ReglamentoRetriever.from_settings(settings)


def mostrar_tools(llamadas: list[dict]) -> None:
    for c in llamadas:
        st.markdown(f"**🔧 {c['tool']}** `{c['input']}`")
        st.code(c["output"][:1200] + ("..." if len(c["output"]) > 1200 else ""), language=None,
                wrap_lines=True)


def mostrar_traza(turno: dict) -> None:
    titulo = "🗺️ Plan-and-Execute" if turno["modo"] == "plan" else "🔁 ReAct"
    with st.expander(f"Cómo lo resolvió el agente · {titulo}"):
        if turno.get("motivo_plan"):
            st.caption(f"Decisión del planificador: {turno['motivo_plan']}")
        if turno["modo"] == "plan":
            for i, paso in enumerate(turno["pasos"], 1):
                st.markdown(f"{ICONO_ESTADO[paso['estado']]} **Paso {i}:** {paso['descripcion']} "
                            f"· _{paso['estado']}_")
                mostrar_tools(paso["tools"])
                if paso["resultado"]:
                    st.caption(f"Resultado: {paso['resultado']}")
        elif turno["tools"]:
            mostrar_tools(turno["tools"])
        else:
            st.caption("Respondió sin llamar herramientas.")


# --------------------------------------------------------------------------
try:
    settings, llm, retriever = recursos()
except Exception as exc:  # faltan claves o no hay conexión
    st.error(f"No se pudo iniciar el agente: {exc}")
    st.info("Revisa que el archivo .env tenga OPENROUTER_API_KEY y EMBEDDING_API_KEY (ver README).")
    st.stop()

if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:8]
    st.session_state.trazas = {}

with st.sidebar:
    st.header("Biblioteca Duoc UC")
    usuario = st.text_input("Usuario", value="estudiante",
                            help="Identifica la memoria de largo plazo (datos que se recuerdan entre conversaciones).")
    if st.button("Nueva conversación", width="stretch"):
        st.session_state.session_id = uuid.uuid4().hex[:8]
        st.session_state.trazas = {}
        st.rerun()
    st.caption(f"Sesión: `{st.session_state.session_id}`")

    st.subheader("Memoria de largo plazo")
    lt = LongTermMemory(settings.long_term_path)
    datos = lt.obtener(usuario)
    if datos:
        st.json(datos)
        if st.button("Olvidar mis datos"):
            lt.olvidar(usuario)
            st.rerun()
    else:
        st.caption("Sin datos guardados todavía.")

    st.subheader("🎫 Derivaciones")
    tickets = [t for t in DerivacionStore(settings.derivaciones_path).listar() if t["usuario"] == usuario]
    for t in tickets[-5:]:
        st.markdown(f"**{t['id']}** · {t['estado']}  \n{t['consulta']}")
    if not tickets:
        st.caption("Sin derivaciones.")

    st.divider()
    st.caption(f"Modelo: `{settings.llm_model}` · esfuerzo `{settings.llm_reasoning_effort}`  \n"
               f"Memoria corto plazo: ventana k={settings.memory_window_k}  \n"
               f"RAG: top_k={settings.top_k}, umbral={settings.score_threshold}")

agente = BibliotecaAgent(settings, llm, retriever, usuario=usuario, session_id=st.session_state.session_id)

st.title("Asistente del Reglamento de Bibliotecas")
st.caption("Pregunta por préstamos, renovaciones, multas, pérdidas, salas de estudio o lentes VR.")

# Historial de la sesión (leído desde el archivo de la memoria de corto plazo).
for i, msg in enumerate(agente.historial_completo()):
    rol = "user" if msg.type == "human" else "assistant"
    with st.chat_message(rol):
        st.markdown(msg.content)
        if rol == "assistant" and i in st.session_state.trazas:
            mostrar_traza(st.session_state.trazas[i])

if pregunta := st.chat_input("Escribe tu consulta..."):
    with st.chat_message("user"):
        st.markdown(pregunta)
    with st.chat_message("assistant"):
        with st.spinner("Pensando..."):
            try:
                turno = agente.responder(pregunta)
            except Exception as exc:
                st.error(f"Error al consultar el modelo: {exc}")
                st.stop()
        st.markdown(turno.respuesta)
        mostrar_traza(turno.as_dict())
    # El mensaje de respuesta es el último del historial: se asocia la traza a ese índice.
    st.session_state.trazas[len(agente.historial_completo()) - 1] = turno.as_dict()
    st.rerun()
