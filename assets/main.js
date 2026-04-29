/* ════════════════════════════════════════════
 * State
 * ════════════════════════════════════════════ */
const MODELS = [
  { id: 'dual-model', name: 'Dual Model', desc: 'Ollama · Spec + Coder 파이프라인' },
];

const STATE = {
  chats: [],
  currentChatId: null,
  notes: '',
  settings: {
    theme: 'system',
    fontSize: 14,
    temperature: 0.7,
    autoplay: false,
    ttsVoice: '',
    runMode: 'confirm',
  },
};

let attachments = [];
let currentPreviewCode = '';
let isGenerating = false;
let currentSpeech = null;
let currentAbortController = null;
let memoryItems = [];

const $ = id => document.getElementById(id);
const sidebar = $('sidebar');
const welcome = $('welcome');
const messagesEl = $('messages');
const chatInput = $('chat-input');
const sendBtn = $('send-btn');
const previewPanel = $('preview-panel');
const DEFAULT_MODEL_ID = 'dual-model';
let draftModel = DEFAULT_MODEL_ID;

/* ════════════════════════════════════════════
 * Persistence (계정별 분리)
 * ════════════════════════════════════════════ */
function _stateKey() {
  const user = localStorage.getItem('vibe_auth_user') || '_guest';
  return `vibe_chat_state_${user}`;
}

function defaultSettings() {
  return {
    theme: 'system',
    fontSize: 14,
    temperature: 0.7,
    autoplay: false,
    ttsVoice: '',
    runMode: 'confirm',
  };
}

function normalizeMessage(message = {}) {
  const timestamp = Number.isFinite(message.timestamp) ? message.timestamp : null;
  const role = message.role === 'assistant' ? 'assistant' : 'user';
  return {
    id: typeof message.id === 'string' && message.id ? message.id : uid(),
    requestId: typeof message.requestId === 'string' && message.requestId ? message.requestId : null,
    role,
    content: typeof message.content === 'string' ? message.content : String(message.content ?? ''),
    attachments: Array.isArray(message.attachments) ? message.attachments : [],
    timestamp,
    createdAt: typeof message.createdAt === 'string' && message.createdAt
      ? message.createdAt
      : (timestamp === null ? null : new Date(timestamp).toISOString()),
    durationMs: Number.isFinite(message.durationMs) ? message.durationMs : null,
  };
}

function normalizeChat(chat = {}) {
  const createdAt = Number.isFinite(chat.createdAt) ? chat.createdAt : Date.now();
  const messages = Array.isArray(chat.messages)
    ? chat.messages.filter(m => m && typeof m === 'object').map(normalizeMessage)
    : [];
  const latestTimestamp = messages.length ? messages[messages.length - 1].timestamp : createdAt;

  return {
    id: typeof chat.id === 'string' && chat.id ? chat.id : uid(),
    title: typeof chat.title === 'string' && chat.title ? chat.title : '새 채팅',
    messages,
    systemPrompt: typeof chat.systemPrompt === 'string' ? chat.systemPrompt : '',
    model: typeof chat.model === 'string' && chat.model ? chat.model : DEFAULT_MODEL_ID,
    pinned: Boolean(chat.pinned),
    createdAt,
    updatedAt: Number.isFinite(chat.updatedAt) ? chat.updatedAt : latestTimestamp,
  };
}

function normalizeStateShape(raw = {}) {
  const rawSettings = raw.settings && typeof raw.settings === 'object' ? raw.settings : {};
  const { defaultModel: _ignoredDefaultModel, ...persistedSettings } = rawSettings;
  const chats = Array.isArray(raw.chats) ? raw.chats.map(normalizeChat) : [];
  const currentChatId = chats.some(chat => chat.id === raw.currentChatId)
    ? raw.currentChatId
    : (chats[0] ? chats[0].id : null);

  return {
    chats,
    currentChatId,
    notes: typeof raw.notes === 'string' ? raw.notes : '',
    settings: { ...defaultSettings(), ...persistedSettings },
  };
}

function buildMessage(role, content, extra = {}) {
  const timestamp = Number.isFinite(extra.timestamp) ? extra.timestamp : Date.now();
  return {
    id: typeof extra.id === 'string' && extra.id ? extra.id : uid(),
    requestId: typeof extra.requestId === 'string' && extra.requestId ? extra.requestId : null,
    role,
    content: typeof content === 'string' ? content : String(content ?? ''),
    attachments: Array.isArray(extra.attachments) ? extra.attachments : [],
    timestamp,
    createdAt: typeof extra.createdAt === 'string' && extra.createdAt
      ? extra.createdAt
      : new Date(timestamp).toISOString(),
    durationMs: Number.isFinite(extra.durationMs) ? extra.durationMs : null,
  };
}

function buildApiMessages(chat) {
  if (!chat || !Array.isArray(chat.messages)) return [];
  return chat.messages
    .filter(m => m && (m.role === 'user' || m.role === 'assistant'))
    .filter(m => typeof m.content === 'string' && m.content.trim())
    .slice(-20)
    .map(m => ({
      role: m.role,
      content: m.content.trim(),
    }));
}

function resetState() {
  STATE.chats = [];
  STATE.currentChatId = null;
  STATE.notes = '';
  STATE.settings = defaultSettings();
  draftModel = DEFAULT_MODEL_ID;
}

function loadState() {
  try {
    const s = localStorage.getItem(_stateKey());
    if (s) {
      Object.assign(STATE, normalizeStateShape(JSON.parse(s)));
    }
  } catch (e) { console.error(e); }
}

async function loadStateFromServer() {
  if (!isLoggedIn()) return;
  try {
    const res = await fetch('/api/chats', { headers: authHeaders() });
    if (res.status === 401) { logout(); return; }
    if (!res.ok) return;
    const data = await res.json();
    Object.assign(STATE, normalizeStateShape({
      chats: data.chats,
      currentChatId: data.currentChatId,
      notes: STATE.notes,
      settings: { ...STATE.settings, ...(data.settings || {}) },
    }));
    saveStateLocal();
    renderChatList();
    renderMessages();
  } catch (e) { console.error('서버에서 채팅 로드 실패:', e); }
}

let _saveTimer = null;
function saveState() {
  saveStateLocal();
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(saveStateToServer, 1000);
}

function saveStateLocal() {
  const key = _stateKey();
  // 1) 전체 저장 시도
  try {
    localStorage.setItem(key, JSON.stringify(STATE));
    return;
  } catch (e) {
    if (!(e && (e.name === 'QuotaExceededError' || e.code === 22 || e.code === 1014))) {
      // 다른 예외는 삼키지 않음
      console.error('로컬 상태 저장 실패:', e);
      return;
    }
  }
  // 2) 할당량 초과 → 현재 채팅만 남기고 축소 저장
  try {
    const current = STATE.chats.find(c => c.id === STATE.currentChatId);
    const minimal = {
      chats: current ? [current] : [],
      currentChatId: current ? current.id : null,
      notes: '',
      settings: STATE.settings,
    };
    localStorage.setItem(key, JSON.stringify(minimal));
    return;
  } catch (_) {}
  // 3) 그래도 넘치면 settings 만 저장
  try {
    localStorage.setItem(key, JSON.stringify({
      chats: [], currentChatId: null, notes: '', settings: STATE.settings,
    }));
  } catch (_) {
    // 최종 실패는 무시 — 서버가 원본이므로 기능은 유지됨
  }
}

async function saveStateToServer() {
  if (!isLoggedIn()) return;
  try {
    const res = await fetch('/api/chats', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ chats: STATE.chats, settings: STATE.settings }),
    });
    if (res.status === 401) { logout(); return; }
  } catch (e) { console.error('서버 저장 실패:', e); }
}

/* ════════════════════════════════════════════
 * Chat CRUD
 * ════════════════════════════════════════════ */
function uid() { return Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }

function createChat() {
  const chat = {
    id: uid(),
    title: '새 채팅',
    messages: [],
    systemPrompt: '',
    model: draftModel,
    pinned: false,
    createdAt: Date.now(),
    updatedAt: Date.now(),
  };
  STATE.chats.unshift(chat);
  STATE.currentChatId = chat.id;
  saveState();
  renderChatList();
  renderMessages();
  return chat;
}

function getCurrentChat() {
  if (!STATE.currentChatId) return null;
  return STATE.chats.find(c => c.id === STATE.currentChatId);
}

function deleteChat(id) {
  STATE.chats = STATE.chats.filter(c => c.id !== id);
  if (STATE.currentChatId === id) {
    STATE.currentChatId = STATE.chats.length ? STATE.chats[0].id : null;
  }
  saveState();
  renderChatList();
  renderMessages();
  toast('채팅 삭제됨');
}

function renameChat(id, newTitle) {
  const chat = STATE.chats.find(c => c.id === id);
  if (chat) { chat.title = newTitle; chat.updatedAt = Date.now(); saveState(); renderChatList(); }
}

function togglePin(id) {
  const chat = STATE.chats.find(c => c.id === id);
  if (chat) { chat.pinned = !chat.pinned; saveState(); renderChatList(); toast(chat.pinned ? '고정됨' : '고정 해제됨'); }
}

function exportChat(id) {
  const chat = STATE.chats.find(c => c.id === id);
  if (!chat) return;
  let md = `# ${chat.title}\n\n`;
  for (const m of chat.messages) {
    md += `## ${m.role === 'user' ? '사용자' : 'AI'}\n\n${m.content}\n\n`;
  }
  download(md, `${chat.title}.md`, 'text/markdown');
  toast('대화를 다운로드했습니다');
}

/* ════════════════════════════════════════════
 * Render: chat list
 * ════════════════════════════════════════════ */
function renderChatList(filter = '') {
  const q = (filter || '').trim().toLowerCase();
  const filtered = STATE.chats.filter(c =>
    !q || c.title.toLowerCase().includes(q) ||
    c.messages.some(m => m.content.toLowerCase().includes(q))
  );
  const pinned = filtered.filter(c => c.pinned);
  const regular = filtered.filter(c => !c.pinned);

  $('pinned-title').classList.toggle('is-hidden', !pinned.length);
  $('pinned-list').innerHTML = pinned.map(c => renderChatItem(c, q)).join('');
  $('chat-list').innerHTML = regular.map(c => renderChatItem(c, q)).join('');

  document.querySelectorAll('.chat-item').forEach(el => {
    el.onclick = (e) => {
      if (e.target.closest('.chat-item-menu')) return;
      switchChat(el.dataset.id);
    };
    el.oncontextmenu = (e) => { e.preventDefault(); showChatContextMenu(e, el.dataset.id); };
    el.querySelector('.chat-item-menu').onclick = (e) => {
      e.stopPropagation();
      const r = el.getBoundingClientRect();
      showChatContextMenu({ clientX: r.right, clientY: r.bottom }, el.dataset.id);
    };
  });
}

function buildSearchSnippet(chat, q) {
  if (!q) return '';
  for (const m of chat.messages) {
    const content = (m.content || '').toString();
    const idx = content.toLowerCase().indexOf(q);
    if (idx === -1) continue;
    const start = Math.max(0, idx - 20);
    const end = Math.min(content.length, idx + q.length + 40);
    const before = escapeHtml(content.slice(start, idx));
    const hit = escapeHtml(content.slice(idx, idx + q.length));
    const after = escapeHtml(content.slice(idx + q.length, end));
    const prefix = start > 0 ? '…' : '';
    const suffix = end < content.length ? '…' : '';
    return `${prefix}${before}<mark>${hit}</mark>${after}${suffix}`;
  }
  return '';
}

