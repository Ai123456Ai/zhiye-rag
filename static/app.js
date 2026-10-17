const $ = id => document.getElementById(id);
let busy = false;
let rerankerAvailable = false;
let currentDocuments = [];
let editingDocumentId = null;

function metadataLabel(doc) {
  return `${doc.document_key || '未标注所属手册'} · ${doc.version || '未标注版本'} · 生效 ${doc.effective_from || '未标注'} · ${doc.scope || '未标注范围'}`;
}

function readMetadata(prefix) {
  return {
    document_key: $(`${prefix}-document-key`).value.trim(), version: $(`${prefix}-version`).value.trim(),
    effective_from: $(`${prefix}-effective-from`).value || null, status: $(`${prefix}-status`).value,
    scope: $(`${prefix}-scope`).value.trim()
  };
}

function refreshScopeFilter() {
  const previous = $('scope-filter').value;
  const family = $('document-filter').value;
  const scopes = [...new Set(currentDocuments.filter(doc => !family || doc.document_key === family).map(doc => doc.scope))];
  $('scope-filter').replaceChildren(new Option('全部范围', ''),
    ...scopes.sort().map(scope => new Option(scope || '未标注范围', JSON.stringify(scope))));
  if ([...$('scope-filter').options].some(option => option.value === previous)) $('scope-filter').value = previous;
}

function refreshFilters(docs) {
  currentDocuments = docs;
  const previous = $('document-filter').value;
  const families = [...new Set(docs.map(doc => doc.document_key).filter(Boolean))].sort();
  $('document-filter').replaceChildren(new Option('全部手册', ''), ...families.map(key => new Option(key, key)));
  if (families.includes(previous)) $('document-filter').value = previous;
  refreshScopeFilter();
}

function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').className = error ? 'error' : '';
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('服务响应异常，请检查后端是否正常运行。'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `请求失败（${response.status}）`);
  return data;
}

function setBusy(value) {
  busy = value;
  for (const node of document.querySelectorAll('button, input, select, textarea')) {
    node.disabled = value || (node.id === 'rerank' && !rerankerAvailable);
  }
  $('ask').textContent = value ? '处理中…' : '查找答案 ↗';
}

