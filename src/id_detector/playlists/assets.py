"""Self-contained result-page controls; no requests when opened offline."""

from html import escape

PLAYLIST_CSS = """
.pl-actions{display:inline-flex;gap:4px;position:relative}
.pl-like,.pl-add{border:1px solid var(--line2);border-radius:6px;background:var(--card2);
color:var(--muted);padding:4px 8px;cursor:pointer}.pl-like.on{color:var(--pink)}
.pl-add:hover,.pl-like:hover{border-color:var(--accent)}
.pl-menu{position:fixed;z-index:25;width:250px;max-width:95vw;padding:10px;
max-height:60vh;overflow-y:auto;
background:var(--card2);border:1px solid var(--line2);border-radius:10px;
box-shadow:0 12px 30px #0008;white-space:normal}
.pl-menu button,.pl-menu input{display:block;width:100%;margin:4px 0;padding:7px;
background:var(--card2);color:var(--fg);border:1px solid var(--line2);border-radius:5px}
"""

PLAYLIST_JS = r"""
document.addEventListener('DOMContentLoaded', function(){
  const actions = Array.from(document.querySelectorAll('.pl-actions'));
  if(!['http:', 'https:'].includes(location.protocol)){
    actions.forEach(el => el.hidden = true);
    actions.forEach(el => el.style.display = 'none');
    return;
  }
  const match = location.pathname.match(/^\/([0-9a-f]+)\/([0-9a-f]+)\/present\//);
  if(!match){
    actions.forEach(el => el.querySelectorAll('button').forEach(b => b.disabled = true));
    return;
  }
  let cached = {playlists:[], liked:[], membership:{}}, token, menu;
  function toast(message){
    const el = document.getElementById('toast');
    if(el){ el.textContent = message; el.classList.add('show');
      setTimeout(() => el.classList.remove('show'), 2400); }
  }
  async function json(url, options){
    const response = await fetch(url, options);
    if(!response.ok) throw new Error('request failed');
    return response.json();
  }
  function paint(){
    actions.forEach(el => {
      const on = cached.liked.includes(el.dataset.candidateId);
      const heart = el.querySelector('.pl-like');
      heart.classList.toggle('on', on); heart.setAttribute('aria-pressed', String(on));
    });
  }
  async function refresh(){ cached = await json('/playlists/state'); paint(); }
  async function post(route, fields){
    if(!token) token = (await json('/csrf')).token;
    const result = await json(route, {method:'POST', headers:{'X-CSRF-Token':token,
      'Content-Type':'application/x-www-form-urlencoded'}, body:new URLSearchParams(fields)});
    if(!result.ok) throw new Error('save failed');
    return result;
  }
  function close(){ if(menu) menu.remove(); menu = null; }
  actions.forEach(el => {
    // Prevent the result row's keyboard seek handler from consuming control keys.
    el.addEventListener('keydown', e => { e.stopPropagation(); if(e.key === 'Escape') close(); });
    el.addEventListener('click', e => e.stopPropagation());
    const fields = {source_key:match[1], media_key:match[2],
      episode_id:el.dataset.episodeId, candidate_id:el.dataset.candidateId};
    const heart = el.querySelector('.pl-like');
    heart.addEventListener('click', async () => {
      heart.disabled = true;
      try{
        const result = await post('/playlists/like', fields);
        cached.liked = cached.liked.filter(id => id !== fields.candidate_id);
        if(result.liked) cached.liked.push(fields.candidate_id);
        paint(); toast(result.liked ? 'Saved to Likes' : 'Removed from Likes');
        refresh().catch(() => {});
      }catch(_){ toast("Couldn't save"); }finally{ heart.disabled = false; }
    });
    el.querySelector('.pl-add').addEventListener('click', () => {
      close(); menu = document.createElement('div'); menu.className = 'pl-menu';
      menu.addEventListener('click', e => e.stopPropagation());
      menu.addEventListener('keydown', e => { if(e.key === 'Escape') close(); });
      const currentMenu = menu;
      async function add(id, name){
        currentMenu.querySelectorAll('button').forEach(b => b.disabled = true);
        try{
          await post('/playlists/add', {...fields, playlist_id:id, name:name || ''});
          close(); toast('Saved to playlist'); refresh().catch(() => {});
        }catch(_){ toast("Couldn't save");
          currentMenu.querySelectorAll('button').forEach(b => b.disabled = false); }
      }
      cached.playlists.forEach(p => {
        const button = document.createElement('button'); button.type = 'button';
        const member = (cached.membership[fields.candidate_id] || []).includes(p.id);
        button.textContent = p.name + (member ? ' ✓' : '');
        button.addEventListener('click', () => add(p.id)); menu.appendChild(button);
      });
      const input = document.createElement('input'); input.placeholder = 'Playlist name';
      input.maxLength = 80; input.setAttribute('aria-label', 'New playlist name');
      const create = document.createElement('button'); create.type = 'button';
      create.textContent = '＋ New playlist';
      create.addEventListener('click', () => {
        if(input.value.trim()) add('__new__', input.value);
      });
      input.addEventListener('keydown', e => {
        if(e.key === 'Enter'){ e.preventDefault(); create.click(); }
      });
      menu.append(input, create); document.body.appendChild(menu);
      const rect = el.getBoundingClientRect();
      menu.style.left = Math.max(8, Math.min(rect.right - 250, innerWidth - 258)) + 'px';
      menu.style.top = Math.max(8, Math.min(rect.bottom + 4,
        innerHeight - menu.offsetHeight - 8)) + 'px';
    });
  });
  document.addEventListener('click', close);
  window.addEventListener('resize', close);
  refresh().catch(() => {});
});
"""


def row_actions_html(entry: dict) -> str:
    candidate = escape(str(entry.get("candidate_id") or entry.get("episode_id") or ""))
    episode = escape(str(entry.get("episode_id") or ""))
    return (
        f'<span class="pl-actions" data-candidate-id="{candidate}" data-episode-id="{episode}">'
        '<button type="button" class="pl-like" title="Save to Likes" '
        'aria-label="Save to Likes">♥</button>'
        '<button type="button" class="pl-add" title="Add to playlist" '
        'aria-label="Add to playlist">＋</button></span>'
    )
