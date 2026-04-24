"""
Reglas de negocio para la facturación automática.

Devuelve los filtros configurados en config.yaml para una logística dada.
"""

from typing import Any


def obtener_filtros(config: dict, logistica_id: str) -> dict[str, str]:
    """Devuelve los valores de filtro de una logística.

    Parameters
    ----------
    config : dict
        Config completo cargado desde config.yaml.
    logistica_id : str
        Clave dentro de ``config["logisticas"]`` (ej: "enviopack").

    Returns
    -------
    dict
        Claves: tipo_pedido, percepciones, vendedor, transporte.

    Raises
    ------
    KeyError
        Si la logística no existe en el config.
    ValueError
        Si la logística está deshabilitada o le falta el value de transporte.
    """
    logisticas: dict[str, Any] = config.get("logisticas", {})
    if logistica_id not in logisticas:
        raise KeyError(
            f"Logística '{logistica_id}' no definida en config.yaml "
            f"(disponibles: {', '.join(logisticas) or 'ninguna'})."
        )

    entrada: dict[str, Any] = logisticas[logistica_id]
    if not entrada.get("habilitada", False):
        raise ValueError(
            f"Logística '{logistica_id}' está deshabilitada en config.yaml "
            f"(habilitada: false)."
        )

    filtros: dict[str, Any] = entrada.get("filtros", {})
    resultado = {
        "tipo_pedido": str(filtros.get("tipo_pedido", "")),
        "percepciones": str(filtros.get("percepciones", "")),
        "vendedor": str(filtros.get("vendedor", "")),
        "transporte": str(filtros.get("transporte", "")),
    }

    faltantes = [k for k, v in resultado.items() if not v]
    if faltantes:
        raise ValueError(
            f"Logística '{logistica_id}' tiene filtros vacíos: {', '.join(faltantes)}. "
            "Completar config.yaml antes de ejecutar."
        )

    return resultado
