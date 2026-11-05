'use strict';
/**
 * Runs the real MIDI Remote script inside a Node `vm` sandbox against a
 * memoized-Proxy mock of `midiremote_api_v1`, simulates host/Cubase activity,
 * and prints the captured outbound SysEx frames as JSON for the Python-side
 * protocol conformance test (tests/test_js_protocol.py).
 *
 * Usage: node tests/js_mock_test.cjs <path/to/ClaudeCode_MCP_Bridge.js>
 */

const fs = require('fs');
const vm = require('vm');

const scriptPath = process.argv[2];
if (!scriptPath) {
  console.error('usage: node js_mock_test.cjs <script.js>');
  process.exit(2);
}
const code = fs.readFileSync(scriptPath, 'utf8');

// ---------------------------------------------------------------- mock API
const meta = new WeakMap(); // proxy -> state
const nodesByName = new Map(); // name -> proxy

const FACTORY_METHODS = new Set([
  'makeDeviceDriver', 'makeMidiInput', 'makeMidiOutput', 'makeDetectionUnit',
  'detectPortPair', 'makeKnob', 'makeButton', 'makeFader', 'makeLabelField',
  'makeTriggerPad', 'makeMixerBankZone', 'makeMixerBankChannel', 'makePage',
  'makeInsertEffectViewer', 'makeParameterValue', 'makeValueBinding',
  'makeActionBinding', 'makeCommandBinding', 'makeSubPageArea', 'makeSubPage',
  'makeControlLayerZone', 'makeControlLayer', 'makeCustomValueVariable',
]);

const DENIED_PROPS = new Set([
  'valueOf', 'toString', 'constructor', 'prototype', 'call', 'apply', 'bind',
  'then', 'length', 'name', 'inspect', 'nodeType', 'hasOwnProperty', 'isPrototypeOf',
]);

function makeNode(name) {
  const state = { name, props: new Map(), children: new Map(), calls: [] };
  const target = function () {};
  const proxy = new Proxy(target, {
    get(t, prop) {
      if (typeof prop === 'symbol') return undefined;
      if (DENIED_PROPS.has(prop)) {
        // return the real Function.prototype methods so primitive conversion
        // (valueOf/toString) still works; everything else behaves as absent
        return Function.prototype[prop];
      }
      if (state.props.has(prop)) return state.props.get(prop);
      if (!state.children.has(prop)) {
        state.children.set(prop, makeNode(name + '.' + String(prop)));
      }
      return state.children.get(prop);
    },
    set(t, prop, value) {
      if (typeof prop !== 'symbol') state.props.set(prop, value);
      return true;
    },
    apply(t, thisArg, args) {
      state.calls.push(args);
      const base = name.split('.').pop();
      if (FACTORY_METHODS.has(base)) {
        const n = factoryCounter.get(name) || 0;
        factoryCounter.set(name, n + 1);
        return makeNode(name + '#' + n);
      }
      // chainable methods return the receiver object, like the real API
      return thisArg && meta.has(thisArg) ? thisArg : proxy;
    },
  });
  meta.set(proxy, state);
  nodesByName.set(name, proxy);
  return proxy;
}

const factoryCounter = new Map();
const apiRoot = makeNode('api');

// ------------------------------------------------------------------ run
const sandbox = {
  require(name) {
    if (name === 'midiremote_api_v1') return apiRoot;
    throw new Error('unexpected require: ' + name);
  },
  console: { log() {} },
};
vm.runInNewContext(code, sandbox, { filename: 'ClaudeCode_MCP_Bridge.js' });

// ------------------------------------------------------------- helpers
function nodeByName(name) {
  const n = nodesByName.get(name);
  if (!n) throw new Error('mock node not found: ' + name);
  return n;
}
function callProp(proxy, prop, ...args) {
  const st = meta.get(proxy);
  const fn = st.props.get(prop);
  if (typeof fn !== 'function') throw new Error(`no callback ${prop} on ${st.name}`);
  return fn(...args);
}
function childCalls(proxy, prop) {
  const child = meta.get(proxy).children.get(prop);
  return child ? meta.get(child).calls : [];
}

const driver = nodeByName('api.makeDeviceDriver#0');
const midiInput = nodeByName('api.makeDeviceDriver#0.mPorts.makeMidiInput#0');
const midiOutput = nodeByName('api.makeDeviceDriver#0.mPorts.makeMidiOutput#0');
const page = nodeByName('api.makeDeviceDriver#0.mMapping.makePage#0');
const ZONE = 'api.makeDeviceDriver#0.mMapping.makePage#0.mHostAccess.mMixConsole.makeMixerBankZone#0';
const zone = nodeByName(ZONE);

// ---------------------------------------------------------- simulation
const AD = { fake: 'device' };
const AM = { fake: 'mapping' };
const NAMES = ['Drums', 'Bass', 'Keys', 'Vox Lead', 'Guitar', 'FX Reverb', 'Group Drums', 'Group Vox'];
const VOLUMES = [0.5, 0.75, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5];

callProp(page, 'mOnActivate', AD, AM);
for (let i = 0; i < 8; ++i) {
  const chan = nodeByName(ZONE + '.makeMixerBankChannel#' + i);
  callProp(chan, 'mOnTitleChange', AD, AM, NAMES[i]);
  const fader = nodeByName('api.makeDeviceDriver#0.mSurface.makeFader#' + i);
  callProp(fader.mSurfaceValue, 'mOnProcessValueChange', AD, VOLUMES[i], 0);
}

// inbound: PING -> (fixed script) responds with a full snapshot
callProp(midiInput, 'mOnSysex', AD, [0xF0, 0x7D, 0x11, 0xF7]);

// inbound: SET_VOLUME slot 2 = 8192 (0.5)
callProp(midiInput, 'mOnSysex', AD, [0xF0, 0x7D, 0x03, 0x02, 0x40, 0x00, 0xF7]);

// inbound: SELECT_SLOT 0 then DESELECT_SLOT 0
callProp(midiInput, 'mOnSysex', AD, [0xF0, 0x7D, 0x01, 0x00, 0xF7]);
callProp(midiInput, 'mOnSysex', AD, [0xF0, 0x7D, 0x13, 0x00, 0xF7]);

// ------------------------------------------------------------- collect
const frames = childCalls(midiOutput, 'sendMidi').map((args) => Array.from(args[1]));
const setProcessValue = [];
for (let i = 0; i < 8; ++i) {
  const fader = nodeByName('api.makeDeviceDriver#0.mSurface.makeFader#' + i);
  for (const args of childCalls(fader.mSurfaceValue, 'setProcessValue')) {
    setProcessValue.push({ node: 'makeFader#' + i, value: args[1] });
  }
}

process.stdout.write(JSON.stringify({
  frames,
  setProcessValue,
  sanity: {
    pageActivated: typeof meta.get(page).props.get('mOnActivate') === 'function',
    bankChannels: (() => {
      let n = 0;
      while (nodesByName.has(ZONE + '.makeMixerBankChannel#' + n)) ++n;
      return n;
    })(),
    zoneName: zone.name,
  },
}));
