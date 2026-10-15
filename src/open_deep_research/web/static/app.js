/* 页面状态：管理研究流、请求版本、历史选择和导入状态。 */
let abortController = null;
let reportMarkdown = '';
let startTime = 0;
let timerInterval = null;
let isResearchRunning = false;
let researchFailed = false;
let archiveSaved = false;
let activeResearchRequest = null;
let selectedHistoryRecord = null;
let historyOffset = 0;
let historyRequestVersion = 0;
let reportRequestVersion = 0;
let historySearchTimer = null;
let isImportingHistory = false;
const HISTORY_PAGE_SIZE = 10;

/* 缓存 DOM 引用，供事件处理和状态渲染复用。 */
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
const apiToken = document.getElementById('api-token');
const reportMeta = document.getElementById('report-meta');
const resultsTitle = document.getElementById('results-title');
const reportNotice = document.getElementById('report-notice');
const reuseBtn = document.getElementById('reuse-btn');
const historySearch = document.getElementById('history-search');
const historyList = document.getElementById('history-list');
const historyMessage = document.getElementById('history-message');
const historyFeedback = document.getElementById('history-feedback');
const historyCount = document.getElementById('history-count');
const historyRefresh = document.getElementById('history-refresh');
const historyImport = document.getElementById('history-import');
const historyPagination = document.getElementById('history-pagination');
const historyPrev = document.getElementById('history-prev');
const historyNext = document.getElementById('history-next');
const HISTORY_STATUS_LABELS = { completed: '已完成', failed: '运行失败', cancelled: '已停止', incomplete: '未完成' };
const HISTORY_ROLE_LABELS = { public_signal: '舆情采集', internal_knowledge: '内部知识', risk_assessment: '风险评估', response_strategy: '应对策略' };

/* 将后端节点名称映射为日志徽标及其样式。 */
const NODE_BADGE = {
    write_research_brief: { label: 'Plan', cls: 'plan' },
    research_phase:        { label: 'Analyze', cls: 'think' },
    compress_research:     { label: 'Write', cls: 'write' },
    final_report_generation: { label: 'Write', cls: 'write' },
};

/* Ctrl/Cmd + Enter 提交当前研究主题，并阻止默认换行行为。 */
topicInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        startResearch();
    }
});

// 将示例主题填入输入框，并把焦点交还给用户。
function setTopic(text) {
    topicInput.value = text;
    topicInput.focus();
}


/* 研究启停：提交参数、消费 SSE 流并在退出时恢复页面操作。 */
// 校验主题并启动一次研究；使用 AbortController 支持用户主动取消。
async function startResearch() {
    if (isResearchRunning) return;
    const topic = topicInput.value.trim();
    if (!topic) { topicInput.focus(); return; }

    abortController = new AbortController();
    reportMarkdown = '';
    startTime = Date.now();
    isResearchRunning = true;
    researchFailed = false;
    archiveSaved = false;
    selectedHistoryRecord = null;
    reportRequestVersion += 1;
    activeResearchRequest = {
        topic,
        model: modelSelect.value,
        search_api: searchSelect.value,
        mode: modeSelect.value,
        org_context: document.getElementById('org-context').value,
        rag_enabled: document.getElementById('rag-enabled')?.checked || false,
    };
    setHistorySelection(null);
    setHistoryActionsDisabled();
    reportMeta.textContent = truncate(topic, 180);
    reportNotice.classList.add('hidden');
    reuseBtn.classList.add('hidden');

    // 清理上一轮展示并锁定提交入口，避免同页重复启动研究。
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

    // 每秒刷新本轮已用时间；resetUI 负责清理定时器。
    timerInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTime) / 1000);
        statusTimer.textContent = elapsed + 's';
    }, 1000);

    const body = JSON.stringify(activeResearchRequest);

    try {
        const response = await fetch('/api/research', {
            method: 'POST',
            headers: apiHeaders({ 'Content-Type': 'application/json' }),
            body,
            signal: abortController.signal,
        });
        if (!response.ok) throw new Error(await apiError(response));

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        // 网络数据块不一定以换行结束，保留末尾片段供下一块继续解析。
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
            statusText.textContent = 'Failed';
        } else {
            statusText.textContent = '已停止';
        }
    } finally {
        resetUI();
        if (archiveSaved) await loadHistory(0);
    }
}