function renderChatItem(chat, q = '') {
  const active = chat.id === STATE.currentChatId ? ' active' : '';
  const pin = chat.pinned ? `<svg class="pin-icon" width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M16 9V4l1-1V2H7v1l1 1v5L6 11v2h5v8l1 1 1-1v-8h5v-2l-2-2z"/></svg>` : '';
  const snippet = q && !chat.title.toLowerCase().includes(q) ? buildSearchSnippet(chat, q) : '';
  const titleBlock = snippet
    ? `<div style="flex:1; overflow:hidden;">
         <div class="chat-item-title">${escapeHtml(chat.title)}</div>
         <span class="chat-item-snippet">${snippet}</span>
       </div>`
    : `<span class="chat-item-title">${escapeHtml(chat.title)}</span>`;
  return `<div class="chat-item${active}" data-id="${chat.id}">
    ${pin}
    ${titleBlock}
    <button class="chat-item-menu">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="1"/><circle cx="12" cy="5" r="1"/><circle cx="12" cy="19" r="1"/></svg>
    </button>
  </div>`;
}

function switchChat(id) {
  STATE.currentChatId = id;
  saveState();
  renderChatList();
  renderMessages();
  closePreview();
}

/* ════════════════════════════════════════════
 * Render: messages
 * ════════════════════════════════════════════ */
function renderMessages() {
  const chat = getCurrentChat();
  if (!chat || chat.messages.length === 0) {
    welcome.classList.remove('hidden');
    messagesEl.innerHTML = '';
    sidebar.classList.remove('collapsed');
    updateModelChip();
    return;
  }
  welcome.classList.add('hidden');
  sidebar.classList.add('collapsed');
  messagesEl.innerHTML = '';
  chat.messages.forEach((m, i) => messagesEl.appendChild(renderMessage(m, i)));
  updateModelChip();
  scrollToBottom();
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;

  const seconds = ms / 1000;
  if (seconds < 60) {
    const precision = seconds < 10 ? 1 : 0;
    return `${seconds.toFixed(precision)}초`;
  }

  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds - (minutes * 60);
  if (remainingSeconds < 1) return `${minutes}분`;

  const precision = remainingSeconds < 10 ? 1 : 0;
  return `${minutes}분 ${remainingSeconds.toFixed(precision)}초`;
}

function getElapsedLabel(chat, index, msg) {
  if (msg.role !== 'assistant') return '';

  let elapsedMs = Number.isFinite(msg.durationMs) ? msg.durationMs : null;
  if (elapsedMs === null) {
    const previousMessage = chat.messages[index - 1];
    if (
      previousMessage &&
      previousMessage.role === 'user' &&
      Number.isFinite(previousMessage.timestamp) &&
      Number.isFinite(msg.timestamp)
    ) {
      elapsedMs = Math.max(0, msg.timestamp - previousMessage.timestamp);
    }
  }

  const durationText = formatDuration(elapsedMs);
  return durationText ? `소요 시간 ${durationText}` : '';
}

function renderMessage(msg, index) {
  const div = document.createElement('div');
  div.className = `message ${msg.role}`;
  div.dataset.index = index;
  const chat = getCurrentChat();
  const elapsedLabel = chat ? getElapsedLabel(chat, index, msg) : '';

  if (msg.role === 'user') {
    const wrap = document.createElement('div');
    wrap.className = 'user-message-wrap';
    if (msg.attachments && msg.attachments.length) {
      const att = document.createElement('div');
      att.className = 'attachments';
      att.innerHTML = msg.attachments.map(a =>
        a.type.startsWith('image/')
          ? `<img class="attachment-thumb" src="${a.data}">`
          : `<div class="attachment-file"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>${escapeHtml(a.name)}</div>`
      ).join('');
      wrap.appendChild(att);
    }
    const bubble = document.createElement('div');
    bubble.className = 'user-bubble';
    bubble.textContent = msg.content;
    wrap.appendChild(bubble);
    wrap.appendChild(buildMessageActions(msg, index));
    div.appendChild(wrap);
  } else {
    const content = document.createElement('div');
    content.className = 'assistant-content';
    const parsed = parseMarkdown(msg.content);
    content.innerHTML = parsed.html;
    div.appendChild(content);
    // 코드블록을 마커 위치에 정확히 삽입
    parsed.codeBlocks.forEach((cb, i) => {
      const marker = content.querySelector(`[data-code-marker="${i}"]`);
      if (marker) {
        marker.replaceWith(createCodeBlock(cb.code, cb.lang));
      } else {
        content.appendChild(createCodeBlock(cb.code, cb.lang));
      }
    });
    const combinedBtn = maybeCreateCombinedPreviewButton(content);
    if (combinedBtn) content.appendChild(combinedBtn);
    if (elapsedLabel) { const t = document.createElement('div'); t.className = 'msg-time'; t.textContent = elapsedLabel; div.appendChild(t); }
    div.appendChild(buildMessageActions(msg, index));
    enhanceContent(content);
  }
  return div;
}

function buildMessageActions(msg, index) {
  const wrap = document.createElement('div');
  wrap.className = 'msg-actions';
  const buttons = [];
  buttons.push({ icon: 'copy', title: '복사', fn: () => { copyText(msg.content); toast('복사됨'); } });
  if (msg.role === 'user') {
    buttons.push({ icon: 'edit', title: '편집', fn: () => editMessage(index) });
  } else {
    buttons.push({ icon: 'refresh', title: '재생성', fn: () => regenerate(index) });
    buttons.push({ icon: 'volume', title: '읽어주기', fn: (btn) => speak(msg.content, btn) });
  }
  buttons.push({ icon: 'trash', title: '삭제', fn: () => deleteMessage(index) });

  buttons.forEach(b => {
    const btn = document.createElement('button');
    btn.className = 'msg-action-btn';
    btn.title = b.title;
    btn.innerHTML = ICONS[b.icon];
    btn.onclick = () => b.fn(btn);
    wrap.appendChild(btn);
  });
  return wrap;
}

const ICONS = {
  copy: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
  edit: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>`,
  refresh: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`,
  volume: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>`,
  trash: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`,
};

function editMessage(index) {
  const chat = getCurrentChat();
  const msg = chat.messages[index];
  const msgEl = messagesEl.children[index];
  const bubble = msgEl.querySelector('.user-bubble');
  bubble.classList.add('editing');
  bubble.innerHTML = `
    <textarea class="edit-textarea">${escapeHtml(msg.content)}</textarea>
    <div class="edit-actions">
      <button onclick="cancelEdit()">취소</button>
      <button class="primary" onclick="saveEdit(${index})">저장 후 재생성</button>
    </div>`;
  bubble.querySelector('textarea').focus();
}

function cancelEdit() { renderMessages(); }

function saveEdit(index) {
  const chat = getCurrentChat();
  const newText = messagesEl.children[index].querySelector('.edit-textarea').value.trim();
  if (!newText) return;
  chat.messages[index].content = newText;
  // Remove all messages after this user message
  chat.messages = chat.messages.slice(0, index + 1);
  chat.updatedAt = Date.now();
  saveState();
  renderMessages();
  generateResponse();
}

function deleteMessage(index) {
  const chat = getCurrentChat();
  chat.messages.splice(index, 1);
  chat.updatedAt = Date.now();
  saveState();
  renderMessages();
}

function regenerate(index) {
  const chat = getCurrentChat();
  // Remove this assistant message and regenerate
  chat.messages = chat.messages.slice(0, index);
  chat.updatedAt = Date.now();
  saveState();
  renderMessages();
  generateResponse();
}

/* ════════════════════════════════════════════
 * Math (KaTeX) + Mermaid rendering
 * ════════════════════════════════════════════ */
let _mermaidInitialized = false;
function ensureMermaid() {
  if (!window.mermaid || _mermaidInitialized) return;
  try {
    window.mermaid.initialize({ startOnLoad: false, theme: document.documentElement.classList.contains('light') ? 'default' : 'dark', securityLevel: 'loose' });
    _mermaidInitialized = true;
  } catch (_) {}
}

let _mermaidIdCounter = 0;
async function renderMermaidIn(el) {
  if (!window.mermaid) return;
  ensureMermaid();
  const blocks = el.querySelectorAll('.mermaid-block.pending');
  for (const wrap of blocks) {
    const src = wrap.dataset.mermaidSrc || '';
    if (!src.trim()) { wrap.classList.remove('pending'); continue; }
    const id = `mmd-${Date.now()}-${_mermaidIdCounter++}`;
    try {
      const { svg } = await window.mermaid.render(id, src);
      wrap.innerHTML = svg;
      wrap.classList.remove('pending');
    } catch (e) {
      wrap.classList.add('error');
      wrap.classList.remove('pending');
      wrap.textContent = 'Mermaid 렌더 실패: ' + (e && e.message ? e.message : e);
    }
  }
}

function renderMathIn(el) {
  if (!window.renderMathInElement) return;
  try {
    window.renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false },
        { left: '$', right: '$', display: false },
      ],
      throwOnError: false,
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
    });
  } catch (_) {}
}

function enhanceContent(el) {
  if (!el) return;
  renderMathIn(el);
  // Mermaid 는 비동기 렌더 — fire-and-forget
  renderMermaidIn(el);
}

/* ════════════════════════════════════════════
 * Markdown parser (simple)
 * ════════════════════════════════════════════ */
function parseMarkdown(text) {
  const codeBlocks = [];
  // Normalize line endings
  text = text.replace(/\r\n/g, '\n');
  // Extract fenced code blocks (handle language + optional extra text on first line)
  text = text.replace(/```([^\n]*)\n([\s\S]*?)```/g, (_, firstLine, code) => {
    const lang = firstLine.trim().split(/\s/)[0] || 'text';
    codeBlocks.push({ lang, code: code.trimEnd() });
    return `\u0000CODE_${codeBlocks.length - 1}\u0000`;
  });
  // Escape
  let html = escapeHtml(text);
  // Allow a small whitelist of HTML through (for <details> spec preview)
  html = html
    .replace(/&lt;details&gt;/g, '<details>')
    .replace(/&lt;\/details&gt;/g, '</details>')
    .replace(/&lt;summary&gt;/g, '<summary>')
    .replace(/&lt;\/summary&gt;/g, '</summary>');
  // Headings
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');
  // Bold / italic
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  // Inline code
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  // Links
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Lists
  html = html.replace(/(^|\n)((?:[-*] .+(?:\n|$))+)/g, (_, p, items) => {
    const lis = items.trim().split(/\n/).map(l => `<li>${l.replace(/^[-*]\s+/, '')}</li>`).join('');
    return `${p}<ul>${lis}</ul>`;
  });
  html = html.replace(/(^|\n)((?:\d+\. .+(?:\n|$))+)/g, (_, p, items) => {
    const lis = items.trim().split(/\n/).map(l => `<li>${l.replace(/^\d+\.\s+/, '')}</li>`).join('');
    return `${p}<ol>${lis}</ol>`;
  });
  // Blockquote
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote>$1</blockquote>');
  // Paragraphs (split on double newline)
  html = html.split(/\n\n+/).map(p => {
    if (p.match(/^<(h[123]|ul|ol|blockquote|pre|details|\/details|summary|\/summary)/)) return p;
    if (p.includes('</details>') || p.includes('<details>') || p.includes('<summary>')) return p;
    if (p.includes('\u0000CODE_')) return p;
    return p.trim() ? `<p>${p.replace(/\n/g, '<br>')}</p>` : '';
  }).join('');
  // Replace code placeholders with markers (we'll insert actual blocks separately)
  html = html.replace(/\u0000CODE_(\d+)\u0000/g, '<div data-code-marker="$1"></div>');
  return { html, codeBlocks };
}

