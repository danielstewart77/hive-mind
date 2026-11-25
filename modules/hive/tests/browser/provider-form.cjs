// P2: execute the real rendered page and request helper at the fetch boundary.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { JSDOM, VirtualConsole } = require('jsdom');
const calls = [], errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => errors.push(error));
const reply = (body, status = 200) => ({ok: status === 200, status, json: async () => body});
const dom = new JSDOM(fs.readFileSync(process.argv[2], 'utf8'), {
  url: 'https://testserver/minds/new', runScripts: 'dangerously', virtualConsole,
  beforeParse(window) {
    window.fetch = async (url, init = {}) => {
      const body = init.body ? JSON.parse(init.body) : {};
      calls.push({url, body});
      if (url === '/api/settings/providers') return reply({providers: [
        {id: 'proxy', name: 'Hive Proxy', kind: 'proxy', api_format: 'openai'},
        {id: 'direct', name: 'Direct OpenAI', kind: 'openai', api_format: 'openai'}
      ]});
      if (url === '/api/roles') return reply({roles: []});
      if (url === '/api/admin/users') return reply({users: [{username: 'browser-fixture'}]});
      if (url.endsWith('/models')) {
        if (url.includes('/proxy/') && body.token === 'mind-only-token') return reply({models: [{id: 'served-model', label: 'Served model'}]});
        if (url.includes('/direct/') && !body.token) return reply({models: [{id: 'direct-model'}]});
        return reply({detail: 'Enter this Mind’s token'}, 400);
      }
      if (url === '/api/minds') return reply({detail: 'Captured creation request'}, 400);
      return reply({available: true});
    };
    window.eval(fs.readFileSync(process.argv[3], 'utf8'));
  }
});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  const window = dom.window, document = window.document;
  await delay(100);
  const provider = document.querySelector('#mind-provider');
  const model = document.querySelector('#mind-default-model');
  const token = document.querySelector('#mind-proxy-key');
  assert.equal(model.tagName, 'SELECT');
  token.value = 'mind-only-token';
  token.dispatchEvent(new window.Event('input', {bubbles: true}));
  await delay(650);
  assert.equal(model.options[1].value, 'served-model');
  model.value = 'served-model';
  document.querySelector('#mind-form').dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true}));
  await delay(30);
  const outgoing = calls.find(call => call.url === '/api/minds');
  assert.equal(outgoing.body.provider_id, 'proxy');
  assert.equal(outgoing.body.provider_token, 'mind-only-token');
  assert.equal(outgoing.body.default_model, 'served-model');
  provider.value = 'direct';
  provider.dispatchEvent(new window.Event('change', {bubbles: true}));
  await delay(50);
  assert.equal(token.value, '');
  assert.equal(model.options[1].value, 'direct-model');
  assert.equal(calls.filter(call => call.url.includes('/direct/models')).every(call => !call.body.token), true);
  assert.deepEqual(errors, []);
  console.log('Provider dropdown, model enumeration, creation payload, and token isolation passed.');
  dom.window.close();
})().catch(error => { dom.window.close(); console.error(error); process.exitCode = 1; });
