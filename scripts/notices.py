"""Collect upstream license texts, or verify and install the checked-in release notices.

Package builds use the checked-in document offline. To refresh it, run
`python3.14 scripts/notices.py --generate --cache DIRECTORY` with Cargo available.
The cache contains public upstream archives only.
"""

import argparse
from collections import defaultdict
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.parse import quote
from urllib.request import Request, urlopen
from xml.etree import ElementTree
import zipfile

if __package__:
    from .fetch_runtime import ARCHIVES, PYTHON, RELEASE
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fetch_runtime import ARCHIVES, PYTHON, RELEASE

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "docs/THIRD_PARTY.md"
TARGETS = (
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "x86_64-pc-windows-msvc",
    "x86_64-pc-windows-gnullvm",
)
PYTHON_FULL = {
    "aarch64-apple-darwin-pgo+lto": "cbb424442f2f04454984a85c260ec56dcb99b1af3fdafa16c03f492d92212f35",
    "x86_64-apple-darwin-pgo+lto": "b56d8dcca317f8a307d51d0d81f784b9ac6e469abefcff48ed1085221fb9922d",
    "x86_64-pc-windows-msvc-pgo": "789e5b1b30aaacbf7a7116c95f4dbff11f08e916ba136cb3d34e4288e1cd46a6",
}
ASTRAL = "https://github.com/astral-sh/python-build-standalone"
SDK_VERSION = "1.0.3800.47"
MINGW_TAG = "20261117"
LEGAL_FILE = re.compile(
    r"^(?:licen[cs]e|copying|copyright|notice|unlicense)(?:$|[-_.])", re.I
)
TERMS_HEADING = "## Windows component terms\n\n```text\n"
WEBVIEW_TERMS = (
    "https://developer.microsoft.com/microsoft-edge/api/eula/webview2?locale=en-us"
)
VC_TERMS = "https://visualstudio.microsoft.com/wp-content/uploads/2021/09/Visual-C-Runtime-2015-2022-License-1.docx"


def locked_crates():
    lock = tomllib.loads((ROOT / "desktop/Cargo.lock").read_text(encoding="utf-8"))
    return {
        (p["name"], p["version"]): p["checksum"]
        for p in lock["package"]
        if p.get("source", "").startswith("registry+")
    }


def inputs_stamp():
    data = [
        sorted((n, v, h) for (n, v), h in locked_crates().items()),
        PYTHON,
        RELEASE,
        ARCHIVES,
    ]
    digest = sha256(
        json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return f"<!-- Cargo.lock and Python runtime inputs: {digest} -->"


def install(destination, stdlib_notice=None, windows=False):
    """Use the compiling Rust toolchain's own standard-library copyright document."""
    document = DOCUMENT.read_text(encoding="utf-8")
    if inputs_stamp() not in document.splitlines()[:6]:
        raise ValueError(
            "Dependency inputs changed. Regenerate docs/THIRD_PARTY.md before packaging."
        )
    if not stdlib_notice:
        rustc = os.environ.get("RUSTC", "rustc")
        sysroot = subprocess.check_output(
            [rustc, "--print", "sysroot"], text=True
        ).strip()
        stdlib_notice = Path(sysroot) / "share/doc/rust/COPYRIGHT-library.html"
    if not stdlib_notice.is_file():
        raise ValueError(
            "Install rust-docs for the build toolchain or pass its COPYRIGHT-library.html with --rust-stdlib-notice."
        )
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DOCUMENT, destination / "THIRD_PARTY.md")
    shutil.copyfile(stdlib_notice, destination / "RUST-STANDARD-LIBRARY.html")
    if windows:
        _, start, after_heading = document.partition(TERMS_HEADING)
        terms, end, _ = after_heading.partition("\n```\n")
        if not start or not end or not terms:
            raise ValueError(
                "Windows component terms are missing from the third-party notice document."
            )
        (destination / "WINDOWS-COMPONENT-TERMS.txt").write_text(
            terms + "\n", encoding="utf-16", newline="\r\n"
        )


