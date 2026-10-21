"""Memoria del agente.

Corto plazo: ConversationBufferWindowMemory (k=10 por defecto). Se envían al
LLM solo las últimas k interacciones (pregunta + respuesta), pero el historial
completo de la sesión queda guardado en disco en storage/sesiones/<id>.json,
así no se pierde si se reinicia la aplicación.

Largo plazo: un archivo JSON (storage/memoria_largo_plazo.json) con datos
estables del usuario que el propio agente decide guardar con la tool
`guardar_dato_usuario` (por ejemplo, si es usuario interno o externo, su sede o
cómo prefiere que le respondan). Se carga al inicio de cada turno y se inyecta
en el prompt de sistema, por lo que sobrevive entre conversaciones.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from langchain_classic.memory import ConversationBufferWindowMemory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.messages import BaseMessage, messages_from_dict, messages_to_dict


def _leer_json(path: Path, defecto):
    if not path.is_file():
        return defecto
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return defecto


def _escribir_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # escritura atómica: evita archivos a medio escribir


# --------------------------------------------------------------------------
# Memoria de corto plazo
# --------------------------------------------------------------------------
class JsonChatMessageHistory(BaseChatMessageHistory):
    """Historial de chat persistido en un archivo JSON (uno por sesión)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @property
    def messages(self) -> list[BaseMessage]:  # type: ignore[override]
        return messages_from_dict(_leer_json(self.path, []))

    def add_messages(self, messages: list[BaseMessage]) -> None:
        data = _leer_json(self.path, [])
        data.extend(messages_to_dict(messages))
        _escribir_json(self.path, data)

    def clear(self) -> None:
        _escribir_json(self.path, [])


def crear_memoria_corto_plazo(sessions_dir: Path, session_id: str, k: int) -> ConversationBufferWindowMemory:
    historial = JsonChatMessageHistory(Path(sessions_dir) / f"{session_id}.json")
    return ConversationBufferWindowMemory(
        k=k,
        memory_key="chat_history",
        input_key="input",
        output_key="output",
        return_messages=True,
        chat_memory=historial,
    )


# --------------------------------------------------------------------------
# Memoria de largo plazo
# --------------------------------------------------------------------------
# Solo se guardan datos útiles para responder sobre el reglamento.
# No se guardan identificadores personales (Art. 2°: privacidad de datos).
DATOS_PERMITIDOS = {
    "nombre": "Cómo prefiere que lo llamen",
    "tipo_usuario": "interno (alumno, titulado, docente, colaborador) o externo (institución en convenio)",
    "sede": "Sede Duoc UC del usuario",
    "carrera": "Carrera que estudia",
    "preferencia_respuesta": "Cómo prefiere que se le responda (ej. respuestas breves)",
}


class LongTermMemory:
    """Datos del usuario que persisten entre conversaciones."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def obtener(self, usuario: str) -> dict:
        return _leer_json(self.path, {}).get(usuario, {}).get("datos", {})

    def guardar(self, usuario: str, dato: str, valor: str) -> dict:
        dato = dato.strip().lower()
        if dato not in DATOS_PERMITIDOS:
            raise ValueError(
                f"Dato '{dato}' no permitido. Solo se pueden guardar: {', '.join(DATOS_PERMITIDOS)}"
            )
        valor = valor.strip()
        if dato == "tipo_usuario":
            valor = valor.lower()
            if valor not in {"interno", "externo"}:
                raise ValueError("tipo_usuario debe ser 'interno' o 'externo'")
        data = _leer_json(self.path, {})
        registro = data.setdefault(usuario, {"datos": {}})
        registro["datos"][dato] = valor
        registro["actualizado"] = datetime.now().isoformat(timespec="seconds")
        _escribir_json(self.path, data)
        return registro["datos"]

    def olvidar(self, usuario: str) -> None:
        data = _leer_json(self.path, {})
        data.pop(usuario, None)
        _escribir_json(self.path, data)

    def como_texto(self, usuario: str) -> str:
        datos = self.obtener(usuario)
        if not datos:
            return "No hay datos guardados de este usuario."
        return "\n".join(f"- {k}: {v}" for k, v in datos.items())


# --------------------------------------------------------------------------
# Derivaciones (escritura): tickets para un encargado de biblioteca
# --------------------------------------------------------------------------
class DerivacionStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def registrar(self, usuario: str, consulta: str, motivo: str) -> dict:
        data = _leer_json(self.path, [])
        ticket = {
            "id": f"DER-{len(data) + 1:04d}",
            "fecha": datetime.now().isoformat(timespec="seconds"),
            "usuario": usuario,
            "consulta": consulta,
            "motivo": motivo,
            "estado": "pendiente",
        }
        data.append(ticket)
        _escribir_json(self.path, data)
        return ticket

    def listar(self) -> list[dict]:
        return _leer_json(self.path, [])
