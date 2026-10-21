"""Herramientas del agente.

El LLM decide qué herramienta usar leyendo el nombre, los tipos de los
argumentos y el docstring de cada función decorada con @tool. Por eso los
docstrings describen con precisión cuándo usar cada una.

- Consulta:      buscar_reglamento        (RAG sobre el reglamento)
- Razonamiento:  calcular_multa           (cálculo determinístico en Python)
- Escritura:     guardar_dato_usuario     (memoria de largo plazo)
                 registrar_derivacion     (ticket para un encargado)
"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from .memory import DATOS_PERMITIDOS, DerivacionStore, LongTermMemory
from .rag import ReglamentoRetriever

MULTA_POR_BLOQUE = 1000  # Art. 18°: $1.000 por cada 7 días de atraso, por ítem
DIAS_POR_BLOQUE = 7


def calcular_multa_atraso(dias_atraso: int, cantidad_items: int, tipo_usuario: str) -> dict:
    """Lógica pura del cálculo (se prueba sin LLM).

    Interpretación literal del Art. 18°: se cobra $1.000 por cada bloque
    completo de 7 días de atraso, por ítem, y solo a usuarios internos.
    """
    tipo = tipo_usuario.strip().lower()
    if tipo not in {"interno", "externo"}:
        raise ValueError("tipo_usuario debe ser 'interno' o 'externo'")
    if dias_atraso < 0:
        raise ValueError("dias_atraso no puede ser negativo")
    if cantidad_items < 1:
        raise ValueError("cantidad_items debe ser al menos 1")

    if tipo == "externo":
        return {"aplica": False, "monto": 0, "bloques": 0,
                "detalle": "El Art. 18° aplica la multa únicamente a usuarios internos."}

    bloques = dias_atraso // DIAS_POR_BLOQUE
    monto = bloques * MULTA_POR_BLOQUE * cantidad_items
    return {
        "aplica": True,
        "monto": monto,
        "bloques": bloques,
        "detalle": (f"{dias_atraso} días = {bloques} bloque(s) completo(s) de {DIAS_POR_BLOQUE} días; "
                    f"{bloques} x ${MULTA_POR_BLOQUE:,} x {cantidad_items} ítem(s) = ${monto:,}").replace(",", "."),
    }


def build_tools(usuario: str, retriever: ReglamentoRetriever, long_term: LongTermMemory,
                derivaciones: DerivacionStore) -> list[BaseTool]:
    """Crea las tools para un usuario concreto (las de escritura necesitan saber quién es)."""

    @tool
    def buscar_reglamento(consulta: str) -> str:
        """Busca en el Reglamento de Bibliotecas Duoc UC los artículos relacionados con la consulta.
        Úsala SIEMPRE antes de responder sobre préstamos, renovaciones, plazos, multas, morosidad,
        pérdida o daño de material, sanciones, normas de conducta, salas de estudio o lentes VR.
        Escribe consultas cortas y de un solo tema (ej. "plazo de préstamo colección general");
        si la pregunta tiene dos temas, haz una búsqueda por tema.
        Devuelve fragmentos del reglamento con su archivo de origen y un score de similitud,
        o el texto SIN_RESPALDO si el reglamento no contiene información relacionada."""
        fragmentos = retriever.buscar(consulta)
        if not retriever.hay_respaldo(fragmentos):
            mejor = max((f.score for f in fragmentos), default=0.0)
            return (f"SIN_RESPALDO: el reglamento no contiene información suficiente sobre esta consulta "
                    f"(mejor score {mejor:.2f} < umbral {retriever.threshold}).")
        return "\n\n".join(
            f"[{f.articulo or 'sin artículo'} | fuente: {f.fuente} | score: {f.score:.2f}]\n{f.texto}"
            for f in fragmentos
        )

    @tool
    def calcular_multa(dias_atraso: int, cantidad_items: int, tipo_usuario: str) -> str:
        """Calcula la multa por atraso en la devolución de material según el Artículo 18°.
        Usa esta herramienta para cualquier monto de multa; no hagas el cálculo tú mismo.
        Requiere: dias_atraso (días corridos de atraso, entero >= 0), cantidad_items
        (cantidad de ítems atrasados, entero >= 1) y tipo_usuario ('interno' o 'externo').
        Si falta alguno de estos datos, pregúntaselo al usuario antes de llamarla."""
        try:
            r = calcular_multa_atraso(dias_atraso, cantidad_items, tipo_usuario)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not r["aplica"]:
            return f"NO_APLICA: {r['detalle']}"
        return f"MONTO: ${r['monto']:,} (Art. 18°). Detalle: {r['detalle']}".replace(",", ".")

    @tool
    def guardar_dato_usuario(dato: str, valor: str) -> str:
        """Guarda en la memoria de largo plazo un dato estable del usuario para recordarlo en
        futuras conversaciones. Úsala cuando el usuario mencione alguno de estos datos:
        nombre, tipo_usuario ('interno' o 'externo'), sede, carrera o preferencia_respuesta.
        No guardes RUT, correos, teléfonos ni otros datos personales sensibles."""
        try:
            datos = long_term.guardar(usuario, dato, valor)
        except ValueError as exc:
            return f"ERROR: {exc}"
        return f"GUARDADO. Datos actuales del usuario: {datos}"

    @tool
    def registrar_derivacion(consulta: str, motivo: str) -> str:
        """Registra un ticket de derivación para que un encargado de biblioteca atienda al usuario.
        Úsala cuando buscar_reglamento devuelva SIN_RESPALDO, cuando el reglamento indique que
        la decisión corresponde al encargado, o cuando el usuario pida hablar con una persona.
        Devuelve el número de ticket para informárselo al usuario."""
        ticket = derivaciones.registrar(usuario, consulta, motivo)
        return f"TICKET {ticket['id']} registrado con estado '{ticket['estado']}'."

    return [buscar_reglamento, calcular_multa, guardar_dato_usuario, registrar_derivacion]


DESCRIPCION_DATOS = "; ".join(f"{k}: {v}" for k, v in DATOS_PERMITIDOS.items())
