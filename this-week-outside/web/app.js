/* This Week Outside — client. */
(() => {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const state = {
    data: null,          // last /api/guide payload
    unit: 'c',
    ai: { available: false },
    writing: false,
    run: 0,
    place: null,
    spotted: new Set(),
  };

  /* helpers */

  function h(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? '' : String(v));
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return node;
  }
  const store = {
    get(key) { try { return JSON.parse(localStorage.getItem('two:' + key)); } catch { return null; } },
    set(key, val) { try { localStorage.setItem('two:' + key, JSON.stringify(val)); } catch { /* private mode */ } },
  };
  const spottedKey = () => {
    const d = state.data;
    if (!d) return null;
    const scope = d.demo ? 'demo' : `${d.place || ''}|${d.lat},${d.lng}|${d.radius_km}km|${d.days}d`;
    return `spotted:${scope}|${d.window.to}`;
  };
  function loadSpotted() {
    const saved = store.get(spottedKey());
    const available = new Set(state.data.groups.flatMap((g) => g.species.map((s) => s.sci)));
    state.spotted = new Set(Array.isArray(saved) ? saved.filter((x) => typeof x === 'string' && available.has(x)) : []);
  }
  function totalSpecies(d) { return d.groups.reduce((n, g) => n + g.species.length, 0); }
  function updateTally() {
    const el = $('#tally');
    const d = state.data;
    if (!el || !d) return;
    const total = totalSpecies(d);
    const n = state.spotted.size;
    const goal = Math.min(3, total);
    el.textContent = n === 0 ? `Spotted 0 of ${total}. Tick a box when you find one.`
      : n >= goal ? `Spotted ${n} of ${total}. Mission accomplished, now go for more.`
      : `Spotted ${n} of ${total}. Keep going.`;
  }
  const safeUrl = (u) => (typeof u === 'string' && /^https:\/\//i.test(u) ? u : null);
  const fmtInt = (n) => (typeof n === 'number' ? n.toLocaleString() : '–');
  function fmtDate(iso) {
    const d = new Date(iso + 'T12:00:00');
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }
  function fmtTime(hhmm) {
    if (!hhmm) return '';
    const d = new Date('1970-01-01T' + hhmm + ':00');
    return Number.isNaN(d.getTime()) ? hhmm : d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  }
  function fmtTemp(c) {
    if (typeof c !== 'number') return '–';
    return state.unit === 'f' ? Math.round((c * 9) / 5 + 32) + '°F' : Math.round(c) + '°C';
  }
  async function api(path, params) {
    const url = path + '?' + new URLSearchParams(params).toString();
    let res;
    try { res = await fetch(url); }
    catch { throw new Error('Could not reach the server. Is it still running?'); }
    let body = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    if (!res.ok) throw new Error((body && body.error) || `Server error (${res.status}).`);
    return body;
  }

  function notice(msg, kind = 'error') {
    const el = $('#notice');
    if (!msg) { el.hidden = true; return; }
    el.textContent = msg;
    el.dataset.kind = kind;
    el.hidden = false;
  }

  let toastTimer;
  function toast(msg) {
    const el = $('#toast');
    el.textContent = msg;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 2200);
  }

  function setBusy(on, text) {
    $('#loading').hidden = !on;
    if (text) $('#loading-text').textContent = text;
    $('#btn-go').disabled = on;
    $('#btn-locate').disabled = on;
    $('#btn-demo').disabled = on;
  }

  /* AI status */

  async function checkAI() {
    const pill = $('#ai-status');
    const box = $('#opt-llm');
    try {
      const s = await api('/api/health');
      state.ai = s.ai || { available: false };
    } catch {
      state.ai = { available: false, hint: 'Server unreachable.' };
    }
    if (state.ai.available) {
      pill.textContent = `AI · ${state.ai.model} · local`;
      pill.className = 'pill pill-ai ok';
    } else {
      pill.textContent = 'AI offline · template text';
      pill.className = 'pill pill-ai warn';
      pill.title = state.ai.hint || '';
      box.checked = false;
      box.disabled = true;
      box.parentElement.title = state.ai.hint || 'No local model found.';
    }
  }

  /* card rendering */

  function speciesLink(s, cls) {
    const url = safeUrl(s.url);
    return url
      ? h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', class: cls }, s.name)
      : h('span', { class: cls }, s.name);
  }

  function bestLine(w) {
    if (!w || w.best_index == null || !w.days[w.best_index]) return null;
    const d = w.days[w.best_index];
    const isToday = w.best_index === 0;
    const parts = [String(d.summary || '').toLowerCase(), `${fmtTemp(d.tmax_c)} high`];
    if (typeof d.rain_pct === 'number') parts.push(`${d.rain_pct}% chance of rain`);
    return {
      day: isToday ? 'Today' : d.weekday_long,
      text: parts.filter(Boolean).join(' · ') + '.',
      golden: d.golden_hour ? fmtTime(d.golden_hour) : '',
    };
  }

  function renderHead(d) {
    const w = d.window || {};
    const stat = (value, label) => h('div', { class: 'stat' }, h('b', null, value), h('span', null, label));
    return h('header', { class: 'card-head' },
      d.demo ? h('div', { class: 'demo-banner' }, 'Sample data · not real sightings') : null,
      h('div', { class: 'card-kicker' }, 'Field card · week of ', fmtDate(w.to)),
      h('h2', { class: 'card-title' }, 'This Week ', h('em', null, 'Outside')),
      h('p', { class: 'card-place' }, d.place || 'Your area', ' · ', d.radius_km, ' km radius'),
      h('p', { class: 'card-dates' }, 'Sightings logged ', fmtDate(w.from), ' – ', fmtDate(w.to)),
      h('div', { class: 'stats' },
        stat(fmtInt(d.totals.species), 'species'),
        d.totals.observations != null ? stat(fmtInt(d.totals.observations), 'sightings') : null,
        d.fresh_known ? stat(fmtInt(d.totals.fresh), 'just showed up') : null));
  }

  function renderIntro(d) {
    return h('div', null,
      h('p', { class: 'card-intro' + (state.writing ? ' writing' : '') }, d.narration.intro),
      state.writing ? h('span', { class: 'writing-badge' }, '✨ your local AI is polishing this text…') : null);
  }

  function renderMission(d) {
    const best = bestLine(d.weather);
    return h('section', { class: 'mission' },
      h('h3', null, 'This week’s mission'),
      h('p', { class: 'mission-text' }, d.narration.mission),
      best
        ? h('p', { class: 'mission-when' },
            h('b', null, 'Best window: ', best.day), ' — ', best.text,
            best.golden ? ` Golden hour from ${best.golden}.` : '')
        : null,
      h('p', { class: 'tally no-print', id: 'tally' }));
  }

  function renderWeek(d) {
    const w = d.weather;
    if (!w || !w.days || !w.days.length) return null;
    return h('section', { class: 'week', 'aria-label': '7-day outlook' },
      w.days.map((day, i) => h('div', { class: 'day' + (i === w.best_index ? ' best' : ''), title: day.summary },
        h('span', { class: 'day-name' }, i === 0 ? 'Today' : day.weekday),
        h('span', { class: 'day-icon', 'aria-hidden': 'true' }, day.icon),
        h('span', { class: 'day-temp' }, fmtTemp(day.tmax_c), h('small', null, ' / ', fmtTemp(day.tmin_c))),
        h('span', { class: 'day-rain' }, '💧 ', typeof day.rain_pct === 'number' ? day.rain_pct + '%' : '–'),
        i === w.best_index ? h('span', { class: 'day-flag' }, '★ best') : null)));
  }

  function renderFresh(d) {
    if (!d.fresh_known || !d.fresh.length) return null;
    return h('section', { class: 'fresh' },
      h('h3', null, 'Just showed up'),
      h('p', { class: 'hint' }, `Logged here in the last ${d.days} days, but not in the two months before.`),
      h('ul', { class: 'chips' }, d.fresh.map((s) => h('li', null, speciesLink(s, 'chip')))));
  }

  function speciesRow(s, d, emoji) {
    const tip = d.narration.tips && d.narration.tips[s.sci];
    const photo = safeUrl(s.photo);
    const done = state.spotted.has(s.sci);
    const li = h('li', { class: 'sp' + (done ? ' spotted' : '') });
    const box = h('input', {
      type: 'checkbox', class: 'tick', checked: done, 'aria-label': `Spotted: ${s.name}`,
      onchange: (ev) => {
        if (ev.target.checked) state.spotted.add(s.sci); else state.spotted.delete(s.sci);
        li.classList.toggle('spotted', ev.target.checked);
        store.set(spottedKey(), [...state.spotted]);
        updateTally();
      },
    });
    li.append(box,
      photo
        ? h('img', { class: 'sp-img', src: photo, alt: '', title: s.photo_credit || '', loading: 'lazy', width: 40, height: 40, referrerpolicy: 'no-referrer' })
        : h('span', { class: 'sp-img sp-img-empty', 'aria-hidden': 'true' }, emoji),
      h('div', { class: 'sp-main' },
        h('span', { class: 'sp-name' }, speciesLink(s), s.fresh ? h('span', { class: 'new-tag' }, 'new') : null),
        s.common ? h('i', { class: 'sp-sci' }, s.sci) : null,
        tip ? h('span', { class: 'sp-tip' }, tip) : null),
      h('span', { class: 'sp-count', title: 'sightings in this window' }, s.count + '×'));
    return li;
  }

  function renderGroups(d) {
    if (!d.groups.length) {
      return h('section', { class: 'empty' },
        h('h3', null, 'Nothing logged nearby lately'),
        h('p', null, 'Try a bigger radius or a longer time window — or be the first to log something today!'));
    }
    return h('div', { class: 'groups' },
      d.groups.map((g) => h('section', { class: 'group' },
        h('h3', null, `${g.emoji} ${g.label}`),
        d.group_tips && d.group_tips[g.key] ? h('p', { class: 'group-tip' }, d.group_tips[g.key]) : null,
        h('ol', { class: 'species' }, g.species.map((s) => speciesRow(s, d, g.emoji))))));
  }

  function renderNotes() {
    return h('section', { class: 'notes' }, h('h3', null, 'Field notes'), h('div', { class: 'note-lines', 'aria-hidden': 'true' }));
  }

  function renderFoot(d) {
    const n = d.narration || {};
    const how = n.source && n.source !== 'template'
      ? `Text written by ${n.source}, running locally.`
      : 'Summary text built straight from the data (no AI model).';
    return h('footer', { class: 'card-foot' },
      d.demo ? h('p', { class: 'safety' }, 'Sample data for demonstration only: these are not real sightings near you.') : null,
      h('p', { class: 'safety' }, 'Look, don’t eat. Never eat a plant or mushroom you identified from this card.'),
      h('p', null, 'Sightings: iNaturalist observers (community-confirmed) · Forecast: Open-Meteo · ', how),
      h('p', { class: 'sendoff' }, 'Now go outside. Leave the phone at home.'));
  }

  function render() {
    const d = state.data;
    if (!d) return;
    const card = $('#card');
    loadSpotted();
    card.replaceChildren(...[
      renderHead(d), renderIntro(d), renderMission(d), renderWeek(d), renderFresh(d), renderGroups(d), renderNotes(), renderFoot(d),
    ].filter(Boolean));
    updateTally();
    card.classList.toggle('print-photos', $('#opt-photos').checked);
    if (d.groups.length >= 5) card.setAttribute('data-dense', ''); else card.removeAttribute('data-dense');
    $('#result').hidden = false;
  }

  /* plain-text export */

  function toPlainText(d) {
    const best = bestLine(d.weather);
    const lines = [
      ...(d.demo ? ['*** SAMPLE DATA, NOT REAL SIGHTINGS ***'] : []),
      `THIS WEEK OUTSIDE — ${d.place || 'Your area'} (${d.radius_km} km)`,
      `Sightings ${fmtDate(d.window.from)} – ${fmtDate(d.window.to)}: ${fmtInt(d.totals.species)} species` +
        (d.totals.observations != null ? `, ${fmtInt(d.totals.observations)} sightings` : ''),
      '',
      d.narration.intro,
      '',
      `MISSION: ${d.narration.mission}`,
    ];
    if (best) lines.push(`BEST WINDOW: ${best.day} — ${best.text}${best.golden ? ' Golden hour from ' + best.golden + '.' : ''}`);
    if (d.fresh_known && d.fresh.length) {
      lines.push('', 'JUST SHOWED UP');
      d.fresh.forEach((s) => lines.push(`- ${s.name}${s.common ? ` (${s.sci})` : ''}`));
    }
    d.groups.forEach((g) => {
      lines.push('', `${g.label.toUpperCase()}`);
      g.species.forEach((s) => {
        const tip = d.narration.tips && d.narration.tips[s.sci];
        lines.push(`- ${s.name}${s.common ? ` (${s.sci})` : ''} — ${s.count}×${tip ? ' — ' + tip : ''}`);
      });
    });
    lines.push('', 'Look, don’t eat: never eat a plant or mushroom you identified from this card.');
    lines.push('Data: iNaturalist · Forecast: Open-Meteo');
    return lines.join('\n');
  }

  async function copyText(text) {
    let ok = false;
    try {
      await navigator.clipboard.writeText(text);
      ok = true;
    } catch {
      const ta = h('textarea', { 'aria-hidden': 'true', style: 'position:fixed;opacity:0' });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      try { ok = document.execCommand('copy'); } catch { ok = false; }
      ta.remove();
    }
    toast(ok ? 'Copied! Paste it anywhere.' : 'Couldn’t copy automatically — select the card text instead.');
  }

  /* flow */

  async function run(place) {
    const token = ++state.run;
    state.place = place;
    state.writing = false;
    notice('');
    setBusy(true, 'Asking the neighbourhood what’s out there…');

    const base = {
      lat: place.lat.toFixed(2),
      lng: place.lng.toFixed(2),
      radius: $('#radius').value,
      days: $('#days').value,
      place: place.label,
      ...(place.demo ? { demo: '1' } : {}),
    };

    try {
      const quick = await api('/api/guide', { ...base, llm: '0' });
      if (token !== state.run) return;
      state.data = quick;
      render();
      setBusy(false);
      $('#result').scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (!place.demo) store.set('place', place);
      if ($('#opt-llm').checked && state.ai.available && quick.totals.species > 0) {
        state.writing = true;
        render();
        try {
          const full = await api('/api/guide', { ...base, llm: '1' });
          if (token !== state.run) return;
          state.data = full;
          if (full.narration.source === 'template') toast('Local AI didn’t answer — showing the quick summary.');
        } catch {
          if (token === state.run) toast('AI writing failed — showing the quick summary.');
        } finally {
          if (token === state.run) { state.writing = false; render(); }
        }
      }
    } catch (err) {
      if (token !== state.run) return;
      setBusy(false);
      notice(err.message);
    }
  }

  const placeFrom = (r) => ({ label: r.label || r.name, lat: r.lat, lng: r.lng });

  function renderAlts(chosen, alts) {
    const box = $('#alts');
    if (!chosen || !alts || !alts.length) { box.hidden = true; box.replaceChildren(); return; }
    box.replaceChildren(
      h('span', null, `Showing ${chosen.label}. Not the right place?`),
      ...alts.map((a) => h('button', {
        type: 'button',
        onclick: () => { $('#place').value = a.label; renderAlts(a, [chosen, ...alts.filter((x) => x !== a)]); run(placeFrom(a)); },
      }, a.label)));
    box.hidden = false;
  }

  async function onSubmit(ev) {
    ev.preventDefault();
    const q = $('#place').value.trim();
    if (state.place && q === state.place.label) return run(state.place);   // same place / geolocated
    if (!q) { notice('Type a town or neighbourhood first — or use your location.', 'info'); return; }

    notice('');
    setBusy(true, 'Finding that place…');
    try {
      const res = await api('/api/geocode', { q });
      if (!res.results.length) {
        setBusy(false);
        notice(`Couldn’t find “${q}”. Try a nearby town, or add the country.`);
        return;
      }
      const [first, ...rest] = res.results;
      $('#place').value = first.label;
      renderAlts(first, rest.slice(0, 4));
      await run(placeFrom(first));
    } catch (err) {
      setBusy(false);
      notice(err.message);
    }
  }

  function onDemo() {
    const place = { label: 'Sample meadow (demo data)', lat: 42.36, lng: -71.06, demo: true };
    $('#place').value = place.label;
    renderAlts(null);
    run(place);
  }

  function onLocate() {
    if (!navigator.geolocation) { notice('Your browser can’t share its location. Type a place instead.'); return; }
    notice('');
    setBusy(true, 'Getting your location…');
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const place = { label: 'Near you', lat: pos.coords.latitude, lng: pos.coords.longitude };
        $('#place').value = place.label;
        renderAlts(null);
        run(place);
      },
      () => { setBusy(false); notice('Couldn’t get your location (permission denied?). Type a place instead.'); },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 });
  }

  /*init */

  function init() {
    checkAI();
    const saved = store.get('place');
    if (saved && typeof saved.label === 'string' && Number.isFinite(saved.lat) && Number.isFinite(saved.lng)) {
      state.place = saved;
      $('#place').value = saved.label;
    }

    $('#form').addEventListener('submit', onSubmit);
    $('#btn-locate').addEventListener('click', onLocate);
    $('#btn-demo').addEventListener('click', onDemo);
    $('#btn-print').addEventListener('click', () => window.print());
    $('#btn-copy').addEventListener('click', () => state.data && copyText(toPlainText(state.data)));
    $('#opt-photos').addEventListener('change', (e) => $('#card').classList.toggle('print-photos', e.target.checked));
    $('#place').addEventListener('input', () => { if (state.place && $('#place').value.trim() !== state.place.label) renderAlts(null); });
    $$('[data-unit]').forEach((btn) => btn.addEventListener('click', () => {
      state.unit = btn.dataset.unit;
      $$('[data-unit]').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
      render();
    }));
  }
  init();
})();
