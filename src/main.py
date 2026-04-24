"""
Facturación automática — Orquestador principal.

Ejecuta el flujo completo para una logística dada:
  1. Conectar al ERP GBP
  2. Navegar al módulo de pedidos
  3. Aplicar filtros configurados
  4. Procesar todos los pedidos encontrados
  5. Desconectar (siempre, incluso ante errores)

Uso CLI:
    python -m src.main [logistica]      # logistica opcional, default: enviopack

Uso desde la web (app.py):
    from src.main import ejecutar
    ejecutar("enviopack")
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.gbp_client import GBPClient
from src.rules import obtener_filtros

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("facturacion")


# ---------------------------------------------------------------------------
# Setup de logging (idempotente)
# ---------------------------------------------------------------------------

def setup_logging(to_stdout: bool = True) -> Path:
    """Configura el logger 'facturacion' con archivo timestamped y stdout.

    Idempotente: si ya hay handlers configurados, no duplica.
    Retorna el path del archivo de log creado.
    """
    load_dotenv()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"facturacion_{timestamp}.log"

    if logger.handlers:
        # Ya configurado — devolver path del primer FileHandler si existe
        for h in logger.handlers:
            if isinstance(h, logging.FileHandler):
                return Path(h.baseFilename)
        return log_file

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return log_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cargar_config() -> dict:
    """Carga la configuración desde config.yaml."""
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"No se encontró config.yaml en {config_path}")
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def validar_env() -> tuple[str, str, str]:
    """Valida las variables de entorno requeridas. Lanza RuntimeError si faltan."""
    load_dotenv()

    erp_url = os.getenv("ERP_URL", "")
    erp_user = os.getenv("ERP_USER", "")
    erp_password = os.getenv("ERP_PASSWORD", "")

    faltantes = [
        nombre
        for nombre, valor in (("ERP_URL", erp_url), ("ERP_USER", erp_user), ("ERP_PASSWORD", erp_password))
        if not valor
    ]
    if faltantes:
        raise RuntimeError(
            f"Faltan variables de entorno: {', '.join(faltantes)} — "
            "definirlas en .env en la raíz del proyecto."
        )

    return erp_url, erp_user, erp_password


# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------

def ejecutar(logistica: str = "enviopack") -> int:
    """Ejecuta el flujo completo de facturación para la logística indicada.

    Parameters
    ----------
    logistica : str
        Identificador de la logística. Por ahora solo "enviopack".

    Returns
    -------
    int
        Cantidad de pedidos procesados.

    Raises
    ------
    Exception
        Cualquier error durante la ejecución — el caller decide qué hacer.
    """
    logger.info("=" * 60)
    logger.info("Facturación automática — Inicio (logística: %s)", logistica)
    logger.info("=" * 60)

    config = cargar_config()
    erp_url, erp_user, erp_password = validar_env()
    filtros = obtener_filtros(config, logistica)

    cliente = GBPClient(config)
    procesados = 0

    try:
        cliente.conectar(erp_url, erp_user, erp_password)
        cliente.navegar_a_pedidos()
        cliente.aplicar_filtros(filtros)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        screenshot_path = LOG_DIR / f"filtros_aplicados_{timestamp}.png"
        cliente._page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Screenshot guardado: %s", screenshot_path)

        procesados = cliente.procesar_pedidos()

        logger.info("=" * 60)
        logger.info("RESULTADO: %d pedidos procesados.", procesados)
        logger.info("=" * 60)
    finally:
        # SIEMPRE desconectar para evitar bloqueo del ERP
        cliente.desconectar()

    return procesados


# ---------------------------------------------------------------------------
# Entry point CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log_file = setup_logging()
    logistica_arg = sys.argv[1] if len(sys.argv) > 1 else "enviopack"
    try:
        ejecutar(logistica_arg)
    except Exception as e:
        logger.error("Error durante la ejecución: %s", e, exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Log completo: %s", log_file)
