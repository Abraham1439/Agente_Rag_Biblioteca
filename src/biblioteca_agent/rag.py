"""Pipeline RAG sobre el reglamento (reutiliza las decisiones del EP1).

- 6 documentos temáticos en data/.
- Un fragmento por artículo, con su sección y número de artículo como encabezado
  y metadato (el corte por caracteres del EP1 generaba fragmentos vacíos).
- Embeddings mistral-embed vía endpoint compatible con OpenAI.
- Índice FAISS guardado en disco (storage/faiss_index) con una huella de los datos
  y parámetros: si algo cambia, se regenera solo.
- score = 1 / (1 + distancia) y umbral 0.45 como primer filtro.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import Settings

DOCUMENT_TYPES = {
    "reglamento_general.txt": "reglamento_general",
    "prestamos_renovaciones.txt": "prestamos",
    "morosos_sanciones.txt": "sanciones",
    "normas_disciplinarias.txt": "normas_disciplinarias",
    "salas_estudio.txt": "salas_estudio",
    "uso_lentes_vr.txt": "lentes_vr",
}


def load_documents(data_dir: Path) -> list[Document]:
    documents = []
    for filename, doc_type in DOCUMENT_TYPES.items():
        path = Path(data_dir) / filename
        if not path.is_file():
            raise FileNotFoundError(f"No se encontró el documento requerido: {path}")
        documents.append(
            Document(page_content=path.read_text(encoding="utf-8"),
                     metadata={"tipo": doc_type, "fuente": filename})
        )
    return documents


CHUNKER_VERSION = "articulos-v1"
_RE_ARTICULO = re.compile(r"^Artículo (\d+)°")


def _es_titulo_seccion(linea: str) -> bool:
    letras = [c for c in linea if c.isalpha()]
    return bool(letras) and linea == linea.upper() and len(linea) < 90 and not _RE_ARTICULO.match(linea)


def _unidades(doc: Document) -> list[dict]:
    """Divide un documento en unidades: un artículo, o un bloque sin artículo bajo una sección."""
    seccion, unidades, actual = "", [], None
    for linea in doc.page_content.splitlines():
        s = linea.strip()
        if s.startswith("REGLAMENTO DE BIBLIOTECAS") or s.startswith("Fuente"):
            continue  # encabezado del archivo: no aporta contenido normativo
        m = _RE_ARTICULO.match(s)
        if m or (s and _es_titulo_seccion(s)):
            if actual and actual["lineas"]:
                unidades.append(actual)
            if m:
                actual = {"seccion": seccion, "articulo": f"Artículo {m.group(1)}°", "lineas": []}
            else:
                seccion = s
                actual = {"seccion": seccion, "articulo": "", "lineas": []}
            continue
        if actual is None:
            actual = {"seccion": seccion, "articulo": "", "lineas": []}
        if s or actual["lineas"]:
            actual["lineas"].append(linea)
    if actual and "".join(actual["lineas"]).strip():
        unidades.append(actual)
    return [u for u in unidades if "".join(u["lineas"]).strip()]


def split_documents(documents: list[Document], chunk_size: int, chunk_overlap: int) -> list[Document]:
    """Un fragmento por artículo, encabezado con su sección y número de artículo.

    En el EP1 el corte por caracteres generaba fragmentos que solo tenían el
    encabezado del archivo ("REGLAMENTO ... DE LOS PRÉSTAMOS") o solo "Artículo 1°",
    y partía artículos largos perdiendo su número. Esos fragmentos vacíos ocupaban
    lugares del top_k. Ahora cada artículo es una unidad completa; solo si supera
    `chunk_size` se divide, y cada parte conserva el encabezado.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", " ", ""],
    )
    chunks = []
    for doc in documents:
        for u in _unidades(doc):
            encabezado = " · ".join(p for p in (u["seccion"], u["articulo"]) if p)
            cuerpo = "\n".join(u["lineas"]).strip()
            partes = [cuerpo] if len(cuerpo) + len(encabezado) + 1 <= chunk_size else splitter.split_text(cuerpo)
            for parte in partes:
                chunks.append(Document(
                    page_content=f"{encabezado}\n{parte}",
                    metadata={**doc.metadata, "articulo": u["articulo"], "seccion": u["seccion"]},
                ))
    return chunks


def build_embeddings(settings: Settings) -> Embeddings:
    from langchain_openai import OpenAIEmbeddings

    if not settings.embedding_api_key:
        raise RuntimeError("Falta EMBEDDING_API_KEY (Mistral) en el archivo .env")
    return OpenAIEmbeddings(
        base_url=settings.embedding_base_url,
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        check_embedding_ctx_length=False,
    )


def score_from_distance(distance: float) -> float:
    return 1.0 / (1.0 + max(float(distance), 0.0))


@dataclass
class Fragmento:
    texto: str
    fuente: str
    score: float
    articulo: str = ""


def huella_indice(settings: Settings) -> str:
    """Identifica con qué datos y parámetros se construyó el índice.
    Si cambian los documentos, el chunking o el modelo de embeddings, el índice se regenera solo."""
    h = hashlib.sha256()
    h.update(f"{CHUNKER_VERSION}|{settings.chunk_size}|{settings.chunk_overlap}|{settings.embedding_model}".encode())
    for filename in DOCUMENT_TYPES:
        path = Path(settings.data_dir) / filename
        if path.is_file():
            h.update(path.read_bytes())
    return h.hexdigest()[:16]


class ReglamentoRetriever:
    """Envuelve el índice FAISS y aplica el umbral de recuperación."""

    def __init__(self, vectorstore, top_k: int, threshold: float):
        self.vectorstore = vectorstore
        self.top_k = top_k
        self.threshold = threshold

    @classmethod
    def from_settings(cls, settings: Settings, embeddings: Embeddings | None = None,
                      rebuild: bool = False) -> "ReglamentoRetriever":
        from langchain_community.vectorstores import FAISS

        embeddings = embeddings or build_embeddings(settings)
        index_dir = settings.index_dir
        meta_path = index_dir / "huella.json"
        huella = huella_indice(settings)
        guardada = json.loads(meta_path.read_text()).get("huella") if meta_path.is_file() else None
        if index_dir.exists() and not rebuild and guardada == huella:
            # El índice lo generamos nosotros mismos, por eso se permite cargar el pickle.
            vs = FAISS.load_local(str(index_dir), embeddings, allow_dangerous_deserialization=True)
        else:
            chunks = split_documents(load_documents(settings.data_dir),
                                     settings.chunk_size, settings.chunk_overlap)
            vs = FAISS.from_documents(chunks, embeddings)
            index_dir.mkdir(parents=True, exist_ok=True)
            vs.save_local(str(index_dir))
            meta_path.write_text(json.dumps({"huella": huella, "chunks": len(chunks)}))
        return cls(vs, settings.top_k, settings.score_threshold)

    def buscar(self, consulta: str) -> list[Fragmento]:
        resultados = self.vectorstore.similarity_search_with_score(consulta, k=self.top_k)
        return [
            Fragmento(doc.page_content, doc.metadata.get("fuente", "desconocida"), score_from_distance(d),
                      doc.metadata.get("articulo", ""))
            for doc, d in resultados
        ]

    def hay_respaldo(self, fragmentos: list[Fragmento]) -> bool:
        return bool(fragmentos) and max(f.score for f in fragmentos) >= self.threshold
