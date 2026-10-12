/* Dependency-free synthetic DOM/API test of the preview/save flow. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag = '') {this.tag = tag; this.children = []; this.listeners = {}; this.value = ''; this.checked = false;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children = children;}
  addEventListener(event, callback) {this.listeners[event] = callback;}
  querySelectorAll(selector) {
    const found = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
    return found.filter(child => child.tag === 'input' && child.checked);
  }
  querySelector() {return button;}
}
const nodes = new Map();
const $ = selector => {if (!nodes.has(selector)) nodes.set(selector, new Element()); return nodes.get(selector);};
const form = $('#gr-reconcile-form'), button = $('#gr-reconcile-form [type=submit]');
form.elements = {output: new Element(), confirm_remaining: new Element()};
$('#gr-batch').value = 'synthetic-batch';
const calls = [], messages = [];
let ambiguous = false, reloads = 0;
const context = vm.createContext({$, el: (tag, cls, text) => Object.assign(new Element(tag), {textContent: text}),
  action: (button, callback) => callback(), message: text => messages.push(text),
  loadGoodreads: async () => {reloads++;},
  api: async (path, payload) => {
    calls.push({path, payload});
    return {items: [{id: 'one', title: '<script>synthetic</script>', author: 'Writer', isbn: '9780306406157',
      state: 'exported', proposed_failed: !ambiguous || Boolean(payload.failed_ids?.length)}],
      flagged: ['One'], mixed: true, ambiguous: ambiguous && !payload.failed_ids?.length,
      unrecognized: false, no_op: false, can_confirm_remaining: true};
  }});
const source = fs.readFileSync('static/companion.js', 'utf8');
vm.runInContext(source.slice(source.indexOf('async function chooseFailedBooks()'), source.indexOf('function displayNumber(')), context);
const submit = () => form.listeners.submit({preventDefault() {}, currentTarget: form});
// Event handlers do not return the action Promise; drain their async continuations.
const settle = async () => {await Promise.resolve(); await Promise.resolve(); await Promise.resolve();};
(async () => {
  form.elements.output.value = 'synthetic mixed report';
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, true);
  assert.equal(reloads, 0);
  assert.equal(button.textContent, 'Save reviewed import result');
  const label = $('#gr-failures').children[0];
  assert.ok(label.children[1].textContent.includes('<script>synthetic</script>')); // text only
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, false);
  assert.equal(reloads, 1);
  ambiguous = true;
  form.elements.output.value = 'unknown report';
  form.elements.output.listeners.input();
  submit(); await settle();
  assert.equal(button.textContent, 'Preview import result');
  const check = $('#gr-failures').children[0].children[0];
  check.checked = true; check.listeners.change();
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, true);
  assert.deepEqual(Array.from(calls.at(-1).payload.failed_ids), ['one']);
  assert.equal(button.textContent, 'Save reviewed import result');
  form.elements.confirm_remaining.checked = true;
  form.elements.confirm_remaining.listeners.change();
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, true); // confirmation changes need a new preview
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, false);
  assert.equal(reloads, 2);
  console.log('Goodreads synthetic preview/selection/save UI checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
