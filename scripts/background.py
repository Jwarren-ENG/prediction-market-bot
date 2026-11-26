"""Install/remove the current user's worker supervisor. No cloud account or administrator is needed."""

import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from xml.sax.saxutils import escape
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "remove", "status"))
    options = parser.parse_args()
    path = Path(__file__).resolve()
    base = path.parent if (path.parent / "pmb.py").exists() else path.parents[1]
    entry = base / "pmb.py"
    label = "com.pmb.paper-worker"
    if sys.platform == "darwin":
        script = Path.home() / "Library/LaunchAgents" / (label + ".plist")
        target = f"gui/{os.getuid()}"
        handle = target + "/" + label
        if options.action == "status":
            return subprocess.run(["launchctl", "print", handle]).returncode
        if options.action == "remove":
            if script.exists():
                if (
                    subprocess.run(
                        ["launchctl", "print", handle], capture_output=True
                    ).returncode
                    == 0
                ):
                    subprocess.run(["launchctl", "bootout", handle], check=True)
                script.unlink(missing_ok=True)
                subprocess.run(
                    [sys.executable, "-I", "-B", str(entry), "stop", "--matching-root"],
                    check=True,
                )
            return 0
        logs = Path.home() / "Library/Logs/Prediction Market Bot"
        logs.mkdir(parents=True, exist_ok=True)
        script.parent.mkdir(parents=True, exist_ok=True)
        settings = {
            "Label": label,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 15,
            "ProgramArguments": [sys.executable, "-I", "-B", str(entry), "serve"],
            "StandardOutPath": str(logs / "paper-worker.log"),
            "StandardErrorPath": str(logs / "paper-worker.log"),
        }
        dump = plistlib.dumps(settings)
        if script.exists() and script.read_bytes() == dump:
            if (
                subprocess.run(
                    ["launchctl", "print", handle], capture_output=True
                ).returncode
                == 0
            ):
                return 0
        if (
            script.exists()
            and subprocess.run(
                ["launchctl", "print", handle], capture_output=True
            ).returncode
            == 0
        ):
            subprocess.run(["launchctl", "bootout", handle], check=True)
        subprocess.run([sys.executable, "-I", "-B", str(entry), "stop"], check=True)
        script.write_bytes(dump)
        return subprocess.run(
            ["launchctl", "bootstrap", target, str(script)]
        ).returncode
    if sys.platform == "win32":
        name = "Prediction Market Bot personal paper worker"
        if options.action == "status":
            return subprocess.run(["schtasks", "/Query", "/TN", name]).returncode
        if options.action == "remove":
            existing = subprocess.run(
                ["schtasks", "/Query", "/TN", name], capture_output=True
            )
            if existing.returncode == 0:
                subprocess.run(["schtasks", "/End", "/TN", name], capture_output=True)
                subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], check=True)
            subprocess.run(
                [sys.executable, "-I", "-B", str(entry), "stop", "--matching-root"],
                check=True,
            )
            return 0
        python = str(Path(sys.executable).with_name("pythonw.exe"))
        user = subprocess.check_output(["whoami"], text=True).strip()
        body = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
<Principals><Principal id="Owner"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure></Settings>
<Actions Context="Owner"><Exec><Command>{escape(python)}</Command><Arguments>-I -B &quot;{escape(str(entry))}&quot; serve</Arguments></Exec></Actions></Task>"""
        with tempfile.TemporaryDirectory() as temp:
            xml = Path(temp) / "worker.xml"
            xml.write_text(body, encoding="utf-16")
            subprocess.run(
                ["schtasks", "/Create", "/F", "/TN", name, "/XML", str(xml)], check=True
            )
        subprocess.run([sys.executable, "-I", "-B", str(entry), "stop"], check=True)
        return subprocess.run(["schtasks", "/Run", "/TN", name]).returncode
    print(
        "Use a user systemd service on Linux. See the project README.", file=sys.stderr
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
