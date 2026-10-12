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
  return {book_id: book.id, source_snapshot: book.source_snapshot, title: value('title'), author: value('author'), isbn: value('isbn'), url: value('url'),
    completed_date: value('completed_date'), public_rating: value('public_rating') ? Number(value('public_rating')) : null,
    confirmed: form.elements.confirmed.checked, ignored: false};
}
// Client-side checksum validation mirroring the server, used only to offer a
// direct Goodreads /book/isbn/ link. A valid checksum never asserts that
// Goodreads recognizes or imports the ISBN; Goodreads decides whether to redirect.
function canonicalIsbn(value) {
  const code = String(value ?? '').replace(/[\s-]/g, '').toUpperCase();
  if (/^97[89]\d{10}$/.test(code)) {
    let sum = 0; for (let i = 0; i < 13; i++) sum += Number(code[i]) * (i % 2 ? 3 : 1);
    return sum % 10 === 0 ? code : '';
  }
  if (/^\d{9}[\dX]$/.test(code)) {
    let sum = 0; for (let i = 0; i < 10; i++) sum += (10 - i) * (code[i] === 'X' ? 10 : Number(code[i]));
    return sum % 11 === 0 ? code : '';
  }
  return '';
}
async function loadGoodreads(automatic = true) {
  const ticket = ++grTicket, result = await api('/api/goodreads'); if (ticket !== grTicket) return;
  grBooks = result.books; grSelected = new Set([...grSelected].filter(id => grBooks.some(b => b.id === id && b.ready)));
  $('#gr-books').replaceChildren(); $('#gr-summary').textContent = `${result.books.filter(b => b.ready).length} ready to export; ${result.books.filter(b => !b.ready).length} need review or a catalog check. ${result.excluded} entries excluded because they were already read, exported, duplicated, ignored, or marked did not finish.`;
  if (!result.books.length) $('#gr-books').append(el('div', 'empty', 'All caught up. New completions will appear here.'));
  for (const book of result.books) {
    const card = el('article', 'paper gr-book'), heading = el('div', 'gr-heading'), checkLabel = el('label', 'check'), check = el('input'); check.type = 'checkbox'; check.disabled = !book.ready; check.checked = grSelected.has(book.id); check.setAttribute('aria-label', 'Export ' + book.title);
    check.addEventListener('change', () => {check.checked ? grSelected.add(book.id) : grSelected.delete(book.id); grCount();});
    checkLabel.append(check, el('span', '', book.original_title)); heading.append(checkLabel);
    const status = el('p', 'muted', (book.ready ? 'Ready to export. ' : 'Review required. ') + book.provenance + ': ' + book.reason); card.append(heading, el('p', 'muted', book.original_author), status);
    const lookupError = el('p', 'error-note'); lookupError.hidden = true; lookupError.setAttribute('role', 'alert'); card.append(lookupError);
    const lookupAction = (button, fn) => action(button, async () => {
      lookupError.hidden = true;
      try { await fn(); } catch (error) { lookupError.textContent = error.message; lookupError.hidden = false; detail.open = true; throw error; }
    });
    if (book.failure) card.append(el('p', 'error-note', 'Previous import failed: ' + book.failure.slice(0, 500) + '. Correct the match and confirm it again.'));
    const detail = el('details'), summary = el('summary', '', 'Review book & public rating'), form = el('form', 'gr-form');
    form.append(field('Goodreads title', 'title', book.title), field('Author(s) — review roles; do not drop coauthors', 'author', book.author), field('ISBN — catalog listing does not guarantee Goodreads acceptance', 'isbn', book.isbn), field('Goodreads book link', 'url', book.url, 'url'), field('Date completed — exported as Date Read', 'completed_date', book.completed_date, 'date'));
    const ratingLabel = el('label', '', 'Public Goodreads stars'), rating = el('select'); rating.name = 'public_rating';
    for (const [value, text] of [['', 'Leave unrated'], ['1', '★'], ['2', '★★'], ['3', '★★★'], ['4', '★★★★'], ['5', '★★★★★']]) { const option = el('option', '', text); option.value = value; rating.append(option); } rating.value = book.public_rating ?? ''; ratingLabel.append(rating); form.append(ratingLabel, el('p', 'muted', 'These stars are only for Goodreads. Your private rating, notes and recommendations stay separate.'));
    const confirmLabel = el('label', 'check'), confirmed = el('input'); confirmed.type = 'checkbox'; confirmed.name = 'confirmed'; confirmed.checked = Boolean(book.confirmed && !book.source_changed); confirmLabel.append(confirmed, el('span', '', 'I checked the work, author(s), language and ISBN edition. A reviewed print edition may represent the audiobook; ISBN checksum alone is not a match.')); form.append(confirmLabel);
    const actions = el('div', 'question-buttons');
    const search = el('a', 'button-link secondary', 'Find Goodreads match'), printSearch = el('a', 'button-link secondary', 'Find print/ebook edition');
    // External Google search only; no crawling and no Goodreads request. The
    // print variant adds format terms so an audiobook page is less likely to top results.
    for (const [link, extra] of [[search, ''], [printSearch, ' paperback OR hardcover OR print']]) {
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      const sync = () => {link.href = 'https://www.google.com/search?q=' + encodeURIComponent('site:goodreads.com/book/show ' + form.elements.title.value + ' ' + form.elements.author.value + extra);};
      sync(); link.addEventListener('click', sync);
    }
    const isbnLink = el('a', 'button-link secondary', 'Open Goodreads edition for this ISBN'); isbnLink.target = '_blank'; isbnLink.rel = 'noopener noreferrer'; isbnLink.hidden = true;
    // A checksum-valid ISBN only offers the public /book/isbn/ redirect; it never
    // asserts that Goodreads recognizes or will import this ISBN.
    const updateIsbnLink = () => {const code = canonicalIsbn(form.elements.isbn.value); isbnLink.hidden = !code; if (code) isbnLink.href = 'https://www.goodreads.com/book/isbn/' + code;};
    updateIsbnLink();
    const lookup = el('button', 'secondary', 'Get ISBN from link'), save = el('button', '', 'Save export details'), ignore = el('button', 'secondary', 'Skip this book'); lookup.type = ignore.type = 'button'; save.type = 'submit';
    const resolve = el('button', 'secondary', 'Find work ISBN (free catalogs)'); resolve.type = 'button';
    resolve.addEventListener('click', () => lookupAction(resolve, async () => {
      const response = await api('/api/goodreads/resolve', {book_ids: [book.id]}), result = response.results[0];
      if (result.automatic) {await loadGoodreads(); message(result.provenance + ': ' + result.reason); return;}
      if (result.candidate) {
        form.elements.title.value = result.candidate.title; form.elements.author.value = result.candidate.author; form.elements.isbn.value = result.candidate.isbn; updateIsbnLink();
        confirmed.checked = false; check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount();
      }
      status.textContent = (result.provenance || 'Catalog lookup') + ': ' + result.reason;
      message(result.reason);
    }));
    lookup.addEventListener('click', () => lookupAction(lookup, async () => {
      if (!form.elements.url.value.trim()) throw new Error('Get ISBN from link needs a Goodreads book link. Paste one above (use Find print/ebook edition to find the print edition first), or use Find work ISBN (free catalogs) without a link.');
      const result = await api('/api/goodreads/lookup', {url: form.elements.url.value}); form.elements.isbn.value = result.isbn; form.elements.url.value = result.url; updateIsbnLink();
      if (result.title) form.elements.title.value = result.title; if (result.author) form.elements.author.value = result.author;
      form.elements.confirmed.checked = false; check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount(); message('ISBN retrieved. Review the title, author roles and edition, confirm the match, then save.');
    }));
    form.addEventListener('input', e => {if (['title', 'author', 'isbn', 'url'].includes(e.target.name)) confirmed.checked = false; check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount(); status.textContent = 'Unsaved changes — review and save before exporting'; updateIsbnLink();});
    form.addEventListener('submit', e => {e.preventDefault(); action(save, async () => {
      const payload = grPayload(form, book); await api('/api/goodreads/save', payload); await loadGoodreads(); message('Goodreads details saved. Your private review is unchanged.');
    });});
    ignore.addEventListener('click', () => action(ignore, async () => {await api('/api/goodreads/save', {...grPayload(form, book), ignored: true}); grSelected.delete(book.id); card.remove(); grCount(); message('Skipped this book for Goodreads.');}));
    actions.append(resolve, search, printSearch, isbnLink, lookup, save, ignore); form.append(actions); detail.append(summary, form); card.append(detail); $('#gr-books').append(card);
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
  // One bounded server-side attempt, not a per-card loop or retry scheduler.
  // Do not discard edits/selections while a slow provider request is in flight.
  if (automatic === true) {
    let edited = false;
    const markEdited = () => {edited = true;};
    $('#gr-books').addEventListener('input', markEdited, {once: true});
    $('#gr-books').addEventListener('click', markEdited, {once: true});
    try {
      const resolved = await api('/api/goodreads/auto-resolve', {});
      if (resolved.results.length && ticket === grTicket && !edited && !grSelected.size) await loadGoodreads(false);
    } catch (error) { if (ticket === grTicket) message('Automatic ISBN check: ' + error.message, true); }
    finally { $('#gr-books').removeEventListener('input', markEdited); $('#gr-books').removeEventListener('click', markEdited); }
  }
}
$('#gr-select-ready').addEventListener('click', () => {for (const check of $('#gr-books').querySelectorAll('input[type=checkbox][aria-label]')) if (!check.disabled && !check.checked) check.click();});
$('#gr-export').addEventListener('click', () => action($('#gr-export'), async () => {const result = await api('/api/goodreads/export', {book_ids: [...grSelected]}); downloadCSV(result); grSelected.clear(); await loadGoodreads(); message(`${result.count} books exported with completion dates. They are now excluded from new exports. Re-download this batch if needed; paste any failed imports below.`);}).then(grCount));
$('#gr-reload').addEventListener('click', () => action($('#gr-reload'), () => loadGoodreads()));
async function chooseFailedBooks() {
  const result = await api('/api/goodreads/batch-items?id=' + encodeURIComponent($('#gr-batch').value)); $('#gr-failures').replaceChildren();
  renderImportItems(result.items); resetImportPreview();
}
let grReportKey = '', grManualFailures = false;
function resetImportPreview() {grReportKey = ''; $('#gr-reconcile-form [type=submit]').textContent = 'Preview import result';}
function renderImportItems(items) {
  $('#gr-failures').replaceChildren();
  for (const book of items) {
    const label = el('label', 'check'), input = el('input'); input.type = 'checkbox'; input.value = book.id; input.checked = Boolean(book.proposed_failed); input.disabled = Boolean(book.reported_success);
    input.addEventListener('change', () => {grManualFailures = true; resetImportPreview();});
    label.append(input, el('span', '', `${book.title} — ${book.author} · ${book.isbn} · ${book.state}${book.reported_success ? ' · reported success' : ''}${book.proposed_failed ? ' · proposed failure' : ''}${book.reason || book.failure ? ' · ' + (book.reason || book.failure).slice(0, 500) : ''}`)); $('#gr-failures').append(label);
  }
}
$('#gr-choose-failures').addEventListener('click', () => action($('#gr-choose-failures'), chooseFailedBooks));
$('#gr-batch').addEventListener('change', () => {$('#gr-failures').replaceChildren(); grManualFailures = false; resetImportPreview();});
$('#gr-reconcile-form').elements.output.addEventListener('input', () => {$('#gr-failures').replaceChildren(); grManualFailures = false; resetImportPreview();});
$('#gr-reconcile-form').elements.confirm_remaining.addEventListener('change', resetImportPreview);
$('#gr-reconcile-form').addEventListener('submit', e => {e.preventDefault(); const form = e.currentTarget; action(form.querySelector('[type=submit]'), async () => {
  const payload = {batch_id: $('#gr-batch').value, text: form.elements.output.value, confirm_remaining: form.elements.confirm_remaining.checked};
  if (grManualFailures) payload.failed_ids = [...$('#gr-failures').querySelectorAll('input:checked')].map(i => i.value);
  const key = JSON.stringify(payload), preview = key !== grReportKey;
  const result = await api('/api/goodreads/reconcile', {...payload, preview});
  renderImportItems(result.items);
  if (preview) {
    if (result.ambiguous || result.unrecognized || result.no_op) {resetImportPreview(); message('Nothing saved. Output is empty or ambiguous; explicitly select the failed books. Unrecognized output cannot confirm remaining imports.');}
    else {grReportKey = key; form.querySelector('[type=submit]').textContent = 'Save reviewed import result'; message(`${result.mixed ? 'Mixed success/failure report. ' : ''}Preview only: ${result.flagged.length} proposed failures. Check the per-book states and selections, then save.${form.elements.confirm_remaining.checked && !result.can_confirm_remaining ? ' Remaining imports will not be marked.' : ''}`);}
    return;
  }
  await loadGoodreads(); resetImportPreview(); grManualFailures = false; form.elements.output.value = ''; form.elements.confirm_remaining.checked = false;
  message(result.flagged.length ? `${result.flagged.length} failed books returned to the review list.` : 'Reviewed import result saved.');
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
