"""Evaluación completa del EP3: batch con ground truth, juez LLM y red team, para v1 y v2.

Uso:
  python evaluacion/correr_ep3.py --simulado           # sin red ni API key (datos de DEMO)
  python evaluacion/correr_ep3.py --real                # agente real: GASTA presupuesto de la key

Opciones útiles:
  --versiones v1 v2      versiones a evaluar (por defecto ambas)
  --reps 3               repeticiones por entrada (consistencia)
  --juez-reps 1          cuántas repeticiones de cada entrada evalúa el juez LLM (0 = sin juez)
  --rt-reps 2            repeticiones de cada ataque del red team (0 = sin red team)
  --casos E01 E05        evaluar solo algunos casos
  --hilos 2              solicitudes en paralelo (1 si aparecen errores 429)

Salida: una carpeta con resultados_batch.csv, red_team.csv, resumen.json, meta.json y, por versión,
storage_<v>/ con eventos.log, spans.jsonl, trazas.jsonl, historiales y tickets (los registros que
se analizan en evaluacion/analizar_registros.py y en el dashboard).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evaluacion"))

from biblioteca_agent.agent import BibliotecaAgent, build_llm  # noqa: E402
from biblioteca_agent.config import Settings  # noqa: E402
from biblioteca_agent.evaluacion import (cargar_batch, cargar_red_team, evaluar_caso, exito_ataque,  # noqa: E402
                                         juzgar, registros_del_turno, tickets_del_turno)
from biblioteca_agent.memory import LongTermMemory  # noqa: E402
from biblioteca_agent.observabilidad import Trazador  # noqa: E402
from biblioteca_agent.rag import ReglamentoRetriever  # noqa: E402
from metricas import metricas_por_version  # noqa: E402


def preparar(simulado: bool, escala: float):
    settings = Settings()
    if simulado:
        import simulador
        simulador.configurar_escala(escala)
        settings.storage_dir = ROOT / "storage" / "simulado"  # índice propio: otros embeddings
        settings.score_threshold = 0.40  # los embeddings bag-of-words dan scores más bajos que mistral-embed
        retriever = ReglamentoRetriever.from_settings(settings, embeddings=simulador.EmbeddingsBolsaDePalabras())
        llm, juez = simulador.LLMSimulado(), simulador.JuezSimulado()
        settings.judge_model = "juez-simulado"
    else:
        retriever = ReglamentoRetriever.from_settings(settings)
        llm = build_llm(settings)
        if settings.judge_model == settings.llm_model:
            print("⚠️  JUDGE_MODEL es igual al modelo del agente: el juez tenderá a aprobar sus propias "
                  "respuestas (autopreferencia). Configura otro modelo en .env.")
        juez = build_llm(settings, modelo=settings.judge_model, esfuerzo=settings.judge_reasoning_effort)
    return settings, retriever, llm, juez


def settings_version(base: Settings, version: str, salida: Path) -> Settings:
    s = copy(base)
    s.agent_version = version
    s.storage_dir = salida / f"storage_{version}"
    return s


def ejecutar_caso(settings, llm, retriever, caso: dict, rep: int) -> dict:
    usuario = f"ep3_{settings.agent_version}_{caso['id']}_{rep}_{uuid.uuid4().hex[:4]}"
    sesion = f"{caso['id']}-{rep}-{uuid.uuid4().hex[:6]}"
    lt = LongTermMemory(settings.long_term_path)  # carga de datos previos (sin pasar por el agente)
    for k, v in (caso.get("memoria_previa") or {}).items():
        lt.guardar(usuario, k, v)
    agente = BibliotecaAgent(settings, llm, retriever, usuario, sesion)
    turno = agente.responder(caso["mensaje"]).as_dict()
    memoria = LongTermMemory(settings.long_term_path).obtener(usuario)
    ev = evaluar_caso(caso, turno, memoria)
    m = turno["metricas"]
    return {"version": settings.agent_version, "caso": caso["id"], "categoria": caso["categoria"], "rep": rep,
            "dentro_alcance": caso.get("dentro_alcance", True), "trace_id": turno["trace_id"],
            "mensaje": turno["pregunta"], "respuesta": turno["respuesta"], "modo": turno["modo"],
            "planificador_usado": turno["planificador_usado"], "plan_parse_ok": turno["plan_parse_ok"],
            "guardrails": ",".join(turno["guardrails"]), "error": turno["error"],
            "latencia_s": turno["latencia_s"], "llamadas_llm": m.get("llamadas_llm", 0),
            "llamadas_herramientas": m.get("llamadas_herramientas", 0),
            "errores_herramientas": m.get("errores_herramientas", 0), "errores_llm": m.get("errores_llm", 0),
            "tokens_entrada": m.get("tokens_entrada", 0), "tokens_salida": m.get("tokens_salida", 0),
            "tokens_razonamiento": m.get("tokens_razonamiento", 0), "costo_usd": m.get("costo_usd", 0.0),
            **{k: (",".join(v) if isinstance(v, list) else v) for k, v in ev.items()},
            "_turno": turno}


def ejecutar_ataque(settings, llm, retriever, ataque: dict, rep: int) -> dict:
    usuario = f"rt_{settings.agent_version}_{ataque['id']}_{rep}_{uuid.uuid4().hex[:4]}"
    mensaje = ataque["mensaje"]
    if ataque.get("memoria_otro_usuario"):
        otro = f"victima_{uuid.uuid4().hex[:4]}"
        lt = LongTermMemory(settings.long_term_path)
        for k, v in ataque["memoria_otro_usuario"].items():
            lt.guardar(otro, k, v)
        mensaje = mensaje.replace("{otro_usuario}", otro)
    for i, previo in enumerate(ataque.get("previo", [])):
        BibliotecaAgent(settings, llm, retriever, usuario, f"{ataque['id']}-previa{i}-{uuid.uuid4().hex[:6]}").responder(previo)
    sesion = f"{ataque['id']}-{rep}-{uuid.uuid4().hex[:6]}"
    turno = BibliotecaAgent(settings, llm, retriever, usuario, sesion).responder(mensaje).as_dict()
    memoria = json.dumps(LongTermMemory(settings.long_term_path).obtener(usuario), ensure_ascii=False)
    registros = registros_del_turno(settings.storage_dir, turno["trace_id"], sesion)
    r = exito_ataque(ataque, turno, memoria, registros, tickets_del_turno(settings.storage_dir, turno["trace_id"]))
    return {"version": settings.agent_version, "ataque": ataque["id"], "riesgo": ataque["riesgo"],
            "owasp": ataque.get("owasp", ""), "rep": rep, "trace_id": turno["trace_id"],
            "logro_objetivo": r["logro_objetivo"], "criterios_cumplidos": ",".join(r["criterios_cumplidos"]),
            "modo": turno["modo"], "guardrails": ",".join(turno["guardrails"]),
            "respuesta": turno["respuesta"][:300], "memoria_final": memoria[:300]}


def correr_juez(settings, juez, casos: dict, filas: list[dict], juez_reps: int, hilos: int) -> tuple[list[dict], float]:
    elegidas = [f for f in filas if f["rep"] < juez_reps]
    costo = {"usd": 0.0}

    def uno(f):
        traz = Trazador(f["trace_id"], f["version"], settings.judge_model, settings.precio_entrada_juez,
                        settings.precio_salida_juez)
        r = juzgar(juez, casos[f["caso"]], f["_turno"], config={"callbacks": [traz], "tags": ["juez"],
                                                              "metadata": {"trace_id": f["trace_id"]}})
        costo["usd"] += traz.resumen()["costo_usd"]
        return f["trace_id"], r

    with ThreadPoolExecutor(max_workers=hilos) as pool:
        res = dict(pool.map(uno, elegidas))
    for f in filas:
        r = res.get(f["trace_id"], {})
        for k in ("juez_ok", "fidelidad", "alucinacion", "relevancia_respuesta", "relevancia_contexto", "correccion",
                  "afirmaciones"):
            f[k] = r.get(k)
        f["afirmaciones_sin_respaldo"] = " | ".join(r.get("afirmaciones_sin_respaldo", []) or [])
        f["razon_juez"] = r.get("razon_juez")
    return filas, costo["usd"]


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modo = ap.add_mutually_exclusive_group(required=True)
    modo.add_argument("--simulado", action="store_true", help="sin red: LLM, embeddings y juez simulados (DEMO)")
    modo.add_argument("--real", action="store_true", help="agente real con OpenRouter y Mistral (gasta la key)")
    ap.add_argument("--versiones", nargs="+", default=["v1", "v2"])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--juez-reps", type=int, default=1)
    ap.add_argument("--rt-reps", type=int, default=2)
    ap.add_argument("--casos", nargs="*")
    ap.add_argument("--hilos", type=int, default=2, help="solicitudes en paralelo (bajar a 1 si aparecen errores 429)")
    ap.add_argument("--escala", type=float, default=0.25, help="(simulado) factor de latencia")
    ap.add_argument("--salida", type=Path)
    a = ap.parse_args()

    settings, retriever, llm, juez = preparar(a.simulado, a.escala)
    casos = cargar_batch()
    if a.casos:
        casos = [c for c in casos if c["id"] in set(a.casos)]
    ataques = cargar_red_team() if a.rt_reps else []
    marca = datetime.now().strftime("%Y%m%d_%H%M")
    salida = a.salida or (ROOT / "docs" / "ep3" / ("demo_simulada" if a.simulado else f"corrida_{marca}"))
    salida.mkdir(parents=True, exist_ok=True)

    turnos = len(casos) * a.reps * len(a.versiones)
    print(f"{'SIMULADO (demo)' if a.simulado else 'REAL'} · agente {settings.llm_model} · juez {settings.judge_model}")
    print(f"{len(casos)} casos × {a.reps} reps × {len(a.versiones)} versiones = {turnos} turnos; "
          f"juez sobre {len(casos) * min(a.juez_reps, a.reps) * len(a.versiones)}; "
          f"red team {len(ataques)} ataques × {a.rt_reps} reps × {len(a.versiones)} versiones")
    print(f"Salida: {_rel(salida)}\n")

    filas_batch, filas_rt, costo_juez = [], [], 0.0
    t_total = time.perf_counter()
    for version in a.versiones:
        sv = settings_version(settings, version, salida)
        retriever.limpiar_cache()  # cada versión parte con la caché vacía (cold start)
        tareas = [(c, r) for c in casos for r in range(a.reps)]
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=a.hilos) as pool:
            filas = list(pool.map(lambda t: ejecutar_caso(sv, llm, retriever, *t), tareas))
        errores = [f for f in filas if f["error"]]
        print(f"[{version}] batch: {len(filas)} turnos en {time.perf_counter() - t0:.0f} s · "
              f"precisión {sum(f['correcto'] for f in filas) / len(filas):.0%}"
              + (f" · ⚠️ {len(errores)} turnos con error técnico (ej.: {errores[0]['error'][:120]})" if errores else ""))
        if a.juez_reps:
            filas, c = correr_juez(sv, juez, {c["id"]: c for c in casos}, filas, a.juez_reps, a.hilos)
            costo_juez += c
            ok = sum(1 for f in filas if f.get("juez_ok") is True)
            fallidas = [f for f in filas if f.get("juez_ok") is False]
            print(f"[{version}] juez: {ok} evaluaciones correctas" + (f", {len(fallidas)} fallidas" if fallidas else ""))
            if fallidas:
                print(f"   ⚠️  ejemplo de falla del juez: {fallidas[0].get('razon_juez')}  (¿JUDGE_MODEL existe en OpenRouter?)")
        filas_batch += filas
        if ataques:
            tareas_rt = [(at, r) for at in ataques for r in range(a.rt_reps)]
            with ThreadPoolExecutor(max_workers=a.hilos) as pool:
                rt = list(pool.map(lambda t: ejecutar_ataque(sv, llm, retriever, *t), tareas_rt))
            print(f"[{version}] red team: {sum(r['logro_objetivo'] for r in rt)} de {len(rt)} ataques lograron su objetivo")
            filas_rt += rt

    df = pd.DataFrame([{k: v for k, v in f.items() if k != "_turno"} for f in filas_batch])
    df.to_csv(salida / "resultados_batch.csv", index=False)
    with (salida / "turnos_batch.jsonl").open("w", encoding="utf-8") as fh:
        for f in filas_batch:
            fh.write(json.dumps(f["_turno"] | {"caso": f["caso"], "rep": f["rep"]}, ensure_ascii=False, default=str) + "\n")
    rt_df = pd.DataFrame(filas_rt)
    if len(rt_df):
        rt_df.to_csv(salida / "red_team.csv", index=False)
    resumen = metricas_por_version(df, rt_df)
    (salida / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = ""
    meta = {"fecha_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "simulado": a.simulado,
            "modelo_agente": settings.llm_model, "esfuerzo": settings.llm_reasoning_effort,
            "modelo_juez": settings.judge_model, "versiones": a.versiones, "reps": a.reps, "juez_reps": a.juez_reps,
            "rt_reps": a.rt_reps, "casos": [c["id"] for c in casos], "hilos": a.hilos, "commit": commit,
            "precio_usd_mtok": [settings.precio_entrada, settings.precio_salida],
            "costo_agente_usd": float(df["costo_usd"].sum()), "costo_juez_usd": costo_juez,
            "duracion_total_s": round(time.perf_counter() - t_total, 1)}
    (salida / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nResumen por versión:")
    print(pd.DataFrame(resumen["versiones"]).T.to_string())
    print(f"\nCosto estimado: agente US$ {meta['costo_agente_usd']:.4f} · juez US$ {costo_juez:.4f}")
    print(f"Archivos en {_rel(salida)}. Dashboard: streamlit run dashboard.py")


if __name__ == "__main__":
    main()
