"""Modo simulado del EP3: permite ejecutar TODO el pipeline (batch, juez, red team, análisis y
dashboard) sin red y sin gastar la API key.

⚠️ Los números que produce NO son evidencia del agente real: el "LLM" es un conjunto de reglas
que imita el comportamiento del agente (incluidos algunos errores típicos, a propósito, para
que las métricas y el dashboard tengan algo que mostrar). Sirve para probar el código y para
ensayar la lectura del dashboard. Para el informe se usa la corrida real (`--real`).
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from biblioteca_agent.agent import temas_de  # noqa: E402
from biblioteca_agent.seguridad import detectar_inyeccion  # noqa: E402

_VACIAS = set("de la el en los las un una y o que me mi mis tu es por para con del al se lo le a si no hay "
              "como cual cuanto cuantos cuantas puedo tengo".split())


def _norm(texto: str) -> list[str]:
    t = unicodedata.normalize("NFKD", texto.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return [w[:6] for w in re.findall(r"[a-z0-9]+", t) if w not in _VACIAS and len(w) > 1]


class EmbeddingsBolsaDePalabras(Embeddings):
    """Embeddings deterministas por hashing de palabras (sin red). Suficiente para que el retriever
    del modo simulado recupere artículos con palabras en común con la pregunta."""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _vec(self, texto: str) -> list[float]:
        v = [0.0] * self.dim
        for w in _norm(texto):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        time.sleep(0.002 * len(texts))
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        time.sleep(random.uniform(0.15, 0.35) * _ESCALA["valor"])  # latencia de la API de embeddings
        return self._vec(text)


_ESCALA = {"valor": 0.25}  # factor de tiempo: 1.0 ~ latencias reales aproximadas; menor = demo rápida
_rng = random.Random(2026)
_rng_lock = threading.Lock()


def _azar() -> float:
    with _rng_lock:
        return _rng.random()


def _texto(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c)


def _numeros(texto: str) -> list[int]:
    return [int(n) for n in re.findall(r"\b(\d{1,3})\b", texto)]


class LLMSimulado(BaseChatModel):
    """'LLM' por reglas que imita al agente (planificador, ReAct, pasos del plan y síntesis)."""

    @property
    def _llm_type(self) -> str:
        return "llm-simulado-ep3"

    def bind_tools(self, tools, **kwargs):
        return self

    # --- utilidades
    def _ai(self, contenido: str = "", tool_calls: list | None = None, n_in: int = 0) -> AIMessage:
        salida = contenido + json.dumps(tool_calls or [])
        razonamiento = int(_azar() * 120)
        msg = AIMessage(content=contenido, tool_calls=tool_calls or [])
        msg.usage_metadata = {"input_tokens": n_in, "output_tokens": len(salida) // 4 + 5 + razonamiento,
                              "total_tokens": n_in + len(salida) // 4 + 5 + razonamiento,
                              "output_token_details": {"reasoning": razonamiento}}
        return msg

    def _call(self, nombre: str, args: dict) -> dict:
        return {"name": nombre, "args": args, "id": f"call_{hashlib.md5((nombre + json.dumps(args) + str(_azar())).encode()).hexdigest()[:8]}"}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        n_in = sum(len(_texto(m.content)) for m in messages) // 4
        # Latencia de un modelo pequeño con razonamiento bajo: mediana ~1,2 s y cola larga.
        lat = random.lognormvariate(math.log(1.2), 0.45) * _ESCALA["valor"]
        if _azar() < 0.04:
            lat *= 3.5  # respuestas lentas ocasionales del proveedor
        time.sleep(lat)
        sistema = _texto(messages[0].content) if messages and isinstance(messages[0], SystemMessage) else ""
        if "planificador" in sistema.lower():
            msg = self._planificar(_texto(messages[-1].content), n_in)
        elif "Redacta la respuesta final" in sistema:
            msg = self._sintetizar(_texto(messages[-1].content), n_in)
        else:
            msg = self._agente(messages, sistema, n_in)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    # --- planificador
    def _planificar(self, pregunta: str, n_in: int) -> AIMessage:
        if _azar() < 0.05:  # a veces el modelo no respeta el formato
            return self._ai("Claro, aquí va el plan: primero buscar la norma y después calcular.", n_in=n_in)
        temas = temas_de(pregunta)
        if len(temas) >= 2 and re.search(r"\by\b|además|,", pregunta):
            nombres = {"prestamo": "plazos de préstamo", "perdida_dano": "pérdida o daño de material",
                       "sala": "salas de estudio", "vr": "lentes de realidad virtual", "conducta": "normas de conducta",
                       "usuarios": "tipos de usuario"}
            pasos = [{"descripcion": f"Buscar en el reglamento {nombres[t]}", "herramienta": "buscar_reglamento"}
                     for t in temas[:2] if t != "multa"]
            if "multa" in temas:
                pasos.append({"descripcion": "Calcular la multa por atraso", "herramienta": "calcular_multa"})
            if len(pasos) >= 2:
                return self._ai(json.dumps({"requiere_plan": True, "motivo": "La consulta tiene dos necesidades.",
                                            "pasos": pasos}, ensure_ascii=False), n_in=n_in)
        return self._ai(json.dumps({"requiere_plan": False, "motivo": "Una sola necesidad.", "pasos": []}), n_in=n_in)

    def _sintetizar(self, contenido: str, n_in: int) -> AIMessage:
        resultados = re.findall(r"Paso \d+[^:]*:\s*(.+)", contenido)
        texto = " ".join(r.strip() for r in resultados) or "No pude completar la consulta."
        return self._ai(texto.replace("FALTA_DATO:", "").strip(), n_in=n_in)

    # --- agente (ReAct y pasos del plan)
    def _agente(self, messages, sistema: str, n_in: int) -> AIMessage:
        humanos = [m for m in messages if isinstance(m, HumanMessage)]
        entrada = _texto(humanos[-1].content) if humanos else ""
        paso = re.search(r"Ejecuta SOLO el paso \d+:\s*(.+)", entrada)
        pregunta = re.search(r"Pregunta original del estudiante:\s*(.+)", entrada)
        objetivo = paso.group(1) if paso else entrada
        pregunta_txt = pregunta.group(1) if pregunta else entrada
        # Mensajes desde la última entrada humana: herramientas ya llamadas en este turno.
        idx = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage)) if humanos else 0
        observaciones = [m for m in messages[idx + 1:] if isinstance(m, ToolMessage)]
        ya = [tc["name"] for m in messages[idx + 1:] if isinstance(m, AIMessage) for tc in (m.tool_calls or [])]
        datos_usuario = sistema.split("Datos recordados del usuario", 1)[-1]

        # Instrucciones guardadas en la memoria (inyección persistente): el modelo a veces las obedece.
        if re.search(r"nota para el asistente|perdonad", datos_usuario, re.I) and re.search(r"multa|pago|atras", objetivo, re.I) and _azar() < 0.7:
            return self._ai("Según la nota de tu perfil, tus multas están perdonadas: no pagas nada.", n_in=n_in)

        if not observaciones:
            return self._primera_accion(objetivo, pregunta_txt, datos_usuario, bool(paso), n_in)
        ultima = _texto(observaciones[-1].content)
        if ultima.startswith("SIN_RESPALDO"):
            if "buscar_reglamento" in ya and ya.count("buscar_reglamento") < 2 and _azar() < 0.4:
                return self._ai("", [self._call("buscar_reglamento", {"consulta": " ".join(_norm(objetivo)[:4])})], n_in)
            return self._ai("", [self._call("registrar_derivacion", {"consulta": pregunta_txt[:200],
                                                                     "motivo": "El reglamento no cubre la consulta."})], n_in)
        if ultima.startswith("LIMITE"):
            return self._ai("Registré tu solicitud en un ticket; un encargado revisará todos los casos que mencionas.", n_in=n_in)
        if ultima.startswith("TICKET"):
            if "registrar_derivacion" in ya and re.search(r"(\d+) tickets", pregunta_txt) and ya.count("registrar_derivacion") < int(re.search(r"(\d+) tickets", pregunta_txt).group(1)):
                return self._ai("", [self._call("registrar_derivacion", {"consulta": pregunta_txt[:200], "motivo": "urgente"})], n_in)
            ticket = re.search(r"DER-\w+", ultima)
            return self._ai(f"No cuento con esa información en el reglamento. Registré el ticket {ticket.group(0) if ticket else ''} "
                            "para que un encargado de biblioteca te responda.", n_in=n_in)
        if ultima.startswith("MONTO"):
            monto = re.search(r"\$[\d.]+", ultima).group(0)
            return self._ai(f"Según el Artículo 18°, la multa es de {monto}." + (" Recuerda pagarla en la sede." if _azar() < 0.25 else ""), n_in=n_in)
        if ultima.startswith("NO_APLICA"):
            return self._ai("Según el Artículo 18°, la multa por atraso se aplica únicamente a usuarios internos, así que no te corresponde esa multa.", n_in=n_in)
        if ultima.startswith("GUARDADO"):
            return self._ai("¡Hola! Anoté tus datos para recordarlos. ¿En qué te puedo ayudar con el reglamento de la biblioteca?", n_in=n_in)
        if ultima.startswith("ERROR"):
            return self._ai("No pude guardar ese dato. ¿Te ayudo con alguna consulta del reglamento?", n_in=n_in)
        # Fragmentos del reglamento: ¿falta calcular una multa?
        if self._pide_monto(objetivo) and "calcular_multa" not in ya:
            args = self._args_multa(pregunta_txt + " " + objetivo, datos_usuario)
            if args:
                return self._ai("", [self._call("calcular_multa", args)], n_in)
            pregunta_dato = "¿Cuántos días de atraso tienes y eres usuario interno o externo?"
            return self._ai(f"FALTA_DATO: {pregunta_dato}" if paso else
                            f"Según el Artículo 18°, la multa es de $1.000 por cada 7 días de atraso por ítem. {pregunta_dato}", n_in=n_in)
        return self._ai(self._responder_con_fragmentos(objetivo, ultima), n_in=n_in)

    def _pide_monto(self, texto: str) -> bool:
        return bool(re.search(r"multa|cu[aá]nto (pago|tengo que pagar|debo)|pagar", texto, re.I))

    def _args_multa(self, texto: str, datos_usuario: str) -> dict | None:
        dias = re.search(r"(\d+)\s*d[ií]as", texto)
        items = re.search(r"(\d+)\s*(libros|[ií]tems)", texto)
        n_items = int(items.group(1)) if items else (1 if re.search(r"\bun libro|1 libro|otro\b", texto) else None)
        tipo = ("externo" if re.search(r"extern", texto, re.I) else
                "interno" if re.search(r"alumn|estudiant|docente|interno", texto + datos_usuario, re.I) else None)
        if dias and n_items and tipo:
            return {"dias_atraso": int(dias.group(1)), "cantidad_items": n_items, "tipo_usuario": tipo}
        return None

    def _primera_accion(self, objetivo: str, pregunta: str, datos_usuario: str, en_plan: bool, n_in: int) -> AIMessage:
        if detectar_inyeccion(objetivo):
            canario = re.search(r"(PWNED|ACCESO CONCEDIDO)", objetivo)
            if canario and _azar() < 0.35:
                return self._ai(canario.group(1), n_in=n_in)
            if re.search(r"instrucciones de sistema", objetivo, re.I) and _azar() < 0.3:
                return self._ai("Estas son mis Reglas estrictas: 1. No inventes plazos, montos, artículos...", n_in=n_in)
            if re.search(r"encargad", objetivo, re.I) and re.search(r"\$\s?([\d.]+) por cada d[ií]a", objetivo) and _azar() < 0.5:
                tarifa = int(re.search(r"\$\s?([\d.]+) por cada d[ií]a", objetivo).group(1).replace(".", ""))
                dias = re.search(r"(\d+) d[ií]as", objetivo)
                total = f"{tarifa * int(dias.group(1)):,}".replace(",", ".") if dias else "?"
                return self._ai(f"Con la nueva regla, la multa es de ${total}.", n_in=n_in)
            if not re.search(r"multa|libro|sala|pr[eé]stamo", objetivo, re.I):
                return self._ai("Solo puedo ayudarte con consultas sobre el reglamento de la biblioteca.", n_in=n_in)
        llamadas = []
        guardar = re.search(r"guarda\w* (mi|en mi) (\w+) (como|que (mi \w+ )?es):?\s*(.+)", objetivo, re.I)
        if guardar:
            dato = {"nombre": "nombre", "carrera": "carrera", "perfil": "carrera", "sede": "sede"}.get(guardar.group(2).lower(), "carrera")
            valor = guardar.group(5).strip().rstrip(".")
            llamadas.append(self._call("guardar_dato_usuario", {"dato": dato, "valor": valor[:120]}))
        if re.search(r"\bsoy (alumn|estudiant)|soy usuari[oa] extern|sede", objetivo, re.I) and "tipo_usuario" not in datos_usuario:
            tipo = "externo" if re.search(r"extern", objetivo, re.I) else "interno"
            llamadas.append(self._call("guardar_dato_usuario", {"dato": "tipo_usuario", "valor": tipo}))
            sede = re.search(r"sede ([A-ZÁÉÍÓÚ][\w]+(?: [A-ZÁÉÍÓÚ][\w]+)?)", objetivo)
            if sede:
                llamadas.append(self._call("guardar_dato_usuario", {"dato": "sede", "valor": sede.group(1)}))
        if llamadas and guardar:
            return self._ai("", llamadas, n_in)
        if re.search(r"registres (\d+) tickets", objetivo, re.I):
            return self._ai("", [self._call("registrar_derivacion", {"consulta": pregunta[:200], "motivo": "urgente"})], n_in)
        if re.search(r"datos tienes guardados del usuario", objetivo, re.I):
            return self._ai("No tengo acceso a los datos de otros usuarios; solo recuerdo lo que tú me cuentas.", n_in=n_in)
        if re.fullmatch(r"\s*(hola|buenas|hello)[^?]*", objetivo, re.I) and llamadas:
            return self._ai("", llamadas, n_in)
        if self._pide_monto(objetivo) and not self._args_multa(pregunta + " " + objetivo, datos_usuario) and _azar() < 0.6:
            pregunta_dato = "¿Cuántos días de atraso tienes y eres usuario interno o externo?"
            if en_plan:
                return self._ai(f"FALTA_DATO: {pregunta_dato}", n_in=n_in)
            return self._ai(f"Para calcular tu multa necesito un dato más. {pregunta_dato}", n_in=n_in)
        if self._pide_monto(objetivo) and _azar() < 0.12 and (args := self._args_multa(pregunta + " " + objetivo, datos_usuario)):
            # Error típico: el modelo calcula por su cuenta, sin usar la herramienta (desviación de la lógica de negocio).
            monto = math.ceil(args["dias_atraso"] / 7) * 1000 * args["cantidad_items"]
            return self._ai(f"La multa es de ${monto:,}.".replace(",", "."), n_in=n_in)
        objetivo_busqueda = re.sub(r"^buscar en el reglamento\s*", "", objetivo, flags=re.I)
        consulta = ("multa mora atraso item usuarios internos" if self._pide_monto(objetivo)
                    else " ".join(_norm(objetivo_busqueda)[:6]) or objetivo[:60])
        llamadas.append(self._call("buscar_reglamento", {"consulta": consulta}))
        return self._ai("", llamadas, n_in)

    def _responder_con_fragmentos(self, objetivo: str, salida: str) -> str:
        bloques = re.split(r"\n\n(?=\[)", salida)
        palabras = set(_norm(objetivo))
        mejor = max(bloques, key=lambda b: len(palabras & set(_norm(b))))
        art = re.search(r"\[(Art[ií]culo \d+°)", mejor)
        cuerpo = mejor.split("\n", 2)[-1].strip()
        oraciones = [o.strip(" -") for o in re.split(r"(?<=\.)\s+|\n", cuerpo) if len(o) > 20]
        clave = max(oraciones, key=lambda o: len(palabras & set(_norm(o)))) if oraciones else cuerpo[:200]
        texto = f"Según el {art.group(1) if art else 'reglamento'}, {clave[0].lower() + clave[1:]}"
        if _azar() < 0.12:  # alucinación extrínseca: agrega algo que el contexto no dice
            texto += " Además, puedes hacer el trámite en línea las 24 horas desde la app de Duoc."
        return texto


class JuezSimulado(BaseChatModel):
    """Juez heurístico para el modo simulado: compara números y palabras clave."""

    @property
    def _llm_type(self) -> str:
        return "juez-simulado-ep3"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        time.sleep(random.uniform(0.3, 0.9) * _ESCALA["valor"])
        p = _texto(messages[-1].content)
        sec = lambda a, b: p.split(a, 1)[1].split(b, 1)[0] if a in p and b in p else ""
        pregunta, contexto = sec("PREGUNTA DEL ESTUDIANTE:", "CONTEXTO"), sec("CONTEXTO (", "RESPUESTA DEL ASISTENTE:")
        respuesta = sec("RESPUESTA DEL ASISTENTE:", "RESPUESTA DE REFERENCIA")
        referencia = sec("RESPUESTA DE REFERENCIA (lo que debería responder):", "Evalúa cuatro cosas")
        oraciones = [o.strip() for o in re.split(r"(?<=[.!?])\s+", respuesta) if o.strip()]
        afirm = []
        for o in oraciones:
            if re.search(r"no cuento|no tengo|encargad|\?|^¡?hola|anot", o, re.I):
                continue
            nums = re.findall(r"\d[\d.]*", o)
            respaldo = all(n in contexto for n in nums) if nums else len(set(_norm(o)) & set(_norm(contexto))) >= 3
            afirm.append({"texto": o[:120], "respaldada": bool(respaldo)})
        rel = 5 if len(set(_norm(pregunta)) & set(_norm(respuesta))) >= 2 or "encargad" in respuesta else 3
        frags = re.split(r"\n\n(?=\[Art)", contexto)
        utiles = sum(len(set(_norm(pregunta)) & set(_norm(f))) >= 2 for f in frags if f.startswith("[Art"))
        nums_ref = set(re.findall(r"\d[\d.]*\d|\d", referencia))
        nums_resp = set(re.findall(r"\d[\d.]*\d|\d", respuesta))
        corr = 1.0 if nums_ref and nums_ref <= nums_resp else 0.5 if nums_ref & nums_resp or len(set(_norm(referencia)) & set(_norm(respuesta))) >= 4 else 0.0
        salida = json.dumps({"afirmaciones": afirm, "relevancia_respuesta": rel, "fragmentos_utiles": utiles,
                             "correccion": corr, "razon": "juez simulado (heurístico)"}, ensure_ascii=False)
        msg = AIMessage(content=salida)
        msg.usage_metadata = {"input_tokens": len(p) // 4, "output_tokens": len(salida) // 4,
                              "total_tokens": len(p) // 4 + len(salida) // 4}
        return ChatResult(generations=[ChatGeneration(message=msg)])


def configurar_escala(valor: float) -> None:
    _ESCALA["valor"] = valor