// 中止当前 fetch 与响应流；统一退出处理由 startResearch 的 finally 执行。
function stopResearch() {
    if (abortController) abortController.abort();
}

/* 按 SSE 事件类型更新进度、日志、报告、用量和历史保存状态。 */
function handleEvent(data) {
    switch (data.type) {
        case 'status':
            statusText.textContent = data.message;
            break;

        case 'stream':
            addLog(data.node, data.content);
            break;

        case 'report':
            resultsTitle.textContent = '研究报告';
            reportMarkdown = data.content;
            resultsPanel.classList.remove('hidden');
            logPanel.classList.add('hidden');
            renderReport(data.content);
            resultsPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
            break;

        case 'usage':
            renderUsage(data);
            break;

        case 'history_saved':
            archiveSaved = true;
            selectedHistoryRecord = { id: data.id, request: activeResearchRequest, status: data.status || 'completed' };
            setHistorySelection(data.id);
            reportMeta.textContent = truncate(activeResearchRequest.topic, 180) + ' · '
                + (HISTORY_STATUS_LABELS[data.status || 'completed']) + ' · 已保存 · ' + formatDate(data.completed_at);
            reuseBtn.classList.remove('hidden');
            break;

        case 'warning':
            reportNotice.textContent = data.message;
            reportNotice.classList.remove('hidden');
            addLog('warning', data.message);
            break;

        case 'error':
            researchFailed = true;
            addLog('error', '⚠ ' + data.message);
            statusText.textContent = 'Failed';
            break;

        case 'done':
            statusText.textContent = researchFailed ? 'Failed' : 'Complete';
            break;
    }
}

// 将 Markdown 转为经过 DOMPurify 净化的 HTML；依赖不可用时展示纯文本。
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

/* 创建日志行：文本通过 textContent 写入，防止外部内容作为 HTML 执行。 */
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

/* 页面辅助：恢复操作状态，并格式化用量与文本。 */
// 清理本轮运行标记、取消控制器和计时器，重新开放允许的操作。
function resetUI() {
    isResearchRunning = false;
    abortController = null;
    startBtn.disabled = false;
    stopBtn.classList.add('hidden');
    clearInterval(timerInterval);
    setHistoryActionsDisabled();
}

// 展示本次运行的输入、输出、总令牌及模型调用次数。
function renderUsage(data) {
    usageBar.classList.remove('hidden');
    document.getElementById('usage-total').textContent = formatNum(data.total_tokens);
    document.getElementById('usage-input').textContent = formatNum(data.input_tokens);
    document.getElementById('usage-output').textContent = formatNum(data.output_tokens);
    document.getElementById('usage-calls').textContent = data.model_calls ?? 0;
}

// 使用浏览器区域格式显示数字；空值或零显示为 0。
function formatNum(n) {
    if (!n) return '0';
    return n.toLocaleString();
}

// 按最大字符长度截断预览，超长时追加省略号。
function truncate(text, max) {
    if (!text) return '';
    return text.length > max ? text.slice(0, max) + '…' : text;
}

/* 历史归档：分页查询、打开报告、沿用参数以及导入既有追踪。 */
// 合并请求头，并仅在页面填写令牌时添加 Bearer 授权信息。
function apiHeaders(extra = {}) {
    const headers = { ...extra };
    const token = apiToken.value.trim();
    if (token) headers.Authorization = 'Bearer ' + token;
    return headers;
}

// 解析 API 错误文本；未授权时展开令牌输入，无 JSON 时使用稳定兜底消息。
async function apiError(response) {
    if (response.status === 401) {
        document.getElementById('api-settings').open = true;
        return '请输入访问令牌，然后重试。';
    }
    try {
        const body = await response.json();
        if (typeof body.detail === 'string') return body.detail;
    } catch { /* Use a stable fallback when the proxy returns a non-JSON error. */ }
    return '请求失败（' + response.status + '），请稍后重试。';
}

// 将后端时间转换为浏览器本地时区的中文日期；无效值显示未知。
function formatDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '时间未知';
    return date.toLocaleString('zh-CN', {
        year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    });
}

