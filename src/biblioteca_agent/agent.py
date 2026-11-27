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

EP3 · Observabilidad y versiones (AGENT_VERSION en .env):
- Ambas versiones generan trace_id, eventos JSON, un span por cada llamada al LLM, tool y
  retriever, y métricas por turno (latencia, llamadas al LLM, tokens, costo).
- v1 = el agente del EP2 sin cambios de comportamiento (línea base para comparar).
- v2 = rediseño basado en lo observado:
    * guardrail de entrada: enmascara PII antes del LLM y bloquea patrones de inyección;
    * router determinista: el planificador (una llamada al LLM) solo se usa si la consulta
      parece tener dos o más temas;
    * planificador con salida JSON forzada (response_format) para evitar fallas de parseo;
    * regla del Art. 1° en el prompt: alumnos y docentes son usuarios internos (hallazgo E05/E06/E18);
    * caché de búsquedas en el retriever;
    * memoria de largo plazo que rechaza valores con instrucciones o PII;
    * tope por turno para las tools de escritura (1 ticket, 5 datos de memoria);
    * guardrail de salida y registros enmascarados.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

from langchain_classic.agents import AgentExecutor, create_tool_calling_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from . import prompts
from .config import Settings
from .memory import DerivacionStore, LongTermMemory, crear_memoria_corto_plazo
from .observabilidad import (TRACE_ID, TRAZADOR_ACTUAL, Trazador, ahora_utc, crear_logger, escribir_jsonl,
                             log_evento, nuevo_trace_id)
from .rag import ReglamentoRetriever
from .seguridad import (MENSAJE_BLOQUEO, contiene_pii, detectar_inyeccion, enmascarar, enmascarar_obj,
                        revisar_salida)
from .tools import USOS_ESCRITURA, build_tools

MAX_PASOS = 4
MENSAJE_ERROR = ("Lo siento, tuve un problema técnico al procesar tu consulta. Intenta de nuevo en unos "
                 "minutos o acércate a un encargado de biblioteca.")


def build_llm(settings: Settings, modelo: str | None = None, esfuerzo: str | None = None) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    if not settings.llm_api_key:
        raise RuntimeError("Falta OPENROUTER_API_KEY en el archivo .env")
    return ChatOpenAI(
        model=modelo or settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        timeout=settings.llm_timeout,
        max_retries=2,
        # Esfuerzo de razonamiento bajo (recomendación del docente por costo).
        extra_body={"reasoning": {"effort": esfuerzo or settings.llm_reasoning_effort}},
    )


def _texto(contenido) -> str:
    """Normaliza el contenido de un mensaje (algunos modelos devuelven listas de bloques)."""
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in contenido)
    return str(contenido)


