// Execute the actual browser controller against a small DOM and HTTP transport.
// No Agent, queue or browser storage algorithm is reimplemented here.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const source = fs.readFileSync('src/open_deep_research/web/static/app.js', 'utf8');
const storageKey = 'public-opinion-pending-run-v1';
const runId = 'a'.repeat(32);
const body = { topic: 'brand recall', model: 'openai:gpt-4.1', search_api: 'tavily',
    mode: 'normal', org_context: '', rag_enabled: true,
    workflow_backend: 'python', business_agent_runtime: 'openai_agents', require_approval: true };

function response(value, status = 200) {
    return { ok: status >= 200 && status < 300, status, json: async () => value };
}
function stream(events) {
    let first = true;
    return { ...response(null), body: { getReader: () => ({ read: async () => {
        if (!first) return { done: true };
        first = false;
        return { done: false, value: new TextEncoder().encode(events.map(event =>
            'id: ' + event.seq + '\ndata: ' + JSON.stringify(event) + '\n\n').join('')) };
    } }) } };
}
function state(status, extra = {}) {
    return { status, usage: {}, role_reports: [], report: '', archive_status: 'pending', ...extra };
}
function storage(value) {
    const values = new Map(value ? [[storageKey, JSON.stringify(value)]] : []);
    return { getItem: key => values.get(key) || null,
        setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}
function page(sessionStorage, transport) {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) {
            const classes = new Set(['hidden']);
            elements.set(id, { value: '', checked: false, disabled: false, textContent: '', innerHTML: '',
                classList: { add: name => classes.add(name), remove: name => classes.delete(name),
                    toggle: (name, flag) => flag ? classes.add(name) : classes.delete(name), contains: name => classes.has(name) },
                addEventListener() {}, setAttribute() {}, removeAttribute() {}, querySelectorAll: () => [],
                appendChild() {}, replaceChildren() {}, focus() {}, scrollIntoView() {} });
        }
        return elements.get(id);
    };
    for (const [id, value] of Object.entries({ topic: body.topic, model: body.model, search: 'tavily', mode: 'normal', 'api-token': 'private-token' })) {
        element(id).value = value;
    }
    element('durable-enabled').checked = true;
    element('rag-enabled').checked = true;
    const calls = [];
    const context = vm.createContext({ console, sessionStorage, crypto: webcrypto, TextDecoder,
        AbortController, URL, URLSearchParams, Date, Blob,
        setInterval: () => 1, clearInterval() {}, clearTimeout() {}, setTimeout: fn => { queueMicrotask(fn); return 1; },
        document: { getElementById: element, createElement: () => element('created-' + elements.size), body: { appendChild() {} } },
        window: { location: { href: 'http://localhost/' }, history: { replaceState() {} } },
        fetch: async (path, options = {}) => {
            if (path.startsWith('/api/history')) return response({ items: [], total: 0 });
            calls.push({ path, options });
            return transport(path, options);
        },
    });
    vm.runInContext(source, context);
    return { call: code => vm.runInContext(code, context), calls, element };
}

test('ambiguous create response survives reload with the same key and request', async () => {
    const saved = storage();
    let first = true;
    const submissions = [];
    const transport = (path, options) => {
        if (path === '/api/runs') {
            submissions.push(options);
            if (first) { first = false; throw new TypeError('connection dropped after commit'); }
            return response({ run_id: runId });
        }
        return response(state('completed', { report: 'retained report' }));
    };
    const original = page(saved, transport);
    await original.call('startResearch()');
    assert.equal(JSON.parse(saved.getItem(storageKey)).runId, null);
    const refreshed = page(saved, transport);
    assert.equal(refreshed.calls.length, 0, 'page load must not create a Run');
    await refreshed.call('startResearch(true)');
    assert.equal(submissions.length, 2);
    assert.equal(submissions[0].headers['Idempotency-Key'], submissions[1].headers['Idempotency-Key']);
    assert.equal(submissions[0].body, submissions[1].body);
    assert.equal(JSON.parse(saved.getItem(storageKey)).finished, true);
    assert.ok(!saved.getItem(storageKey).includes('private-token'));
});

