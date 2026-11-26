#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    path::{Path, PathBuf},
    process::Command,
};
use tauri::{Manager, Url, WebviewUrl, WebviewWindowBuilder};

fn start_worker(resources: &Path) -> Result<Url, String> {
    let payload = resources.join("payload");
    let interpreter = if cfg!(windows) {
        payload.join("runtime").join("python.exe")
    } else if cfg!(all(target_os = "macos", target_arch = "x86_64"))
        && payload.join("runtime-intel").exists()
    {
        payload.join("runtime-intel").join("bin").join("python3")
    } else {
        payload.join("runtime").join("bin").join("python3")
    };
    let mut command = Command::new(interpreter);
    command
        .arg("-I")
        .arg("-B")
        .arg(payload.join("pmb.py"))
        .arg("desktop");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x08000000);
    }
    let output = command
        .output()
        .map_err(|e| format!("Could not start the bundled worker: {e}"))?;
    if !output.status.success() {
        let details = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "Worker startup needs attention. {}",
            details.trim().chars().take(400).collect::<String>()
        ));
    }
    let data: serde_json::Value = serde_json::from_slice(&output.stdout)
        .map_err(|_| "The worker returned an invalid connection record.".to_string())?;
    let url = Url::parse(
        data["url"]
            .as_str()
            .ok_or("The worker returned no address.")?,
    )
    .map_err(|_| "The worker address could not be read.".to_string())?;
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || url.fragment().is_none()
    {
        return Err("The local worker returned an unexpected address.".into());
    }
    Ok(url)
}

// Capture native UI results. An isolated offline CI run may also verify the authenticated replay control.
fn smoke_report(window: tauri::WebviewWindow, path: PathBuf) {
    let check_replay = std::env::var("PMB_DESKTOP_SMOKE_ACTION").as_deref() == Ok("replay");
    if check_replay
        && (std::env::var("PMB_OFFLINE").as_deref() != Ok("1")
            || std::env::var_os("PMB_DATA_DIR").is_none_or(|path| path.is_empty()))
    {
        return;
    }
    let query = if check_replay {
        include_str!("smoke-replay.js")
    } else {
        "({heading: document.querySelector('main h1')?.textContent || '', rows:document.querySelectorAll('main table tbody tr').length, nav:document.querySelector('nav')?.innerText || ''})"
    };
    std::thread::spawn(move || {
        for _ in 0..60 {
            std::thread::sleep(std::time::Duration::from_millis(350));
            let (tx, rx) = std::sync::mpsc::channel();
            if window
                .eval_with_callback(query, move |value| {
                    let _ = tx.send(value);
                })
                .is_err()
            {
                continue;
            }
            if let Ok(text) = rx.recv_timeout(std::time::Duration::from_secs(2)) {
                if let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) {
                    if value["rows"].as_u64().unwrap_or(0) > 0
                        && value["nav"].as_str().unwrap_or("").contains("Portfolio")
                    {
                        if std::fs::write(&path, value.to_string()).is_ok() {
                            let _ = window.close();
                        }
                        return;
                    }
                }
            }
        }
    });
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            let resources = app.path().resource_dir()?;
            let window =
                WebviewWindowBuilder::new(app, "desk", WebviewUrl::App("index.html".into()))
                    .title("Prediction Market Bot")
                    .inner_size(1270.0, 835.0)
                    .min_inner_size(575.0, 470.0)
                    .on_page_load(|window, payload| {
                        if payload.event() == tauri::webview::PageLoadEvent::Finished
                            && payload.url().host_str() == Some("127.0.0.1")
                        {
                            if let Some(path) = std::env::var_os("PMB_DESKTOP_SMOKE_REPORT") {
                                smoke_report(window, PathBuf::from(path));
                            }
                        }
                    })
                    .build()?;
            std::thread::spawn(move || {
                let result = start_worker(&resources)
                    .and_then(|url| window.navigate(url).map_err(|e| e.to_string()));
                if let Err(message) = result {
                    let text = serde_json::Value::String(message).to_string();
                    let _ = window.eval(format!(
                        "document.getElementById('status').textContent = {text}"
                    ));
                }
            });
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("desktop runtime unavailable");
}
