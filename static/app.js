const API = '';
const app = document.getElementById('app');
const status = document.getElementById('status');
let buscaTimer = null;

function setStatus(t) { status.textContent = t; }

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

async function listaAnimes(filtro = '') {
  app.innerHTML = '<div class="loading">carregando...</div>';
  try {
    const d = await getJSON(`${API}/animes?limit=200`);
    let animes = d.animes;
    if (filtro) {
      const f = filtro.toLowerCase();
      animes = animes.filter(a => (a.titulo || '').toLowerCase().includes(f));
    }
    if (!animes.length) {
      app.innerHTML = '<div class="loading">nenhum anime</div>';
      return;
    }
    app.innerHTML = `<div class="grid">${animes.map(a => `
      <div class="card" onclick="verAnime('${a.id}')">
        <img src="${a.capa || ''}" loading="lazy" onerror="this.style.visibility='hidden'">
        <div class="info">
          <div class="titulo">${a.titulo || a.id}</div>
          <div class="meta">${a.episodes_count || 0} eps ${a.ano ? '· ' + a.ano : ''}</div>
        </div>
      </div>
    `).join('')}</div>`;
    setStatus(`${animes.length} animes`);
  } catch (e) {
    app.innerHTML = `<div class="erro">erro: ${e.message}</div>`;
  }
}

async function verAnime(id) {
  app.innerHTML = '<div class="loading">carregando...</div>';
  try {
    const [a, e] = await Promise.all([
      getJSON(`${API}/animes/${id}`),
      getJSON(`${API}/animes/${id}/episodes`)
    ]);
    app.innerHTML = `
      <a href="#" class="voltar" onclick="voltar(event)">← voltar</a>
      <div class="detalhe">
        <img src="${a.capa || ''}" onerror="this.style.visibility='hidden'">
        <div>
          <h2>${a.titulo || a.id}</h2>
          ${a.ano ? `<div class="info-linha">ano: ${a.ano}</div>` : ''}
          ${a.nota ? `<div class="info-linha">nota: ${a.nota}</div>` : ''}
          ${a.status ? `<div class="info-linha">status: ${a.status}</div>` : ''}
          ${a.generos?.length ? `<div class="generos">${a.generos.map(g => `<span class="genero">${g}</span>`).join('')}</div>` : ''}
          ${a.sinopse ? `<div class="sinopse">${a.sinopse}</div>` : ''}
          <div class="eps">
            ${e.episodes.map(ep => `
              <div class="ep" data-anime="${id}" data-ep="${ep.id}" onclick="tocar(this)">
                ep ${ep.numero ?? '?'}
              </div>
            `).join('')}
          </div>
          <div class="player-box" id="player-box">
            <video id="player" controls playsinline></video>
          </div>
          <div class="player-actions" id="player-actions"></div>
          <div id="player-erro"></div>
        </div>
      </div>`;
    setStatus(`${e.count} eps`);
  } catch (e) {
    app.innerHTML = `<div class="erro">erro: ${e.message}</div>`;
  }
}

async function tocar(el) {
  const anime = el.dataset.anime;
  const ep = el.dataset.ep;
  const box = document.getElementById('player-box');
  const video = document.getElementById('player');
  const actions = document.getElementById('player-actions');
  const erroBox = document.getElementById('player-erro');
  erroBox.innerHTML = '';
  el.classList.add('carregando');
  setStatus(`resolvendo ${ep}...`);
  try {
    const r = await getJSON(`${API}/resolve/${anime}/${ep}`);
    box.classList.add('on');
    video.src = r.url;
    video.play().catch(() => {});
    actions.innerHTML = `
      <a href="${r.url}" target="_blank" download>baixar mp4</a>
      <a href="${API}/stream/${anime}/${ep}" target="_blank">abrir stream</a>
      <span style="color:#666">${r.cached ? 'cache' : 'novo'} · expira em ${Math.round((r.expire - Date.now()/1000)/60)}min</span>`;
    setStatus(`${ep} ok`);
  } catch (e) {
    el.classList.add('dead');
    erroBox.innerHTML = `<div class="erro">falha: ${e.message}</div>`;
    setStatus(`${ep} falhou`);
  } finally {
    el.classList.remove('carregando');
  }
}

function voltar(ev) {
  ev.preventDefault();
  listaAnimes(document.getElementById('busca').value);
}

document.getElementById('busca').addEventListener('input', (e) => {
  clearTimeout(buscaTimer);
  buscaTimer = setTimeout(() => listaAnimes(e.target.value), 250);
});

listaAnimes();
