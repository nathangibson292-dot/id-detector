(function(){
  var form = document.getElementById('genre-form'); if(!form) return;
  var input = document.getElementById('gq');
  var errorBox = document.getElementById('genre-error');
  var live = document.getElementById('genre-live');
  var msg = document.getElementById('g-msg');
  var timeBox = document.getElementById('g-time');
  var download = document.getElementById('g-download');
  var resultBox = document.getElementById('genre-result');
  var failBox = document.getElementById('genre-fail');
  var csrf = form.getAttribute('data-csrf') || '';
  var current = null, polling = null, lastInput = '';
  var PLATFORM = {soundcloud: 'SoundCloud', youtube: 'YouTube', spotify: 'Spotify'};
  var SOURCE = {heard: 'Heard', discogs: 'Discogs', lastfm: 'Last.fm'};

  function el(tag, cls, text){
    var node = document.createElement(tag);
    if(cls) node.className = cls;
    if(text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function pct(x){ return Math.round((x || 0) * 100) + '%'; }
  function clock(seconds){
    if(seconds === null || seconds === undefined) return '';
    seconds = Math.round(seconds);
    var m = Math.floor(seconds / 60), s = seconds % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }
  function setError(text){ errorBox.textContent = text || ''; }

  function stages(steps){
    var list = live.querySelector('.stages');
    steps.forEach(function(step){
      if(step.key === 'model') return;
      var li = list.querySelector('[data-step="' + step.key + '"]'); if(!li) return;
      li.className = 'stage' + (step.state === 'done' || step.state === 'skipped' ? ' done'
        : step.state === 'active' ? ' active' : step.state === 'failed' ? ' failed' : '');
      li.setAttribute('aria-current', step.state === 'active' ? 'step' : 'false');
    });
  }

  function showStatus(j){
    live.hidden = false;
    stages(j.steps || []);
    var model = (j.steps || []).filter(function(s){ return s.key === 'model'; })[0];
    if(model && model.state === 'active' && j.download){
      download.hidden = false;
      var done = j.download.done || 0, total = j.download.total || 1;
      download.querySelector('.bar span').style.width = Math.round(100 * done / total) + '%';
      download.querySelector('small').textContent = (done / 1e6).toFixed(1) + ' of '
        + (total / 1e6).toFixed(1) + ' MB';
    } else { download.hidden = true; }
    msg.querySelector('b').textContent = j.message || '';
    timeBox.textContent = j.elapsed ? j.elapsed.toFixed(1) + ' s' : '';
  }

  function sourceBadges(sources){
    var wrap = el('span', 'g-srcs');
    (sources || []).forEach(function(s){ wrap.appendChild(el('span', 'g-src ' + s, SOURCE[s] || s)); });
    return wrap;
  }

  function trackBlock(r, candidates){
    var t = r.track || {};
    var box = el('div', 'g-track');
    if(t.art){
      var img = el('img', 'g-art'); img.src = '/genre/art/' + t.art; img.alt = '';
      img.width = 88; img.height = 88; box.appendChild(img);
    } else {
      var ph = el('div', 'g-art ph'); var eq = el('span', 'eq');
      for(var i = 0; i < 4; i++) eq.appendChild(el('i'));
      ph.appendChild(eq); ph.setAttribute('aria-hidden', 'true'); box.appendChild(ph);
    }
    var info = el('div');
    info.appendChild(el('p', 'eyebrow', r.cached ? 'Analysed (saved answer)' : 'Analysed'));
    info.appendChild(el('h2', '', t.title || 'Untitled'));
    var by = el('p', 'g-by');
    if(t.uploader) by.appendChild(el('span', '', t.uploader));
    var chip = el('span', 'chip plat-' + (t.platform || 'file'));
    chip.appendChild(el('span', 'pd')); chip.appendChild(document.createTextNode(PLATFORM[t.platform] || 'Web'));
    by.appendChild(chip);
    if(t.duration) by.appendChild(el('span', '', clock(t.duration)));
    if(t.url && /^https:\/\//.test(t.url)){
      var a = el('a', '', 'Open on ' + (PLATFORM[t.platform] || 'the site'));
      a.href = t.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; by.appendChild(a);
    }
    info.appendChild(by);
    var names = r.lookup_names || {};
    if(names.title && (names.artist || '') + names.title !== (t.title || '')){
      info.appendChild(el('p', 'g-by', 'Looked up as: ' + (names.artist ? names.artist + ' – ' : '') + names.title));
    }
    box.appendChild(info);
    if(candidates && candidates.length > 1){
      var pick = el('details', 'g-pick');
      pick.appendChild(el('summary', '', 'Not the right track? Pick another match'));
      var list = el('ul');
      candidates.forEach(function(c){
        var li = el('li'); var b = el('button'); b.type = 'button';
        b.appendChild(el('b', '', c.title || c.url));
        b.appendChild(el('small', '', (c.uploader ? c.uploader + ' · ' : '') + (PLATFORM[c.platform] || 'Web')));
        b.appendChild(el('span', '', c.duration ? clock(c.duration) : ''));
        if(c.url === t.url) b.setAttribute('aria-current', 'true');
        b.addEventListener('click', function(){ start(lastInput || input.value, c.url); });
        li.appendChild(b); list.appendChild(li);
      });
      pick.appendChild(list); box.appendChild(pick);
    }
    return box;
  }

  function answerBlock(r){
    var box = el('div', 'g-answer');
    var genres = r.genres || [];
    var top = genres[0] || {name: 'Unknown', share: 0};
    var kicker = el('p', 'g-kicker');
    var parentChip = el('span', 'chip'); parentChip.appendChild(el('b', '', r.parent || ''));
    parentChip.appendChild(document.createTextNode(' parent genre'));
    kicker.appendChild(parentChip);
    var agree = el('span', 'st g-' + (r.agreement || 'heard-only'),
      {agree: 'Sources agree', partial: 'Mostly agree', disagree: 'Sources disagree', 'heard-only': 'Listening only'}[r.agreement] || '');
    kicker.appendChild(agree);
    box.appendChild(kicker);
    var h = el('h3', 'g-top'); h.appendChild(el('span', 'grad', top.name));
    h.appendChild(el('small', '', pct(top.share)));
    box.appendChild(h);
    box.appendChild(el('p', 'g-summary', r.summary || ''));
    var bars = el('ol', 'g-bars');
    bars.setAttribute('aria-label', 'Top genres');
    genres.slice(0, 3).forEach(function(g){
      var li = el('li'); var head = el('div', 'g-bar-h');
      head.appendChild(el('b', '', g.name)); head.appendChild(el('span', '', pct(g.share)));
      li.appendChild(head);
      var bar = el('div', 'g-track-bar'); var fill = el('i');
      fill.style.width = Math.max(2, Math.round((g.share || 0) * 100)) + '%';
      bar.appendChild(fill); bar.setAttribute('aria-hidden', 'true'); li.appendChild(bar);
      li.appendChild(sourceBadges(g.sources));
      bars.appendChild(li);
    });
    box.appendChild(bars);
    return box;
  }

  function sideBlock(r){
    var box = el('div', 'g-side');
    var bpm = el('div', 'g-tile'); bpm.appendChild(el('p', 'k', 'Tempo'));
    var v = el('p', 'v');
    if(r.bpm){ v.appendChild(document.createTextNode(String(r.bpm.value))); v.appendChild(el('small', '', 'BPM')); }
    else v.appendChild(document.createTextNode('—'));
    bpm.appendChild(v);
    if(r.bpm && r.bpm.confidence < 0.15) bpm.appendChild(el('p', 'note', 'Not certain — another tempo scored almost as well.'));
    box.appendChild(bpm);
    var key = el('div', 'g-tile'); key.appendChild(el('p', 'k', 'Key'));
    var kv = el('p', 'v');
    if(r.key){ kv.appendChild(document.createTextNode(r.key.name)); kv.appendChild(el('span', 'g-cam', r.key.camelot)); }
    else kv.appendChild(document.createTextNode('—'));
    key.appendChild(kv);
    if(r.key && r.key.alternative){
      key.appendChild(el('p', 'note', 'Not certain — could also be ' + r.key.alternative.name + ' (' + r.key.alternative.camelot + ').'));
    }
    box.appendChild(key);
    var ev = el('div', 'g-tile'); ev.appendChild(el('p', 'k', 'Evidence'));
    var list = el('ul', 'g-ev');
    var heardTop = ((r.model_ranking || [])[0] || {});
    var rows = [['heard', 'Heard', heardTop.name ? 'sounds most like ' + heardTop.name : 'nothing clear']];
    [['discogs', 'Discogs'], ['lastfm', 'Last.fm']].forEach(function(pair){
      var src = (r.sources || {})[pair[0]] || {status: 'off'};
      rows.push([pair[0], pair[1], src.status === 'off' ? 'off (no key)' : (src.detail || src.status)]);
    });
    rows.forEach(function(row){
      var li = el('li', row[2].indexOf('off') === 0 ? 'off' : '');
      li.appendChild(el('b', 'g-src ' + row[0], row[1])); li.appendChild(el('span', '', row[2]));
      list.appendChild(li);
    });
    ev.appendChild(list); box.appendChild(ev);
    return box;
  }

  function whyBlock(r){
    var d = el('details', 'g-why');
    d.appendChild(el('summary', '', 'How this was worked out'));
    var inner = el('div', 'g-why-in');
    var heard = el('div'); heard.appendChild(el('h4', '', 'Heard (the model’s own ranking)'));
    var list = el('ol', 'g-model');
    var maxP = ((r.model_ranking || [])[0] || {}).probability || 1;
    (r.model_ranking || []).slice(0, 8).forEach(function(m){
      var li = el('li'); li.appendChild(el('span', '', m.name));
      var mini = el('span', 'mini'); var i = el('i'); i.style.width = Math.round(100 * m.probability / maxP) + '%';
      mini.appendChild(i); li.appendChild(mini); li.appendChild(el('span', '', pct(m.probability)));
      list.appendChild(li);
    });
    heard.appendChild(list);
    var segs = (r.segments || []).map(function(s){ return clock(s.start); }).join(', ');
    if(segs) heard.appendChild(el('p', '', 'Listened to 15 s at ' + segs + '.'));
    inner.appendChild(heard);
    [['discogs', 'Discogs'], ['lastfm', 'Last.fm']].forEach(function(pair){
      var src = (r.sources || {})[pair[0]] || {status: 'off'};
      var col = el('div'); col.appendChild(el('h4', '', pair[1]));
      var line = el('p');
      if(src.status === 'off'){
        line.textContent = 'Off. ';
        var how = el('span', '', pair[0] === 'discogs' ? 'Add DISCOGS_API_TOKEN to the .env file to turn it on.' : 'Add LASTFM_API_KEY to the .env file to turn it on.');
        line.appendChild(how);
      } else {
        line.appendChild(el('b', '', src.status === 'ok' ? 'Found: ' : src.status === 'error' ? 'Unavailable: ' : 'Nothing useful: '));
        line.appendChild(document.createTextNode(src.detail || ''));
      }
      col.appendChild(line);
      if(src.raw && src.raw.length){
        var tags = el('div', 'g-tags'); src.raw.forEach(function(t){ tags.appendChild(el('span', '', t)); });
        col.appendChild(tags);
      }
      if(src.status === 'ok') col.appendChild(el('p', '', 'Weight in the answer: ' + ((r.weights || {})[pair[0]] || 0)));
      inner.appendChild(col);
    });
    var rule = el('div', 'g-rule');
    rule.appendChild(el('h4', '', 'The rule'));
    var p = el('p');
    p.appendChild(document.createTextNode('Score = ('));
    p.appendChild(el('code', '', 'heard + ' + ((r.weights || {}).discogs || 0) + ' × Discogs + ' + ((r.weights || {}).lastfm || 0) + ' × Last.fm'));
    p.appendChild(document.createTextNode(') ÷ (1 + the two weights). Heard = the model’s probabilities squared and shared out to 100%. Discogs counts up to 0.7 and Last.fm up to 0.5, scaled by how well the record matched; broad styles like House count half. BPM and key are measured from the same audio.'));
    rule.appendChild(p);
    var tm = r.timings || {};
    if(tm.total) rule.appendChild(el('p', '', 'Took ' + tm.total.toFixed(1) + ' s (finding the track ' + (tm.find || 0).toFixed(1) + ' s, listening ' + (tm.listen || 0).toFixed(1) + ' s).' + (r.cached ? ' This is the saved answer, shown instantly.' : '')));
    inner.appendChild(rule);
    d.appendChild(inner);
    return d;
  }

  function showResult(j){
    var r = j.result; if(!r) return;
    resultBox.textContent = '';
    var card = el('article', 'g-card');
    card.setAttribute('aria-label', 'Genre answer');
    card.appendChild(trackBlock(r, j.candidates && j.candidates.length ? j.candidates : r.candidates));
    var body = el('div', 'g-body'); body.appendChild(answerBlock(r)); body.appendChild(sideBlock(r));
    card.appendChild(body);
    card.appendChild(whyBlock(r));
    resultBox.appendChild(card);
    resultBox.hidden = false;
    var heading = resultBox.querySelector('.g-top');
    if(heading){ heading.setAttribute('tabindex', '-1'); heading.focus({preventScroll: true}); }
  }

  function showFailure(j){
    failBox.hidden = false;
    failBox.querySelector('p').textContent = j.error || 'The check stopped.';
    msg.querySelector('b').textContent = 'Stopped';
  }

  function poll(id){
    fetch('/genre/check/' + id, {headers: {'Accept': 'application/json'}}).then(function(r){
      if(!r.ok) throw new Error('status ' + r.status); return r.json();
    }).then(function(j){
      if(current !== id) return;
      showStatus(j);
      if(j.status === 'done'){ form.removeAttribute('aria-busy'); showResult(j); setTimeout(function(){ live.hidden = true; }, 600); return; }
      if(j.status === 'failed'){ form.removeAttribute('aria-busy'); showFailure(j); return; }
      polling = setTimeout(function(){ poll(id); }, 600);
    }).catch(function(){ if(current === id) polling = setTimeout(function(){ poll(id); }, 2000); });
  }

  function start(text, pick){
    text = (text || '').trim();
    if(!text){ setError('Type an artist and title, or paste a link.'); input.focus(); return; }
    setError(''); failBox.hidden = true; resultBox.hidden = true; lastInput = text;
    if(polling) clearTimeout(polling);
    live.hidden = false; download.hidden = true;
    stages([{key: 'find', state: 'active'}, {key: 'listen', state: 'waiting'}, {key: 'lookup', state: 'waiting'}, {key: 'done', state: 'waiting'}]);
    msg.querySelector('b').textContent = 'Starting'; timeBox.textContent = '';
    form.setAttribute('aria-busy', 'true');
    try { history.replaceState(null, '', '/genre?q=' + encodeURIComponent(text)); } catch(e) {}
    var payload = {input: text}; if(pick) payload.pick = pick;
    fetch('/genre/check', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify(payload)}).then(function(r){
      return r.json().then(function(j){ return {ok: r.ok, j: j}; });
    }).then(function(res){
      if(!res.ok){ live.hidden = true; form.removeAttribute('aria-busy'); setError(res.j.error || 'That did not work.'); return; }
      current = res.j.id; showStatus(res.j);
      if(res.j.status === 'done'){ form.removeAttribute('aria-busy'); showResult(res.j); live.hidden = true; return; }
      if(res.j.status === 'failed'){ form.removeAttribute('aria-busy'); showFailure(res.j); return; }
      poll(current);
    }).catch(function(){ live.hidden = true; form.removeAttribute('aria-busy'); setError('The ID’er server did not answer. Is it still running?'); });
  }

  form.addEventListener('submit', function(event){ event.preventDefault(); start(input.value); });
  var params = new URLSearchParams(window.location.search);
  var q = params.get('q');
  var sameSite = document.referrer && document.referrer.indexOf(window.location.origin + '/') === 0;
  if(q){ input.value = q; if(sameSite || form.getAttribute('data-autostart') === '1') start(q); else input.focus(); }
})();
