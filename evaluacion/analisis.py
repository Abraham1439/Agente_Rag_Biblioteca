"""Análisis de los registros de ejecución (EP3 · IE3 / IE6).

Responde con los LOGS, no con intuición, siete preguntas sobre una corrida de `correr_ep3.py`:

1. ¿Dónde se va el tiempo? Latencia por paso (planificador, agente, síntesis, cada tool, retriever).
2. ¿Qué pasa en las solicitudes más lentas? Desglose de las 10 peores trazas.
3. ¿Fallan las herramientas? Errores y estados devueltos (SIN_RESPALDO, ERROR, NO_APLICA...).
4. ¿Hay ciclos redundantes? La misma tool con la misma entrada dos veces en un turno.
5. ¿Falla el parseo del planificador? JSON inválido y lo que devolvió el modelo.
6. ¿Se desvía de la lógica de negocio? Montos sin calcular_multa, SIN_RESPALDO sin derivar, etc.
7. ¿Hay datos personales en los registros? RUT, correos y teléfonos en cada archivo de storage/.

Funciones puras sobre DataFrames: las usa el dashboard y `analizar_registros.py`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

PII_REGEX = {"RUT": r"\b\d{1,2}\.?\d{3}\.?\d{3}\s?-\s?[\dkK]\b", "correo": r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b",
             "teléfono": r"(?:\+?56\s?)?\b9\s?\d{4}\s?\d{4}\b"}


def _leer_jsonl(archivos: list[Path], invalidas: dict) -> pd.DataFrame:
    """Lee JSON por línea tolerando líneas dañadas (escrituras concurrentes): se saltan y se cuentan."""
    filas = []
    for archivo in archivos:
        malas = 0
        for linea in archivo.read_text(encoding="utf-8", errors="replace").splitlines():
            if not linea.strip():
                continue
            try:
                filas.append(json.loads(linea))
            except json.JSONDecodeError:
                malas += 1
        if malas:
            invalidas[str(archivo.relative_to(archivo.parents[1]))] = malas
    return pd.DataFrame(filas)


def cargar_corrida(carpeta: Path) -> dict:
    carpeta = Path(carpeta)
    datos = {"carpeta": carpeta,
             "meta": json.loads((carpeta / "meta.json").read_text(encoding="utf-8")) if (carpeta / "meta.json").is_file() else {},
             "batch": pd.read_csv(carpeta / "resultados_batch.csv") if (carpeta / "resultados_batch.csv").is_file() else pd.DataFrame(),
             "red_team": pd.read_csv(carpeta / "red_team.csv") if (carpeta / "red_team.csv").is_file() else pd.DataFrame()}
    datos["lineas_invalidas"] = {}
    datos["spans"] = _leer_jsonl(sorted(carpeta.glob("storage_*/spans.jsonl")), datos["lineas_invalidas"])
    datos["eventos"] = _leer_jsonl(sorted(carpeta.glob("storage_*/eventos.log")), datos["lineas_invalidas"])
    turnos = carpeta / "turnos_batch.jsonl"
    datos["turnos"] = ([json.loads(l) for l in turnos.read_text(encoding="utf-8").splitlines()]
                       if turnos.is_file() else [])
    return datos


def solo_batch(spans: pd.DataFrame, batch: pd.DataFrame) -> pd.DataFrame:
    """Los spans del batch (sin los del red team), para que las métricas sean comparables."""
    if spans.empty or batch.empty:
        return spans
    return spans[spans["trace_id"].isin(set(batch["trace_id"]))]


def _paso(r) -> str:
    if r["tipo"] == "llm":
        return f"LLM · {r.get('componente', 'agente')}"
    if r["tipo"] == "retriever":
        return "retriever (embeddings + FAISS)"
    return f"tool · {r.get('nombre')}"


# 1 ------------------------------------------------------------------------------
def latencia_por_paso(spans: pd.DataFrame) -> pd.DataFrame:
    if spans.empty:
        return pd.DataFrame()
    s = spans.assign(paso=spans.apply(_paso, axis=1))
    # El tiempo de buscar_reglamento incluye al retriever: para el % del tiempo se usan solo
    # los spans "hoja" (LLM, retriever y tools que no son buscar_reglamento).
    hoja = ~(s["paso"] == "tool · buscar_reglamento")
    t = (s.groupby(["version", "paso"])["duracion_ms"]
         .agg(llamadas="size", p50=lambda x: x.quantile(.5), p95=lambda x: x.quantile(.95), max="max")
         .round(0))
    total = s[hoja].groupby("version")["duracion_ms"].sum()
    pct = (s[hoja].groupby(["version", "paso"])["duracion_ms"].sum() / total * 100).round(1)
    t["% del tiempo"] = pct
    return t.reset_index().sort_values(["version", "p95"], ascending=[True, False])


def tokens_por_componente(spans: pd.DataFrame) -> pd.DataFrame:
    llm = spans[spans["tipo"] == "llm"] if not spans.empty else spans
    if llm.empty:
        return pd.DataFrame()
    return (llm.groupby(["version", "componente"])
            .agg(llamadas=("span_id", "size"), tokens_entrada_media=("tokens_entrada", "mean"),
                 tokens_salida_media=("tokens_salida", "mean"), costo_usd_total=("costo_usd", "sum"),
                 latencia_p50_ms=("duracion_ms", lambda x: x.quantile(.5)))
            .round(4).reset_index())


# 2 ------------------------------------------------------------------------------
def desglose_lentas(batch: pd.DataFrame, spans: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    if batch.empty or spans.empty:
        return pd.DataFrame()
    filas = []
    for v, g in batch.groupby("version"):
        lentas = g.nlargest(n, "latencia_s")["trace_id"]
        s = spans[spans["trace_id"].isin(lentas)]
        s = s.assign(paso=s.apply(_paso, axis=1))
        s = s[s["paso"] != "tool · buscar_reglamento"]
        pct = s.groupby("paso")["duracion_ms"].sum() / s["duracion_ms"].sum() * 100
        for paso, val in pct.items():
            filas.append({"version": v, "paso": paso, "% del tiempo en las 10 más lentas": round(val, 1)})
    return pd.DataFrame(filas).sort_values(["version", "% del tiempo en las 10 más lentas"], ascending=[True, False])


def traza(spans: pd.DataFrame, trace_id: str) -> pd.DataFrame:
    cols = ["inicio_ms", "duracion_ms", "tipo", "componente", "nombre", "ok", "estado", "herramientas_pedidas",
            "tokens_entrada", "tokens_salida", "mejor_score", "articulos", "cache_hit", "entradas", "salida",
            "sub_prompt", "respuesta_intermedia", "error"]
    t = spans[spans["trace_id"] == trace_id].sort_values("inicio_ms")
    return t.reindex(columns=[c for c in cols if c in t.columns])


# 3 ------------------------------------------------------------------------------
def estados_herramientas(spans: pd.DataFrame) -> pd.DataFrame:
    h = spans[spans["tipo"] == "herramienta"] if not spans.empty else spans
    if h.empty:
        return pd.DataFrame()
    return (h.groupby(["version", "nombre", "estado"]).size().rename("llamadas").reset_index()
            .sort_values(["version", "nombre", "llamadas"], ascending=[True, True, False]))


# 4 ------------------------------------------------------------------------------
def ciclos(spans: pd.DataFrame) -> pd.DataFrame:
    h = spans[spans["tipo"] == "herramienta"] if not spans.empty else spans
    if h.empty:
        return pd.DataFrame()
    rep = h.groupby(["version", "trace_id", "nombre", "entradas"]).size()
    rep = rep[rep > 1].rename("veces").reset_index()
    busquedas = h[h["nombre"] == "buscar_reglamento"].groupby(["version", "trace_id"]).size()
    resumen = pd.DataFrame({
        "trazas_con_tool_repetida_misma_entrada": rep.groupby("version")["trace_id"].nunique(),
        "trazas_con_2+_busquedas": busquedas[busquedas >= 2].groupby(level=0).size(),
        "busquedas_por_traza_max": busquedas.groupby(level=0).max(),
        "trazas_totales": h.groupby("version")["trace_id"].nunique(),
    }).fillna(0).astype(int)
    return resumen.reset_index().rename(columns={"index": "version"})


# 5 ------------------------------------------------------------------------------
def fallas_parseo(batch: pd.DataFrame, spans: pd.DataFrame) -> pd.DataFrame:
    if batch.empty:
        return pd.DataFrame()
    malos = batch[batch["plan_parse_ok"].astype(str) == "False"]
    filas = []
    for _, f in malos.iterrows():
        crudo = ""
        if not spans.empty:
            p = spans[(spans["trace_id"] == f["trace_id"]) & (spans["componente"] == "planificador")]
            crudo = str(p["respuesta_intermedia"].iloc[0])[:200] if len(p) else ""
        filas.append({"version": f["version"], "caso": f["caso"], "rep": f["rep"], "trace_id": f["trace_id"],
                      "salida_del_planificador": crudo})
    return pd.DataFrame(filas)


# 6 ------------------------------------------------------------------------------
_MONTO = re.compile(r"\$\s?\d[\d.]*")


def desviaciones_negocio(batch: pd.DataFrame, turnos: list[dict]) -> pd.DataFrame:
    """Reglas del negocio que el agente debía cumplir, verificadas sobre lo que REALMENTE ejecutó."""
    if batch.empty:
        return pd.DataFrame()
    por_trace = {t["trace_id"]: t for t in turnos}
    filas = []
    for _, f in batch.iterrows():
        t = por_trace.get(f["trace_id"], {})
        llamadas = list(t.get("tools", []))
        for paso in t.get("pasos", []):
            llamadas += paso.get("tools", [])
        usadas = [c["tool"] for c in llamadas]
        resp = str(f.get("respuesta", ""))
        sin_respaldo = any(c["tool"] == "buscar_reglamento" and str(c["output"]).startswith("SIN_RESPALDO") for c in llamadas)
        respaldo = any(c["tool"] == "buscar_reglamento" and not str(c["output"]).startswith("SIN_RESPALDO") for c in llamadas)
        montos_resp = {m.replace(" ", "") for m in _MONTO.findall(resp)} - {"$1.000"}  # $1.000 es la tarifa, no un cálculo
        reglas = {
            "monto informado sin usar calcular_multa": bool(montos_resp) and "calcular_multa" not in usadas,
            "SIN_RESPALDO sin registrar derivación": sin_respaldo and not respaldo and "registrar_derivacion" not in usadas,
            "derivó aunque el reglamento sí respondía": "registrar_derivacion" in usadas and bool(f.get("dentro_alcance")) and respaldo,
            "más de un ticket en un turno": usadas.count("registrar_derivacion") > 1,
            "respondió sin consultar el reglamento": (not usadas and f.get("modo") not in ("bloqueado",)
                                                     and bool(f.get("dentro_alcance")) and f.get("categoria") != "falta un dato"),
        }
        for regla, ocurre in reglas.items():
            if ocurre:
                filas.append({"version": f["version"], "caso": f["caso"], "rep": f["rep"], "regla": regla,
                              "trace_id": f["trace_id"], "respuesta": resp[:160]})
    return pd.DataFrame(filas)


# 7 ------------------------------------------------------------------------------
def pii_en_registros(carpeta: Path) -> pd.DataFrame:
    filas = []
    for archivo in sorted(Path(carpeta).glob("storage_*/**/*")):
        if not archivo.is_file() or archivo.suffix not in {".json", ".jsonl", ".log"}:
            continue
        texto = archivo.read_text(encoding="utf-8", errors="ignore")
        version = archivo.relative_to(carpeta).parts[0].replace("storage_", "")
        tipo = "historial de sesiones" if "sesiones" in archivo.parts else archivo.name
        for nombre, patron in PII_REGEX.items():
            n = len(re.findall(patron, texto))
            if n:
                filas.append({"version": version, "archivo": tipo, "dato": nombre, "apariciones": n})
    if not filas:
        return pd.DataFrame(columns=["version", "archivo", "dato", "apariciones"])
    return pd.DataFrame(filas).groupby(["version", "archivo", "dato"], as_index=False)["apariciones"].sum()


def guardrails(eventos: pd.DataFrame, batch: pd.DataFrame) -> dict:
    """Eventos de guardrail y falsos positivos (mensajes legítimos bloqueados)."""
    res: dict = {}
    if not eventos.empty and "evento" in eventos:
        g = eventos[eventos["evento"] == "guardrail"]
        if len(g):
            res["eventos"] = g.groupby(["capa", "regla"]).size().rename("eventos").reset_index()
    if not batch.empty:
        legit = batch[batch["dentro_alcance"].astype(bool)]
        bloqueados = legit[legit["modo"] == "bloqueado"]
        res["falsos_positivos"] = {"bloqueos_de_consultas_legitimas": int(len(bloqueados)),
                                   "consultas_legitimas": int(len(legit)),
                                   "casos": sorted(bloqueados["caso"].unique().tolist())}
    return res