/* ════════════════════════════════════════════
 * Code block
 * ════════════════════════════════════════════ */
// 파일경로(src/main.jsx)가 lang으로 들어오는 경우 확장자로 언어 추정
function resolveLang(lang) {
  let l = (lang || '').toLowerCase();
  if (l.includes('.')) {
    const ext = l.split('.').pop();
    const map = { html: 'html', htm: 'html', js: 'javascript', mjs: 'javascript',
      jsx: 'jsx', ts: 'typescript', tsx: 'tsx', dart: 'dart' };
    if (map[ext]) l = map[ext];
  }
  return l;
}

function createCodeBlock(code, lang) {
  const resolvedLang = resolveLang(lang);
  // Mermaid 다이어그램 — code-block 대신 렌더용 컨테이너 반환
  if (resolvedLang === 'mermaid' || (lang || '').toLowerCase() === 'mermaid') {
    const wrap = document.createElement('div');
    wrap.className = 'mermaid-block pending';
    wrap.dataset.mermaidSrc = code;
    wrap.textContent = code;  // 폴백 — Mermaid 로드 실패 시 원본 표시
    return wrap;
  }
  const isPreviewable = ['html', 'jsx', 'tsx', 'javascript', 'dart'].includes(resolvedLang);
  const div = document.createElement('div');
  div.className = 'code-block';
  div.dataset.code = code;
  div.dataset.lang = lang;
  div.innerHTML = `
    <div class="code-header">
      <span class="code-lang">${lang}</span>
      <div class="code-actions">
        ${isPreviewable ? `<button class="code-btn run-inline-btn" title="이 코드를 채팅 안에서 바로 실행">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
          인라인 실행
        </button>` : ''}
        ${isPreviewable ? `<button class="code-btn preview-btn">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
          미리보기
        </button>` : ''}
        <button class="code-btn copy-code-btn">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          복사
        </button>
      </div>
    </div>
    <div class="code-content">${highlightCode(code, lang)}</div>`;
  if (isPreviewable) {
    div.querySelector('.preview-btn').onclick = () => openPreview(code, resolvedLang, div, lang);
    div.querySelector('.run-inline-btn').onclick = () => toggleInlineSim(div, code, resolvedLang, lang);
  }
  div.querySelector('.copy-code-btn').onclick = (e) => {
    copyText(code);
    const b = e.currentTarget;
    const orig = b.innerHTML;
    b.textContent = '복사됨!';
    setTimeout(() => b.innerHTML = orig, 1500);
  };
  return div;
}

