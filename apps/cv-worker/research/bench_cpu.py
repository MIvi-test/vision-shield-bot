# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = [
#     "torch>=2.6",
#     "open_clip_torch>=2.32",
#     "transformers>=4.50",
#     "huggingface_hub>=0.30",
#     "pillow>=11.0",
#     "psutil>=6.0",
#     "sentencepiece>=0.2",
# ]
# ///
"""S1-09: замер кандидатов CV-моделей на CPU.

Каждая модель запускается в отдельном процессе, чтобы пиковая память не
смешивалась. Для каждой модели записываются лицензия и ревизия весов,
SHA-256 файла весов, время загрузки, время на изображение (p50/p95) и пиковая
RAM. Для моделей темы считается грубая разделимость suitable/off_topic на
наборе S1-15 — это проверка здравого смысла, а не оценка качества (S3-02).

Запуск из корня репозитория (зависимости ставятся в отдельное окружение,
pyproject.toml проекта не меняется):

    uv run apps/cv-worker/research/bench_cpu.py

Результаты: data/results/cv-bench/ (вне Git) и таблица в выводе.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_IMAGES = REPO_ROOT / "data" / "datasets" / "nature"
DEFAULT_MANIFEST = REPO_ROOT / "datasets" / "nature" / "manifest.jsonl"
DEFAULT_POLICY = (
    REPO_ROOT / "packages" / "contracts" / "policies" / "campaigns" / "nature-v0.json"
)
DEFAULT_OUT = REPO_ROOT / "data" / "results" / "cv-bench"
SANITIZED_MAX_SIDE = 2048  # output.max_side_px из media-profile-v0
SANITIZED_QUALITY = 85  # output.quality из media-profile-v0
WARMUP = 2


@dataclass(frozen=True)
class Candidate:
    name: str
    kind: str  # "topic" | "nsfw" | "baseline"
    repo: str  # Hugging Face repo id
    weight_files: tuple[str, ...]
    open_clip_name: str = ""  # для моделей без open_clip_config.json на HF
    open_clip_pretrained: str = ""


CANDIDATES = {
    c.name: c
    for c in [
        Candidate(
            "clip-vit-b32-laion2b",
            "topic",
            "laion/CLIP-ViT-B-32-laion2B-s34B-b79K",
            ("open_clip_model.safetensors", "open_clip_pytorch_model.bin"),
        ),
        Candidate(
            "clip-vit-b32-xlmr-laion5b",
            "topic",
            "laion/CLIP-ViT-B-32-xlm-roberta-base-laion5B-s13B-b90k",
            ("open_clip_model.safetensors", "open_clip_pytorch_model.bin"),
            open_clip_name="xlm-roberta-base-ViT-B-32",
            open_clip_pretrained="laion5b_s13b_b90k",
        ),
        Candidate(
            "nsfw-falconsai-vit",
            "nsfw",
            "Falconsai/nsfw_image_detection",
            ("model.safetensors", "pytorch_model.bin"),
        ),
        Candidate(
            "nsfw-adamcodd-vit",
            "nsfw",
            "AdamCodd/vit-base-nsfw-detector",
            ("model.safetensors", "pytorch_model.bin"),
        ),
        # Базовый уровень без модели: доля зелёного в кадре. CLIP должен его обогнать.
        Candidate("baseline-green", "baseline", "", ()),
    ]
}
DEFAULT_MODELS = list(CANDIDATES)


# --- общие утилиты ----------------------------------------------------------


def peak_rss_bytes() -> int:
    import psutil

    info = psutil.Process().memory_info()
    peak = getattr(info, "peak_wset", None)  # Windows
    if peak is not None:
        return int(peak)
    import resource  # Linux/macOS

    maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(maxrss if sys.platform == "darwin" else maxrss * 1024)


def current_rss_bytes() -> int:
    import psutil

    return int(psutil.Process().memory_info().rss)


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def auc(positive: list[float], negative: list[float]) -> float | None:
    """Вероятность, что случайный suitable получит score выше случайного off_topic."""
    if not positive or not negative:
        return None
    wins = sum(
        1.0 if p > n else 0.5 if p == n else 0.0 for p in positive for n in negative
    )
    return wins / (len(positive) * len(negative))


def load_labels(manifest: Path) -> dict[str, tuple[str, str | None]]:
    if not manifest.exists():
        return {}
    rows = [
        json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()
    ]
    return {r["id"]: (r["label"], r.get("category")) for r in rows}


def sanitized_like(path: Path) -> bytes:
    """Похоже на результат media worker: поворот по EXIF, RGB, ≤2048 px, JPEG q85."""
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = 25_000_000
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((SANITIZED_MAX_SIDE, SANITIZED_MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=SANITIZED_QUALITY)
    return buf.getvalue()


def synthetic_images(n: int) -> list[tuple[str, bytes]]:
    from PIL import Image

    out = []
    for i in range(n):
        im = Image.effect_noise((2048, 1365), 64).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=SANITIZED_QUALITY)
        out.append((f"synthetic-{i:03d}", buf.getvalue()))
    return out


def hf_metadata(candidate: Candidate) -> dict[str, Any]:
    """Лицензия и ревизия с карточки модели, SHA-256 файла весов из кэша."""
    if not candidate.repo:
        return {}
    meta: dict[str, Any] = {"source": f"hf-hub:{candidate.repo}"}
    from huggingface_hub import model_info, try_to_load_from_cache

    try:
        info = model_info(candidate.repo)
        card = info.card_data.to_dict() if info.card_data else {}
        meta["revision"] = info.sha
        meta["license"] = card.get("license")
    except Exception as exc:  # noqa: BLE001 — метаданные не должны ронять замер
        meta["metadata_error"] = type(exc).__name__
    for filename in candidate.weight_files:
        cached = try_to_load_from_cache(candidate.repo, filename)
        if isinstance(cached, str) and os.path.exists(cached):
            digest = hashlib.sha256()
            with open(cached, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            meta["weights_file"] = filename
            meta["weights_sha256"] = digest.hexdigest()
            meta["weights_bytes"] = os.path.getsize(cached)
            break
    return meta


# --- загрузчики моделей -----------------------------------------------------

Scorer = Callable[[bytes], float]


def load_topic(candidate: Candidate, policy: dict[str, Any]) -> Scorer:
    import open_clip
    import torch
    from PIL import Image

    if candidate.open_clip_name:
        model, _, preprocess = open_clip.create_model_and_transforms(
            candidate.open_clip_name, pretrained=candidate.open_clip_pretrained
        )
        tokenizer = open_clip.get_tokenizer(candidate.open_clip_name)
    else:
        hub = f"hf-hub:{candidate.repo}"
        model, _, preprocess = open_clip.create_model_and_transforms(hub)
        tokenizer = open_clip.get_tokenizer(hub)
    model.eval()
    positive = policy["cv"]["topic"]["positive_prompts"]
    negative = policy["cv"]["topic"]["negative_prompts"]
    with torch.no_grad():
        text = model.encode_text(tokenizer(positive + negative))
        text = text / text.norm(dim=-1, keepdim=True)
        scale = model.logit_scale.exp()

    def score(data: bytes) -> float:
        with Image.open(io.BytesIO(data)) as im:
            pixels = preprocess(im.convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            image = model.encode_image(pixels)
            image = image / image.norm(dim=-1, keepdim=True)
            probs = (scale * image @ text.T).softmax(dim=-1)[0]
        return float(probs[: len(positive)].sum())

    return score


def load_nsfw(candidate: Candidate) -> Scorer:
    import torch
    from PIL import Image
    from transformers import AutoImageProcessor, AutoModelForImageClassification

    processor = AutoImageProcessor.from_pretrained(candidate.repo)
    model = AutoModelForImageClassification.from_pretrained(candidate.repo)
    model.eval()
    labels = {int(i): str(name).lower() for i, name in model.config.id2label.items()}
    nsfw_ids = [i for i, name in labels.items() if "nsfw" in name or "porn" in name]
    if not nsfw_ids:
        raise RuntimeError(f"Не найден класс nsfw среди меток {labels}")

    def score(data: bytes) -> float:
        with Image.open(io.BytesIO(data)) as im:
            inputs = processor(images=im.convert("RGB"), return_tensors="pt")
        with torch.no_grad():
            probs = model(**inputs).logits.softmax(dim=-1)[0]
        return float(sum(probs[i] for i in nsfw_ids))

    return score


def load_baseline() -> Scorer:
    from PIL import Image

    def score(data: bytes) -> float:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB").resize((224, 224))
            r, g, b = (sum(c) / len(c) for c in zip(*im.getdata()))
        return g / max(r + g + b, 1.0)

    return score


# --- один процесс = одна модель ---------------------------------------------


def run_single(args: argparse.Namespace) -> dict[str, Any]:
    candidate = CANDIDATES[args.single]
    if candidate.kind != "baseline":
        import torch

        if args.threads:
            torch.set_num_threads(args.threads)
        threads = torch.get_num_threads()
    else:
        threads = 1
    images = [(p.stem, p.read_bytes()) for p in sorted(Path(args.cache).glob("*.jpg"))]
    if not images:
        raise RuntimeError("Нет подготовленных изображений")
    labels = load_labels(Path(args.manifest))

    rss_before = current_rss_bytes()
    t0 = time.perf_counter()
    if candidate.kind == "topic":
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
        scorer = load_topic(candidate, policy)
    elif candidate.kind == "nsfw":
        scorer = load_nsfw(candidate)
    else:
        scorer = load_baseline()
    load_s = time.perf_counter() - t0
    rss_loaded = current_rss_bytes()

    for _, data in images[:WARMUP]:
        scorer(data)
    timings, scores = [], {}
    for image_id, data in images:
        t = time.perf_counter()
        scores[image_id] = scorer(data)
        timings.append((time.perf_counter() - t) * 1000)

    result: dict[str, Any] = {
        "model": candidate.name,
        "kind": candidate.kind,
        "threads": threads,
        "images": len(images),
        "load_s": round(load_s, 2),
        "rss_before_mb": round(rss_before / 2**20),
        "rss_loaded_mb": round(rss_loaded / 2**20),
        "peak_rss_mb": round(peak_rss_bytes() / 2**20),
        "ms_p50": round(statistics.median(timings), 1),
        "ms_p95": round(percentile(timings, 0.95), 1),
        "ms_max": round(max(timings), 1),
        **hf_metadata(candidate),
    }
    by_label: dict[str, list[float]] = {}
    for image_id, value in scores.items():
        label, category = labels.get(image_id, ("unlabeled", None))
        key = f"{label}:{category}" if category else label
        by_label.setdefault(key, []).append(value)
    result["mean_score_by_label"] = {
        k: round(statistics.fmean(v), 3) for k, v in sorted(by_label.items())
    }
    if candidate.kind in {"topic", "baseline"}:
        value = auc(by_label.get("suitable", []), by_label.get("off_topic", []))
        result["auc_suitable_vs_off_topic"] = None if value is None else round(value, 3)
    result["scores"] = {k: round(v, 4) for k, v in scores.items()}
    return result


# --- управляющий процесс ----------------------------------------------------


def system_info() -> dict[str, Any]:
    import psutil

    return {
        "os": f"{platform.system()} {platform.release()}",
        "cpu": platform.processor() or platform.machine(),
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "ram_gb": round(psutil.virtual_memory().total / 2**30, 1),
        "python": platform.python_version(),
    }


def prepare_cache(images_dir: Path, cache: Path, synthetic: int) -> int:
    cache.mkdir(parents=True, exist_ok=True)
    for old in cache.glob("*.jpg"):
        old.unlink()
    sources = (
        sorted(
            p
            for p in images_dir.glob("*")
            if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )
        if images_dir.exists()
        else []
    )
    if sources:
        for path in sources:
            (cache / f"{path.stem}.jpg").write_bytes(sanitized_like(path))
        return len(sources)
    print(f"Нет изображений в {images_dir}; используются синтетические (только время).")
    for image_id, data in synthetic_images(synthetic):
        (cache / f"{image_id}.jpg").write_bytes(data)
    return synthetic


def print_table(results: list[dict[str, Any]]) -> None:
    head = "| Модель | Загрузка, с | Пик RAM, МБ | p50, мс | p95, мс | AUC | Лицензия |"
    print("\n" + head + "\n| --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        if "error" in r:
            print(f"| {r['model']} | ошибка: {r['error']} | | | | | |")
            continue
        print(
            f"| {r['model']} | {r['load_s']} | {r['peak_rss_mb']} | {r['ms_p50']} "
            f"| {r['ms_p95']} | {r.get('auc_suitable_vs_off_topic', '—')} "
            f"| {r.get('license', '—')} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models", nargs="+", default=DEFAULT_MODELS, choices=list(CANDIDATES)
    )
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGES)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--threads", type=int, default=0, help="0 = как решит PyTorch")
    parser.add_argument("--synthetic", type=int, default=30)
    parser.add_argument("--single", choices=list(CANDIDATES), help=argparse.SUPPRESS)
    parser.add_argument("--cache", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.single:
        try:
            result = run_single(args)
        except Exception as exc:  # noqa: BLE001 — ошибка одной модели не роняет остальные
            result = {"model": args.single, "error": f"{type(exc).__name__}: {exc}"}
        print("RESULT " + json.dumps(result, ensure_ascii=False))
        return 0

    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.out / "sanitized-cache"
    print("Готовлю изображения (как после media worker: ≤2048 px, JPEG q85)...")
    count = prepare_cache(args.images, cache, args.synthetic)
    print(f"Изображений: {count}")

    results = []
    for name in args.models:
        print(
            f"\n== {name}: загрузка и замер (первый запуск скачивает веса)...",
            flush=True,
        )
        cmd = [
            sys.executable,
            __file__,
            "--single",
            name,
            "--cache",
            str(cache),
            "--manifest",
            str(args.manifest),
            "--policy",
            str(args.policy),
            "--threads",
            str(args.threads),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
        if lines:
            result = json.loads(lines[-1][len("RESULT ") :])
        else:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            result = {
                "model": name,
                "error": " / ".join(tail) or f"код {proc.returncode}",
            }
        results.append(result)
        print(
            "   ",
            {
                k: v
                for k, v in result.items()
                if k not in {"scores", "mean_score_by_label"}
            },
        )

    report = {"system": system_info(), "threads_arg": args.threads, "results": results}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_file = args.out / f"bench-{stamp}.json"
    out_file.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\nСистема:", report["system"])
    print_table(results)
    print(f"\nПолный результат: {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