def remote(url, cache, digest=None):
    """Cache content-addressed inputs and fetch missing files from public primary sources."""
    key = digest or sha256(url.encode()).hexdigest()
    path = cache / key
    if not path.is_file():
        context = ssl.create_default_context()
        if Path("/etc/ssl/cert.pem").is_file():
            context.load_verify_locations("/etc/ssl/cert.pem")
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as output:
            pending = Path(output.name)
        try:
            request = Request(url, headers={"User-Agent": "PMB-release-notices"})
            with urlopen(
                request, context=context, timeout=90
            ) as response, pending.open("wb") as output:
                shutil.copyfileobj(response, output)
            if digest and file_sha(pending) != digest:
                raise ValueError(f"Upstream checksum failed for {url}")
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    if digest and file_sha(path) != digest:
        raise ValueError(f"Cached checksum failed for {url}")
    return path


def file_sha(path):
    with path.open("rb") as file:
        digest = sha256()
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Document:
    def __init__(self):
        self.lines = [
            "# Third-party notices",
            "",
            inputs_stamp(),
            "",
            "The desktop packages include the notices below. The Apple and Windows builds share this index. "
            "Each source link identifies the upstream component. The Rust standard library notices for the "
            "compiler used to build the executable accompany this document in RUST-STANDARD-LIBRARY.html.",
            "",
        ]
        self.texts = {}

    def notice(self, data):
        text = (
            data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
        )
        if not text:
            raise ValueError("Empty upstream license text")
        number = sha256(text.encode()).hexdigest()[:12]
        self.texts[number] = text
        return f"[{number}](#notice-{number})"

    def finish(self):
        self.lines.extend(["", "## License and attribution texts", ""])
        for number, text in sorted(self.texts.items()):
            fence = "`" * (max([3, *(len(m) for m in re.findall(r"`+", text))]) + 1)
            self.lines.extend(
                [f"### Notice {number}", "", fence + "text", text, fence, ""]
            )
        return "\n".join(self.lines)


def crate_records(cargo):
    packages = defaultdict(list)
    for target in TARGETS:
        result = subprocess.check_output(
            [
                cargo,
                "tree",
                "--locked",
                "--manifest-path",
                str(ROOT / "desktop/Cargo.toml"),
                "--target",
                target,
                "--edges",
                "normal,build",
                "--prefix",
                "none",
                "--format",
                "{p}",
            ],
            text=True,
        )
        for name, version in sorted(
            set(re.findall(r"(?m)^([\w-]+) v([^\s]+)", result))
        ):
            if name != "prediction-market-bot":
                packages[(name, version)].append(target)
    return packages


def rust_notices(doc, cache, cargo):
    doc.lines.extend(
        [
            "## Rust desktop dependencies",
            "",
            "These versions are resolved for the supported Apple and Windows targets, including "
            "crates used while building the executable. Source links provide the exact, unmodified "
            "crate source archives, including the Mozilla Public License components.",
            "",
            "| Crate and source code | Upstream license expression | Notices |",
            "| --- | --- | --- |",
        ]
    )
    registry = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")) / "registry"
    locked = locked_crates()
    for name, version in sorted(crate_records(cargo)):
        filename = f"{name}-{version}"
        source = f"https://static.crates.io/crates/{name}/{filename}.crate"
        cached = list((registry / "cache").glob(f"*/{filename}.crate"))
        archive = next(
            (p for p in cached if file_sha(p) == locked[(name, version)]), None
        )
        if not archive:
            archive = remote(source, cache, locked[(name, version)])
        with tarfile.open(archive) as package:

            def read(path):
                return package.extractfile(f"{filename}/{path}").read()

            manifest = tomllib.loads(read("Cargo.toml").decode())["package"]
            paths = [
                m.name[len(filename) + 1 :] for m in package.getmembers() if m.isfile()
            ]
            legal = [
                p
                for p in paths
                if LEGAL_FILE.match(Path(p).name) and not p.endswith((".rs", ".spdx"))
            ]
            if manifest.get("license-file"):
                legal.append(manifest["license-file"].removeprefix("./"))
            references = {doc.notice(read(p)) for p in set(legal)}
            if not references:
                references.update(
                    missing_crate_notices(
                        doc, manifest, json.loads(read(".cargo_vcs_info.json")), cache
                    )
                )
        expression = manifest.get("license") or "See included license"
        doc.lines.append(
            f"| [{filename}]({source}) | {expression} | {', '.join(sorted(references))} |"
        )
    doc.lines.append("")


