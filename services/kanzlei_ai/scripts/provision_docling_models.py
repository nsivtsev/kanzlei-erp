"""Stage the public native PDF models using the same pins as the Paddle worker."""

import argparse
from pathlib import Path

from huggingface_hub import hf_hub_download

from kanzlei_ai.providers.paddle_ocr import _DOCLING_ASSETS, _verify_asset

_REVISIONS = {
    "docling-project--docling-layout-heron": "8f39ad3c0b4c58e9c2d2c84a38465abf757272d8",
    "docling-project--docling-models": "fc0f2d45e2218ea24bce5045f58a389aed16dc23",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    for relative_path, checksum in _DOCLING_ASSETS.items():
        directory, filename = relative_path.split("/", 1)
        model = hf_hub_download(
            repo_id=directory.replace("--", "/", 1),
            revision=_REVISIONS[directory],
            filename=filename,
            local_dir=args.destination / directory,
        )
        _verify_asset(Path(model), checksum)


if __name__ == "__main__":
    main()
