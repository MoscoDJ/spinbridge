// SpinBridge UI: habla con el engine por WebSocket (estado 2 veces/s, vistas previas en binario).
const PORT = (window.spinbridge && window.spinbridge.port) || 8765;
const SLOT_COLORS = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6', '--s7', '--s8'];
const $ = (id) => document.getElementById(id);

let ws = null;
let state = null;
let reqId = 0;
const pending = new Map();
let slotsSignature = '';
let settingsLoaded = false;
const previewUrls = new Map(); // key -> objectURL

// ---------------------------------------------------------------- conexión

function connect() {
  ws = new WebSocket(`ws://127.0.0.1:${PORT}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    setEngine(true);
    send({ cmd: 'preview', on: true });
  };
  ws.onclose = () => {
    setEngine(false);
    setTimeout(connect, 1000);
  };
  ws.onerror = () => {};
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'state') render(msg);
      else if (msg.type === 'result') {
        const p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          msg.ok ? p.resolve() : p.reject(new Error(msg.error));
        }
      }
    } else {
      onPreview(new Uint8Array(ev.data));
    }
  };
}

function send(msg) {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return reject(new Error('engine desconectado'));
    const id = ++reqId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, ...msg }));
  });
}

function cmd(msg) {
  return send(msg).catch((e) => toast(e.message));
}

function setEngine(ok) {
  const el = $('engineState');
  el.textContent = ok ? 'conectado' : 'desconectado';
  el.className = ok ? 'ok' : 'bad';
  if (!ok) {
    $('statusPill').className = 'status-pill';
    $('statusText').textContent = 'SIN ENGINE';
  }
}

// ---------------------------------------------------------------- vistas previas

function setPreview(key, bytes, img) {
  const url = URL.createObjectURL(new Blob([bytes], { type: 'image/jpeg' }));
  const old = previewUrls.get(key);
  previewUrls.set(key, url);
  if (img) img.src = url;
  if (old) setTimeout(() => URL.revokeObjectURL(old), 1000);
}

function onPreview(data) {
  const kind = String.fromCharCode(data[0]);
  if (kind === 'C') {
    setPreview('composite', data.subarray(1), $('compositeImg'));
    $('compositeEmpty').hidden = true;
  } else if (kind === 'F') {
    const mac = new TextDecoder().decode(data.subarray(1, 18));
    const img = document.querySelector(`.fan-preview[data-mac="${mac}"] img`);
    setPreview(`fan:${mac}`, data.subarray(18), img);
    if (img) img.parentElement.querySelector('.pv-label').hidden = true;
  }
}

// ---------------------------------------------------------------- render

function render(s) {
  state = s;
  document.body.classList.toggle('live', s.running);
  const pill = $('statusPill');
  pill.className = 'status-pill ' + (s.running ? 'live' : 'stopped');
  $('statusText').textContent = s.running ? 'EN VIVO' : 'DETENIDO';
  $('btnRun').classList.toggle('active', s.running);
  $('btnStop').classList.toggle('active', !s.running);
  $('cpu').textContent = s.cpu == null ? '–' : `${Math.round(s.cpu)}%`;

  renderSource(s);
  renderSettings(s);
  renderSlots(s);
  renderDetected(s);
  renderLog(s);
}

function renderSource(s) {
  const src = s.source;
  const badge = $('sourceBadge');
  const labels = { ok: 'RECIBIENDO', negro: 'SEÑAL EN NEGRO', 'sin cuadros': 'SIN CUADROS', 'sin emisor': 'SIN EMISOR', conectando: 'CONECTANDO' };
  badge.textContent = labels[src.status] || src.status;
  badge.className = 'source-badge ' + (src.status === 'ok' ? 'ok' : 'bad');
  const messages = {
    negro: 'Arena está enviando negro: revisa que haya clips reproduciéndose en las capas de los ventiladores',
    'sin cuadros': 'El emisor existe pero no llegan cuadros: revisa que Resolume y SpinBridge usen la misma GPU',
  };
  if (src.status !== 'ok') $('compositeEmpty').hidden = false;
  $('compositeEmpty').textContent = src.status === 'ok' ? '' : (messages[src.status] || 'Sin señal de Spout');
  $('srcSize').textContent = src.size ? `${src.size[0]} × ${src.size[1]}` : '–';
  $('srcFps').textContent = src.fps || '–';
  $('srcGpu').textContent = src.gl_renderer || '–';
  $('srcGpu').title = src.gl_renderer || '';

  const sel = $('senderSelect');
  const options = ['', ...src.senders];
  if (src.wanted && !options.includes(src.wanted)) options.push(src.wanted);
  const sig = options.join('|');
  if (sel.dataset.sig !== sig) {
    sel.innerHTML = '';
    for (const o of options) {
      const opt = document.createElement('option');
      opt.value = o;
      opt.textContent = o === '' ? '(automático: el primero)' : (src.senders.includes(o) ? o : `${o} (no disponible)`);
      sel.appendChild(opt);
    }
    sel.dataset.sig = sig;
  }
  if (document.activeElement !== sel) sel.value = src.wanted || '';

  // rectángulos de slots sobre la vista previa
  const ov = $('slotOverlay');
  const size = src.size || [3360, 672];
  const sig2 = JSON.stringify([s.slots, size]);
  if (ov.dataset.sig !== sig2) {
    ov.innerHTML = '';
    s.slots.forEach((slot, i) => {
      const [x, y, w, h] = slot.rect;
      const d = document.createElement('div');
      d.className = 'rect';
      const color = `var(${SLOT_COLORS[(slot.id - 1) % SLOT_COLORS.length]})`;
      Object.assign(d.style, {
        left: `${(x / size[0]) * 100}%`, top: `${(y / size[1]) * 100}%`,
        width: `${(w / size[0]) * 100}%`, height: `${(h / size[1]) * 100}%`, borderColor: color, color,
      });
      d.innerHTML = `<span>${slot.id}</span>`;
      ov.appendChild(d);
    });
    ov.dataset.sig = sig2;
  }
}

function renderSettings(s) {
  $('perFanHint').textContent = `Garantizado: ${s.per_fan_mbps} Mbit/s por ventilador, más lo que no usen los demás`;
  if (settingsLoaded) return;
  const st = s.settings;
  $('setFps').value = st.fps;
  $('setQuality').value = st.quality;
  $('setMinQuality').value = st.min_quality;
  $('setMbps').value = st.target_mbps;
  $('setTotalMbps').value = st.total_mbps;
  $('setMask').checked = st.circle_mask;
  $('setOptimize').checked = st.jpeg_optimize;
  settingsLoaded = true;
}

function fanBySlot(s, id) {
  return s.fans.find((f) => f.slot === id);
}

function stateClass(f) {
  if (f.state === 'en vivo') return 'live';
  if (f.state === 'conectado' || f.state === 'identificando') return 'ok';
  if (f.state === 'conectando' || f.state === 'iniciando proyección') return 'warn';
  return 'bad';
}

function renderSlots(s) {
  const container = $('slots');
  const sig = JSON.stringify(s.slots.map((sl) => [sl.id, (fanBySlot(s, sl.id) || {}).mac]));
  if (sig !== slotsSignature) {
    slotsSignature = sig;
    container.innerHTML = '';
    for (const slot of s.slots) container.appendChild(buildSlot(slot, fanBySlot(s, slot.id)));
  }
  for (const slot of s.slots) {
    const f = fanBySlot(s, slot.id);
    if (f) updateSlot(container.querySelector(`.slot[data-slot="${slot.id}"]`), f, s);
  }
}

function buildSlot(slot, fan) {
  const el = document.createElement('div');
  el.className = 'slot';
  el.dataset.slot = slot.id;
  el.style.setProperty('--slot-color', `var(${SLOT_COLORS[(slot.id - 1) % SLOT_COLORS.length]})`);
  el.innerHTML = `<div class="slot-head"><span class="slot-num">SLOT ${slot.id}</span>
    <span class="hint">x ${slot.rect[0]}</span></div>`;
  if (!fan) {
    el.insertAdjacentHTML('beforeend', '<div class="slot-empty">Arrastra un ventilador aquí</div>');
  } else {
    el.insertAdjacentHTML('beforeend', `
      <div class="fan-chip" draggable="true" data-mac="${fan.mac}">
        <span class="grip">⠿</span><span class="state-dot"></span>
        <div><div class="fan-name"></div><div class="fan-sub"></div></div>
      </div>
      <div class="fan-state"></div>
      <div class="fan-preview" data-mac="${fan.mac}"><img alt=""><div class="pv-label">sin imagen</div></div>
      <div class="metrics">
        <div class="metric"><b data-k="fps">–</b><small>fps</small></div>
        <div class="metric"><b data-k="mbps">–</b><small>Mbit/s</small></div>
        <div class="metric"><b data-k="quality">–</b><small>calidad</small></div>
        <div class="metric"><b data-k="kb">–</b><small>KB/cuadro</small></div>
        <div class="metric"><b data-k="rtt">–</b><small>latencia</small></div>
        <div class="metric"><b data-k="drops">–</b><small>descartes/s</small></div>
      </div>
      <div class="fan-actions">
        <button class="btn-small" data-act="identify">Identificar</button>
        <button class="btn-small" data-act="stream">Stream</button>
        <button class="btn-small" data-act="power-on">Motor ON</button>
        <button class="btn-small danger" data-act="power-off">OFF</button>
        <div class="bright">
          <button class="btn-small" data-act="bright-down">−</button>
          <span data-k="bright">–</span>
          <button class="btn-small" data-act="bright-up">+</button>
        </div>
      </div>`);
    const img = el.querySelector('.fan-preview img');
    const url = previewUrls.get(`fan:${fan.mac}`);
    if (url) { img.src = url; el.querySelector('.pv-label').hidden = true; }
    el.querySelector('.fan-actions').addEventListener('click', (e) => {
      const act = e.target.dataset.act;
      if (act) fanAction(fan.mac, act);
    });
    bindDrag(el.querySelector('.fan-chip'));
  }
  bindDrop(el, slot.id);
  return el;
}

function updateSlot(el, f) {
  if (!el) return;
  el.querySelector('.fan-name').textContent = f.name;
  el.querySelector('.fan-sub').textContent = `${f.ip || 'sin IP'} · ${f.mac}`;
  el.querySelector('.state-dot').className = 'state-dot ' + stateClass(f);
  const st = el.querySelector('.fan-state');
  st.textContent = f.state + (f.error && f.state !== 'en vivo' ? ` — ${f.error}` : '');
  st.className = 'fan-state ' + (stateClass(f) === 'bad' ? 'bad' : '');
  const set = (k, v) => { const b = el.querySelector(`[data-k="${k}"]`); if (b) b.textContent = v; };
  el.querySelector('.fan-preview').classList.toggle('stale', !f.projecting);
  set('fps', f.projecting ? f.fps : '–');
  set('mbps', f.projecting ? f.mbps : '–');
  set('quality', f.projecting ? f.quality : '–');
  set('kb', f.projecting ? f.kb : '–');
  set('rtt', f.rtt_ms == null ? '–' : `${f.rtt_ms} ms`);
  set('drops', f.projecting ? f.drop_rate : '–');
  set('bright', f.brightness == null ? '–' : `${50 + 10 * f.brightness}%`);
  const sb = el.querySelector('[data-act="stream"]');
  sb.textContent = f.want_stream ? 'Stream ✓' : 'Stream ✗';
  sb.classList.toggle('on', f.want_stream);
  el.querySelectorAll('[data-act^="power"], [data-act^="bright"]').forEach((b) => { b.disabled = !f.connected; });
}

function renderDetected(s) {
  const box = $('detected');
  const loose = [
    ...s.fans.filter((f) => f.slot == null).map((f) => ({ ...f, known: true })),
    ...s.detected.map((d) => ({ ...d, known: false })),
  ];
  const sig = JSON.stringify(loose.map((d) => [d.mac, d.ip, d.name, d.state]));
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = '';
    if (!loose.length) box.innerHTML = '<div class="empty">No hay ventiladores sin asignar</div>';
    for (const d of loose) {
      const chip = document.createElement('div');
      chip.className = 'fan-chip';
      chip.draggable = true;
      chip.dataset.mac = d.mac;
      const busy = !d.known && d.connect_status !== 255 ? ' · ocupado por otra app' : '';
      chip.innerHTML = `<span class="grip">⠿</span><span class="state-dot ${d.known ? stateClass(d) : 'ok'}"></span>
        <div><div class="fan-name">${escapeHtml(d.name)}</div>
        <div class="fan-sub">${d.ip || ''} · ${d.mac}${busy}</div></div>`;
      if (d.known) {
        const btn = document.createElement('button');
        btn.className = 'btn-small';
        btn.textContent = 'Identificar';
        btn.onclick = () => cmd({ cmd: 'identify', mac: d.mac });
        chip.appendChild(btn);
      }
      bindDrag(chip);
      box.appendChild(chip);
    }
  }
  const err = $('discoveryError');
  err.hidden = !s.discovery_error;
  err.textContent = s.discovery_error || '';
}

function renderLog(s) {
  const pre = $('log');
  const text = s.logs.join('\n');
  if (pre.dataset.sig === text) return;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  pre.textContent = text;
  pre.dataset.sig = text;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function escapeHtml(t) {
  return String(t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// ---------------------------------------------------------------- arrastrar y soltar

function bindDrag(chip) {
  chip.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/mac', chip.dataset.mac);
    e.dataTransfer.effectAllowed = 'move';
  });
}

function bindDrop(el, slotId) {
  el.addEventListener('dragover', (e) => { e.preventDefault(); el.classList.add('drag-over'); });
  el.addEventListener('dragleave', () => el.classList.remove('drag-over'));
  el.addEventListener('drop', (e) => {
    e.preventDefault();
    el.classList.remove('drag-over');
    const mac = e.dataTransfer.getData('text/mac');
    if (mac) cmd({ cmd: 'assign', mac, slot: slotId });
  });
}

// ---------------------------------------------------------------- acciones

function confirmModal(title, text, okLabel = 'Confirmar') {
  return new Promise((resolve) => {
    $('modalTitle').textContent = title;
    $('modalText').textContent = text;
    $('modalOk').textContent = okLabel;
    $('modal').hidden = false;
    const done = (v) => {
      $('modal').hidden = true;
      $('modalOk').onclick = $('modalCancel').onclick = null;
      resolve(v);
    };
    $('modalOk').onclick = () => done(true);
    $('modalCancel').onclick = () => done(false);
  });
}

async function fanAction(mac, act) {
  const f = state && state.fans.find((x) => x.mac === mac);
  const name = f ? f.name : mac;
  if (act === 'identify') return cmd({ cmd: 'identify', mac });
  if (act === 'stream') return cmd({ cmd: 'fan_stream', mac, on: !(f && f.want_stream) });
  if (act === 'power-on') {
    if (await confirmModal(`Encender motor de ${name}`,
      'Las aspas empezarán a girar. Asegúrate de que nadie esté cerca del ventilador.', 'Encender')) {
      return cmd({ cmd: 'power', mac, on: true });
    }
  }
  if (act === 'power-off') return cmd({ cmd: 'power', mac, on: false });
  if (act === 'bright-up' || act === 'bright-down') {
    const cur = f && f.brightness != null ? f.brightness : 5;
    const level = Math.max(0, Math.min(5, cur + (act === 'bright-up' ? 1 : -1)));
    return cmd({ cmd: 'brightness', mac, level });
  }
}

async function allBrightness(delta) {
  if (!state) return;
  for (const f of state.fans.filter((x) => x.connected)) {
    const cur = f.brightness != null ? f.brightness : 5;
    await cmd({ cmd: 'brightness', mac: f.mac, level: Math.max(0, Math.min(5, cur + delta)) });
  }
}

function toast(text, kind = '') {
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.textContent = text;
  $('toasts').appendChild(t);
  setTimeout(() => t.remove(), 6000);
}

// ---------------------------------------------------------------- eventos fijos

$('btnRun').onclick = () => cmd({ cmd: 'run' });
$('btnStop').onclick = () => cmd({ cmd: 'stop' });
$('senderSelect').onchange = (e) => cmd({ cmd: 'set_sender', name: e.target.value || null });
$('btnSaveSettings').onclick = () => cmd({
  cmd: 'settings',
  values: {
    fps: Number($('setFps').value),
    quality: Number($('setQuality').value),
    min_quality: Number($('setMinQuality').value),
    target_mbps: Number($('setMbps').value),
    total_mbps: Number($('setTotalMbps').value),
    circle_mask: $('setMask').checked,
    jpeg_optimize: $('setOptimize').checked,
  },
}).then(() => { settingsLoaded = false; toast('Ajustes guardados', 'info'); });
$('btnAllPowerOn').onclick = async () => {
  if (await confirmModal('Encender TODOS los motores',
    'Todas las aspas empezarán a girar. Asegúrate de que nadie esté cerca de los ventiladores.', 'Encender todos')) {
    cmd({ cmd: 'power', mac: 'all', on: true });
  }
};
$('btnAllPowerOff').onclick = () => cmd({ cmd: 'power', mac: 'all', on: false });
$('btnAllBrightUp').onclick = () => allBrightness(1);
$('btnAllBrightDown').onclick = () => allBrightness(-1);
bindDrop($('detected'), null);

document.addEventListener('keydown', (e) => {
  if (e.key === 'F9') { e.preventDefault(); cmd({ cmd: 'run' }); }
  if (e.key === 'F10') { e.preventDefault(); cmd({ cmd: 'stop' }); }
});

if (window.spinbridge && window.spinbridge.onEngineExit) {
  window.spinbridge.onEngineExit((code) => toast(`El engine se detuvo (código ${code}). Reinicia SpinBridge.`));
}

connect();