def parse_plan(texto: str) -> dict:
    """Extrae el JSON del planificador. Si no se puede leer, se cae a modo ReAct.

    EP3: `parse_ok` indica si el JSON se pudo leer; las fallas de parseo se cuentan en el análisis."""
    match = re.search(r"\{.*\}", texto, re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
        parse_ok = bool(match) and isinstance(data, dict)
    except json.JSONDecodeError:
        data, parse_ok = {}, False
    if not isinstance(data, dict):
        data = {}
    pasos = [p for p in data.get("pasos", []) if isinstance(p, dict) and p.get("descripcion")]
    requiere = bool(data.get("requiere_plan")) and len(pasos) >= 2
    return {
        "requiere_plan": requiere,
        "motivo": data.get("motivo", "No se pudo interpretar el plan; se usa ReAct." if not data else ""),
        "pasos": pasos[:MAX_PASOS] if requiere else [],
        "parse_ok": parse_ok,
    }


# Temas del reglamento para el router determinista de la v2. Si una consulta toca dos o más,
# se consulta al planificador; si toca uno o ninguno, se va directo a ReAct (una llamada menos).
TEMAS = {
    "prestamo": r"pr[eé]stam|prestad|pedir (un|el) libro|retir|renov|cu[aá]nto tiempo|plazo|colecci",
    "multa": r"multa|atras|pag[oa]r?\b|cobr|mora|moros",
    "perdida_dano": r"perd[ií]|p[eé]rdida|da[ñn]|romp|rot[oa]|repon",
    "sala": r"\bsalas?\b|reserv",
    "vr": r"lentes|\bvr\b|realidad virtual",
    "conducta": r"comer|comida|alimento|beb(er|ida)|ruido|m[uú]sica|sanci|desalojo",
    "usuarios": r"extern|titulad|convenio|docente|colaborador",
}
_TEMAS = {k: re.compile(v, re.IGNORECASE) for k, v in TEMAS.items()}


def temas_de(texto: str) -> list[str]:
    return [k for k, p in _TEMAS.items() if p.search(texto)]


def requiere_planificador(texto: str) -> bool:
    """Router determinista (v2): solo vale la pena planificar si hay dos o más temas."""
    return len(temas_de(texto)) >= 2


@dataclass
class Turno:
    pregunta: str
    respuesta: str = ""
    modo: str = "react"  # "react", "plan" o "bloqueado"
    motivo_plan: str = ""
    pasos: list[dict] = field(default_factory=list)   # modo plan: descripcion, estado, resultado, tools
    tools: list[dict] = field(default_factory=list)   # modo react: llamadas a tools
    # --- EP3: observabilidad ---
    trace_id: str = ""
    version: str = "v1"
    latencia_s: float = 0.0
    metricas: dict = field(default_factory=dict)
    planificador_usado: bool = True
    plan_parse_ok: bool | None = None
    guardrails: list[str] = field(default_factory=list)
    error: str | None = None

    def todas_las_tools(self) -> list[dict]:
        llamadas = list(self.tools)
        for paso in self.pasos:
            llamadas += paso.get("tools", [])
        return llamadas

    def as_dict(self) -> dict:
        return {"pregunta": self.pregunta, "respuesta": self.respuesta, "modo": self.modo,
                "motivo_plan": self.motivo_plan, "pasos": self.pasos, "tools": self.tools,
                "trace_id": self.trace_id, "version": self.version, "latencia_s": self.latencia_s,
                "metricas": self.metricas, "planificador_usado": self.planificador_usado,
                "plan_parse_ok": self.plan_parse_ok, "guardrails": self.guardrails, "error": self.error}


def _tool_calls(intermediate_steps) -> list[dict]:
    llamadas = []
    for action, observation in intermediate_steps:
        llamadas.append({"tool": action.tool, "input": action.tool_input, "output": str(observation)})
    return llamadas


class BibliotecaAgent:
    def __init__(self, settings: Settings, llm: BaseChatModel, retriever: ReglamentoRetriever,
                 usuario: str, session_id: str):
        self.settings = settings
        self.version = settings.agent_version if settings.agent_version in {"v1", "v2"} else "v2"
        self.v2 = self.version == "v2"
        self.llm = llm
        self.retriever = retriever
        self.retriever.usar_cache = self.v2
        self.usuario = usuario
        self.session_id = session_id
        self.long_term = LongTermMemory(settings.long_term_path, validar_valores=self.v2)
        self.derivaciones = DerivacionStore(settings.derivaciones_path, seguro=self.v2)
        self.memory = crear_memoria_corto_plazo(settings.sessions_dir, session_id, settings.memory_window_k)
        self.tools = build_tools(usuario, retriever, self.long_term, self.derivaciones, limitar_escritura=self.v2)
        self.logger = crear_logger(settings.eventos_path, enmascarar_pii=self.v2)
        # v2: el planificador debe responder JSON (salida estructurada del proveedor).
        self.llm_planificador = llm.bind(response_format={"type": "json_object"}) if self.v2 else llm

        prompt = ChatPromptTemplate.from_messages([
            ("system", prompts.SYSTEM_AGENTE + (prompts.REGLA_TIPO_USUARIO if self.v2 else "")),
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
        self._trazador: Trazador | None = None

    # ------------------------------------------------------------------ util
    def historial_completo(self) -> list[BaseMessage]:
        """Todo lo guardado en disco para esta sesión (no solo la ventana)."""
        return self.memory.chat_memory.messages

    def _datos_usuario(self) -> str:
        return self.long_term.como_texto(self.usuario)

    def _config(self, componente: str) -> dict:
        """Callbacks y metadatos de cada llamada: alimentan los spans y, si está activo, LangSmith."""
        return {"callbacks": [self._trazador] if self._trazador else [], "tags": [componente],
                "metadata": {"trace_id": TRACE_ID.get(), "version": self.version, "sesion": self.session_id},
                "run_name": f"biblioteca_{componente}"}

    def _ejecutar(self, entrada: str, chat_history: list[BaseMessage]) -> dict:
        return self.executor.invoke({
            "input": entrada,
            "chat_history": chat_history,
            # Se lee en cada llamada: si una tool guardó un dato, el siguiente paso ya lo ve.
            "datos_usuario": self._datos_usuario(),
        }, config=self._config("agente"))

    # -------------------------------------------------------------- planificar
    def planificar(self, pregunta: str, chat_history: list[BaseMessage]) -> dict:
        mensajes = [SystemMessage(prompts.SYSTEM_PLANIFICADOR.format(datos_usuario=self._datos_usuario()))]
        mensajes += chat_history[-4:]  # contexto reciente para detectar respuestas a preguntas previas
        mensajes.append(HumanMessage(pregunta))
        return parse_plan(_texto(self.llm_planificador.invoke(mensajes, config=self._config("planificador")).content))

    # ---------------------------------------------------------- guardrail entrada
    def _guardrail_entrada(self, turno: Turno, pregunta: str) -> str | None:
        """v2: devuelve el texto que verá el LLM, o None si el mensaje se bloquea."""
        if len(pregunta) > self.settings.max_input_chars:
            turno.guardrails.append("entrada_truncada")
            log_evento(self.logger, "guardrail", logging.WARNING, capa="entrada", regla="longitud_maxima",
                       accion="truncar", largo=len(pregunta))
            pregunta = pregunta[: self.settings.max_input_chars]
        if contiene_pii(pregunta):
            turno.guardrails.append("pii_entrada")
            log_evento(self.logger, "guardrail", logging.WARNING, capa="entrada", regla="pii", accion="enmascarar")
            pregunta = enmascarar(pregunta)
        if patrones := detectar_inyeccion(pregunta):
            turno.guardrails.append("inyeccion")
            log_evento(self.logger, "guardrail", logging.WARNING, capa="entrada", regla="inyeccion",
                       patrones=patrones, accion="bloquear")
            return None
        return pregunta

    # ------------------------------------------------------------------ turno
    def responder(self, pregunta: str) -> Turno:
        trace_id = nuevo_trace_id()
        token_trace = TRACE_ID.set(trace_id)
        sanitizar = enmascarar if self.v2 else (lambda s: s)
        self._trazador = Trazador(trace_id, self.version, self.settings.llm_model, self.settings.precio_entrada,
                                  self.settings.precio_salida, sanitizar=sanitizar)
        token_trazador = TRAZADOR_ACTUAL.set(self._trazador)
        token_usos = USOS_ESCRITURA.set({})  # contador de tools de escritura de este turno
        turno = Turno(pregunta=sanitizar(pregunta), trace_id=trace_id, version=self.version)
        t0 = time.perf_counter()
        log_evento(self.logger, "solicitud_recibida", version=self.version, usuario=self.usuario,
                   sesion=self.session_id, mensaje=pregunta[:500], largo=len(pregunta))
        try:
            entrada = self._guardrail_entrada(turno, pregunta) if self.v2 else pregunta
            if entrada is None:
                turno.modo, turno.respuesta, turno.planificador_usado = "bloqueado", MENSAJE_BLOQUEO, False
                turno.motivo_plan = "Guardrail de entrada: patrón de prompt injection."
            else:
                self._resolver(turno, entrada)
            if self.v2:
                turno.respuesta, reglas = revisar_salida(turno.respuesta)
                for regla in reglas:
                    turno.guardrails.append(regla)
                    log_evento(self.logger, "guardrail", logging.WARNING, capa="salida", regla=regla, accion="corregir")
            # Guardar en memoria (en v2, el texto ya va enmascarado).
            self.memory.save_context({"input": entrada if entrada is not None else turno.pregunta},
                                     {"output": turno.respuesta})
        except Exception as exc:  # error técnico: se registra y se responde con un mensaje seguro
            turno.error = f"{type(exc).__name__}: {sanitizar(str(exc))[:300]}"
            turno.respuesta = MENSAJE_ERROR
            log_evento(self.logger, "error", logging.ERROR, error=turno.error)
        finally:
            turno.latencia_s = round(time.perf_counter() - t0, 3)
            turno.metricas = self._trazador.resumen()
            self._trazador.guardar(self.settings.spans_path)
            log_evento(self.logger, "respuesta_enviada", logging.ERROR if turno.error else logging.INFO,
                       version=self.version, modo=turno.modo, latencia_s=turno.latencia_s,
                       planificador_usado=turno.planificador_usado, plan_parse_ok=turno.plan_parse_ok,
                       tools=[c["tool"] for c in turno.todas_las_tools()], guardrails=turno.guardrails,
                       error=turno.error, respuesta=turno.respuesta[:500], **turno.metricas)
            self._registrar_traza(turno)
            TRAZADOR_ACTUAL.reset(token_trazador)
            USOS_ESCRITURA.reset(token_usos)
            TRACE_ID.reset(token_trace)
            self._trazador = None
        return turno

    def _resolver(self, turno: Turno, entrada: str) -> None:
        # 1. Cargar memoria de corto plazo (solo las últimas k interacciones).
        chat_history = self.memory.load_memory_variables({})["chat_history"]

        # 2. Decidir estrategia (v2: el router evita la llamada al planificador si hay un solo tema).
        if self.v2 and not requiere_planificador(entrada):
            plan = {"requiere_plan": False, "pasos": [], "parse_ok": None,
                    "motivo": f"Router: un solo tema ({', '.join(temas_de(entrada)) or 'ninguno'}); no se planifica."}
            turno.planificador_usado = False
        else:
            plan = self.planificar(entrada, chat_history)
        turno.motivo_plan, turno.plan_parse_ok = plan["motivo"], plan["parse_ok"]
        log_evento(self.logger, "plan_decidido", requiere_plan=plan["requiere_plan"], pasos=len(plan["pasos"]),
                   planificador_usado=turno.planificador_usado, plan_parse_ok=plan["parse_ok"])

        if not plan["requiere_plan"]:
            resultado = self._ejecutar(entrada, chat_history)
            turno.modo = "react"
            turno.respuesta = _texto(resultado["output"])
            turno.tools = _tool_calls(resultado.get("intermediate_steps", []))
        else:
            turno.modo = "plan"
            self._ejecutar_plan(turno, entrada, plan["pasos"], chat_history)

    def _ejecutar_plan(self, turno: Turno, entrada: str, pasos: list[dict], chat_history: list[BaseMessage]) -> None:
        turno.pasos = [{"descripcion": p["descripcion"], "herramienta": p.get("herramienta", ""),
                        "estado": "pendiente", "resultado": "", "tools": []} for p in pasos]
        plan_txt = "\n".join(f"{i}. {p['descripcion']}" for i, p in enumerate(turno.pasos, 1))
        resultados: list[str] = []

        for i, paso in enumerate(turno.pasos, 1):
            texto_paso = prompts.PASO_PLAN.format(
                pregunta=entrada, plan=plan_txt,
                resultados="\n".join(resultados) or "(ninguno todavía)",
                numero=i, descripcion=paso["descripcion"],
            )
            r = self._ejecutar(texto_paso, chat_history)
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
            HumanMessage(f"Pregunta del estudiante: {entrada}\n\nResultados:\n" + "\n\n".join(resultados)),
        ], config=self._config("sintesis"))
        turno.respuesta = _texto(sintesis.content)

    def _registrar_traza(self, turno: Turno) -> None:
        registro = {"fecha": ahora_utc(), "usuario": self.usuario, "sesion": self.session_id,
                    "modelo": self.settings.llm_model, **turno.as_dict()}
        # v2: también se enmascaran los argumentos y salidas de las tools que quedan en la traza.
        escribir_jsonl(self.settings.traces_path, enmascarar_obj(registro) if self.v2 else registro)
