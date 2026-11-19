"""Build an AstrBot WebUI upload archive with a stable plugin directory name."""

import hashlib
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
FILES = (
    "__init__.py",
    "main.py",
    "mail.py",
    "probe.py",
    "settings.py",
    "watchdog.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "LICENSE",
)


def build(output_root: Path) -> Path:
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    plugin_name = re.search(r"^name: (astrbot_plugin_[a-z0-9_]+)$", metadata, re.MULTILINE)
    version = re.search(r"^version: (v\d+\.\d+\.\d+)$", metadata, re.MULTILINE)
    if plugin_name is None or version is None:
        raise ValueError("Invalid name or version in metadata.yaml")
    name = plugin_name[1]
    output = output_root / version[1] / f"{name}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        # AstrBot 4.10.4 unpacks the first archive entry as the top-level directory.
        directory = ZipInfo(f"{name}/")
        directory.external_attr = (0o40755 << 16) | 0x10
        archive.writestr(directory, b"")
        for relative in FILES:
            entry = ZipInfo(f"{name}/{relative}")
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, (ROOT / relative).read_bytes())
    if output.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Plugin archive exceeds 16 MiB")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return output


if __name__ == "__main__":
    print(build(ROOT / "dist"))
