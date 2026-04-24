"""
Cliente de automatización para el ERP GBP (Global Blue Point).

Encapsula toda la interacción con el navegador: login, navegación
al módulo de pedidos, aplicación de filtros y procesamiento automático.
"""

import logging
from typing import Any

from playwright.sync_api import (
    sync_playwright,
    Playwright,
    Browser,
    BrowserContext,
    Page,
    Frame,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger("facturacion")


# ---------------------------------------------------------------------------
# Helpers (adaptados de validacion_gbp.py)
# ---------------------------------------------------------------------------

def _aceptar_dialogo(dialog) -> None:
    """Acepta cualquier alert/confirm/prompt del navegador."""
    logger.info("Diálogo detectado: '%s' — aceptando.", dialog.message)
    dialog.accept()


def _seleccionar_perfil(page: Page, perfil_requerido: str, timeout_ms: int) -> None:
    """Verifica el perfil activo y lo cambia si es necesario."""
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

    logger.info("Cambiando perfil a '%s'...", perfil_requerido)
    selector_perfil.select_option(label=perfil_requerido)
    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    page.wait_for_timeout(2000)

    perfil_nuevo = selector_perfil.evaluate(
        "el => el.options[el.selectedIndex].text"
    )
    logger.info("Perfil cambiado a: '%s'", perfil_nuevo)
    if perfil_requerido not in perfil_nuevo:
        logger.warning(
            "El perfil no se cambió correctamente. Actual: '%s'", perfil_nuevo
        )


# ---------------------------------------------------------------------------
# GBPClient
# ---------------------------------------------------------------------------

class GBPClient:
    """Cliente para automatizar operaciones en el ERP GBP."""

    def __init__(self, config: dict) -> None:
        """Inicializa el cliente con la configuración del proyecto.

        Parameters
        ----------
        config : dict
            Diccionario completo cargado desde config.yaml.
            Debe contener las claves ``gbp.*`` y las credenciales
            se pasan por separado en :meth:`conectar`.
        """
        gbp: dict[str, Any] = config.get("gbp", {})
        self._timeout: int = gbp.get("timeout", 30)
        self._timeout_largo: int = gbp.get("timeout_largo", 300)
        self._headless: bool = gbp.get("headless", False)
        self._perfil: str = gbp.get("perfil", "04-Administrativos")

        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None  # página principal (popup del ERP)

    # -- Propiedades de conveniencia ------------------------------------

    @property
    def _timeout_ms(self) -> int:
        return self._timeout * 1000

    @property
    def _timeout_largo_ms(self) -> int:
        return self._timeout_largo * 1000

    # -- Ciclo de vida --------------------------------------------------

    def conectar(self, url: str, usuario: str, password: str) -> None:
        """Lanza el navegador, hace login y selecciona el perfil.

        Reproduce el flujo completo de ``login_erp()`` de validacion_gbp.py.
        """
        logger.info("Lanzando navegador (headless=%s)...", self._headless)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self._headless,
            args=["--start-maximized"],
        )
        self._context = self._browser.new_context(no_viewport=True)
        page = self._context.new_page()
        page.on("dialog", _aceptar_dialogo)

        # Paso 1: navegar → se abre popup automático
        logger.info("Navegando a %s...", url)
        with self._context.expect_page(timeout=self._timeout_ms) as popup_info:
            page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        popup = popup_info.value
        popup.on("dialog", _aceptar_dialogo)
        popup.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
        logger.info("Popup abierto: %s", popup.url)

        # Paso 2: click en "Ingresar"
        logger.info("Esperando botón 'Ingresar' (#lnkLogin)...")
        popup.locator("#lnkLogin").wait_for(state="visible", timeout=self._timeout_ms)
        popup.locator("#lnkLogin").click()
        popup.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)

        # Paso 3: credenciales
        logger.info("Ingresando credenciales...")
        campo_usuario = popup.locator("#UserName")
        campo_usuario.wait_for(state="visible", timeout=self._timeout_ms)
        campo_usuario.fill(usuario)

        campo_password = popup.locator("#Password")
        campo_password.wait_for(state="visible", timeout=self._timeout_ms)
        # press_sequentially porque el campo tiene onkeyup JS handler
        campo_password.press_sequentially(password, delay=50)

        # Paso 4: click en botón de login
        boton_login = popup.locator("#wucIB_Login_LB")
        if boton_login.count() == 0:
            boton_login = popup.get_by_role("link", name="LogIn")
        if boton_login.count() == 0:
            boton_login = popup.get_by_role("link", name="Entrar")
        logger.info("Clickeando botón de login...")
        boton_login.click(timeout=self._timeout_ms)

        # Verificar error de credenciales
        popup.wait_for_timeout(2000)
        if popup.locator("text=not valid").count() > 0:
            raise RuntimeError(
                "Login fallido: usuario o contraseña incorrectos."
            )

        # Paso 5: esperar wfmMain
        logger.info("Esperando carga de wfmMain...")
        popup.wait_for_url("**/wfmMain**", timeout=self._timeout_ms)
        logger.info("Login exitoso. URL: %s", popup.url)

        # Paso 6: perfil
        _seleccionar_perfil(popup, self._perfil, self._timeout_ms)

        self._page = popup
        logger.info("Conexión establecida correctamente.")

    def desconectar(self) -> None:
        """Cierra sesión (logout) y luego cierra el navegador.

        SIEMPRE llamar a este método, incluso ante errores.
        Si no se hace logout, el ERP queda bloqueado ~5 minutos.
        """
        # Logout
        if self._page is not None:
            try:
                logger.info("Cerrando sesión del ERP (lnkLogout)...")
                for frame in [self._page] + self._page.frames:
                    salir = frame.locator("#lnkLogout")
                    if salir.count() > 0:
                        salir.click(timeout=self._timeout_ms)
                        logger.info("Logout exitoso.")
                        self._page.wait_for_timeout(2000)
                        break
                else:
                    logger.warning(
                        "No se encontró #lnkLogout. "
                        "Revisá que la sesión se haya cerrado."
                    )
            except Exception as e:
                logger.warning("Error al intentar logout: %s", e)

        # Cerrar navegador
        if self._browser is not None:
            try:
                self._browser.close()
                logger.info("Navegador cerrado.")
            except Exception as e:
                logger.warning("Error al cerrar navegador: %s", e)

        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass

        self._page = None
        self._browser = None
        self._context = None
        self._pw = None

    # -- Navegación al módulo de pedidos --------------------------------

    def navegar_a_pedidos(self) -> None:
        """Navega a 'Procesar y Modificar Pedidos' en el menú lateral.

        Usa las funciones JS del TreeView de ASP.NET para seleccionar
        el nodo correctamente (un click directo congela la página).
        Luego espera a que ``frameWorkingArea`` cargue wfmSaleOrderAll.aspx.
        """
        assert self._page is not None, "Llamar a conectar() primero."

        logger.info("Navegando a 'Procesar y Modificar Pedidos'...")

        # Esperar a que el menú cargue
        self._page.wait_for_timeout(2000)

        frame_menu: Frame | None = self._page.frame("frameMenu")
        if frame_menu is None:
            raise RuntimeError("No se encontró frameMenu.")

        frame_menu.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)

        # Usar TreeView_SelectNode de ASP.NET para activar el nodo.
        # Esto dispara la navegación en frameWorkingArea sin congelar.
        logger.info("Activando nodo TreeView1t173 vía JS...")
        frame_menu.evaluate("""
        () => {
            var el = document.getElementById('TreeView1t173');
            if (!el) throw new Error('No se encontró TreeView1t173');

            // Seleccionar el nodo en el TreeView
            if (typeof TreeView_SelectNode === 'function') {
                TreeView_SelectNode(TreeView1_Data, el, 'TreeView1t173');
            }

            // Navegar al href del nodo en frameWorkingArea
            var href = el.getAttribute('href');
            if (href && href.indexOf('__doPostBack') >= 0) {
                // Ejecutar el postback directamente
                eval(href.replace('javascript:', ''));
            } else if (href) {
                var target = el.getAttribute('target') || 'frameWorkingArea';
                var f = window.parent.frames[target] || window.parent.frames['frameWorkingArea'];
                if (f) f.location.href = href;
            }
        }
        """)

        # Esperar a que frameWorkingArea cargue la página de pedidos
        logger.info("Esperando carga de wfmSaleOrderAll.aspx...")
        self._page.wait_for_timeout(2000)

        frame_work: Frame | None = None
        intentos = 0
        max_intentos = 15
        while intentos < max_intentos:
            frame_work = self._page.frame("frameWorkingArea")
            if frame_work is not None and "wfmSaleOrderAll" in (frame_work.url or ""):
                break
            self._page.wait_for_timeout(1000)
            intentos += 1

        if frame_work is None or "wfmSaleOrderAll" not in (frame_work.url or ""):
            raise RuntimeError(
                "frameWorkingArea no cargó wfmSaleOrderAll.aspx "
                f"después de {max_intentos} intentos."
            )

        frame_work.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
        logger.info("Página de pedidos cargada correctamente.")

    # -- Aplicar filtros ------------------------------------------------

    def aplicar_filtros(self, filtros: dict[str, str]) -> None:
        """Aplica los filtros en frameWorkingArea y ejecuta la búsqueda.

        Parameters
        ----------
        filtros : dict
            Claves: tipo_pedido, percepciones, vendedor, transporte.
        """
        assert self._page is not None, "Llamar a conectar() primero."

        frame_work: Frame | None = self._page.frame("frameWorkingArea")
        if frame_work is None:
            raise RuntimeError("No se encontró frameWorkingArea.")

        logger.info("Aplicando filtros en la grilla de pedidos...")

        # Tipo de pedido → "OK PARA EMISIÓN" (value=50)
        # IMPORTANTE: este dropdown tiene onchange con __doPostBack que recarga la página.
        # Hay que esperar a que termine el postback antes de tocar los demás filtros.
        tipo = filtros.get("tipo_pedido", "50")
        frame_work.locator("#ddlTypeSalesOrder").select_option(value=tipo)
        logger.info("Tipo de pedido: %s — esperando recarga por postback...", tipo)
        self._page.wait_for_timeout(3000)

        # Re-obtener el frame porque el postback lo recargó
        frame_work = self._page.frame("frameWorkingArea")
        if frame_work is None:
            raise RuntimeError("Se perdió frameWorkingArea después del postback.")
        frame_work.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)

        # Checkbox "Desde Fecha de Entrega" — DESPUÉS del postback para que no se resetee.
        # Usar .click() en vez de .check() para disparar el onclick="CheckedChanged(...)".
        chk_fecha = frame_work.locator("#dcFromDate_CHK")
        if chk_fecha.count() > 0:
            if not chk_fecha.is_checked():
                chk_fecha.click()
                logger.info("Checkbox de fecha desde activado.")
                self._page.wait_for_timeout(1000)

        # Percepciones → "No" (value=0)
        percepciones = filtros.get("percepciones", "0")
        frame_work.locator("#ddlHasPerceptions").select_option(value=percepciones)
        logger.info("Percepciones: %s", percepciones)
        self._page.wait_for_timeout(500)

        # Vendedor → "Ventas Web" (value=9)
        vendedor = filtros.get("vendedor", "9")
        frame_work.locator("#ddlSalesman").select_option(value=vendedor)
        logger.info("Vendedor: %s", vendedor)
        self._page.wait_for_timeout(500)

        # Transporte → "EnvioPack" (value=77)
        transporte = filtros.get("transporte", "77")
        frame_work.locator("#ddlDelivery").select_option(value=transporte)
        logger.info("Transporte: %s", transporte)
        self._page.wait_for_timeout(500)

        # Click en Buscar
        logger.info("Clickeando botón Buscar (#WucIB_Search_LB)...")
        frame_work.locator("#WucIB_Search_LB").click()

        # Esperar a que la grilla se recargue con los resultados filtrados.
        # La grilla vieja puede seguir visible, hay que esperar a que el
        # postback la refresque realmente.
        logger.info("Esperando resultados...")
        self._page.wait_for_timeout(5000)

        # Re-obtener frame después del postback de búsqueda
        frame_work = self._page.frame("frameWorkingArea")
        if frame_work is None:
            raise RuntimeError("Se perdió frameWorkingArea después de Buscar.")
        frame_work.wait_for_load_state("domcontentloaded", timeout=self._timeout_largo_ms)
        logger.info("Grilla de pedidos cargada.")

    # -- Procesar pedidos -----------------------------------------------

    def procesar_pedidos(self) -> int:
        """Procesa todos los pedidos visibles clickeando cada botón verde.

        Cada botón abre un popup vía ``jsfnShowNoModalDialog()``
        que muestra "Cargando, espere..." y luego se cierra solo.

        Returns
        -------
        int
            Cantidad de pedidos procesados.
        """
        assert self._page is not None, "Llamar a conectar() primero."
        assert self._context is not None

        frame_work: Frame | None = self._page.frame("frameWorkingArea")
        if frame_work is None:
            raise RuntimeError("No se encontró frameWorkingArea.")

        # Selector de los botones verdes de procesamiento automático
        selector_botones = (
            '#grdData input[type="image"]'
            '[title="Procesar un Pedido Automaticamente"]'
        )

        botones = frame_work.locator(selector_botones)
        total = botones.count()

        if total == 0:
            logger.info("No se encontraron pedidos para procesar.")
            return 0

        logger.info("Se encontraron %d pedidos para procesar.", total)
        procesados = 0

        for indice in range(total):
            # Re-obtener el frame cada iteración (la página puede recargarse)
            frame_work = self._page.frame("frameWorkingArea")
            if frame_work is None:
                logger.error("Se perdió frameWorkingArea durante el procesamiento.")
                break

            botones = frame_work.locator(selector_botones)
            cantidad_actual = botones.count()
            if indice >= cantidad_actual:
                logger.info("No hay más botones disponibles (índice %d, hay %d).", indice, cantidad_actual)
                break

            # Tomar el botón por índice — cada fila tiene su propio botón verde
            boton = botones.nth(indice)

            # Obtener el nombre del cliente para el log
            nombre_cliente = self._obtener_nombre_cliente(frame_work, boton)
            logger.info(
                "Procesando pedido %d/%d — Cliente: %s",
                procesados + 1, total, nombre_cliente,
            )

            # Click en el botón → abre popup
            try:
                with self._context.expect_page(
                    timeout=self._timeout_largo_ms
                ) as popup_info:
                    boton.click()

                popup_page = popup_info.value
                popup_page.on("dialog", _aceptar_dialogo)
                logger.info("Popup abierto para procesamiento.")

                # Esperar a que el popup se cierre (el ERP lo cierra automáticamente)
                popup_page.wait_for_event(
                    "close", timeout=self._timeout_largo_ms
                )
                logger.info("Popup cerrado — pedido procesado.")

            except PWTimeout:
                logger.warning(
                    "Timeout esperando popup para pedido %d. Continuando...",
                    procesados + 1,
                )
            except Exception as e:
                logger.warning(
                    "Error procesando pedido %d: %s. Continuando...",
                    procesados + 1, e,
                )

            procesados += 1

            # Esperar a que la página se estabilice antes del siguiente
            logger.info("Esperando estabilidad antes del siguiente pedido...")
            self._page.wait_for_timeout(3000)

            # Re-obtener frame y esperar que la grilla esté lista
            frame_work = self._page.frame("frameWorkingArea")
            if frame_work is not None:
                try:
                    frame_work.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
                    frame_work.locator("#grdData").wait_for(
                        state="visible", timeout=self._timeout_largo_ms
                    )
                except PWTimeout:
                    logger.warning("Timeout esperando recarga de grilla.")

        logger.info("Procesamiento completado: %d/%d pedidos.", procesados, total)
        return procesados

    # -- Helpers privados -----------------------------------------------

    @staticmethod
    def _obtener_nombre_cliente(frame: Frame, boton) -> str:
        """Intenta extraer el nombre del cliente de la fila del botón."""
        try:
            nombre = boton.evaluate("""
                (el) => {
                    const fila = el.closest('tr');
                    if (!fila) return 'Desconocido';
                    // El nombre suele estar en la segunda o tercera celda
                    const celdas = fila.querySelectorAll('td');
                    for (const celda of celdas) {
                        const texto = celda.textContent.trim();
                        // Buscar celda con texto largo que no sea número
                        if (texto.length > 3 && isNaN(texto.replace(/[.,]/g, ''))) {
                            return texto;
                        }
                    }
                    return 'Desconocido';
                }
            """)
            return nombre
        except Exception:
            return "Desconocido"
