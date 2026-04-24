(() => {
  const botones = document.querySelectorAll(".btn-logistica");
  const estadoEl = document.getElementById("estado-actual");
  const estadoWrap = document.querySelector(".estado");
  const logOut = document.getElementById("log-output");
  const btnLimpiar = document.getElementById("btn-limpiar");

  let eventSource = null;

  function setEstado(texto, clase = "") {
    estadoEl.textContent = texto;
    estadoWrap.classList.remove("ok", "error", "running");
    if (clase) estadoWrap.classList.add(clase);
  }

  function clasificarLog(linea) {
    if (/\b(ERROR|CRITICAL|FATAL|Traceback|Exception)\b/i.test(linea)) return "log-error";
    if (/\b(WARN|WARNING)\b/i.test(linea)) return "log-warn";
    if (/\b(DEBUG)\b/i.test(linea)) return "log-debug";
    if (/(✓|OK|Completado|exitos|success)/i.test(linea)) return "log-success";
    return "log-info";
  }

  function agregarLog(linea) {
    const texto = linea.replace(/\\n/g, "\n");
    texto.split("\n").forEach((sub) => {
      if (sub === "" && texto.length > 0) return;
      const span = document.createElement("span");
      span.className = "log-line " + clasificarLog(sub);
      span.textContent = sub + "\n";
      logOut.appendChild(span);
    });
    logOut.scrollTop = logOut.scrollHeight;
  }

  function setBotonesHabilitados(habilitados, corriendoId = null) {
    botones.forEach((btn) => {
      const id = btn.dataset.logistica;
      const estaDeshabilitadoDeOrigen = btn.hasAttribute("data-disabled-origen")
        ? true
        : (btn.disabled && !btn.classList.contains("corriendo"));
      // Primera vez: marcar los que ya vienen deshabilitados por el backend
      if (!btn.hasAttribute("data-disabled-origen") && btn.disabled) {
        btn.setAttribute("data-disabled-origen", "1");
      }

      if (btn.hasAttribute("data-disabled-origen")) {
        // Siempre deshabilitado (no implementado)
        btn.disabled = true;
        btn.classList.remove("corriendo");
        return;
      }

      btn.disabled = !habilitados;
      if (id === corriendoId) {
        btn.classList.add("corriendo");
      } else {
        btn.classList.remove("corriendo");
      }
    });
  }

  async function ejecutar(logisticaId) {
    setEstado(`Iniciando ${logisticaId}…`, "running");
    setBotonesHabilitados(false, logisticaId);

    let resp;
    try {
      resp = await fetch(`/run/${encodeURIComponent(logisticaId)}`, { method: "POST" });
    } catch (e) {
      setEstado(`Error de red: ${e}`, "error");
      setBotonesHabilitados(true);
      return;
    }

    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      setEstado(`No se pudo iniciar: ${data.error || resp.statusText}`, "error");
      setBotonesHabilitados(true);
      return;
    }

    const { job_id } = await resp.json();
    setEstado(`Ejecutando ${logisticaId} (job ${job_id})…`, "running");
    conectarStream(job_id, logisticaId);
  }

  function conectarStream(jobId, logisticaId) {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/stream/${encodeURIComponent(jobId)}`);

    eventSource.addEventListener("log", (ev) => {
      agregarLog(ev.data.replace(/\\n/g, "\n"));
    });

    eventSource.addEventListener("end", async () => {
      eventSource.close();
      eventSource = null;
      // Consultar estado final
      try {
        const r = await fetch("/status");
        const s = await r.json();
        if (s.status === "ok") {
          setEstado(
            `Completado: ${s.procesados ?? 0} pedidos procesados (${s.logistica}).`,
            "ok"
          );
        } else if (s.status === "error") {
          setEstado(`Finalizado con error: ${s.error || "desconocido"}`, "error");
        } else {
          setEstado("Finalizado.", "");
        }
      } catch {
        setEstado("Finalizado.", "");
      }
      setBotonesHabilitados(true);
    });

    eventSource.onerror = () => {
      // EventSource reintenta solo; si queda colgado mucho, el usuario verá el estado
    };
  }

  botones.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      logOut.textContent = "";
      ejecutar(btn.dataset.logistica);
    });
  });

  btnLimpiar.addEventListener("click", () => {
    logOut.textContent = "";
  });

  // Al cargar, verificar si ya hay un job corriendo (ej. refrescaron la página)
  (async () => {
    try {
      const r = await fetch("/status");
      const s = await r.json();
      if (s.running) {
        setEstado(`Ejecución en curso: ${s.logistica} (job ${s.id})`, "running");
        setBotonesHabilitados(false, s.logistica);
        conectarStream(s.id, s.logistica);
      }
    } catch {
      /* ignorar */
    }
  })();
})();
