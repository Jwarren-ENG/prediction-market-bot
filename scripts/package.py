"""Package a native Tauri executable with an isolated Python worker and the local desktop UI."""

import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

if __package__:
    from . import notices
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import notices

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "Prediction Market Bot"
VERSION = "0.1.3"


def payload(target, runtime, stdlib_notice, windows=False):
    target.mkdir(parents=True)
    omit = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    for name in ("worker", "ui"):
        shutil.copytree(ROOT / name, target / name, ignore=omit)
    shutil.copy2(ROOT / "pmb.py", target / "pmb.py")
    shutil.copytree(runtime, target / "runtime", symlinks=True, ignore=omit)
    shutil.copy2(ROOT / "scripts" / "background.py", target / "background.py")
    notices.install(target / "notices", stdlib_notice, windows)


def mac(args, temp, output):
    app = temp / (PRODUCT + ".app")
    contents = app / "Contents"
    binary = contents / "MacOS" / "PMB"
    binary.parent.mkdir(parents=True)
    shutil.copy2(args.binary, binary)
    if args.intel_binary:
        subprocess.run(
            [
                "lipo",
                "-create",
                str(args.binary),
                str(args.intel_binary),
                "-output",
                str(binary),
            ],
            check=True,
        )
    helper = contents / "MacOS" / "pmb-worker"
    shutil.copy2(ROOT / "scripts/mac-worker", helper)
    helper.chmod(0o755)
    resources = contents / "Resources"
    payload(resources / "payload", args.runtime, args.rust_stdlib_notice)
    if args.intel_runtime:
        shutil.copytree(
            args.intel_runtime,
            resources / "payload/runtime-intel",
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    shutil.copy2(ROOT / "desktop/icons/icon.icns", resources / "AppIcon.icns")
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump(
            dict(
                CFBundleName=PRODUCT,
                CFBundleDisplayName=PRODUCT,
                CFBundleIdentifier="com.pmb.researchdesk",
                CFBundleExecutable="PMB",
                CFBundlePackageType="APPL",
                CFBundleIconFile="AppIcon",
                CFBundleShortVersionString=VERSION,
                CFBundleVersion="4",
                LSMinimumSystemVersion="12.0",
                NSHighResolutionCapable=True,
                NSPrincipalClass="NSApplication",
                NSAppTransportSecurity={"NSAllowsLocalNetworking": True},
            ),
            f,
        )
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", args.identity, str(app)], check=True
    )
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    dest = output / app.name
    if dest.exists():
        raise ValueError(f"Output already exists: {dest}")
    shutil.move(str(app), str(dest))
    return dest


def windows(args, temp, output):
    stage = temp / "windows"
    stage.mkdir()
    shutil.copy2(args.binary, stage / "PMB.exe")
    loader = args.binary.parent / "WebView2Loader.dll"
    if loader.is_file():
        shutil.copy2(loader, stage / loader.name)
    payload(stage / "payload", args.runtime, args.rust_stdlib_notice, windows=True)
    if not (stage / "payload/runtime/python.exe").is_file():
        raise ValueError("Expected the Windows x64 Python runtime.")
    dest = output / f"PMB-{VERSION}-Windows-x64-Setup.exe"
    if dest.exists():
        raise ValueError(f"Output already exists: {dest}")

    # Avoid command-line definition syntax differences between native Windows and macOS makensis.
    def literal(path):
        return str(path).replace("$", "$$").replace('"', '$\\"')

    defs = "\n".join(
        f'!define {k} "{literal(v)}"'
        for k, v in {
            "APPVERSION": VERSION,
            "STAGE": stage,
            "WEBVIEW": args.bootstrap,
            "OUTPUT": dest,
            "APPICON": ROOT / "desktop/icons/icon.ico",
            "COMPONENT_TERMS": stage / "payload/notices/WINDOWS-COMPONENT-TERMS.txt",
        }.items()
    )
    remove = temp / "remove-owned.nsh"
    files = [p for p in stage.rglob("*") if p.is_file()]
    dirs = [p for p in stage.rglob("*") if p.is_dir()]
    from pathlib import PureWindowsPath

    def installed(path):
        return (
            "$INSTDIR"
            + chr(92)
            + str(PureWindowsPath(path.relative_to(stage))).replace("$", "$$")
        )

    remove.write_text(
        "\n".join(
            ['Delete /REBOOTOK "' + installed(p) + '"' for p in files]
            + [
                'RMDir "' + installed(p) + '"'
                for p in sorted(dirs, key=lambda p: -len(p.parts))
            ]
        ),
        encoding="utf-8",
    )
    defs += '\n!define OWNED "' + literal(remove) + '"'
    script = temp / "package.nsi"
    script.write_text(
        defs + "\n" + (ROOT / "scripts/windows.nsi").read_text(), encoding="utf-8"
    )
    subprocess.run([str(args.makensis), str(script)], check=True)
    return dest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=["macos", "windows"], required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--rust-stdlib-notice",
        type=Path,
        help="COPYRIGHT-library.html from the Rust toolchain that built the native binary; defaults to rustc's sysroot",
    )
    parser.add_argument(
        "--intel-binary",
        type=Path,
        help="Additional Intel executable for a universal Mac build",
    )
    parser.add_argument(
        "--intel-runtime",
        type=Path,
        help="Additional Intel Python for a universal Mac build",
    )
    parser.add_argument(
        "--identity",
        default="-",
        help="Mac signing identity; default is a local ad hoc signature",
    )
    parser.add_argument("--makensis", default="makensis")
    parser.add_argument(
        "--bootstrap", type=Path, help="Microsoft Evergreen WebView2 bootstrapper"
    )
    args = parser.parse_args()
    if args.platform == "windows" and not (args.bootstrap and args.bootstrap.is_file()):
        parser.error("Windows packaging requires the Microsoft WebView2 bootstrapper.")
    if bool(args.intel_binary) != bool(args.intel_runtime):
        parser.error("Specify the Intel binary and runtime together.")
    for name in (
        "runtime",
        "binary",
        "out",
        "bootstrap",
        "intel_binary",
        "intel_runtime",
    ):
        if getattr(args, name):
            setattr(args, name, getattr(args, name).resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pmb-package-", dir=args.out) as work:
        result = (mac if args.platform == "macos" else windows)(
            args, Path(work), args.out
        )
        print(result)


if __name__ == "__main__":
    main()
