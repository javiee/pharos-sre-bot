from fastembed import TextEmbedding

# This model name and its 384-dim output are part of our data contract:
# the Qdrant collection MUST be created with the same dimension.
EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384


class Embedder:
    """Wraps a fastembed model. Loads the model once, embeds many times."""

    def __init__(self) -> None:
        # Constructing TextEmbedding downloads (first run) and loads the model.
        # We do it once and reuse it — model loading is the expensive part.
        self._model = TextEmbedding(model_name=EMBED_MODEL)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Turn a list of strings into a list of 384-float vectors."""
        # fastembed returns a generator of numpy arrays; we materialise them
        # as plain Python lists because that's what the Qdrant client wants.
        return [vector.tolist() for vector in self._model.embed(texts)]