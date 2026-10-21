"""Orquestación del agente.

Cada turno sigue este flujo:
1. Cargar memoria: últimas k interacciones (corto plazo) + datos del usuario (largo plazo).
2. Planificador: decide si la consulta necesita un plan de varios pasos.
   - No necesita plan  -> modo ReAct: el AgentExecutor razona, llama tools y observa
                          en ciclo hasta responder (máximo `max_iterations`).
   - Necesita plan     -> modo Plan-and-Execute: se ejecuta cada paso con el mismo
                          AgentExecutor, pasando los resultados anteriores como contexto.
                          Si un paso necesita un dato que falta (FALTA_DATO) el plan se
                          detiene y se le pregunta al usuario; en el turno siguiente se
                          vuelve a planificar con la nueva información (replanificación).
3. Guardar en memoria la pregunta y la respuesta final, y registrar la traza.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from . import prompts
from .config import Settings
from .memory import DerivacionStore, LongTermMemory, crear_memoria_corto_plazo
from .rag import ReglamentoRetriever
from .tools import build_tools

MAX_PASOS = 4


def build_llm(settings: Settings) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    if not settings.llm_api_key:
        raise RuntimeError("Falta OPENROUTER_API_KEY en el archivo .env")
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
        max_retries=2,
        # Esfuerzo de razonamiento bajo (recomendación del docente por costo).
        extra_body={"reasoning": {"effort": settings.llm_reasoning_effort}},
    )


def _texto(contenido) -> str:
    """Normaliza el contenido de un mensaje (algunos modelos devuelven listas de bloques)."""
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in contenido)
    return str(contenido)


def parse_plan(texto: str) -> dict:
    """Extrae el JSON del planificador. Si no se puede leer, se cae a modo ReAct."""
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    pasos = [p for p in data.get("pasos", []) if isinstance(p, dict) and p.get("descripcion")]
    requiere = bool(data.get("requiere_plan")) and len(pasos) >= 2
    return {
        "requiere_plan": requiere,
        "motivo": data.get("motivo", "No se pudo interpretar el plan; se usa ReAct." if not data else ""),
        "pasos": pasos[:MAX_PASOS] if requiere else [],
    }


@dataclass
class Turno:
    pregunta: str
    respuesta: str = ""
    modo: str = "react"  # "react" o "plan"
    motivo_plan: str = ""
    pasos: list[dict] = field(default_factory=list)   # modo plan: descripcion, estado, resultado, tools
    tools: list[dict] = field(default_factory=list)   # modo react: llamadas a tools

    def as_dict(self) -> dict:
        return {"pregunta": self.pregunta, "respuesta": self.respuesta, "modo": self.modo,
                "motivo_plan": self.motivo_plan, "pasos": self.pasos, "tools": self.tools}


def _tool_calls(intermediate_steps) -> list[dict]:
    llamadas = []
    for action, observation in intermediate_steps:
        llamadas.append({"tool": action.tool, "input": action.tool_input, "output": str(observation)})
    return llamadas


class BibliotecaAgent:
    def __init__(self, settings: Settings, llm: BaseChatModel, retriever: ReglamentoRetriever,
                 usuario: str, session_id: str):
        self.settings = settings
        self.llm = llm
        self.retriever = retriever
        self.usuario = usuario
        self.session_id = session_id
        self.long_term = LongTermMemory(settings.long_term_path)
        self.derivaciones = DerivacionStore(settings.derivaciones_path)
        self.memory = crear_memoria_corto_plazo(settings.sessions_dir, session_id, settings.memory_window_k)
        self.tools = build_tools(usuario, retriever, self.long_term, self.derivaciones)

        prompt = ChatPromptTemplate.from_messages([
            ("system", prompts.SYSTEM_AGENTE),
            MessagesPlaceholder("chat_history"),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ])
        agent = create_tool_calling_agent(llm, self.tools, prompt)
        self.executor = AgentExecutor(
            agent=agent,
            tools=self.tools,
            max_iterations=settings.max_iterations,  # corta el ciclo ReAct si no converge
            return_intermediate_steps=True,
            handle_parsing_errors=True,
        )

    # ------------------------------------------------------------------ util
    def historial_completo(self) -> list[BaseMessage]:
        """Todo lo guardado en disco para esta sesión (no solo la ventana)."""
        return self.memory.chat_memory.messages

    def _datos_usuario(self) -> str:
        return self.long_term.como_texto(self.usuario)

    def _ejecutar(self, entrada: str, chat_history: list[BaseMessage]) -> dict:
        return self.executor.invoke({
            "input": entrada,
            "chat_history": chat_history,
            # Se lee en cada llamada: si una tool guardó un dato, el siguiente paso ya lo ve.
            "datos_usuario": self._datos_usuario(),
        })

    # -------------------------------------------------------------- planificar
    def planificar(self, pregunta: str, chat_history: list[BaseMessage]) -> dict:
        mensajes = [SystemMessage(prompts.SYSTEM_PLANIFICADOR.format(datos_usuario=self._datos_usuario()))]
        mensajes += chat_history[-4:]  # contexto reciente para detectar respuestas a preguntas previas
        mensajes.append(HumanMessage(pregunta))
        return parse_plan(_texto(self.llm.invoke(mensajes).content))

    # ------------------------------------------------------------------ turno
    def responder(self, pregunta: str) -> Turno:
        turno = Turno(pregunta=pregunta)
        # 1. Cargar memoria de corto plazo (solo las últimas k interacciones).
        chat_history = self.memory.load_memory_variables({})["chat_history"]

        # 2. Decidir estrategia.
        plan = self.planificar(pregunta, chat_history)
        turno.motivo_plan = plan["motivo"]

        if not plan["requiere_plan"]:
            resultado = self._ejecutar(pregunta, chat_history)
            turno.modo = "react"
            turno.respuesta = _texto(resultado["output"])
            turno.tools = _tool_calls(resultado.get("intermediate_steps", []))
        else:
            turno.modo = "plan"
            self._ejecutar_plan(turno, plan["pasos"], chat_history)

        # 3. Guardar en memoria y en la traza.
        self.memory.save_context({"input": pregunta}, {"output": turno.respuesta})
        self._registrar_traza(turno)
        return turno

    def _ejecutar_plan(self, turno: Turno, pasos: list[dict], chat_history: list[BaseMessage]) -> None:
        turno.pasos = [{"descripcion": p["descripcion"], "herramienta": p.get("herramienta", ""),
                        "estado": "pendiente", "resultado": "", "tools": []} for p in pasos]
        plan_txt = "\n".join(f"{i}. {p['descripcion']}" for i, p in enumerate(turno.pasos, 1))
        resultados: list[str] = []

        for i, paso in enumerate(turno.pasos, 1):
            entrada = prompts.PASO_PLAN.format(
                pregunta=turno.pregunta, plan=plan_txt,
                resultados="\n".join(resultados) or "(ninguno todavía)",
                numero=i, descripcion=paso["descripcion"],
            )
            r = self._ejecutar(entrada, chat_history)
            salida = _texto(r["output"]).strip()
            paso["tools"] = _tool_calls(r.get("intermediate_steps", []))
            paso["resultado"] = salida
            if "FALTA_DATO" in salida[:60].upper():  # tolera formato extra del modelo (ej. **FALTA_DATO:**)
                # Condición cambiante: falta información -> se detiene el plan y se pregunta.
                paso["estado"] = "bloqueado"
                for resto in turno.pasos[i:]:
                    resto["estado"] = "omitido"
                resultados.append(f"Paso {i} bloqueado: {salida}")
                break
            paso["estado"] = "completado"
            resultados.append(f"Paso {i} ({paso['descripcion']}): {salida}")

        sintesis = self.llm.invoke([
            SystemMessage(prompts.SYSTEM_SINTESIS.format(datos_usuario=self._datos_usuario())),
            HumanMessage(f"Pregunta del estudiante: {turno.pregunta}\n\nResultados:\n" + "\n\n".join(resultados)),
        ])
        turno.respuesta = _texto(sintesis.content)

    def _registrar_traza(self, turno: Turno) -> None:
        path = self.settings.traces_path
        path.parent.mkdir(parents=True, exist_ok=True)
        registro = {"fecha": datetime.now().isoformat(timespec="seconds"), "usuario": self.usuario,
                    "sesion": self.session_id, "modelo": self.settings.llm_model, **turno.as_dict()}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