def missing_crate_notices(doc, manifest, vcs, cache):
    """Some workspace packages omit the root legal file from their published crate."""
    repo = (
        manifest["repository"].removesuffix(".git").removeprefix("https://github.com/")
    )
    revision = vcs["git"]["sha1"]
    paths = {
        "dropbox/rust-alloc-no-stdlib": "LICENSE",
        "wravery/webview2-rs": "LICENSE",
        "madsmtm/objc2": "LICENSE.md",
    }
    refs = []
    if repo in paths:
        url = f"https://raw.githubusercontent.com/{repo}/{revision}/{paths[repo]}"
        refs.append(doc.notice(remote(url, cache).read_bytes()))
    elif repo != "servo/stylo" or manifest["license"] != "MPL-2.0":
        raise ValueError(
            f"Review license text missing from {manifest['name']} at {repo}/{revision}"
        )
    standard = {
        "madsmtm/objc2": (
            "MIT",
            "b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5",
        ),
        "servo/stylo": (
            "MPL-2.0",
            "66a3107d5ad6a058aab753eaac2047ccb2ed0e39465dd0fe5844da3e300d5172",
        ),
    }
    if repo in standard:
        name, digest = standard[repo]
        url = f"https://raw.githubusercontent.com/spdx/license-list-data/v3.27.0/text/{name}.txt"
        refs.append(doc.notice(remote(url, cache, digest).read_bytes()))
    return refs


def python_notices(doc, cache):
    doc.lines.extend(
        [
            "## Python interpreter and native extensions",
            "",
            f"The application includes Astral’s standalone CPython {PYTHON} distribution, release {RELEASE}. "
            "The installed interpreter and its bundled Python packages retain their original notices. "
            "The full build manifests identify additional licenses for native extensions in the stripped runtimes. "
            f"[Distribution and source pins]({ASTRAL}/tree/{RELEASE})",
            "",
        ]
    )
    for target, digest in PYTHON_FULL.items():
        filename = f"cpython-{PYTHON}+{RELEASE}-{target}-full.tar.zst"
        url = f"{ASTRAL}/releases/download/{RELEASE}/{quote(filename, safe='')}"
        doc.lines.extend(
            [
                f"### {target}",
                "",
                f"[Upstream full archive and build manifest]({url})",
                "",
            ]
        )
        with tarfile.open(remote(url, cache, digest), "r|*") as archive:
            texts = {}
            metadata = None
            for item in archive:
                if item.name == "python/PYTHON.json":
                    metadata = json.load(archive.extractfile(item))
                elif item.isfile() and item.name.startswith("python/licenses/"):
                    texts[item.name.removeprefix("python/")] = archive.extractfile(
                        item
                    ).read()
        if (
            not metadata
            or metadata["python_version"] != PYTHON
            or metadata["target_triple"] not in target
        ):
            raise ValueError(f"Runtime manifest did not match {filename}")
        modules = defaultdict(set)
        modules[metadata["license_path"]].add("CPython")
        for module, variants in metadata["build_info"]["extensions"].items():
            for variant in variants:
                for path in variant.get("license_paths", []):
                    modules[path].add(module)
        for path, names in sorted(modules.items()):
            doc.lines.append(
                f"- {Path(path).name} ({', '.join(sorted(names))}) {doc.notice(texts[path])}"
            )
        doc.lines.append("")


