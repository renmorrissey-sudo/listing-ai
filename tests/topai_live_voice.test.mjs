import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const source = readFileSync(new URL('../static/topai_live_voice.js', import.meta.url), 'utf8').replace(/^import Vapi.*\r?\n/, '');
class Element {
  constructor() { this.dataset = {}; this.children = []; this.listeners = {}; this.hidden = true; this.textContent = ''; this.parent = null; }
  append(...children) { children.forEach((child) => { if (child && typeof child === 'object') child.parent = this; }); this.children.push(...children); }
  appendChild(child) { this.append(child); }
  replaceChildren(...children) { this.children = children; }
  get lastChild() { return this.children[this.children.length - 1]; }
  querySelector(selector) {
    if (selector === '[data-empty]') return this.children.find((child) => child.dataset?.empty) || null;
    const match = selector.match(/^\[data-partial="(.+)"\]$/);
    return match ? this.children.find((child) => child.dataset?.partial === match[1]) || null : null;
  }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
  removeAttribute(name) { if (name === 'data-partial') delete this.dataset.partial; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(type, cb) { this.listeners[type] = cb; }
}
function environment({vapiConstructorThrows = false, nestedVapiExport = false} = {}) {
  const stored = new Map();
  const channels = [];
  const navigations = [];
  const calls = [];
  const requests = [];
  const openedWindows = [];
  const siteLocation = {origin: 'https://topai.test', pathname: '/app', assign(url) { navigations.push(url); }};
  let resolver = async () => ({ok: true, json: async () => ({url: '/crm/leads/42', name: 'Mark Smith', lead_id: 42})});
  class Channel {
    constructor(name) { this.name = name; channels.push(this); }
    addEventListener(type, cb) { this.receive = cb; }
    postMessage(data) { for (const peer of channels) if (peer !== this && peer.name === this.name) peer.receive?.({data}); }
  }
  class Vapi {
    constructor() {
      if (vapiConstructorThrows) throw new Error('SDK construction failed');
      this.events = {}; this.starts = 0; this.stops = 0; this.sent = []; calls.push(this);
    }
    on(name, cb) { this.events[name] = cb; }
    async start(...args) { this.starts++; this.startArgs = args; this.events['call-start'](); }
    stop() { this.stops++; }
    send(message) { this.sent.push(message); }
  }
  function page(mode = 'site', pathname = '/app', broadcast = true) {
    const elements = new Map();
    for (const suffix of ['button', 'panel', 'end', 'status', 'transcript', 'config', 'history-toggle', 'history', 'history-list']) elements.set(`topai-live-${suffix}`, new Element());
    elements.get('topai-live-config').textContent = JSON.stringify({mode, userId: 1, sessionId: mode === 'window' ? 'session-1' : '', configured: true, publicKey: 'public', assistantId: 'assistant', assistantConfig: {firstMessage: 'Hi', model: {provider: 'openai'}}});
    const listeners = {};
    const window = {
      location: {origin: 'https://topai.test', pathname},
      opener: {closed: false, location: siteLocation},
      open(url, name, features) {
        const popup = {closed: false, focus() {}, location: {href: url || 'about:blank', replace(next) { this.href = next; }}};
        openedWindows.push({url, name, features, popup});
        return popup;
      },
      addEventListener(type, cb) { listeners[type] = cb; },
      setInterval() {},
    };
    if (broadcast) window.BroadcastChannel = Channel;
    const context = vm.createContext({
      window,
      Vapi: nestedVapiExport ? {default: {default: Vapi}} : Vapi,
      BroadcastChannel: Channel, URL, console: {debug() {}, error() {}},
      document: {getElementById: (id) => elements.get(id), body: {classList: {contains: () => mode === 'window'}}, createElement: () => new Element(), createTextNode: (text) => ({textContent: text})},
      localStorage: {getItem: (key) => stored.get(key) || null, setItem: (key, value) => stored.set(key, value), removeItem: (key) => stored.delete(key)},
      crypto: {randomUUID: () => 'session-new'},
      fetch: async (url, options) => {
        if (url === '/api/live-voice/open-lead') { requests.push(JSON.parse(options.body)); return resolver(); }
        return {ok: true, json: async () => ({})};
      },
    });
    vm.runInContext(source, context);
    return {elements, window, listeners};
  }
  return {page, calls, requests, navigations, openedWindows, stored, setResolver: (fn) => {resolver = fn;}};
}
const settle = () => new Promise((resolve) => setImmediate(resolve));
const tool = (id = 'open-1') => ({type: 'tool-calls', toolCallList: [{id, function: {name: 'open_lead', arguments: JSON.stringify({lead_name: 'Mark Smith'})}}]});

test('site button opens a new live session at its final URL', async () => {
  const env = environment();
  const page = env.page('site');
  await page.elements.get('topai-live-button').listeners.click();
  assert.equal(env.openedWindows.length, 1);
  assert.equal(env.openedWindows[0].url, '/ask-topai-live?session_id=session-new');
  assert.equal(env.openedWindows[0].name, 'topaiAskLive-1');
  assert.match(env.openedWindows[0].features, /popup/);
});

test('call window reports an SDK startup failure instead of staying blank', () => {
  const env = environment({vapiConstructorThrows: true});
  const page = env.page('window');
  page.elements.get('topai-live-button').listeners.click();
  assert.equal(page.elements.get('topai-live-panel').hidden, false);
  assert.equal(page.elements.get('topai-live-status').textContent, 'Disconnected');
  assert.equal(page.elements.get('topai-live-transcript').children[0].textContent, 'TopAI could not connect. Please try again.');
});

test('call window resolves the CDN nested default export', () => {
  const env = environment({nestedVapiExport: true});
  const page = env.page('window');
  page.elements.get('topai-live-button').listeners.click();
  assert.equal(env.calls.length, 1);
  assert.equal(env.calls[0].starts, 1);
});

test('opening a lead overlaps speech; page navigation reattaches to the same call', async () => {
  const env = environment();
  const popup = env.page('window');
  const vapi = env.calls[0];
  assert.equal(vapi.starts, 0);
  popup.elements.get('topai-live-button').listeners.click();
  assert.equal(vapi.starts, 1);
  assert.equal(vapi.startArgs.length, 1);
  assert.equal(vapi.startArgs[0].firstMessage, 'Hi');
  let finish;
  env.setResolver(() => new Promise((resolve) => {finish = resolve;}));
  vapi.events.message(tool());
  vapi.events['speech-start']();
  assert.equal(vapi.stops, 0);
  assert.equal(env.navigations.length, 0);
  finish({ok: true, json: async () => ({url: '/crm/leads/42', name: 'Mark Smith', lead_id: 42})});
  await settle();
  assert.deepEqual(env.navigations, ['https://topai.test/crm/leads/42']);
  assert.equal(vapi.starts, 1);
  assert.equal(vapi.stops, 0);
  vapi.events.message({type: 'transcript', role: 'assistant', transcriptType: 'final', transcript: 'Let us review Mark together.'});
  const leadPage = env.page('site', '/crm/leads/42');
  assert.equal(leadPage.elements.get('topai-live-panel').dataset.state, 'speaking');
  assert.equal(leadPage.elements.get('topai-live-transcript').children.length, 1);
  assert.ok(vapi.sent.some((m) => m.message.content.includes('page has loaded')));
  leadPage.listeners.pagehide?.();
  env.page('site', '/crm/leads');
  assert.equal(vapi.starts, 1);
  assert.equal(vapi.stops, 0);
  assert.ok(vapi.sent.every((m) => m.triggerResponse === false));
  vapi.events.message(tool());
  await settle();
  assert.equal(env.requests.length, 1);
});

test('ambiguous lead and unsafe destination never navigate or stop the call', async () => {
  const env = environment(); const popup = env.page('window'); const vapi = env.calls[0];
  popup.elements.get('topai-live-button').listeners.click();
  env.setResolver(async () => ({ok: false, json: async () => ({error: 'More than one lead matches.'})}));
  vapi.events.message(tool()); await settle();
  assert.equal(env.navigations.length, 0);
  assert.ok(vapi.sent[0].message.content.includes('More than one lead'));
  env.setResolver(async () => ({ok: true, json: async () => ({url: 'https://other.test/crm/leads/42'})}));
  vapi.events.message(tool('open-2')); await settle();
  assert.equal(env.navigations.length, 0);
  assert.equal(vapi.stops, 0);
});

test('storage fallback restores speaking state and only ends the matching session', () => {
  const env = environment(); const popup = env.page('window', '/ask-topai-live', false); const vapi = env.calls[0];
  popup.elements.get('topai-live-button').listeners.click();
  vapi.events['speech-start']();
  const page = env.page('site', '/crm/leads/42', false);
  const key = 'topai-live-active-session-1-ping';
  page.listeners.storage({key, newValue: JSON.stringify({type: 'state', sessionId: 'session-1', state: 'speaking', turns: []})});
  assert.equal(page.elements.get('topai-live-panel').dataset.state, 'speaking');
  popup.listeners.storage({key, newValue: JSON.stringify({type: 'end-requested', sessionId: 'different'})});
  assert.equal(vapi.stops, 0);
  popup.listeners.storage({key, newValue: JSON.stringify({type: 'end-requested', sessionId: 'session-1'})});
  assert.equal(vapi.stops, 1);
  popup.listeners.storage({key, newValue: JSON.stringify({type: 'end-requested', sessionId: 'session-1'})});
  assert.equal(vapi.stops, 1);
});

test('an unvalidated interruption never injects a duplicate continuation response', () => {
  const env = environment(); const popup = env.page('window'); const vapi = env.calls[0];
  popup.elements.get('topai-live-button').listeners.click();
  vapi.events['speech-start']();
  vapi.events.message({type: 'user-interrupted'});
  assert.equal(vapi.sent.length, 0);
  assert.equal(popup.elements.get('topai-live-status').textContent, 'Listening');
});

test('a final transcript replaces its partial line and repeated finals are ignored', () => {
  const env = environment(); const popup = env.page('window'); const vapi = env.calls[0];
  popup.elements.get('topai-live-button').listeners.click();
  const transcript = popup.elements.get('topai-live-transcript');
  vapi.events.message({type: 'transcript', role: 'assistant', transcriptType: 'partial', transcript: 'Here is the'});
  assert.equal(transcript.children.length, 1);
  vapi.events.message({type: 'transcript', role: 'assistant', transcriptType: 'final', transcript: 'Here is the answer.'});
  assert.equal(transcript.children.length, 1);
  assert.equal(transcript.children[0].lastChild.textContent, 'Here is the answer.');
  assert.equal(transcript.children[0].dataset.partial, undefined);
  vapi.events.message({type: 'transcript', role: 'assistant', transcriptType: 'final', transcript: 'Here is the answer.'});
  assert.equal(transcript.children.length, 1);
  vapi.events.message({type: 'transcript', role: 'assistant', transcriptType: 'partial', transcript: 'Here is the answer'});
  vapi.events.message({type: 'transcript', role: 'assistant', transcriptType: 'final', transcript: 'Here is the answer.'});
  assert.equal(transcript.children.length, 1);
});

test('opening History switches the panel into the expanded history layout', async () => {
  const env = environment(); const page = env.page('site');
  const panel = page.elements.get('topai-live-panel');
  const toggle = page.elements.get('topai-live-history-toggle');
  await toggle.listeners.click();
  assert.equal(panel.dataset.historyOpen, 'true');
  assert.equal(toggle['aria-expanded'], 'true');
  await toggle.listeners.click();
  assert.equal(panel.dataset.historyOpen, 'false');
  assert.equal(toggle['aria-expanded'], 'false');
});

test('a pending lead lookup cannot navigate after the user ends the call', async () => {
  const env = environment(); const popup = env.page('window'); const vapi = env.calls[0];
  popup.elements.get('topai-live-button').listeners.click();
  let finish; env.setResolver(() => new Promise((resolve) => {finish = resolve;}));
  vapi.events.message(tool());
  await popup.elements.get('topai-live-end').listeners.click();
  finish({ok: true, json: async () => ({url: '/crm/leads/42'})}); await settle();
  assert.equal(env.navigations.length, 0);
  assert.equal(vapi.stops, 1);
});
