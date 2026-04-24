"""
Fase 0 — Validación de Playwright contra GBP (ERP web).

Este script hace login al ERP, lista las opciones del menú lateral (TreeView)
y genera un dump del árbol de navegación para identificar dónde está la
sección de facturación.

Uso:
    python validacion_gbp.py
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, Frame, TimeoutError as PWTimeout

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_file = LOG_DIR / f"validacion_gbp_{timestamp}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("validacion_gbp")

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).parent / "config.yaml"


def cargar_config() -> dict:
    """Carga la configuración desde config.yaml."""
    if not CONFIG_PATH.exists():
        logger.error("No se encontró config.yaml en %s", CONFIG_PATH)
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Login (adaptado de tu script de stock, mismo flujo)
# ---------------------------------------------------------------------------
def aceptar_dialogo(dialog):
    """Acepta cualquier alert/confirm/prompt del navegador."""
    logger.info("Diálogo detectado: '%s' — aceptando.", dialog.message)
    dialog.accept()


def seleccionar_perfil(page: Page, perfil_requerido: str, timeout: int) -> None:
    """
    Verifica que el perfil activo sea el correcto. Si no lo es, lo cambia.

    El select #ddlProfile está en la página principal (wfmMain), no en un frame.
    Al cambiar el valor, dispara un __doPostBack que recarga la página.
    """
    timeout_ms = timeout * 1000
    logger.info("Verificando perfil activo...")

    selector_perfil = page.locator("#ddlProfile")

    # Buscar en la página principal y en frames
    if selector_perfil.count() == 0:
        for frame in page.frames:
            selector_perfil = frame.locator("#ddlProfile")
            if selector_perfil.count() > 0:
                break

    if selector_perfil.count() == 0:
        logger.warning("No se encontró el selector de perfil (#ddlProfile).")
        return

    perfil_actual = selector_perfil.evaluate(
        "el => el.options[el.selectedIndex].text"
    )
    logger.info("Perfil actual: '%s'", perfil_actual)

    if perfil_requerido in perfil_actual:
        logger.info("Perfil correcto, no hay que cambiarlo.")
        return

    # Cambiar al perfil requerido
    logger.info("Cambiando perfil a '%s'...", perfil_requerido)
    selector_perfil.select_option(label=perfil_requerido)
    # El select dispara un __doPostBack que recarga la página
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2000)

    # Verificar que el cambio se aplicó
    perfil_nuevo = selector_perfil.evaluate(
        "el => el.options[el.selectedIndex].text"
    )
    logger.info("Perfil cambiado a: '%s'", perfil_nuevo)
    if perfil_requerido not in perfil_nuevo:
        logger.warning("El perfil no se cambió correctamente. Actual: '%s'", perfil_nuevo)


def login_erp(page: Page, url: str, usuario: str, password: str, timeout: int,
              perfil_requerido: str = "04-Administrativos") -> Page:
    """
    Hace login al ERP GBP y selecciona el perfil correcto.

    Flujo:
    1. Navegar a /arg → se abre popup automático a wfmPresentation.aspx
    2. En el popup: click en #lnkLogin (botón naranja "Ingresar")
       → carga formulario de credenciales en la misma ventana
    3. Llenar #UserName y #Password
    4. Click en botón de login (#wucIB_Login_LB)
    5. Esperar redirección a wfmMain.aspx
    6. Verificar/cambiar perfil a perfil_requerido
    """
    timeout_ms = timeout * 1000
    page.on("dialog", aceptar_dialogo)

    # Paso 1: navegar a /arg → se abre popup automático
    logger.info("Navegando a %s...", url)
    with page.context.expect_page(timeout=timeout_ms) as popup_info:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    popup = popup_info.value
    popup.on("dialog", aceptar_dialogo)
    popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    logger.info("Popup abierto: %s", popup.url)

    # Paso 2: click en #lnkLogin (botón naranja "Ingresar")
    logger.info("Esperando botón 'Ingresar' (#lnkLogin)...")
    popup.locator("#lnkLogin").wait_for(state="visible", timeout=timeout_ms)
    logger.info("Clickeando #lnkLogin...")
    popup.locator("#lnkLogin").click()
    popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

    # Paso 3: llenar credenciales (misma ventana, ahora muestra el formulario)
    logger.info("Esperando formulario de login...")
    campo_usuario = popup.locator("#UserName")
    campo_usuario.wait_for(state="visible", timeout=timeout_ms)

    logger.info("Ingresando credenciales...")
    campo_usuario.fill(usuario)

    campo_password = popup.locator("#Password")
    campo_password.wait_for(state="visible", timeout=timeout_ms)
    # Usar press_sequentially (tecla por tecla) porque el campo tiene un
    # onkeyup (wfmLogin_hidP2DC) que procesa la contraseña con cada tecla.
    # fill() no dispara eventos de teclado y el ERP no registra el password.
    campo_password.press_sequentially(password, delay=50)

    # Paso 4: click en botón de login
    boton_login = popup.locator("#wucIB_Login_LB")
    if boton_login.count() == 0:
        boton_login = popup.get_by_role("link", name="LogIn")
    if boton_login.count() == 0:
        boton_login = popup.get_by_role("link", name="Entrar")
    logger.info("Clickeando botón de login...")
    boton_login.click(timeout=timeout_ms)

    # Verificar si hubo error de credenciales
    popup.wait_for_timeout(2000)
    error_msg = popup.locator("text=not valid")
    if error_msg.count() > 0:
        logger.error("Credenciales inválidas. Verificá ERP_USER y ERP_PASSWORD en .env")
        raise Exception("Login fallido: usuario o contraseña incorrectos.")

    # Paso 5: esperar a que cargue wfmMain
    logger.info("Esperando carga de wfmMain...")
    popup.wait_for_url("**/wfmMain**", timeout=timeout_ms)
    logger.info("Login exitoso. URL: %s", popup.url)

    # Paso 6: verificar/cambiar perfil a "04-Administrativos"
    seleccionar_perfil(popup, perfil_requerido=perfil_requerido, timeout=timeout)

    return popup


# ---------------------------------------------------------------------------
# Exploración del menú
# ---------------------------------------------------------------------------
def explorar_menu(page: Page, timeout: int) -> list[dict]:
    """
    Accede al frameMenu y extrae todos los elementos del menú lateral.
    Busca links, nodos de árbol y cualquier elemento clickeable.
    """
    timeout_ms = timeout * 1000

    # Listar todos los frames disponibles
    logger.info("Frames disponibles: %s", [f.name or f.url for f in page.frames])

    # Acceder al frame del menú
    logger.info("Accediendo a frameMenu...")
    frame_menu: Frame = page.frame("frameMenu")
    if not frame_menu:
        # Intentar buscar el menú en otros frames
        for frame in page.frames:
            nombre = frame.name or ""
            if "menu" in nombre.lower() or "tree" in nombre.lower():
                frame_menu = frame
                logger.info("Frame de menú encontrado como: %s", nombre)
                break
        if not frame_menu:
            logger.error("No se encontró frame de menú.")
            return []

    # Esperar a que el frame cargue contenido
    logger.info("Esperando contenido del menú...")
    try:
        frame_menu.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        frame_menu.wait_for_selector("a, span, td", timeout=timeout_ms)
    except PWTimeout:
        logger.error("El frame del menú no cargó contenido.")
        return []

    # Extraer todos los elementos del menú de forma genérica
    logger.info("Extrayendo elementos del menú...")
    nodos = frame_menu.evaluate("""
    () => {
        const resultado = [];

        // Buscar todos los links y elementos clickeables
        const elementos = document.querySelectorAll('a, [onclick], [role="treeitem"]');
        const vistos = new Set();

        for (const el of elementos) {
            const texto = (el.textContent || '').trim();
            if (!texto || texto.length > 80 || vistos.has(texto)) continue;
            vistos.add(texto);

            const id = el.id || '';
            const href = el.getAttribute('href') || '';
            const onclick = el.getAttribute('onclick') || '';
            const tag = el.tagName;

            // Calcular nivel de indentación
            let nivel = 0;
            let parent = el.parentElement;
            while (parent) {
                const pTag = parent.tagName;
                if (pTag === 'DIV' || pTag === 'UL' || pTag === 'TABLE') {
                    if (parent.id && (parent.id.includes('Nodes') || parent.id.includes('Child')))
                        nivel++;
                }
                parent = parent.parentElement;
            }

            // Detectar si es nodo expandible o hoja
            const esNodo = el.classList.contains('treeNode') ||
                           el.querySelector('img[src*="plus"]') !== null ||
                           onclick.includes('Toggle') ||
                           (href.includes('javascript:') && !href.includes('__doPostBack'));
            const esHoja = href.includes('__doPostBack') || href.includes('.aspx');

            resultado.push({
                id: id,
                texto: texto,
                tag: tag,
                href: href.substring(0, 100),
                nivel: nivel,
                tipo: esNodo ? 'nodo' : (esHoja ? 'hoja' : 'otro')
            });
        }

        return resultado;
    }
    """)

    return nodos


# ---------------------------------------------------------------------------
# Logout — SIEMPRE usar "Salir" antes de cerrar el navegador
# ---------------------------------------------------------------------------
def logout_erp(page: Page, timeout: int) -> None:
    """
    Cierra la sesión del ERP usando el enlace #lnkLogout.

    IMPORTANTE: nunca cerrar el navegador sin hacer logout primero.
    Si se cierra con la X, el ERP queda bloqueado ~5 minutos.
    """
    timeout_ms = timeout * 1000
    try:
        logger.info("Cerrando sesión del ERP (lnkLogout)...")

        # El botón "salir" es <a id="lnkLogout"> y puede estar en la página o en un frame
        for frame in [page] + page.frames:
            salir = frame.locator("#lnkLogout")
            if salir.count() > 0:
                salir.click(timeout=timeout_ms)
                logger.info("Logout exitoso — click en #lnkLogout.")
                page.wait_for_timeout(2000)
                return

        logger.warning("No se encontró #lnkLogout. Revisá manualmente que la sesión se haya cerrado.")
    except Exception as e:
        logger.warning("Error al intentar logout: %s. Revisá que la sesión no haya quedado abierta.", e)


# ---------------------------------------------------------------------------
# Captura de pantalla
# ---------------------------------------------------------------------------
def capturar_pantalla(page: Page, nombre: str) -> Path:
    """Saca screenshot y lo guarda en logs/."""
    ruta = LOG_DIR / f"{nombre}_{timestamp}.png"
    page.screenshot(path=str(ruta), full_page=True)
    logger.info("Screenshot guardado: %s", ruta)
    return ruta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Punto de entrada principal."""
    logger.info("=" * 60)
    logger.info("Fase 0 — Validación de Playwright contra GBP (ERP web)")
    logger.info("=" * 60)

    # Validar variables de entorno
    erp_url = os.getenv("ERP_URL")
    erp_user = os.getenv("ERP_USER")
    erp_password = os.getenv("ERP_PASSWORD")

    faltantes = []
    if not erp_url:
        faltantes.append("ERP_URL")
    if not erp_user:
        faltantes.append("ERP_USER")
    if not erp_password:
        faltantes.append("ERP_PASSWORD")

    if faltantes:
        logger.error(
            "Faltan variables de entorno: %s\n"
            "Crealas en un archivo .env en la raíz del proyecto.\n"
            "Ver .env.example como referencia.",
            ", ".join(faltantes),
        )
        sys.exit(1)

    config = cargar_config()
    gbp_cfg = config.get("gbp", {})
    timeout: int = gbp_cfg.get("timeout", 30)
    headless: bool = gbp_cfg.get("headless", False)
    perfil: str = gbp_cfg.get("perfil", "04-Administrativos")

    with sync_playwright() as pw:
        logger.info("Lanzando navegador (headless=%s)...", headless)
        browser = pw.chromium.launch(headless=headless, args=["--start-maximized"])
        context = browser.new_context(no_viewport=True)
        page = context.new_page()

        try:
            # --- Login + selección de perfil ---
            pagina_erp = login_erp(page, erp_url, erp_user, erp_password, timeout,
                                   perfil_requerido=perfil)

            # --- Screenshot post-login ---
            capturar_pantalla(pagina_erp, "post_login")

            # --- Explorar menú ---
            pagina_erp.wait_for_timeout(3000)  # dar tiempo a que cargue el menú
            nodos = explorar_menu(pagina_erp, timeout)

            if not nodos:
                logger.warning("No se encontraron nodos del menú. Revisá el screenshot en logs/.")
            else:
                logger.info("Se encontraron %d elementos en el menú.", len(nodos))

                # Guardar dump del menú
                dump_file = LOG_DIR / f"menu_treeview_{timestamp}.txt"
                with open(dump_file, "w", encoding="utf-8") as f:
                    f.write("MENÚ COMPLETO — GBP ERP\n")
                    f.write("=" * 80 + "\n\n")
                    f.write(f"{'ID':<30} {'TIPO':<6} {'TAG':<5} TEXTO\n")
                    f.write("-" * 80 + "\n")
                    for nodo in nodos:
                        indent = "  " * nodo.get("nivel", 0)
                        marca = "[+]" if nodo["tipo"] == "nodo" else " > " if nodo["tipo"] == "hoja" else "   "
                        f.write(
                            f"{nodo.get('id', ''):<30} {nodo['tipo']:<6} {nodo.get('tag', ''):<5} "
                            f"{indent}{marca} {nodo['texto']}\n"
                        )
                        if nodo.get("href"):
                            f.write(f"{'':>30}        {indent}     href: {nodo['href']}\n")
                logger.info("Dump del menú guardado en: %s", dump_file)

                # Mostrar resumen en consola
                logger.info("")
                logger.info("MENÚ COMPLETO (resumen):")
                logger.info("-" * 60)
                for nodo in nodos:
                    indent = "  " * nodo.get("nivel", 0)
                    marca = "[+]" if nodo["tipo"] == "nodo" else " > " if nodo["tipo"] == "hoja" else "   "
                    id_str = f"  ({nodo['id']})" if nodo.get("id") else ""
                    logger.info("  %s%s %s%s", indent, marca, nodo["texto"], id_str)

            # --- Screenshot del menú expandido ---
            capturar_pantalla(pagina_erp, "menu_expandido")

        except PWTimeout as e:
            logger.error("Timeout durante la operación: %s", e)
            capturar_pantalla(page, "error_timeout")
        except Exception as e:
            logger.error("Error inesperado: %s", e, exc_info=True)
            capturar_pantalla(page, "error")
        finally:
            # IMPORTANTE: siempre cerrar sesión con "Salir" antes de cerrar el browser
            try:
                pagina_activa = locals().get('pagina_erp', page)
                logout_erp(pagina_activa, timeout)
            except Exception as e:
                logger.warning("No se pudo hacer logout: %s", e)
            browser.close()
            logger.info("Navegador cerrado.")

    # --- Resumen final ---
    logger.info("")
    logger.info("=" * 60)
    logger.info("RESUMEN")
    logger.info("=" * 60)
    logger.info("Log completo: %s", log_file)
    logger.info("")
    logger.info("PRÓXIMO PASO:")
    logger.info("  1. Revisá el dump del menú en logs/ y buscá la sección de facturación.")
    logger.info("  2. Decime los IDs de los nodos del TreeView que hay que expandir/clickear")
    logger.info("     para llegar al formulario de facturación.")
    logger.info("  3. Con eso armo la navegación automática hasta el formulario.")


if __name__ == "__main__":
    main()
