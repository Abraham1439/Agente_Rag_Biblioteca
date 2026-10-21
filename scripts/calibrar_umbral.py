"""Calibra el umbral de recuperación con preguntas dentro y fuera de alcance.

Solo usa embeddings de Mistral (EMBEDDING_API_KEY); no llama al LLM ni gasta
la key de OpenRouter. En el EP1 todas las preguntas superaron 0.45 (incluso
"¿cómo instalo Linux?" obtuvo 0.65), por lo que el umbral no filtraba nada.

Uso:  python scripts/calibrar_umbral.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from biblioteca_agent.config import Settings  # noqa: E402
from biblioteca_agent.rag import ReglamentoRetriever  # noqa: E402

DENTRO = [
    "¿Cuántos días puedo mantener en préstamo un libro de la colección general?",
    "¿Cuánto es la multa por atraso en la devolución de un libro?",
    "¿Cuántas horas al día puedo reservar una sala de estudio?",
    "Perdí el libro que tenía prestado, ¿qué debo hacer?",
    "¿Puedo comer dentro de las salas de estudio?",
    "¿Cuántos libros puede llevarse un usuario externo?",
    "¿Puedo usar los lentes de realidad virtual?",
    "¿Qué pasa si devuelvo un notebook dañado?",
]
FUERA = [
    "¿Cómo puedo instalar Linux en una computadora?",
    "¿La biblioteca vende café o snacks?",
    "¿Cuál es la capital de Francia?",
    "¿Cuándo son las vacaciones de invierno?",
    "Recomiéndame una película de terror",
]


def mejor_score(r: ReglamentoRetriever, q: str) -> float:
    return max(f.score for f in r.buscar(q))


def main() -> None:
    settings = Settings()
    r = ReglamentoRetriever.from_settings(settings)
    dentro = [(mejor_score(r, q), q) for q in DENTRO]
    fuera = [(mejor_score(r, q), q) for q in FUERA]
    for titulo, filas in (("DENTRO DE ALCANCE", dentro), ("FUERA DE ALCANCE", fuera)):
        print(f"\n{titulo}")
        for s, q in sorted(filas):
            print(f"  {s:.3f}  {q}")
    min_dentro, max_fuera = min(s for s, _ in dentro), max(s for s, _ in fuera)
    print(f"\nUmbral actual: {settings.score_threshold}")
    print(f"Mínimo dentro de alcance: {min_dentro:.3f} | Máximo fuera de alcance: {max_fuera:.3f}")
    if max_fuera < min_dentro:
        print(f"Separación limpia. Umbral sugerido (punto medio): {(min_dentro + max_fuera) / 2:.2f}")
    else:
        print("Los rangos se solapan: el umbral no basta y la segunda capa (prompt + derivación) sigue siendo necesaria.")


if __name__ == "__main__":
    main()
