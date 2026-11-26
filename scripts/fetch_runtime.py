"""Fetch an audited standalone CPython for native packaging (Python 3.12+)."""

import argparse
import hashlib
from pathlib import Path
import shutil
import ssl
import tarfile
import tempfile
from urllib.request import Request, urlopen

RELEASE = "20261124"
PYTHON = "3.13.18"
ARCHIVES = {
    "mac-arm64": (
        "aarch64-apple-darwin",
        "296f0903cccda15a5c9d3d8393cd2204718aeffd9b095a00f812d75c7b1c0967",
    ),
    "mac-x64": (
        "x86_64-apple-darwin",
        "bb16e167ec9e38368118ce3e43ba2c7852fea2cdba16e1852555a8997747f78a",
    ),
    "windows-x64": (
        "x86_64-pc-windows-msvc",
        "a455077dd29dd8637b3ab6d745fc0a210d3504a0dd6bd31195ce1e11a81ec9ae",
    ),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("platform", choices=ARCHIVES)
    ap.add_argument("destination", type=Path)
    args = ap.parse_args()
    dest = args.destination.resolve()
    if dest.exists():
        ap.error("Choose a new destination directory.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    target, digest = ARCHIVES[args.platform]
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/cpython-{PYTHON}%2B{RELEASE}-{target}-install_only_stripped.tar.gz"
    ssl_ctx = ssl.create_default_context()
    if Path("/etc/ssl/cert.pem").is_file():
        ssl_ctx.load_verify_locations("/etc/ssl/cert.pem")
    with tempfile.TemporaryDirectory(dir=dest.parent) as temp:
        archive = Path(temp) / "python.tar.gz"
        with (
            urlopen(
                Request(url, headers={"User-Agent": "PMB-runtime-build"}),
                context=ssl_ctx,
                timeout=120,
            ) as res,
            archive.open("wb") as file,
        ):
            shutil.copyfileobj(res, file)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise ValueError("Runtime checksum mismatch.")
        with tarfile.open(archive) as bundle:
            bundle.extractall(temp, filter="data")
        shutil.move(str(Path(temp) / "python"), str(dest))
    print(dest)


if __name__ == "__main__":
    main()
