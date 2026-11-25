"""Genera la hoja de datos del análisis de registros de una corrida (tablas en Markdown y CSV).

Uso:  python evaluacion/analizar_registros.py docs/ep3/corrida_YYYYMMDD_HHMM

Escribe en la carpeta de la corrida:
  - hallazgos_datos.md   tablas con la evidencia (SIN redacción de conclusiones: el análisis y la
                         interpretación los escribe el equipo, como exige la pauta)
  - analisis/*.csv       las mismas tablas en CSV
  - graficos/*.png       gráficos estáticos para el informe
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analisis as A  # noqa: E402
from metricas import metricas_por_version  # noqa: E402


def md(df: pd.DataFrame, max_filas: int = 40) -> str:
    if df is None or len(df) == 0:
        return "_(sin registros)_\n"
    return df.head(max_filas).to_markdown(index=False, floatfmt=".3g") + "\n"


def pct(x):
    return "—" if x is None or pd.isna(x) else f"{x:.0%}"


def main(carpeta: Path) -> None:
    d = A.cargar_corrida(carpeta)
    if d.get("lineas_invalidas"):
        print(f"⚠️  líneas de registro dañadas (se omitieron): {d['lineas_invalidas']}")
    batch, spans, rt = d["batch"], A.solo_batch(d["spans"], d["batch"]), d["red_team"]
    out = carpeta / "analisis"
    out.mkdir(exist_ok=True)
    resumen = metricas_por_version(batch, rt)["versiones"]
    tablas = {
        "metricas": pd.DataFrame(resumen),
        "latencia_por_paso": A.latencia_por_paso(spans),
        "desglose_10_mas_lentas": A.desglose_lentas(batch, spans),
        "tokens_por_componente": A.tokens_por_componente(spans),
        "estados_herramientas": A.estados_herramientas(spans),
        "ciclos": A.ciclos(spans),
        "fallas_parseo_planificador": A.fallas_parseo(batch, spans),
        "desviaciones_negocio": A.desviaciones_negocio(batch, d["turnos"]),
        "pii_en_registros": A.pii_en_registros(carpeta),
    }
    for nombre, t in tablas.items():
        t.to_csv(out / f"{nombre}.csv", index=nombre == "metricas")

    por_categoria = (batch.pivot_table(index="categoria", columns="version", values="correcto", aggfunc="mean")
                     .map(pct).reset_index())
    por_caso = batch.pivot_table(index="caso", columns="version", values="correcto", aggfunc="mean").map(pct).reset_index()
    desv = tablas["desviaciones_negocio"]
    desv_res = desv.groupby(["version", "regla"]).size().rename("ocurrencias").reset_index() if len(desv) else desv
    gr = A.guardrails(d["eventos"], batch)
    peor = batch.loc[batch["latencia_s"].idxmax()] if len(batch) else None
    rt_tabla = (rt.pivot_table(index=["ataque", "riesgo"], columns="version", values="logro_objetivo", aggfunc="sum")
                .reset_index() if len(rt) else rt)

    met = pd.DataFrame(resumen)
    filas_fmt = []
    for k, row in met.iterrows():
        filas_fmt.append({"métrica": k, **{v: (pct(x) if k in {"precision", "precision_dentro_alcance", "precision_fuera_alcance",
                                                            "hechos_ok", "trayectoria_ok", "recall_contexto", "fidelidad",
                                                            "alucinacion", "relevancia_respuesta", "relevancia_contexto",
                                                            "correccion", "consistencia", "tasa_error", "fallas_parseo_plan",
                                                            "uso_planificador", "modo_plan", "llamadas_repetidas"}
                                               else (f"{x:.3g}" if isinstance(x, float) else x)) for v, x in row.items()}})

    meta = d["meta"]
    texto = [f"# Hoja de datos del análisis de registros · {carpeta.name}\n",
             ("> ⚠️ **CORRIDA SIMULADA (demo).** Estos números NO son evidencia del agente real: sirven para probar el "
              "pipeline. Para el informe usa una corrida con `--real`.\n" if meta.get("simulado") else
              f"> Corrida real · agente `{meta.get('modelo_agente')}` (esfuerzo {meta.get('esfuerzo')}) · juez "
              f"`{meta.get('modelo_juez')}` · {meta.get('fecha_utc')} · commit `{meta.get('commit')}`\n"),
             "Este archivo contiene SOLO datos. La interpretación, los hallazgos, las propuestas de rediseño y las "
             "conclusiones las redacta el equipo (la pauta califica con 0 % esos textos si los escribe una IA).\n",
             "## 1. Métricas por versión\n", md(pd.DataFrame(filas_fmt), 60),
             "## 2. Precisión por categoría de entrada\n", md(por_categoria),
             "## 3. Precisión por caso (detecta casos inestables)\n", md(por_caso),
             "## 4. Latencia por paso (ms)\n", md(tablas["latencia_por_paso"]),
             "## 5. ¿Dónde se va el tiempo en las 10 solicitudes más lentas?\n", md(tablas["desglose_10_mas_lentas"]),
             "## 6. Tokens y costo por componente del agente\n", md(tablas["tokens_por_componente"]),
             "## 7. Estados devueltos por las herramientas\n", md(tablas["estados_herramientas"]),
             "## 8. Ciclos redundantes\n", md(tablas["ciclos"]),
             "## 9. Fallas de parseo del planificador\n", md(tablas["fallas_parseo_planificador"]),
             "## 10. Desviaciones de la lógica de negocio\n", md(desv_res), "Detalle:\n", md(desv, 25),
             "## 11. Datos personales en los registros\n", md(tablas["pii_en_registros"]),
             "## 12. Guardrails\n", md(gr.get("eventos")),
             f"Falsos positivos (consultas legítimas bloqueadas): `{gr.get('falsos_positivos')}`\n",
             "## 13. Red team (ataques que lograron su objetivo)\n", md(rt_tabla)]
    if peor is not None:
        texto += [f"## 14. La traza más lenta · {peor['version']} · {peor['caso']} · {peor['latencia_s']} s · "
                  f"`{peor['trace_id']}`\n", md(A.traza(spans, peor["trace_id"])[
                      ["inicio_ms", "duracion_ms", "tipo", "componente", "nombre", "estado", "herramientas_pedidas",
                       "tokens_entrada", "tokens_salida"]].fillna(""), 40)]
    (carpeta / "hallazgos_datos.md").write_text("\n".join(texto), encoding="utf-8")
    print(f"✅ {carpeta / 'hallazgos_datos.md'} y {out}/*.csv")

    try:
        import graficos
        rutas = graficos.generar(carpeta, batch, spans, rt, simulado=bool(meta.get('simulado')))
        print(f"✅ {len(rutas)} gráficos en {carpeta / 'graficos'}")
    except ImportError as exc:
        print(f"(sin gráficos: falta {exc.name}; pip install matplotlib)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]).resolve())
