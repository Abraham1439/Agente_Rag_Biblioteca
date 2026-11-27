"""Dashboard de observabilidad del agente (EP3 · IE4).

Ejecutar con:  streamlit run dashboard.py      (y elegir la corrida en la barra lateral)

Lee las corridas de evaluacion/correr_ep3.py guardadas en docs/ep3/<corrida>/ y muestra:
  Resumen · Calidad · Latencia y recursos · Registros · Seguridad · Explorador de trazas
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "evaluacion"))
import analisis as A  # noqa: E402
from metricas import metricas_version  # noqa: E402

st.set_page_config(page_title="Observabilidad · Agente Biblioteca Duoc UC", page_icon="📊", layout="wide")

COLOR = {"v1": "#2a78d6", "v2": "#eb6834"}          # paleta categórica validada (slots 1 y 2)
BIEN, MAL = "#0ca30c", "#d03b3b"                    # colores de estado: siempre con ✓/✗ y texto
PLANTILLA = dict(template="plotly_white", font=dict(size=13), margin=dict(l=10, r=10, t=50, b=10),
                 legend=dict(orientation="h", y=1.08, x=1, xanchor="right", title=None))
PORCENTAJES = {"precision", "precision_dentro_alcance", "precision_fuera_alcance", "hechos_ok", "trayectoria_ok",
               "recall_contexto", "fidelidad", "alucinacion", "relevancia_respuesta", "relevancia_contexto",
               "correccion", "consistencia", "tasa_error", "fallas_parseo_plan", "uso_planificador", "modo_plan",
               "llamadas_repetidas"}


def corridas() -> list[Path]:
    base = ROOT / "docs" / "ep3"
    return sorted((p.parent for p in base.glob("*/resultados_batch.csv")), key=lambda p: p.stat().st_mtime, reverse=True)


@st.cache_data(show_spinner="Leyendo registros...")
def cargar(carpeta: str) -> dict:
    d = A.cargar_corrida(Path(carpeta))
    d["pii"] = A.pii_en_registros(Path(carpeta))
    return d


def fmt(k: str, x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    if k in PORCENTAJES:
        return f"{x:.0%}"
    if isinstance(x, float):
        return f"{x:.3g}"
    return str(x)


def figura(fig: go.Figure, alto: int = 380, titulo: str | None = None) -> None:
    fig.update_layout(height=alto, **PLANTILLA)
    if titulo:
        fig.update_layout(title=titulo)
    st.plotly_chart(fig, width="stretch")


# ------------------------------------------------------------------ barra lateral
lista = corridas()
if not lista:
    st.title("Dashboard de observabilidad")
    st.info("Todavía no hay corridas. Ejecuta `python evaluacion/correr_ep3.py --simulado` (demo) "
            "o `--real` (agente real) y vuelve a abrir el dashboard.")
    st.stop()

with st.sidebar:
    st.header("📊 Observabilidad")
    carpeta = st.selectbox("Corrida", lista, format_func=lambda p: p.name)
    d = cargar(str(carpeta))
    meta, batch_full, rt_full = d["meta"], d["batch"], d["red_team"]
    versiones = st.multiselect("Versiones", sorted(batch_full["version"].unique()),
                               default=sorted(batch_full["version"].unique()))
    with st.expander("Filtrar por categoría de entrada"):
        categorias = st.multiselect("Categorías", sorted(batch_full["categoria"].unique()),
                                    default=sorted(batch_full["categoria"].unique()))
    st.caption(f"Agente: `{meta.get('modelo_agente')}` · esfuerzo `{meta.get('esfuerzo')}`  \n"
               f"Juez: `{meta.get('modelo_juez')}`  \n"
               f"{meta.get('reps')} repeticiones · {len(meta.get('casos', []))} entradas · {meta.get('fecha_utc', '')[:16]} UTC  \n"
               f"Costo de la corrida: agente US$ {meta.get('costo_agente_usd', 0):.4f} · juez US$ {meta.get('costo_juez_usd', 0):.4f}")

batch = batch_full[batch_full["version"].isin(versiones) & batch_full["categoria"].isin(categorias)]
rt = rt_full[rt_full["version"].isin(versiones)] if len(rt_full) else rt_full
spans = A.solo_batch(d["spans"], batch)
spans = spans[spans["version"].isin(versiones)] if len(spans) else spans
met = {v: metricas_version(batch[batch["version"] == v]) for v in versiones if (batch["version"] == v).any()}
for v in met:
    if len(rt):
        r = rt[rt["version"] == v]
        met[v]["ataques_exitosos"], met[v]["ataques_totales"] = int(r["logro_objetivo"].sum()), int(len(r))

st.title("Agente del Reglamento de Bibliotecas · Observabilidad")
if meta.get("simulado"):
    st.warning("**Corrida SIMULADA (demo).** El LLM, los embeddings y el juez son simulados: estos números sirven "
               "para probar el pipeline, NO son evidencia del agente real.", icon="⚠️")

tabs = st.tabs(["Resumen", "Calidad", "Latencia y recursos", "Registros", "Seguridad", "Explorador de trazas"])

# ------------------------------------------------------------------------ Resumen
with tabs[0]:
    kpis = [("precision", "Precisión", True), ("consistencia", "Consistencia", True),
            ("fidelidad", "Fidelidad", True), ("latencia_p95_s", "p95 (s)", False),
            ("llamadas_llm_por_turno", "Llamadas LLM", False),
            ("costo_usd_por_1000_turnos", "US$/1.000", False), ("tasa_error", "Errores", False)]
    for v in met:
        st.markdown(f"#### Versión {v}")
        cols = st.columns(len(kpis) + 1)
        for col, (k, nombre, mas_es_mejor) in zip(cols, kpis):
            delta = None
            if v != versiones[0] and versiones[0] in met and met[v].get(k) is not None and met[versiones[0]].get(k) is not None:
                dif = met[v][k] - met[versiones[0]][k]
                if abs(dif) > 1e-9:
                    delta = f"{dif:+.0%}" if k in PORCENTAJES else f"{dif:+.3g}"
            col.metric(nombre, fmt(k, met[v].get(k)), delta=delta,
                       delta_color="normal" if mas_es_mejor else "inverse")
        if "ataques_exitosos" in met[v]:
            cols[-1].metric("Ataques", f"{met[v]['ataques_exitosos']}/{met[v]['ataques_totales']}",
                            help="Ataques del red team que lograron su objetivo")
    st.caption("Precisión = cumple todo el ground truth · Fidelidad = afirmaciones respaldadas (juez LLM) · "
               "p95 = latencia de punta a punta · Llamadas LLM y US$/1.000 = por turno · Ataques = exitosos/intentos. "
               "Las flechas comparan con la primera versión elegida.")
    st.markdown("#### Todas las métricas")
    tabla = pd.DataFrame(met)
    st.dataframe(tabla.apply(lambda fila: fila.map(lambda x: fmt(fila.name, x)), axis=1), width="stretch")
    st.caption("Precisión: % de ejecuciones que cumplen todo el ground truth (hechos, contenido prohibido, herramientas, "
               "derivación y memoria). Consistencia: % de entradas cuyas repeticiones coinciden. Fidelidad: % de "
               "afirmaciones respaldadas por el contexto según el juez LLM. Las latencias son de punta a punta por turno.")

# ------------------------------------------------------------------------ Calidad
with tabs[1]:
    c1, c2 = st.columns(2)
    with c1:
        pc = batch.groupby(["categoria", "version"])["correcto"].mean().mul(100).reset_index()
        fig = px.bar(pc, y="categoria", x="correcto", color="version", barmode="group", orientation="h",
                     color_discrete_map=COLOR, text_auto=".0f", labels={"correcto": "precisión (%)", "categoria": ""})
        fig.update_xaxes(range=[0, 110])
        figura(fig, 520, "Precisión por categoría de entrada")
    with c2:
        juez_cols = ["recall_contexto", "fidelidad", "relevancia_respuesta", "relevancia_contexto", "correccion"]
        jm = pd.DataFrame([{"versión": v, "métrica": k, "valor": (met[v].get(k) or 0) * 100}
                           for v in met for k in juez_cols])
        fig = px.bar(jm, x="métrica", y="valor", color="versión", barmode="group", color_discrete_map=COLOR,
                     text_auto=".0f", labels={"valor": "%", "métrica": ""})
        fig.update_yaxes(range=[0, 110])
        figura(fig, 520, "Juez LLM y recall de contexto")
    st.markdown("#### Resultado de cada repetición (✓ cumple el ground truth · ✗ no cumple)")
    for v in met:
        g = batch[batch["version"] == v].pivot_table(index="rep", columns="caso", values="correcto", aggfunc="first")
        texto = g.map(lambda x: "✓" if x else "✗")
        fig = go.Figure(go.Heatmap(z=g.astype(float).values, x=g.columns, y=[f"rep {r}" for r in g.index],
                                   text=texto.values, texttemplate="%{text}", colorscale=[[0, MAL], [1, BIEN]],
                                   zmin=0, zmax=1, showscale=False, xgap=2, ygap=2,
                                   hovertemplate="%{x} · %{y}: %{text}<extra></extra>"))
        figura(fig, 170 + 30 * len(g), f"Versión {v}: columnas con ✓ y ✗ mezclados = entrada inconsistente")
    st.markdown("#### Ejecuciones que no cumplen el ground truth")
    cols = ["version", "caso", "rep", "categoria", "modo", "hechos_ok", "prohibidos_ok", "tools_ok", "derivacion_ok",
            "tools_usadas", "fidelidad", "afirmaciones_sin_respaldo", "respuesta", "trace_id"]
    st.dataframe(batch[~batch["correcto"].astype(bool)].reindex(columns=cols), width="stretch", hide_index=True)

# ------------------------------------------------------------- Latencia y recursos
with tabs[2]:
    lp = A.latencia_por_paso(spans)
    c1, c2 = st.columns(2)
    with c1:
        if len(lp):
            largo = lp.melt(id_vars=["version", "paso"], value_vars=["p50", "p95"], var_name="percentil", value_name="ms")
            largo["serie"] = largo["version"] + " · " + largo["percentil"]
            fig = px.bar(largo, y="paso", x="ms", color="version", pattern_shape="percentil", barmode="group",
                         orientation="h", color_discrete_map=COLOR, labels={"paso": ""},
                         hover_data={"percentil": True, "ms": ":.0f"})
            figura(fig, 480, "Latencia por paso: p50 (lleno) y p95 (rayado), en ms")
    with c2:
        fig = px.scatter(batch, x=batch["tokens_entrada"] + batch["tokens_salida"], y="latencia_s", color="version",
                         color_discrete_map=COLOR, symbol="modo", hover_data=["caso", "rep", "llamadas_llm", "trace_id"],
                         labels={"x": "tokens por turno", "latencia_s": "latencia (s)"})
        fig.update_traces(marker=dict(size=10, line=dict(width=2, color="white")))
        figura(fig, 480, "Dispersión: latencia vs. tokens por turno")
    c3, c4 = st.columns(2)
    with c3:
        fig = px.histogram(batch, x=batch["costo_usd"] * 1000, color="version", barmode="overlay", nbins=25,
                           color_discrete_map=COLOR, opacity=0.75, labels={"x": "US$ por 1.000 turnos de este tipo"})
        fig.update_traces(marker_line_color="white", marker_line_width=2)
        figura(fig, 380, "Distribución del costo por turno")
    with c4:
        fig = px.box(batch, x="modo", y="latencia_s", color="version", color_discrete_map=COLOR, points="all",
                     hover_data=["caso"], labels={"latencia_s": "latencia (s)", "modo": ""})
        figura(fig, 380, "Latencia por modo de ejecución")
    st.markdown("#### Tokens, costo y llamadas por componente")
    st.dataframe(A.tokens_por_componente(spans), width="stretch", hide_index=True)
    st.markdown("#### ¿Dónde se va el tiempo en las 10 solicitudes más lentas?")
    st.dataframe(A.desglose_lentas(batch, spans), width="stretch", hide_index=True)

# ------------------------------------------------------------------------ Registros
with tabs[3]:
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### Estados devueltos por las herramientas")
        st.dataframe(A.estados_herramientas(spans), width="stretch", hide_index=True)
        st.markdown("#### Ciclos redundantes")
        st.dataframe(A.ciclos(spans), width="stretch", hide_index=True)
    with c2:
        st.markdown("#### Fallas de parseo del planificador")
        st.dataframe(A.fallas_parseo(batch, spans), width="stretch", hide_index=True)
        st.markdown("#### Datos personales encontrados en los registros")
        pii = d["pii"][d["pii"]["version"].isin(versiones)] if len(d["pii"]) else d["pii"]
        if len(pii):
            fig = px.bar(pii, x="archivo", y="apariciones", color="version", barmode="group", pattern_shape="dato",
                         color_discrete_map=COLOR, labels={"archivo": ""})
            figura(fig, 320)
        else:
            st.success("✓ No se encontraron RUT, correos ni teléfonos en los registros de las versiones elegidas.")
    st.markdown("#### Desviaciones de la lógica de negocio")
    desv = A.desviaciones_negocio(batch, [t for t in d["turnos"] if t.get("version") in versiones])
    if len(desv):
        st.dataframe(desv.groupby(["version", "regla"]).size().rename("ocurrencias").reset_index(), hide_index=True)
        st.dataframe(desv, width="stretch", hide_index=True)
    else:
        st.success("✓ Sin desviaciones detectadas con las reglas definidas.")
    gr = A.guardrails(d["eventos"][d["eventos"]["version"].isin(versiones)] if "version" in d["eventos"] else d["eventos"], batch)
    if gr.get("eventos") is not None:
        st.markdown("#### Eventos de guardrail")
        st.dataframe(gr["eventos"], hide_index=True)
    if gr.get("falsos_positivos"):
        st.caption(f"Falsos positivos del guardrail de entrada (consultas legítimas bloqueadas): {gr['falsos_positivos']}")

# ------------------------------------------------------------------------ Seguridad
with tabs[4]:
    if len(rt):
        t = rt.groupby(["ataque", "riesgo", "owasp", "version"])["logro_objetivo"].agg(["sum", "size"]).reset_index()
        t["resultado"] = t.apply(lambda r: f"{'✗ vulnerable' if r['sum'] else '✓ resistió'} ({int(r['sum'])} de {int(r['size'])})", axis=1)
        st.dataframe(t.pivot_table(index=["ataque", "riesgo", "owasp"], columns="version", values="resultado",
                                   aggfunc="first").reset_index(), width="stretch", hide_index=True)
        st.markdown("#### Detalle de cada intento")
        st.dataframe(rt, width="stretch", hide_index=True)
    else:
        st.info("Esta corrida no incluye red team.")

# --------------------------------------------------------------- Explorador de trazas
with tabs[5]:
    c1, c2, c3 = st.columns(3)
    v = c1.selectbox("Versión", list(met))
    caso = c2.selectbox("Caso", sorted(batch["caso"].unique()))
    opciones = batch[(batch["version"] == v) & (batch["caso"] == caso)]
    fila = c3.selectbox("Repetición", opciones.to_dict("records"),
                        format_func=lambda f: f"rep {f['rep']} · {f['latencia_s']} s · {'✓' if f['correcto'] else '✗'}")
    if fila:
        st.markdown(f"**Pregunta:** {fila['mensaje']}  \n**Respuesta:** {fila['respuesta']}  \n"
                    f"`trace_id {fila['trace_id']}` · modo **{fila['modo']}** · {fila['llamadas_llm']} llamadas al LLM · "
                    f"{int(fila['tokens_entrada'] + fila['tokens_salida'])} tokens")
        tr = A.traza(d["spans"], fila["trace_id"])
        if len(tr):
            tr = tr.assign(paso=tr.apply(lambda r: f"{r['tipo']} · {r.get('componente') if r['tipo'] == 'llm' else r.get('nombre')}", axis=1))
            fig = go.Figure(go.Bar(y=tr["paso"], x=tr["duracion_ms"], base=tr["inicio_ms"], orientation="h",
                                   marker_color=[COLOR.get(v)] * len(tr),
                                   hovertext=tr["duracion_ms"].map(lambda x: f"{x:.0f} ms"), hoverinfo="text+y"))
            fig.update_yaxes(autorange="reversed")
            fig.update_xaxes(title="ms desde el inicio del turno")
            figura(fig, 120 + 32 * len(tr), "Cascada de spans del turno")
            st.dataframe(tr.drop(columns=["paso"]), width="stretch", hide_index=True)
        ev = d["eventos"]
        if len(ev) and "trace_id" in ev:
            st.markdown("#### Eventos del turno (eventos.log)")
            st.dataframe(ev[ev["trace_id"] == fila["trace_id"]].dropna(axis=1, how="all"), width="stretch", hide_index=True)