function highlightCode(code, lang) {
  // 파일경로 형태가 들어오면 확장자로 언어를 추정
  let l = (lang || '').toLowerCase();
  if (l.includes('.')) {
    const ext = l.split('.').pop();
    const map = { html: 'html', htm: 'html', css: 'css', js: 'javascript', mjs: 'javascript',
      jsx: 'jsx', ts: 'typescript', tsx: 'tsx', py: 'python', json: 'json', md: 'markdown',
      sh: 'bash', yml: 'yaml', yaml: 'yaml', sql: 'sql', rs: 'rust', go: 'go', java: 'java',
      dart: 'dart', xml: 'xml', toml: 'ini' };
    l = map[ext] || ext;
  }
  // 1순위: highlight.js (VSCode Dark+ 스타일 vs2015)
  if (window.hljs) {
    try {
      const langAlias = { js: 'javascript', py: 'python', htm: 'html', yml: 'yaml',
                          jsx: 'javascript', tsx: 'typescript' };
      const useLang = langAlias[l] || l;
      if (useLang && window.hljs.getLanguage(useLang)) {
        return '<pre><code class="hljs language-' + useLang + '">' +
               window.hljs.highlight(code, { language: useLang, ignoreIllegals: true }).value +
               '</code></pre>';
      }
      // 언어 미상 → auto-detect
      return '<pre><code class="hljs">' +
             window.hljs.highlightAuto(code).value + '</code></pre>';
    } catch (_) { /* 폴백으로 떨어짐 */ }
  }
  // 폴백: 기존의 간단 정규식 하이라이터
  let s = escapeHtml(code);
  if (l === 'html') {
    s = s.replace(/(&lt;\/?)([\w-]+)/g, '$1<b class="hl-tag">$2</b>')
      .replace(/([\w-]+)=/g, '<b class="hl-attr">$1</b>=')
      .replace(/(&quot;)([^&]*)(\1)/g, '<b class="hl-str">$1$2$3</b>');
  } else if (l === 'css') {
    s = s.replace(/(\/\*[\s\S]*?\*\/)/g, '<b class="hl-cmt">$1</b>')
      .replace(/([.#]?[\w-]+)\s*\{/g, '<b class="hl-fn">$1</b> {')
      .replace(/([\w-]+):/g, '<b class="hl-attr">$1</b>:');
  } else if (['js', 'javascript', 'python', 'py', 'typescript', 'ts'].includes(l)) {
    s = s.replace(/(\/\/[^\n]*|#[^\n]*)/g, '<b class="hl-cmt">$1</b>')
      .replace(/\b(const|let|var|function|return|if|else|for|while|class|import|export|from|async|await|new|def|print|in|not|and|or|True|False|None|interface|type|enum)\b/g, '<b class="hl-kw">$1</b>')
      .replace(/(&quot;[^&]*&quot;|&#x27;[^&]*&#x27;|`[^`]*`)/g, '<b class="hl-str">$1</b>')
      .replace(/\b(\d+)\b/g, '<b class="hl-num">$1</b>');
  }
  return '<pre>' + s + '</pre>';
}

/* ════════════════════════════════════════════
 * Preview panel
 * ════════════════════════════════════════════ */
let currentPreviewLang = 'html';

// 같은 메시지(부모 컨테이너) 안의 모든 code-block 을 {경로: 코드} 맵으로 수집
let currentPreviewFiles = {};
let currentPreviewEntryPath = '';

function gatherSiblingFiles(triggerEl) {
  const files = {};
  if (!triggerEl) return files;
  let parent = triggerEl.parentElement;
  // 메시지 본문 컨테이너를 찾을 때까지 위로 (보통 직속 부모가 .content)
  for (let i = 0; i < 5 && parent; i++) {
    const blocks = parent.querySelectorAll('.code-block');
    if (blocks.length >= 1) {
      blocks.forEach(blk => {
        const path = blk.dataset.lang || '';
        const content = blk.dataset.code || '';
        if (path && content && path.includes('.')) {
          files[path] = content;
        }
      });
      if (blocks.length >= 1) break;
    }
    parent = parent.parentElement;
  }
  return files;
}

function openPreview(code, lang = 'html', triggerEl = null, originalLang = '') {
  currentPreviewCode = code;
  currentPreviewLang = (lang || 'html').toLowerCase();
  currentPreviewFiles = gatherSiblingFiles(triggerEl);
  currentPreviewEntryPath = originalLang || '';
  previewPanel.classList.add('open');
  switchPreviewTab('preview');
}

function closePreview() {
  previewPanel.classList.remove('open');
  $('preview-frame').srcdoc = '';
}

/* Inline simulation — render iframe directly under code block */
function toggleInlineSim(codeBlockEl, code, resolvedLang, originalLang) {
  // 이미 인라인 시뮬이 붙어있으면 토글로 제거
  const existing = codeBlockEl.nextElementSibling;
  if (existing && existing.classList && existing.classList.contains('inline-sim')) {
    existing.remove();
    return;
  }
  // 형제 코드블록 정보 활용해 멀티파일도 지원
  currentPreviewFiles = gatherSiblingFiles(codeBlockEl);
  currentPreviewEntryPath = originalLang || '';
  const srcdoc = buildPreviewSrcdoc(code, resolvedLang);

  const wrap = document.createElement('div');
  wrap.className = 'inline-sim';
  wrap.innerHTML = `
    <div class="inline-sim-header">
      <span>▶ 인라인 실행 — ${escapeHtml(originalLang || resolvedLang)}</span>
      <button class="inline-sim-close" title="닫기">✕ 닫기</button>
    </div>
    <iframe sandbox="allow-scripts allow-same-origin"></iframe>`;
  wrap.querySelector('iframe').srcdoc = srcdoc;
  wrap.querySelector('.inline-sim-close').onclick = () => wrap.remove();
  codeBlockEl.insertAdjacentElement('afterend', wrap);
}

// 언어별 미리보기 srcdoc 빌더
// 다중 파일 React 프로젝트를 가상 모듈 시스템으로 렌더링
// - .css 는 <style> 로 주입
// - .jsx/.tsx/.js/.ts 는 Babel 로 CommonJS 변환 후 require 체인으로 실행
// - import 'react'/'react-dom'/'react-dom/client' 는 내장 매핑
function buildReactMultiFilePreview(files, preferredEntry) {
  // 엔트리 후보: main.jsx → index.jsx → App.jsx 순
  const entryCandidates = [
    preferredEntry,
    'src/main.jsx', 'src/main.tsx', 'src/index.jsx', 'src/index.tsx',
    'main.jsx', 'main.tsx', 'index.jsx', 'index.tsx',
  ].filter(Boolean);

  let entry = null;
  let manualMount = false;
  for (const c of entryCandidates) {
    if (files[c]) { entry = c; break; }
  }
  // createRoot/ReactDOM.render 호출을 포함한 파일
  if (!entry) {
    for (const p in files) {
      if (/\.(jsx|tsx|js)$/i.test(p) && /createRoot|ReactDOM\.render/.test(files[p])) {
        entry = p; break;
      }
    }
  }
  // App.jsx 로 폴백 (수동 마운트)
  if (!entry) {
    for (const p in files) {
      if (/(^|\/)App\.(jsx|tsx)$/i.test(p)) { entry = p; manualMount = true; break; }
    }
  }
  if (!entry) {
    return `<!DOCTYPE html><html><body><div style="color:#c00;padding:16px;font-family:monospace">엔트리 파일을 찾을 수 없습니다. (main.jsx / index.jsx / App.jsx 중 하나가 필요)</div></body></html>`;
  }

  // HTML 파서가 스크립트 내 </... 를 태그 닫힘으로 오인하지 않도록 < 를 이스케이프
  const filesJson = JSON.stringify(files).replace(/</g, '\\u003c');
  return `<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>React Preview</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:16px;background:#fff;color:#111;}</style>
<script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"><\/script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"><\/script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"><\/script>
</head>
<body>
<div id="root"></div>
<script>
  const __files = ${filesJson};
  const __modules = {};
  const __reactExports = Object.assign({ default: React, __esModule: true }, React);
  const __reactDomExports = Object.assign({ default: ReactDOM, __esModule: true }, ReactDOM);
  const __builtins = {
    'react': __reactExports,
    'react-dom': __reactDomExports,
    'react-dom/client': __reactDomExports,
  };

  function resolvePath(from, to) {
    if (__builtins[to]) return to;
    const fromDir = from ? from.split('/').slice(0, -1).join('/') : '';
    let abs;
    if (to.startsWith('./') || to.startsWith('../')) {
      const parts = (fromDir ? fromDir.split('/') : []).concat(to.split('/'));
      const out = [];
      for (const p of parts) {
        if (p === '' || p === '.') continue;
        if (p === '..') out.pop();
        else out.push(p);
      }
      abs = out.join('/');
    } else {
      abs = to;
    }
    if (__files[abs]) return abs;
    const exts = ['.jsx', '.tsx', '.js', '.ts', '.css',
                  '/index.jsx', '/index.tsx', '/index.js', '/index.ts'];
    for (const ext of exts) {
      if (__files[abs + ext]) return abs + ext;
    }
    // 케이스 무시 매칭 (모델이 대소문자를 섞어 내는 경우 대비)
    const lower = abs.toLowerCase();
    for (const key of Object.keys(__files)) {
      if (key.toLowerCase() === lower) return key;
      for (const ext of exts) {
        if (key.toLowerCase() === lower + ext) return key;
      }
    }
    throw new Error('Module not found: ' + to + ' (from ' + (from || '<entry>') + ')');
  }

  function requireModule(request, fromPath) {
    const resolved = resolvePath(fromPath, request);
    if (__builtins[resolved]) return __builtins[resolved];
    if (__modules[resolved]) return __modules[resolved].exports;

    const src = __files[resolved];
    if (resolved.endsWith('.css')) {
      const style = document.createElement('style');
      style.textContent = src;
      document.head.appendChild(style);
      __modules[resolved] = { exports: {} };
      return __modules[resolved].exports;
    }

    const isTS = /\\.tsx?$/.test(resolved);
    const presets = isTS
      ? [['react'], ['typescript', { allExtensions: true, isTSX: true }]]
      : [['react']];
    let transformed;
    try {
      transformed = Babel.transform(src, {
        presets: presets,
        plugins: ['transform-modules-commonjs'],
        filename: resolved,
      }).code;
    } catch (e) {
      throw new Error('Babel 변환 실패 (' + resolved + '): ' + (e.message || e));
    }

    const mod = { exports: {} };
    __modules[resolved] = mod;
    try {
      const fn = new Function('require', 'module', 'exports', 'React', 'ReactDOM', transformed);
      fn(function(p) { return requireModule(p, resolved); }, mod, mod.exports, React, ReactDOM);
    } catch (e) {
      throw new Error('실행 오류 (' + resolved + '): ' + (e.message || e));
    }
    return mod.exports;
  }

  try {
    const entryExports = requireModule('./' + '${entry}', '');
    ${manualMount ? `
      const AppComp = entryExports.default || entryExports.App || entryExports;
      if (!AppComp) throw new Error('App 컴포넌트를 export 해주세요.');
      ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(AppComp));
    ` : '// entry 파일이 자체적으로 createRoot 호출'}
  } catch (e) {
    document.getElementById('root').innerHTML =
      '<pre style="color:#c00;white-space:pre-wrap;font-family:ui-monospace,monospace;padding:12px;background:#fff5f5;border:1px solid #fcc;border-radius:6px">' +
      (e && e.message ? e.message : String(e)) +
      (e && e.stack ? '\\n\\n' + e.stack : '') + '</pre>';
  }
<\/script>
</body>
</html>`;
}

function buildPreviewSrcdoc(code, lang) {
  const l = (lang || 'html').toLowerCase();
  if (l === 'html') return code;

  // Node.js / 서버 사이드 JS 감지 → 브라우저에서 실행 불가하므로 안내 화면
  const isNodeCode = l === 'javascript' && /(^|\n)\s*(const|let|var)\s+\w+\s*=\s*require\s*\(|module\.exports|process\.(env|argv)|app\.listen\s*\(|require\s*\(\s*['"]express['"]\s*\)/.test(code);
  if (isNodeCode) {
    const safeCode = (code || '').slice(0, 2000)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return `<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>Node.js Preview</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:linear-gradient(135deg,#1e1e2e 0%,#2d3748 100%); color:#e2e2f0; padding:24px; }
  .card { max-width:480px; text-align:center; }
  .icon { font-size:56px; margin-bottom:14px; }
  .title { font-size:20px; font-weight:700; margin-bottom:10px; }
  .desc { font-size:13px; color:#a0a0c0; line-height:1.7; margin-bottom:16px; }
  pre { text-align:left; background:rgba(0,0,0,0.35); padding:12px; border-radius:8px;
    font-size:11px; color:#c8c8e0; max-height:220px; overflow:auto;
    font-family:ui-monospace,Menlo,Consolas,monospace; }
</style></head><body><div class="card">
  <div class="icon">🖥️</div>
  <div class="title">Node.js 서버 코드</div>
  <div class="desc">서버 사이드(Node.js) 코드는 브라우저에서 직접 실행할 수 없습니다.<br>
  터미널에서 <code>node &lt;파일명&gt;</code> 으로 실행해 주세요.</div>
  <pre>${safeCode}${(code || '').length > 2000 ? '\n... (생략)' : ''}</pre>
</div></body></html>`;
  }

  if (l === 'jsx' || l === 'tsx' || l === 'javascript') {
    // 다중 파일이면 가상 모듈 시스템으로 실행 (Vite 스타일 프로젝트 지원)
    const files = currentPreviewFiles || {};
    const multiFile = Object.keys(files).filter(p => /\.(jsx|tsx|js|ts|css)$/i.test(p)).length >= 2;
    if (multiFile) {
      return buildReactMultiFilePreview(files, currentPreviewEntryPath);
    }

    // 단일 파일 폴백: import/export 제거 + React hooks 전역 노출
    const sanitized = code
      .replace(/^\s*import\s.+?;?\s*$/gm, '')
      .replace(/^\s*export\s+default\s+/gm, '')
      .replace(/^\s*export\s+/gm, '');
    const presets = (l === 'tsx') ? 'react,typescript' : 'react';
    return `<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>React Preview</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:0;padding:16px;background:#fff;color:#111;}</style>
<script crossorigin src="https://unpkg.com/react@18/umd/react.development.js"><\/script>
<script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.development.js"><\/script>
<script src="https://unpkg.com/@babel/standalone/babel.min.js"><\/script>
<script>
  // React hooks 와 자주 쓰는 API 를 전역으로 노출 — import 제거된 코드가 바로 쓰게.
  window.useState = React.useState;
  window.useEffect = React.useEffect;
  window.useRef = React.useRef;
  window.useMemo = React.useMemo;
  window.useCallback = React.useCallback;
  window.useContext = React.useContext;
  window.useReducer = React.useReducer;
  window.useLayoutEffect = React.useLayoutEffect;
  window.Fragment = React.Fragment;
  window.createContext = React.createContext;
<\/script>
</head>
<body>
<div id="root"></div>
<script type="text/babel" data-presets="${presets}">
try {
${sanitized}

  const __rootEl = document.getElementById('root');
  const __rootComp = (typeof App !== 'undefined' && App)
                  || (typeof Main !== 'undefined' && Main)
                  || (typeof Page !== 'undefined' && Page)
                  || (typeof Home !== 'undefined' && Home);
  if (!__rootComp) {
    __rootEl.innerHTML = '<div style="color:#c00;font-family:monospace;padding:12px;background:#fff5f5;border:1px solid #fcc;border-radius:6px">최상위 컴포넌트를 찾을 수 없습니다. 컴포넌트 이름을 <code>App</code>, <code>Main</code>, <code>Page</code>, <code>Home</code> 중 하나로 지정해 주세요.</div>';
  } else {
    ReactDOM.createRoot(__rootEl).render(React.createElement(__rootComp));
  }
} catch (e) {
  document.getElementById('root').innerHTML =
    '<pre style="color:#c00;white-space:pre-wrap;font-family:ui-monospace,monospace;padding:12px;background:#fff5f5;border:1px solid #fcc;border-radius:6px">런타임 오류: ' + (e && e.message ? e.message : e) + (e && e.stack ? '\\n\\n' + e.stack : '') + '</pre>';
}
<\/script>
</body>
</html>`;
  }

  if (l === 'dart') {
    // Flutter/Dart는 브라우저에서 직접 실행 불가 → 안내 화면
    const safeCode = (code || '').slice(0, 2000)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    return `<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><title>Flutter Preview</title>
<style>
  body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%); color:#e2e2f0; padding:24px; }
  .card { max-width:460px; text-align:center; }
  .icon { font-size:56px; margin-bottom:14px; }
  .title { font-size:20px; font-weight:700; margin-bottom:10px; }
  .desc { font-size:13px; color:#a0a0c0; line-height:1.7; margin-bottom:16px; }
  .desc a { color:#4f8cff; text-decoration:none; font-weight:600; }
  .desc a:hover { text-decoration:underline; }
  pre { text-align:left; background:rgba(0,0,0,0.35); padding:12px; border-radius:8px;
    font-size:11px; color:#c8c8e0; max-height:200px; overflow:auto;
    font-family:ui-monospace,Menlo,Consolas,monospace; }
</style></head>
<body>
  <div class="card">
    <div class="icon">📱</div>
    <div class="title">Flutter 코드 미리보기 불가</div>
    <div class="desc">
      Flutter(Dart) 코드는 브라우저에서 직접 실행할 수 없습니다.<br>
      <a href="https://dartpad.dev" target="_blank" rel="noopener">DartPad</a> 또는
      로컬 Flutter 환경(<code>flutter run</code>)에서 실행해 주세요.
    </div>
    <pre>${safeCode}${(code || '').length > 2000 ? '\n... (생략)' : ''}</pre>
  </div>
</body>
</html>`;
  }

  return code;
}

/* ════════════════════════════════════════════
 * Combined preview — 메시지 내 모든 파일을 한 화면에 합쳐서 미리보기
 * (pipelines/preview_builder.py 의 JS 포팅)
 * ════════════════════════════════════════════ */
const _HTML_EXTS = ['.html', '.htm'];
const _CSS_EXTS = ['.css'];
const _JS_EXTS = ['.js', '.mjs'];

function _hasExt(path, exts) {
  const p = (path || '').toLowerCase();
  return exts.some(e => p.endsWith(e));
}

function _pickHtmlEntry(files) {
  const names = Object.keys(files);
  for (const pref of ['index.html', 'index.htm']) {
    const hit = names.find(n => n.toLowerCase() === pref || n.toLowerCase().endsWith('/' + pref));
    if (hit) return hit;
  }
  return names.find(n => _hasExt(n, _HTML_EXTS)) || null;
}

function buildCombinedHtmlSrcdoc(files) {
  const entry = _pickHtmlEntry(files);
  if (!entry) return null;
  let html = files[entry];
  const used = new Set([entry]);

  // <link rel="stylesheet" href="X.css"> → <style>...</style>
  html = html.replace(
    /<link\b[^>]*?rel=["']stylesheet["'][^>]*?href=["']([^"']+)["'][^>]*?>/gi,
    (match, href) => {
      const key = Object.keys(files).find(
        k => k === href || k.endsWith('/' + href) || href.endsWith(k)
      );
      if (key && _hasExt(key, _CSS_EXTS)) {
        used.add(key);
        return `<style>\n${files[key]}\n</style>`;
      }
      return match;
    }
  );

  // <script src="X.js"><\/script> → <script>...<\/script>
  html = html.replace(
    /<script\b[^>]*?src=["']([^"']+)["'][^>]*?><\/script>/gi,
    (match, src) => {
      const key = Object.keys(files).find(
        k => k === src || k.endsWith('/' + src) || src.endsWith(k)
      );
      if (key && _hasExt(key, _JS_EXTS)) {
        used.add(key);
        return `<script>\n${files[key]}\n<\/script>`;
      }
      return match;
    }
  );

  // 참조되지 않은 CSS/JS 파일을 </body> 앞에 자동 주입
  const orphanStyle = Object.keys(files)
    .filter(k => !used.has(k) && _hasExt(k, _CSS_EXTS))
    .map(k => `<style>\n${files[k]}\n</style>`).join('\n');
  const orphanScript = Object.keys(files)
    .filter(k => !used.has(k) && _hasExt(k, _JS_EXTS))
    .map(k => `<script>\n${files[k]}\n<\/script>`).join('\n');
  const tail = orphanStyle + orphanScript;
  if (tail) {
    if (/<\/body>/i.test(html)) {
      html = html.replace(/<\/body>/i, tail + '</body>');
    } else {
      html += tail;
    }
  }
  return html;
}

function collectMessageFiles(contentEl) {
  const files = {};
  contentEl.querySelectorAll('.code-block').forEach(blk => {
    const path = blk.dataset.lang || '';
    const content = blk.dataset.code || '';
    if (path && content && path.includes('.')) {
      files[path] = content;
    }
  });
  return files;
}

function openCombinedPreview(files) {
  // React 파일이 섞여 있으면 React 빌더, 아니면 HTML 결합 빌더
  const hasReact = Object.keys(files).some(p => /\.(jsx|tsx)$/i.test(p));
  let srcdoc;
  let langLabel;
  if (hasReact) {
    srcdoc = buildReactMultiFilePreview(files, '');
    langLabel = 'jsx';
  } else {
    srcdoc = buildCombinedHtmlSrcdoc(files);
    if (!srcdoc) {
      toast && toast('미리보기를 만들 수 있는 HTML/JSX 파일이 없습니다');
      return;
    }
    langLabel = 'html';
  }
  currentPreviewCode = srcdoc;
  currentPreviewLang = langLabel;
  currentPreviewFiles = files;
  currentPreviewEntryPath = '';
  previewPanel.classList.add('open');
  // 위의 switchPreviewTab 은 buildPreviewSrcdoc 을 다시 호출하므로,
  // React 가 아닌 경우에는 이미 합쳐진 HTML 을 직접 iframe 에 넣는다.
  document.querySelectorAll('.preview-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === 'preview'));
  $('preview-frame').style.display = 'block';
  $('preview-code-view').style.display = 'none';
  $('preview-frame').srcdoc = srcdoc;
}

function maybeCreateCombinedPreviewButton(contentEl) {
  const files = collectMessageFiles(contentEl);
  const paths = Object.keys(files);
  if (paths.length < 2) return null;  // 파일이 1개면 기존 개별 미리보기로 충분
  const hasRenderable = paths.some(p =>
    /\.(html?|jsx|tsx)$/i.test(p));
  if (!hasRenderable) return null;

  const btn = document.createElement('button');
  btn.className = 'combined-preview-btn';
  btn.innerHTML = `
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
      <circle cx="12" cy="12" r="3"/>
    </svg>
    🖥️ 전체 미리보기 (${paths.length}개 파일)`;
  btn.onclick = () => openCombinedPreview(collectMessageFiles(contentEl));
  return btn;
}

function switchPreviewTab(tab) {
  document.querySelectorAll('.preview-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  if (tab === 'preview') {
    $('preview-frame').style.display = 'block';
    $('preview-code-view').style.display = 'none';
    $('preview-frame').srcdoc = buildPreviewSrcdoc(currentPreviewCode, currentPreviewLang);
  } else {
    $('preview-frame').style.display = 'none';
    $('preview-code-view').style.display = 'block';
    $('preview-code-view').textContent = currentPreviewCode;
  }
}

/* ════════════════════════════════════════════
 * Send / generate
 * ════════════════════════════════════════════ */
function sendMessage() {
  const text = chatInput.value.trim();
  if (!text || isGenerating) return;

  let chat = getCurrentChat();
  if (!chat) chat = createChat();
  const requestId = uid();
  const sentAt = Date.now();

  chat.messages.push(buildMessage('user', text, {
    requestId,
    attachments: attachments.slice(),
    timestamp: sentAt,
  }));

  if (chat.title === '새 채팅') {
    chat.title = text.slice(0, 30);
  }
  chat.updatedAt = sentAt;
  saveState();

  attachments = [];
  renderAttachments();
  chatInput.value = '';
  chatInput.style.height = 'auto';
  updateSendBtn();

  renderChatList();
  renderMessages();
  generateResponse();
}

/* ════════════════════════════════════════════
 * Loading bubble helpers
 * ════════════════════════════════════════════ */
function createLoadingBubble(_labelText) {
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.innerHTML =
    '<div class="assistant-content assistant-loading">' +
      '<span class="assistant-loading-text">' +
        '잘 생각하기' +
        '<span class="assistant-loading-dots"><span>.</span><span>.</span><span>.</span></span>' +
      '</span>' +
    '</div>';
  return div;
}

// 타이머/문구 순환 제거 — 인터벌 핸들 호환용 no-op
function startLoadingTimer(_loadingDiv, _startedAt) {
  return 0;
}

/* ── 스트리밍 버블: 청크가 도착할 때마다 점진적으로 렌더링 ── */
function createStreamingBubble() {
  const div = document.createElement('div');
  div.className = 'message assistant streaming';
  const content = document.createElement('div');
  content.className = 'assistant-content';
  div.appendChild(content);
  return { div, content };
}

function updateStreamingContent(contentEl, text) {
  // 미완성 코드펜스가 있으면 임시로 닫아서 렌더가 깨지지 않도록 한다
  const fenceCount = (text.match(/```/g) || []).length;
  const safeText = fenceCount % 2 === 1 ? text + '\n```' : text;

  // 재렌더 전에 현재 <details>의 열림 상태를 순서대로 보존 — 그렇지 않으면
  // 사용자가 "생성된 명세서" 같은 블록을 연 직후 다음 청크가 도착하면서
  // innerHTML 가 통째로 새로 쓰여 다시 닫혀버린다.
  const prevOpenStates = Array.from(contentEl.querySelectorAll('details')).map(d => d.open);

  const parsed = parseMarkdown(safeText);
  contentEl.innerHTML = parsed.html;
  parsed.codeBlocks.forEach((cb, i) => {
    const marker = contentEl.querySelector(`[data-code-marker="${i}"]`);
    const block = createCodeBlock(cb.code, cb.lang);
    if (marker) marker.replaceWith(block);
    else contentEl.appendChild(block);
  });

  // 열림 상태 복원
  contentEl.querySelectorAll('details').forEach((d, i) => {
    if (prevOpenStates[i]) d.open = true;
  });
  // 수식/Mermaid 는 스트리밍 중 렌더하지 않음 — 완성 후 renderMessage 단계에서 일괄 처리.
  // (스트림 청크마다 KaTeX 가 전체 트리를 스캔하면 O(N²) 비용 + 깜빡임 발생)
}

/* 스트리밍 청크마다 동기적으로 호출되면 비용이 누적되므로, requestAnimationFrame
 * 으로 코얼레싱해 한 프레임당 최대 1회만 실제 렌더가 일어나도록 한다. */
let _streamRafPending = false;
let _streamRafArgs = null;
function scheduleStreamUpdate(contentEl, text) {
  _streamRafArgs = { contentEl, text };
  if (_streamRafPending) return;
  _streamRafPending = true;
  requestAnimationFrame(() => {
    _streamRafPending = false;
    const args = _streamRafArgs;
    _streamRafArgs = null;
    if (args) updateStreamingContent(args.contentEl, args.text);
  });
}

/* ── 서버가 보내는 상태 프레임(\x01STATUS:...\x02) 처리 ──
 * 들어오는 바이트 스트림에서 제어 프레임만 뽑아 로딩 라벨을 갱신하고,
 * 나머지는 그대로 반환한다. 프레임이 중간에 끊긴 경우(열림표식만 받고
 * 닫힘표식이 안 옴)는 다음 청크가 올 때까지 pending 에 남겨둔다.
 *
 * 반환: { content: string, pending: string } */
function extractStatusFrames(pending, onStatus) {
  let content = '';
  let rest = pending;
  while (true) {
    const begin = rest.indexOf('\x01');
    if (begin === -1) {
      content += rest;
      rest = '';
      break;
    }
    content += rest.slice(0, begin);
    const end = rest.indexOf('\x02', begin + 1);
    if (end === -1) {
      // 아직 닫히지 않은 프레임 — 다음 청크와 합쳐서 다시 파싱
      rest = rest.slice(begin);
      break;
    }
    const frame = rest.slice(begin + 1, end);
    if (frame.startsWith('STATUS:') && typeof onStatus === 'function') {
      onStatus(frame.slice(7));
    }
    // 알 수 없는 프레임은 조용히 무시
    rest = rest.slice(end + 1);
  }
  return { content, pending: rest };
}

function updateLoadingLabel(_loadingDiv, _text) {
  // 서버의 상태 프레임(명세서 작성 중 / 코드 생성 중 NNN자 누적 등)은
  // heartbeat 목적으로만 수신하고 UI 라벨은 항상 "잘 생각하기" 로 고정.
}

/* ════════════════════════════════════════════
 * Spec confirmation UI (확인 후 진행 모드)
 * — 문서 스타일 편집 카드
 * ════════════════════════════════════════════ */
function askSpecConfirmation(spec) {
  return new Promise((resolve) => {
    const card = document.createElement('div');
    card.className = 'message assistant';
    card.appendChild(buildSpecDoc(spec, {
      onCancel: () => { card.remove(); resolve(null); },
      onApprove: (updated) => { card.remove(); resolve(updated); },
    }));
    messagesEl.appendChild(card);
    scrollToBottom();
  });
}

function buildSpecDoc(spec, { onCancel, onApprove }) {
  // 안전 기본값
  const project = spec.project || {};
  const features = Array.isArray(spec.features) ? spec.features : [];
  const files = Array.isArray(spec.files) ? spec.files : [];
  const components = Array.isArray(spec.components) ? spec.components : [];
  const apiList = Array.isArray(spec.api) ? spec.api : [];
  const constraints = Array.isArray(spec.constraints) ? spec.constraints : [];
  const userStory = typeof spec.user_story === 'string' ? spec.user_story : '';

  const doc = document.createElement('div');
  doc.className = 'assistant-content';
  doc.innerHTML = `
    <div class="spec-doc">
      <div class="spec-doc-header">
        <div class="spec-doc-title">📋 명세서 검토</div>
        <div class="spec-doc-subtitle">필요한 부분을 직접 수정한 뒤 확정해 주세요. 항목 위에 마우스를 올리면 × 삭제 버튼이 나타납니다.</div>
      </div>

      <section class="spec-section">
        <h4>프로젝트</h4>
        <div class="spec-field">
          <span class="spec-field-label">이름</span>
          <div class="spec-field-value"><input class="spec-input" data-path="project.name"></div>
        </div>
        <div class="spec-field">
          <span class="spec-field-label">타입</span>
          <div class="spec-field-value"><input class="spec-input" data-path="project.type"></div>
        </div>
        <div class="spec-field">
          <span class="spec-field-label">범위</span>
          <div class="spec-field-value"><input class="spec-input" data-path="project.scope"></div>
        </div>
        <div class="spec-field">
          <span class="spec-field-label">기술 스택</span>
          <div class="spec-field-value"><input class="spec-input" data-path="project.tech_stack" data-array="true" placeholder="쉼표로 구분"></div>
        </div>
        <div class="spec-field">
          <span class="spec-field-label">설명</span>
          <div class="spec-field-value"><textarea class="spec-textarea" rows="2" data-path="project.description"></textarea></div>
        </div>
      </section>

      <section class="spec-section">
        <h4>기능 (이 항목이 모두 구현되어야 통과)</h4>
        <div class="spec-features-body" data-list="features"></div>
        <button class="spec-add-btn" data-add="features">＋ 기능 추가</button>
      </section>

      <section class="spec-section">
        <h4>파일</h4>
        <ul class="spec-list" data-list="files"></ul>
        <button class="spec-add-btn" data-add="files">＋ 파일 추가</button>
      </section>

      <section class="spec-section" data-section="components">
        <h4>컴포넌트</h4>
        <div class="spec-components-body"></div>
      </section>

      <section class="spec-section" data-section="api">
        <h4>API</h4>
        <div class="spec-api-body"></div>
      </section>

      <section class="spec-section">
        <h4>제약 사항</h4>
        <ul class="spec-list" data-list="constraints"></ul>
        <button class="spec-add-btn" data-add="constraints">＋ 제약 추가</button>
      </section>

      <section class="spec-section">
        <h4>사용자 스토리</h4>
        <textarea class="spec-textarea" rows="2" data-path="user_story"></textarea>
      </section>

      <div class="spec-doc-actions">
        <button class="spec-cancel-btn">취소</button>
        <button class="primary spec-approve-btn">확정하고 코드 생성</button>
      </div>
    </div>
  `;

  // ── 값 채우기 ─────────────────────────────────────────────
  const setFieldValue = (path, value) => {
    const el = doc.querySelector(`[data-path="${path}"]`);
    if (!el) return;
    if (el.dataset.array === 'true') {
      el.value = Array.isArray(value) ? value.join(', ') : (value || '');
    } else {
      el.value = value == null ? '' : String(value);
    }
  };
  setFieldValue('project.name', project.name);
  setFieldValue('project.type', project.type);
  setFieldValue('project.scope', project.scope);
  setFieldValue('project.tech_stack', project.tech_stack);
  setFieldValue('project.description', project.description);
  setFieldValue('user_story', userStory);

  // 파일 리스트
  const filesUl = doc.querySelector('[data-list="files"]');
  const addFileRow = (file = { path: '', role: '' }) => {
    const li = document.createElement('li');
    li.className = 'spec-list-item';
    li.innerHTML = `
      <input class="spec-input spec-item-path" placeholder="파일 경로">
      <span class="spec-item-sep">—</span>
      <input class="spec-input spec-item-role" placeholder="역할 설명">
      <button class="spec-remove-btn" title="삭제">×</button>
    `;
    li.querySelector('.spec-item-path').value = file.path || '';
    li.querySelector('.spec-item-role').value = file.role || '';
    li.querySelector('.spec-remove-btn').onclick = () => li.remove();
    filesUl.appendChild(li);
  };
  files.forEach(addFileRow);
  doc.querySelector('[data-add="files"]').onclick = () => addFileRow();

  // 기능 리스트
  const featuresBody = doc.querySelector('[data-list="features"]');
  let featureAutoId = 1;
  const addFeatureRow = (feat = null) => {
    const f = feat || { id: '', description: '', acceptance_criteria: [] };
    if (!f.id) f.id = `F${featureAutoId}`;
    featureAutoId += 1;
    const criteriaText = Array.isArray(f.acceptance_criteria) ? f.acceptance_criteria.join('\n') : '';
    const item = document.createElement('div');
    item.className = 'spec-feature-item';
    item.innerHTML = `
      <div class="spec-feature-head">
        <input class="spec-input spec-feature-id" placeholder="ID (예: F1)" style="width:80px">
        <input class="spec-input spec-feature-desc" placeholder="이 기능이 무엇을 하는지">
        <button class="spec-remove-btn" title="삭제">×</button>
      </div>
      <textarea class="spec-textarea spec-feature-criteria" rows="3" placeholder="인수 기준 (한 줄에 하나씩)"></textarea>
    `;
    item.querySelector('.spec-feature-id').value = f.id || '';
    item.querySelector('.spec-feature-desc').value = f.description || '';
    item.querySelector('.spec-feature-criteria').value = criteriaText;
    item.querySelector('.spec-remove-btn').onclick = () => item.remove();
    featuresBody.appendChild(item);
  };
  features.forEach(addFeatureRow);
  doc.querySelector('[data-add="features"]').onclick = () => addFeatureRow();

  // 제약사항 리스트
  const constraintsUl = doc.querySelector('[data-list="constraints"]');
  const addConstraintRow = (text = '') => {
    const li = document.createElement('li');
    li.className = 'spec-list-item';
    li.innerHTML = `
      <input class="spec-input" style="flex:1" placeholder="제약 조건">
      <button class="spec-remove-btn" title="삭제">×</button>
    `;
    li.querySelector('input').value = text;
    li.querySelector('.spec-remove-btn').onclick = () => li.remove();
    constraintsUl.appendChild(li);
  };
  constraints.forEach(addConstraintRow);
  doc.querySelector('[data-add="constraints"]').onclick = () => addConstraintRow();

  // 컴포넌트 (readonly 문서 표시)
  const compBody = doc.querySelector('.spec-components-body');
  if (components.length === 0) {
    compBody.innerHTML = '<div class="spec-readonly-item" style="color:var(--text3)">없음</div>';
  } else {
    components.forEach(c => {
      const item = document.createElement('div');
      item.className = 'spec-readonly-item';
      const props = Array.isArray(c.props) ? c.props.join(', ') : (c.props || '');
      item.innerHTML = `
        <div class="spec-readonly-name">${escapeHtml(c.name || '(이름 없음)')}</div>
        ${props ? `<div class="spec-readonly-meta">props: ${escapeHtml(props)}</div>` : ''}
        ${c.behavior ? `<div class="spec-readonly-meta">${escapeHtml(c.behavior)}</div>` : ''}
      `;
      compBody.appendChild(item);
    });
  }

  // API (readonly 문서 표시)
  const apiBody = doc.querySelector('.spec-api-body');
  if (apiList.length === 0) {
    apiBody.innerHTML = '<div class="spec-readonly-item" style="color:var(--text3)">없음</div>';
  } else {
    apiList.forEach(a => {
      const item = document.createElement('div');
      item.className = 'spec-readonly-item';
      item.innerHTML = `<div class="spec-readonly-meta">${escapeHtml(JSON.stringify(a))}</div>`;
      apiBody.appendChild(item);
    });
  }

  // ── 버튼 바인딩 ──────────────────────────────────────────
  doc.querySelector('.spec-cancel-btn').onclick = () => onCancel();
  doc.querySelector('.spec-approve-btn').onclick = () => {
    try {
      onApprove(readSpecDoc(doc, spec));
    } catch (e) {
      toast('명세 저장 실패: ' + e.message, 'error');
    }
  };

  return doc;
}

function readSpecDoc(doc, originalSpec) {
  // 원본을 깊은 복사한 뒤 편집 가능 필드만 덮어씁니다.
  const result = JSON.parse(JSON.stringify(originalSpec || {}));
  result.project = result.project || {};

  doc.querySelectorAll('[data-path]').forEach(el => {
    const path = el.dataset.path;
    const parts = path.split('.');
    let value;
    if (el.dataset.array === 'true') {
      value = (el.value || '').split(',').map(s => s.trim()).filter(Boolean);
    } else {
      value = el.value;
    }
    if (parts.length === 1) {
      result[parts[0]] = value;
    } else {
      result[parts[0]] = result[parts[0]] || {};
      result[parts[0]][parts[1]] = value;
    }
  });

  // 파일 리스트
  const filesUl = doc.querySelector('[data-list="files"]');
  if (filesUl) {
    result.files = Array.from(filesUl.querySelectorAll('.spec-list-item')).map(li => ({
      path: li.querySelector('.spec-item-path').value.trim(),
      role: li.querySelector('.spec-item-role').value.trim(),
    })).filter(f => f.path);
  }

  // 기능 리스트
  const featuresBody = doc.querySelector('[data-list="features"]');
  if (featuresBody) {
    result.features = Array.from(featuresBody.querySelectorAll('.spec-feature-item')).map(item => ({
      id: item.querySelector('.spec-feature-id').value.trim(),
      description: item.querySelector('.spec-feature-desc').value.trim(),
      acceptance_criteria: item.querySelector('.spec-feature-criteria').value
        .split('\n').map(s => s.trim()).filter(Boolean),
    })).filter(f => f.id && f.description);
  }

  // 제약 리스트
  const constraintsUl = doc.querySelector('[data-list="constraints"]');
  if (constraintsUl) {
    result.constraints = Array.from(constraintsUl.querySelectorAll('.spec-list-item input'))
      .map(i => i.value.trim()).filter(Boolean);
  }

  // 필수 필드 보강 (백엔드 _validate_spec 통과)
  result.components = result.components || [];
  result.api = result.api || [];
  result.user_story = result.user_story || '';
  result.features = result.features || [];
  return result;
}

async function generateResponse() {
  const chat = getCurrentChat();
  if (!chat) return;
  isGenerating = true;
  currentAbortController = new AbortController();
  updateSendBtn();

  const lastUser = [...chat.messages].reverse().find(m => m.role === 'user');
  const userText = lastUser ? lastUser.content : '';
  if (lastUser && !lastUser.requestId) lastUser.requestId = uid();
  const contextMessages = buildApiMessages(chat);

  // 로딩 표시 (우측 끝에 실시간 경과 시간 + 파도타기 점)
  const loadingDiv = createLoadingBubble('응답을 생성하고 있습니다');
  messagesEl.appendChild(loadingDiv);
  scrollToBottom(true);

  let fullContent = '';
  const responseStartedAt = Date.now();
  const loadingTimerId = startLoadingTimer(loadingDiv, responseStartedAt);

  // 실행 모드: 'confirm' → 1차 명세 → 사용자 확인 → 코드 스트림
  //             'auto'    → 기존 통합 /api/chat 엔드포인트로 바로 진행
  const runMode = ($('run-mode-select') && $('run-mode-select').value) || 'confirm';

  try {
    if (runMode === 'confirm') {
      // ── 1단계: 분류 + (코딩이면) 명세 생성, (일반 질문이면) 바로 응답 ──
      const specRes = await fetch('/api/chat/spec', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        signal: currentAbortController.signal,
        body: JSON.stringify({
          message: userText,
          chat_id: chat?.id || null,
          messages: contextMessages,
          attachments: (lastUser && lastUser.attachments) || [],
        }),
      });
      if (specRes.status === 401) { logout(); throw new Error('unauthorized'); }
      if (!specRes.ok) {
        const errText = await specRes.text().catch(() => '');
        throw new Error(`요청 처리 실패 (${specRes.status}): ${errText}`);
      }
      const specData = await specRes.json();

      // 일반 질문: 명세/확인 단계 건너뛰고 바로 응답 표시
      if (specData.mode === 'chat') {
        fullContent = specData.reply || '';
        // 공통 처리(로딩 제거, 메시지 추가)로 폴백
      } else {
        // 로딩 정리 및 사용자 확인 UI 삽입
        clearInterval(loadingTimerId);
        loadingDiv.remove();

        const approved = await askSpecConfirmation(specData.spec);
      if (!approved) {
        // 사용자 취소 → 취소 메시지만 남기고 종료
        const cancelMsg = buildMessage('assistant', '🛑 사용자가 명세 확인 단계에서 취소했습니다.', {
          requestId: lastUser ? lastUser.requestId : uid(),
          timestamp: Date.now(),
          durationMs: Date.now() - responseStartedAt,
        });
        chat.messages.push(cancelMsg);
        renderMessages();
        scrollToBottom();
        chat.updatedAt = Date.now();
        saveState();
        isGenerating = false;
        updateSendBtn();
        return;
      }

      // ── 2단계: 코드 생성 스트림 ────────────────────────
      const loadingDiv2 = createLoadingBubble('코드 생성 중');
      messagesEl.appendChild(loadingDiv2);
      scrollToBottom(true);
      const codeStartedAt = Date.now();
      const codeTimerId = startLoadingTimer(loadingDiv2, codeStartedAt);

      let streamBubble2 = null;
      try {
        const codeRes = await fetch('/api/chat/code', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          signal: currentAbortController.signal,
          body: JSON.stringify({
            message: userText,
            chat_id: chat?.id || null,
            spec: approved,
            messages: contextMessages,
          }),
        });
        if (codeRes.status === 401) { logout(); throw new Error('unauthorized'); }
        if (!codeRes.ok) {
          const errText = await codeRes.text().catch(() => '');
          throw new Error(`코드 생성 실패 (${codeRes.status}): ${errText}`);
        }
        const reader = codeRes.body.getReader();
        const decoder = new TextDecoder();
        let framePending = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          framePending += decoder.decode(value, { stream: true });
          const { content, pending } = extractStatusFrames(framePending, (status) => {
            updateLoadingLabel(loadingDiv2, status);
          });
          framePending = pending;
          if (!content) continue;
          fullContent += content;
          if (!streamBubble2) {
            loadingDiv2.remove();
            streamBubble2 = createStreamingBubble();
            messagesEl.appendChild(streamBubble2.div);
          }
          scheduleStreamUpdate(streamBubble2.content, fullContent);
          scrollToBottom();
        }
      } finally {
        clearInterval(codeTimerId);
        if (loadingDiv2.parentNode) loadingDiv2.remove();
        if (streamBubble2 && streamBubble2.div.parentNode) streamBubble2.div.remove();
      }

      // 명세서 미리보기를 답변 맨 앞에 붙여서 기록
      const specHeader =
        '<details><summary>📋 확정된 명세서 (클릭하여 펼치기)</summary>\n\n' +
        '```json\n' + JSON.stringify(approved, null, 2) + '\n```\n\n</details>\n\n';
      fullContent = specHeader + fullContent;
      }
    } else {
      // ── 자동 모드: 기존 통합 엔드포인트 ──────────────────
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        signal: currentAbortController.signal,
        body: JSON.stringify({
          message: userText,
          chat_id: chat?.id || null,
          messages: contextMessages,
          attachments: (lastUser && lastUser.attachments) || [],
        }),
      });
      if (res.status === 401) { logout(); throw new Error('unauthorized'); }
      if (!res.ok) {
        const errText = await res.text().catch(() => '');
        throw new Error(`서버 오류 (${res.status}): ${errText}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let streamBubble = null;
      let framePending = '';
      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          framePending += decoder.decode(value, { stream: true });
          const { content, pending } = extractStatusFrames(framePending, (status) => {
            updateLoadingLabel(loadingDiv, status);
          });
          framePending = pending;
          if (!content) continue;
          fullContent += content;
          if (!streamBubble) {
            if (loadingDiv.parentNode) loadingDiv.remove();
            streamBubble = createStreamingBubble();
            messagesEl.appendChild(streamBubble.div);
          }
          scheduleStreamUpdate(streamBubble.content, fullContent);
          scrollToBottom();
        }
      } finally {
        if (streamBubble && streamBubble.div.parentNode) streamBubble.div.remove();
      }
    }
  } catch (err) {
    if (err.name === 'AbortError' || /aborted/i.test(err.message || '')) {
      fullContent = (fullContent || '') + `\n\n🛑 사용자가 응답을 중단했습니다.`;
    } else if (err.message !== 'unauthorized') {
      fullContent = (fullContent || '') + `\n\n⚠️ ${err.message}`;
    }
  }

  // 로딩 제거 후 완성된 메시지를 한번에 표시 (confirm 모드는 이미 정리된 상태)
  clearInterval(loadingTimerId);
  if (loadingDiv.parentNode) loadingDiv.remove();
  const finishedAt = Date.now();
  const assistantMsg = buildMessage('assistant', fullContent, {
    requestId: lastUser ? lastUser.requestId : uid(),
    timestamp: finishedAt,
    durationMs: Math.max(0, finishedAt - responseStartedAt),
  });
  chat.messages.push(assistantMsg);
  renderMessages();
  scrollToBottom();

  chat.updatedAt = finishedAt;
  saveState();
  isGenerating = false;
  currentAbortController = null;
  updateSendBtn();
  if (STATE.settings.autoplay && assistantMsg.content) speak(assistantMsg.content);
}

function stopGeneration() {
  if (currentAbortController) {
    try { currentAbortController.abort(); } catch (_) {}
  }
}


/* ════════════════════════════════════════════
 * Voice (TTS / STT)
 * ════════════════════════════════════════════ */
function speak(text, btn) {
  if (currentSpeech) {
    speechSynthesis.cancel();
    currentSpeech = null;
    document.querySelectorAll('.msg-action-btn.active').forEach(b => b.classList.remove('active'));
    return;
  }
  if (!('speechSynthesis' in window)) { toast('음성 합성이 지원되지 않습니다', 'error'); return; }
  // Strip markdown and code blocks
  const clean = text.replace(/```[\s\S]*?```/g, '').replace(/[*#`>_-]/g, '');
  const utter = new SpeechSynthesisUtterance(clean);
  const voices = speechSynthesis.getVoices();
  const v = voices.find(v => v.name === STATE.settings.ttsVoice) || voices.find(v => v.lang.startsWith('ko'));
  if (v) utter.voice = v;
  utter.onend = () => { currentSpeech = null; if (btn) btn.classList.remove('active'); };
  utter.onerror = () => { currentSpeech = null; if (btn) btn.classList.remove('active'); };
  currentSpeech = utter;
  if (btn) btn.classList.add('active');
  speechSynthesis.speak(utter);
}


/* ════════════════════════════════════════════
 * Attachments
 * ════════════════════════════════════════════ */
function handleFiles(files) {
  Array.from(files).forEach(f => {
    const reader = new FileReader();
    reader.onload = () => {
      attachments.push({ name: f.name, type: f.type, data: reader.result });
      renderAttachments();
    };
    reader.readAsDataURL(f);
  });
}

function renderAttachments() {
  $('attachment-preview').innerHTML = attachments.map((a, i) =>
    `<div class="attachment-preview-item">
      ${a.type.startsWith('image/') ? `<img src="${a.data}">` : `<div class="attachment-name">${escapeHtml(a.name.slice(0,12))}</div>`}
      <button class="remove-attach" onclick="removeAttachment(${i})">×</button>
    </div>`
  ).join('');
}
function removeAttachment(i) { attachments.splice(i, 1); renderAttachments(); }

/* ════════════════════════════════════════════
 * Modals
 * ════════════════════════════════════════════ */
function openModal(id) {
  $(id).classList.add('open');
  if (id === 'settings-modal') populateSettings();
  if (id === 'notes-modal') $('notes-textarea').value = STATE.notes;
  if (id === 'model-modal') populateModels();
}
function closeModal(id) { $(id).classList.remove('open'); }

function populateSettings() {
  $('theme-select').value = STATE.settings.theme || 'system';
  loadMemoryFromServer().then(renderMemoryList);
}

function switchSettingsTab(tab) {
  document.querySelectorAll('.settings-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.settings-section').forEach(s => s.classList.toggle('active', s.id === `section-${tab}`));
}

async function loadMemoryFromServer() {
  if (!isLoggedIn()) { memoryItems = []; return; }
  try {
    const res = await fetch('/api/memory', { headers: authHeaders() });
    if (!res.ok) { memoryItems = []; return; }
    const data = await res.json();
    memoryItems = Array.isArray(data.items) ? data.items : [];
  } catch { memoryItems = []; }
}

function renderMemoryList() {
  const wrap = $('memory-list');
  if (!wrap) return;
  wrap.innerHTML = '';
  memoryItems.forEach((text, i) => {
    const row = document.createElement('div');
    row.className = 'memory-item';
    row.innerHTML = `<input type="text" value="${escapeHtml(text)}"><button title="삭제">×</button>`;
    row.querySelector('input').oninput = (e) => { memoryItems[i] = e.target.value; };
    row.querySelector('button').onclick = () => { memoryItems.splice(i, 1); renderMemoryList(); };
    wrap.appendChild(row);
  });
  if (!memoryItems.length) {
    wrap.innerHTML = '<div style="color: var(--text3); font-size: 12px; padding: 6px 0;">아직 기억된 항목이 없습니다.</div>';
  }
}

async function saveMemoryToServer() {
  const items = memoryItems.map(s => (s || '').trim()).filter(Boolean);
  try {
    const res = await fetch('/api/memory', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ items }),
    });
    if (!res.ok) throw new Error('저장 실패');
    memoryItems = items;
    renderMemoryList();
    toast('기억 저장됨');
  } catch (e) { toast('기억 저장 실패: ' + e.message, 'error'); }
}

function populateModels() {
  const chat = getCurrentChat();
  const current = chat ? chat.model : draftModel;
  $('model-list').innerHTML = MODELS.map(m =>
    `<div class="prompt-card${m.id===current ? ' selected' : ''}" onclick="selectModel('${m.id}')">
      <div class="prompt-card-title">${m.name} ${m.id===current?'✓':''}</div>
      <div class="prompt-card-desc">${m.desc}</div>
    </div>`
  ).join('');
}
function selectModel(id) {
  const chat = getCurrentChat();
  if (chat) { chat.model = id; saveState(); }
  else { draftModel = id; }
  updateModelChip();
  closeModal('model-modal');
  toast(`모델 변경: ${MODELS.find(m=>m.id===id).name}`);
}
function updateModelChip() {
  const el = $('model-chip-name');
  if (!el) return;
  const chat = getCurrentChat();
  const id = chat ? chat.model : draftModel;
  const model = MODELS.find(m => m.id === id);
  el.textContent = model ? model.name : id;
}

function saveNotes() {
  STATE.notes = $('notes-textarea').value;
  saveState();
  closeModal('notes-modal');
  toast('노트 저장됨');
}

/* ════════════════════════════════════════════
 * Settings handlers
 * ════════════════════════════════════════════ */
function resolveTheme(pref) {
  if (pref === 'system') {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
      ? 'light'
      : 'dark';
  }
  return pref === 'light' ? 'light' : 'dark';
}
function applyTheme() {
  const effective = resolveTheme(STATE.settings.theme);
  document.documentElement.classList.toggle('light', effective === 'light');
}
// 시스템 설정 변경 시 자동 반영 ('system' 선택 시에만)
if (window.matchMedia) {
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (STATE.settings.theme === 'system') applyTheme();
  });
}
function applyFontSize() {
  document.body.style.fontSize = STATE.settings.fontSize + 'px';
}

/* ════════════════════════════════════════════
 * Context menu
 * ════════════════════════════════════════════ */
function showChatContextMenu(e, chatId) {
  const chat = STATE.chats.find(c => c.id === chatId);
  if (!chat) return;
  const menu = $('context-menu');
  menu.innerHTML = `
    <button class="context-menu-item" onclick="togglePin('${chatId}'); closeContextMenu()">
      ${ICONS.copy.replace('copy', '')}
      ${chat.pinned ? '고정 해제' : '고정'}
    </button>
    <button class="context-menu-item" onclick="promptRename('${chatId}'); closeContextMenu()">
      ${ICONS.edit}
      이름 변경
    </button>
    <button class="context-menu-item" onclick="exportChat('${chatId}'); closeContextMenu()">
      ${ICONS.copy}
      내보내기 (.md)
    </button>
    <div class="context-menu-divider"></div>
    <button class="context-menu-item danger" onclick="if(confirm('정말 삭제하시겠습니까?'))deleteChat('${chatId}'); closeContextMenu()">
      ${ICONS.trash}
      삭제
    </button>`;
  menu.style.left = Math.min(e.clientX, window.innerWidth - 180) + 'px';
  menu.style.top = Math.min(e.clientY, window.innerHeight - 200) + 'px';
  menu.classList.add('open');
}
function closeContextMenu() { $('context-menu').classList.remove('open'); }

function promptRename(id) {
  const chat = STATE.chats.find(c => c.id === id);
  const name = prompt('새 이름:', chat.title);
  if (name) renameChat(id, name);
}

/* ════════════════════════════════════════════
 * Utils
 * ════════════════════════════════════════════ */
function escapeHtml(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#x27;'); }
function copyText(t) { navigator.clipboard.writeText(t); }
function scrollToBottom(force = false) {
  const c = $('chat-container');
  if (!c) return;
  // 사용자가 위로 스크롤해서 읽고 있는 중이면 강제로 끌어내리지 않는다.
  // (force=true 인 호출 — 새 메시지 전송 등 — 은 항상 바닥으로 이동)
  const distance = c.scrollHeight - c.scrollTop - c.clientHeight;
  if (force || distance < 120) {
    c.scrollTop = c.scrollHeight;
  }
}
function download(content, name, type) {
  const blob = new Blob([content], { type });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}
function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  $('toast-container').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 2200);
}
function exportAllChats() {
  download(JSON.stringify({ chats: STATE.chats, exportedAt: Date.now() }, null, 2), 'vibe-chats.json', 'application/json');
  toast('내보내기 완료');
}
const SEND_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M2 21L23 12 2 3v7l15 2-15 2v7z"/></svg>`;
const STOP_ICON = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1"/></svg>`;

function updateSendBtn() {
  if (isGenerating) {
    sendBtn.disabled = false;
    sendBtn.classList.add('stop');
    sendBtn.classList.remove('active');
    sendBtn.title = '응답 중단';
    sendBtn.innerHTML = STOP_ICON;
    return;
  }
  sendBtn.classList.remove('stop');
  sendBtn.title = '전송 (Enter)';
  sendBtn.innerHTML = SEND_ICON;
  const has = chatInput.value.trim() !== '' || attachments.length > 0;
  sendBtn.disabled = !has;
  sendBtn.classList.toggle('active', has);
}

/* ════════════════════════════════════════════
 * Event listeners
 * ════════════════════════════════════════════ */
/* ════════════════════════════════════════════
 * Auth
 * ════════════════════════════════════════════ */
function authHeaders() {
  const t = localStorage.getItem('vibe_auth_token');
  return t ? { Authorization: `Bearer ${t}` } : {};
}

function isLoggedIn() {
  return !!localStorage.getItem('vibe_auth_token');
}

function hideLogin() {
  $('login-overlay').classList.add('hidden');
}

async function logout() {
  try {
    await fetch('/api/logout', { method: 'POST', headers: authHeaders() });
  } catch {}
  localStorage.removeItem('vibe_auth_token');
  localStorage.removeItem('vibe_auth_user');
  resetState();
  renderChatList();
  renderMessages();
  showLogin();
}

function showSignup() {
  $('login-overlay').classList.remove('hidden');
  $('login-form').classList.add('is-hidden');
  $('signup-form').classList.remove('is-hidden');
  $('signup-error').textContent = '';
  $('signup-username').focus();
}
function showLogin() {
  $('login-overlay').classList.remove('hidden');
  $('signup-form').classList.add('is-hidden');
  $('login-form').classList.remove('is-hidden');
  $('login-error').textContent = '';
  $('login-username').focus();
}

async function handleLogin(e) {
  e.preventDefault();
  const username = $('login-username').value.trim();
  const password = $('login-password').value;
  const errEl = $('login-error');
  const btn = $('login-submit');
  errEl.textContent = '';
  btn.disabled = true;
  btn.textContent = '로그인 중...';
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || '로그인 실패');
    }
    const data = await res.json();
    localStorage.setItem('vibe_auth_token', data.token);
    localStorage.setItem('vibe_auth_user', data.username);
    $('login-password').value = '';
    hideLogin();
    resetState();
    loadState();
    await loadStateFromServer();
    // 로그인 직후에는 마지막 대화를 이어서 열지 않고 기본 화면(웰컴)으로 시작
    STATE.currentChatId = null;
    saveStateLocal();
    renderChatList();
    renderMessages();
  } catch (err) {
    errEl.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '로그인';
  }
}

async function handleSignup(e) {
  e.preventDefault();
  const username = $('signup-username').value.trim();
  const password = $('signup-password').value;
  const password2 = $('signup-password2').value;
  const errEl = $('signup-error');
  const btn = $('signup-submit');
  errEl.textContent = '';
  if (password !== password2) { errEl.textContent = '비밀번호가 일치하지 않습니다'; return; }
  btn.disabled = true;
  btn.textContent = '가입 중...';
  try {
    const res = await fetch('/api/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || '회원가입 실패');
    }
    toast('회원가입 완료! 로그인해주세요');
    $('signup-username').value = '';
    $('signup-password').value = '';
    $('signup-password2').value = '';
    showLogin();
    $('login-username').value = username;
  } catch (err) {
    errEl.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = '회원가입';
  }
}

function init() {
  loadState();
  applyTheme();
  applyFontSize();
  renderChatList();
  renderMessages();
  updateModelChip();

  // Auth wiring — submit 는 폼의 인라인 onsubmit 에서 처리 (JS init 타이밍 무관하게 URL 유출 방지)
  $('logout-btn').onclick = logout;
  if (isLoggedIn()) { hideLogin(); loadStateFromServer(); } else showLogin();

  $('settings-btn').onclick = () => openModal('settings-modal');
  $('toggle-sidebar').onclick = () => sidebar.classList.toggle('collapsed');
  $('new-chat-btn').onclick = () => { createChat(); chatInput.focus(); };
  $('search-input').oninput = (e) => renderChatList(e.target.value);

  chatInput.addEventListener('input', () => {
    chatInput.style.height = 'auto';
    chatInput.style.height = Math.min(chatInput.scrollHeight, 200) + 'px';
    updateSendBtn();
  });
  // 한글 IME 조합 상태를 직접 추적 (Safari 는 isComposing/keyCode=229 동작이 불안정)
  let isComposingInput = false;
  chatInput.addEventListener('compositionstart', () => { isComposingInput = true; });
  chatInput.addEventListener('compositionend', () => { isComposingInput = false; });
  chatInput.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.shiftKey) return;
    // 조합 중 Enter 는 확정용 — 전송하지 않음
    if (isComposingInput || e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    sendMessage();
  });
  sendBtn.onclick = () => { if (isGenerating) stopGeneration(); else sendMessage(); };

  $('attach-btn').onclick = () => $('file-input').click();
  $('file-input').onchange = (e) => handleFiles(e.target.files);
  // 실행 모드 선택 기억
  if (STATE.settings.runMode) $('run-mode-select').value = STATE.settings.runMode;
  $('run-mode-select').onchange = (e) => { STATE.settings.runMode = e.target.value; saveState(); };

  // Settings
  $('theme-select').onchange = (e) => { STATE.settings.theme = e.target.value; saveState(); applyTheme(); };
  $('memory-add-btn').onclick = () => { memoryItems.push(''); renderMemoryList(); const inputs = $('memory-list').querySelectorAll('input'); if (inputs.length) inputs[inputs.length - 1].focus(); };
  $('memory-save-btn').onclick = saveMemoryToServer;

  // Mobile sidebar overlay
  const backdrop = $('sidebar-backdrop');
  const openMobileSidebar = () => { sidebar.classList.add('mobile-open'); backdrop.classList.add('show'); };
  const closeMobileSidebar = () => { sidebar.classList.remove('mobile-open'); backdrop.classList.remove('show'); };
  $('mobile-menu-btn').onclick = openMobileSidebar;
  backdrop.onclick = closeMobileSidebar;
  // 채팅 클릭 시 모바일에서는 자동으로 닫음
  document.addEventListener('click', (e) => {
    if (window.innerWidth <= 768 && e.target.closest('.chat-item, #new-chat-btn')) closeMobileSidebar();
  });

  // Drag & drop files
  document.addEventListener('dragover', (e) => e.preventDefault());
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files);
  });

  // Click outside context menu
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.context-menu') && !e.target.closest('.chat-item-menu')) closeContextMenu();
  });
  // Close modals on backdrop click
  document.querySelectorAll('.modal-backdrop').forEach(m => {
    m.onclick = (e) => { if (e.target === m) m.classList.remove('open'); };
  });

  // Keyboard shortcuts
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'n') { e.preventDefault(); createChat(); chatInput.focus(); }
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); $('search-input').focus(); sidebar.classList.remove('collapsed'); }
    if (e.key === 'Escape') {
      document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open'));
      closeContextMenu();
      if (previewPanel.classList.contains('open')) closePreview();
    }
  });

  // Load TTS voices
  if ('speechSynthesis' in window) {
    speechSynthesis.onvoiceschanged = () => { if ($('settings-modal').classList.contains('open')) populateSettings(); };
  }
}

function useSuggestion(text) {
  chatInput.value = text;
  chatInput.dispatchEvent(new Event('input'));
  sendMessage();
}

init();
