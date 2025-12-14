"""Utility to test the card processing logic locally.

This script reads an image file from disk, runs the card detection and OCR
pipeline, and writes the resulting card crops to an output directory. To
invoke it, run::

    python test.py --input /path/to/image.jpg --output out_dir

The script does not interact with Azure storage and is intended solely
for local experimentation.
"""
import argparse
import os
from pathlib import Path

import cv2

from CardProcessor import process_utils


def main() -> None:
    parser = argparse.ArgumentParser(description="Test card cropping locally")
    parser.add_argument("--input", required=True, help="Path to input image")
    parser.add_argument("--output", required=True, help="Directory to save outputs")
    args = parser.parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    with input_path.open("rb") as f:
        data = f.read()
    cards = process_utils.process_image(data)
    if not cards:
        print("No cards detected.")
        return
    for idx, (name, img_bytes) in enumerate(cards, 1):
        filename = f"card_{idx}_{name.replace(' ', '_')}.jpg"
        out_path = output_dir / filename
        with out_path.open("wb") as out_file:
            out_file.write(img_bytes)
        print(f"Saved {out_path}")


if __name__ == "__main__":
    main()