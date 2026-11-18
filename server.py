import os
import sys
import time
import socket
import shutil
import tempfile
import subprocess
import webbrowser
from threading import Lock, Thread, Timer
from flask import Flask, request, jsonify, send_file, after_this_request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "server.log")
UPDATE_STAMP_PATH = os.path.join(BASE_DIR, ".last_update")

# Quando avviato con pythonw.exe (senza terminale), stdout/stderr possono essere None.
if sys.stdout is None or sys.stderr is None:
    try:
        log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = log_file
        if sys.stderr is None:
            sys.stderr = log_file
    except OSError:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull

HOST = "127.0.0.1"
PORT = 5000
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AUTO_UPDATE_COOLDOWN_SEC = 15 * 60       # evita aggiornamenti ripetuti su errori ravvicinati
DAILY_UPDATE_INTERVAL_SEC = 24 * 60 * 60 # controllo silenzioso giornaliero all'avvio

_update_lock = Lock()
_last_auto_update_attempt = 0.0

app = Flask(__name__)


def _get_python_cli():
    exe_dir = os.path.dirname(sys.executable)
    candidate = os.path.join(exe_dir, "python.exe")
    if os.path.exists(candidate):
        return candidate
    return sys.executable


def _run_hidden(cmd, timeout=180):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _get_ytdlp_version():
    try:
        res = _run_hidden([_get_python_cli(), "-m", "yt_dlp", "--version"], timeout=15)
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return "sconosciuta"


def _upgrade_ytdlp(force=False):
    """Aggiorna yt-dlp tramite pip; restituisce (updated: bool, message: str)."""
    global _last_auto_update_attempt
    with _update_lock:
        now = time.time()
        if not force and (now - _last_auto_update_attempt) < AUTO_UPDATE_COOLDOWN_SEC:
            return False, "Aggiornamento gia tentato di recente."
        _last_auto_update_attempt = now

        old_ver = _get_ytdlp_version()
        try:
            res = _run_hidden(
                [_get_python_cli(), "-m", "pip", "install", "--upgrade", "yt-dlp"],
                timeout=120,
            )
            try:
                with open(UPDATE_STAMP_PATH, "w", encoding="utf-8") as f:
                    f.write(str(now))
            except OSError:
                pass

            if res.returncode != 0:
                return False, f"Aggiornamento pip fallito: {res.stderr.strip()}"

            new_ver = _get_ytdlp_version()
            if new_ver != old_ver:
                print(f"[Auto-Update] yt-dlp aggiornato: {old_ver} -> {new_ver}")
                return True, f"Motore aggiornato ({old_ver} -> {new_ver})"
            return False, f"Motore gia all'ultima versione ({new_ver})"
        except Exception as e:
            return False, f"Errore durante l'aggiornamento: {e}"


def _background_daily_update():
    try:
        if os.path.exists(UPDATE_STAMP_PATH):
            with open(UPDATE_STAMP_PATH, "r", encoding="utf-8") as f:
                last = float((f.read() or "0").strip())
            if (time.time() - last) < DAILY_UPDATE_INTERVAL_SEC:
                return
    except Exception:
        pass
    _upgrade_ytdlp(force=True)


def _find_downloaded_file(temp_dir, printed_path=""):
    if printed_path and os.path.exists(printed_path):
        return printed_path
    for name in os.listdir(temp_dir):
        if name.lower().endswith(".mp4"):
            return os.path.join(temp_dir, name)
    files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir)]
    return files[0] if files else ""


def _execute_ytdlp(url, temp_dir, fallback_recode=False):
    has_ffmpeg = shutil.which("ffmpeg") is not None
    outtmpl = os.path.join(temp_dir, "%(title).60s_%(id)s.%(ext)s")

    if fallback_recode:
        fmt = "best"
    elif has_ffmpeg:
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"
    else:
        fmt = "best[ext=mp4]/best"

    cmd = [
        _get_python_cli(),
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--restrict-filenames",
        "--no-warnings",
        "-f",
        fmt,
        "-o",
        outtmpl,
        "--print",
        "after_move:filepath",
    ]

    if fallback_recode and has_ffmpeg:
        cmd.extend(["--recode-video", "mp4"])
    else:
        cmd.extend(["--merge-output-format", "mp4"])

    cmd.append(url)
    res = _run_hidden(cmd, timeout=300)
    if res.returncode != 0:
        err_msg = (res.stderr or res.stdout or "Errore sconosciuto").strip()
        raise RuntimeError(err_msg)

    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    printed_path = lines[-1] if lines else ""
    filepath = _find_downloaded_file(temp_dir, printed_path)
    if not filepath or not os.path.exists(filepath):
        raise RuntimeError("File video non trovato dopo il download.")
    return filepath


@app.route("/")
def home():
    return send_file(os.path.join(BASE_DIR, "index.html"))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "yt_dlp_version": _get_ytdlp_version()})


@app.route("/download", methods=["POST"])
def download_video():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "Inserisci un link valido (es. https://...)."}), 400

    temp_dir = tempfile.mkdtemp(prefix="videotomp4_")

    try:
        try:
            filepath = _execute_ytdlp(url, temp_dir, fallback_recode=False)
        except Exception as first_err:
            # 1) Auto-aggiornamento di yt-dlp se un social ha cambiato regole/API
            updated, _ = _upgrade_ytdlp(force=False)
            if updated:
                try:
                    filepath = _execute_ytdlp(url, temp_dir, fallback_recode=False)
                except Exception:
                    # 2) Fallback adattivo su formato/ricodifica universale
                    filepath = _execute_ytdlp(url, temp_dir, fallback_recode=True)
            else:
                # 2) Prova comunque il fallback adattivo prima di arrendersi
                try:
                    filepath = _execute_ytdlp(url, temp_dir, fallback_recode=True)
                except Exception:
                    raise first_err

        @after_this_request
        def cleanup(response):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return response

        download_name = os.path.basename(filepath)
        if not download_name.lower().endswith(".mp4"):
            download_name = os.path.splitext(download_name)[0] + ".mp4"

        return send_file(
            filepath,
            as_attachment=True,
            download_name=download_name,
            mimetype="video/mp4",
        )
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return jsonify({"error": f"Impossibile scaricare il video: {str(e)}"}), 500


def apri_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


def _is_port_in_use(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    if _is_port_in_use(HOST, PORT):
        print(f"Video to MP4 e gia attivo su: {url}")
        apri_browser()
    else:
        print(f"Video to MP4 attivo su: {url}")
        Thread(target=_background_daily_update, daemon=True).start()
        Timer(1.0, apri_browser).start()
        app.run(host=HOST, port=PORT, debug=False)
