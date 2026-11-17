// P2: leaving the page aborts its request and prevents another poll.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createErrorPoller} = require('../src/static/hive-errors.js');

test('leaving Hive Errors aborts polling and ignores a late response', async () => {
    let signal, resolve, renders = 0, timers = 0;
    const poller = createErrorPoller({
        request: s => { signal = s; return new Promise(r => resolve = r); },
        render: () => renders++, error: () => {},
        schedule: () => { timers++; return 1; }, cancel: () => {},
    });
    poller.start();
    poller.stop();
    assert.equal(signal.aborted, true);
    resolve({components: []});
    await new Promise(r => setImmediate(r));
    assert.equal(renders, 0);
    assert.equal(timers, 0);
});

test('Hive Errors polls again only after the prior request finishes', async () => {
    let requests = 0, next;
    const poller = createErrorPoller({
        request: async () => { requests++; return {components: []}; },
        render: () => {}, error: () => {},
        schedule: f => { next = f; return 1; }, cancel: () => {},
    });
    poller.start();
    poller.start();
    await new Promise(r => setImmediate(r));
    assert.equal(requests, 1);
    next();
    await new Promise(r => setImmediate(r));
    assert.equal(requests, 2);
    poller.stop();
});
