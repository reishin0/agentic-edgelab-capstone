"""Versioned public-method notes and the Checkpoint 3.1 retrieval pipeline."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from math import isfinite

from .contracts import fingerprint
from .providers import ProviderError, Transport, pinecone_url, post_json


NAMESPACE = "edgelab-public-methods-v1"
EMBED = "llama-text-embed-v2"
RERANK = "bge-reranker-v2-m3"
DIMENSION = 1024
INFERENCE = "https://api.pinecone.io"
QUERY = {
    "cointegration": "Engle-Granger I(1) inputs and ADF and KPSS diagnostics on levels and first differences",
    "evaluation": "Factual evidence fidelity and evaluation limitations",
    "retrieval": "Semantic embedding retrieval, reranking and governed tool use",
}


@dataclass(frozen=True, slots=True)
class Note:
    id: str
    topic: str
    url: str
    locator: str
    text: str

    def metadata(self) -> dict:
        return {**asdict(self), "corpus_version": NAMESPACE, "public": True,
                "reviewed": True, "active": True, "content_sha256": fingerprint(asdict(self))}


STATSMODELS = "https://www.statsmodels.org/stable/generated/statsmodels.tsa.stattools."
CORPUS = (
    Note("eg", "cointegration", STATSMODELS + "coint.html", "Description and Notes",
         "The augmented Engle-Granger test assumes the input series are integrated of order one. "
         "Its null is no cointegration. Use its own cointegration p-value, not the ordinary ADF p-value on fitted residuals."),
    Note("adf", "cointegration", STATSMODELS + "adfuller.html", "Description and Notes",
         "The ADF null is a unit root. Specify deterministic terms and lag policy before testing. "
         "Failure to reject is not proof of a unit root."),
    Note("kpss", "cointegration", STATSMODELS + "kpss.html", "Description and Notes",
         "KPSS tests a null of stationarity around the chosen deterministic component. "
         "Pair it with ADF on levels and first differences; conflicting diagnostics leave the classification unresolved."),
    Note("feedback", "evaluation", "https://arxiv.org/abs/2204.05862", "Version 1, limitations",
         "Human preferences are not a guarantee of factual correctness. Check evidence support separately from answer quality."),
    Note("evaluation", "evaluation", "https://arxiv.org/abs/2204.05862", "Version 1, evaluations",
         "Evaluate helpfulness and harms explicitly. A reward or preference measure cannot replace review of specific failures."),
    Note("embed", "retrieval", "https://docs.pinecone.io/models/llama-text-embed-v2", "Configuration",
         "Use the same embedding family and vector dimension at ingestion and search. Distinguish query from passage input types."),
    Note("rerank", "retrieval", "https://docs.pinecone.io/reference/api/2025-10/inference/rerank", "Request and response",
         "A reranker compares candidate text with the query; its scores describe relevance, not correctness. "
         "Reranking does not recover evidence that was never retrieved."),
    Note("tool-use", "retrieval",
         "https://developers.openai.com/cookbook/examples/responses_api/responses_api_tool_orchestration",
         "Tool orchestration example",
         "Keep tool selection and execution separate. The application orchestrates calls and uses the returned outputs as evidence."),
)
BY_ID = {note.id: note for note in CORPUS}
CORPUS_SHA256 = fingerprint([asdict(note) for note in CORPUS])


def local_notes(topic: str) -> tuple[Note, ...]:
    if topic not in QUERY:
        raise ValueError("Unknown public-method topic")
    return tuple(note for note in CORPUS if note.topic == topic)


def _score(value: object) -> float:
    if type(value) not in {int, float} or not isfinite(value):
        raise ValueError("Invalid provider score")
    return float(value)


@dataclass(frozen=True, slots=True)
class PineconeMethods:
    index_host: str
    api_key: str = field(repr=False)
    transport: Transport = field(default=post_json, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "index_host", pinecone_url(self.index_host))
        if not self.api_key:
            raise ValueError("PINECONE_API_KEY is required for Pinecone mode")

    def _post(self, url: str, payload: dict) -> dict:
        return self.transport(url, {
            "Api-Key": self.api_key, "X-Pinecone-Api-Version": "2025-10",
        }, payload, 15)

    def _vectors(self, texts: list[str], input_type: str) -> list[list[float]]:
        result = self._post(INFERENCE + "/embed", {
            "model": EMBED, "parameters": {"input_type": input_type, "dimension": DIMENSION, "truncate": "NONE"},
            "inputs": [{"text": text} for text in texts],
        })
        try:
            vectors = [row["values"] for row in result["data"]]
            if len(vectors) != len(texts) or any(
                not isinstance(vector, list) or len(vector) != DIMENSION for vector in vectors
            ):
                raise ValueError
            return [[_score(value) for value in vector] for vector in vectors]
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError("Pinecone returned an invalid embedding batch") from None

    def seed(self, reviewed_digest: str) -> int:
        if reviewed_digest != CORPUS_SHA256:
            raise ValueError("Review the exact public corpus and supply its digest before upsert")
        vectors = self._vectors([note.text for note in CORPUS], "passage")
        reply = self._post(self.index_host + "/vectors/upsert", {
            "namespace": NAMESPACE, "vectors": [
                {"id": note.id, "values": vector, "metadata": note.metadata()}
                for note, vector in zip(CORPUS, vectors, strict=True)
            ],
        })
        if reply.get("upsertedCount") != len(CORPUS):
            raise ProviderError("Pinecone did not confirm the complete upsert")
        return len(CORPUS)

    def search(self, topic: str) -> tuple[Note, ...]:
        if topic not in QUERY:
            raise ValueError("Only fixed public-method topics may be sent to Pinecone")
        query = QUERY[topic]
        vector, = self._vectors([query], "query")
        reply = self._post(self.index_host + "/query", {
            "vector": vector, "namespace": NAMESPACE, "topK": 8, "includeMetadata": True,
            "filter": {key: {"$eq": value} for key, value in {
                "corpus_version": NAMESPACE, "topic": topic, "public": True, "reviewed": True, "active": True,
            }.items()},
        })
        try:
            matches = reply["matches"]
            if reply.get("namespace") != NAMESPACE or not isinstance(matches, list) or len(matches) > 8:
                raise ValueError
            candidates = {}
            for hit in matches:
                note = BY_ID.get(hit["id"])
                if (note and note.topic == topic and _score(hit["score"]) >= 0.30
                        and hit["metadata"] == note.metadata()
                        and all(hit["metadata"][flag] is True for flag in ("public", "reviewed", "active"))):
                    candidates[note.id] = note
            if not candidates:
                return ()
            notes = list(candidates.values())
            reranked = self._post(INFERENCE + "/rerank", {
                "model": RERANK, "query": query, "top_n": len(notes), "return_documents": False,
                "documents": [{"id": note.id, "text": note.text} for note in notes],
                "parameters": {"truncate": "END"},
            })["data"]
            if not isinstance(reranked, list) or len(reranked) > len(notes):
                raise ValueError
            ranks = {}
            for row in reranked:
                index = row["index"]
                if type(index) is not int or not 0 <= index < len(notes) or index in ranks:
                    raise ValueError
                ranks[index] = _score(row["score"])
            selected, seen, sources = [], set(), Counter()
            for index in sorted(ranks, key=lambda i: (-ranks[i], notes[i].id)):
                note = notes[index]
                if ranks[index] >= 0.50 and note.text not in seen and sources[note.url] < 2:
                    selected.append(note)
                    seen.add(note.text)
                    sources[note.url] += 1
                if len(selected) == 3:
                    break
            return tuple(selected)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError, OverflowError):
            raise ProviderError("Pinecone returned invalid retrieval data") from None