test('a fresh page always creates a durable Run without execution selectors', async () => {
    const view = page(storage(), path => path === '/api/runs'
        ? response({ run_id: runId }) : response(state('completed', { report: 'default report' })));
    view.element('durable-enabled').checked = false;
    await view.call('startResearch()');
    const submitted = view.calls.filter(call => call.options.method === 'POST');
    assert.equal(submitted.length, 1);
    assert.equal(submitted[0].path, '/api/runs');
    const request = JSON.parse(submitted[0].options.body);
    assert.equal(request.workflow_backend, undefined);
    assert.equal(request.business_agent_runtime, undefined);
    assert.ok(submitted[0].options.headers['Idempotency-Key']);
    assert.ok(!view.calls.some(call => call.path === '/api/research'));
});

test('known Run reload only subscribes and deduplicates durable event sequences', async () => {
    const saved = storage({ key: 'key', request: body, runId, cursor: 7 });
    let reads = 0;
    const view = page(saved, (path) => {
        if (path.endsWith('/events')) return stream([
            { seq: 8, type: 'status', message: 'stage' }, { seq: 8, type: 'status', message: 'duplicate' },
            { seq: 9, type: 'report', content: 'report' }, { seq: 10, type: 'done' },
        ]);
        return response(state(++reads === 1 ? 'running' : 'completed', { report: reads > 1 ? 'report' : '' }));
    });
    await view.call('startResearch(true)');
    assert.ok(view.calls.every(call => call.options.method !== 'POST'));
    assert.equal(view.calls.find(call => call.path.endsWith('/events')).options.headers['Last-Event-ID'], '7');
    assert.equal(JSON.parse(saved.getItem(storageKey)).cursor, 10);
    assert.equal(view.element('report-content').textContent, 'report');
});

test('approval is tied to the saved plan and submitted once without another create', async () => {
    const saved = storage({ key: 'key', request: body, runId, cursor: 0 });
    let approved = false;
    const view = page(saved, (path, options) => {
        if (path.endsWith('/approval')) { approved = true; return response(state('queued')); }
        return response(approved ? state('completed', { report: 'approved report' })
            : state('awaiting_approval', { plan: { version: 'version', brief: 'immutable brief', sections: [] } }));
    });
    await view.call('startResearch(true)');
    assert.ok(view.element('approval-plan').textContent.includes('immutable brief'));
    await view.call("Promise.all([decidePlan('approve'), decidePlan('approve')])");
    const approvals = view.calls.filter(call => call.path.endsWith('/approval'));
    assert.equal(approvals.length, 1);
    assert.deepEqual(JSON.parse(approvals[0].options.body), { plan_version: 'version', decision: 'approve' });
    assert.ok(view.calls.every(call => call.path !== '/api/runs'));
});

test('Stop sends durable cancellation without aborting the subscription', async () => {
    const saved = storage({ key: 'key', request: body, runId, cursor: 0 });
    const view = page(saved, () => response(state('cancelling')));
    view.call('abortController = { abort() { throw new Error("subscription aborted"); } }');
    await view.call('stopResearch()');
    assert.equal(view.calls[0].path, '/api/runs/' + runId + '/cancel');
    assert.equal(view.calls[0].options.method, 'POST');
});

test('nonterminal EOF reconnects and never starts another execution', async () => {
    const saved = storage({ key: 'key', request: body, runId, cursor: 0 });
    let reads = 0;
    const view = page(saved, path => path.endsWith('/events') ? stream([])
        : response(state(++reads < 3 ? 'running' : 'completed', { report: reads === 3 ? 'final' : '' })));
    await view.call('startResearch(true)');
    assert.equal(view.calls.filter(call => call.path.endsWith('/events')).length, 2);
    assert.ok(view.calls.every(call => call.options.method !== 'POST'));
    assert.equal(view.element('status-text').textContent, '已完成');
});

test('terminal native status waits for settlement and displays final usage', async () => {
    const saved = storage({ key: 'key', request: body, runId, cursor: 0 });
    let reads = 0;
    const view = page(saved, path => {
        if (path.endsWith('/events')) return stream([]);
        reads += 1;
        return response(reads === 1
            ? state('interrupted', { settlement: 'pending', usage: { total_tokens: 3 } })
            : state('failed', { settlement: 'settled', usage: { total_tokens: 12 } }));
    });
    await view.call('startResearch(true)');
    assert.equal(reads, 2, 'a terminal status cannot end observation before settlement');
    assert.equal(view.calls.filter(call => call.path.endsWith('/events')).length, 1);
    assert.ok(view.calls.every(call => call.options.method !== 'POST'));
    assert.equal(view.element('usage-total').textContent, '12');
    assert.equal(view.element('status-text').textContent, '运行失败');
    assert.equal(JSON.parse(saved.getItem(storageKey)).finished, true);
});
