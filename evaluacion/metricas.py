"""Métricas de observabilidad del EP3, calculadas a partir de los resultados del batch y del red team.

| Grupo          | Métrica                         | Definición |
|----------------|---------------------------------|------------|
| Precisión      | precision                       | % de ejecuciones que cumplen TODO el ground truth (hechos, prohibidos, trayectoria, derivación, memoria, sin error) |
|                | precision_dentro / _fuera       | lo mismo, separado por consultas dentro y fuera del alcance del reglamento |
|                | recall_contexto                 | % de los artículos esperados que el retriever efectivamente recuperó |
| Juez LLM       | fidelidad, alucinacion          | % de afirmaciones respaldadas por el contexto (y su complemento) |
|                | relevancia_respuesta / contexto | ¿responde lo preguntado? / fragmentos útiles sobre recuperados |
|                | correccion                      | coincidencia con la respuesta de referencia |
| Consistencia   | consistencia                    | % de entradas cuyas repeticiones dan el mismo resultado (veredicto, modo, herramientas, derivación) |
| Errores        | tasa_error                      | % de turnos con excepción, error de LLM o de herramienta |
|                | fallas_parseo_plan              | % de llamadas al planificador cuyo JSON no se pudo leer |
| Latencia       | p50 / p95 / max (s)             | de punta a punta por turno; el promedio esconde la cola |
| Recursos       | llamadas LLM, tokens, costo     | por turno y por cada 1.000 turnos |
| Seguridad      | ataques_exitosos                | ataques del red team que lograron su objetivo |
"""
from __future__ import annotations

import pandas as pd


def _media(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return float(s.mean()) if len(s) else None


def consistencia(df: pd.DataFrame) -> float | None:
    """% de casos cuyas repeticiones coinciden en veredicto, modo, herramientas y derivación."""
    if df.empty or df["rep"].nunique() < 2:
        return None
    firma = df.assign(_tools=df["tools_usadas"].fillna("").map(lambda t: ",".join(sorted(set(str(t).split(",")) - {""}))))
    iguales = firma.groupby("caso")[["correcto", "modo", "_tools", "derivo"]].apply(
        lambda g: g.astype(str).drop_duplicates().shape[0] == 1)
    return float(iguales.mean())


def casos_inconsistentes(df: pd.DataFrame) -> list[str]:
    if df.empty:
        return []
    g = df.groupby("caso")["correcto"].nunique()
    return sorted(g[g > 1].index)


def metricas_version(df: pd.DataFrame) -> dict:
    lat = df["latencia_s"]
    plan = df[df["planificador_usado"].astype(bool)]
    parse = pd.to_numeric(plan["plan_parse_ok"].map({True: 1, False: 0, "True": 1, "False": 0}), errors="coerce").dropna()
    tok = df["tokens_entrada"] + df["tokens_salida"]
    errores = df["error"].notna() & (df["error"].astype(str) != "") | (df["errores_herramientas"] > 0) | (df["errores_llm"] > 0)
    return {
        "turnos": int(len(df)),
        "precision": _media(df["correcto"]),
        "precision_dentro_alcance": _media(df.loc[df["dentro_alcance"].astype(bool), "correcto"]),
        "precision_fuera_alcance": _media(df.loc[~df["dentro_alcance"].astype(bool), "correcto"]),
        "hechos_ok": _media(df["hechos_ok"]), "trayectoria_ok": _media(df["tools_ok"]),
        "recall_contexto": _media(df["recall_contexto"]),
        "fidelidad": _media(df.get("fidelidad", pd.Series(dtype=float))),
        "alucinacion": _media(df.get("alucinacion", pd.Series(dtype=float))),
        "relevancia_respuesta": _media(df.get("relevancia_respuesta", pd.Series(dtype=float))),
        "relevancia_contexto": _media(df.get("relevancia_contexto", pd.Series(dtype=float))),
        "correccion": _media(df.get("correccion", pd.Series(dtype=float))),
        "consistencia": consistencia(df),
        "casos_inconsistentes": ",".join(casos_inconsistentes(df)),
        "tasa_error": float(errores.mean()) if len(df) else None,
        "fallas_parseo_plan": (1 - float(parse.mean())) if len(parse) else None,
        "uso_planificador": _media(df["planificador_usado"].astype(bool)),
        "modo_plan": float((df["modo"] == "plan").mean()) if len(df) else None,
        "llamadas_repetidas": _media(df["llamadas_repetidas"] > 0),
        "latencia_p50_s": float(lat.quantile(.5)), "latencia_p95_s": float(lat.quantile(.95)),
        "latencia_max_s": float(lat.max()), "latencia_media_s": float(lat.mean()),
        "llamadas_llm_por_turno": float(df["llamadas_llm"].mean()),
        "tokens_por_turno": float(tok.mean()),
        "costo_usd_por_1000_turnos": float(df["costo_usd"].mean() * 1000),
    }


def metricas_por_version(df: pd.DataFrame, rt: pd.DataFrame | None = None) -> dict:
    res = {"versiones": {}}
    for v, g in df.groupby("version"):
        m = metricas_version(g)
        if rt is not None and len(rt):
            r = rt[rt["version"] == v]
            m["ataques_exitosos"] = int(r["logro_objetivo"].sum())
            m["ataques_totales"] = int(len(r))
        res["versiones"][v] = m
    return res
