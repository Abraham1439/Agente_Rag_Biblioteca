"""Pruebas de memoria (corto y largo plazo) y del retriever."""
import pytest

from biblioteca_agent.memory import LongTermMemory, crear_memoria_corto_plazo
from biblioteca_agent.rag import ReglamentoRetriever, load_documents, split_documents
from biblioteca_agent.tools import build_tools
from biblioteca_agent.memory import DerivacionStore


def test_ventana_envia_solo_ultimas_k_pero_guarda_todo(settings):
    mem = crear_memoria_corto_plazo(settings.sessions_dir, "s1", k=2)
    for i in range(4):
        mem.save_context({"input": f"pregunta {i}"}, {"output": f"respuesta {i}"})
    ventana = mem.load_memory_variables({})["chat_history"]
    assert [m.content for m in ventana] == ["pregunta 2", "respuesta 2", "pregunta 3", "respuesta 3"]
    assert len(mem.chat_memory.messages) == 8  # el archivo conserva toda la sesión


def test_historial_sobrevive_a_un_reinicio(settings):
    mem = crear_memoria_corto_plazo(settings.sessions_dir, "s1", k=10)
    mem.save_context({"input": "soy de Plaza Norte"}, {"output": "anotado"})
    # Simula reiniciar la app: se crea un objeto nuevo leyendo el mismo archivo.
    mem2 = crear_memoria_corto_plazo(settings.sessions_dir, "s1", k=10)
    assert mem2.load_memory_variables({})["chat_history"][0].content == "soy de Plaza Norte"


def test_sesiones_distintas_no_comparten_corto_plazo(settings):
    crear_memoria_corto_plazo(settings.sessions_dir, "s1", 10).save_context({"input": "a"}, {"output": "b"})
    assert crear_memoria_corto_plazo(settings.sessions_dir, "s2", 10).load_memory_variables({})["chat_history"] == []


def test_largo_plazo_persiste_y_valida(settings):
    lt = LongTermMemory(settings.long_term_path)
    lt.guardar("seb", "tipo_usuario", "Interno")
    lt.guardar("seb", "sede", "Plaza Norte")
    assert LongTermMemory(settings.long_term_path).obtener("seb") == {"tipo_usuario": "interno", "sede": "Plaza Norte"}
    with pytest.raises(ValueError):
        lt.guardar("seb", "tipo_usuario", "vip")
    lt.olvidar("seb")
    assert lt.obtener("seb") == {}


def test_un_fragmento_por_articulo(settings):
    chunks = split_documents(load_documents(settings.data_dir), settings.chunk_size, settings.chunk_overlap)
    articulos = [c.metadata["articulo"] for c in chunks if c.metadata["articulo"]]
    # Artículos 1 a 34 salvo el 16 (no está en los TXT) + el bloque de información operativa de la sede.
    assert len(chunks) == 34 and len(set(articulos)) == 33
    # Ningún fragmento es solo un encabezado (el problema del corte por caracteres del EP1).
    assert not any(c.page_content.startswith("REGLAMENTO DE BIBLIOTECAS") for c in chunks)
    assert all(len(c.page_content.split("\n", 1)[1].strip()) > 40 for c in chunks)


def test_articulo_15_queda_completo_y_con_contexto(settings):
    chunks = split_documents(load_documents(settings.data_dir), settings.chunk_size, settings.chunk_overlap)
    art15 = [c.page_content for c in chunks if c.metadata["articulo"] == "Artículo 15°"]
    assert len(art15) == 1
    assert art15[0].startswith("DE LOS PRÉSTAMOS · Artículo 15°")
    assert "Colección General: tiempo de préstamo de 7 días" in art15[0]
    assert "préstamo Inter sede" in art15[0]  # el final del artículo no quedó separado


def test_articulo_largo_se_divide_conservando_encabezado(settings):
    chunks = split_documents(load_documents(settings.data_dir), chunk_size=500, chunk_overlap=80)
    partes = [c.page_content for c in chunks if c.metadata["articulo"] == "Artículo 15°"]
    assert len(partes) > 1 and all(p.startswith("DE LOS PRÉSTAMOS · Artículo 15°") for p in partes)


def test_indice_se_regenera_si_cambia_el_chunking(settings, retriever):
    import json
    from langchain_core.embeddings import DeterministicFakeEmbedding
    huella_antes = json.loads((settings.index_dir / "huella.json").read_text())["huella"]
    settings.chunk_size = 500
    ReglamentoRetriever.from_settings(settings, embeddings=DeterministicFakeEmbedding(size=64))
    assert json.loads((settings.index_dir / "huella.json").read_text())["huella"] != huella_antes


def test_indice_se_guarda_y_se_recarga(settings, retriever):
    from langchain_core.embeddings import DeterministicFakeEmbedding
    assert settings.index_dir.exists()
    recargado = ReglamentoRetriever.from_settings(settings, embeddings=DeterministicFakeEmbedding(size=64))
    assert len(recargado.buscar("multa por atraso")) == settings.top_k


def test_umbral_devuelve_sin_respaldo(settings, retriever):
    retriever.threshold = 1.01  # ningún score puede superar 1
    lt, der = LongTermMemory(settings.long_term_path), DerivacionStore(settings.derivaciones_path)
    buscar = {t.name: t for t in build_tools("seb", retriever, lt, der)}["buscar_reglamento"]
    assert buscar.invoke({"consulta": "¿venden café?"}).startswith("SIN_RESPALDO")


def test_busqueda_con_respaldo_incluye_fuente(settings, retriever):
    retriever.threshold = 0.0
    lt, der = LongTermMemory(settings.long_term_path), DerivacionStore(settings.derivaciones_path)
    buscar = {t.name: t for t in build_tools("seb", retriever, lt, der)}["buscar_reglamento"]
    salida = buscar.invoke({"consulta": "multa"})
    assert "| fuente:" in salida and "Artículo" in salida
