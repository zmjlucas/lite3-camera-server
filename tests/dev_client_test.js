'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const DevClient = require('../src/web/dev_client.js');

function reply(body, status = 200, protocol = '1') {
  return {ok: status < 400, status, headers: {get: () => protocol}, json: async () => body};
}

function browserPage(name, api = {}) {
  const elements = new Map();
  const document = {
    hidden: true,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        textContent: '', addEventListener() {}, replaceChildren() {}, add() {}, removeAttribute() {}
      });
      return elements.get(id);
    },
    querySelector(selector) { return this.getElementById(selector); },
    querySelectorAll: () => [],
    addEventListener() {}
  };
  const context = vm.createContext({
    Lite3DevClient: function () { return {status: () => new Promise(() => {}), ...api}; },
    document, window: {addEventListener() {}}, setTimeout() {}, clearTimeout() {},
    AbortController, fetch: () => new Promise(() => {}), Option: function () {}
  });
  const html = fs.readFileSync(path.join(__dirname, '../src/web/' + name + '.html'), 'utf8');
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
  return {context, document, elements};
}

async function checkConsoleResponseShapes() {
  const status = {mode: 'readonly'};
  const {context, document} = browserPage('control', {acquire: async () => ({lease: 'owned', status})});
  context.updateState({status});
  assert.equal(vm.runInContext('state', context), null, 'status updates require the current unwrapped shape');
  document.hidden = false;
  await context.acquire();
  assert.equal(vm.runInContext('state', context), status, 'acquire reads its explicit status member');

  const arm = {observed: true, simulated: false, position_deg: {b: 12.5, s: 0, e: 0, w: 0}};
  for (const [body, expected] of [[arm, '12.5'], [{arm}, '未知']]) {
    const page = browserPage('control', {armState: async () => body});
    await page.context.readArm('armState');
    assert.equal(page.elements.get('joint-b').textContent, expected,
      'arm observations use the endpoint response directly');
  }
}

async function checkCameraCatalogShape() {
  const {context} = browserPage('camera');
  const source = {id: 'front', label: 'Front camera', device: '/dev/video4',
    backend: 'v4l2', available: true, error: null};
  const catalog = {sources: [source], selected: 'front'};
  assert.equal(await context.readCatalog(reply(catalog)), catalog);
  for (const field of ['available', 'error']) {
    const incomplete = {...source};
    delete incomplete[field];
    await assert.rejects(context.readCatalog(reply({sources: [incomplete], selected: 'front'})), /catalog/i);
  }
}

async function main() {
  const calls = [];
  const client = new DevClient(async (url, options) => {
    calls.push({url, ...options, body: options.body && JSON.parse(options.body)});
    return reply({mode: 'simulation', lease: 'owned'});
  });
  await client.status();
  assert.equal(calls[0].url, '/api/status');
  assert.equal(calls[0].method, 'GET');
  assert.equal(calls[0].body, undefined);
  await client.acquire('browser-console');
  assert.deepEqual(calls.at(-1).body, {owner: 'browser-console'});
  await client.velocity('owned', 7, {vx: 0.1, ttl: 0.4});
  assert.deepEqual(calls.at(-1).body, {lease: 'owned', seq: 7, vx: 0.1, vy: 0, yaw: 0, ttl: 0.4});
  await client.armJog('owned', 8, {joint: 'e', delta: 1, speed: 2});
  assert.deepEqual(calls.at(-1).body, {lease: 'owned', seq: 8, joint: 'e', delta: 1, speed: 2});
  await client.stop(null, {keepalive: true});
  assert.equal(calls.at(-1).url, '/api/dog/stop');
  assert.deepEqual(calls.at(-1).body, {});
  assert.equal(calls.at(-1).keepalive, true);
  await client.reset('revision-1');
  assert.deepEqual(calls.at(-1).body, {revision: 'revision-1'});
  await client.armState();
  assert.deepEqual(calls.at(-1).body, {});
  assert.equal(calls.at(-1).url, '/api/arm/state');
  for (const call of calls) {
    assert.equal(call.redirect, 'error');
    assert.equal(call.cache, 'no-store');
    assert.ok(call.signal instanceof AbortSignal);
  }

  const current = new DevClient(async () => reply({mode: 'disabled', future: true}));
  assert.equal((await current.status()).future, true);
  for (const protocol of [null, '2', '1, 1', '', ' 1', '1 ']) {
    const incompatible = new DevClient(async () => reply({}, 200, protocol));
    await assert.rejects(incompatible.status(), /protocol/i);
  }
  for (const body of [null, [], 3]) {
    await assert.rejects(new DevClient(async () => reply(body)).status(), /object/i);
  }
  let attempts = 0;
  const rejected = new DevClient(async () => {
    attempts++;
    return reply({error: 'Lease expired', code: 'conflict'}, 409);
  });
  await assert.rejects(rejected.stand('stale', 1), error => error.status === 409 && error.message === 'Lease expired');
  assert.equal(attempts, 1, 'commands must never retry automatically');
  const timedOut = new DevClient((url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  }));
  await assert.rejects(timedOut.request('status', undefined, {timeout: 5}), /aborted/);
  await checkConsoleResponseShapes();
  await checkCameraCatalogShape();
  console.log('PASS: browser client requests, protocol validation, response shapes, errors and timeout');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
