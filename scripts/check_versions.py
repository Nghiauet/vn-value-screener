"""Exit 1 unless pyproject.toml, the package, server.json (and its PyPI package), and the tag, if given, agree.

    python scripts/check_versions.py [vX.Y.Z]
"""
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def versions() -> dict[str, str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    init = (ROOT / "src" / "vn_value_screener" / "__init__.py").read_text()
    server = json.loads((ROOT / "server.json").read_text())
    found = {
        "pyproject.toml": pyproject["project"]["version"],
        "__init__.py": re.search(r'__version__ = "([^"]+)"', init).group(1),
        "server.json": server["version"],
    }
    for i, package in enumerate(server["packages"]):
        found[f"server.json packages[{i}]"] = package["version"]
        if package["identifier"] != pyproject["project"]["name"]:
            sys.exit(f"server.json packages[{i}] is {package['identifier']!r}, not {pyproject['project']['name']!r}")
    readme = (ROOT / "README.md").read_text()
    if f"mcp-name: {server['name']} " not in readme and f"mcp-name: {server['name']}\n" not in readme:
        sys.exit(f"README.md lacks 'mcp-name: {server['name']}' (the registry's PyPI ownership check)")
    return found


def main() -> int:
    found = versions()
    if len(sys.argv) > 1:
        found["tag"] = sys.argv[1].removeprefix("refs/tags/").removeprefix("v")
    if len(set(found.values())) != 1:
        print("versions disagree: " + ", ".join(f"{k} {v}" for k, v in found.items()), file=sys.stderr)
        return 1
    print(f"version {next(iter(found.values()))} everywhere ({', '.join(found)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
