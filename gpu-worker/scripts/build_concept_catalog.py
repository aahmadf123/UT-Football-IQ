"""Offline CLIP text-tower concept-catalog encoder (Issue #195).

Builds the precomputed CLIP text vectors that power genuine CLIP text-tower
zero-shot search (``POST /api/v1/search/text``). It reads the committed
American-football concept vocabulary
(``backend/app/data/concept_catalog.json`` — phrases, ``vector: null``),
runs each phrase through the **CLIP ViT-B/32 text tower**, and writes the
512-d L2-normalised vectors back into the file.

OFFLINE TOOLING — this script:

  * is **never** in the request path, same-session, or nightly pipeline,
  * is **not** wired into the model router (``pipeline.model_router``); the
    embeddings stage still routes to ``play-embed-clip-vitb32-baseline``,
  * commits **vectors only** — it must never commit CLIP model weights
    (``.pt`` / ``.pth`` / ``.safetensors`` are git-ignored),
  * is American football only — it refuses to encode soccer phrases.

The committed output is in CLIP's shared text-image space, so it can be
cosine-compared against ``playembeddings.clip_vector`` (the raw CLIP *image*
embedding the nightly embed stage now persists).

Usage::

    # Real build (needs CLIP weights via open_clip or transformers):
    python -m scripts.build_concept_catalog \
        --catalog ../backend/app/data/concept_catalog.json

    # Inspect the phrases that would be encoded, without loading CLIP:
    python -m scripts.build_concept_catalog --catalog <path> --dry-run

After a real build, commit the populated JSON (vectors, not weights).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# Mirror backend.app.models.PLAY_EMBEDDING_CLIP_DIM / stage_embed.CLIP_DIM.
CLIP_DIM = 512
DEFAULT_MODEL = "ViT-B-32"
DEFAULT_PRETRAINED = "openai"

# Defensive soccer guard — the committed vocabulary is American football only,
# but never silently encode a soccer phrase if one is ever introduced.
SOCCER_MARKERS: tuple[str, ...] = (
    " xg",
    "expected goals",
    "4-4-2",
    "offside",
    "pitch",
    "penalty kick",
    "corner kick",
    "midfielder",
    "striker",
    "goalkeeper",
    "premier league",
    "la liga",
)

EncodeFn = Callable[[Sequence[str]], list[list[float]]]


def _l2_normalise(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return list(values)
    return [v / norm for v in values]


def load_doc(path: Path) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def _assert_american_football(phrases: Sequence[str]) -> None:
    for phrase in phrases:
        low = f" {phrase.lower()} "
        hits = [m for m in SOCCER_MARKERS if m in low]
        if hits:
            raise ValueError(
                f"Refusing to encode a soccer phrase ({hits}): {phrase!r}. "
                "Football-IQ is American football only."
            )


def build_catalog(doc: dict[str, Any], encode_fn: EncodeFn) -> dict[str, Any]:
    """Return a copy of ``doc`` with every concept's ``vector`` filled.

    ``encode_fn`` maps a list of phrases to a list of CLIP text vectors. Kept
    injectable so the build is unit-testable without CLIP weights.
    """
    concepts = doc.get("concepts", [])
    phrases = [str(c["phrase"]) for c in concepts]
    _assert_american_football(phrases)

    vectors = encode_fn(phrases)
    if len(vectors) != len(concepts):
        raise ValueError(f"encoder returned {len(vectors)} vectors for {len(concepts)} phrases")

    out_concepts = []
    for concept, vec in zip(concepts, vectors, strict=True):
        vec_list = [float(x) for x in vec]
        if len(vec_list) != CLIP_DIM:
            raise ValueError(
                f"concept {concept.get('concept_id')!r}: expected {CLIP_DIM}-d, got {len(vec_list)}"
            )
        out_concepts.append({**concept, "vector": _l2_normalise(vec_list)})

    out = dict(doc)
    out["dim"] = CLIP_DIM
    out["generated_at"] = dt.datetime.now(dt.UTC).isoformat()
    out["concepts"] = out_concepts
    return out


def _load_clip_text_encoder(model: str, pretrained: str) -> EncodeFn:
    """Lazily build a CLIP text-tower encode function.

    Tries ``open_clip`` first, then ``transformers``. Never downloads or
    commits weights here beyond what those libraries fetch at runtime.
    """
    try:
        import open_clip  # type: ignore
        import torch  # type: ignore

        clip_model, _, _ = open_clip.create_model_and_transforms(model, pretrained=pretrained)
        tokenizer = open_clip.get_tokenizer(model)
        clip_model.eval()

        def encode(phrases: Sequence[str]) -> list[list[float]]:
            with torch.no_grad():
                tokens = tokenizer(list(phrases))
                feats = clip_model.encode_text(tokens)
                return [row.tolist() for row in feats.cpu()]

        return encode
    except ImportError:
        pass

    try:
        import torch  # type: ignore
        from transformers import CLIPModel, CLIPTokenizer  # type: ignore

        name = "openai/clip-vit-base-patch32"
        clip_model = CLIPModel.from_pretrained(name)
        tokenizer = CLIPTokenizer.from_pretrained(name)
        clip_model.eval()

        def encode(phrases: Sequence[str]) -> list[list[float]]:
            with torch.no_grad():
                inputs = tokenizer(list(phrases), padding=True, return_tensors="pt")
                feats = clip_model.get_text_features(**inputs)
                return [row.tolist() for row in feats.cpu()]

        return encode
    except ImportError as exc:  # pragma: no cover - depends on optional deps
        raise SystemExit(
            "No CLIP library available. Install one of:\n"
            "  pip install open_clip_torch torch\n"
            "  pip install transformers torch\n"
            f"(import error: {exc})"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the CLIP text-tower concept catalog.")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "backend" / "app" / "data" / "concept_catalog.json",
        help="Path to the concept_catalog.json (read phrases, write vectors in place by default).",
    )
    parser.add_argument("--out", type=Path, default=None, help="Output path (defaults to --catalog).")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="open_clip model name (ViT-B-32).")
    parser.add_argument("--pretrained", default=DEFAULT_PRETRAINED, help="open_clip pretrained tag.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the phrases that would be encoded; do not load CLIP or write.",
    )
    args = parser.parse_args(argv)

    doc = load_doc(args.catalog)
    concepts = doc.get("concepts", [])
    phrases = [str(c["phrase"]) for c in concepts]

    if args.dry_run:
        _assert_american_football(phrases)
        print(f"{len(phrases)} American-football concept phrases (dry run, no vectors written):")
        for c in concepts:
            print(f"  {c['concept_id']:<16} {c['phrase']}")
        return 0

    encode_fn = _load_clip_text_encoder(args.model, args.pretrained)
    out_doc = build_catalog(doc, encode_fn)
    out_path = args.out or args.catalog
    out_path.write_text(json.dumps(out_doc, indent=2) + "\n")
    print(
        f"Encoded {len(out_doc['concepts'])} concepts with {args.model}/{args.pretrained} "
        f"→ {out_path}\nCommit the populated JSON (vectors only — never CLIP weights)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
