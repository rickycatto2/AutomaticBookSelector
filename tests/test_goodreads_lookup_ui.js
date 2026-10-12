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
  let automatic = false, hold;
  const context = vm.createContext({console, Set, Date, Intl, encodeURIComponent, document: {querySelectorAll: () => []}, location: {hash: ''},
    $, el: (...args) => new Element(...args), message: (...args) => messages.push(args),
    action: async (button, fn) => {try {await fn();} catch (e) {messages.push([e.message, true]);}},
    api: async (path, payload) => {
      calls.push([path, payload]);
      if (path === '/api/goodreads') return {books: [{...book, ready: automatic}], excluded: 0, batches: []};
      if (path === '/api/goodreads/auto-resolve') {if (hold) await hold; automatic = true; return {results: [{automatic: true}]};}
      if (path === '/api/goodreads/resolve') throw new Error('Public catalog unavailable; review manually.');
      throw new Error('Unexpected API ' + path);
    }});
  const source = fs.readFileSync('static/companion.js', 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('function displayNumber')), context);
  await context.loadGoodreads();
  assert.equal(calls.filter(c => c[0].endsWith('auto-resolve')).length, 1, 'no per-card loop or recursive auto retry');
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
  const pending = context.loadGoodreads();
  await new Promise(resolve => setImmediate(resolve));
  card = $('#gr-books').children[0];
  card.all().find(n => n.name === 'title').value = 'Unsaved user edit';
  for (const fn of $('#gr-books').listeners.input) fn();
  release(); await pending;
  assert.equal($('#gr-books').children[0], card, 'slow automatic response must not discard the open form');
  assert.equal(card.all().find(n => n.name === 'title').value, 'Unsaved user edit');
  console.log('Goodreads UI synthetic checks passed');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
