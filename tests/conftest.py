"""Fixtures compartidas. Ningún test usa red ni gasta la API key:
- Embeddings: DeterministicFakeEmbedding (vectores falsos pero reproducibles).
- LLM: FakeToolModel, que devuelve respuestas predefinidas (incluyendo llamadas a tools).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from typing import Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from biblioteca_agent.config import Settings  # noqa: E402
from biblioteca_agent.rag import ReglamentoRetriever  # noqa: E402


class FakeToolModel(BaseChatModel):
    """LLM falso: devuelve en orden los mensajes entregados (incluidas llamadas a tools).
    Acepta bind_tools, que es lo que necesita create_tool_calling_agent."""

    messages: Iterator[AIMessage]

    @property
    def _llm_type(self) -> str:
        return "fake-tool-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=next(self.messages))])

    def bind_tools(self, tools, **kwargs):
        return self


def tool_call(nombre: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": nombre, "args": args, "id": call_id}])


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.storage_dir = tmp_path / "storage"
    s.data_dir = ROOT / "data"
    s.memory_window_k = 10
    return s


@pytest.fixture
def retriever(settings) -> ReglamentoRetriever:
    return ReglamentoRetriever.from_settings(settings, embeddings=DeterministicFakeEmbedding(size=64),
                                             rebuild=True)


@pytest.fixture
def fake_llm():
    def _crear(respuestas: list) -> FakeToolModel:
        return FakeToolModel(messages=iter(respuestas))
    return _crear
