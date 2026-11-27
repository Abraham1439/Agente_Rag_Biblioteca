"""Integridad de los documentos del reglamento (mitigación de data poisoning en el RAG · OWASP LLM04).

El índice FAISS se regenera solo cuando cambian los TXT de data/ (huella en rag.py). Eso es
cómodo, pero también significa que cualquiera que edite un TXT cambia lo que responde el agente.
Este script compara los TXT con un manifiesto SHA-256 aprobado por el equipo:

  python scripts/verificar_integridad.py            # verifica (código de salida 1 si algo cambió)
  python scripts/verificar_integridad.py --aprobar  # registra el estado actual como aprobado

En producción, el manifiesto se firmaría y la verificación correría antes de reindexar (CI/CD).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from biblioteca_agent.seguridad import manifiesto_datos, verificar_integridad  # noqa: E402

DATA = ROOT / "data"
MANIFIESTO = ROOT / "data" / "MANIFIESTO_SHA256.json"


def main() -> None:
    if "--aprobar" in sys.argv:
        MANIFIESTO.write_text(json.dumps(manifiesto_datos(DATA), indent=2), encoding="utf-8")
        print(f"✅ Manifiesto actualizado: {MANIFIESTO.relative_to(ROOT)}")
        return
    alterados = verificar_integridad(DATA, MANIFIESTO)
    if alterados:
        print(f"❌ Documentos que no coinciden con el manifiesto aprobado: {', '.join(alterados)}")
        print("   Revisa el cambio con el equipo antes de reindexar; si es legítimo, ejecuta --aprobar.")
        sys.exit(1)
    print("✅ Los 6 documentos del reglamento coinciden con el manifiesto aprobado.")


if __name__ == "__main__":
    main()
