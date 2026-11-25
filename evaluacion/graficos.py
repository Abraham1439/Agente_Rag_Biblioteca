"""Gráficos estáticos (PNG) para el informe del EP3.

Paleta: categórica validada (slot 1 azul = v1, slot 2 naranja = v2), texto en tinta neutra,
grilla recesiva, una sola escala por gráfico. El dashboard interactivo está en dashboard.py.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

import analisis as A  # noqa: E402
from metricas import metricas_version  # noqa: E402

COLOR = {"v1": "#2a78d6", "v2": "#eb6834"}
TINTA, TINTA_2, GRILLA, FONDO = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": FONDO, "axes.facecolor": FONDO, "axes.edgecolor": GRILLA, "axes.labelcolor": TINTA_2,
    "xtick.color": TINTA_2, "ytick.color": TINTA_2, "text.color": TINTA, "font.size": 10,
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRILLA,
    "grid.linewidth": 0.8, "axes.axisbelow": True, "axes.titleweight": "bold", "axes.titlesize": 12,
    "legend.frameon": False,
})


def _guardar(fig, ruta: Path) -> Path:
    fig.tight_layout()
    fig.savefig(ruta, dpi=160)
    plt.close(fig)
    return ruta


def _barras_agrupadas(ax, categorias: list[str], series: dict[str, list[float]], horizontal: bool = False, fmt="{:.0f}"):
    n = len(series)
    ancho = 0.8 / max(n, 1)
    for i, (v, vals) in enumerate(series.items()):
        desplazamiento = (i - (n - 1) / 2) * ancho
        pos = [j + desplazamiento for j in range(len(categorias))]
        vals = [0 if pd.isna(x) else x for x in vals]
        if horizontal:
            barras = ax.barh(pos, vals, height=ancho * 0.92, color=COLOR.get(v, "#888"), label=v)
        else:
            barras = ax.bar(pos, vals, width=ancho * 0.92, color=COLOR.get(v, "#888"), label=v)
        ax.bar_label(barras, labels=[fmt.format(x) for x in vals], padding=2, fontsize=8, color=TINTA_2)
    if horizontal:
        ax.set_yticks(range(len(categorias)), categorias)
        if not ax.yaxis_inverted():
            ax.invert_yaxis()
    else:
        ax.set_xticks(range(len(categorias)), categorias)


def generar(carpeta: Path, batch: pd.DataFrame, spans: pd.DataFrame, rt: pd.DataFrame, simulado: bool = False) -> list[Path]:
    out = Path(carpeta) / "graficos"
    out.mkdir(exist_ok=True)
    versiones = sorted(batch["version"].unique())
    rutas = []
    sufijo = " · SIMULADO" if simulado else ""

    # 1. Calidad por versión
    nombres = {"precision": "Precisión", "consistencia": "Consistencia", "recall_contexto": "Recall contexto",
               "fidelidad": "Fidelidad", "relevancia_respuesta": "Relevancia\nrespuesta",
               "relevancia_contexto": "Relevancia\ncontexto", "correccion": "Corrección"}
    met = {v: metricas_version(batch[batch["version"] == v]) for v in versiones}
    fig, ax = plt.subplots(figsize=(10, 4.2))
    _barras_agrupadas(ax, list(nombres.values()),
                      {v: [(met[v].get(k) or 0) * 100 for k in nombres] for v in versiones}, fmt="{:.0f}%")
    ax.set_ylim(0, 112)
    ax.set_ylabel("%")
    ax.set_title(f"Calidad del agente por versión{sufijo}")
    ax.legend(loc="upper right", ncols=2)
    rutas.append(_guardar(fig, out / "1_calidad_por_version.png"))

    # 2. Latencia por paso (p50 y p95)
    lp = A.latencia_por_paso(spans)
    if len(lp):
        pasos = lp.groupby("paso")["p95"].max().sort_values(ascending=False).index.tolist()
        fig, axes = plt.subplots(1, 2, figsize=(11, 0.45 * len(pasos) + 1.6), sharey=True)
        for ax, q in zip(axes, ("p50", "p95")):
            _barras_agrupadas(ax, pasos, {v: [lp[(lp.version == v) & (lp.paso == p)][q].sum() or float("nan")
                                              for p in pasos] for v in versiones}, horizontal=True)
            ax.set_title(f"Latencia {q} por paso (ms){sufijo}")
            ax.set_xlabel("milisegundos")
            ax.margins(x=0.12)
        axes[1].legend(loc="lower right")
        rutas.append(_guardar(fig, out / "2_latencia_por_paso.png"))

    # 3. Dispersión latencia vs tokens
    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    for v in versiones:
        g = batch[batch["version"] == v]
        ax.scatter(g["tokens_entrada"] + g["tokens_salida"], g["latencia_s"], s=36, alpha=0.75,
                   color=COLOR.get(v), edgecolors=FONDO, linewidths=1, label=v)
    ax.set_xlabel("tokens por turno (entrada + salida)")
    ax.set_ylabel("latencia de punta a punta (s)")
    ax.set_title(f"Latencia vs. tokens por turno{sufijo}")
    ax.legend()
    rutas.append(_guardar(fig, out / "3_dispersion_latencia_tokens.png"))

    # 4. Distribución de costos por turno (small multiples, misma escala)
    fig, axes = plt.subplots(1, len(versiones), figsize=(5 * len(versiones), 3.6), sharex=True, sharey=True,
                             squeeze=False)
    lim = batch["costo_usd"].max() * 1000 or 1
    for ax, v in zip(axes[0], versiones):
        g = batch[batch["version"] == v]["costo_usd"] * 1000
        ax.hist(g, bins=15, range=(0, lim), color=COLOR.get(v), edgecolor=FONDO, linewidth=2)
        ax.axvline(g.median(), color=TINTA, linewidth=1.5, linestyle="--")
        ax.text(g.median(), ax.get_ylim()[1] * 0.92, f"  mediana {g.median():.3f}", fontsize=8, color=TINTA)
        ax.set_title(f"Costo por turno · {v}{sufijo}")
        ax.set_xlabel("US$ por cada 1.000 turnos de este tipo")
    axes[0][0].set_ylabel("turnos")
    rutas.append(_guardar(fig, out / "4_distribucion_costos.png"))

    # 5. Latencia por categoría (p50) y recursos
    cat = batch.groupby(["categoria", "version"])["latencia_s"].median().unstack()
    cat = cat.loc[cat.max(axis=1).sort_values(ascending=False).index]
    fig, ax = plt.subplots(figsize=(9, 0.38 * len(cat) + 1.4))
    _barras_agrupadas(ax, cat.index.tolist(), {v: cat[v].tolist() for v in versiones if v in cat}, horizontal=True,
                      fmt="{:.1f}")
    ax.set_xlabel("latencia p50 (s)")
    ax.set_title(f"Latencia mediana por categoría de entrada{sufijo}")
    ax.margins(x=0.12)
    ax.legend(loc="lower right")
    rutas.append(_guardar(fig, out / "5_latencia_por_categoria.png"))

    # 6. Red team
    if len(rt):
        t = rt.pivot_table(index="ataque", columns="version", values="logro_objetivo", aggfunc="mean") * 100
        fig, ax = plt.subplots(figsize=(8, 3.8))
        _barras_agrupadas(ax, t.index.tolist(), {v: t[v].tolist() for v in versiones if v in t}, fmt="{:.0f}%")
        ax.set_ylim(0, 115)
        ax.set_ylabel("% de intentos en que el ataque logró su objetivo")
        ax.set_title(f"Red team por ataque{sufijo}")
        ax.legend(loc="upper right")
        rutas.append(_guardar(fig, out / "6_red_team.png"))
    return rutas
