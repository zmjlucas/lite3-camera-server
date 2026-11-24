"""Portable offline checks. Run with Python 3.8+; no Make or Bash is required."""
import argparse
import ast
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--syntax-only", action="store_true", help="skip the unit tests")
    parser.add_argument("--shell", action="store_true", help="also check remote Bash syntax (requires Bash)")
    parser.add_argument("--legacy", action="store_true", help="only check remote Python 3.6 syntax (use Python 3.8-3.12)")
    args = parser.parse_args()
    if args.legacy:
        if sys.version_info >= (3, 13):
            parser.error("Use Python 3.8-3.12 to check Python 3.6 syntax")
        sources = [ROOT / "src/server.py", *sorted((ROOT / "src/remote").glob("*.py"))]
        grammar = (3, 6)
    else:
        sources = [path for folder in ("src", "scripts", "tests") for path in sorted((ROOT / folder).rglob("*.py"))]
        grammar = (3, 8)
    for path in sources:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=grammar)
    print("PASS: Python {}.{} syntax ({} files)".format(*grammar, len(sources)), flush=True)
    if args.shell:
        bash = shutil.which("bash")
        if not bash:
            parser.error("--shell requires Bash; omit it for native Windows checks")
        for folder in ("src/remote", "tests/fixtures"):
            for path in sorted((ROOT / folder).glob("*.sh")):
                subprocess.run([bash, "-n", str(path)], check=True)
        print("PASS: remote Bash syntax", flush=True)
    if args.legacy:
        print("Python 3.6 runtime operation remains unverified.")
    elif not args.syntax_only:
        return subprocess.call([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=str(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
