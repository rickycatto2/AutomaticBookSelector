/* Goodreads public exports and measured BookRamp statistics. No paid AI calls. */
let grSelected = new Set(), grBooks = [], grTicket = 0, statsTicket = 0, latestStats;
let grMatchJob = null, grBatchRunning = false, grBatchCancelled = false, grBatchWake = null, grDirty = false, grAutoBusy = false;
function grMatchProgress(note = '') {
  if (!grMatchJob) {$('#gr-match-progress').textContent = note; return;}
  const items = grMatchJob.items, count = state => items.filter(i => i.state === state).length;
  $('#gr-match-progress').textContent = `${items.length - count('queued')} / ${items.length} processed · ${count('suggested') + count('ready')} suggested (Save required) · ${count('review')} review · ${count('skipped')} skipped · ${count('provider_error') + count('pending')} provider errors / interrupted.${note ? ' ' + note : ''}`;
  $('#gr-match-eligible').textContent = count('queued') ? 'Match eligible books (resume)' : 'Match eligible books';
}
async function matchEligibleBooks(retryProvider = false) {
  if (grBatchRunning) return;
  if (grDirty || grAutoBusy) {message(grDirty ? 'Save or reload unsaved edits before matching.' : 'Wait for the current automatic check to finish.', true); return;}
  grBatchRunning = true; grBatchCancelled = false;
  $('#gr-match-eligible').disabled = true; $('#gr-match-cancel').hidden = false;
  $('#gr-books').inert = true; $('#gr-reload').disabled = true;
  try {
    const response = await api(retryProvider === 'recheck' ? '/api/goodreads/recheck' : retryProvider === true ? '/api/goodreads/retry-provider' : '/api/goodreads/match-batch', {}); grMatchJob = response.job;
    if (!grMatchJob) {grMatchProgress('No eligible undecided books. Review existing suggestions per book.'); return;}
    // The server owns the bounded durable queue; never iterate the Goodreads seed.
    for (const item of grMatchJob.items.filter(i => i.state === 'queued').slice(0, 25)) {
      if (grBatchCancelled) break;
      grMatchProgress('Waiting five seconds before the next book…');
      await new Promise(resolve => {const timer = setTimeout(() => {grBatchWake = null; resolve();}, 5000); grBatchWake = () => {clearTimeout(timer); grBatchWake = null; resolve();};});
      if (grBatchCancelled) break;
      grMatchProgress('Matching one book…');
      const resolved = await api('/api/goodreads/resolve', {book_ids: [item.id], batch_id: grMatchJob.id});
      grMatchJob = resolved.job; grMatchProgress();
    }
    await loadGoodreads(false);
    grMatchProgress(grBatchCancelled ? 'Paused. Resume explicitly when ready.' : 'Finished. Review uncertain results; nothing uploaded.');
  } catch (error) {
    // A lost response may have committed. Reload durable state before any resume;
    // never retry the in-flight request automatically.
    try {await loadGoodreads(false);} catch (_) {}
    grMatchProgress('Stopped: ' + error.message + ' Reload before resuming if status is unavailable.');
    message(error.message, true);
  } finally {
    grBatchRunning = false; $('#gr-match-eligible').disabled = false; $('#gr-match-cancel').hidden = true;
    $('#gr-books').inert = false; $('#gr-reload').disabled = false;
  }
}
$('#gr-match-eligible').addEventListener('click', matchEligibleBooks);
$('#gr-match-cancel').addEventListener('click', () => {grBatchCancelled = true; if (grBatchWake) grBatchWake(); grMatchProgress('Pausing after the current request…');});
$('#gr-books').addEventListener('input', () => {grDirty = true;});
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
    confirmed: true, ignored: false};
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
async function loadGoodreads(automatic = false) {
  const ticket = ++grTicket, result = await api('/api/goodreads'); if (ticket !== grTicket) return;
  grBooks = result.books; resetImportPreview(); grSelected = new Set([...grSelected].filter(id => grBooks.some(b => b.id === id && b.ready)));
  grMatchJob = result.match_job || null; grDirty = false; grMatchProgress();
  const queue = grBooks.filter(b => !b.skipped), skipped = grBooks.filter(b => b.skipped);
  $('#gr-skipped-list').replaceChildren(); $('#gr-skipped-summary').textContent = `Skipped (${skipped.length})`;
  for (const book of skipped) {
    const row = el('div', 'batch-title'), restore = el('button', 'secondary', 'Restore to queue'); restore.type = 'button';
    restore.setAttribute('aria-label', 'Restore to queue: ' + book.title);
    restore.addEventListener('click', () => action(restore, async () => {
      await api('/api/goodreads/skip', {book_id: book.id, ignored: false}); await loadGoodreads();
      message('Restored to queue. Saved details and export history retained; review and Save if needed.');
    }));
    row.append(el('span', '', `${book.title} — ${book.author}`), restore); $('#gr-skipped-list').append(row);
  }
  $('#gr-books').replaceChildren(); $('#gr-summary').textContent = `${queue.length} completed books in queue · ${queue.filter(b => b.ready).length} saved and ready · ${skipped.length} skipped (hidden, recoverable below). ${result.excluded} confirmed/read-history entries excluded. Exporting does not clear the queue: confirm imported books in Export history. Skip books you never want on Goodreads.`;
  if (!queue.length) $('#gr-books').append(el('div', 'empty', 'All caught up. New completions will appear here.'));
  for (const book of queue) {
    const card = el('article', 'paper gr-book'), heading = el('div', 'gr-heading'), checkLabel = el('label', 'check'), check = el('input'); check.type = 'checkbox'; check.disabled = !book.ready; check.checked = grSelected.has(book.id); check.setAttribute('aria-label', 'Export ' + book.title);
    check.addEventListener('change', () => {check.checked ? grSelected.add(book.id) : grSelected.delete(book.id); grCount();});
    const cover = el('img', 'gr-cover'); cover.src = '/api/cover?id=' + encodeURIComponent(book.id); cover.alt = 'Cover of ' + book.title; cover.loading = 'lazy'; cover.addEventListener('error', () => {cover.hidden = true;});
    checkLabel.append(check, el('span', '', book.title)); heading.append(cover, checkLabel);
    const status = el('p', 'muted', (book.ready ? 'Ready to export. ' : book.provider_error ? 'Lookup could not run; export stays blocked. ' : 'Review required. ') + book.provenance + ': ' + (book.lookup_status || book.reason)); card.append(heading, el('p', 'muted', book.author), status);
    if (book.original_author !== book.author) card.append(el('p', 'muted', 'ABS author metadata: ' + book.original_author));
    card.append(el('p', 'gr-state', book.import_state || 'never-attempted'));
    if (book.retry_eligible) {
      const retry = el('button', 'secondary', 'Retry provider-blocked books (up to 25)'); retry.type = 'button';
      retry.addEventListener('click', () => matchEligibleBooks(true)); card.append(retry);
    }
    if (book.recheck_eligible) {
      const recheck = el('button', 'secondary', 'Re-check nothing found / ambiguous books (up to 25)'); recheck.type = 'button';
      recheck.addEventListener('click', () => matchEligibleBooks('recheck')); card.append(recheck);
    }
    if (book.evidence?.length) card.append(el('p', 'muted', `Evidence score ${book.score}/100 · auto threshold ${book.threshold} plus linked ISBN identity. Edition/language differences are allowed; Goodreads acceptance remains unverified.`));
    const lookupError = el('p', 'error-note'); lookupError.hidden = true; lookupError.setAttribute('role', 'alert'); card.append(lookupError);
    const lookupAction = (button, fn) => action(button, async () => {
      lookupError.hidden = true;
      try { await fn(); } catch (error) { lookupError.textContent = error.message; lookupError.hidden = false; detail.open = true; throw error; }
    });
    if (book.failure) card.append(el('p', 'error-note', 'Previous import failed: ' + book.failure.slice(0, 500) + '. Correct the match and confirm it again.'));
    const detail = el('details'), summary = el('summary', '', 'More details ▾'), form = el('form', 'gr-form'), extras = el('div', 'gr-details');
    if (book.suggestion) card.append(el('p', 'muted', `Saved catalog suggestion (unconfirmed): ${book.suggestion.title} — ${book.suggestion.author} · ${book.suggestion.isbn}. Use Find work ISBN or copy these details for review.`));
    for (const candidate of (book.candidates || []).slice(0, 10)) card.append(el('p', 'muted', `Catalog evidence (not a confirmed match): ${candidate.title} — ${candidate.author} · ${(candidate.isbns || []).join(', ')}`));
    extras.append(field('Goodreads title', 'title', book.title), field('Author(s) — review roles; do not drop coauthors', 'author', book.author), field('Goodreads book link', 'url', book.url, 'url'), field('Date completed — exported as Date Read', 'completed_date', book.completed_date, 'date'));
    detail.append(summary, extras); form.append(detail);
    form.append(field('Paste ASIN or checksum-valid ISBN-10/13', 'isbn', book.isbn));
    const ratingLabel = el('label', '', 'Public Goodreads stars'), rating = el('select'); rating.name = 'public_rating';
    for (const [value, text] of [['', 'Leave unrated'], ['1', '★'], ['2', '★★'], ['3', '★★★'], ['4', '★★★★'], ['5', '★★★★★']]) { const option = el('option', '', text); option.value = value; rating.append(option); } rating.value = book.public_rating ?? ''; ratingLabel.append(rating); form.append(ratingLabel, el('p', 'muted', 'These stars are only for Goodreads. Your private rating, notes and recommendations stay separate.'));
    const confirmed = el('input'); confirmed.type = 'hidden'; confirmed.name = 'confirmed'; confirmed.checked = Boolean(book.confirmed && !book.source_changed);
    form.append(confirmed, el('p', 'muted', 'Save confirms your chosen book details for export, not that Goodreads imported it.'));
    const actions = el('div', 'question-buttons');
    const search = el('a', 'button-link secondary', 'Find Goodreads match'), printSearch = el('a', 'button-link secondary', 'Find print/ebook edition');
    // External Google search only; no crawling and no Goodreads request.
    // Search only the first author to avoid narrator-filled metadata; saved authors are unchanged.
    // The print variant retains its alternative format terms.
    for (const [link, extra] of [[search, ' audiobook'], [printSearch, ' audiobook paperback OR hardcover OR print']]) {
      link.target = '_blank'; link.rel = 'noopener noreferrer';
      const sync = () => {
        const title = String(form.elements.title.value ?? '').trim(), author = String(form.elements.author.value ?? '').split(/[,;]/, 1)[0].trim();
        // Replace lone surrogate code units before encoding; odd pasted fields must not throw.
        const query = ['site:goodreads.com/book/show', title, author, extra.trim()].filter(Boolean).join(' ').toWellFormed();
        link.href = 'https://www.google.com/search?q=' + encodeURIComponent(query).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
      };
      sync(); link.addEventListener('click', sync);
    }
    const isbnLink = el('a', 'button-link secondary', 'Open Goodreads edition for this ISBN'); isbnLink.target = '_blank'; isbnLink.rel = 'noopener noreferrer'; isbnLink.hidden = true;
    // A checksum-valid ISBN only offers the public /book/isbn/ redirect; it never
    // asserts that Goodreads recognizes or will import this ISBN.
    const updateIsbnLink = () => {const code = canonicalIsbn(form.elements.isbn.value); isbnLink.hidden = !code; if (code) isbnLink.href = 'https://www.goodreads.com/book/isbn/' + code;};
    updateIsbnLink();
    const lookup = el('button', 'secondary', 'Get ISBN from link'), save = el('button', '', 'Save'), ignore = el('button', 'secondary', 'Skip this book'); lookup.type = ignore.type = 'button'; save.type = 'submit';
    const resolve = el('button', 'secondary', 'Find work ISBN (free catalogs)'); resolve.type = 'button';
    resolve.addEventListener('click', () => lookupAction(resolve, async () => {
      const response = await api('/api/goodreads/resolve', {book_ids: [book.id], title: form.elements.title.value, author: form.elements.author.value, source_snapshot: book.source_snapshot}), result = response.results[0];
      // Explicit lookups are suggestions, never a reload that discards any form.
      if (result.candidate) {
        status.textContent = `Suggestion: ${result.candidate.title} — ${result.candidate.author} · catalog ISBN ${result.candidate.isbn}. Existing edits retained. `;
        if (!form.elements.isbn.value.trim() && !book.saved_decision) {
          form.elements.isbn.value = result.candidate.isbn;
          grDirty = true; updateIsbnLink();
        }
        confirmed.checked = false; check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount();
      }
      status.textContent += (result.provenance || 'Catalog lookup') + ': ' + result.reason;
      message(result.reason);
    }));
    lookup.addEventListener('click', () => lookupAction(lookup, async () => {
      if (!form.elements.url.value.trim()) throw new Error('Get ISBN from link needs a Goodreads book link. Paste one above (use Find print/ebook edition to find the print edition first), or use Find work ISBN (free catalogs) without a link.');
      const result = await api('/api/goodreads/lookup', {url: form.elements.url.value});
      if (!form.elements.isbn.value.trim() && !book.saved_decision) form.elements.isbn.value = result.isbn;
      status.textContent = `Link suggestion: ISBN ${result.isbn}. Existing manual details retained.`; updateIsbnLink();
      form.elements.confirmed.checked = false; check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount(); message('ISBN retrieved. Review the title, author roles and edition, confirm the match, then save.');
    }));
    form.addEventListener('input', e => {if (['title', 'author', 'isbn', 'url'].includes(e.target.name)) confirmed.checked = false; check.disabled = true; check.checked = false; grSelected.delete(book.id); grCount(); status.textContent = 'Unsaved changes — review and save before exporting'; updateIsbnLink();});
    form.addEventListener('submit', e => {e.preventDefault(); lookupAction(save, async () => {
      const payload = grPayload(form, book);
      if (!canonicalIsbn(payload.isbn) && !/^[A-Z0-9]{10}$/.test(payload.isbn)) throw new Error('Enter a checksum-valid ISBN-10/13 or an ASIN of exactly 10 uppercase alphanumeric characters.');
      await api('/api/goodreads/save', payload); grSelected.add(book.id); await loadGoodreads(); message('Saved for export. Your private review is unchanged.');
    });});
    ignore.addEventListener('click', () => action(ignore, async () => {await api('/api/goodreads/skip', {book_id: book.id, source_snapshot: book.source_snapshot, ignored: true}); grSelected.delete(book.id); await loadGoodreads(); message('Skipped and hidden from queue. Restore to queue in Skipped; saved details and history retained. Unsaved edits were not saved.');}));
    actions.append(search, save, ignore); extras.append(resolve, printSearch, isbnLink, lookup); form.append(actions, el('p', 'muted', 'Skip this book if you never want it on Goodreads: hidden and excluded from exports and Goodreads suggestions until you Restore to queue. Saved details are retained; unsaved edits are not saved.'), detail); card.append(form); $('#gr-books').append(card);
  }
  const previousBatch = $('#gr-batch').value;
  $('#gr-batches').replaceChildren(); $('#gr-batch').replaceChildren();
  for (const batch of result.batches) {
    const option = el('option', '', `${new Date(batch.created_at).toLocaleString()} · ${batch.count} books`); option.value = batch.id; $('#gr-batch').append(option);
    const row = el('div', 'batch-row'), text = el('span', '', `${new Date(batch.created_at).toLocaleString()} · ${batch.count} exported · ${batch.failed} failed · ${batch.imported} confirmed imported`), download = el('button', 'secondary', 'Download again');
    download.addEventListener('click', () => action(download, async () => downloadCSV(await api('/api/goodreads/batch?id=' + encodeURIComponent(batch.id)))));
    const titles = el('details'), label = el('summary', '', 'View individual titles'), list = el('div');
    titles.append(label, list); titles.addEventListener('toggle', async () => {
      if (!titles.open || list.children.length) return;
      try {
        const data = await api('/api/goodreads/batch-items?id=' + encodeURIComponent(batch.id));
        for (const item of data.items) {
          const entry = el('div', 'batch-title'); entry.append(el('span', '', `${item.title} — ${item.author} · ${item.isbn} · ${item.state === 'imported' ? 'confirmed imported' : item.state === 'exported' ? 'exported-unconfirmed' : item.state}`));
          if (item.state !== 'imported') {
            const back = el('button', 'secondary', 'Return to review'); back.type = 'button';
            back.title = 'Bring this one book back into the working queue for review';
            back.addEventListener('click', () => action(back, async () => {await api('/api/goodreads/return-to-review', {batch_id: batch.id, book_id: item.id}); await loadGoodreads(); message('Returned this one book to the working queue for review; saved details and export history retained.');})); entry.append(back, el('small', 'muted', 'Brings this one book back into the working queue.'));
          }
          if (item.state === 'exported') {
            const imported = el('button', 'secondary', 'Confirm imported'); imported.type = 'button';
            imported.title = 'Only confirm after checking this title imported successfully into Goodreads; it then leaves the queue';
            imported.addEventListener('click', () => action(imported, async () => {await api('/api/goodreads/confirm-imported', {batch_id: batch.id, book_id: item.id}); await loadGoodreads(); message('This title is now confirmed imported.');})); entry.append(imported);
          }
          list.append(entry);
        }
      } catch (error) {message(error.message, true);}
    });
    row.append(text, download, titles); $('#gr-batches').append(row);
  }
  if (result.batches.some(b => b.id === previousBatch)) $('#gr-batch').value = previousBatch;
  $('#gr-reconcile-form').hidden = !result.batches.length; grCount();
  // One bounded server-side attempt, not a per-card loop or retry scheduler.
  // Do not discard edits/selections while a slow provider request is in flight.
  if (automatic === true && !grBatchRunning) {
    grAutoBusy = true;
    let edited = false;
    const markEdited = () => {edited = true;};
    $('#gr-books').addEventListener('input', markEdited, {once: true});
    $('#gr-books').addEventListener('click', markEdited, {once: true});
    try {
      const resolved = await api('/api/goodreads/auto-resolve', {});
      if (resolved.results.length && ticket === grTicket && !edited && !grSelected.size) await loadGoodreads(false);
    } catch (error) { if (ticket === grTicket) message('Automatic ISBN check: ' + error.message, true); }
    finally { grAutoBusy = false; $('#gr-books').removeEventListener('input', markEdited); $('#gr-books').removeEventListener('click', markEdited); }
  }
}
$('#gr-select-ready').addEventListener('click', () => {for (const check of $('#gr-books').querySelectorAll('input[type=checkbox][aria-label]')) if (!check.disabled && !check.checked) check.click();});
$('#gr-export').addEventListener('click', () => action($('#gr-export'), async () => {const result = await api('/api/goodreads/export', {book_ids: [...grSelected]}); downloadCSV(result); grSelected.clear(); await loadGoodreads(); message(`${result.count} books exported. They stay exported-unconfirmed until you explicitly confirm imports below. Paste any errors there.`);}).then(grCount));
$('#gr-reload').addEventListener('click', () => action($('#gr-reload'), () => loadGoodreads()));
async function chooseFailedBooks() {
  const result = await api('/api/goodreads/batch-items?id=' + encodeURIComponent($('#gr-batch').value)); $('#gr-failures').replaceChildren();
  renderImportItems(result.items); resetImportPreview();
}
let grReportKey = '', grManualFailures = false, grClearItems = null, grPendingPlan = null;
function resetImportPreview() {grReportKey = ''; grClearItems = null; grPendingPlan = null; $('#gr-reconcile-form [type=submit]').textContent = 'Finish this batch: clear imports, keep failures'; $('#gr-import-summary').textContent = ''; $('#gr-clear-imports').hidden = false;}
function importPlan(items, confirm) {
  const skipped = items.filter(b => grBooks.some(saved => saved.id === b.id && saved.skipped));
  const failed = items.filter(b => b.proposed_failed && !skipped.includes(b));
  const imported = confirm ? items.filter(b => b.state === 'exported' && !b.proposed_failed && !skipped.includes(b)) : [];
  return {skipped, failed, imported};
}
function showImportPlan(plan) {
  grPendingPlan = plan;
  $('#gr-clear-imports').hidden = true;
  $('#gr-import-summary').textContent = `${plan.imported.length} books will be marked imported and leave your queue; ${plan.failed.length} failed books will go back to review for correction; ${plan.skipped.length} skipped books stay excluded from exports and suggestions. Nothing has changed yet. Confirm below to apply this plan.`;
  $('#gr-reconcile-form [type=submit]').textContent = 'Confirm: apply this plan and clear imported books';
}
async function confirmImportedTitles(items, batchId) {
  let confirmed = 0;
  try {
    for (const book of items) {
      await api('/api/goodreads/confirm-imported', {batch_id: batchId, book_id: book.id});
      confirmed++;
    }
  } catch (error) {
    let detail = error.message;
    try {await loadGoodreads();} catch (reloadError) {detail += ` Queue reload failed: ${reloadError.message}`;}
    throw new Error(`Confirmed ${confirmed} of ${items.length} before the failure; the rest are still in your queue. Review and repeat. ${detail}`);
  }
}
$('#gr-clear-imports').addEventListener('click', () => action($('#gr-clear-imports'), async () => {
  // Deliberate no-failures path uses the existing per-title confirmation gate,
  // never invented report evidence or a bypass of reconcile's prose gates.
  const form = $('#gr-reconcile-form');
  const report = await api('/api/goodreads/reconcile', {batch_id: $('#gr-batch').value, text: form.elements.output.value, preview: true});
  renderImportItems(report.items);
  if (report.items.some(b => b.proposed_failed) || grManualFailures) {resetImportPreview(); message('Failures are selected or detected. Use Finish this batch and resolve the failed books instead.'); return;}
  if (form.elements.output.value.trim() && (report.ambiguous || report.unrecognized)) {resetImportPreview(); message('Unrecognized or ambiguous output cannot clear the remainder. Choose failed books manually, or remove the output only after verifying every remaining title in Goodreads.'); return;}
  grClearItems = importPlan(report.items, true).imported;
  grReportKey = '';
  showImportPlan(importPlan(report.items, true));
}));
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
  if (grClearItems !== null) {
    const items = grClearItems; resetImportPreview();
    await confirmImportedTitles(items, $('#gr-batch').value);
    await loadGoodreads(); resetImportPreview(); grManualFailures = false; form.elements.output.value = ''; form.elements.confirm_remaining.checked = false;
    message(`${items.length} books confirmed imported and cleared from your queue. Skipped books remain excluded.`); return;
  }
  const payload = {batch_id: $('#gr-batch').value, text: form.elements.output.value, confirm_remaining: form.elements.confirm_remaining.checked};
  if (grManualFailures) payload.failed_ids = [...$('#gr-failures').querySelectorAll('input:checked')].map(i => i.value);
  const key = JSON.stringify(payload), preview = key !== grReportKey;
  const planToApply = grPendingPlan;
  const skipPresent = preview ? grBooks.some(b => b.skipped) : Boolean(planToApply?.skipped.length);
  if (!preview) resetImportPreview();
  const result = await api('/api/goodreads/reconcile', {...payload, confirm_remaining: payload.confirm_remaining && !skipPresent, preview});
  renderImportItems(result.items);
  if (preview) {
    $('#gr-clear-imports').hidden = result.items.some(b => b.proposed_failed);
    if (result.ambiguous || result.unrecognized || result.no_op) {resetImportPreview(); message('Nothing changed. For no failures, verify the titles in Goodreads and use “I already imported these”. For ambiguity, choose failed books manually. Unknown text alone cannot clear imports.');}
    else {grReportKey = key; showImportPlan(importPlan(result.items, payload.confirm_remaining && result.can_confirm_remaining));}
    return;
  }
  const plan = importPlan(result.items, payload.confirm_remaining && result.can_confirm_remaining);
  if (skipPresent) await confirmImportedTitles(plan.imported, payload.batch_id);
  await loadGoodreads(); resetImportPreview(); grManualFailures = false; form.elements.output.value = ''; form.elements.confirm_remaining.checked = false;
  message(`Batch finished: ${result.can_confirm_remaining ? planToApply.imported.length : 0} books confirmed imported and cleared from your queue; ${planToApply.failed.length} failed books returned to review; ${planToApply.skipped.length} skipped books remain excluded.`);
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
