'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../resource/client/ui-web/js/node-switch.js'), 'utf8');

function fixture() {
  const buttons = [0, 1].map(i => ({attrs: {'data-node-index': String(i)}, label: '切换',
    disabled: false, className: i === 0 ? 'btn-success' : 'btn-outline-secondary'}));
  const requests = [];
  let reloads = 0;
  function $(value) {
    const list = typeof value === 'string' ? buttons : [value];
    return {
      attr(key, val) { if (val === undefined) return list[0].attrs[key]; list.forEach(b => b.attrs[key] = val); return this; },
      prop(key, val) { list.forEach(b => b[key] = val); return this; },
      text(val) { if (val === undefined) return list[0].label; list.forEach(b => b.label = val); return this; }
    };
  }
  $.post = (url, params) => {
    const request = {url, params}; requests.push(request);
    return {done(fn) { request.success = fn; return this; }, always(fn) { request.complete = fn; return this; }};
  };
  const window = {location: {reload() { reloads++; }}};
  vm.runInNewContext(source, {jQuery: $, window});
  return {buttons, requests, window, reloads: () => reloads};
}

const fail = fixture();
fail.window.buttonSwitch(fail.buttons[1]);
assert.equal(fail.buttons[0].className, 'btn-success');
assert.equal(fail.buttons[1].className, 'btn-outline-secondary');
assert(fail.buttons.every(b => b.disabled));
assert.equal(fail.reloads(), 0);
fail.window.buttonSwitch(fail.buttons[0]);
assert.equal(fail.requests.length, 1);
fail.requests[0].complete(); // HTTP failure: success callback does not run.
assert(fail.buttons.every(b => !b.disabled));
assert.equal(fail.buttons[1].label, '切换');
assert.equal(fail.buttons[0].className, 'btn-success');
assert.equal(fail.reloads(), 0);

const success = fixture();
success.window.buttonSwitch(success.buttons[1]);
assert.equal(success.requests[0].params.nodenum, '1');
success.requests[0].success(); success.requests[0].complete();
assert.equal(success.reloads(), 1);

const invalid = fixture();
invalid.buttons[1].attrs['data-node-index'] = '1;bad';
invalid.window.buttonSwitch(invalid.buttons[1]);
assert.equal(invalid.requests.length, 0);
console.log('Node switch UI: pending, failure, success and invalid-index checks passed.');
