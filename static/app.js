const $ = s => document.querySelector(s);
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
let state, currentBooks = [], selection = new Set(), loadingPicks = 0, lastSync = null, librarySignature = '';
async function api(path, data, raw = false) {
  const options = data === undefined ? {} : {method: 'POST', headers: {'X-Selector-Request': '1', 'Content-Type': raw ? 'text/csv' : 'application/json'}, body: raw ? data : JSON.stringify(data)};
  const response = await fetch(path, options); const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The request did not complete.');
  return result;
}
function message(text, error = false) { $('#message').hidden = false; $('#message').textContent = text; $('#message').className = error ? 'error' : ''; }
async function action(button, fn) { button.disabled = true; try { await fn(); } catch (error) { message(error.message, true); } finally { button.disabled = false; } }
function updateCount() { $('#selected-count').textContent = selection.size; $('#queue').disabled = selection.size === 0; }
function showPanel(name) { document.querySelectorAll('.panel').forEach(p => p.hidden = p.id !== name); document.querySelectorAll('.tabs button').forEach(b => {b.classList.toggle('active', b.dataset.panel === name); b.setAttribute('aria-pressed', String(b.dataset.panel === name));}); }
document.querySelectorAll('.tabs button').forEach(b => b.addEventListener('click', () => showPanel(b.dataset.panel)));
function fillProfile() {
  const form = $('#profile-form'); for (const key of ['likes', 'avoids', 'preferred_authors', 'author_preference', 'max_hours']) form.elements[key].value = state.profile[key];
  $('#dimensions').replaceChildren();
  for (const [key, label] of Object.entries(state.dimensions)) {
    const wrap = el('div', 'dimension'), name = el('label', '', label), range = el('input'), value = el('output', '', state.profile.weights[key]);
    range.type = 'range'; range.min = '-3'; range.max = '3'; range.value = state.profile.weights[key]; range.name = key; range.id = 'weight-' + key; name.htmlFor = range.id;
    range.addEventListener('input', () => value.textContent = range.value); wrap.append(name, range, value); $('#dimensions').append(wrap);
  }
}
async function refreshState(initial = false) {
  state = await api('/api/state');
  $('#count-library').textContent = state.stats.catalogue.toLocaleString(); $('#count-history').textContent = state.stats.history.toLocaleString(); $('#count-finished').textContent = state.stats.abs_finished; $('#count-feedback').textContent = state.stats.feedback;
  const timestamp = state.last_sync ? new Date(state.last_sync).toLocaleString() : 'not synced yet';
  $('#sync-status').textContent = (state.sync.running ? state.sync.phase + '…' : state.sync.error || state.sync.phase) + ' · Last sync: ' + timestamp;
  $('#sync').disabled = state.sync.running;
  $('#import-status').textContent = state.goodreads_import ? `${state.goodreads_import.rows.toLocaleString()} entries seeded on ${new Date(state.goodreads_import.at).toLocaleDateString()}.` : 'No Goodreads seed yet. Add your export below.';
  $('#connection-status').textContent = state.username ? `Connected to ABS as ${state.username}. ${state.stats.in_progress} books in progress.` : state.configured ? 'Credentials configured. Waiting for first successful sync.' : 'Add ABS_URL and ABS_TOKEN to .env, then restart the app.';
  $('#question-count').textContent = state.questions.length;
  if (initial) fillProfile();
  renderQuestions();
  const signature = JSON.stringify(state.libraries);
  if (signature !== librarySignature) {
    librarySignature = signature; const old = $('#library').value; $('#library').replaceChildren();
    state.libraries.forEach(l => {const option = el('option', '', l.name); option.value = l.id; $('#library').append(option);});
    if (state.libraries.some(l => l.id === old)) $('#library').value = old;
    await refreshPlaylists();
  }
  if (initial || state.last_sync !== lastSync) { lastSync = state.last_sync; await loadPicks(); }
}
async function refreshPlaylists() {
  const old = $('#playlist').value; $('#playlist').replaceChildren(); const create = el('option', '', 'Create or use AutomaticBookSelector'); create.value = ''; $('#playlist').append(create);
  if (!$('#library').value) return;
  try { const result = await api('/api/playlists?library=' + encodeURIComponent($('#library').value)); result.playlists.forEach(p => { const o = el('option', '', `${p.name} (${p.count})`); o.value = p.id; $('#playlist').append(o); });
    if (result.playlists.some(p => p.id === old)) $('#playlist').value = old;
    else { const ours = result.playlists.find(p => p.name === 'AutomaticBookSelector'); if (ours) $('#playlist').value = ours.id; }
  } catch (error) { message(error.message, true); }
}
async function loadPicks() {
  const ticket = ++loadingPicks;
  const result = await api('/api/recommendations?library=' + encodeURIComponent($('#library').value) + '&search=' + encodeURIComponent($('#search').value));
  if (ticket !== loadingPicks) return;
  currentBooks = result.books; $('#eligible').textContent = result.eligible ? `${result.eligible.toLocaleString()} unread works · showing ${result.books.length}` : '';
  $('#book-grid').replaceChildren();
  if (!result.books.length) $('#book-grid').append(el('div', 'empty', state?.sync.running ? 'Your catalogue is syncing. First picks will appear shortly.' : 'No matches yet. Try another search, loosen your length preference, or sync your library.'));
  result.books.forEach((book, index) => {
    const article = el('article', 'book'), top = el('div', 'book-top'), symbol = el('div', 'book-symbol', String(index + 1).padStart(2, '0'));
    symbol.setAttribute('aria-hidden', 'true'); const heading = el('div'); heading.append(el('h3', '', book.title), el('p', 'book-author', book.author || 'Unknown author'));
    const check = el('label', 'check'), checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.checked = selection.has(book.id); checkbox.setAttribute('aria-label', `Select ${book.title}`);
    checkbox.addEventListener('change', () => {checkbox.checked ? selection.add(book.id) : selection.delete(book.id); updateCount();}); check.append(checkbox); top.append(symbol, heading, check);
    const content = el('div', 'book-content'), meta = el('div', 'book-meta'); if (book.duration) meta.append(el('span', 'chip', `${(book.duration / 3600).toFixed(1)} hours`));
    book.genres.slice(0, 2).forEach(g => meta.append(el('span', 'chip', g))); content.append(meta, el('p', 'reason-label', 'WHY IT MAY BE YOUR KIND OF BOOK'));
    const reasons = el('ul', 'reasons'); book.reasons.forEach(r => reasons.append(el('li', '', r))); content.append(reasons);
    const details = el('details'); details.append(el('summary', '', 'Read the blurb & listening notes'), el('p', 'blurb', book.description || 'No description available.'));
    if (book.narrator) details.append(el('p', 'blurb', 'Narrated by ' + book.narrator)); book.cautions.forEach(c => details.append(el('p', 'blurb', c))); content.append(details);
    const actions = el('div', 'book-actions'), note = el('button', '', 'Leave feedback'), hide = el('button', '', 'Less like this');
    note.addEventListener('click', () => openFeedback(book)); hide.addEventListener('click', () => action(hide, async () => { await api('/api/feedback', {book_id: book.id, dismissed: true}); selection.delete(book.id); updateCount(); await loadPicks(); message('Hidden this book. Add feedback to explain which themes you want less of.'); }));
    actions.append(note, hide); if (book.queued) actions.append(el('span', '', 'Added to ABS')); content.append(actions); article.append(top, content); $('#book-grid').append(article);
  }); updateCount();
}
function renderQuestions() {
  $('#questions').replaceChildren();
  if (!state.questions.length) $('#questions').append(el('div', 'empty', 'All caught up. When ABS marks a book finished, you can tell your shelf what worked for you here.'));
  for (const book of state.questions) {
    const card = el('article', 'question'), info = el('div'); info.append(el('h3', '', book.title), el('p', '', `${book.author}${book.recommended ? ' · One of your selected recommendations' : ' · Finished in ABS'}`));
    const actions = el('div', 'question-buttons'), feedback = el('button', '', 'How did it feel?'), skip = el('button', 'secondary', 'Skip');
    feedback.addEventListener('click', () => openFeedback(book)); skip.addEventListener('click', () => action(skip, async () => {await api('/api/questions/dismiss', {book_id: book.id}); await refreshState();}));
    actions.append(feedback, skip); card.append(info, actions); $('#questions').append(card);
  }
}
function openFeedback(book) { const form = $('#feedback-form'); form.reset(); form.elements.book_id.value = book.id; $('#feedback-title').textContent = book.title; $('#feedback-dialog').showModal(); }
$('#close-dialog').addEventListener('click', () => $('#feedback-dialog').close());
$('#feedback-form').addEventListener('submit', event => { event.preventDefault(); const f = event.currentTarget; action(f.querySelector('[type=submit]'), async () => { await api('/api/feedback', {book_id: f.elements.book_id.value, rating: f.elements.rating.value ? Number(f.elements.rating.value) : null, notes: f.elements.notes.value, enjoyed: f.elements.enjoyed.value, avoid: f.elements.avoid.value, dnf: f.elements.dnf.checked}); $('#feedback-dialog').close(); await refreshState(); await loadPicks(); message('Feedback saved. Your next picks now use what you told us.'); }); });
$('#profile-form').addEventListener('submit', event => {event.preventDefault(); const f = event.currentTarget; const data = {}; for (const k of ['likes', 'avoids', 'preferred_authors', 'author_preference']) data[k] = f.elements[k].value; data.max_hours = Number(f.elements.max_hours.value); data.weights = {}; for (const k of Object.keys(state.dimensions)) data.weights[k] = Number(f.elements[k].value); action(f.querySelector('[type=submit]'), async () => { await api('/api/profile', data); await loadPicks(); message('Your taste is saved. Discovery has a fresh set of picks.'); });});
$('#sync').addEventListener('click', () => action($('#sync'), async () => {await api('/api/sync', {}); message('Sync started. Progress and catalogue changes will appear shortly.');}));
$('#refresh').addEventListener('click', () => action($('#refresh'), async () => {await refreshPlaylists(); await loadPicks();}));
$('#library').addEventListener('change', async () => {selection.clear(); updateCount(); try {await refreshPlaylists(); await loadPicks();} catch (error) {message(error.message, true);}});
let searchTimer; $('#search').addEventListener('input', () => {clearTimeout(searchTimer); searchTimer = setTimeout(() => loadPicks().catch(e => message(e.message, true)), 300);});
$('#queue').addEventListener('click', () => action($('#queue'), async () => {const result = await api('/api/queue', {book_ids: [...selection], library_id: $('#library').value, playlist_id: $('#playlist').value}); selection.clear(); updateCount(); await refreshPlaylists(); await loadPicks(); message(`${result.added} books added to ${result.name}. ${result.already_present ? result.already_present + ' were already there. ' : ''}Bookramp can now pull this ABS playlist.`);}));
$('#import').addEventListener('click', () => action($('#import'), async () => {const file = $('#csv-file').files[0]; if (!file) throw new Error('Choose your Goodreads CSV first.'); const result = await api('/api/import', await file.arrayBuffer(), true); await refreshState(); await loadPicks(); message(result.skipped ? result.reason : `${result.imported} Goodreads entries imported.`);}));
refreshState(true).catch(e => message(e.message, true));
setInterval(() => refreshState().catch(e => message(e.message, true)), 5000);
updateCount();