// 同步 URL 中的历史 ID 与列表选中态，支持刷新后重新打开记录。
function setHistorySelection(id) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set('history', id);
    else url.searchParams.delete('history');
    window.history.replaceState(null, '', url);
    for (const row of historyList.querySelectorAll('.history-item')) {
        if (row.dataset.id === id) row.setAttribute('aria-current', 'true');
        else row.removeAttribute('aria-current');
    }
}

// 根据研究和导入状态禁用冲突操作，避免覆盖正在展示的研究结果。
function setHistoryActionsDisabled() {
    historyImport.disabled = isResearchRunning || isImportingHistory;
    reuseBtn.disabled = isResearchRunning;
    for (const row of historyList.querySelectorAll('.history-item')) {
        row.disabled = isResearchRunning;
    }
}

// 按搜索词和偏移读取历史；请求版本号防止较慢旧响应覆盖最新列表。
async function loadHistory(offset = historyOffset) {
    const version = ++historyRequestVersion;
    historyList.setAttribute('aria-busy', 'true');
    historyMessage.textContent = '正在加载历史记录…';
    historyMessage.classList.remove('hidden');
    historyRefresh.disabled = true;
    historyPrev.disabled = true;
    historyNext.disabled = true;
    const params = new URLSearchParams({
        q: historySearch.value.trim(), limit: String(HISTORY_PAGE_SIZE), offset: String(offset),
    });
    try {
        const response = await fetch('/api/history?' + params, { headers: apiHeaders(), cache: 'no-store' });
        if (!response.ok) throw new Error(await apiError(response));
        const data = await response.json();
        // 用户已发起新查询时，丢弃当前过期响应。
        if (version !== historyRequestVersion) return;
        if (offset && offset >= data.total) return loadHistory(0);
        historyOffset = offset;
        historyCount.textContent = String(data.total);
        historyList.replaceChildren();
        for (const item of data.items) {
            const row = document.createElement('button');
            row.type = 'button';
            row.className = 'history-item';
            row.dataset.id = item.id;
            row.setAttribute('aria-label', '查看研究记录：' + item.topic);
            if (selectedHistoryRecord?.id === item.id) row.setAttribute('aria-current', 'true');
            const title = document.createElement('span');
            title.className = 'history-topic';
            title.textContent = item.topic;
            const date = document.createElement('span');
            date.className = 'history-date';
            date.textContent = formatDate(item.completed_at);
            const status = document.createElement('span');
            const statusName = item.status || 'completed';
            status.className = 'history-status status-' + statusName;
            status.textContent = HISTORY_STATUS_LABELS[statusName] || '未完成';
            if (statusName !== 'completed' && item.stage_count) status.textContent += ' · 保留 ' + item.stage_count + ' 份阶段报告';
            const preview = document.createElement('span');
            preview.className = 'history-preview';
            preview.textContent = (item.preview || '').replace(/^#{1,6}\s+/gm, '')
                .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/(\*\*|__|`)/g, '')
                .replace(/\s+/g, ' ').trim();
            const source = document.createElement('span');
            source.className = 'history-source';
            source.textContent = item.source === 'langsmith' ? 'LangSmith 导入' : '网页研究';
            row.append(title, date, status, preview, source);
            // 每条历史按钮捕获自己的记录 ID，点击后打开对应归档。
            row.addEventListener('click', () => openHistory(item.id));
            historyList.appendChild(row);
        }
        historyMessage.textContent = historySearch.value.trim()
            ? '没有匹配的研究，试试其他关键词。' : '还没有历史记录。研究结束或中断后会保存，也可以导入 LangSmith 中的旧运行。';
        historyMessage.classList.toggle('hidden', data.items.length > 0);
        historyPagination.classList.toggle('hidden', data.total <= HISTORY_PAGE_SIZE);
        document.getElementById('history-page').textContent =
            Math.floor(offset / HISTORY_PAGE_SIZE) + 1 + ' / ' + Math.max(1, Math.ceil(data.total / HISTORY_PAGE_SIZE));
        historyPrev.disabled = offset === 0;
        historyNext.disabled = offset + HISTORY_PAGE_SIZE >= data.total;
    } catch (err) {
        if (version !== historyRequestVersion) return;
        historyList.replaceChildren();
        historyCount.textContent = '—';
        historyPagination.classList.add('hidden');
        historyMessage.textContent = err.message;
        historyMessage.classList.remove('hidden');
    } finally {
        if (version === historyRequestVersion) {
            historyList.setAttribute('aria-busy', 'false');
            historyRefresh.disabled = false;
            setHistoryActionsDisabled();
        }
    }
}

// 加载归档正文和阶段报告；若用户已切换记录或开始研究，则忽略旧响应。
async function openHistory(id) {
    if (isResearchRunning) return;
    const version = ++reportRequestVersion;
    historyFeedback.textContent = '正在打开报告…';
    historyFeedback.classList.remove('hidden');
    try {
        const response = await fetch('/api/history/' + encodeURIComponent(id), {
            headers: apiHeaders(), cache: 'no-store',
        });
        if (!response.ok) throw new Error(await apiError(response));
        const record = await response.json();
        if (version !== reportRequestVersion || isResearchRunning) return;
        selectedHistoryRecord = record;
        reportMarkdown = historyMarkdown(record);
        renderReport(reportMarkdown);
        resultsTitle.textContent = record.report ? '研究报告' : '研究阶段记录';
        if (Object.keys(record.usage || {}).length) renderUsage(record.usage);
        else usageBar.classList.add('hidden');
        const graph = record.graph || {};
        const evidenceInfo = graph.sources || graph.evidence || graph.findings
            ? ' · ' + graph.sources + ' 个来源 / ' + graph.evidence + ' 条证据' : '';
        reportMeta.textContent = truncate(record.topic, 180) + ' · ' + formatDate(record.completed_at) + evidenceInfo;
        const incomplete = (record.status || 'completed') !== 'completed' || !record.report;
        reportNotice.classList.toggle('hidden', !incomplete);
        if (incomplete) reportNotice.textContent = record.report
            ? '这次运行未正常结束，以下为已经保存的报告。'
            : '这次研究未生成最终报告。以下保留原始运行信息和已完成的阶段输出。';
        resultsPanel.classList.remove('hidden');
        reuseBtn.classList.remove('hidden');
        statusBar.classList.add('hidden');
        logPanel.classList.add('hidden');
        emptyState.classList.add('hidden');
        setHistorySelection(record.id);
        historyFeedback.classList.add('hidden');
        resultsPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (err) {
        if (version !== reportRequestVersion) return;
        historyFeedback.textContent = err.message;
        historyFeedback.classList.remove('hidden');
    }
}

// 优先返回最终报告；未完成运行则按原角色阶段组装可查看的 Markdown 记录。
function historyMarkdown(record) {
    if (record.report) return record.report;
    const stages = record.role_reports || [];
    const parts = ['# 未完成研究记录', '> 最终研究报告未生成。以下内容来自原始运行记录及已完成的阶段报告。',
        '状态：' + (HISTORY_STATUS_LABELS[record.status] || '未完成')];
    if (record.failed_stage) parts.push('失败阶段：' + (HISTORY_ROLE_LABELS[record.failed_stage] || record.failed_stage));
    if (record.error_message) parts.push(record.error_message);
    const counts = {};
    for (const stage of stages) {
        counts[stage.role] = (counts[stage.role] || 0) + 1;
        const title = (HISTORY_ROLE_LABELS[stage.role] || stage.role) + ' ' + counts[stage.role];
        parts.push('## ' + title + (stage.completed_at ? ' · ' + formatDate(stage.completed_at) : ''), stage.content);
    }
    if (!stages.length) parts.push('没有可恢复的阶段报告。原始研究主题、参数和运行时间已保留，可通过“沿用参数”查看。');
    return parts.join('\n\n');
}

// 将选中归档的研究参数填回表单；历史模型不再可选时提示用户重新选择。
function reuseHistoryParameters() {
    if (!selectedHistoryRecord || isResearchRunning) return;
    const request = selectedHistoryRecord.request;
    topicInput.value = request.topic || '';
    let missingModel = false;
    for (const [select, value] of [[modelSelect, request.model], [searchSelect, request.search_api], [modeSelect, request.mode]]) {
        // 逐项检查历史值是否仍在当前下拉选项中，避免写入已移除的模型或模式。
        if (value && Array.from(select.options).some(option => option.value === value)) select.value = value;
        else if (select === modelSelect && value) missingModel = true;
    }
    document.getElementById('org-context').value = request.org_context || '';
    document.getElementById('rag-enabled').checked = request.rag_enabled === true;
    topicInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
    topicInput.focus({ preventScroll: true });
    toast(missingModel ? '已填入研究参数，请选择当前可用的模型。' : '已填入研究参数，可修改后开始新的分析。');
}

// 导入 LangSmith 既有运行，并在结束时刷新列表及恢复按钮状态。
async function importHistory() {
    if (isImportingHistory || isResearchRunning) return;
    isImportingHistory = true;
    setHistoryActionsDisabled();
    historyFeedback.textContent = '正在从 LangSmith 导入研究运行及保留的阶段报告…';
    historyFeedback.classList.remove('hidden');
    try {
        const response = await fetch('/api/history/import/langsmith', { method: 'POST', headers: apiHeaders() });
        if (!response.ok) throw new Error(await apiError(response));
        const result = await response.json();
        historyFeedback.textContent = '已导入 ' + result.imported + ' 条研究记录。'
            + '已保存的研究不会重复导入。';
        historySearch.value = '';
        await loadHistory(0);
    } catch (err) {
        historyFeedback.textContent = err.message;
    } finally {
        isImportingHistory = false;
        setHistoryActionsDisabled();
    }
}

// 刷新当前分页，导入按钮直接复用异步导入函数。
historyRefresh.addEventListener('click', () => loadHistory());
historyImport.addEventListener('click', importHistory);
// 根据固定页大小计算前后页偏移，上一页不允许小于零。
historyPrev.addEventListener('click', () => loadHistory(Math.max(0, historyOffset - HISTORY_PAGE_SIZE)));
// 下一页沿当前偏移递增，是否可点击由历史查询结果控制。
historyNext.addEventListener('click', () => loadHistory(historyOffset + HISTORY_PAGE_SIZE));
// 输入变化立即让旧请求失效，延迟 250 毫秒再搜索以减少连续请求。
historySearch.addEventListener('input', () => {
    // 在防抖计时结束前使正在返回的旧结果失效。
    historyRequestVersion += 1;
    clearTimeout(historySearchTimer);
    // 连续输入重置计时，停顿后从第一页执行新搜索。
    historySearchTimer = setTimeout(() => loadHistory(0), 250);
});
// 令牌变更后重试历史查询，并尝试恢复 URL 指定的尚未打开记录。
apiToken.addEventListener('change', () => {
    loadHistory(0);
    const id = new URL(window.location.href).searchParams.get('history');
    if (id && !selectedHistoryRecord) openHistory(id);
});
reuseBtn.addEventListener('click', reuseHistoryParameters);
// 页面首次进入先加载历史列表，再按 URL 中的历史 ID 恢复报告。
loadHistory(0);
const initialHistoryId = new URL(window.location.href).searchParams.get('history');
if (initialHistoryId) openHistory(initialHistoryId);

/* 报告操作：下载原始 Markdown、复制内容和显示临时提示。 */
// 通过临时 Blob URL 下载 Markdown，触发后立即释放 URL 资源。
function downloadReport() {
    if (!reportMarkdown) return;
    const blob = new Blob([reportMarkdown], { type: 'text/markdown' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = selectedHistoryRecord && selectedHistoryRecord.status && selectedHistoryRecord.status !== 'completed'
        ? 'research-record.md' : 'research-report.md';
    a.click();
    URL.revokeObjectURL(a.href);
    toast('Downloaded');
}

// 优先使用剪贴板 API；不可用时通过临时文本框执行兼容复制。
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

// 创建短暂提示元素，并在两秒后自动移除。
function toast(msg) {
    const el = document.createElement('div');
    el.className = 'toast';
    el.textContent = msg;
    document.body.appendChild(el);
    // 移除本次创建的提示节点，不影响之后产生的其他提示。
    setTimeout(() => el.remove(), 2000);
}
