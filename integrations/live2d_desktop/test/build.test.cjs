const {test} = require('node:test');
const assert = require('node:assert/strict');
const {build} = require('../scripts/build-native.cjs');

test('verify-only installs, tests and runs Electron without invoking packaging', () => {
  const calls = [];
  assert.equal(build(true, args => {calls.push(args); return {status: 0};}), 0);
  assert.deepEqual(calls, [['ci', '--no-audit', '--no-fund'], ['test'], ['run', 'smoke']]);
});
test('the existing default still builds installers after verification', () => {
  const calls = [];
  assert.equal(build(false, args => {calls.push(args); return {status: 0};}), 0);
  assert.deepEqual(calls.at(-1), ['run', 'pack']);
});
test('a failed test stops smoke and packaging and retains its exit code', () => {
  const calls = [];
  assert.equal(build(false, args => {calls.push(args); return {status: args[0] === 'test' ? 23 : 0};}), 23);
  assert.equal(calls.length, 2);
});
test('a terminated process cannot produce a successful verification', () => {
  assert.equal(build(true, () => ({status: null, signal: 'SIGTERM'})), 1);
});
test('a process startup error is not hidden', () => {
  assert.throws(() => build(true, () => ({error: new Error('controlled spawn failure')})), /controlled spawn failure/);
});
