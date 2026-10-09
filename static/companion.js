/* Goodreads public exports and measured BookRamp statistics. No paid AI calls. */
let grSelected = new Set(), grBooks = [], grTicket = 0, statsTicket = 0, latestStats;
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob), link = el('a'); link.href = url; link.download = filename;
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 30000);
}
function downloadFile(path, filename) {const link = el('a'); link.href = path; link.download = filename; document.body.append(link); link.click(); link.remove();}
function downloadCSV(result) { downloadFile('/api/goodreads/download?id=' + encodeURIComponent(result.id), result.filename); }
function grCount() { $('#gr-selected').textContent = `${grSelected.size} selected`; $('#gr-export').disabled = !grSelected.size; }
function field(label, name, value, type = 'text') {
  const wrap = el('label', '', label), input = el('input'); input.name = name; input.type = type; input.value = value ?? ''; input.maxLength = 1000; wrap.append(input); return wrap;
}
function grPayload(form, book) {
  const value = k => form.elements[k].value;
  return {book_id: book.id, title: value('title'), author: value('author'), isbn: value('isbn'), url: value('url'),
    completed_date: value('completed_date'), public_rating: value('public_rating') ? Number(value('public_rating')) : null,
    confirmed: form.elements.confirmed.checked, ignored: false};
}
async function loadGoodreads() {
  const ticket = ++grTicket, result = await api('/api/goodreads'); if (ticket !== grTicket) return;
  grBooks = result.books; grSelected = new Set([...grSelected].filter(id => grBooks.some(b => b.id === id && b.ready)));
  $('#gr-books').replaceChildren(); $('#gr-summary').textContent = `${result.books.length} completed books to review. ${result.excluded} entries excluded because they were already read, exported, duplicated, ignored, or marked did not finish.`;
  if (!result.books.length) $('#gr-books').append(el('div', 'empty', 'All caught up. New completions will appear here.'));
  for (const book of result.books) {
    const card = el('article', 'paper gr-book'), heading = el('div', 'gr-heading'), checkLabel = el('label', 'check'), check = el('input'); check.type = 'checkbox'; check.disabled = !book.ready; check.checked = grSelected.has(book.id); check.setAttribute('aria-label', 'Export ' + book.title);
    check.addEventListener('change', () => {check.checked ? grSelected.add(book.id) : grSelected.delete(book.id); grCount();});
    checkLabel.append(check, el('span', '', book.original_title)); heading.append(checkLabel);
    const status = el('p', 'muted', book.ready ? 'Ready to export' : 'Review and save the match, ISBN and completion date'); card.append(heading, el('p', 'muted', book.original_author), status);
    if (book.failure) card.append(el('p', 'error-note', 'Previous import failed. Correct the match and confirm it again.'));
    const detail = el('details'), summary = el('summary', '', 'Review book & public rating'), form = el('form', 'gr-form');
    form.append(field('Goodreads title', 'title', book.title), field('Primary author (not narrators)', 'author', book.author), field('ISBN recognized by Goodreads', 'isbn', book.isbn), field('Goodreads book link', 'url', book.url, 'url'), field('Date completed — exported as Date Read', 'completed_date', book.completed_date, 'date'));
    const ratingLabel = el('label', '', 'Public Goodreads stars'), rating = el('select'); rating.name = 'public_rating';
    for (const [value, text] of [['', 'Leave unrated'], ['1', '★'], ['2', '★★'], ['3', '★★★'], ['4', '★★★★'], ['5', '★★★★★']]) { const option = el('option', '', text); option.value = value; rating.append(option); } rating.value = book.public_rating ?? ''; ratingLabel.append(rating); form.append(ratingLabel, el('p', 'muted', 'These stars are only for Goodreads. Your private rating, notes and recommendations stay separate.'));
    const confirmLabel = el('label', 'check'), confirmed = el('input'); confirmed.type = 'checkbox'; confirmed.name = 'confirmed'; confirmed.checked = Boolean(book.confirmed); confirmLabel.append(confirmed, el('span', '', 'I checked that this is the correct book and ISBN. Any edition is fine.')); form.append(confirmLabel);
    const actions = el('div', 'question-buttons'), search = el('a', 'button-link secondary', 'Find Goodreads match'); search.href = 'https://www.google.com/search?q=' + encodeURIComponent('site:goodreads.com/book/show ' + form.elements.title.value + ' ' + form.elements.author.value); search.target = '_blank'; search.rel = 'noopener noreferrer';
    search.addEventListener('click', () => {search.href = 'https://www.google.com/search?q=' + encodeURIComponent('site:goodreads.com/book/show ' + form.elements.title.value + ' ' + form.elements.author.value);});
    const lookup = el('button', 'secondary', 'Get ISBN from link'), save = el('button', '', 'Save export details'), ignore = el('button', 'secondary', 'Skip this book'); lookup.type = ignore.type = 'button'; save.type = 'submit';
    lookup.addEventListener('click', () => action(lookup, async () => {
      const result = await api('/api/goodreads/lookup', {url: form.elements.url.value}); form.elements.isbn.value = result.isbn; form.elements.url.value = result.url;
      if (result.title) form.elements.title.value = result.title; if (result.author) form.elements.author.value = result.author;
      form.elements.confirmed.checked = false; check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount(); message('ISBN retrieved. Check the title and primary author, confirm the match, then save.');
    }));
    form.addEventListener('input', () => {check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount(); status.textContent = 'Unsaved changes — save before exporting';});
    form.addEventListener('submit', e => {e.preventDefault(); action(save, async () => {
      const payload = grPayload(form, book); await api('/api/goodreads/save', payload); Object.assign(book, payload); book.ready = Boolean(payload.confirmed && payload.isbn && payload.completed_date); check.disabled = !book.ready;
      status.textContent = book.ready ? 'Ready to export' : 'Saved. An ISBN and confirmed match are required to export.'; message('Goodreads details saved. Your private review is unchanged.');
    });});
    ignore.addEventListener('click', () => action(ignore, async () => {await api('/api/goodreads/save', {...grPayload(form, book), ignored: true}); grSelected.delete(book.id); card.remove(); grCount(); message('Skipped this book for Goodreads.');}));
    actions.append(search, lookup, save, ignore); form.append(actions); detail.append(summary, form); card.append(detail); $('#gr-books').append(card);
  }
  const previousBatch = $('#gr-batch').value;
  $('#gr-batches').replaceChildren(); $('#gr-batch').replaceChildren();
  for (const batch of result.batches) {
    const option = el('option', '', `${new Date(batch.created_at).toLocaleString()} · ${batch.count} books`); option.value = batch.id; $('#gr-batch').append(option);
    const row = el('div', 'batch-row'), text = el('span', '', `${new Date(batch.created_at).toLocaleString()} · ${batch.count} exported · ${batch.failed} failed · ${batch.imported} confirmed imported`), download = el('button', 'secondary', 'Download again');
    download.addEventListener('click', () => action(download, async () => downloadCSV(await api('/api/goodreads/batch?id=' + encodeURIComponent(batch.id))))); row.append(text, download); $('#gr-batches').append(row);
  }
  if (result.batches.some(b => b.id === previousBatch)) $('#gr-batch').value = previousBatch;
  $('#gr-reconcile-form').hidden = !result.batches.length; grCount();
}
$('#gr-select-ready').addEventListener('click', () => {for (const check of $('#gr-books').querySelectorAll('input[type=checkbox][aria-label]')) if (!check.disabled && !check.checked) check.click();});
$('#gr-export').addEventListener('click', () => action($('#gr-export'), async () => {const result = await api('/api/goodreads/export', {book_ids: [...grSelected]}); downloadCSV(result); grSelected.clear(); await loadGoodreads(); message(`${result.count} books exported with completion dates. They are now excluded from new exports. Re-download this batch if needed; paste any failed imports below.`);}).then(grCount));
$('#gr-reload').addEventListener('click', () => action($('#gr-reload'), loadGoodreads));
async function chooseFailedBooks() {
  const result = await api('/api/goodreads/batch-items?id=' + encodeURIComponent($('#gr-batch').value)); $('#gr-failures').replaceChildren();
  for (const book of result.items) {const label = el('label', 'check'), input = el('input'); input.type = 'checkbox'; input.value = book.id; label.append(input, el('span', '', book.title)); $('#gr-failures').append(label);}
}
$('#gr-choose-failures').addEventListener('click', () => action($('#gr-choose-failures'), chooseFailedBooks));
$('#gr-batch').addEventListener('change', () => $('#gr-failures').replaceChildren());
$('#gr-reconcile-form').addEventListener('submit', e => {e.preventDefault(); const form = e.currentTarget; action(form.querySelector('[type=submit]'), async () => {
  const result = await api('/api/goodreads/reconcile', {batch_id: $('#gr-batch').value, text: form.elements.output.value, confirm_remaining: form.elements.confirm_remaining.checked, failed_ids: [...$('#gr-failures').querySelectorAll('input:checked')].map(i => i.value)});
  await loadGoodreads(); $('#gr-failures').replaceChildren();
  if (result.unrecognized) {for (const book of result.items) {const label = el('label', 'check'), input = el('input'); input.type = 'checkbox'; input.value = book.id; label.append(input, el('span', '', book.title)); $('#gr-failures').append(label);} message('Could not identify the failed books safely. Select them below and save the result again.');}
  else {form.elements.output.value = ''; form.elements.confirm_remaining.checked = false; message(result.flagged.length ? `${result.flagged.length} failed books returned to the review list.` : 'Import result saved.');}
});});
function displayNumber(value, suffix = '') { return value === null || value === undefined ? 'Not available' : value.toFixed(1) + suffix; }
async function loadStats() {
  $('#download-card').disabled = true;
  const ticket = ++statsTicket, data = await api('/api/stats?month=' + encodeURIComponent($('#stats-month').value)); if (ticket !== statsTicket) return; latestStats = data;
  $('#stats-metrics').replaceChildren();
  for (const [label, value] of [['Books completed', String(data.completed)], ['Source audiobook hours', displayNumber(data.source_hours)], ['Actual listening hours', displayNumber(data.actual_hours)], ['Hours saved vs 1×', displayNumber(data.hours_saved)], ['Average listening speed', displayNumber(data.average_speed, '×')], ['Time-weighted median speed', displayNumber(data.median_speed, '×')], ['Fastest sustained speed', displayNumber(data.maximum_sustained_speed, '×')], ['Again actions', data.again_count === null ? 'Not available' : String(data.again_count)]]) {
    const box = el('div', 'paper metric'); box.append(el('strong', '', value), el('span', 'muted', label)); $('#stats-metrics').append(box);
  }
  $('#stats-note').textContent = `${data.note} ${data.measured_sessions} of ${data.session_count} sessions have paired time measurements.${data.median_speed === null ? ' Median requires time-weighted speed buckets for every session in this period.' : ''}`;
  $('#stats-books').replaceChildren();
  for (const book of data.books) {const row = el('article', 'paper'); row.append(el('h3', '', book.title), el('p', 'muted', `${book.sessions} sessions · ${book.measured_sessions ? (book.wall_seconds / 3600).toFixed(1) + ' actual hours' : 'Listening time unavailable'}`)); $('#stats-books').append(row);}
  if (!data.session_count) $('#stats-books').append(el('div', 'empty', 'Pair BookRamp to begin collecting measured listening time. ABS completion counts are available already; speed and listening hours are not guessed.'));
  renderReadingCard(data);
  $('#download-card').disabled = false;
}
function renderReadingCard(data) {
  const canvas = $('#reading-card'), ctx = canvas.getContext('2d'); ctx.fillStyle = '#f8f5ed'; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = '#1e493b'; ctx.font = '24px sans-serif'; ctx.fillText('MY LISTENING MONTH', 70, 75);
  const label = new Date(data.month + '-15T12:00:00').toLocaleDateString(undefined, {month: 'long', year: 'numeric'}); ctx.font = '60px Georgia'; ctx.fillText(label, 70, 160);
  const entries = [[String(data.completed), 'BOOKS COMPLETED'], [displayNumber(data.source_hours), 'SOURCE HOURS'], [displayNumber(data.actual_hours), 'ACTUAL HOURS'], [displayNumber(data.average_speed, '×'), 'AVERAGE SPEED'], [displayNumber(data.hours_saved), 'HOURS SAVED VS 1×'], [displayNumber(data.maximum_sustained_speed, '×'), 'FASTEST SUSTAINED']];
  entries.forEach(([value, title], i) => {const x = 70 + (i % 2) * 510, y = 265 + Math.floor(i / 2) * 155; ctx.font = '52px Georgia'; ctx.fillStyle = '#1e493b'; ctx.fillText(value, x, y); ctx.font = '20px sans-serif'; ctx.fillStyle = '#6b756c'; ctx.fillText(title, x, y + 40);});
  ctx.font = '19px sans-serif'; ctx.fillText('AutomaticBookSelector · My shelf. My pace.', 70, 775);
  ctx.font = '17px sans-serif'; ctx.fillText(`${data.measured_sessions}/${data.session_count} sessions measured · America/Chicago · Sessions started in this month`, 70, 818);
  ctx.fillText('Listening totals include replays. Skipped audio is excluded. Private reviews are omitted.', 70, 850);
}
// Use numeric date parts rather than depending on a locale's date order.
const monthParts = new Intl.DateTimeFormat('en', {timeZone:'America/Chicago',year:'numeric',month:'2-digit'}).formatToParts(new Date()); $('#stats-month').value = monthParts.find(p => p.type === 'year').value + '-' + monthParts.find(p => p.type === 'month').value;
$('#stats-month').addEventListener('change', () => loadStats().catch(e => message(e.message, true)));
$('#stats-refresh').addEventListener('click', () => action($('#stats-refresh'), loadStats));
$('#download-card').addEventListener('click', () => {if (latestStats) downloadFile('/api/card.png?month=' + encodeURIComponent(latestStats.month), 'my-listening-' + latestStats.month + '.png');});
async function loadConnections() {
  const result = await api('/api/bookramp/connections'); $('#br-connections').replaceChildren();
  for (const connection of result.connections) {
    const row = el('div', 'batch-row'); row.append(el('span', '', connection.name + (connection.revoked_at ? ' · Revoked' : connection.last_seen_at ? ' · Last upload ' + new Date(connection.last_seen_at).toLocaleString() : ' · Awaiting first upload')));
    if (!connection.revoked_at) {const revoke = el('button', 'secondary', 'Revoke token'); revoke.addEventListener('click', () => action(revoke, async () => {await api('/api/bookramp/revoke', {id:connection.id}); await loadConnections(); message('BookRamp token revoked. Saved sessions remain available.');})); row.append(revoke);} $('#br-connections').append(row);
  }
}
$('#br-create-form').addEventListener('submit', e => {e.preventDefault(); action(e.currentTarget.querySelector('button'), async () => {const result = await api('/api/bookramp/create', {name: $('#br-name').value}); $('#br-token').value = result.token; $('#br-token-box').hidden = false; await loadConnections(); message('Connection created. Copy the token into BookRamp; it is shown only once.');});});
$('#br-clear-token').addEventListener('click', () => {$('#br-token').value = ''; $('#br-token-box').hidden = true;});
document.querySelectorAll('.tabs button').forEach(button => button.addEventListener('click', () => {if (button.dataset.panel === 'goodreads') loadGoodreads().catch(e => message(e.message, true)); if (button.dataset.panel === 'stats') {loadStats().catch(e => message(e.message, true)); loadConnections().catch(e => message(e.message, true));}}));
if (location.hash === '#goodreads') loadGoodreads().catch(e => message(e.message, true));
if (location.hash === '#stats') {loadStats().catch(e => message(e.message, true)); loadConnections().catch(e => message(e.message, true));}
