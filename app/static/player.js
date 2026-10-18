/* Player + selector de servidores. hls.js se carga desde CDN en base.html. */

(function () {
  "use strict";

  const video = document.getElementById("player");
  const statusEl = document.getElementById("status");
  if (!video) return;

  let hls = null;

  function setStatus(msg, isError) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.className = "status" + (isError ? " err" : "");
  }

  function play(url) {
    if (hls) {
      hls.destroy();
      hls = null;
    }
    if (window.Hls && window.Hls.isSupported()) {
      hls = new window.Hls({
        maxBufferLength: 30,
        enableWorker: true,
      });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, function () {
        setStatus("");
        video.play().catch(function () {});
      });
      hls.on(window.Hls.Events.ERROR, function (_evt, data) {
        if (!data || !data.fatal) return;
        switch (data.type) {
          case window.Hls.ErrorTypes.NETWORK_ERROR:
            setStatus("Error de red, reintentando…", true);
            hls.startLoad();
            break;
          case window.Hls.ErrorTypes.MEDIA_ERROR:
            setStatus("Error de media, recuperando…", true);
            hls.recoverMediaError();
            break;
          default:
            setStatus("No se pudo reproducir este servidor.", true);
            hls.destroy();
            hls = null;
        }
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
      video.play().catch(function () {});
    } else {
      setStatus("Tu navegador no soporta HLS.", true);
    }
  }

  async function loadStream(id, btn) {
    document.querySelectorAll(".srv").forEach(function (b) {
      b.classList.remove("active");
    });
    if (btn) btn.classList.add("active");
    setStatus("Cargando…");

    try {
      const res = await fetch("/api/stream/" + id, { headers: { Accept: "application/json" } });
      const body = await res.json().catch(function () { return {}; });
      if (!res.ok) {
        setStatus(body.detail || "Servidor no disponible.", true);
        return;
      }
      play(body.url);
    } catch (err) {
      setStatus("No se pudo contactar con el servidor.", true);
    }
  }

  document.querySelectorAll(".srv").forEach(function (btn) {
    btn.addEventListener("click", function () {
      loadStream(btn.dataset.id, btn);
    });
  });
})();
