/*
 * Nextcloud Server Manager
 * Copyright (c) 2026 roswitina@hotmail.com
 * SPDX-License-Identifier: MIT
 * Lizenz: siehe LICENSE · Gewährleistungs- und Haftungsausschluss: siehe HAFTUNGSAUSSCHLUSS.md
 */
'use strict';
// Rückfrage für Formulare mit data-confirm und Schutz vor Doppelklicks.
document.addEventListener('submit', function (e) {
  var f = e.target, msg = f.getAttribute('data-confirm');
  if (msg && !window.confirm(msg)) { e.preventDefault(); return; }
  f.querySelectorAll('button').forEach(function (b) { setTimeout(function () { b.disabled = true; }, 0); });
});

// Live-Ausgabe eines laufenden Jobs.
(function () {
  var box = document.getElementById('job');
  if (!box || box.getAttribute('data-status') !== 'running') return;
  var url = box.getAttribute('data-url'), out = document.getElementById('job-output'),
      state = document.getElementById('job-state');
  function poll() {
    fetch(url, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
      if (r.status === 401) { window.location.href = '/login'; throw new Error('login'); }
      return r.json();
    }).then(function (j) {
      var atBottom = out.scrollTop + out.clientHeight >= out.scrollHeight - 20;
      out.textContent = j.output || '(noch keine Ausgabe)';
      if (atBottom) out.scrollTop = out.scrollHeight;
      if (j.status === 'running') {
        state.textContent = 'LÄUFT · ' + j.duration.toFixed(0) + ' s';
        setTimeout(poll, 2000);
      } else {
        window.location.reload();
      }
    }).catch(function (e) { if (e.message !== 'login') setTimeout(poll, 5000); });
  }
  setTimeout(poll, 1000);
})();

// PHP-Formular: beim Wechsel der Einstellung aktuellen Wert vorausfüllen.
(function () {
  var sel = document.getElementById('key'), input = document.getElementById('value'),
      info = document.getElementById('value-info');
  if (!sel || !input || !info) return;
  sel.addEventListener('change', function () {
    var o = sel.options[sel.selectedIndex];
    var cur = o.getAttribute('data-current'), rec = o.getAttribute('data-rec');
    input.value = cur !== '' ? cur : rec;
    info.textContent = 'Aktuell: ' + (cur || '–') + ' (aus ' + o.getAttribute('data-origin') + ') · Empfehlung: ' + rec +
      ' · ' + o.getAttribute('data-hint');
    input.select();
  });
})();

// config.php-Katalog: Suche über Schlüssel und Beschreibung.
(function () {
  var q = document.getElementById('catalog-search');
  if (!q) return;
  var items = document.querySelectorAll('[data-search]'), count = document.getElementById('catalog-count');
  q.addEventListener('input', function () {
    var t = q.value.trim().toLowerCase(), n = 0;
    items.forEach(function (el) {
      var hit = !t || el.getAttribute('data-search').indexOf(t) !== -1;
      el.hidden = !hit; if (hit) n++;
    });
    document.querySelectorAll('.catalog-group').forEach(function (g) {
      g.hidden = !g.querySelector('[data-search]:not([hidden])');
      if (t) g.open = true;
    });
    if (count) count.textContent = n + ' Treffer';
  });
})();

// Druckansicht: Knopf „Drucken / als PDF speichern“.
document.querySelectorAll('[data-print]').forEach(function (b) {
  b.addEventListener('click', function () { window.print(); });
});
