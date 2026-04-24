"""
Servidor web para facturación automática.

Expone una UI en localhost (LAN) con un botón por logística.
Solo permite un job a la vez (lock global) porque comparten el login GBP.

Uso:
    python app.py
    # luego abrir http://localhost:5000 (o http://<ip-lan>:5000 desde otra PC)
"""

import logging
import queue
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, stream_with_context

from src.main import cargar_config, ejecutar, setup_logging


# ---------------------------------------------------------------------------
# Catálogo de logísticas (leído de config.yaml en cada request)
# ---------------------------------------------------------------------------

def listar_logisticas() -> list[dict]:
    """Lee el bloque `logisticas:` del config.yaml y lo adapta a la UI.

    Se relee en cada request para que editar el YAML no requiera reiniciar.
    """
    config = cargar_config()
    logisticas: dict = config.get("logisticas", {})
    resultado = []
    for lid, datos in logisticas.items():
        resultado.append({
            "id": lid,
            "label": datos.get("label", lid),
            "enabled": bool(datos.get("habilitada", False)),
            "nota": datos.get("nota", ""),
        })
    return resultado


def obtener_logistica(logistica_id: str) -> dict | None:
    """Busca una logística por id. Retorna None si no existe."""
    for l in listar_logisticas():
        if l["id"] == logistica_id:
            return l
    return None

# ---------------------------------------------------------------------------
# Estado de jobs
# ---------------------------------------------------------------------------
# Solo 1 job a la vez. `JOB_LOCK` protege el acceso a `current_job`.
JOB_LOCK = threading.Lock()
current_job: dict | None = None  # {"id", "logistica", "status", "queue", "started_at", ...}

# Sentinela para cerrar el stream SSE
_STREAM_END = object()


# ---------------------------------------------------------------------------
# Logging handler que empuja cada registro a la queue de un job
# ---------------------------------------------------------------------------

class QueueLogHandler(logging.Handler):
    """Reenvía cada log del logger 'facturacion' a la queue del job actual."""

    def __init__(self, q: queue.Queue) -> None:
        super().__init__(level=logging.INFO)
        self._q = q
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put_nowait(self.format(record))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Runner del job (thread)
# ---------------------------------------------------------------------------

def _run_job(job_id: str, logistica: str) -> None:
    """Ejecuta el flujo en un thread, empujando logs a la queue del job."""
    global current_job
    assert current_job is not None

    logger = logging.getLogger("facturacion")
    handler = QueueLogHandler(current_job["queue"])
    logger.addHandler(handler)

    try:
        procesados = ejecutar(logistica)
        current_job["status"] = "ok"
        current_job["procesados"] = procesados
        current_job["queue"].put_nowait(
            f"=== Finalizado OK — {procesados} pedidos procesados ==="
        )
    except Exception as e:
        current_job["status"] = "error"
        current_job["error"] = str(e)
        logger.error("Error durante la ejecución: %s", e, exc_info=True)
        current_job["queue"].put_nowait(f"=== Finalizado con ERROR: {e} ===")
    finally:
        current_job["finished_at"] = datetime.now().isoformat(timespec="seconds")
        current_job["queue"].put_nowait(_STREAM_END)
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html", logisticas=listar_logisticas())


@app.route("/status")
def status():
    """Devuelve el estado del job activo (si hay)."""
    with JOB_LOCK:
        if current_job is None:
            return jsonify({"running": False})
        return jsonify({
            "running": current_job["status"] == "running",
            "id": current_job["id"],
            "logistica": current_job["logistica"],
            "status": current_job["status"],
            "started_at": current_job["started_at"],
            "finished_at": current_job.get("finished_at"),
            "procesados": current_job.get("procesados"),
            "error": current_job.get("error"),
        })


@app.route("/run/<logistica_id>", methods=["POST"])
def run(logistica_id: str):
    """Dispara un job nuevo si no hay otro corriendo."""
    global current_job

    logistica = obtener_logistica(logistica_id)
    if logistica is None:
        return jsonify({"error": f"Logística desconocida: {logistica_id}"}), 404
    if not logistica["enabled"]:
        return jsonify({"error": f"Logística '{logistica['label']}' no está habilitada."}), 400

    with JOB_LOCK:
        if current_job is not None and current_job["status"] == "running":
            return jsonify({
                "error": "Ya hay un job corriendo",
                "running_id": current_job["id"],
                "running_logistica": current_job["logistica"],
            }), 409

        job_id = uuid.uuid4().hex[:8]
        current_job = {
            "id": job_id,
            "logistica": logistica_id,
            "status": "running",
            "queue": queue.Queue(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }

    t = threading.Thread(target=_run_job, args=(job_id, logistica_id), daemon=True)
    t.start()

    return jsonify({"job_id": job_id, "logistica": logistica_id})


@app.route("/stream/<job_id>")
def stream(job_id: str):
    """Server-Sent Events: transmite los logs del job en tiempo real."""
    with JOB_LOCK:
        if current_job is None or current_job["id"] != job_id:
            return Response("Job no encontrado", status=404)
        q = current_job["queue"]

    def gen():
        # Keep-alive inicial
        yield ": connected\n\n"
        while True:
            try:
                item = q.get(timeout=15)
            except queue.Empty:
                # Keep-alive para que el navegador no cierre la conexión
                yield ": keepalive\n\n"
                continue

            if item is _STREAM_END:
                yield "event: end\ndata: fin\n\n"
                break

            # Cada línea de log va como un evento SSE 'log'
            # Escapar saltos de línea (SSE usa \n como separador de campo)
            safe = str(item).replace("\n", "\\n")
            yield f"event: log\ndata: {safe}\n\n"

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Setup de logging a archivo (se comparte entre jobs durante la sesión del server)
    log_file = setup_logging(to_stdout=True)
    print(f"[server] Log file: {log_file}")
    print("[server] Arrancando en http://0.0.0.0:5000 (accesible desde LAN)")
    # threaded=True para que SSE y /run no se bloqueen entre sí
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)
