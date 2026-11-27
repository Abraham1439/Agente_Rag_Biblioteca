"""Instrumentación del agente (EP3): logs, trazas y métricas.

Tres pilares de la observabilidad, unidos por el mismo `trace_id` (uuid4, uno por turno):

1. Eventos  -> storage/eventos.log   (JSON por línea, hora UTC con milisegundos)
   solicitud_recibida, plan_decidido, guardrail, respuesta_enviada, error.
2. Spans    -> storage/spans.jsonl   (uno por cada paso del turno)
   - llm:         cada llamada al modelo (planificador, agente, síntesis), con tokens, costo,
                  herramientas que pidió, último sub-prompt y respuesta intermedia.
   - herramienta: cada tool (entradas, salida, duración, error).
   - retriever:   cada búsqueda en FAISS (mejor score, artículos recuperados).
3. Métricas por turno -> storage/trazas.jsonl (latencia total, llamadas al LLM, tokens, costo).

Los spans se capturan con un callback de LangChain (`Trazador`), sin modificar las tools.
Si LANGSMITH_TRACING=true y LANGSMITH_API_KEY están en .env, LangChain envía además la
misma ejecución a LangSmith; el trace_id viaja como metadato para cruzar ambos registros.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langchain_core.callbacks import BaseCallbackHandler

from .seguridad import FiltroPII

TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
TRAZADOR_ACTUAL: contextvars.ContextVar["Trazador | None"] = contextvars.ContextVar("trazador", default=None)

_lock_archivos = threading.Lock()


def ahora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def nuevo_trace_id() -> str:
    return str(uuid.uuid4())


# ------------------------------------------------------------------------- eventos

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                           "nivel": record.levelname, "evento": record.getMessage(),
                           **getattr(record, "campos", {})}, ensure_ascii=False, default=str)


def crear_logger(archivo: Path, enmascarar_pii: bool) -> logging.Logger:
    """Logger de eventos en JSON. Con `enmascarar_pii` (v2) todo campo de texto pasa por FiltroPII."""
    archivo = Path(archivo)
    archivo.parent.mkdir(parents=True, exist_ok=True)
    nombre = f"biblioteca.eventos.{archivo.resolve()}.{'pii' if enmascarar_pii else 'raw'}"
    logger = logging.getLogger(nombre)
    if not logger.handlers:  # un handler por archivo y modo, aunque se creen muchos agentes
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = logging.FileHandler(archivo, mode="a", encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        if enmascarar_pii:
            handler.addFilter(FiltroPII())
        logger.addHandler(handler)
    return logger


def log_evento(logger: logging.Logger, evento: str, nivel: int = logging.INFO, **campos: Any) -> None:
    logger.log(nivel, evento, extra={"campos": {"trace_id": TRACE_ID.get(), **campos}})


# --------------------------------------------------------------------------- spans

def _texto(contenido: Any) -> str:
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):  # algunos modelos devuelven bloques de contenido
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in contenido)
    return json.dumps(contenido, ensure_ascii=False, default=str)


class Trazador(BaseCallbackHandler):
    """Callback de LangChain que guarda un span por cada llamada al LLM y a cada herramienta.

    `componente` se toma de los tags de la ejecución: "planificador", "agente", "sintesis" o "juez".
    `sanitizar` se aplica a todo texto antes de guardarlo (v2: enmascarado de PII)."""

    raise_error = False  # un error de observabilidad nunca debe botar al agente

    def __init__(self, trace_id: str, version: str, modelo: str, precio_entrada: float, precio_salida: float,
                 sanitizar: Callable[[str], str] = lambda s: s, max_chars: int = 600):
        self.trace_id, self.version, self.modelo = trace_id, version, modelo
        self.precio_entrada, self.precio_salida = precio_entrada, precio_salida
        self.sanitizar, self.max_chars = sanitizar, max_chars
        self.t_inicio = time.perf_counter()
        self._t0: dict[Any, float] = {}
        self._meta: dict[Any, dict] = {}
        self.spans: list[dict] = []
        self._lock = threading.Lock()

    # -- utilidades
    def _limpio(self, x: Any, n: int | None = None) -> str:
        return self.sanitizar(_texto(x))[: n or self.max_chars]

    def costo(self, tokens_entrada: int, tokens_salida: int) -> float:
        return (tokens_entrada * self.precio_entrada + tokens_salida * self.precio_salida) / 1e6

    def _cerrar(self, run_id, parent_run_id, **campos) -> None:
        ini = self._t0.pop(run_id, time.perf_counter())
        fin = time.perf_counter()
        span = {"trace_id": self.trace_id, "version": self.version, "span_id": str(run_id)[:8],
                "padre": str(parent_run_id)[:8] if parent_run_id else None,
                "inicio_ms": round((ini - self.t_inicio) * 1000, 1), "duracion_ms": round((fin - ini) * 1000, 1),
                **self._meta.pop(run_id, {}), **campos}
        with self._lock:
            self.spans.append(span)

    # -- LLM
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, **kw):
        self._t0[run_id] = time.perf_counter()
        ultimo = messages[0][-1] if messages and messages[0] else None
        componente = next((t for t in (tags or []) if t in {"planificador", "agente", "sintesis", "juez"}), "agente")
        self._meta[run_id] = {"tipo": "llm", "componente": componente,
                              "mensajes_en_contexto": len(messages[0]) if messages else 0,
                              "sub_prompt": self._limpio(f"[{ultimo.type}] {_texto(ultimo.content)}") if ultimo else ""}

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kw):
        gen = response.generations[0][0] if response.generations and response.generations[0] else None
        msg = getattr(gen, "message", None)
        uso = (getattr(msg, "usage_metadata", None) or {}) if msg is not None else {}
        if not uso and response.llm_output:  # respaldo: formato de OpenAI
            u = response.llm_output.get("token_usage") or {}
            uso = {"input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0)}
        t_in, t_out = int(uso.get("input_tokens", 0) or 0), int(uso.get("output_tokens", 0) or 0)
        detalles = uso.get("output_token_details") or {}
        self._cerrar(run_id, parent_run_id, nombre=self.modelo, ok=True,
                     tokens_entrada=t_in, tokens_salida=t_out,
                     tokens_razonamiento=int(detalles.get("reasoning", 0) or 0),
                     costo_usd=self.costo(t_in, t_out),
                     herramientas_pedidas=[t["name"] for t in (getattr(msg, "tool_calls", None) or [])],
                     respuesta_intermedia=self._limpio(getattr(msg, "content", "") if msg else (gen.text if gen else "")))

    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kw):
        self._cerrar(run_id, parent_run_id, nombre=self.modelo, ok=False, tokens_entrada=0, tokens_salida=0,
                     costo_usd=0.0, error=f"{type(error).__name__}: {self._limpio(str(error), 300)}")

    # -- herramientas
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kw):
        self._t0[run_id] = time.perf_counter()
        self._meta[run_id] = {"tipo": "herramienta", "componente": "herramienta",
                              "nombre": (serialized or {}).get("name") or kw.get("name"),
                              "entradas": self._limpio(kw.get("inputs") or input_str, 300)}

    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kw):
        salida = _texto(getattr(output, "content", output))
        # MONTO, NO_APLICA, SIN_RESPALDO, ERROR, GUARDADO, TICKET... o FRAGMENTOS si devolvió artículos
        estado = "FRAGMENTOS" if salida.startswith("[") else salida.split(":", 1)[0].split(" ", 1)[0].strip().upper()[:20]
        self._cerrar(run_id, parent_run_id, ok=not salida.startswith("ERROR"), estado=estado,
                     salida=self._limpio(salida, 400))

    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kw):
        self._cerrar(run_id, parent_run_id, ok=False, estado="EXCEPCION",
                     error=f"{type(error).__name__}: {self._limpio(str(error), 300)}")

    # -- spans manuales (retriever, guardrails)
    @contextlib.contextmanager
    def span(self, tipo: str, nombre: str, **campos):
        run_id = uuid.uuid4()
        self._t0[run_id] = time.perf_counter()
        self._meta[run_id] = {"tipo": tipo, "componente": tipo, "nombre": nombre}
        extra: dict = {}
        try:
            yield extra
            self._cerrar(run_id, None, ok=True, **campos, **extra)
        except Exception as exc:
            self._cerrar(run_id, None, ok=False, error=f"{type(exc).__name__}: {self._limpio(str(exc), 300)}",
                         **campos, **extra)
            raise

    # -- resumen y persistencia
    def resumen(self) -> dict:
        llm = [s for s in self.spans if s.get("tipo") == "llm"]
        herr = [s for s in self.spans if s.get("tipo") == "herramienta"]
        t_in = sum(s.get("tokens_entrada", 0) for s in llm)
        t_out = sum(s.get("tokens_salida", 0) for s in llm)
        return {"llamadas_llm": len(llm),
                "llamadas_llm_por_componente": {c: sum(s["componente"] == c for s in llm)
                                                for c in sorted({s["componente"] for s in llm})},
                "llamadas_herramientas": len(herr),
                "errores_llm": sum(not s.get("ok", True) for s in llm),
                "errores_herramientas": sum(not s.get("ok", True) for s in herr),
                "tokens_entrada": t_in, "tokens_salida": t_out,
                "tokens_razonamiento": sum(s.get("tokens_razonamiento", 0) for s in llm),
                "costo_usd": self.costo(t_in, t_out)}

    def guardar(self, archivo: Path) -> None:
        archivo = Path(archivo)
        archivo.parent.mkdir(parents=True, exist_ok=True)
        with _lock_archivos, archivo.open("a", encoding="utf-8") as f:
            for s in sorted(self.spans, key=lambda s: s["inicio_ms"]):
                f.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")


@contextlib.contextmanager
def span_actual(tipo: str, nombre: str, **campos):
    """Abre un span manual en el trazador del turno en curso (si hay uno)."""
    trazador = TRAZADOR_ACTUAL.get()
    if trazador is None:
        yield {}
        return
    with trazador.span(tipo, nombre, **campos) as extra:
        yield extra


def escribir_jsonl(archivo: Path, registro: dict) -> None:
    archivo = Path(archivo)
    archivo.parent.mkdir(parents=True, exist_ok=True)
    with _lock_archivos, archivo.open("a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")
