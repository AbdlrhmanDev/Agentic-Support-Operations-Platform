"""Policy ingestion and search over pgvector."""

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PolicyChunk
from app.policies.embeddings import Embedder

DOCUMENTS_DIR = Path(__file__).parent / "documents"
_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


@dataclass(frozen=True)
class PolicyHit:
    chunk_id: str
    policy_id: str
    version: str
    title: str
    section: str
    content: str
    score: float


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse_policy(text: str) -> list[PolicyChunk]:
    """Split one policy document into a chunk per `##` section."""
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise ValueError("Policy document is missing its front matter block.")
    meta = dict(line.split(":", 1) for line in match.group(1).splitlines() if ":" in line)
    meta = {key.strip(): value.strip() for key, value in meta.items()}
    policy_id, version, title = meta["id"], meta["version"], meta["title"]

    chunks = []
    for block in re.split(r"^## ", text[match.end() :], flags=re.MULTILINE)[1:]:
        section, _, body = block.partition("\n")
        chunks.append(
            PolicyChunk(
                id=f"{policy_id}@{version}#{_slug(section)}",
                policy_id=policy_id,
                version=version,
                title=title,
                section=section.strip(),
                content=body.strip(),
            )
        )
    return chunks


def load_policies(directory: Path = DOCUMENTS_DIR) -> list[PolicyChunk]:
    """Read and split every policy document in a directory."""
    return [
        chunk
        for path in sorted(directory.glob("*.md"))
        for chunk in parse_policy(path.read_text(encoding="utf-8"))
    ]


async def ingest_policies(
    session: AsyncSession, embedder: Embedder, directory: Path = DOCUMENTS_DIR
) -> int:
    """Replace the stored policy chunks with the documents on disk."""
    chunks = await asyncio.to_thread(load_policies, directory)
    for chunk in chunks:
        chunk.embedding = await embedder.embed(f"{chunk.title}. {chunk.section}. {chunk.content}")
    await session.execute(delete(PolicyChunk))
    session.add_all(chunks)
    await session.flush()
    return len(chunks)


async def search_policies(
    session: AsyncSession, embedder: Embedder, query: str, top_k: int
) -> list[PolicyHit]:
    distance = PolicyChunk.embedding.cosine_distance(await embedder.embed(query))
    rows = await session.execute(
        select(PolicyChunk, distance.label("distance")).order_by(distance).limit(top_k)
    )
    return [
        PolicyHit(
            chunk_id=chunk.id,
            policy_id=chunk.policy_id,
            version=chunk.version,
            title=chunk.title,
            section=chunk.section,
            content=chunk.content,
            score=round(1.0 - float(dist), 4),
        )
        for chunk, dist in rows.all()
    ]
