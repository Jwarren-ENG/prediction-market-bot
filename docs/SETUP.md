# Set up Prediction Market Bot

Prediction Market Bot (PMB) runs a private worker for Central Park weather, Bitcoin spot and three Bitcoin contract intervals. Each Bitcoin contract trial has its own $10,000 in paper cash. The offline weather replay runs without a login or internet connection after installation. For current quotes, enable the public feed in the relevant workspace. [Project repository](https://github.com/Jwarren-ENG/prediction-market-bot) · [Releases and asset downloads](https://github.com/Jwarren-ENG/prediction-market-bot/releases)

| What you want to do | Start here |
| --- | --- |
| Use the desktop app | [Install on macOS](#install-on-macos) or [Install on Windows](#install-on-windows) |
| Try it in a browser or run on a private server | [Run from source](#run-from-source) |
| See current contracts or verify that scans are working | [Turn on forward paper](#turn-on-forward-paper) |
| Keep collecting while the desktop window is closed | [Run the worker at login](#run-the-worker-at-login) |
| Move the desk to another computer | [Back up and restore](#back-up-and-restore) |

## Install on macOS

1. From the project's GitHub **Releases** page, download the macOS universal ZIP for the version you want. **Source code (zip)** is the source checkout. The desktop asset contains **Prediction Market Bot.app**. The universal build includes an Apple Silicon executable and an Intel executable, with a Python runtime for each. The app bundle declares macOS 12 as its minimum.
2. Double-click the ZIP in Finder. Move **Prediction Market Bot.app** to a permanent Applications folder before starting it. The command examples below use the **Applications** folder inside your home folder. If you placed the app in the main Applications folder, use `/Applications/Prediction Market Bot.app`.
3. Open the app. The distributed Mac build carries an ad hoc signature and lacks Apple Developer ID notarization. macOS may ask you to review it. Confirm the download source and use the per-app approval procedure appropriate for your system or choose the source installation. [Apple’s guide to downloaded apps](https://support.apple.com/guide/mac-help/open-a-mac-app-from-an-unknown-developer-mh40616/mac)
4. A clean workspace opens in **Research** with the synthetic replay already populated. Open **Worker** and find **Worker health** to see the version, heartbeat and operating mode. Window startup can take a few seconds.

You can verify the bundle's signature integrity and run the offline replay check in Terminal. The code signature check does not identify the publisher. If the release includes a checksum file, compare the downloaded ZIP’s SHA-256 against the entry for the same filename and version before opening it. For example, to calculate the digest for the version 0.3.0 archive in Downloads, run `shasum -a 256 "$HOME/Downloads/PMB-0.3.0-macOS-Universal.zip"`.

~~~sh
app="$HOME/Applications/Prediction Market Bot.app"
codesign --verify --deep --strict "$app"
worker="$app/Contents/MacOS/pmb-worker"
"$worker" replay-check
~~~

## Install on Windows

1. Download the **Windows x64 Setup.exe** asset from the same GitHub release. For version 0.3.0, its name is `PMB-0.3.0-Windows-x64-Setup.exe`. If checksums accompany the download, use PowerShell’s `Get-FileHash -Algorithm SHA256` with the installer path and compare the result.
2. Run the installer under the Windows account that will own the practice desk. Review its bundled component terms on the agreement screen. By default it installs under `%LOCALAPPDATA%\Programs\Prediction Market Bot`. The PMB installer is unsigned. Review your organization’s software policy if Windows asks about the publisher.
3. The normal installer checks for Microsoft Edge WebView2. If the runtime is missing, the included Microsoft bootstrapper uses the network to install it. On an offline or managed PC, an administrator can provision Microsoft’s Evergreen Runtime before installation. [Microsoft WebView2 distribution](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)
4. Open **Start → Prediction Market Bot → Prediction Market Bot**. Find **Research** and **Worker**. Python is included in the package. If the native window cannot open, you can still check the installed worker using PowerShell.

~~~powershell
$payload = Join-Path $env:LOCALAPPDATA 'Programs\Prediction Market Bot\payload'
$python = Join-Path $payload 'runtime\python.exe'
$entry = Join-Path $payload 'pmb.py'
& $python -I -B $entry replay-check
~~~

For the browser interface, run `& $python -I -B $entry serve` in one private PowerShell window. In a second window, set the same three variables and run `& $python -I -B $entry connect`. Open the returned private link. If a worker is already running, go straight to `connect`. A successful browser session verifies the packaged worker independently of the native WebView2 window. Check the release notes or native workflow result for Windows desktop validation for your build. [Release assets](https://github.com/Jwarren-ENG/prediction-market-bot/releases) · [Native workflow runs](https://github.com/Jwarren-ENG/prediction-market-bot/actions)

## First session

1. In **Research**, find **Price change replay**, choose **Open case**, and inspect **Decision**, **Evidence**, **Order book** and **Settlement**. In this case, a change in the executable book makes the same forecast qualify for a paper buy. **Run replay** repeats the fixture safely.
2. Open **Portfolio**. Compare **Starting paper cash**, **Marked equity**, **Net gain or loss** and **Realized P&L**. A filled position can still be open. Its value affects equity before settlement. The account label tells you whether you are viewing synthetic replay or forward observations.
3. Open **Activity** for the event log. Select an event type to narrow the list. In forward mode this page also shows the last quote check, comparable price moves, and scan records.
4. In **Worker → Display settings → Time zone**, choose the display clock you prefer. The corner displays your chosen clock and UTC side by side, updating every second. Choosing UTC shows it once. This selection belongs to the current browser or desktop view. The Central Park event date keeps its New York calendar date.

**Offline replay** selects the synthetic ledger and case in the interface. An online worker can still collect weather and run its authorized forward tasks in the background. For a fully disconnected exercise, use the `--offline` example in [Run from source](#run-from-source), or start a fresh desktop process with `PMB_OFFLINE=1` and its own data directory.

## Turn on forward paper

1. In **Worker → Operating mode**, choose **Forward paper**. The first online scan can fetch public National Weather Service and NOAA data before you enable Kalshi. Look for **Recent public weather reports** and the most recent scan details.
2. Decide whether you want automatic paper entries. To start by observing only, choose **Pause entries** in **Worker → Paper automation** before authorizing market capture. On a new worker, eligible paper entries are otherwise armed as soon as market permission and the strategy’s requirements are satisfied.
3. Under **Paper automation → Public market collection**, check **I authorize market data capture under my applicable terms** if permitted for your private use. The worker reads the public Kalshi API. No exchange key is involved. Clearing the checkbox stops new venue requests, discards in-flight venue data and clears the in-memory display history. Prior forward evidence and ledger records stay in the private workspace. [Kalshi’s public market data guide](https://docs.kalshi.com/getting_started/quick_start_market_data) · [Kalshi data terms](https://kalshi-public-docs.s3.amazonaws.com/kalshi-data-terms-of-service.pdf)
4. Open **Markets** and select a contract. **Last venue response received** should advance roughly every 15 seconds during normal operation, even when prices hold steady. **Price history** opens on **4 hours**. Choose **1 hour** or **15 min** to zoom in. **Recent observations** labels each timestamp as a worker poll or a Kalshi minute close. A new process can retrieve completed minutes from before startup. A missing side, unavailable minute or new listing can leave a gap.
5. Choose **Run scan** for a full weather, contract, depth, fee and risk assessment. The button reports **Scanning…**, **Scan complete**, **Scan complete with gaps**, or **Scan failed**. Inspect the receipt in **Activity** and the contract’s **Decision** and **Evidence** tabs in **Research**. A new installation will often show **Watch** while the model accumulates qualifying history. On **Portfolio → Evidence for paper entries**, **Comparable station days** shows the best-covered open forecast against its minimum. **Contracts with enough history** counts how many open contracts with checked sample counts meet that threshold. The panel appears once suitable forward records exist.
6. **Paper auto trading** on **Markets** and **Research** controls future forward paper entries. The switch reports **Yes**, **No** or **Unknown** based on worker confirmation. It cannot clear a risk stop. Use **Worker → Paper automation → Recheck risk and resume** after reviewing the cause. The weather Worker pause/resume applies to its replay and forward ledgers. Bitcoin has its own control. A pause allows ongoing quote collection, evidence checks and settlement of existing positions while market permission is active.

## Start the Bitcoin paper trial

1. Choose **Bitcoin** in the upper-left workspace selector. In **Worker**, enable the Kalshi or Polymarket US public feed for the contracts you want to follow. Both Polymarket US intervals share one data permission. Kalshi capture is shared with the weather desk. Account creation, deposits and exchange keys are unnecessary for paper trades.
2. In **Markets**, choose **5 minutes** (Polymarket US), **15 minutes** (Kalshi), or **1 hour** (Polymarket US). Each opens its own $10,000 paper account. Check the ticker and closing time above the book. Every new window has its own opening target and prices. The clock rolls with the window even when the next quotes take time to arrive.
3. To place a paper order, select buy or sell, YES or NO, quantity and a per-contract price limit. The worker attempts an immediate full fill at posted prices within your limit, including a separate taker fee. The receipt names the contract, executed price, quantity and fee or gives the reason for rejection. An expired window, a missing fee schedule, a stale book or insufficient size prevents a fill. Open **Portfolio** for holdings and **Activity** for receipts. Selling reduces an existing position. Any remaining units await the venue’s official settlement.
4. To let the worker choose event-contract trades, turn on the selected interval’s paper auto trading. A new install starts with automation off. Enable each trial you want to run and review its limits in **Worker**. The two US accounts and the Kalshi account keep independent cash and risk controls. Turning off a public feed also pauses its dependent automations. Keep the host online and awake for continuous coverage.
5. Use **Portfolio** to review today’s results in America/New_York, or the previous completed days when available. Read the count of completed windows and any unmarked positions alongside net gain and fees. The **Compare** tab also includes the weather, spot and passive common-time comparison. [Rule, limits and settlement](HOW_IT_WORKS.md#bitcoin-contract-paper-trading) · [Published bot results and their limitations](BITCOIN_BOT_EVIDENCE.md)
6. To run the independent spot account, select **Spot strategy**, enable collection, and turn on its daily rule. It reads Bitstamp’s BTC/USD book and completed candles. A manual buy uses a dollar budget including fees, and a manual sale accepts a BTC quantity or the full position. The price limit includes the 0.05% adverse price allowance. Successful manual spot orders turn off its automatic rule until you enable it again. Spot open cost is capped at $500. [Historical spot study](BITCOIN_RESEARCH.md)

## Run from source

Use Python 3.12 or later for these instructions. The worker and browser interface use Python’s standard library and the assets in `ui/`. Node.js is needed only for the JavaScript tests. Rust and Tauri are needed to build the native desktop window. [Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/)

Download and extract the source archive for the release. If you use Git, these commands check out the source for version 0.3.0.

~~~sh
git clone --branch v0.3.0 --depth 1 https://github.com/Jwarren-ENG/prediction-market-bot.git pmb
cd pmb
~~~

Open a terminal in the folder that contains `pmb.py`, `worker` and `ui`. On Windows, use `py -3.13` in place of `python3` if the Python launcher is installed with Python 3.13. You can also use the full path to a supported Python executable.

~~~sh
python3 --version
python3 -I -B pmb.py --help
python3 -I -B pmb.py replay-check
python3 -I -B pmb.py serve
~~~

Keep the terminal running. Open a second terminal **in the same checkout** and run the following command. Paste the returned link directly into a private browser window. It includes a bearer token that grants control of the worker.

~~~sh
python3 -I -B pmb.py connect
~~~

The default address is loopback on port 47823. To stop this worker from the second terminal, run `python3 -I -B pmb.py stop`. With a supervisor installed, follow the removal procedure below first if you want it to stay stopped. To choose a port automatically, use `serve --port 0` and get the assigned address with `connect`. The normal full scan interval is 300 seconds. For source deployments, `serve --interval SECONDS` accepts 60 to 86400. The separate quote sampler runs on its own clock.

For an isolated exercise, choose a new private data directory and port. Use the same `--data-dir` argument in both terminals. This example uses a directory outside the source checkout.

~~~sh
python3 -I -B pmb.py --data-dir "$HOME/pmb-offline-practice" serve --offline --port 0
~~~

~~~sh
python3 -I -B pmb.py --data-dir "$HOME/pmb-offline-practice" connect
~~~

In PowerShell, `"$HOME\pmb-offline-practice"` works for the example path. Put `--data-dir` before the subcommand, and `--offline` or `--port` after `serve`. The environment variables `PMB_DATA_DIR`, `PMB_PORT` and `PMB_OFFLINE=1` provide equivalent startup defaults. A worker locks its data directory and checks its private access key before a desktop attaches.

To exercise the repository checks locally, run these commands from the checkout. The replay check prints a JSON object with `"synthetic": true` and two decision comparisons. The example produces a **Pass** at an average ask of 55¢ and a **Paper buy** at 51.55¢. Both use the same modeled 60% probability.

~~~sh
python3 -B -m unittest discover -s tests -v
node --test tests/test_markets_ui.cjs
python3 -I -B pmb.py replay-check
~~~

## Build the native packages

Use a source checkout at the same tag as the package you want to reproduce. The following recipes target **v0.3.0**. The native build needs a current stable Rust toolchain, Cargo, Python 3.12 or later and the platform tools below. Run all commands from the repository root. Package to a fresh output directory. The packager refuses to overwrite a previous app or installer, and the runtime fetcher expects its destination to be new. Runtime archives are pinned by version and SHA-256 in the repository. [Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/) · [Runtime pins](../scripts/fetch_runtime.py) · [Third-party notices](THIRD_PARTY.md)

**macOS universal ZIP.** Install Xcode Command Line Tools and Rust with rustup on the build Mac. Both Rust targets are needed even when the machine itself is Apple Silicon. The packager uses `lipo` for the universal executable and includes both runtimes.

~~~sh
rustup target add aarch64-apple-darwin x86_64-apple-darwin
rustup component add rust-docs
cargo build --manifest-path desktop/Cargo.toml --locked --release --target aarch64-apple-darwin
cargo build --manifest-path desktop/Cargo.toml --locked --release --target x86_64-apple-darwin

python3 scripts/fetch_runtime.py mac-arm64 build/runtime-arm64
python3 scripts/fetch_runtime.py mac-x64 build/runtime-intel
python3 scripts/package.py --platform macos \
  --binary desktop/target/aarch64-apple-darwin/release/prediction-market-bot \
  --runtime build/runtime-arm64 \
  --intel-binary desktop/target/x86_64-apple-darwin/release/prediction-market-bot \
  --intel-runtime build/runtime-intel \
  --out dist-macos

codesign --verify --deep --strict "dist-macos/Prediction Market Bot.app"
"dist-macos/Prediction Market Bot.app/Contents/MacOS/pmb-worker" replay-check
ditto -c -k --sequesterRsrc --keepParent "dist-macos/Prediction Market Bot.app" \
  "dist-macos/PMB-0.3.0-macOS-Universal.zip"
~~~

For a build for your Mac’s own architecture, run `cargo build --manifest-path desktop/Cargo.toml --locked --release`, fetch just the runtime for that architecture, use `desktop/target/release/prediction-market-bot` as `--binary` and omit both Intel companion options. Label that ZIP with the architecture it contains. A shell built without packaging needs a matching bundled worker payload to start. The packager applies and verifies an ad hoc signature by default. Developer ID signing and Apple notarization are separate release operations.

**Windows x64 installer.** Use an x64 Windows build host with Visual Studio C++ Build Tools (Desktop development with C++ and a Windows SDK), the stable x64 MSVC Rust toolchain, NSIS and Python. Use an x64 Developer PowerShell. The following block checks the Microsoft signature on the WebView2 bootstrapper before embedding it. Adjust the `makensis.exe` path if NSIS was installed elsewhere.

~~~powershell
rustup component add rust-docs
cargo build --manifest-path desktop/Cargo.toml --locked --release
py -3.13 scripts/fetch_runtime.py windows-x64 build/runtime-win-x64

$bootstrap = Join-Path (Resolve-Path 'build') 'WebView2Setup.exe'
Invoke-WebRequest 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $bootstrap
$signature = Get-AuthenticodeSignature $bootstrap
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    throw 'The Microsoft WebView2 bootstrapper signature could not be verified.'
}
$nsis = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'NSIS\makensis.exe'
if (-not (Test-Path $nsis)) { throw 'Set $nsis to the installed makensis.exe.' }

py -3.13 scripts/package.py --platform windows --binary desktop/target/release/prediction-market-bot.exe --runtime build/runtime-win-x64 --bootstrap $bootstrap --makensis $nsis --out dist-windows
Get-FileHash -Algorithm SHA256 'dist-windows/PMB-0.3.0-Windows-x64-Setup.exe'
~~~

The resulting PMB setup executable is unsigned. Install it using the Windows procedure above, run the packaged replay, and open the native window to check it on Windows. The **Native paper desk** GitHub Actions workflow has separate macOS and Windows jobs. It runs the test suites and exercises packaged components on the target hosts. Select a run for the commit you are installing to inspect its outcome. [Build workflow](../.github/workflows/native.yml) · [Actions runs](https://github.com/Jwarren-ENG/prediction-market-bot/actions) · [Packaging flags](../scripts/package.py)

For an unattended installation, review the bundled third-party component terms first. If you accept them, pass `/S /ACCEPTCOMPONENTTERMS` to the setup executable. Interactive installs present the terms before copying files. [Component notices](THIRD_PARTY.md)

## Run the worker at login

Closing the desktop window leaves an already running worker alive. To start it again at login and recover it after a process failure, install the per-user supervisor. Put the app or source checkout at its permanent path first. Installation stops an existing worker in the same data directory and transfers ownership to the supervised process.

**macOS desktop package.** In Terminal, set the correct app location and run the helper.

~~~sh
worker="$HOME/Applications/Prediction Market Bot.app/Contents/MacOS/pmb-worker"
"$worker" background install
"$worker" background status
~~~

**Windows desktop package.** Run the following in PowerShell. This creates a Task Scheduler entry called **Prediction Market Bot personal paper worker** for the current user.

~~~powershell
$payload = Join-Path $env:LOCALAPPDATA 'Programs\Prediction Market Bot\payload'
$python = Join-Path $payload 'runtime\python.exe'
$supervisor = Join-Path $payload 'background.py'
& $python -I -B $supervisor install
& $python -I -B $supervisor status
~~~

For a source checkout on macOS or Windows, use the same interpreter that runs PMB. Run `python3 -I -B scripts/background.py install` or `status`. Mac uses a LaunchAgent called `com.pmb.paper-worker`. Windows uses a logon task with the current user’s interactive token. To stop automatic restarts when backing up, upgrading or uninstalling, run the corresponding `background remove` command on Mac, `& $python -I -B $supervisor remove` on Windows, or `python3 -I -B scripts/background.py remove` from source.

The supplied helpers start with their normal service environment. A custom data directory, mail configuration or provider key set in an interactive terminal may be absent at the next login. Supply those values through the service environment you manage, and use **Worker health** to confirm you reached the intended workspace. A machine that is asleep, powered off or signed out interrupts capture.

### Always-on private host

The same worker can run as a user service on Linux. Install the source under a stable directory and use an absolute Python path. For example, save the following as `~/.config/systemd/user/pmb.service`, replacing the two example absolute paths with the actual checkout and interpreter.

~~~ini
[Unit]
Description=Prediction Market Bot paper worker
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/pmb
ExecStart=/usr/bin/python3 -I -B /absolute/path/to/pmb/pmb.py serve
Restart=on-failure
RestartSec=15
UMask=0077

[Install]
WantedBy=default.target
~~~

~~~sh
systemctl --user daemon-reload
systemctl --user enable --now pmb.service
systemctl --user status pmb.service
~~~

The host’s user service manager must run across logout for continuous coverage. Follow your host administrator’s service policy. A public network listener is unnecessary. Keep one authoritative data directory on that host. From a second machine with SSH access, forward a local port while viewing the desk.

~~~sh
ssh -N -L 47824:127.0.0.1:47823 your_user@your_host
~~~

In a private terminal on the host, run `python3 -I -B pmb.py connect`. In the returned private link, change only the local port from `47823` to `47824`. Open the link in your browser, or paste it under **Worker → Connect to another worker** and choose **Open worker**. Keep the forwarding session active. The native app starts with its local worker on a fresh launch.

## Optional analysts and email

**Research analysts.** The local analyst works without a key. To add an external analyst, open **Worker → Research analysts**, choose the provider, enter a model ID available through your provider account and supply its key if required. Set the daily cap, enable the provider and choose **Save analyst settings**. Open **Research → contract → Evidence → Run research**. Automatic reviews can be enabled separately. With a daily cap of zero, local notes still work. External requests can incur provider charges.

On macOS, keys entered in the app use Keychain. Windows protects them using the user account’s DPAPI. A service can supply `GEMINI_API_KEY`, `PERPLEXITY_API_KEY` (also `PPLX_API_KEY`), or `OMNIROUTE_API_KEY` through its private environment. Set `OMNIROUTE_BASE_URL` for an environment key bound to a different gateway. The default local gateway is `http://127.0.0.1:20128/v1`. Remote gateways require HTTPS. Providers, model IDs and quotas depend on your account. [Research implementation](../worker/research.py)

**Email notifications.** A forward scan can trigger a daily owner digest and source-health or recovery notices. Delivery starts off. To preview the generic content locally use `python3 -I -B pmb.py mail-preview`. To opt in, supply the following environment values to the worker service. Keep credentials out of shell history and shared configuration.

| Setting | Value |
| --- | --- |
| `PMB_MAIL_ENABLED` | `1` enables delivery when the configuration is complete |
| `PMB_MAIL_TO`, `PMB_MAIL_FROM` | One owner recipient and a valid sender address |
| `PMB_SMTP_HOST` | Mail provider’s SMTP hostname |
| `PMB_SMTP_USER`, `PMB_SMTP_PASSWORD` | Credentials for that SMTP account |
| `PMB_SMTP_SECURITY` | `ssl` for implicit TLS, or `starttls` for required TLS upgrade |
| `PMB_SMTP_PORT` | Optional override. The respective defaults are 465 and 587. |

Check **Worker → Email notifications** for readiness, delivery failures and ambiguous acknowledgments. A stopped process cannot send an outage message about itself. [Notification implementation](../worker/notifications.py)

## Back up and restore

| Platform | Default private workspace |
| --- | --- |
| macOS | `~/Library/Application Support/Prediction Market Bot` |
| Windows | `%LOCALAPPDATA%\Prediction Market Bot` |
| Linux | `$XDG_DATA_HOME/prediction-market-bot` or `~/.local/share/prediction-market-bot` |

`replay.sqlite` and `forward.sqlite` contain separate weather accounts. `bitcoin.sqlite` stores the Bitcoin spot strategy, its manual order receipts and its passive account. Each `btc-contract-paper-*.sqlite` file holds one Bitcoin event-contract account, including orders, settlements, limits and Eastern daily results. `worker.sqlite` holds settings, scan receipts, paired performance observations, analyst notes and mail state. `feeds/feeds.sqlite3` contains the collected point-in-time inputs and outcomes. `access.key` grants control of the worker. Treat the entire workspace and its backups as private. The display’s 15-second quote tape is memory only. The weather desk can retrieve completed minute and hourly price history again from the venue.

1. Close the app. Remove or stop its supervisor, then issue the matching packaged or source `stop` command. For systemd, use `systemctl --user stop pmb.service`. Check that the terminal worker exited and the workspace’s `connection.json` is gone before copying databases.
2. Copy the **entire** workspace, including the `feeds` directory and SQLite sidecar files if present, to private encrypted storage. Record the app version with the backup. Keep this copy intact.
3. Restore a copy to an empty private directory. Start one worker there with `--data-dir`. For a first check, use `serve --offline --port 0` so that newer source activity cannot obscure the comparison. Open its private link and compare cash, orders, positions, fee totals, recent events and the selected operating mode.
4. When satisfied, stop the verification worker, start the intended worker on that directory and re-enable its supervisor as needed. Re-enter research keys when moving to a different OS account. Supply service-only credentials separately.

For upgrades, back up first, stop the supervised worker, replace the packaged app in its permanent location or run the new Windows installer, then restart and confirm the version under **Worker health**. Removing the Mac app or Windows package leaves the separately stored practice history available for a later restore.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| The app cannot connect or asks for authentication | Use `connect` against the same data directory as `serve`. Open the entire URL including its fragment privately. A plain loopback address can load the page before it has authority to read or control the worker. |
| The port is busy or the directory is locked | An app or supervisor may already own the workspace. Try `connect`. For a separate exercise, choose both a different directory and `--port 0`. |
| **Worker offline** or no forward scans | Check **Worker health → External feeds**. A process started with `--offline` or `PMB_OFFLINE=1` needs a restart in online mode to fetch sources. Switching the visible account cannot change that startup setting. |
| Weather appears but **Markets** is empty | Check **Public market collection**, whether the host is online, and the message above the list. An empty active catalog, a network error and a Kalshi rate limit have distinct messages. |
| Chart rows stay flat, have a blank side, or start late | Read the last venue receipt and **Recent observations**. Venues can report unchanged prices or omit one side or some minute closes. A just-listed contract may have very little history. |
| A scan is partial, or every contract says **Watch** | Open the latest **Activity** receipt and the contract’s **Decision** tab. Common reasons include an unavailable source, a changed rule or fee, stale depth, or insufficient comparable resolved days. |
| New entries remain paused | Inspect **Worker → Paper automation** and **Portfolio → Paper risk**. A risk stop needs the cause to clear and an explicit recheck. The forward risk latch persists across restarts. |
| An external analyst is unavailable | Check provider enablement, model access, key availability in the worker’s account, and daily and monthly budgets. Verify a **local** note first. |
| The app closes but requests continue | The worker and window have separate lifetimes. See **Run the worker at login** and use the appropriate `stop` command when you intend to stop all work. |

For the forecast calculation, precise entry limits and the distinction between marked equity and realized gains, read the [strategy and accounting guide](HOW_IT_WORKS.md).
