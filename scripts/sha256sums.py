"""Print SHA256SUMS.txt records for the supplied release archives."""

import argparse
import hashlib
from pathlib import Path


def main(argv=None) -> int:
    """Hash each file without loading the archive into memory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    for path in args.files:
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        print(f"{digest}  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
