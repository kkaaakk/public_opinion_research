/* ── State ──────────────────────────────────────────────────────── */
let abortController = null;
let reportMarkdown = '';
let startTime = 0;
let timerInterval = null;

/* ── DOM refs ───────────────────────────────────────────────────── */
const topicInput = document.getElementById('topic');
const startBtn = document.getElementById('start-btn');
const stopBtn = document.getElementById('stop-btn');
const statusBar = document.getElementById('status-bar');
const statusText = document.getElementById('status-text');
const statusTimer = document.getElementById('status-timer');
const logPanel = document.getElementById('log-panel');
const logEntries = document.getElementById('log-entries');
const resultsPanel = document.getElementById('results-panel');
const reportEl = document.getElementById('report-content');
const usageBar = document.getElementById('usage-bar');
const emptyState = document.getElementById('empty-state');
const modelSelect = document.getElementById('model');
const searchSelect = document.getElementById('search');
const modeSelect = document.getElementById('mode');

/* ── Node → badge mapping ───────────────────────────────────────── */
const NODE_BADGE = {
    write_research_brief: { label: 'Plan', cls: 'plan' },
    research_phase:        { label: 'Analyze', cls: 'think' },
    compress_research:     { label: 'Write', cls: 'write' },
    final_report_generation: { label: 'Write', cls: 'write' },
};

/* ── Keyboard shortcut ──────────────────────────────────────────── */
topicInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        startResearch();
    }
});

function setTopic(text) {
    topicInput.value = text;
    topicInput.focus();
}


/* ── Start / Stop ───────────────────────────────────────────────── */
async function startResearch() {
    const topic = topicInput.value.trim();
    if (!topic) { topicInput.focus(); return; }

    abortController = new AbortController();
    reportMarkdown = '';
    startTime = Date.now();

    // Reset UI
    startBtn.disabled = true;
    stopBtn.classList.remove('hidden');
    statusBar.classList.remove('hidden');
    statusText.textContent = 'Initializing…';
    statusTimer.textContent = '';
    logPanel.classList.remove('hidden');
    logEntries.innerHTML = '';
    resultsPanel.classList.add('hidden');
    usageBar.classList.add('hidden');
    emptyState.classList.add('hidden');

    // Start timer
    timerInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTime) / 1000);
        statusTimer.textContent = elapsed + 's';
    }, 1000);

    const body = JSON.stringify({
        topic,
        model: modelSelect.value,
        search_api: searchSelect.value,
        mode: modeSelect.value,
        org_context: document.getElementById('org-context').value,
        rag_enabled: document.getElementById('rag-enabled')?.checked || false,
    });

    try {
        const response = await fetch('/api/research', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body,
            signal: abortController.signal,
        });
        if (!response.ok) throw new Error('Server error: ' + response.status);

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (line.startsWith('data: ')) {
                    try { handleEvent(JSON.parse(line.slice(6))); } catch (e) {}
                }
            }
        }
    } catch (err) {
        if (err.name !== 'AbortError') {
            addLog('error', '⚠ ' + err.message);
        }
    } finally {
        resetUI();
    }
}

function stopResearch() {
    if (abortController) abortController.abort();
}

/* ── Event handler ──────────────────────────────────────────────── */
function handleEvent(data) {
    switch (data.type) {
        case 'status':
            statusText.textContent = data.message;
            break;

        case 'stream':
            addLog(data.node, data.content);
            break;

        case 'report':
            reportMarkdown = data.content;
            resultsPanel.classList.remove('hidden');
            logPanel.classList.add('hidden');
            renderReport(data.content);
            resultsPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            break;

        case 'usage':
            usageBar.classList.remove('hidden');
            document.getElementById('usage-total').textContent = formatNum(data.total_tokens);
            document.getElementById('usage-input').textContent = formatNum(data.input_tokens);
            document.getElementById('usage-output').textContent = formatNum(data.output_tokens);
            document.getElementById('usage-calls').textContent = data.model_calls;
            break;

        case 'error':
            addLog('error', '⚠ ' + data.message);
            statusText.textContent = 'Failed';
            break;

        case 'done':
            statusText.textContent = 'Complete';
            break;
    }
}

function renderReport(markdown) {
    // Reports contain untrusted web, social, MCP, and RAG text. If the
    // sanitizer CDN is unavailable, fail closed and show plain text.
    if (typeof marked === 'undefined') {
        reportEl.textContent = markdown || '';
        return;
    }
    const html = marked.parse(markdown || '');
    if (typeof DOMPurify !== 'undefined' && typeof DOMPurify.sanitize === 'function') {
        reportEl.innerHTML = DOMPurify.sanitize(html);
    } else {
        reportEl.textContent = markdown || '';
    }
}

/* ── Log entries ────────────────────────────────────────────────── */
function addLog(node, text) {
    const entry = document.createElement('div');
    entry.className = 'log-entry';

    const badge = NODE_BADGE[node];
    if (badge) {
        const span = document.createElement('span');
        span.className = 'log-node ' + badge.cls;
        span.textContent = badge.label;
        entry.appendChild(span);
    }

    const content = document.createElement('span');
    content.className = 'log-text';
    content.textContent = truncate(text, 500);
    entry.appendChild(content);

    logEntries.appendChild(entry);
    logPanel.scrollTop = logPanel.scrollHeight;
}

/* ── UI helpers ─────────────────────────────────────────────────── */
function resetUI() {
    startBtn.disabled = false;
    stopBtn.classList.add('hidden');
    clearInterval(timerInterval);
}

function formatNum(n) {
    if (!n) return '0';
    return n.toLocaleString();
}

function truncate(text, max) {
    if (!text) return '';
    return text.length > max ? text.slice(0, max) + '…' : text;
}

/* ── Actions ────────────────────────────────────────────────────── */
function downloadReport() {
    if (!reportMarkdown) return;
    const blob = new Blob([reportMarkdown], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'research-report.md';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('Downloaded');
}

async function copyReport() {
    if (!reportMarkdown) return;
    try {
        await navigator.clipboard.writeText(reportMarkdown);
    } catch {
        const ta = document.createElement('textarea');
        ta.value = reportMarkdown;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
    }
    toast('Copied');
}

function toast(msg) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 2000);
}