async function refreshDocuments() {
  const docs = await api('/api/documents');
  refreshFilters(docs);
  $('doc-count').textContent = `${docs.length} 份`;
  $('documents').replaceChildren();
  if (!docs.length) {
    const empty = document.createElement('p');
    empty.className = 'hint'; empty.textContent = '还没有资料。可以先导入演示手册，体验一次检索。';
    $('documents').append(empty);
  }
  for (const doc of docs) {
    const row = document.createElement('div'); row.className = 'doc';
    const info = document.createElement('div'); info.className = 'doc-info';
    const title = document.createElement('span'); title.className = 'doc-name'; title.textContent = doc.name; title.title = doc.name;
    const meta = document.createElement('small'); meta.textContent = `${doc.chunk_count} 个片段 · ${(doc.size / 1024).toFixed(1)} KB`;
    const version = document.createElement('small'); version.textContent = metadataLabel(doc);
    const states = { selected: '参与检索', archived: '已归档', future: '尚未生效', superseded: '已被新版替代', ambiguous: '需核实版本或范围' };
    const state = document.createElement('small'); state.textContent = states[doc.retrieval_state] || '';
    const actions = document.createElement('div'); actions.className = 'doc-actions';
    const edit = document.createElement('button'); edit.className = 'doc-action'; edit.textContent = '版本';
    edit.setAttribute('aria-label', `编辑 ${doc.name} 的版本信息`);
    edit.addEventListener('click', () => {
      if (busy) return;
      editingDocumentId = doc.id;
      $('metadata-name').textContent = doc.name;
      for (const [field, id] of Object.entries({ document_key: 'document-key', version: 'version', effective_from: 'effective-from', scope: 'scope', status: 'status' })) {
        $(`edit-${id}`).value = doc[field] || (field === 'status' ? 'active' : '');
      }
      $('metadata-error').textContent = '';
      $('metadata-dialog').showModal();
    });
    const view = document.createElement('button'); view.className = 'doc-action'; view.textContent = '原文';
    view.setAttribute('aria-label', `查看 ${doc.name} 原文`);
    view.addEventListener('click', async () => {
      if (busy) return;
      setBusy(true);
      try {
        const result = await api(`/api/documents/${encodeURIComponent(doc.id)}`);
        $('document-title').textContent = result.document.name;
        $('document-meta').textContent = metadataLabel(result.document);
        $('document-preview').replaceChildren();
        for (const chunk of result.chunks) {
          const heading = document.createElement('h3'); heading.textContent = `片段 ${chunk.ordinal}${chunk.page ? ` · 第 ${chunk.page} 页` : ''}`;
          const text = document.createElement('p'); text.textContent = chunk.text;
          $('document-preview').append(heading, text);
        }
        $('document-dialog').showModal();
      } catch (error) { notice(error.message, true); }
      finally { setBusy(false); }
    });
    const remove = document.createElement('button'); remove.className = 'delete'; remove.textContent = '×'; remove.setAttribute('aria-label', `删除 ${doc.name}`);
    remove.addEventListener('click', async () => {
      if (busy || !confirm(`删除文档“${doc.name}”及其检索索引？`)) return;
      setBusy(true);
      try { await api(`/api/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' }); await refreshDocuments(); $('result').hidden = true; notice('文档已删除。'); }
      catch (error) { notice(error.message, true); }
      finally { setBusy(false); }
    });
    info.append(title, meta, version, state); actions.append(view, edit, remove);
    row.append(info, actions); $('documents').append(row);
  }
}

async function importDocument(path, options) {
  if (busy) return;
  setBusy(true); notice('正在解析并建立索引；首次使用语义模型可能需要下载模型…');
  try {
    const result = await api(path, options);
    await refreshDocuments(); $('result').hidden = true;
    notice(result.created ? '资料已保存。检索会按版本、生效日期与有效状态筛选。' : '相同内容已在知识库中，无需重复导入。');
  } catch (error) { notice(error.message, true); }
  finally { setBusy(false); $('file').value = ''; }
}

$('file').addEventListener('change', () => {
  const file = $('file').files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) { notice('单个文档不能超过 10 MB。', true); $('file').value = ''; return; }
  const body = new FormData(); body.append('file', file);
  for (const [key, value] of Object.entries(readMetadata('upload'))) body.append(key, value ?? '');
  importDocument('/api/documents', { method: 'POST', body });
});
$('sample').addEventListener('click', () => importDocument('/api/documents/sample', { method: 'POST' }));
$('metadata-cancel').addEventListener('click', () => $('metadata-dialog').close());
$('document-close').addEventListener('click', () => $('document-dialog').close());
$('document-filter').addEventListener('change', () => { refreshScopeFilter(); $('result').hidden = true; });
$('scope-filter').addEventListener('change', () => { $('result').hidden = true; });
$('metadata-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !editingDocumentId) return;
  setBusy(true);
  try {
    await api(`/api/documents/${encodeURIComponent(editingDocumentId)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(readMetadata('edit'))
    });
    await refreshDocuments(); $('metadata-dialog').close(); $('result').hidden = true;
    notice('版本信息已更新，后续检索立即使用新的筛选规则。');
  } catch (error) { $('metadata-error').textContent = error.message; }
  finally { setBusy(false); }
});
for (const suggestion of document.querySelectorAll('[data-question]')) {
  suggestion.addEventListener('click', () => { $('question').value = suggestion.dataset.question; $('question').focus(); });
}

$('question-form').addEventListener('submit', async event => {
  event.preventDefault();
  const question = $('question').value.trim();
  if (busy || !question) return;
  setBusy(true); notice('正在检索与问题相关的资料…'); $('result').hidden = true;
  try {
    const result = await api('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, mode: $('mode').value, top_k: Number($('top-k').value), rerank: $('rerank').checked,
        decompose: $('decompose').checked,
        document_key: $('document-filter').value || null,
        scope: $('scope-filter').value ? JSON.parse($('scope-filter').value) : null })
    });
    $('answer-label').textContent = result.answer_mode === 'document_conflict' ? '需要核实资料' : result.answer_mode === 'excerpts' ? '资料摘录' : '回答';
    $('answer').textContent = result.answer;
    $('metrics').textContent = `${result.retrieval_mode.toUpperCase()}${result.rerank_enabled ? ' + 重排序' : ''}${result.query_plan?.applied ? ' + 问题拆解' : ''} · ${result.latency_ms} ms`;
    $('warnings').textContent = result.warnings.join(' ');
    $('query-plan').hidden = !result.query_plan?.applied;
    $('subqueries').replaceChildren();
    for (const query of result.query_plan?.applied ? result.query_plan.queries.slice(1) : []) {
      const item = document.createElement('li'); item.textContent = query; $('subqueries').append(item);
    }
    $('conflicts').replaceChildren();
    for (const conflict of result.conflicts || []) {
      const heading = document.createElement('h3'); heading.textContent = `${conflict.document_key}：候选资料（尚未确定适用版本）`;
      $('conflicts').append(heading);
      for (const doc of conflict.documents) {
        const details = document.createElement('details'); details.className = 'source';
        const title = document.createElement('summary'); title.textContent = `${doc.name} · ${metadataLabel(doc)}`;
        const text = document.createElement('p');
        text.textContent = doc.excerpt ? `候选片段 ${doc.excerpt.ordinal}：\n${doc.excerpt.text}` : '此候选文档没有召回匹配片段，可在左侧查看原文。';
        details.append(title, text); $('conflicts').append(details);
      }
    }
    $('source-count').textContent = `(${result.sources.length})`;
    $('sources').replaceChildren();
    for (const source of result.sources) {
      const details = document.createElement('details'); details.className = 'source';
      const summary = document.createElement('summary');
      summary.textContent = `[${source.citation}] ${source.document_name} · ${source.page ? `第 ${source.page} 页` : `片段 ${source.ordinal}`}`;
      const score = document.createElement('small');
      score.textContent = `检索分数 ${source.score}${source.rerank_score === undefined ? '' : ` · 重排序分数 ${source.rerank_score}`}（不是置信度）`;
      const text = document.createElement('p'); text.textContent = source.text;
      const metadata = document.createElement('small'); metadata.textContent = metadataLabel(source);
      details.append(summary, metadata, document.createElement('br'), score, text); $('sources').append(details);
      if (source.retrieval_query) {
        const query = document.createElement('small'); query.textContent = `查找问题：${source.retrieval_query}`;
        details.append(query);
      }
    }
    $('result').hidden = false; notice('');
  } catch (error) { notice(error.message, true); }
  finally { setBusy(false); }
});

async function initialize() {
  try {
    const health = await api('/api/health');
    $('status').textContent = health.generation_enabled ? '大模型已配置' : '本地检索模式';
    const names = { bm25: '关键词 BM25', vector: '向量检索', hybrid: '混合检索 RRF' };
    $('mode').replaceChildren(...health.retrieval_modes.map(mode => new Option(names[mode], mode)));
    rerankerAvailable = health.reranker_available === true;
    $('rerank-option').hidden = !rerankerAvailable;
    $('rerank').disabled = !rerankerAvailable;
    $('mode-note').textContent = health.generation_enabled
      ? '回答由已配置的大模型生成，请结合引用原文核对。'
      : '当前展示检索原文，不调用大模型。配置模型接口后可生成回答。';
    await refreshDocuments();
  } catch (error) { $('status').textContent = '连接失败'; notice(error.message, true); }
}
initialize();