def platform_notices(doc, cache):
    doc.lines.extend(
        [
            "## Fonts",
            "",
            "IBM Plex Sans and IBM Plex Mono use the SIL Open Font License 1.1. "
            "The font files and their original notices can be found under ui/fonts in the application payload. "
            "[IBM Plex source](https://github.com/IBM/plex)",
            "",
        ]
    )
    for name in ["PlexSans", "PlexMono"]:
        doc.lines.append(
            f"- IBM {name} {doc.notice((ROOT / f'ui/fonts/LICENSE-{name}.txt').read_bytes())}"
        )
    doc.lines.extend(
        [
            "",
            "## Windows distribution components",
            "",
            "Microsoft Edge WebView2 provides the Windows web view. The installer includes Microsoft's Evergreen "
            "bootstrapper, which downloads the device's WebView2 Runtime when required. Microsoft maintains that "
            "runtime and its updates. [Microsoft terms and download](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) "
            "· [Distribution guide](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution) "
            "· [Microsoft Privacy Statement](https://privacy.microsoft.com/privacystatement)",
            "",
            "The Windows Python distribution also includes the Microsoft Visual C++ Runtime. Its Microsoft "
            "Distributable Code terms are included in payload/runtime/LICENSE.txt. "
            f"[CPython distribution terms](https://github.com/python/cpython/blob/v{PYTHON}/PC/crtlicense.txt)",
            "",
        ]
    )
    sdk = f"https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{SDK_VERSION}/microsoft.web.webview2.{SDK_VERSION}.nupkg"
    with zipfile.ZipFile(
        remote(
            sdk,
            cache,
            "56c9f26bdd07916a2d1949fb58a5c7e434dfa1173577dca879206050c4e718db",
        )
    ) as sdk_archive:
        refs = [doc.notice(sdk_archive.read(n)) for n in ["LICENSE.txt", "NOTICE.txt"]]
    doc.lines.append(
        f"- [Microsoft WebView2 SDK {SDK_VERSION}]({sdk}) loader and notices. {' '.join(refs)}"
    )
    nsis = "https://raw.githubusercontent.com/kichik/nsis/v313/COPYING"
    doc.lines.append(
        f"- [NSIS installer engine and bundled plug-ins]({nsis}) {doc.notice(remote(nsis, cache, '6cc665347be5c84b76bf0fd6f85fbb0c33261e89b3408aa3e3e55dbe95e12d89').read_bytes())}"
    )
    filename = f"llvm-mingw-{MINGW_TAG}-ucrt-macos-universal"
    url = f"https://github.com/mstorsjo/llvm-mingw/releases/download/{MINGW_TAG}/{filename}.tar.xz"
    with tarfile.open(
        remote(
            url,
            cache,
            "98d340bbb8d46b09ad6886ffba88bd9d5415e131c316de48e6bd7647377446aa",
        ),
        "r|*",
    ) as archive:
        refs = []
        for member in archive:
            if member.isfile() and (
                member.name == filename + "/LICENSE.TXT"
                or member.name.startswith(
                    filename + "/x86_64-w64-mingw32/share/mingw32/COPYING"
                )
            ):
                refs.append(
                    (
                        Path(member.name).name,
                        doc.notice(archive.extractfile(member).read()),
                    )
                )
    if len(refs) < 2:
        raise ValueError("Missing cross-toolchain runtime notices")
    doc.lines.extend(
        [
            f"- [LLVM-MinGW {MINGW_TAG} cross-build runtime materials]({url}) "
            + ", ".join(f"{n} {r}" for n, r in sorted(refs)),
            "",
        ]
    )


