/* Synthetic DOM/API only. Run: node tests/test_goodreads_lookup_ui.js */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag, className = '', text = '') {Object.assign(this, {tag, className, textContent: text, children: [], listeners: {}, dataset: {}, value: '', hidden: false});}
  append(...nodes) {this.children.push(...nodes);}
  replaceChildren(...nodes) {this.children = nodes;}
  setAttribute() {}
  addEventListener(name, fn) {(this.listeners[name] ||= []).push(fn);}
  removeEventListener(name, fn) {this.listeners[name] = (this.listeners[name] || []).filter(f => f !== fn);}
  all() {return this.children.flatMap(n => [n, ...n.all()]);}
  get elements() {return Object.fromEntries(this.all().filter(n => n.name).map(n => [n.name, n]));}
  querySelector() {return this.all().find(n => n.type === 'submit') || new Element('button');}
  querySelectorAll() {return [];}
}
async function main() {
  const nodes = new Map(), calls = [], messages = [];
  const $ = selector => {if (!nodes.has(selector)) nodes.set(selector, new Element('div')); return nodes.get(selector);};
  for (const name of ['output', 'confirm_remaining']) {const n = new Element('input'); n.name = name; $('#gr-reconcile-form').append(n);}
  const book = {id: 'synthetic', title: 'One', original_title: 'One', author: 'Writer', original_author: 'Writer', isbn: '', url: '', completed_date: '2026-10-01', reason: 'Review', provenance: 'Source', ready: false};
   let automatic = false, hold, suggestion = false;
  const context = vm.createContext({console, Set, Date, Intl, encodeURIComponent, document: {querySelectorAll: () => []}, location: {hash: ''},
    $, el: (...args) => new Element(...args), message: (...args) => messages.push(args),
    action: async (button, fn) => {try {await fn();} catch (e) {messages.push([e.message, true]);}},
    api: async (path, payload) => {
      calls.push([path, payload]);
      if (path === '/api/goodreads') return {books: [{...book, ready: automatic && !book.skipped}], excluded: 0, batches: []};
       if (path === '/api/goodreads/skip') {book.skipped = payload.ignored; return {skipped: book.skipped};}
       if (path === '/api/goodreads/save') return {saved: true};
      if (path === '/api/goodreads/auto-resolve') {if (hold) await hold; automatic = true; return {results: [{automatic: true}]};}
       if (path === '/api/goodreads/resolve') {
         if (suggestion) return {results: [{automatic: false, candidate: {title: 'Catalog title', author: 'Catalog author', isbn: '9780306406157'}, reason: 'Synthetic suggestion', provenance: 'Catalog'}]};
         throw new Error('Public catalog unavailable; review manually.');
       }
      throw new Error('Unexpected API ' + path);
    }});
  const source = fs.readFileSync('static/companion.js', 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('function displayNumber')), context);
  await context.loadGoodreads();
   assert.equal(calls.filter(c => c[0].endsWith('auto-resolve')).length, 0, 'catalog matching is optional; list loads make no provider calls');
   assert.ok($('#gr-books').children[0].all().some(n => n.tag === 'img' && n.src === '/api/cover?id=synthetic'));
  let card = $('#gr-books').children[0];
  let lookup = card.all().find(n => n.textContent === 'Get ISBN from link');
  await lookup.listeners.click[0]();
  assert.match(card.all().find(n => n.className === 'error-note').textContent, /needs a Goodreads book link/);
  assert.equal(card.all().find(n => n.className === 'error-note').hidden, false);
  assert.equal(calls.filter(c => c[0].endsWith('/lookup')).length, 0);
  await card.all().find(n => n.textContent === 'Find work ISBN (free catalogs)').listeners.click[0]();
  assert.match(card.all().find(n => n.className === 'error-note').textContent, /Public catalog unavailable/);
  let release;
  hold = new Promise(resolve => {release = resolve;});
   const pending = context.loadGoodreads(true); // legacy bounded endpoint remains explicitly callable
  await new Promise(resolve => setImmediate(resolve));
  card = $('#gr-books').children[0];
  card.all().find(n => n.name === 'title').value = 'Unsaved user edit';
  for (const fn of $('#gr-books').listeners.input) fn();
  release(); await pending;
  assert.equal($('#gr-books').children[0], card, 'slow automatic response must not discard the open form');
  assert.equal(card.all().find(n => n.name === 'title').value, 'Unsaved user edit');
  // New print/ebook search link: same origin-safe Google query with format terms.
  const search = card.all().find(n => n.textContent === 'Find Goodreads match');
  const printSearch = card.all().find(n => n.textContent === 'Find print/ebook edition');
  assert.ok(printSearch && printSearch.target === '_blank' && printSearch.rel === 'noopener noreferrer');
  assert.ok(printSearch.href.startsWith('https://www.google.com/search?q='));
  const queryPart = link => link.href.slice(link.href.indexOf('?q=') + 3);
  // Hostile title/author input (spaces, &, +, quotes, formula triggers) stays percent-encoded.
   const form = card.all().find(n => n.className === 'gr-form');
   suggestion = true;
   form.elements.title.value = 'Edited public title'; form.elements.author.value = 'Edited public author';
   form.elements.isbn.value = 'B0CHYSM5M4';
   await card.all().find(n => n.textContent === 'Find work ISBN (free catalogs)').listeners.click[0]();
   const explicit = calls.filter(c => c[0].endsWith('/resolve')).at(-1)[1];
   assert.equal(explicit.title, 'Edited public title'); assert.equal(explicit.author, 'Edited public author');
   assert.equal(form.elements.title.value, 'Edited public title', 'lookup retains unsaved title');
   assert.equal(form.elements.author.value, 'Edited public author', 'lookup retains unsaved author');
   assert.equal(form.elements.isbn.value, 'B0CHYSM5M4', 'catalog suggestion does not replace unsaved ASIN');
   assert.equal($('#gr-books').children[0], card, 'explicit lookup never reloads unsaved forms');
   assert.ok(card.all().some(n => n.textContent.includes('catalog ISBN 9780306406157')));
   form.elements.isbn.value = '';
  form.elements.title.value = '=cmd|calc "Cleat Cute" +2';
   form.elements.author.value = 'Amy + "Bob" & Co @x, Narrator; Other';
  for (const fn of search.listeners.click) fn();
  for (const fn of printSearch.listeners.click) fn();
  for (const link of [search, printSearch]) {
    assert.ok(!/[&+= #"'<>]/.test(queryPart(link)), 'raw query must contain no unencoded URL or formula characters: ' + link.href);
  }
  assert.equal(decodeURIComponent(queryPart(search)),
      'goodreads =cmd|calc "Cleat Cute" +2 Amy + "Bob" & Co @x audiobook');
  assert.equal(decodeURIComponent(queryPart(printSearch)),
      'goodreads =cmd|calc "Cleat Cute" +2 Amy + "Bob" & Co @x audiobook paperback OR hardcover OR print');
   const noiseMarkers = ['unabridged', 'abridged', 'audiobook', 'audiobook edition', 'audio book', 'audio edition', 'large print', 'ebook', 'e-book', 'kindle', 'kindle edition', 'digital edition', 'cd', 'mp3', 'audio cd', 'unabridged audio'];
   const titleCases = [
     ...noiseMarkers.flatMap(marker => [[`The (${marker}) Title`, 'The Title'], [`The [${marker.toUpperCase()}] Title`, 'The Title']]),
     ['[Audio CD] The (UnAbRiDgEd) Title [Kindle Edition]', 'The Title'],
     ['The Title (Book 2) [Volume 3] (A Novel): Subtitle', 'The Title (Book 2) [Volume 3] (A Novel): Subtitle'],
     ['The Title (Illustrated) [Not abridged] (Unabridged version)', 'The Title (Illustrated) [Not abridged] (Unabridged version)'],
     ['(Unabridged) [Ebook]', '(Unabridged) [Ebook]'],
   ];
   form.elements.author.value = 'Malcolm Gladwell, Narrator';
   for (const [title, cleaned] of titleCases) {
     form.elements.title.value = title;
     for (const [link, tail] of [[search, 'audiobook'], [printSearch, 'audiobook paperback OR hardcover OR print']]) {
       for (const fn of link.listeners.click) fn();
       assert.equal(decodeURIComponent(queryPart(link)), `goodreads ${cleaned} Malcolm Gladwell ${tail}`);
       assert.equal(form.elements.title.value, title, 'search clicks never rewrite the form title');
     }
   }
   // Build and click both links, then submit through the real Save handler.
   const absTitle = 'The American Way of Killing (Unabridged)';
   book.title = absTitle;
   await context.loadGoodreads();
   const savedCard = $('#gr-books').children[0], savedForm = savedCard.all().find(n => n.className === 'gr-form');
   for (const label of ['Find Goodreads match', 'Find print/ebook edition']) {
     const link = savedCard.all().find(n => n.textContent === label);
     assert.ok(decodeURIComponent(queryPart(link)).includes('goodreads The American Way of Killing Writer audiobook'));
     for (const fn of link.listeners.click) fn();
     assert.equal(savedForm.elements.title.value, absTitle, 'building/clicking links preserves exact ABS title');
   }
   savedForm.elements.isbn.value = '9780306406157';
   for (const fn of savedForm.listeners.submit) fn({preventDefault() {}});
   await new Promise(resolve => setImmediate(resolve));
   assert.equal(calls.filter(c => c[0] === '/api/goodreads/save').at(-1)[1].title, absTitle, 'saved/export payload keeps (Unabridged)');
   assert.equal(book.title, absTitle, 'search does not mutate source book');
   book.title = 'One';
   for (const author of ['Gracie Page, Venice Van Someren', 'Gracie Page; Venice Van Someren']) {
     form.elements.author.value = author;
     for (const link of [search, printSearch]) {
       for (const fn of link.listeners.click) fn();
       assert.match(decodeURIComponent(queryPart(link)), /Gracie Page audiobook/);
       assert.doesNotMatch(decodeURIComponent(queryPart(link)), /Venice/);
       assert.equal(form.elements.author.value, author, 'search never rewrites export authors');
     }
   }
   for (const [title, author] of [['', ''], [null, undefined], ['\uD800<svg onload="x"> &x=1#', "O'Writer (!)*; Narrator"]]) {
     form.elements.title.value = title; form.elements.author.value = author;
     for (const link of [search, printSearch]) {
       for (const fn of link.listeners.click) fn();
       const url = new URL(link.href);
       assert.equal(url.origin, 'https://www.google.com');
       assert.equal(url.hash, '');
       assert.deepEqual([...url.searchParams.keys()], ['q']);
       assert.ok(!/[&+= #"'<>!()*]/.test(queryPart(link)));
       assert.match(url.searchParams.get('q'), /audiobook/);
     }
   }
   assert.ok(card.all().some(n => n.textContent === 'Skip this book'));
    assert.ok(card.all().some(n => /excluded from exports and Goodreads suggestions until you Restore to queue/.test(n.textContent)));
   const html = fs.readFileSync('static/index.html', 'utf8');
   const help = html.match(/<aside id="gr-queue-help"[\s\S]*?<\/aside>/)[0];
    for (const text of ['How to clear your queue', 'rejection output', 'I checked that the other books in this batch imported successfully', 'still-exported', 'ambiguity', 'Confirm imported', 'Skip this book', 'Restore to queue', 'hidden from the queue', 'still inform your recommendations', 'never export to Goodreads', 'free catalogs', 'ABS-supplied ASIN', 'not Goodreads', 'read the exact counts', 'confirm the plan']) assert.ok(help.includes(text), text);
   assert.doesNotMatch(html + source, /clear[-_ ](?:everything|all|queue)|bulk[-_ ]clear/i);
  // ISBN direct link only appears for a checksum-valid ISBN, never asserting recognition.
  const isbnLink = card.all().find(n => n.textContent === 'Open Goodreads edition for this ISBN');
  assert.equal(isbnLink.hidden, true, 'no ISBN link without an ISBN');
  const isbnInput = form.elements.isbn, formInput = () => {for (const fn of form.listeners.input) fn({target: isbnInput});};
  isbnInput.value = '978-1-250-87330-9'; formInput();
  assert.equal(isbnLink.hidden, false);
  assert.equal(isbnLink.href, 'https://www.goodreads.com/book/isbn/9781250873309');
  isbnInput.value = '9781250873308'; formInput(); // bad checksum
  assert.equal(isbnLink.hidden, true, 'invalid checksum must not offer a link');
  isbnInput.value = '=1+1'; formInput(); // formula-style input
  assert.equal(isbnLink.hidden, true);
  isbnInput.value = '034541005X'; formInput(); // ISBN-10 with X check digit
  assert.equal(isbnLink.hidden, false);
  assert.equal(isbnLink.href, 'https://www.goodreads.com/book/isbn/034541005X');
   automatic = false; book.provider_error = true; book.retry_eligible = true; book.skipped = true;
  book.reason = 'Google Books [daily_quota]: Daily quota exceeded (HTTP 429).';
  await context.loadGoodreads(false);
  card = $('#gr-books').children[0];
    assert.equal(card.className, 'empty', 'skipped-only queue has no working card');
    assert.equal($('#gr-skipped-summary').textContent, 'Skipped (1)');
    assert.ok($('#gr-skipped-list').all().some(n => n.textContent === 'One — Writer'));
    automatic = true;
    await $('#gr-skipped-list').all().find(n => n.textContent === 'Restore to queue').listeners.click[0]();
    assert.equal(book.skipped, false);
    assert.equal($('#gr-skipped-summary').textContent, 'Skipped (0)');
    card = $('#gr-books').children[0];
    assert.equal(card.all().find(n => n.tag === 'input' && n.type === 'checkbox').disabled, false, 'restore re-enables ready export');
    automatic = false; await context.loadGoodreads(false); card = $('#gr-books').children[0];
   const status = card.all().find(n => n.tag === 'p' && n.textContent.includes('daily_quota'));
  assert.match(status.textContent, /Lookup could not run; export stays blocked/);
  assert.doesNotMatch(status.textContent, /Review required/);
   assert.ok(card.all().find(n => n.textContent === 'Retry provider-blocked books (up to 25)'));
   book.saved_decision = true; book.confirmed = false; book.isbn = '9798217058785';
   await context.loadGoodreads(false);
   card = $('#gr-books').children[0];
   await card.all().find(n => n.textContent === 'Find work ISBN (free catalogs)').listeners.click[0]();
   assert.equal(card.all().find(n => n.name === 'isbn').value, '9798217058785', 'catalog lookup retains even an unconfirmed user-saved reviewed ISBN');
   console.log('Goodreads UI synthetic checks passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
