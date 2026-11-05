"""Build unsupported export copies without changing the Qtegra fixtures."""


def write_y_only_csv(rich, target):
    """Keep both headers and MainRuns Y rows, including their BOM and CRLF."""
    lines = rich.read_bytes().splitlines(keepends=True)
    kept = lines[:2] + [
        line for line in lines[2:]
        if line.startswith(b"MainRuns,") and line.split(b",", 4)[3] == b"Y"
    ]
    target.write_bytes(b"".join(kept))
    return target