class TermsText(HTMLParser):
    """Keep Microsoft's visible license text and paragraph boundaries in the native setup page."""

    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(" ".join(data.split()))

    def handle_endtag(self, tag):
        if tag in {"p", "li", "h3", "div", "br"}:
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def paragraphs(self):
        return "\n\n".join(
            " ".join(line.split())
            for line in " ".join(self.parts).splitlines()
            if line.strip()
        )


def windows_terms(doc, cache):
    doc.lines.extend(
        [
            "## Windows component terms",
            "",
            "```text",
            "Microsoft components in Prediction Market Bot for Windows",
            "",
            "The following terms apply to the indicated Microsoft components. The setup checkbox or the "
            "/ACCEPTCOMPONENTTERMS option records your agreement to these component terms.",
            "",
            "The desktop web view includes Microsoft Defender SmartScreen. SmartScreen collects and sends "
            "information to Microsoft as described in the Microsoft Privacy Statement (https://aka.ms/privacy) "
            "and the Microsoft Edge Privacy Whitepaper (https://learn.microsoft.com/en-us/microsoft-edge/privacy-whitepaper#smartscreen). "
            "Microsoft's Evergreen Runtime may check for updates and download and install them automatically.",
            "",
        ]
    )
    with zipfile.ZipFile(
        remote(
            VC_TERMS,
            cache,
            "f1e3d56ceb2ad68aae0711b910375009e651ac5530fa0760f0dea6e81e54fae1",
        )
    ) as vc:
        body = ElementTree.fromstring(vc.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = [
            "".join(t.text or "" for t in p.findall(".//w:t", ns)).strip()
            for p in body.findall(".//w:body/w:p", ns)
        ]
    doc.lines.extend(
        [
            "Microsoft Visual C++ 2015–2022 Runtime, used by the bundled Windows Python interpreter",
            "Source: https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/",
            "",
            "\n\n".join(p for p in paragraphs if p),
            "",
        ]
    )
    crt = (
        f"https://raw.githubusercontent.com/python/cpython/v{PYTHON}/PC/crtlicense.txt"
    )
    doc.lines.extend(
        [
            "CPython conditions for distribution of Microsoft runtime code",
            "Source: " + crt,
            "",
            remote(
                crt,
                cache,
                "3c71b3ae243121f6f227374cd1ef7c59ed07b1969b330d0e947f91a3a1b3fd73",
            )
            .read_text()
            .strip(),
            "",
        ]
    )
    versions = [
        (
            "Evergreen Runtime end-user terms",
            "&consumer=true",
            "consumerHtml",
            "223fb6b4f683cf66291c4958b881f97dc6f14f68d608b6298ed793521f609ca9",
        ),
        (
            "Additional terms for redistribution of the Microsoft Evergreen Runtime",
            "",
            "evergreenHtml",
            "821657eb0994850b0ea86b53242c52a22016e45859fee74bcd456dd77c0ba18e",
        ),
    ]
    for label, query, field, digest in versions:
        url = WEBVIEW_TERMS + query
        parser = TermsText()
        parser.feed(json.loads(remote(url, cache, digest).read_bytes())[field])
        parser.close()
        doc.lines.extend([label, "Source: " + url, "", parser.paragraphs(), ""])
    doc.lines.extend(["```", ""])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    if not args.generate:
        if inputs_stamp() not in DOCUMENT.read_text(encoding="utf-8").splitlines()[:6]:
            raise SystemExit("Third-party notices need regeneration.")
        print("Third-party notices match the Cargo and Python inputs.")
        return
    if not args.cache:
        parser.error("--generate requires --cache DIRECTORY")
    args.cache.mkdir(parents=True, exist_ok=True)
    doc = Document()
    rust_notices(doc, args.cache, os.environ.get("CARGO", "cargo"))
    python_notices(doc, args.cache)
    platform_notices(doc, args.cache)
    windows_terms(doc, args.cache)
    DOCUMENT.write_text(doc.finish(), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
