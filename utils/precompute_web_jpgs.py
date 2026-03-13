#!/usr/bin/env python3
"""Precompute web-friendly JPEGs from TIFF files.

This script walks a root directory, finds `.tif` / `.tiff` files, and creates
matching `_web.jpg` files next to them. It is intended for preprocessing large
zone TIFFs ahead of time so the web app does not generate JPEGs during
requests.

Examples:
  python precompute_web_jpgs.py --root layout_data/Sonrisa
  python precompute_web_jpgs.py --root /mnt/fileshare/layout_data --force
  python precompute_web_jpgs.py --root layout_data/Sonrisa --only-zone-tiffs
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image


Image.MAX_IMAGE_PIXELS = 2_000_000_000


def iter_tiff_files(root: Path, only_zone_tiffs: bool) -> list[Path]:
    matches: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in {".tif", ".tiff"}:
            continue
        if only_zone_tiffs and not path.stem.lower().endswith("_zone"):
            continue
        matches.append(path)
    return sorted(matches)


def tif_to_rgb_image(tif_path: Path, max_dimension: int) -> Image.Image:
    with rasterio.open(tif_path) as src:
        img_data = src.read()

    if len(img_data.shape) == 3:
        if img_data.shape[0] >= 3:
            img_array = np.transpose(img_data[:3], (1, 2, 0))
        elif img_data.shape[0] == 1:
            img_array = np.dstack([img_data[0], img_data[0], img_data[0]])
        else:
            img_array = np.transpose(img_data, (1, 2, 0))
    else:
        img_array = np.dstack([img_data, img_data, img_data])

    if img_array.dtype != np.uint8:
        img_array_normalized = np.zeros_like(img_array, dtype=np.uint8)
        for i in range(img_array.shape[2]):
            band = img_array[:, :, i]
            band_min = np.nanmin(band)
            band_max = np.nanmax(band)
            if band_max > band_min:
                img_array_normalized[:, :, i] = (
                    (band - band_min) / (band_max - band_min) * 255
                ).astype(np.uint8)
        img_array = img_array_normalized

    image = Image.fromarray(img_array)
    if image.mode != "RGB":
        image = image.convert("RGB")

    if max(image.width, image.height) > max_dimension:
        ratio = max_dimension / max(image.width, image.height)
        new_size = (int(image.width * ratio), int(image.height * ratio))
        image = image.resize(new_size, Image.LANCZOS)

    return image


def convert_one_tiff(
    tif_path: Path,
    *,
    max_dimension: int,
    quality: int,
    force: bool,
) -> tuple[str, Path, float]:
    web_path = tif_path.with_name(f"{tif_path.stem}_web.jpg")
    existed_before = web_path.exists()
    if existed_before and not force:
        return ("skipped", web_path, 0.0)

    start = time.perf_counter()
    image = tif_to_rgb_image(tif_path, max_dimension=max_dimension)
    image.save(web_path, "JPEG", quality=quality, optimize=True)
    elapsed = time.perf_counter() - start
    return ("updated" if existed_before else "created", web_path, elapsed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Precompute _web.jpg files from TIFFs.")
    parser.add_argument(
        "--root",
        required=True,
        help="Root folder to scan (for example layout_data/Sonrisa or a mounted file share path).",
    )
    parser.add_argument(
        "--max-dimension",
        type=int,
        default=4000,
        help="Maximum width/height of generated JPEGs. Default: 4000",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=85,
        help="JPEG quality. Default: 85",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate _web.jpg even if it already exists.",
    )
    parser.add_argument(
        "--only-zone-tiffs",
        action="store_true",
        help="Only convert TIFFs whose basename ends with _zone.",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"Root path does not exist: {root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"Root path is not a directory: {root}", file=sys.stderr)
        return 1

    tif_files = iter_tiff_files(root, only_zone_tiffs=args.only_zone_tiffs)
    if not tif_files:
        print(f"No TIFF files found under {root}")
        return 0

    print(f"Scanning {root}")
    print(f"Found {len(tif_files)} TIFF files")

    created = 0
    skipped = 0
    failed = 0
    total_seconds = 0.0

    for tif_path in tif_files:
        try:
            web_path = tif_path.with_name(f"{tif_path.stem}_web.jpg")
            existed_before = web_path.exists()
            status, out_path, elapsed = convert_one_tiff(
                tif_path,
                max_dimension=args.max_dimension,
                quality=args.quality,
                force=args.force,
            )
            total_seconds += elapsed
            if status == "skipped":
                skipped += 1
                print(f"SKIP   {tif_path}")
            else:
                created += 1
                verb = "UPDATE" if existed_before and args.force else "CREATE"
                print(f"{verb:<6} {tif_path} -> {out_path} ({elapsed:.2f}s)")
        except Exception as exc:
            failed += 1
            print(f"ERROR  {tif_path}: {exc}", file=sys.stderr)

    print(
        f"Done. created={created} skipped={skipped} failed={failed} "
        f"elapsed={total_seconds:.2f}s"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
