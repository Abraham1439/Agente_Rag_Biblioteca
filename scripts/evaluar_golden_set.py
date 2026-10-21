"""Evalúa el agente real contra un golden set (preguntas con respuesta esperada).

ATENCIÓN: llama al modelo de OpenRouter, por lo que gasta presupuesto de la key
(aprox. 8 preguntas, ~25 llamadas con gpt-6-luna y esfuerzo low). Ejecutarlo solo
para generar la evidencia final, no durante el desarrollo.

Criterio: cada caso pasa si la respuesta contiene al menos una opción de cada
grupo de `debe_contener` y, si corresponde, se usó la tool o el modo esperado.
Se exige un umbral global (80 %), como se hace con golden sets en la industria,
porque las respuestas de un LLM no son determinísticas.

Uso:  python scripts/evaluar_golden_set.py
"""
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from biblioteca_agent.agent import BibliotecaAgent, build_llm  # noqa: E402
from biblioteca_agent.config import Settings  # noqa: E402
from biblioteca_agent.rag import ReglamentoRetriever  # noqa: E402

UMBRAL_APROBACION = 0.8


def tools_usadas(turno) -> set[str]:
    llamadas = list(turno.tools)
    for paso in turno.pasos:
        llamadas += paso["tools"]
    return {c["tool"] for c in llamadas}


def main() -> None:
    settings = Settings()
    settings.storage_dir = ROOT / "storage" / "golden_set"  # no mezcla con datos de la demo
    llm, retriever = build_llm(settings), ReglamentoRetriever.from_settings(settings)
    casos = json.loads((ROOT / "scripts" / "golden_set.json").read_text(encoding="utf-8"))

    resultados = []
    for caso in casos:
        usuario = f"gs_{caso['id']}_{uuid.uuid4().hex[:4]}"
        if caso.get("previo"):
            BibliotecaAgent(settings, llm, retriever, usuario, "previa").responder(caso["previo"])
        turno = BibliotecaAgent(settings, llm, retriever, usuario, "principal").responder(caso["pregunta"])

        texto = turno.respuesta.lower()
        checks = {"contenido": all(any(op.lower() in texto for op in grupo) for grupo in caso["debe_contener"])}
        if "tool_esperada" in caso:
            checks["tool"] = caso["tool_esperada"] in tools_usadas(turno)
        if "modo_esperado" in caso:
            checks["modo"] = turno.modo == caso["modo_esperado"]
        ok = all(checks.values())
        resultados.append({"id": caso["id"], "ok": ok, "checks": checks, "modo": turno.modo,
                           "tools": sorted(tools_usadas(turno)), "pregunta": caso["pregunta"],
                           "respuesta": turno.respuesta})
        print(f"[{'OK ' if ok else 'FALLA'}] {caso['id']} {checks} -> {turno.respuesta[:120]!r}")

    tasa = sum(r["ok"] for r in resultados) / len(resultados)
    print(f"\nAprobados: {sum(r['ok'] for r in resultados)}/{len(resultados)} ({tasa:.0%}) "
          f"- umbral {UMBRAL_APROBACION:.0%}: {'APROBADO' if tasa >= UMBRAL_APROBACION else 'NO APROBADO'}")

    salida = ROOT / "docs" / "evidencia" / "golden_set_resultados.json"
    salida.parent.mkdir(parents=True, exist_ok=True)
    salida.write_text(json.dumps({"fecha": datetime.now().isoformat(timespec="seconds"),
                                  "modelo": settings.llm_model, "tasa": tasa, "resultados": resultados},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Resultados guardados en {salida.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
