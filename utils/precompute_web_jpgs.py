#!/usr/bin/env python3
"""Precompute web-friendly JPEGs from zone TIFF files.

This script walks a root directory, finds zone TIFF files whose basename
matches `G<number>_zone`, and creates matching `_web.jpg` files next to them.
It is intended for preprocessing large zone TIFFs ahead of time so the web app
does not generate JPEGs during requests.

Examples:
  python precompute_web_jpgs.py --root layout_data/Sonrisa
  python precompute_web_jpgs.py --root /mnt/fileshare/layout_data --force
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from itertools import chain
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image


Image.MAX_IMAGE_PIXELS = 2_000_000_000
ZONE_TIFF_STEM_RE = re.compile(r"^G\d+_zone$", re.IGNORECASE)

def is_zone_tiff(path: Path) -> bool:
    return path.suffix.lower() in {".tif", ".tiff"} and bool(ZONE_TIFF_STEM_RE.fullmatch(path.stem))


def iter_zone_tiff_batches(root: Path):
    """Yield one folder at a time to keep processing sequential and predictable."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        batch = []
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if is_zone_tiff(path):
                batch.append(path)
        if batch:
            yield Path(dirpath), batch


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
    parser = argparse.ArgumentParser(description="Precompute _web.jpg files from zone TIFFs.")
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
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="Optional pause between conversions to reduce load on the server. Default: 0",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        print(f"Root path does not exist: {root}", file=sys.stderr)
        return 1
    if not root.is_dir():
        print(f"Root path is not a directory: {root}", file=sys.stderr)
        return 1

    batches = iter_zone_tiff_batches(root)
    first_batch = next(batches, None)
    if not first_batch:
        print(f"No zone TIFF files matching G<number>_zone.tif/.tiff found under {root}")
        return 0

    print(f"Scanning {root}")
    print("Processing zone TIFFs one folder at a time")

    created = 0
    skipped = 0
    failed = 0
    total_seconds = 0.0
    folder_count = 0

    for folder_path, tif_paths in chain([first_batch], batches):
        folder_count += 1
        print(f"\nFolder {folder_count}: {folder_path} ({len(tif_paths)} file(s))")
        for tif_path in tif_paths:
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
                if args.sleep_seconds > 0:
                    time.sleep(args.sleep_seconds)
            except Exception as exc:
                failed += 1
                print(f"ERROR  {tif_path}: {exc}", file=sys.stderr)

    print(
        f"Done. folders={folder_count} created={created} skipped={skipped} failed={failed} "
        f"elapsed={total_seconds:.2f}s"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
