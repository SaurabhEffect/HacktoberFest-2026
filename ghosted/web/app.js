/* Ghosted front end. Vanilla JS, no build step.
   Everything that comes from the network (issue titles, bodies, model text) is put on the page with
   textContent, never innerHTML, so a hostile issue cannot inject markup. */
(function () {
  'use strict';

  /* ================= small helpers ================= */
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var HOME_TITLE = 'Ghosted · answer the issues nobody answered';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function append(el, kids) {
    kids.forEach(function (kid) {
      if (Array.isArray(kid)) { append(el, kid); return; }
      if (kid === null || kid === undefined || kid === false) return;
      el.appendChild(kid && kid.nodeType ? kid : document.createTextNode(String(kid)));
    });
  }

  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        var value = attrs[key];
        if (value === false || value === null || value === undefined) return;
        if (key === 'class') el.className = value;
        else if (key === 'text') el.textContent = value;
        else if (key === 'on') Object.keys(value).forEach(function (ev) { el.addEventListener(ev, value[ev]); });
        else el.setAttribute(key, value === true ? '' : String(value));
      });
    }
    append(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }

  function icon(name, cls) {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'ico' + (cls ? ' ' + cls : ''));
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    var use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#' + name);
    svg.appendChild(use);
    return svg;
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function truncate(text, max) {
    text = String(text || '');
    return text.length > max ? text.slice(0, max - 1).replace(/\s+\S*$/, '') + '…' : text;
  }
  function safeUrl(url) { return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : ''; }
  function toInt(value) {
    var n = parseInt(String(value).trim(), 10);
    return isNaN(n) ? undefined : n;
  }

  function fmtDate(iso) {
    var d = iso ? new Date(iso) : null;
    if (!d || isNaN(d.getTime())) return 'an unknown date';
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  }
  function daysSince(iso) {
    var d = iso ? new Date(iso) : null;
    if (!d || isNaN(d.getTime())) return null;
    return Math.max(0, Math.floor((Date.now() - d.getTime()) / 86400000));
  }
  /* 1400 days -> ["3y", "10mo"]; 45 days -> ["45d"] */
  function spanParts(days) {
    days = Math.max(0, Math.round(Number(days) || 0));
    if (days < 60) return [days + 'd'];
    var months = Math.round(days / 30.4375);
    if (months < 24) return [months + 'mo'];
    var years = Math.floor(months / 12), rest = months % 12;
    return rest ? [years + 'y', rest + 'mo'] : [years + 'y'];
  }

  var toastTimer = 0;
  function toast(message) {
    var el = $('#toast');
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove('show'); }, 3200);
  }

  var storage = {
    get: function (key) { try { return JSON.parse(window.localStorage.getItem(key)); } catch (e) { return null; } },
    set: function (key, value) { try { window.localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* private mode: fine */ } }
  };

  function ApiError(status, message) {
    this.name = 'ApiError';
    this.status = status;
    this.message = message;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function api(method, path, body) {
    var opts = { method: method, headers: {} };
    if (method !== 'GET') {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body || {});
    }
    return fetch(path, opts).then(function (res) {
      return res.json().then(function (data) { return { res: res, data: data }; },
                             function () { return { res: res, data: null }; });
    }, function () {
      throw new ApiError(0, "Can't reach Ghosted. Is the terminal window that started it still running?");
    }).then(function (out) {
      if (!out.res.ok) {
        throw new ApiError(out.res.status, (out.data && out.data.error) || ('Something went wrong (HTTP ' + out.res.status + ').'));
      }
      return out.data;
    });
  }

  /* ================= state ================= */
  var SETTINGS_KEY = 'ghosted.settings.v1';
  var FALLBACK_URLS = { ollama: 'http://localhost:11434', openai: 'http://localhost:1234/v1' };
  var TOKEN_HINT = "Raises GitHub's limit from 60 to 5,000 requests an hour. Read-only is plenty. Used for this scan only, never saved.";

  var VERDICTS = {
    fixed: { label: 'Already fixed', icon: 'i-check' },
    valid: { label: 'Still valid', icon: 'i-alert' },
    needs_info: { label: 'Needs info', icon: 'i-question' }
  };
  var STRENGTH_LABEL = { closing: 'closes', mention: 'mentions', similar: 'looks similar' };
  var FLAG_TEXT = {
    verdict_from_evidence: "A merged change says it closes this issue, so Ghosted set the verdict from that evidence instead of the model's guess.",
    fixed_without_evidence: 'The model guessed this was fixed, but nothing in the history backs that up, so Ghosted downgraded it to needs info.',
    reason_replaced: "The model's explanation mentioned something Ghosted could not verify, so it was replaced.",
    reply_replaced: "The model's draft mentioned something Ghosted could not verify (a link, version or issue number), so a safe template was used instead."
  };
  var STAGES = [['model', 'Warm up'], ['repo', 'Read the repo'], ['issues', 'Find silent issues'], ['evidence', 'Dig for fixes'], ['analyze', 'Read and draft']];
  var STAGE_TITLE = { queued: 'Getting ready', model: 'Waking the model', repo: 'Reading the repository', issues: 'Counting the silent issues', evidence: 'Digging for fixes', analyze: 'Reading and drafting', finish: 'Done' };
  var STAGE_BASE = { queued: 2, model: 5, repo: 10, issues: 16, evidence: 22 };

  var state = {
    view: null,
    focusRepo: false,
    config: { defaults: { backend: 'ollama', base_url: FALLBACK_URLS.ollama, model: '' }, has_env_token: false, demo_repo: 'ghost-town/forgotten-lib' },
    settings: { backend: null, urls: {}, models: {}, max: '', minAge: '', order: '' },
    autoBackend: true,
    fellBack: false,
    models: { ok: null, list: [], error: '' },
    starting: false,
    scanId: null, scan: null, results: [], cards: new Map(),
    filter: 'all', hideSkipped: false, onlyApproved: false, cancelling: false,
    pollTimer: 0, pollFails: 0, loadSeq: 0,
    saves: new Map()
  };

  var nativeGrow = !!(window.CSS && CSS.supports && CSS.supports('field-sizing', 'content'));

  /* ================= settings form ================= */
  function currentBackend() {
    var checked = $('input[name="backend"]:checked');
    return checked ? checked.value : 'ollama';
  }
  function setBackendRadio(value) {
    $$('input[name="backend"]').forEach(function (r) { r.checked = (r.value === value); });
  }
  function persist() { storage.set(SETTINGS_KEY, state.settings); }

  function loadSettings() {
    var saved = storage.get(SETTINGS_KEY) || {};
    state.settings = {
      backend: saved.backend === 'ollama' || saved.backend === 'openai' || saved.backend === 'rules' ? saved.backend : null,
      urls: saved.urls && typeof saved.urls === 'object' ? saved.urls : {},
      models: saved.models && typeof saved.models === 'object' ? saved.models : {},
      max: saved.max || '', minAge: saved.minAge === 0 || saved.minAge ? saved.minAge : '', order: saved.order === 'newest' ? 'newest' : ''
    };
    state.autoBackend = !state.settings.backend;
  }

  function setHint(el, nodes) { clear(el); append(el, nodes); }

  function syncBackendFields() {
    var backend = currentBackend();
    var usesModel = backend !== 'rules';
    $('#field-url').hidden = !usesModel;
    $('#field-model').hidden = !usesModel;
    if (!usesModel) return;
    var url = state.settings.urls[backend] || (backend === 'ollama' ? state.config.defaults.base_url : FALLBACK_URLS.openai);
    $('#base-url').value = url;
    $('#base-url').placeholder = FALLBACK_URLS[backend];
    $('#model').value = state.settings.models[backend] || (backend === 'ollama' ? state.config.defaults.model : '') || '';
    $('#model').placeholder = backend === 'ollama' ? 'gemma3:4b' : 'the model your server has loaded';
    setHint($('#url-hint'), backend === 'ollama'
      ? ['Ollama listens on ', h('code', { text: 'http://localhost:11434' }), ' by default.']
      : ['LM Studio uses ', h('code', { text: 'http://localhost:1234/v1' }), '. llama.cpp and vLLM print theirs when they start.']);
    updateModelHint();
  }

  function modelInstalled(list, wanted) {
    if (!wanted) return false;
    return list.indexOf(wanted) >= 0 || (wanted.indexOf(':') < 0 && list.indexOf(wanted + ':latest') >= 0);
  }

  function updateModelHint() {
    var backend = currentBackend();
    var wanted = $('#model').value.trim();
    var list = state.models.list || [];
    if (backend === 'ollama') {
      if (wanted && list.length && !modelInstalled(list, wanted)) {
        setHint($('#model-hint'), ["Ollama doesn't list this model yet. Pull it with ", h('code', { text: 'ollama pull ' + wanted })]);
      } else {
        setHint($('#model-hint'), ['Any small instruction-tuned model works. Gemma is a good start: ', h('code', { text: 'ollama pull gemma3:4b' })]);
      }
    } else {
      setHint($('#model-hint'), ['Type the model name your server is serving. The list fills in when the server offers one.']);
    }
  }

  function fillModelList() {
    var backend = currentBackend();
    var list = $('#model-list');
    clear(list);
    (state.models.list || []).forEach(function (name) { list.appendChild(h('option', { value: name })); });
    var input = $('#model');
    if (!input.value.trim() && state.models.list.length) {
      var preferred = state.config.defaults.model;
      input.value = modelInstalled(state.models.list, preferred) ? preferred : state.models.list[0];
      state.settings.models[backend] = input.value;
      persist();
    }
    updateModelHint();
  }

  function backendName(backend) { return backend === 'ollama' ? 'Ollama' : 'your model server'; }

  function renderEngine(loading) {
    var backend = currentBackend();
    var chip = $('#engine-chip');
    var kind = 'wait', message = '', helpNodes = null;
    if (backend === 'rules') {
      kind = 'ok';
      message = 'Rule-based preview · no model needed';
      if (state.fellBack) {
        helpNodes = ['No local model was found, so Ghosted is using the rule-based preview: the same evidence checks, with template replies. For real drafts, install Ollama, run ',
          h('code', { text: 'ollama pull gemma3:4b' }), ', then choose “Local model” in Settings.'];
      }
    } else if (loading) {
      message = 'Looking for ' + backendName(backend) + '…';
    } else if (state.models.ok && state.models.list.length) {
      kind = 'ok';
      message = (backend === 'ollama' ? 'Ollama is awake' : 'Model server is awake') + ' · ' + plural(state.models.list.length, 'model');
    } else if (state.models.ok) {
      kind = 'warn';
      message = 'Ollama is awake but has no models yet';
      helpNodes = ['Pull a small one first: ', h('code', { text: 'ollama pull gemma3:4b' }), '. Or '];
    } else {
      kind = 'warn';
      message = backend === 'ollama' ? "Can't reach Ollama" : "Can't read this server's model list";
      helpNodes = backend === 'ollama'
        ? ["Ghosted couldn't reach Ollama at ", h('code', { text: $('#base-url').value.trim() || FALLBACK_URLS.ollama }), '. Start it, then ', h('code', { text: 'ollama pull gemma3:4b' }), '. Or ']
        : ["Ghosted couldn't read a model list from ", h('code', { text: $('#base-url').value.trim() || FALLBACK_URLS.openai }),
           '. If the server is running, type the model name yourself in Settings. Or '];
    }
    chip.className = 'chip ' + kind;
    $('.dot', chip).className = 'dot' + (kind === 'wait' ? ' wait' : '');
    $('#engine-chip-text').textContent = message;

    var help = $('#engine-help');
    clear(help);
    if (helpNodes && !(backend === 'rules' && !state.fellBack)) {
      append(help, helpNodes);
      if (backend !== 'rules') {
        help.appendChild(h('button', { class: 'linkish', type: 'button', text: 'use the rule-based preview', on: { click: function () {
          setBackendRadio('rules'); onBackendChange();
        } } }));
        help.appendChild(document.createTextNode(' to try it without a model.'));
      }
    }
    help.hidden = !help.firstChild;
  }

  var modelSeq = 0;
  function checkModels() {
    var backend = currentBackend();
    if (backend === 'rules') { state.models = { ok: null, list: [], error: '' }; renderEngine(false); return; }
    var seq = ++modelSeq;
    renderEngine(true);
    var query = new URLSearchParams({ backend: backend, base_url: $('#base-url').value.trim() });
    api('GET', '/api/models?' + query.toString()).catch(function (e) {
      return { ok: false, models: [], error: e.message };
    }).then(function (data) {
      if (seq !== modelSeq) return;
      state.models = { ok: !!data.ok, list: data.models || [], error: data.error || '' };
      if (state.autoBackend && backend === 'ollama' && !(state.models.ok && state.models.list.length)) {
        // First visit, nothing chosen yet, no model found: start in the preview so the demo just works.
        state.fellBack = true;
        setBackendRadio('rules');
        syncBackendFields();
        renderEngine(false);
        return;
      }
      state.fellBack = false;
      fillModelList();
      renderEngine(false);
    });
  }

  function onBackendChange() {
    state.autoBackend = false;
    state.fellBack = false;
    state.settings.backend = currentBackend();
    persist();
    syncBackendFields();
    checkModels();
  }

  var checkTimer = 0;
  function debounceCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(checkModels, 600);
  }

  function applySettingsToForm() {
    var s = state.settings;
    setBackendRadio(s.backend || 'ollama');
    if (s.max) $('#max-issues').value = s.max;
    if (s.minAge !== '') $('#min-age').value = s.minAge;
    if (s.order) $('#order').value = s.order;
    syncBackendFields();
    $('#token-hint').textContent = state.config.has_env_token
      ? 'A GITHUB_TOKEN is set in your environment, so you can leave this blank. A token typed here is used for this scan only and never saved.'
      : TOKEN_HINT;
  }

  function settingsBody() {
    var backend = currentBackend();
    var body = { backend: backend, max_issues: toInt($('#max-issues').value), min_age_days: toInt($('#min-age').value), order: $('#order').value };
    if (backend !== 'rules') {
      body.base_url = $('#base-url').value.trim();
      body.model = $('#model').value.trim();
    }
    var token = $('#token').value.trim();
    if (token) body.token = token;
    return body;
  }

  /* ================= home: start a scan, history ================= */
  function showFormError(message, activeId) {
    var box = $('#form-error');
    clear(box);
    box.appendChild(document.createTextNode(message));
    if (activeId) {
      box.appendChild(document.createTextNode(' '));
      box.appendChild(h('a', { href: '#/scan/' + activeId, text: 'Open the running scan' }));
    }
    box.hidden = false;
  }
  function hideFormError() { $('#form-error').hidden = true; }

  function setBusy(on) {
    state.starting = on;
    $('#go').disabled = on;
    $('#try-demo').disabled = on;
    $('#go span').textContent = on ? 'Starting…' : 'Find the ghosts';
  }

  function startScan(demo) {
    if (state.starting) return;
    hideFormError();
    var repo = $('#repo').value.trim();
    if (!demo && !repo) {
      showFormError('Type a repository first, for example ghost-town/forgotten-lib, or try the demo graveyard.');
      $('#repo').focus();
      return;
    }
    var body = settingsBody();
    body.demo = demo;
    if (demo) {
      delete body.token;
      var haveModel = state.models.ok && state.models.list.length && body.backend !== 'rules';
      if (body.backend !== 'rules' && !haveModel) {
        body.backend = 'rules';
        delete body.base_url;
        delete body.model;
        toast('No model found, so the demo is using the rule-based preview.');
      }
    } else {
      body.repo = repo;
    }
    setBusy(true);
    api('POST', '/api/scans', body).then(function (data) {
      setBusy(false);
      window.location.hash = '#/scan/' + data.scan.id;
    }, function (e) {
      setBusy(false);
      if (e.status === 409) {
        api('GET', '/api/scans').then(function (list) {
          var running = (list.scans || []).filter(function (s) { return s.active; })[0];
          showFormError(e.message, running ? running.id : null);
        }, function () { showFormError(e.message); });
      } else {
        showFormError(e.message);
      }
    });
  }

  function renderHistory(scans) {
    var box = $('#history'), list = $('#history-list');
    clear(list);
    box.hidden = !scans.length;
    scans.forEach(function (s) {
      var c = s.counts || {};
      var bits = [];
      if (s.active || s.status === 'queued' || s.status === 'running') bits.push('Running…');
      else if (s.status === 'error') bits.push('Stopped early');
      else if (s.status === 'cancelled') bits.push('Cancelled');
      var read = (c.fixed || 0) + (c.valid || 0) + (c.needs_info || 0);
      if (read) bits.push((c.fixed || 0) + ' fixed · ' + (c.valid || 0) + ' valid · ' + (c.needs_info || 0) + ' need info');
      else if (s.status === 'done') bits.push('No ignored issues');
      if (s.demo) bits.push('demo');
      bits.push(fmtDate(s.created_at));

      var del = h('button', { class: 'del', type: 'button', 'aria-label': 'Delete the scan of ' + s.repo, text: '×' });
      var li = h('li', null,
        h('a', { class: 'open', href: '#/scan/' + s.id }, h('strong', { text: s.repo }), h('span', { class: 'meta', text: bits.join(' · ') })),
        del);
      var armTimer = 0;
      del.addEventListener('click', function () {
        if (!del.hasAttribute('data-armed')) {
          del.setAttribute('data-armed', '1');
          del.textContent = 'Delete?';
          armTimer = setTimeout(function () { del.removeAttribute('data-armed'); del.textContent = '×'; }, 3000);
          return;
        }
        clearTimeout(armTimer);
        api('DELETE', '/api/scans/' + s.id).then(function () {
          li.remove();
          if (!list.children.length) box.hidden = true;
          toast('Scan deleted.');
        }, function (e) {
          del.removeAttribute('data-armed');
          del.textContent = '×';
          toast(e.message);
        });
      });
      list.appendChild(li);
    });
  }

  function loadHistory() {
    api('GET', '/api/scans').then(function (data) { renderHistory(data.scans || []); }, function () { /* the banner handles outages */ });
  }

  /* ================= routing ================= */
  function setOffline(on) { $('#offline').hidden = !on; }

  function stopPolling() {
    clearTimeout(state.pollTimer);
    state.pollTimer = 0;
    state.loadSeq++;
  }
  function schedulePoll(ms) {
    clearTimeout(state.pollTimer);
    state.pollTimer = setTimeout(refreshScan, ms);
  }

  function route() {
    var match = /^#\/scan\/(\d+)$/.exec(window.location.hash);
    if (match) showScan(parseInt(match[1], 10));
    else showHome();
  }

  function showHome() {
    var wasHome = state.view === 'home';
    state.view = 'home';
    stopPolling();
    flushAll();
    $('#view-home').hidden = false;
    $('#view-scan').hidden = true;
    document.title = HOME_TITLE;
    if (!wasHome) loadHistory();
    var id = '';
    try { id = decodeURIComponent(window.location.hash.slice(1)); } catch (e) { id = ''; }
    var target = id && id.charAt(0) !== '/' ? document.getElementById(id) : null;
    if (target) target.scrollIntoView(); else window.scrollTo(0, 0);
    if (state.focusRepo) { state.focusRepo = false; $('#repo').focus({ preventScroll: true }); }
  }

  function resetScanView(id) {
    state.scanId = id;
    state.scan = null;
    state.results = [];
    state.cards = new Map();
    state.cancelling = false;
    state.filter = 'all';
    state.hideSkipped = false;
    state.onlyApproved = false;
    $('#hide-skipped').checked = false;
    $('#only-approved').checked = false;
    clear($('#queue'));
    clear($('#notes'));
    clear($('#scan-chips'));
    ['#progress', '#alert', '#summary', '#results', '#empty', '#scan-desc', '#filter-empty'].forEach(function (sel) { $(sel).hidden = true; });
    ['#alert', '#summary', '#notes', '#scan-title'].forEach(function (sel) { $(sel)._sig = null; });
    $('#scan-title').textContent = 'Loading…';
    $('#scan-eyebrow').textContent = 'Review queue';
    document.title = 'Ghosted · loading…';
  }

  function showScan(id) {
    flushAll();
    stopPolling();
    state.view = 'scan';
    $('#view-home').hidden = true;
    $('#view-scan').hidden = false;
    window.scrollTo(0, 0);
    if (state.scanId !== id) resetScanView(id);
    refreshScan();
  }

  /* ================= scan view: data ================= */
  function isLive(scan) { return !!scan.active || scan.status === 'queued' || scan.status === 'running'; }

  function cardSig(r, live) {
    return [r.status, r.verdict, r.confidence, r.reason, r.reply, (r.flags || []).join(','), r.error,
      (r.evidence || []).length, r.status === 'pending' ? (live ? 'live' : 'stopped') : ''].join('␟');
  }

  function applyScan(scan, results) {
    var live = isLive(scan);
    var old = new Map();
    state.results.forEach(function (r) { old.set(r.id, r); });
    state.scan = scan;
    state.results = results.map(function (sr) {
      var o = old.get(sr.id);
      return o && cardSig(o, live) === cardSig(sr, live) ? o : sr;   // keep local edits and review marks
    });
  }

  function refreshScan() {
    var id = state.scanId;
    var seq = ++state.loadSeq;
    api('GET', '/api/scans/' + id).then(function (data) {
      if (seq !== state.loadSeq || id !== state.scanId || state.view !== 'scan') return;
      state.pollFails = 0;
      setOffline(false);
      applyScan(data.scan, data.results || []);
      renderScan();
      if (isLive(data.scan)) schedulePoll(1000);
    }, function (e) {
      if (seq !== state.loadSeq || id !== state.scanId || state.view !== 'scan') return;
      if (e.status === 404) {
        toast('That scan no longer exists.');
        window.location.hash = '#/';
        return;
      }
      state.pollFails++;
      if (state.pollFails >= 3) setOffline(true);
      schedulePoll(2000);
    });
  }

  /* ================= scan view: rendering ================= */
  function setSlot(el, sig, build) {
    if (el._sig === sig) return;
    el._sig = sig;
    clear(el);
    build(el);
  }

  function renderScan() {
    var scan = state.scan;
    if (!scan) return;
    var live = isLive(scan);
    renderHeader(scan);
    renderProgress(scan, live);
    renderAlert(scan, live);
    renderNotes(scan);
    var has = state.results.length > 0;
    $('#summary').hidden = !has;
    $('#results').hidden = !has;
    if (has) {
      renderSummary();
      renderToolbar();
      renderQueue(live);
    }
    renderEmpty(scan, live);
  }

  function renderHeader(scan) {
    var info = scan.repo_info || {};
    var title = $('#scan-title');
    var sig = [scan.id, scan.repo, scan.demo, scan.engine, info.full_name, info.description, info.stars, info.language, info.archived, info.pushed_at, info.url].join('|');
    if (title._sig === sig) return;
    title._sig = sig;
    var full = info.full_name || scan.repo || '';
    var slash = full.indexOf('/');
    clear(title);
    if (slash > 0) title.appendChild(document.createTextNode(full.slice(0, slash + 1)));
    title.appendChild(h('em', { text: slash > 0 ? full.slice(slash + 1) : full }));
    document.title = 'Ghosted · ' + full;
    $('#scan-eyebrow').textContent = scan.demo ? 'Demo graveyard · fictional repository' : 'Review queue';
    var desc = $('#scan-desc');
    desc.textContent = info.description || '';
    desc.hidden = !info.description;

    var chips = $('#scan-chips');
    clear(chips);
    if (info.archived) chips.appendChild(h('span', { class: 'chip warn', text: 'Archived: GitHub will not take new comments' }));
    if (info.stars) chips.appendChild(h('span', { class: 'chip', text: Number(info.stars).toLocaleString() + ' stars' }));
    if (info.language) chips.appendChild(h('span', { class: 'chip', text: info.language }));
    var pushed = daysSince(info.pushed_at);
    if (pushed !== null) chips.appendChild(h('span', { class: 'chip', text: 'Last push ' + spanParts(pushed).join(' ') + ' ago' }));
    if (scan.engine) chips.appendChild(h('span', { class: 'chip', text: scan.engine }));
    if (scan.demo) chips.appendChild(h('span', { class: 'chip', text: 'Made-up data' }));
    else if (safeUrl(info.url)) chips.appendChild(h('a', { class: 'chip', href: info.url, target: '_blank', rel: 'noopener noreferrer' }, 'View on GitHub', icon('i-external')));
  }

  function renderProgress(scan, live) {
    var box = $('#progress');
    box.hidden = !live;
    if (!live) { state.cancelling = false; box.classList.remove('running'); return; }
    box.classList.add('running');
    $('#progress-title').textContent = STAGE_TITLE[scan.stage] || 'Working';
    $('#progress-msg').textContent = scan.message || '';
    var pct = STAGE_BASE[scan.stage];
    if (scan.stage === 'analyze' && scan.total > 0) pct = 25 + 75 * (scan.done / scan.total);
    if (pct === undefined) pct = 25;
    pct = Math.max(3, Math.min(100, Math.round(pct)));
    $('#fill').style.width = pct + '%';
    $('#track').setAttribute('aria-valuenow', String(pct));

    var stages = $('#stages');
    if (!stages.children.length) STAGES.forEach(function (s) { stages.appendChild(h('li', { text: s[1] })); });
    var idx = -1;
    STAGES.forEach(function (s, i) { if (s[0] === scan.stage) idx = i; });
    $$('li', stages).forEach(function (li, i) { li.className = idx < 0 ? '' : (i < idx ? 'done' : (i === idx ? 'now' : '')); });

    $('#progress-count').textContent = scan.total ? plural(scan.done, 'issue') + ' read of ' + scan.total : '';
    var cancel = $('#cancel');
    cancel.disabled = state.cancelling;
    cancel.textContent = state.cancelling ? 'Stopping…' : 'Stop';
  }

  function renderAlert(scan, live) {
    var box = $('#alert');
    var failed = 0, unread = 0;
    state.results.forEach(function (r) { if (r.status === 'error') failed++; else if (r.status === 'pending') unread++; });
    var canResume = state.results.length > 0 && failed + unread > 0;
    var kind = null;
    if (!live && scan.status === 'error') kind = 'error';
    else if (!live && scan.status === 'cancelled') kind = 'cancelled';
    else if (!live && scan.status === 'done' && failed > 0) kind = 'partial';
    box.hidden = !kind;
    if (!kind) { box._sig = null; return; }

    setSlot(box, [kind, scan.error, scan.message, failed, unread, canResume].join('|'), function (el) {
      var title, text, variant = 'alert', actions = [];
      if (kind === 'error') {
        title = 'Ghosted had to stop';
        text = scan.error || scan.message || 'Something went wrong.';
      } else if (kind === 'cancelled') {
        variant = 'alert info';
        title = 'Stopped';
        text = scan.message || 'The scan was stopped.';
      } else {
        variant = 'alert note';
        title = "Some issues couldn't be read";
        text = (scan.message || '') + ' Their cards say what went wrong; everything else is saved.';
      }
      if (canResume) {
        actions.push(h('button', { class: 'btn btn-ink btn-sm', type: 'button', text: kind === 'partial' ? 'Retry those' : 'Resume', on: { click: resumeScan } }));
      }
      if (kind === 'error') {
        actions.push(h('a', { class: 'btn btn-line btn-sm', href: '#/', text: 'Back to search', on: { click: function () {
          if (!scan.demo && scan.repo) $('#repo').value = scan.repo;
        } } }));
      }
      el.appendChild(h('div', { class: variant, role: 'alert' },
        h('p', null, h('strong', { text: title }), text),
        actions.length ? h('div', { class: 'acts' }, actions) : null));
    });
  }

  function resumeScan() {
    api('POST', '/api/scans/' + state.scanId + '/retry', {}).then(function () {
      state.cancelling = false;
      refreshScan();
    }, function (e) { toast(e.message); });
  }

  function renderNotes(scan) {
    var warnings = ((scan.stats || {}).warnings) || [];
    setSlot($('#notes'), warnings.join('\n'), function (el) {
      warnings.forEach(function (w) { el.appendChild(h('div', { class: 'alert note' }, h('p', { text: w }))); });
    });
  }

  function tally() {
    var c = { all: state.results.length, fixed: 0, valid: 0, needs_info: 0, approved: 0, skipped: 0, pending: 0, error: 0, done: 0 };
    state.results.forEach(function (r) {
      c[r.status] = (c[r.status] || 0) + 1;
      if (r.status === 'done' && VERDICTS[r.verdict]) c[r.verdict]++;
      if (r.review === 'approved') c.approved++;
      if (r.review === 'skipped') c.skipped++;
    });
    return c;
  }

  function sticker(verdict, extra) {
    var key = VERDICTS[verdict] ? verdict : 'needs_info';
    return h('span', { class: 'sticker st-' + key + (extra ? ' ' + extra : ''), 'aria-hidden': 'true' }, icon(VERDICTS[key].icon));
  }

  function renderSummary() {
    var scan = state.scan, c = tally();
    var stats = scan.stats || {};
    var total = stats.ghosted_total;
    var sig = [c.fixed, c.valid, c.needs_info, total, stats.analyzed].join('|');
    setSlot($('#summary'), sig, function (el) {
      ['fixed', 'valid', 'needs_info'].forEach(function (key) {
        el.appendChild(h('div', { class: 'tile' }, sticker(key),
          h('div', null, h('b', { text: String(c[key]) }), h('span', { text: VERDICTS[key].label.toLowerCase() }))));
      });
      if (typeof total === 'number') {
        el.appendChild(h('div', { class: 'tile plain' },
          h('div', null, h('b', { text: String(total) }), h('span', { text: 'silent issues in this repo' }),
            stats.analyzed < total ? h('small', { text: 'showing ' + stats.analyzed }) : null)));
      }
    });
  }

  function passes(r) {
    if (state.hideSkipped && r.review === 'skipped') return false;
    if (state.filter === 'all') return true;
    if (state.filter === 'approved') return r.review === 'approved';
    return r.status === 'done' && r.verdict === state.filter;
  }

  function renderToolbar() {
    var c = tally();
    $$('.filter').forEach(function (btn) {
      var key = btn.getAttribute('data-filter');
      $('b', btn).textContent = String(key === 'all' ? c.all : (c[key] || 0));
      btn.setAttribute('aria-pressed', String(state.filter === key));
    });
    var suffix = state.onlyApproved ? '&only=approved' : '';
    ['md', 'csv', 'json'].forEach(function (fmt) {
      $('#dl-' + fmt).setAttribute('href', '/api/scans/' + state.scanId + '/export?format=' + fmt + suffix);
    });
  }

  function applyFilter() {
    var shown = 0;
    state.results.forEach(function (r) {
      var el = state.cards.get(r.id);
      if (!el) return;
      var ok = passes(r);
      el.hidden = !ok;
      if (ok) shown++;
    });
    $('#filter-empty').hidden = shown > 0 || state.results.length === 0;
    growAll();
  }

  function grow(ta) {
    if (nativeGrow || !ta.offsetParent) return;
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight + 2, 420) + 'px';
  }
  function growAll() {
    if (nativeGrow) return;
    $$('textarea.reply').forEach(grow);
  }

  function renderQueue(live) {
    var queue = $('#queue');
    var seen = new Set();
    var prev = null;
    state.results.forEach(function (r) {
      seen.add(r.id);
      var el = state.cards.get(r.id);
      var sig = cardSig(r, live);
      if (!el || el._sig !== sig || el._r !== r) {
        var fresh = buildCard(r, state.scan);
        fresh._sig = sig;
        fresh._r = r;
        if (el) {
          if (r.status === 'done') fresh.classList.add('fresh');
          queue.replaceChild(fresh, el);
        } else {
          queue.insertBefore(fresh, prev ? prev.nextSibling : queue.firstChild);
        }
        state.cards.set(r.id, fresh);
        el = fresh;
      }
      prev = el;
    });
    state.cards.forEach(function (el, id) {
      if (!seen.has(id)) { el.remove(); state.cards.delete(id); }
    });
    applyFilter();
  }

  function renderEmpty(scan, live) {
    var box = $('#empty');
    var show = !live && scan.status === 'done' && state.results.length === 0;
    box.hidden = !show;
    if (!show) return;
    var st = scan.stats || {}, p = scan.params || {};
    var bits = [];
    if (typeof st.open_items === 'number') {
      bits.push('Ghosted looked through ' + plural(st.open_items, 'open item') + ' in ' + scan.repo + '.');
      if (st.pull_requests) bits.push(plural(st.pull_requests, 'was a pull request', 'were pull requests') + '.');
      if (st.with_replies) bits.push(plural(st.with_replies, 'issue already had a reply', 'issues already had replies') + '.');
      if (st.too_new) bits.push(plural(st.too_new, 'was', 'were') + ' younger than ' + plural(p.min_age_days || 0, 'day') + '.');
    } else {
      bits.push('Nothing in ' + scan.repo + ' matched.');
    }
    bits.push('Try a smaller “silent for at least” number in Settings, or a repository with a longer-neglected tracker.');
    $('#empty-text').textContent = bits.join(' ');
  }

  /* ================= scan view: one card ================= */
  function byline(r, demo) {
    var who;
    if (!r.author) who = 'someone';
    else if (demo || /[^A-Za-z0-9-]/.test(r.author)) who = '@' + r.author;
    else who = h('a', { href: 'https://github.com/' + r.author, target: '_blank', rel: 'noopener noreferrer', text: '@' + r.author });
    return h('p', { class: 'byline' }, 'Opened by ', who, ' · 0 replies');
  }

  function evidenceList(r, demo) {
    var ul = h('ul', { class: 'evidence', 'aria-label': 'Evidence found in the repository history' });
    if (!r.evidence || !r.evidence.length) {
      ul.appendChild(h('li', { class: 'ev none', text: 'No merged PR, commit or release mentions this issue.' }));
      return ul;
    }
    r.evidence.forEach(function (e) {
      var strength = STRENGTH_LABEL[e.strength] ? e.strength : 'similar';
      var detail = [truncate(e.title, 64), e.date].filter(Boolean).join(' · ');
      var kids = [h('i', { text: STRENGTH_LABEL[strength] }), h('span', { class: 'ref', text: e.ref || 'change' }), detail ? h('small', { text: detail }) : null];
      var attrs = { class: 'ev ' + strength, title: e.note || '' };
      var node = (!demo && safeUrl(e.url))
        ? h.apply(null, ['a', { class: attrs.class, title: attrs.title, href: e.url, target: '_blank', rel: 'noopener noreferrer' }].concat(kids))
        : h.apply(null, ['span', attrs].concat(kids));
      ul.appendChild(h('li', null, node));
    });
    return ul;
  }

  function syncReview(li, r) {
    var ui = li._ui;
    if (!ui) return;
    var review = r.review || 'new';
    var approved = review === 'approved', skipped = review === 'skipped';
    li.setAttribute('data-review', review);
    ui.approve.className = 'btn btn-sm ' + (approved ? 'btn-ok' : 'btn-ink');
    ui.approve.setAttribute('aria-pressed', String(approved));
    clear(ui.approve);
    ui.approve.appendChild(icon('i-check'));
    ui.approve.appendChild(document.createTextNode(approved ? 'Approved' : 'Approve'));
    ui.skip.className = 'btn btn-sm btn-line';
    ui.skip.setAttribute('aria-pressed', String(skipped));
    ui.skip.textContent = skipped ? 'Skipped · undo' : 'Skip';
  }

  function afterReviewChange() { renderSummary(); renderToolbar(); applyFilter(); }

  function setReview(li, r, value) {
    var prev = r.review || 'new';
    r.review = value;
    syncReview(li, r);
    afterReviewChange();
    api('PATCH', '/api/results/' + r.id, { review: value }).catch(function (e) {
      r.review = prev;
      syncReview(li, r);
      afterReviewChange();
      toast("Couldn't save that: " + e.message);
    });
  }

  function copyText(ta) {
    var text = ta.value;
    var fallback = function () {
      var ok = false;
      try { ta.focus(); ta.select(); ok = document.execCommand('copy'); } catch (e) { ok = false; }
      toast(ok ? 'Reply copied. Paste it into the issue yourself.' : 'Select the text and press Ctrl or Cmd + C.');
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast('Reply copied. Paste it into the issue yourself.'); }, fallback);
    } else {
      fallback();
    }
  }

  function regenerate(li, r, button) {
    var pending = state.saves.get(r.id);
    if (pending) pending.cancel();
    var scanId = state.scanId;
    li.classList.add('regen');
    button.disabled = true;
    clear(button);
    button.appendChild(icon('i-refresh'));
    button.appendChild(document.createTextNode('Thinking…'));
    api('POST', '/api/results/' + r.id + '/regenerate', {}).then(function (data) {
      if (scanId !== state.scanId) return;
      var idx = -1;
      state.results.forEach(function (x, i) { if (x.id === r.id) idx = i; });
      if (idx >= 0) state.results[idx] = data.result;
      renderQueue(isLive(state.scan));
      afterReviewChange();
      toast('Fresh draft ready.');
    }, function (e) {
      li.classList.remove('regen');
      button.disabled = false;
      clear(button);
      button.appendChild(icon('i-refresh'));
      button.appendChild(document.createTextNode('Regenerate'));
      toast(e.message);
    });
  }

  function buildAnalysis(main, li, r, scan, demo) {
    var key = VERDICTS[r.verdict] ? r.verdict : 'needs_info';
    main.appendChild(h('div', { class: 'verdict-line v-' + key },
      h('span', { class: 'vtext', text: VERDICTS[key].label }),
      h('span', { class: 'conf', text: (r.confidence || 'medium') + ' confidence' })));
    main.appendChild(h('p', { class: 'reason', text: r.reason || '' }));

    var flags = (r.flags || []).filter(function (f) { return FLAG_TEXT[f]; });
    if (flags.length) main.appendChild(h('ul', { class: 'flags' }, flags.map(function (f) { return h('li', { text: FLAG_TEXT[f] }); })));
    main.appendChild(evidenceList(r, demo));

    var serverEdit = r.edited_reply === null || r.edited_reply === undefined ? null : r.edited_reply;
    var ta = h('textarea', { class: 'reply', id: 'reply-' + r.id, rows: '7', maxlength: '4000', spellcheck: 'true' });
    ta.value = serverEdit !== null ? serverEdit : (r.reply || '');
    var editedTag = h('span', { class: 'edited', text: 'Edited' });
    var savedTag = h('span', { class: 'saved', text: 'Saved' });
    var resetBtn = h('button', { class: 'btn btn-line btn-sm', type: 'button', text: "Reset to the model's draft" });
    var isEdited = function () { return ta.value !== (r.reply || ''); };
    var refreshEdited = function () { editedTag.hidden = !isEdited(); resetBtn.hidden = !isEdited(); };
    refreshEdited();

    var timer = 0, inflight = Promise.resolve(), savedTimer = 0;
    var flush = function () {
      clearTimeout(timer);
      timer = 0;
      state.saves.delete(r.id);
      var text = ta.value;
      var payload = text === (r.reply || '') ? null : text;
      inflight = inflight.then(function () {
        var current = r.edited_reply === null || r.edited_reply === undefined ? null : r.edited_reply;
        if (payload === current) return null;
        return api('PATCH', '/api/results/' + r.id, { reply: payload }).then(function (res) {
          var saved = res.result.edited_reply;
          r.edited_reply = saved === undefined ? null : saved;
          savedTag.classList.add('show');
          clearTimeout(savedTimer);
          savedTimer = setTimeout(function () { savedTag.classList.remove('show'); }, 1800);
        }, function (e) { toast("Couldn't save your edit: " + e.message); });
      });
      return inflight;
    };
    var cancel = function () { clearTimeout(timer); timer = 0; state.saves.delete(r.id); };
    ta.addEventListener('input', function () {
      grow(ta);
      refreshEdited();
      clearTimeout(timer);
      timer = setTimeout(flush, 700);
      state.saves.set(r.id, { flush: flush, cancel: cancel });
    });
    ta.addEventListener('blur', function () { if (state.saves.has(r.id)) flush(); });
    resetBtn.addEventListener('click', function () {
      ta.value = r.reply || '';
      grow(ta);
      refreshEdited();
      flush();
      toast("Back to the model's draft.");
    });

    main.appendChild(h('div', { class: 'reply-head' },
      h('label', { for: 'reply-' + r.id, text: 'Draft reply to the reporter' }),
      h('span', { class: 'state' }, editedTag, savedTag)));
    main.appendChild(ta);

    var approve = h('button', { type: 'button' });
    var skip = h('button', { type: 'button' });
    li._ui = { approve: approve, skip: skip };
    approve.addEventListener('click', function () { setReview(li, r, r.review === 'approved' ? 'new' : 'approved'); });
    skip.addEventListener('click', function () { setReview(li, r, r.review === 'skipped' ? 'new' : 'skipped'); });
    var copy = h('button', { class: 'btn btn-line btn-sm', type: 'button', on: { click: function () { copyText(ta); } } }, icon('i-copy'), 'Copy reply');
    var actions = [approve, skip, copy];
    if ((scan.params || {}).backend !== 'rules') {
      var regen = h('button', { class: 'btn btn-line btn-sm', type: 'button', title: 'Ask the model again. Your edits to this draft are replaced.' }, icon('i-refresh'), 'Regenerate');
      regen.addEventListener('click', function () { regenerate(li, r, regen); });
      actions.push(regen);
    }
    actions.push(resetBtn);
    actions.push(h('span', { class: 'spacer' }));
    if (!demo && safeUrl(r.url)) {
      actions.push(h('a', { class: 'btn btn-line btn-sm', href: r.url, target: '_blank', rel: 'noopener noreferrer' }, 'Open on GitHub', icon('i-external')));
    }
    main.appendChild(h('div', { class: 'card-actions' }, actions));
    syncReview(li, r);

    if (r.body && r.body.trim()) {
      main.appendChild(h('details', { class: 'orig' }, h('summary', { text: 'What the reporter wrote' }), h('pre', { text: r.body })));
    } else {
      main.appendChild(h('p', { class: 'muted orig', text: 'The reporter left the description empty.' }));
    }
    window.requestAnimationFrame(function () { grow(ta); });
  }

  function buildCard(r, scan) {
    var demo = !!scan.demo;
    var live = isLive(scan);
    var li = h('li', {
      class: 'card' + (r.status === 'pending' ? ' pending' : '') + (r.status === 'error' ? ' errored' : ''),
      'data-id': r.id, 'data-status': r.status, 'data-verdict': r.verdict || '', 'data-review': r.review || 'new',
      'aria-labelledby': 'ct-' + r.id
    });
    var rail = h('div', { class: 'card-rail' },
      h('div', { class: 'age' }, spanParts(r.age_days).map(function (p) { return h('span', { text: p }); })),
      h('div', { class: 'age-label', text: 'of silence' }),
      h('div', { class: 'age-sub', text: 'since ' + fmtDate(r.created_at) }));

    var main = h('div', { class: 'card-main' });
    if (r.status === 'done') main.appendChild(sticker(r.verdict, 'card-sticker'));
    main.appendChild(h('p', { class: 'kicker', text: 'Issue #' + r.number }));
    var titleNode = (!demo && safeUrl(r.url))
      ? h('a', { href: r.url, target: '_blank', rel: 'noopener noreferrer', text: r.title })
      : r.title;
    main.appendChild(h('h3', { class: 'card-title', id: 'ct-' + r.id }, titleNode));
    main.appendChild(byline(r, demo));
    if (r.labels && r.labels.length) {
      main.appendChild(h('ul', { class: 'labels', 'aria-label': 'Labels' }, r.labels.slice(0, 8).map(function (l) { return h('li', { text: l }); })));
    }

    if (r.status === 'pending') {
      main.appendChild(live
        ? h('p', { class: 'waiting' }, h('span', { class: 'dot wait' }), 'Waiting for its turn…')
        : h('p', { class: 'waiting', text: 'Not read yet. Press Resume to carry on.' }));
    } else if (r.status === 'error') {
      main.appendChild(h('p', { class: 'errtext', text: "Ghosted couldn't read this one: " + (r.error || 'unknown error') }));
      main.appendChild(h('p', { class: 'muted', text: 'Everything else is saved. Press Retry to try this one again.' }));
    } else {
      buildAnalysis(main, li, r, scan, demo);
    }
    li.appendChild(rail);
    li.appendChild(main);
    return li;
  }

  /* ================= saving edits ================= */
  function flushAll() {
    var jobs = [];
    state.saves.forEach(function (entry) { jobs.push(entry.flush()); });
    return Promise.all(jobs);
  }

  /* ================= wiring ================= */
  function bind() {
    $('#scan-form').addEventListener('submit', function (ev) { ev.preventDefault(); startScan(false); });
    $('#try-demo').addEventListener('click', function () { startScan(true); });
    $('#repo').addEventListener('input', hideFormError);

    $('#toggle-settings').addEventListener('click', function () {
      var panel = $('#settings');
      panel.hidden = !panel.hidden;
      this.setAttribute('aria-expanded', String(!panel.hidden));
    });
    $$('input[name="backend"]').forEach(function (radio) { radio.addEventListener('change', onBackendChange); });
    $('#base-url').addEventListener('input', function () {
      state.settings.urls[currentBackend()] = $('#base-url').value.trim();
      persist();
      debounceCheck();
    });
    $('#model').addEventListener('input', function () {
      state.settings.models[currentBackend()] = $('#model').value.trim();
      persist();
      updateModelHint();
    });
    $('#refresh-models').addEventListener('click', checkModels);
    $('#max-issues').addEventListener('change', function () { state.settings.max = $('#max-issues').value; persist(); });
    $('#min-age').addEventListener('change', function () { state.settings.minAge = $('#min-age').value; persist(); });
    $('#order').addEventListener('change', function () { state.settings.order = $('#order').value; persist(); });

    $('#brand').addEventListener('click', function (ev) {
      if (state.view === 'home' && (window.location.hash === '' || window.location.hash === '#/')) {
        ev.preventDefault();
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    });
    $('#nav-cta').addEventListener('click', function (ev) {
      state.focusRepo = true;
      if (state.view === 'home' && (window.location.hash === '' || window.location.hash === '#/')) {
        ev.preventDefault();
        state.focusRepo = false;
        window.scrollTo({ top: 0, behavior: 'smooth' });
        $('#repo').focus({ preventScroll: true });
      }
    });

    $$('.filter').forEach(function (btn) {
      btn.addEventListener('click', function () {
        state.filter = btn.getAttribute('data-filter');
        renderToolbar();
        applyFilter();
      });
    });
    $('#hide-skipped').addEventListener('change', function () { state.hideSkipped = this.checked; applyFilter(); });
    $('#only-approved').addEventListener('change', function () { state.onlyApproved = this.checked; renderToolbar(); });
    ['md', 'csv', 'json'].forEach(function (fmt) {
      $('#dl-' + fmt).addEventListener('click', function (ev) {
        if (!state.saves.size) return;           // nothing waiting to be saved: let the browser download
        ev.preventDefault();
        var href = this.href;
        flushAll().then(function () { window.location.href = href; });
      });
    });
    $('#cancel').addEventListener('click', function () {
      state.cancelling = true;
      renderProgress(state.scan, true);
      api('POST', '/api/scans/' + state.scanId + '/cancel', {}).catch(function (e) {
        state.cancelling = false;
        if (state.scan) renderProgress(state.scan, isLive(state.scan));
        toast(e.message);
      });
    });

    window.addEventListener('hashchange', route);
    document.addEventListener('visibilitychange', function () { if (document.hidden) flushAll(); });
    window.addEventListener('resize', growAll);
  }

  function fitPlaceholder() {
    // The long hint is cut off on a phone, so shorten it there.
    if (!window.matchMedia) return;
    var query = window.matchMedia('(max-width: 720px)');
    var apply = function () { $('#repo').placeholder = query.matches ? 'owner/name or URL' : 'owner/name or a github.com link'; };
    apply();
    try { query.addEventListener('change', apply); } catch (e) { /* old Safari: the first value is fine */ }
  }

  function init() {
    bind();
    fitPlaceholder();
    api('GET', '/api/config').then(function (cfg) {
      state.config = cfg;
    }, function () { setOffline(true); }).then(function () {
      loadSettings();
      applySettingsToForm();
      route();
      checkModels();
    });
  }

  init();
})();
