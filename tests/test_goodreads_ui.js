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
let ambiguous = false, reloads = 0, noFailures = false, failTitle = '', extraItems = [];
const context = vm.createContext({$, grBooks: [{id: 'skip', skipped: true}], el: (tag, cls, text) => Object.assign(new Element(tag), {textContent: text}),
  action: async (button, callback) => {
    if (button.disabled) return;
    button.disabled = true;
    try {await callback();} catch (error) {messages.push(error.message);} finally {button.disabled = false;}
  }, message: text => messages.push(text),
  loadGoodreads: async () => {reloads++;},
  api: async (path, payload) => {
    calls.push({path, payload});
    if (path === '/api/goodreads/confirm-imported' && payload.book_id === failTitle) throw new Error('Synthetic stale title');
    return {items: [{id: 'one', title: '<script>synthetic</script>', author: 'Writer', isbn: '9780306406157',
      state: 'exported', proposed_failed: !noFailures && (!ambiguous || Boolean(payload.failed_ids?.length))},
      {id: 'two', title: 'Two', state: 'exported'}, {id: 'skip', title: 'Skipped', state: 'exported'}, ...extraItems],
      flagged: ['One'], mixed: true, ambiguous: ambiguous && !payload.failed_ids?.length,
       unrecognized: noFailures && payload.text === 'unknown', no_op: noFailures && !payload.text, can_confirm_remaining: !ambiguous};
  }});
const source = fs.readFileSync('static/companion.js', 'utf8');
vm.runInContext(source.slice(source.indexOf('async function chooseFailedBooks()'), source.indexOf('function displayNumber(')), context);
const submit = () => form.listeners.submit({preventDefault() {}, currentTarget: form});
// Event handlers do not return the action Promise; drain their async continuations.
const settle = async () => {await new Promise(resolve => setImmediate(resolve));};
(async () => {
   assert.equal(calls.length, 0); // wiring/load never clears anything
   form.elements.confirm_remaining.checked = true;
  form.elements.output.value = 'synthetic mixed report';
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, true);
  assert.equal(reloads, 0);
   assert.equal(button.textContent, 'Confirm: apply this plan and clear imported books');
   assert.ok($('#gr-import-summary').textContent.includes('1 books will be marked imported'));
   assert.ok($('#gr-import-summary').textContent.includes('1 failed books will go back to review'));
   assert.ok($('#gr-import-summary').textContent.includes('1 skipped books stay excluded'));
  const label = $('#gr-failures').children[0];
  assert.ok(label.children[1].textContent.includes('<script>synthetic</script>')); // text only
  submit(); await settle();
   assert.equal(calls.findLast(c => c.path === '/api/goodreads/reconcile').payload.preview, false);
   assert.equal(calls.at(-1).path, '/api/goodreads/confirm-imported');
   assert.equal(calls.at(-1).payload.book_id, 'two');
  assert.equal(reloads, 1);
  ambiguous = true;
  form.elements.output.value = 'unknown report';
  form.elements.output.listeners.input();
  submit(); await settle();
   assert.equal(button.textContent, 'Finish this batch: clear imports, keep failures');
  const check = $('#gr-failures').children[0].children[0];
  check.checked = true; check.listeners.change();
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, true);
  assert.deepEqual(Array.from(calls.at(-1).payload.failed_ids), ['one']);
   assert.equal(button.textContent, 'Confirm: apply this plan and clear imported books');
   form.elements.confirm_remaining.checked = false;
  form.elements.confirm_remaining.listeners.change();
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, true); // confirmation changes need a new preview
  submit(); await settle();
  assert.equal(calls.at(-1).payload.preview, false);
   assert.equal(reloads, 2);
   ambiguous = false; noFailures = true;
   for (const text of ['', 'Imported: 9780306406157']) {
     form.elements.output.value = text; form.elements.output.listeners.input();
     const before = calls.filter(c => c.path === '/api/goodreads/confirm-imported').length;
     if (!text) {
       submit(); await settle();
       assert.equal(calls.at(-1).payload.preview, true);
       assert.equal(button.textContent, 'Finish this batch: clear imports, keep failures');
       assert.equal($('#gr-clear-imports').hidden, false);
       assert.equal(calls.filter(c => c.path === '/api/goodreads/confirm-imported').length, before);
     }
     await $('#gr-clear-imports').listeners.click();
     assert.equal(calls.at(-1).payload.preview, true);
     assert.equal(calls.filter(c => c.path === '/api/goodreads/confirm-imported').length, before);
     assert.ok($('#gr-import-summary').textContent.includes('2 books will be marked imported'));
     assert.ok($('#gr-import-summary').textContent.includes('0 failed books'));
     assert.equal($('#gr-clear-imports').hidden, true); // one visible apply control
     submit(); await settle(); await settle();
     assert.equal(calls.filter(c => c.path === '/api/goodreads/confirm-imported').length, before + 2);
     assert.ok(!calls.filter(c => c.path === '/api/goodreads/confirm-imported').some(c => c.payload.book_id === 'skip'));
   }
   form.elements.output.value = 'unknown'; form.elements.output.listeners.input();
   await $('#gr-clear-imports').listeners.click();
   assert.equal(button.textContent, 'Finish this batch: clear imports, keep failures');
   assert.ok(messages.at(-1).includes('cannot clear'));
   // Both non-atomic paths report only completed requests, retain the server
   // error, reload reality, and consume the plan before any asynchronous work.
   for (const clearDirectly of [true, false]) {
     noFailures = clearDirectly;
     extraItems = [{id: 'three', title: 'Three', state: 'exported'}, {id: 'four', title: 'Four', state: 'exported'}];
     failTitle = clearDirectly ? 'two' : 'three';
     form.elements.output.value = clearDirectly ? '' : 'synthetic mixed report';
     form.elements.output.listeners.input();
     form.elements.confirm_remaining.checked = true;
     if (clearDirectly) await $('#gr-clear-imports').listeners.click();
     else {submit(); await settle();}
     const beforeReloads = reloads, beforeCalls = calls.length;
     submit();
     assert.equal(button.disabled, true);
     assert.equal(vm.runInContext('grPendingPlan === null && grClearItems === null', context), true);
     submit(); // disabled apply button cannot run the same plan twice
     await settle();
     assert.equal(reloads, beforeReloads + 1);
     assert.equal(messages.at(-1), `Confirmed 1 of ${clearDirectly ? 4 : 3} before the failure; the rest are still in your queue. Review and repeat. Synthetic stale title`);
     assert.deepEqual(calls.slice(beforeCalls).filter(c => c.path === '/api/goodreads/confirm-imported').map(c => c.payload.book_id), clearDirectly ? ['one', 'two'] : ['two', 'three']);
     assert.equal(button.disabled, false);
   }
   console.log('Goodreads synthetic preview/selection/save UI checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
