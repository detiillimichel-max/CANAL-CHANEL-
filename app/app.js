const $ = (selector) => document.querySelector(selector);
const state = { country: "br", channels: [], filtered: [], category: "all", query: "", sort: "name", shown: 24, hls: null, installPrompt: null };
const countryNames = { br: "Brasil", pt: "Portugal", us: "Estados Unidos" };
const categoryNames = { agronegocio:"Agronegócio", desenhos:"Desenhos", educacao_cultura:"Educação e cultura", entretenimento:"Entretenimento", esportes:"Esportes", filmes:"Filmes", jornalismo:"Jornalismo", lifestyle:"Lifestyle", musica:"Música", outros:"Outros", regionais:"Regionais", religiao:"Religião", documentarios:"Documentários", compras:"Compras", politica:"Política", negocios:"Negócios", geral:"Geral" };
const safeText = (value) => String(value ?? "").replace(/[<>]/g, "");
function channelCategory(channel) {
  const key = channel.categories?.[0] || channel.classification?.primary || "outros";
  return categoryNames[key] || key.replaceAll("_", " ");
}
function confirmedStreams(channel) {
  return (channel.streams || []).filter((stream) => {
    const status = String(stream.availability_status || stream.validation?.result || "").toLowerCase();
    return status === "reachable_hls" || status === "reachable_non_hls" || status === "reachable";
  });
}
function setMessage(message) { $("#catalogMessage").textContent = message; }
function renderCategories() {
  const counts = new Map();
  state.channels.forEach((channel) => {
    const key = channel.categories?.[0] || channel.classification?.primary || "outros";
    counts.set(key, (counts.get(key) || 0) + 1);
  });
  const bar = $("#categoryBar");
  bar.innerHTML = "";
  const all = document.createElement("button");
  all.className = "category-chip" + (state.category === "all" ? " active" : "");
  all.dataset.category = "all"; all.type = "button"; all.textContent = "Todos";
  bar.append(all);
  [...counts.entries()].sort((a,b) => (categoryNames[a[0]] || a[0]).localeCompare(categoryNames[b[0]] || b[0], "pt-BR")).forEach(([key,count]) => {
    const button = document.createElement("button");
    button.className = "category-chip" + (state.category === key ? " active" : "");
    button.dataset.category = key; button.type = "button";
    button.textContent = (categoryNames[key] || key) + " · " + count;
    bar.append(button);
  });
  $("#categoryCount").textContent = counts.size;
}
function updateFiltered() {
  const q = state.query.trim().toLocaleLowerCase("pt-BR");
  state.filtered = state.channels.filter((channel) => {
    const category = channel.categories?.[0] || channel.classification?.primary || "outros";
    const haystack = [channel.name, channel.id, categoryNames[category], ...(channel.category_labels || [])].join(" ").toLocaleLowerCase("pt-BR");
    return (state.category === "all" || category === state.category) && (!q || haystack.includes(q));
  });
  state.filtered.sort((a,b) => {
    if (state.sort === "category") return channelCategory(a).localeCompare(channelCategory(b), "pt-BR") || (a.name || "").localeCompare(b.name || "", "pt-BR");
    return (a.name || "").localeCompare(b.name || "", "pt-BR");
  });
  state.shown = 24;
  renderChannels();
}
function renderChannels() {
  const grid = $("#channelGrid");
  grid.innerHTML = "";
  const visible = state.filtered.slice(0, state.shown);
  visible.forEach((channel) => {
    const card = document.createElement("article"); card.className = "channel-card";
    const logo = document.createElement("img"); logo.className = "channel-logo"; logo.alt = ""; logo.loading = "lazy";
    if (channel.logo) { logo.src = channel.logo; logo.referrerPolicy = "no-referrer"; logo.onerror = () => { const fallback = document.createElement("div"); fallback.className = "channel-logo channel-fallback"; fallback.textContent = (channel.name || "TV").slice(0,2).toUpperCase(); logo.replaceWith(fallback); }; }
    else { logo.className += " channel-fallback"; logo.textContent = (channel.name || "TV").slice(0,2).toUpperCase(); }
    const info = document.createElement("div"); info.className = "channel-info";
    const title = document.createElement("h3"); title.textContent = safeText(channel.name || "Canal sem nome");
    const meta = document.createElement("p"); meta.textContent = channelCategory(channel) + " · " + (countryNames[state.country] || state.country);
    info.append(title, meta);
    const streams = confirmedStreams(channel);
    const button = document.createElement("button"); button.className = "play-button"; button.type = "button";
    button.textContent = streams.length ? "Abrir" : "Indisponível"; button.disabled = !streams.length;
    button.setAttribute("aria-label", streams.length ? "Abrir " + (channel.name || "canal") : "Sem transmissão confirmada");
    button.addEventListener("click", () => openPlayer(channel, streams[0]));
    card.append(logo, info, button); grid.append(card);
  });
  setMessage(state.filtered.length ? "Exibindo " + visible.length + " de " + state.filtered.length + " canais. Os links são fontes externas e podem mudar." : "Nenhum canal encontrado com esse filtro.");
  $("#loadMoreBtn").hidden = state.shown >= state.filtered.length;
  $("#loadMoreBtn").textContent = "Carregar mais canais";
}
async function loadCatalog(country) {
  state.country = country;
  $("#catalogStatus").textContent = "Carregando " + (countryNames[country] || country) + "…";
  $("#channelGrid").innerHTML = ""; setMessage("Buscando catálogo atualizado…");
  try {
    const response = await fetch("./catalogos/catalogo-" + country + ".json", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    if (!Array.isArray(data.channels)) throw new Error("Formato de catálogo inesperado");
    state.channels = data.channels;
    $("#channelCount").textContent = Number(data.summary?.catalogue_channels ?? state.channels.length).toLocaleString("pt-BR");
    $("#catalogStatus").textContent = "Atualizado · " + new Date(data.generated_at || Date.now()).toLocaleDateString("pt-BR");
    renderCategories(); updateFiltered();
  } catch (error) {
    state.channels = []; state.filtered = [];
    $("#channelCount").textContent = "—"; $("#categoryCount").textContent = "—";
    $("#catalogStatus").textContent = "Catálogo indisponível";
    setMessage("Não foi possível carregar o catálogo. Verifique a publicação do GitHub Pages e tente novamente. (" + error.message + ")");
  }
}
async function openPlayer(channel, stream) {
  const dialog = $("#playerDialog"), video = $("#videoPlayer");
  stopPlayer();
  $("#playerTitle").textContent = channel.name || "Canal";
  $("#playerMessage").textContent = "Fonte selecionada. A autorização de retransmissão não é verificada por este aplicativo.";
  $("#openStreamLink").href = stream.url; $("#openStreamLink").hidden = false;
  dialog.showModal();
  const url = stream.url || "";
  if (!/^https:\/\//i.test(url)) {
    $("#playerMessage").textContent = "Esta fonte não utiliza HTTPS. Para segurança, abra a fonte somente se confiar nela.";
    return;
  }
  try {
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = url;
    } else if (url.toLowerCase().includes(".m3u8")) {
      await loadHlsLibrary();
      if (!window.Hls || !window.Hls.isSupported()) throw new Error("HLS não é suportado neste navegador.");
      state.hls = new window.Hls({ enableWorker: true, lowLatencyMode: false });
      state.hls.loadSource(url); state.hls.attachMedia(video);
      state.hls.on(window.Hls.Events.ERROR, (_event, data) => { if (data.fatal) $("#playerMessage").textContent = "Falha na reprodução desta fonte. Tente abrir o link externamente."; });
    } else {
      video.src = url;
    }
    video.play().catch(() => { $("#playerMessage").textContent = "Toque em reproduzir no player. Alguns navegadores exigem início manual."; });
  } catch (error) {
    $("#playerMessage").textContent = error.message + " Você pode tentar abrir a fonte externamente.";
  }
}
function loadHlsLibrary() {
  if (window.Hls) return Promise.resolve();
  if (window.hlsLibraryPromise) return window.hlsLibraryPromise;
  window.hlsLibraryPromise = new Promise((resolve,reject) => {
    const script = document.createElement("script");
    script.src = "https://cdn.jsdelivr.net/npm/hls.js@1.6.15/dist/hls.min.js";
    script.onload = resolve; script.onerror = () => reject(new Error("Não foi possível carregar o suporte HLS."));
    document.head.append(script);
  });
  return window.hlsLibraryPromise;
}
function stopPlayer() {
  const video = $("#videoPlayer");
  if (state.hls) { state.hls.destroy(); state.hls = null; }
  video.pause(); video.removeAttribute("src"); video.load();
}
$("#categoryBar").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-category]");
  if (!button) return;
  state.category = button.dataset.category;
  renderCategories(); updateFiltered();
});
$("#searchInput").addEventListener("input", (event) => { state.query = event.target.value; updateFiltered(); });
$("#sortSelect").addEventListener("change", (event) => { state.sort = event.target.value; updateFiltered(); });
$("#countrySelect").addEventListener("change", (event) => loadCatalog(event.target.value));
$("#loadMoreBtn").addEventListener("click", () => { state.shown += 24; renderChannels(); });
$("#closePlayer").addEventListener("click", () => $("#playerDialog").close());
$("#playerDialog").addEventListener("close", stopPlayer);
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault(); state.installPrompt = event; $("#installBtn").hidden = false;
});
$("#installBtn").addEventListener("click", async () => {
  if (!state.installPrompt) return;
  state.installPrompt.prompt(); await state.installPrompt.userChoice;
  state.installPrompt = null; $("#installBtn").hidden = true;
});
if ("serviceWorker" in navigator && location.protocol.startsWith("http")) {
  window.addEventListener("load", () => navigator.serviceWorker.register("./sw.js").catch((error) => console.warn("Service worker:", error)));
}
loadCatalog(state.country);