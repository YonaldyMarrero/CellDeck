/*
 * CellDeck - página de emparejamiento (PC). Muestra PIN, dirección y QR para el modo WiFi.
 * Las rutas /api/pairing solo responden a la propia PC.
 */
(function (global) {
  'use strict';

  function $(id) { return document.getElementById(id); }
  function show(id, visible) { $(id).classList.toggle('hidden', !visible); }

  async function getJson(path, token) {
    const res = await fetch(path, { headers: token ? { 'X-CellDeck-Token': token } : {}, cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  // Mismo almacenamiento que el panel (util.js): el token de la PC sirve para ambas páginas.
  const TOKEN_KEY = 'celldeck.token';

  function savedToken() {
    try { return JSON.parse(global.localStorage.getItem(TOKEN_KEY)); } catch (e) { return null; }
  }

  function saveToken(token) {
    try { global.localStorage.setItem(TOKEN_KEY, JSON.stringify(token)); } catch (e) { /* sin almacenamiento */ }
  }

  /** Canjea el enlace de la bandeja (#admin=...) por un token de administración. */
  async function adminFromHash() {
    const m = /[#&]admin=([A-Za-z0-9_-]{1,64})/.exec(global.location.hash);
    if (!m) return null;
    if (global.history && global.history.replaceState) {
      global.history.replaceState(null, '', global.location.pathname + global.location.search);
    }
    const res = await fetch('/api/session/admin', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: m[1] }),
      cache: 'no-store'
    });
    if (!res.ok) return null;
    const data = await res.json();
    saveToken(data.token);
    return data.token;
  }

  async function load() {
    const msg = $('pair-msg');
    try {
      const token = (await adminFromHash()) || savedToken();
      const info = await getJson('/api/pairing', token).catch(function () {
        throw new Error('ábrela desde el icono de CellDeck en la bandeja → "Mostrar QR / PIN"');
      });
      $('usb-pin').textContent = info.pin;
      $('usb-url').textContent = info.usb_url;
      if (info.mode !== 'wifi') {
        msg.textContent = 'Modo USB activo.';
        show('usb-only', true);
        return;
      }
      msg.textContent = 'Modo WiFi activo.' + (info.allow_remote_edit
        ? ' ⚠ La edición desde el celular está permitida.' : '');
      show('wifi-box', true);
      $('pin').textContent = info.pin;
      if (!info.urls.length) {
        $('url').textContent = 'No se encontró ninguna red local activa.';
        return;
      }
      const main = info.urls[0];
      $('url').textContent = main.split('#')[0];
      const others = info.urls.slice(1).map(function (u) { return u.split('#')[0]; });
      $('other-urls').textContent = others.length ? 'Otras redes: ' + others.join(' · ') : '';
      // El QR se pide con la cabecera del token (una <img> no puede enviarla).
      const res = await fetch('/api/pairing/qr.png?url=' + encodeURIComponent(main),
        { headers: { 'X-CellDeck-Token': token } });
      if (res.ok) $('qr').src = URL.createObjectURL(await res.blob());
    } catch (err) {
      msg.textContent = 'No se pudo cargar: ' + err.message;
    }
  }

  global.addEventListener('load', load);
})(window);
