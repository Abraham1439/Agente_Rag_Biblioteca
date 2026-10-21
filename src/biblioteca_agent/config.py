"""Configuración del agente.

Todo lo que puede cambiar entre ambientes (modelo, claves, timeouts, rutas,
parámetros de memoria y RAG) se lee desde variables de entorno o desde el
archivo .env, para no tener que modificar código al operar el agente.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Raíz del repositorio: src/biblioteca_agent/config.py -> sube dos niveles.
ROOT_DIR = Path(__file__).resolve().parents[2]

load_dotenv(ROOT_DIR / ".env")


def _env(nombre: str, defecto: str) -> str:
    valor = os.getenv(nombre)
    return valor if valor not in (None, "") else defecto


@dataclass
class Settings:
    # --- LLM (OpenRouter, API compatible con OpenAI) ---
    llm_base_url: str = field(default_factory=lambda: _env("LLM_BASE_URL", "https://openrouter.ai/api/v1"))
    llm_api_key: str = field(default_factory=lambda: _env("OPENROUTER_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "openai/gpt-6-luna"))
    llm_reasoning_effort: str = field(default_factory=lambda: _env("LLM_REASONING_EFFORT", "low"))
    llm_timeout: float = field(default_factory=lambda: float(_env("LLM_TIMEOUT", "60")))

    # --- Embeddings (Mistral, igual que en el EP1) ---
    embedding_base_url: str = field(default_factory=lambda: _env("EMBEDDING_BASE_URL", "https://api.mistral.ai/v1"))
    embedding_api_key: str = field(default_factory=lambda: _env("EMBEDDING_API_KEY", ""))
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "mistral-embed"))

    # --- RAG (chunk_size = máximo por artículo; un artículo = un fragmento) ---
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT_DIR / "data"))))
    chunk_size: int = field(default_factory=lambda: int(_env("CHUNK_SIZE", "1200")))
    chunk_overlap: int = field(default_factory=lambda: int(_env("CHUNK_OVERLAP", "80")))
    top_k: int = field(default_factory=lambda: int(_env("TOP_K", "4")))
    score_threshold: float = field(default_factory=lambda: float(_env("SCORE_THRESHOLD", "0.45")))

    # --- Memoria ---
    memory_window_k: int = field(default_factory=lambda: int(_env("MEMORY_WINDOW_K", "10")))
    storage_dir: Path = field(default_factory=lambda: Path(_env("STORAGE_DIR", str(ROOT_DIR / "storage"))))

    # --- Agente ---
    max_iterations: int = field(default_factory=lambda: int(_env("AGENT_MAX_ITERATIONS", "8")))

    @property
    def sessions_dir(self) -> Path:
        return self.storage_dir / "sesiones"

    @property
    def long_term_path(self) -> Path:
        return self.storage_dir / "memoria_largo_plazo.json"

    @property
    def derivaciones_path(self) -> Path:
        return self.storage_dir / "derivaciones.json"

    @property
    def index_dir(self) -> Path:
        return self.storage_dir / "faiss_index"

    @property
    def traces_path(self) -> Path:
        return self.storage_dir / "trazas.jsonl"
