"""Protocolos de seguridad y privacidad del agente (EP3).

Capas que usa la versión 2 del agente (ver docs/ep3/README_EP3.md, sección Seguridad):

- Entrada:   `enmascarar` quita RUT, correos, teléfonos y números de tarjeta ANTES de que el texto
             llegue al LLM, a la memoria o a los registros. `detectar_inyeccion` marca patrones
             conocidos de prompt injection y jailbreak (se registra un evento de seguridad).
- Memoria:   `valor_memoria_seguro` impide guardar en la memoria de largo plazo valores que
             contengan instrucciones o datos personales: esos valores se inyectan en el prompt
             de sistema en cada turno (inyección persistente).
- Salida:    `revisar_salida` enmascara PII y detecta fuga del prompt de sistema o enlaces externos.
- Registros: `FiltroPII` (logging) y `enmascarar` en trazas, historial y tickets.
- Datos:     `manifiesto_datos` / `verificar_integridad` detectan si alguien alteró los documentos
             del reglamento (envenenamiento del RAG).

Todo es determinista (expresiones regulares y reglas): no agrega llamadas al LLM ni costo.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

# ----------------------------------------------------------------------------- PII


def dv_rut(numero: int) -> str:
    """Dígito verificador de un RUT chileno (módulo 11)."""
    s, m = 0, 2
    for d in reversed(str(numero)):
        s += int(d) * m
        m = 2 if m == 7 else m + 1
    r = 11 - s % 11
    return "0" if r == 11 else "K" if r == 10 else str(r)


def _luhn(texto: str) -> bool:
    d = [int(x) for x in re.sub(r"\D", "", texto)][::-1]
    return len(d) >= 13 and (sum(d[0::2]) + sum(sum(divmod(2 * x, 10)) for x in d[1::2])) % 10 == 0


# (etiqueta, patrón, validador). El validador de Luhn evita enmascarar números largos que no son
# tarjetas. El RUT se enmascara aunque su dígito verificador no valide: un RUT mal escrito sigue
# siendo un dato personal, y el formato con guion no se confunde con otros números del dominio.
PATRONES_PII = [
    ("RUT", re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}\s?-\s?[\dkK]\b"), None),
    ("TARJETA", re.compile(r"\b(?:\d[ -]?){13,19}\b"), _luhn),
    ("CORREO", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"), None),
    ("TELEFONO", re.compile(r"(?:\+?56\s?)?\b9\s?\d{4}\s?\d{4}\b"), None),
]


def enmascarar(texto: str) -> str:
    """Reemplaza datos personales por una etiqueta, p. ej. 'mi RUT es [RUT]'."""
    if not isinstance(texto, str) or not texto:
        return texto
    for etiqueta, patron, valido in PATRONES_PII:
        texto = patron.sub(lambda m: f"[{etiqueta}]" if valido is None or valido(m.group()) else m.group(), texto)
    return texto


def enmascarar_obj(obj):
    """Enmascara recursivamente todos los textos de un dict/lista (registros estructurados)."""
    if isinstance(obj, str):
        return enmascarar(obj)
    if isinstance(obj, dict):
        return {k: enmascarar_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [enmascarar_obj(v) for v in obj]
    return obj


def contiene_pii(texto: str) -> bool:
    return isinstance(texto, str) and enmascarar(texto) != texto


class FiltroPII(logging.Filter):
    """Filtro de logging: todo campo de texto sale enmascarado hacia el archivo de eventos."""

    def filter(self, record: logging.LogRecord) -> bool:
        campos = getattr(record, "campos", None)
        if isinstance(campos, dict):
            record.campos = {k: enmascarar(v) if isinstance(v, str) else v for k, v in campos.items()}
        return True


# ------------------------------------------------------------------- prompt injection

PATRONES_INYECCION = {
    "ignorar_instrucciones": r"\b(ignora|olvida|omite|descarta)\w*\s+(\w+\s+){0,3}(instrucciones|reglas|indicaciones|prompt)",
    "revelar_prompt": r"\b(repite|muestra|revela|dime|imprime|copia)\w*\s+(\w+\s+){0,4}(instrucciones|prompt|reglas)\s+(de\s+sistema|internas|completas|originales)",
    "rol_falso": r"\b(soy|habla\s+el|te\s+habla)\s+(el|la)?\s*(encargad[oa]|jefe|jefa|administrador|desarrollador|docente\s+a\s+cargo)\b.{0,40}\b(desde\s+hoy|ahora|nueva\s+regla|autorizo|ordeno)",
    "cambio_de_rol": r"\b(desde\s+ahora|a\s+partir\s+de\s+ahora)\b.{0,40}\b(eres|act[uú]a|responde\s+(solo|únicamente))",
    "modo_especial": r"\b(modo\s+(desarrollador|dios|sin\s+restricciones)|jailbreak|\bDAN\b)",
    "etiqueta_sistema": r"(^|\s)(system|sistema|instrucci[oó]n)\s*:",
}
_INYECCION = {k: re.compile(v, re.IGNORECASE) for k, v in PATRONES_INYECCION.items()}


def detectar_inyeccion(texto: str) -> list[str]:
    """Nombres de los patrones de inyección que aparecen en el texto (lista vacía si no hay)."""
    return [k for k, p in _INYECCION.items() if isinstance(texto, str) and p.search(texto)]


# Respuesta fija cuando el guardrail de entrada bloquea un mensaje (no se llama al LLM).
MENSAJE_BLOQUEO = ("Solo puedo ayudarte con consultas sobre el Reglamento de Bibliotecas Duoc UC. "
                   "No puedo cambiar mis reglas de funcionamiento. ¿Tienes alguna duda sobre préstamos, "
                   "multas, salas de estudio o lentes VR?")


def valor_memoria_seguro(valor: str, max_chars: int = 40) -> str | None:
    """Motivo por el que un valor NO puede ir a la memoria de largo plazo, o None si es seguro.

    Esos valores se inyectan en el prompt de sistema en cada turno: si se acepta texto libre,
    un usuario puede dejar una instrucción guardada que el agente obedecerá en todas sus
    conversaciones futuras (prompt injection persistente)."""
    if len(valor) > max_chars:
        return f"el valor supera {max_chars} caracteres"
    if contiene_pii(valor):
        return "el valor contiene datos personales (RUT, correo, teléfono o tarjeta)"
    if detectar_inyeccion(valor) or re.search(r"[{}<>\[\]]|\binstrucci|\bresponde\b|\bignora", valor, re.I):
        return "el valor contiene instrucciones o caracteres no permitidos"
    return None


# ---------------------------------------------------------------------------- salida

# Frases literales del prompt de sistema: si aparecen en la respuesta, hubo fuga del prompt.
FRAGMENTOS_PROMPT = ("Reglas estrictas", "No inventes plazos, montos", "registrar_derivacion",
                     "guardar_dato_usuario", "Datos recordados del usuario", "Responde SOLO con un objeto JSON")
_URL = re.compile(r"https?://(?![\w.-]*duoc\.cl)\S+", re.IGNORECASE)


def revisar_salida(respuesta: str) -> tuple[str, list[str]]:
    """Guardrail de salida. Devuelve la respuesta corregida y las reglas que se activaron."""
    reglas = []
    if contiene_pii(respuesta):
        reglas.append("pii_en_salida")
        respuesta = enmascarar(respuesta)
    if any(f.lower() in respuesta.lower() for f in FRAGMENTOS_PROMPT):
        reglas.append("fuga_prompt")
        respuesta = MENSAJE_BLOQUEO
    if _URL.search(respuesta):
        reglas.append("enlace_externo")
        respuesta = _URL.sub("[enlace omitido]", respuesta)
    return respuesta, reglas


# ------------------------------------------------------------- integridad de los datos


def manifiesto_datos(data_dir: Path) -> dict[str, str]:
    """SHA-256 de cada documento del reglamento."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(data_dir).glob("*.txt"))}


def verificar_integridad(data_dir: Path, manifiesto_path: Path) -> list[str]:
    """Documentos que no coinciden con el manifiesto aprobado (lista vacía = todo íntegro)."""
    if not Path(manifiesto_path).is_file():
        return ["(no existe manifiesto aprobado)"]
    aprobado = json.loads(Path(manifiesto_path).read_text(encoding="utf-8"))
    actual = manifiesto_datos(data_dir)
    return sorted(n for n in set(aprobado) | set(actual) if aprobado.get(n) != actual.get(n))
