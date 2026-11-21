"""Command-line entry point for the shared LE59 decoder."""
import importlib.util
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    source = root / "tools/olserver/roster_decode.py"
    if not source.is_file():
        source = root / "server/roster_decode.py"
    spec = importlib.util.spec_from_file_location("roster_decode", source)
    decoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(decoder)
    decoder.main(root)


if __name__ == "__main__":
    main()
