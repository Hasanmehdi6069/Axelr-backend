// ============================================================
// AXELR AI - FRONTEND v24.3 (PRODUCTION-READY)
// ============================================================

// ============================================================
// CONFIGURATION
// ============================================================
const API_BASE_URL = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1"
    ? "http://localhost:8000"
    : "https://axelr-backend.onrender.com";

const GOOGLE_CLIENT_ID = "474929925590-kfpurq4aou35pkscf6gbr963vf4hfa7g.apps.googleusercontent.com";

const AXELR_AVATAR_SVG =
    `<svg viewBox="0 0 100 100" width="22" height="22" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M50 15 L20 32.5 L20 67.5 L50 85" stroke="#ffffff" stroke-width="6" stroke-linejoin="bevel" fill="rgba(255,255,255,0.05)"/><path d="M50 15 L80 32.5 L50 50 L80 67.5 L50 85" stroke="currentColor" stroke-width="6" stroke-linejoin="bevel" fill="none"/><path d="M20 32.5 L50 50 L20 67.5" stroke="#ffffff" stroke-width="3" stroke-linejoin="bevel" opacity="0.5"/></svg>`;

const ICONS = {
    copy: `<span class="material-symbols-rounded" style="font-size:16px;">content_copy</span>`,
    edit: `<span class="material-symbols-rounded" style="font-size:16px;">edit</span>`,
    thumbsUp: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>`,
    thumbsDown: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V6h-11.28a2 2 0 0 0-2 1.7L6.34 17a2 2 0 0 0 2 2.3H10z"/><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"/></svg>`,
    regenerate: `<span class="material-symbols-rounded" style="font-size:16px;">refresh</span>`,
    stop: `<span class="material-symbols-rounded" style="font-size:16px;">stop</span>`,
    moreVertical: `<span class="material-symbols-rounded" style="font-size:18px;">more_vert</span>`,
    leftArrow: `<span class="material-symbols-rounded" style="font-size:16px;">chevron_left</span>`,
    rightArrow: `<span class="material-symbols-rounded" style="font-size:16px;">chevron_right</span>`,
};

const SIDEBAR_ICONS = {
    push_pin: `<span class="material-symbols-rounded" style="font-size:14px;">push_pin</span>`,
    edit: `<span class="material-symbols-rounded" style="font-size:14px;">edit</span>`,
    link: `<span class="material-symbols-rounded" style="font-size:14px;">link</span>`,
    inventory: `<span class="material-symbols-rounded" style="font-size:14px;">inventory</span>`,
    delete: `<span class="material-symbols-rounded" style="font-size:14px;">delete</span>`,
    undo: `<span class="material-symbols-rounded" style="font-size:14px;">undo</span>`,
    restore: `<span class="material-symbols-rounded" style="font-size:14px;">restore</span>`,
    delete_forever: `<span class="material-symbols-rounded" style="font-size:14px;">delete_forever</span>`,
};

// ============================================================
// TOKEN MANAGEMENT
// ============================================================
let googleAuthUserToken = localStorage.getItem('google_auth_token') || null;
let tokenRefreshPromise = null;

function decodeJwt(token) {
    try {
        const base64Url = token.split('.')[1];
        const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
        return JSON.parse(decodeURIComponent(atob(base64).split('').map(c => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)).join('')));
    } catch {
        return null;
    }
}

function getTokenExpiry(token) {
    const payload = decodeJwt(token);
    return payload ? payload.exp * 1000 : 0;
}

function refreshGoogleToken() {
    if (tokenRefreshPromise) return tokenRefreshPromise;
    tokenRefreshPromise = new Promise((resolve, reject) => {
        if (typeof google === 'undefined' || !google.accounts || !google.accounts.oauth2) {
            reject(new Error('Google Identity Services not loaded'));
            return;
        }
        const client = google.accounts.oauth2.initTokenClient({
            client_id: GOOGLE_CLIENT_ID,
            scope: 'profile email',
            callback: (resp) => {
                if (resp.error) {
                    reject(new Error(resp.error));
                } else {
                    resolve(resp.access_token);
                }
            }
        });
        client.requestAccessToken({ prompt: 'none' });
    });
    tokenRefreshPromise.finally(() => { tokenRefreshPromise = null; });
    return tokenRefreshPromise;
}

async function ensureValidToken() {
    let token = googleAuthUserToken;
    if (!token) {
        token = localStorage.getItem('google_auth_token');
        if (token) googleAuthUserToken = token;
    }
    if (!token) throw new Error('No token available');

    const expiry = getTokenExpiry(token);
    const now = Date.now();
    if (expiry - now < 5 * 60 * 1000) {
        try {
            const newToken = await refreshGoogleToken();
            googleAuthUserToken = newToken;
            localStorage.setItem('google_auth_token', newToken);
            return newToken;
        } catch (e) {
            console.warn('Token refresh failed:', e);
            localStorage.removeItem('google_auth_token');
            googleAuthUserToken = null;
            throw new Error('Session expired. Please sign in again.');
        }
    }
    return token;
}
async function apiFetch(url, options = {}) {
    const maxRetries = 3;
    let lastError;
    for (let attempt = 0; attempt < maxRetries; attempt++) {
        try {
            if (url.includes('/api/guest/') || url.includes('/api/auth/github') || url.includes('/api/auth/webauthn')) {
                return fetch(url, options);
            }
            let token = await ensureValidToken();
            options.headers = {
                ...options.headers,
                'Authorization': `Bearer ${token}`
            };
            const response = await fetch(url, options);
            if (response.status === 401) {
                try {
                    const newToken = await refreshGoogleToken();
                    googleAuthUserToken = newToken;
                    localStorage.setItem('google_auth_token', newToken);
                    options.headers['Authorization'] = `Bearer ${newToken}`;
                    const retryResponse = await fetch(url, options);
                    if (retryResponse.status === 401) {
                        throw new Error('Unauthorized');
                    }
                    return retryResponse;
                } catch (refreshError) {
                    executeGlobalLogout();
                    throw new Error('Session expired');
                }
            }
            return response;
        } catch (error) {
            lastError = error;
            if (error.message === 'Session expired' || error.message === 'Unauthorized') {
                executeGlobalLogout();
                throw error;
            }
            // Only retry on network errors (not 4xx/5xx)
            if (error instanceof TypeError && error.message.includes('network')) {
                await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
                continue;
            }
            throw error;
        }
    }
    throw lastError;
}
setInterval(async () => {
    try {
        await ensureValidToken();
    } catch (_) { /* silent */ }
}, 10 * 60 * 1000);

// ============================================================
// DOM REFS
// ============================================================
function getEl(id) {
    const el = document.getElementById(id);
    return el;
}
const promptInput = getEl('prompt-input');
const fileInput = getEl('omni-file-input');
const fileStagingContainer = getEl('file-staging-container');
const viewport = getEl('viewport');
const historyListContainer = getEl('history-list-container');
const accountDropdownCard = getEl('account-dropdown-card');
const modelDropdownCard = getEl('model-dropdown-card');
const sidebarTriggerArea = getEl('sidebar-trigger-area');
const sidebarNode = getEl('sidebar-container-node');
const sendBtn = getEl('send-trigger');
const commandWrapper = getEl('command-wrapper');
const mainWrapper = getEl('content-mask');
const authWall = getEl('auth-wall');
const heroDisplay = getEl('hero-display');
const mainBackBtn = getEl('main-back-btn');

// ============================================================
// STATE
// ============================================================
let stagedFiles = [];
let cachedLogHistory = [];
let activeSessionId = null;
let runningStructuredCache = null;
let runningFileTitle = 'Export.csv';
let isListeningForVocal = false;
let currentTab = 'active';
let isInitialAppLoad = true;
let globalAbortController = null;
let lastUserCommand = '';
let regenerateTimer = null;
let hasRegenerated = false;
let currentUserId = null;
let isProcessing = false;
let isUserScrolling = false;
let scrollTimeout = null;
let viewportObserver = null;
let observerActive = true;
let ignoreSidebarClose = false;
let manipulationCount = parseInt(sessionStorage.getItem('axelr_manipulation_count')) || 0;
let manipulationLockUntil = parseInt(sessionStorage.getItem('axelr_manipulation_lock')) || 0;
let suppressRegenerateForNextResponse = false;
let appInitialized = false;
let isGuestMode = false;
let guestSessionId = null;
let streamingBubble = null;
let streamingContentDiv = null;

window.addEventListener('unhandledrejection', (event) => {
    console.error('Unhandled rejection:', event.reason);
    showToast('An unexpected error occurred. Please refresh.', 'error');
    event.preventDefault();
});
// ============================================================
// WORKSPACE THEME
// ============================================================
function updateWorkspaceTheme(workspace) {
    document.body.classList.remove('workspace-data', 'workspace-design', 'workspace-general');
    if (workspace === 'design') {
        document.body.classList.add('workspace-design');
    } else if (workspace === 'general') {
        document.body.classList.add('workspace-general');
    } else {
        document.body.classList.add('workspace-data');
    }
    localStorage.setItem('Axelr_workspace', workspace);
    const isMobile = window.innerWidth <= 768;
    const logo = getEl('sidebar-logo-text');
    const heroTitle = getEl('hero-title-text');
    const heroSub = getEl('hero-sub-text');
    if (workspace === 'design') {
        if (logo) logo.innerText = 'AXELR DESIGN';
        if (heroTitle) heroTitle.innerText = 'What are we designing today?';
        if (heroSub) heroSub.innerText = 'AI-powered UI/UX generation & live deployment.';
        if (promptInput) promptInput.placeholder = isMobile ? "Upload a mockup..." : "Upload a mockup or request a UI component...";
    } else if (workspace === 'general') {
    if (logo) logo.innerText = 'AXELR';
    if (heroTitle) heroTitle.innerText = 'What can I help you with?';
    if (heroSub) heroSub.innerText = 'Intelligence execution for any task – from code to creativity.';
    if (promptInput) promptInput.placeholder = isMobile ? "Ask anything..." : "Ask me anything – I\'m here to help...";
    } else {
        if (logo) logo.innerText = 'AXELR DATA';
        if (heroTitle) heroTitle.innerText = 'What are we building today?';
        if (heroSub) heroSub.innerText = 'AI-powered architecture and data execution.';
        if (promptInput) promptInput.placeholder = isMobile ? "Upload a receipt..." : "Upload a receipt, invoice, or CSV for extraction...";
    }
    // Update model branding
    updateModelBranding(workspace, window.currentUser?.tier || 'free');
    updateFeaturesMenu(workspace);
}
let eli5Active = false;
document.getElementById('eli5-toggle')?.addEventListener('click', function() {
    eli5Active = !eli5Active;
    this.classList.toggle('active');
    this.innerHTML = eli5Active 
        ? '<span class="material-symbols-rounded">child_care</span> ON' 
        : '<span class="material-symbols-rounded">child_care</span>';
    showToast(eli5Active ? 'ELI5 mode ON – responses will be simplified.' : 'ELI5 mode OFF', 'info');
});

async function brainstorm(topic) {
    const resp = await apiFetch(`${API_BASE_URL}/api/brainstorm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: topic })
    });
    const data = await resp.json();
    if (data.success) {
        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble nexus-bubble';
        bubble.innerHTML = `<div class="bubble-content">${DOMPurify.sanitize(marked.parse(data.ideas))}</div>`;
        viewport.appendChild(bubble);
        scrollToBottom();
    }
}
document.getElementById('multi-agent-btn')?.addEventListener('click', function() {
    const task = prompt('Enter the task for the agents:');
    if (!task) return;
    const agents = [
        { name: 'Researcher', role: 'research' },
        { name: 'Coder', role: 'code' },
        { name: 'Reviewer', role: 'review' }
    ];
    runMultiAgent(task, agents);
});
let currentPeriod = 'monthly';
function setPricingPeriod(period) {
    currentPeriod = period;
    document.querySelectorAll('.period-btn').forEach(b => b.classList.toggle('active', b.dataset.period === period));
    // Update prices in the upgrade cards
    document.querySelectorAll('.price-output').forEach(el => {
        const monthly = parseFloat(el.dataset.monthly);
        const annual = monthly * 10; // 20% off = 10 months for price of 12
        el.innerHTML = period === 'monthly' ? `$${monthly}<span>/mo</span>` : `$${annual}<span>/yr</span>`;
    });
}
// Multi-Agent
function createNexusBubble(markdown) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble nexus-bubble';
    const avatar = document.createElement('div');
    avatar.className = 'ai-avatar-bubble';
    avatar.innerHTML = AXELR_AVATAR_SVG;
    const content = document.createElement('div');
    content.className = 'bubble-content';
    content.innerHTML = DOMPurify.sanitize(marked.parse(markdown || ''));
    bubble.append(avatar, content);
    return bubble;
}

async function runMultiAgent(task, agents) {
    const btn = document.getElementById('multi-agent-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = 'Spawning...'; }
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/agents/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task, agents, workspace: getWorkspace() })
        });
        const data = await resp.json();
        if (data.success) {
            const bubble = createNexusBubble(data.combined);
            viewport.appendChild(bubble);
            scrollToBottom();
        } else {
            showToast('Agent error: ' + (data.message || 'Unknown'), 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '🧠 Agents'; }
    }
}

// Knowledge Vault
async function saveKnowledge(key, value, tags = []) {
    await apiFetch(`${API_BASE_URL}/api/knowledge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key, value, tags })
    });
    showToast('Knowledge saved', 'success');
}
async function loadKnowledgeList() { /* ... */ }
async function deleteKnowledge(id) { /* ... */ }

// Workflow
async function runWorkflow(steps) {
    const response = await fetch(`${API_BASE_URL}/api/workflow/run`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${await ensureValidToken()}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ steps, workspace: getWorkspace() })
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        for (let i = 0; i < events.length - 1; i++) {
            const event = events[i];
            if (event.startsWith('data: ')) {
                try {
                    const json = JSON.parse(event.slice(6));
                    if (json.status === 'completed' && json.output) {
                        const bubble = createNexusBubble(`**${json.step}**\n\n${json.output}`);
                        viewport.appendChild(bubble);
                        scrollToBottom();
                    } else if (json.status === 'done') {
                        const bubble = createNexusBubble(`**✅ Workflow Complete**\n\n${json.final}`);
                        viewport.appendChild(bubble);
                        scrollToBottom();
                    }
                } catch (e) { /* ignore */ }
            }
        }
        buffer = events[events.length - 1];
    }
}

// Persona
async function applyPersona(personaId) {
    const resp = await apiFetch(`${API_BASE_URL}/api/personas/${personaId}`);
    const data = await resp.json();
    if (data.system_prompt) {
        await apiFetch(`${API_BASE_URL}/api/user/instructions`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ instructions: data.system_prompt })
        });
        showToast('Persona applied!', 'success');
        await loadUserProfile();
        closeModals();
    }
}

// Code Execution
async function executeCodeBlock(btn, language) {
    const pre = btn.closest('pre');
    const code = pre ? pre.querySelector('code') : null;
    if (!code) return;
    const codeText = code.innerText;
    btn.innerText = 'Running…';
    btn.disabled = true;
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/execute-code`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language, code: codeText })
        });
        const data = await resp.json();
        const output = data.output || data.error || 'No output';
        const bubble = createNexusBubble(`**▶️ Output**\n\n\`\`\`\n${output}\n\`\`\``);
        viewport.appendChild(bubble);
        scrollToBottom();
    } catch (e) {
        showToast('Execution error: ' + e.message, 'error');
    } finally {
        btn.innerText = '▶ Run';
        btn.disabled = false;
    }
}
// ============================================================
// MODEL BRANDING & DROPDOWN
// ============================================================
// ============================================================
// MODEL BRANDING & DROPDOWN – Per Workspace
// ============================================================
// ============================================================
// MODEL BRANDING & DROPDOWN – Per Workspace
// ============================================================
// Fetch model config from backend
let MODEL_CONFIG = { general: { models: [] }, data: { models: [] }, design: { models: [] } };

async function loadModelConfig() {
    try {
        const resp = await fetch(`${API_BASE_URL}/api/model-config`);
        if (resp.ok) {
            MODEL_CONFIG = await resp.json();
            // Ensure all workspaces exist
            ['general', 'data', 'design'].forEach(w => {
                if (!MODEL_CONFIG[w]) MODEL_CONFIG[w] = { models: [] };
            });
        }
    } catch(e) {
        console.warn('Using fallback model config');
        // Fallback config (production ready)
        MODEL_CONFIG = {
            general: {
                models: [
                    { id: 'flash', label: 'AXELR‑FLASH', badge: 'FREE', desc: 'Instant answers for everyday questions', tier: 'free' },
                    { id: 'pro', label: 'AXELR‑HYPER', badge: 'HYPER', desc: 'Deep reasoning & code generation', tier: 'pro' },
                    { id: 'business', label: 'AXELR‑OMNI', badge: 'OMNI', desc: 'Unlimited context & multi‑agent orchestration', tier: 'business' }
                ]
            },
            data: {
                models: [
                    { id: 'flash', label: 'AXELR‑FLASH', badge: 'DATA', desc: 'Lightning‑fast extractions & analysis', tier: 'free' },
                    { id: 'pro', label: 'AXELR‑PRO DATA', badge: 'PRO', desc: 'Advanced extraction with higher limits', tier: 'pro' },
                    { id: 'business', label: 'AXELR‑ENTERPRISE', badge: 'ENTERPRISE', desc: 'Massive throughput & custom pipelines', tier: 'business' }
                ]
            },
            design: {
                models: [
                    { id: 'flash', label: 'AXELR‑ARCHITECT', badge: 'BUILDER', desc: 'Instant UI/UX components', tier: 'free' },
                    { id: 'pro', label: 'AXELR‑STUDIO', badge: 'PRO', desc: 'Complex interactions & design systems', tier: 'pro' },
                    { id: 'business', label: 'AXELR‑DESIGN OPS', badge: 'DESIGN OPS', desc: 'Team‑scale design & deployment', tier: 'business' }
                ]
            }
        };
    }
}

function renderModelDropdown(workspace) {
    const container = document.getElementById('model-dropdown-card');
    if (!container) return;
    const config = MODEL_CONFIG[workspace] || MODEL_CONFIG.general;
    const selectedId = localStorage.getItem('axelr_selected_model') || config.models[0]?.id || 'flash';

    container.innerHTML = config.models.map((m) => {
        const activeClass = (m.id === selectedId) ? 'active' : '';
        let tierClass = '';
        if (m.tier === 'pro') tierClass = 'pro';
        else if (m.tier === 'business') tierClass = 'designer';
        return `
            <div class="model-option ${activeClass} ${tierClass}" data-model-id="${m.id}" onclick="selectModel(event, '${m.id}')">
                <div class="model-title">${m.label} <span style="background:rgba(0,242,254,0.1);color:var(--accent-glow);padding:2px 8px;border-radius:4px;font-size:9px;">${m.badge}</span></div>
                <div class="model-desc">${m.desc}</div>
            </div>
        `;
    }).join('');

    // Update the selected model label
    const selectedModel = config.models.find(m => m.id === selectedId);
    if (selectedModel) {
        document.getElementById('model-text-display').innerText = selectedModel.label;
        document.getElementById('model-badge-display').innerText = selectedModel.badge;
    }
}

function updateModelBranding(workspace, tier) {
    const config = MODEL_CONFIG[workspace] || MODEL_CONFIG.general;
    // Determine best matching model based on tier
    let defaultModel = config.models.find(m => m.tier === tier) || config.models[0];
    if (!defaultModel) defaultModel = config.models[0];
    // Store selected model id
    const selectedId = defaultModel.id;
    localStorage.setItem('axelr_selected_model', selectedId);
    // Update UI
    document.getElementById('model-text-display').innerText = defaultModel.label;
    document.getElementById('model-badge-display').innerText = defaultModel.badge;
    renderModelDropdown(workspace);
}

function selectModel(e, modelId) {
    if (e) e.stopPropagation();
    const workspace = getWorkspace();
    const config = MODEL_CONFIG[workspace] || MODEL_CONFIG.general;
    const model = config.models.find(m => m.id === modelId);
    if (model) {
        document.getElementById('model-text-display').innerText = model.label;
        document.getElementById('model-badge-display').innerText = model.badge;
        localStorage.setItem('axelr_selected_model', modelId);
        renderModelDropdown(workspace);
    }
    document.getElementById('model-dropdown-card').style.display = 'none';
}

// Call this after loading profile and on workspace switch
// Also call loadModelConfig() during initial app initialization
// ============================================================
// SCROLL FUNCTIONS
// ============================================================
function scrollToBottom(smooth = true) {
    if (!viewport || isUserScrolling) return;
    const lastMessage = viewport.querySelector('.chat-bubble:last-child');
    if (lastMessage) {
        try {
            lastMessage.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'end' });
        } catch (_) {
            viewport.scrollTop = viewport.scrollHeight;
        }
    }
}

function updateViewportAfterRender() {
    if (window._viewportUpdateId) cancelAnimationFrame(window._viewportUpdateId);
    window._viewportUpdateId = requestAnimationFrame(() => {
        if (!isUserScrolling) scrollToBottom(true);
        adjustViewportPadding();
        window._viewportUpdateId = null;
    });
}

// ============================================================
// UTILITY FUNCTIONS
// ============================================================
function escapeHtmlEntities(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

function extractHtmlCode(text) {
    if (!text) return null;
    const htmlBlockRegex = /```html\s*([\s\S]*?)```/i;
    let match = text.match(htmlBlockRegex);
    if (match) return match[1].trim();
    const genericBlockRegex = /```(?:\w+)?\s*([\s\S]*?)```/g;
    let block;
    while ((block = genericBlockRegex.exec(text)) !== null) {
        const content = block[1].trim();
        if (content.startsWith("<") && (content.includes("<html") || content.includes("<!DOCTYPE") || content.includes("<div") || content.includes("<body"))) {
            return content;
        }
    }
    const htmlTagRegex = /(<!DOCTYPE html>|<html>|<html[\s\S]*?>)/i;
    const startMatch = text.match(htmlTagRegex);
    if (startMatch) {
        const startIndex = startMatch.index;
        const endMatch = text.match(/<\/html>/i);
        if (endMatch) {
            const endIndex = endMatch.index + endMatch[0].length;
            return text.substring(startIndex, endIndex).trim();
        } else {
            return text.substring(startIndex).trim();
        }
    }
    return null;
}

function getWorkspace() {
    return localStorage.getItem('Axelr_workspace') || 'data';
}

function getDraftKey() {
    return `Axelr_prompt_draft_${currentUserId || 'anonymous'}`;
}
async function getRelevantKnowledge(query) {
    if (!query || query.length < 3) return '';
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/knowledge/search?q=${encodeURIComponent(query)}`);
        const data = await resp.json();
        if (data.results && data.results.length > 0) {
            return '📚 Relevant knowledge:\n' + data.results.map(k => `${k.key}: ${k.value}`).join('\n');
        }
    } catch (e) {
        console.warn('Knowledge search failed:', e);
    }
    return '';
}
// ============================================================
// QUOTA DISPLAY
// ============================================================
function getDailyLimit(tier, subTierOptions, workspace) {
    const hasData = subTierOptions?.hasDataAccess || false;
    const hasDesign = subTierOptions?.hasDesignAccess || false;
    const isDesign = workspace === 'design';
    if (tier === 'free' || tier === 'guest') {
        return isDesign ? 3 : 5;
    } else if (tier === 'pro') {
        if (hasData && hasDesign) return isDesign ? 15 : 20;
        if (hasData) return isDesign ? 0 : 19;
        if (hasDesign) return isDesign ? 13 : 0;
        return 0;
    } else if (tier === 'business') {
        if (hasData && hasDesign) return isDesign ? 25 : 30;
        if (hasData) return isDesign ? 0 : 28;
        if (hasDesign) return isDesign ? 20 : 0;
        return 0;
    }
    return isDesign ? 3 : 5;
}

function updateQuotaDisplay(data) {
    const workspace = getWorkspace();
    const limit = getDailyLimit(data.tier, data.subTierOptions, workspace);
    let used = 0;
    if (workspace === 'design') {
        used = data.quotas?.dailyGenerationsUsed || 0;
    } else {
        used = data.quotas?.dailyExtractionsUsed || 0;
    }
    const percentage = limit > 0 ? Math.min((used / limit) * 100, 100) : 0;
    const quotaCount = getEl('quota-numerical-count');
    const quotaFill = getEl('quota-progress-bar-fill');
    if (quotaCount) quotaCount.innerText = `${used}/${limit} Used (${Math.round(percentage)}%)`;
    if (quotaFill) quotaFill.style.width = `${percentage}%`;
    const settingsCount = getEl('settings-quota-count');
    const settingsFill = getEl('settings-quota-fill');
    if (settingsCount && quotaCount) settingsCount.innerText = quotaCount.innerText;
    if (settingsFill && quotaFill) settingsFill.style.width = quotaFill.style.width;
}

// ============================================================
// THEME FUNCTIONS
// ============================================================
let currentThemePreference = localStorage.getItem('axelr_theme') || 'system';
let systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;

function applyTheme(theme) {
    if (theme === 'dark') {
        document.body.classList.remove('light-theme');
        document.body.classList.add('dark-theme');
    } else {
        document.body.classList.remove('dark-theme');
        document.body.classList.add('light-theme');
    }
    document.querySelectorAll('.theme-option').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === currentThemePreference);
    });
    const desc = getEl('theme-desc');
    if (desc) {
        if (currentThemePreference === 'system') {
            desc.textContent = `Using system (${systemDark ? 'Dark' : 'Light'})`;
        } else {
            desc.textContent = currentThemePreference.charAt(0).toUpperCase() + currentThemePreference.slice(1);
        }
    }
}

function setTheme(pref) {
    currentThemePreference = pref;
    localStorage.setItem('axelr_theme', pref);
    if (pref === 'system') {
        systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        applyTheme(systemDark ? 'dark' : 'light');
    } else {
        applyTheme(pref);
    }
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', (e) => {
    systemDark = e.matches;
    if (currentThemePreference === 'system') applyTheme(systemDark ? 'dark' : 'light');
});
// ============================================================
// PRODUCTION GLOBAL TOAST SYSTEM (PORTAL-SAFE)
// ============================================================
// ============================================================
// PRODUCTION GLOBAL TOAST SYSTEM (PORTAL-SAFE)
// ============================================================
function showToast(message, type = 'error') {
    let container = document.getElementById('global-toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'global-toast-container';
        container.className = 'toast-container';
        document.documentElement.appendChild(container); // Mount to html root to isolate from body flex layout
    }

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;

    const iconName = type === 'success' ? 'check_circle' : (type === 'info' ? 'info' : 'warning');

    toast.innerHTML = `
        <span class="material-symbols-rounded toast-icon">${iconName}</span>
        <span class="toast-message">${escapeHtmlEntities(message || 'An unexpected error occurred.')}</span>
        <button class="toast-close" title="Dismiss">&times;</button>
    `;

    const closeBtn = toast.querySelector('.toast-close');
    const dismiss = () => {
        toast.classList.remove('toast-show');
        setTimeout(() => toast.remove(), 250);
    };

    closeBtn.onclick = (e) => {
        e.stopPropagation();
        dismiss();
    };

    container.appendChild(toast);

    requestAnimationFrame(() => {
        toast.classList.add('toast-show');
    });

    setTimeout(dismiss, 5000);
}
function showMainUI() {
    const authWallEl = document.getElementById('auth-wall');
    const mainWrapperEl = document.getElementById('content-mask');
    
    if (authWallEl) {
        authWallEl.style.display = 'none';
    }
    if (mainWrapperEl) {
        mainWrapperEl.classList.add('visible');
        mainWrapperEl.style.display = 'flex'; // MUST be flex, not block
    }
    
    const hero = document.getElementById('hero-display');
    if (hero && !document.querySelector('.chat-bubble')) {
        hero.style.display = 'flex';
    }
    
    const wsSel = getEl('workspace-selector');
    if (wsSel) wsSel.style.display = 'none';

    // Refresh model branding and features for active workspace
    const ws = getWorkspace();
    updateModelBranding(ws, window.currentUser?.tier || 'free');
    updateFeaturesMenu();
    console.log('✅ Main UI unlocked and visible');
}

function showAuthWall() {
    if (authWall) authWall.style.display = 'flex';
    if (mainWrapper) {
        mainWrapper.classList.remove('visible');
        mainWrapper.style.display = 'none';
    }
    console.log('🔒 Auth wall shown');
    const banner = getEl('guest-banner');
    if (banner) banner.style.display = 'none';
}
function showToast(message, type = 'error') {
    // Remove existing toasts
    const existing = document.querySelector('.toast-container');
    if (existing) existing.remove();

    const container = document.createElement('div');
    container.className = 'toast-container';
    container.style.cssText = `
        position: fixed;
        top: 20px;
        left: 50%;
        transform: translateX(-50%);
        z-index: 999999;
        max-width: 90%;
        pointer-events: none;
    `;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    toast.style.cssText = `
        display: inline-block;
        padding: 14px 28px;
        border-radius: 12px;
        background: var(--bg-card);
        color: var(--text-main);
        border: 1px solid var(--border-muted);
        box-shadow: 0 12px 40px rgba(0,0,0,0.5);
        font-weight: 500;
        font-size: 14px;
        backdrop-filter: blur(12px);
        transform: translateY(-20px);
        opacity: 0;
        transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.3s ease;
        pointer-events: auto;
    `;
    // Color overrides per type
    if (type === 'success') {
        toast.style.borderColor = 'rgba(16,185,129,0.3)';
        toast.style.background = 'rgba(16,185,129,0.15)';
        toast.style.color = '#10b981';
    } else if (type === 'error') {
        toast.style.borderColor = 'rgba(239,68,68,0.3)';
        toast.style.background = 'rgba(239,68,68,0.15)';
        toast.style.color = '#fca5a5';
    } else if (type === 'info') {
        toast.style.borderColor = 'rgba(59,130,246,0.3)';
        toast.style.background = 'rgba(59,130,246,0.15)';
        toast.style.color = '#93c5fd';
    } else if (type === 'warning') {
        toast.style.borderColor = 'rgba(234,179,8,0.3)';
        toast.style.background = 'rgba(234,179,8,0.15)';
        toast.style.color = '#fde047';
    }

    container.appendChild(toast);
    document.body.appendChild(container);

    // Animate in
    requestAnimationFrame(() => {
        toast.style.transform = 'translateY(0)';
        toast.style.opacity = '1';
    });

    // Auto‑dismiss after 4.5 seconds
    const timer = setTimeout(() => {
        toast.style.transform = 'translateY(-20px)';
        toast.style.opacity = '0';
        setTimeout(() => {
            if (container.parentNode) container.remove();
        }, 300);
    }, 4500);

    // Allow click to dismiss immediately
    toast.addEventListener('click', () => {
        clearTimeout(timer);
        toast.style.transform = 'translateY(-20px)';
        toast.style.opacity = '0';
        setTimeout(() => {
            if (container.parentNode) container.remove();
        }, 300);
    });
}
// ============================================================
// GUEST MODE INIT
// ============================================================
async function initGuestMode() {
    if (localStorage.getItem('google_auth_token')) {
        isGuestMode = false;
        return;
    }
    isGuestMode = true;
    const banner = getEl('guest-banner');
    if (banner) banner.style.display = 'block';
    const bannerText = getEl('guest-banner-text');
    if (bannerText) {
        bannerText.innerHTML = `
            You are in <strong style="color:#fde047;">Explorer Mode</strong> — your conversations are ephemeral and will not be saved.
            <a href="#" onclick="showAuthWall(); return false;" style="color:var(--accent-glow); text-decoration:none; font-weight:600;">Sign in</a> to unlock persistent memory, unlimited generations, and premium AI models.
        `;
    }
    try {
        const resp = await fetch(`${API_BASE_URL}/api/guest/session`, { method: 'POST' });
        const data = await resp.json();
        guestSessionId = data.sessionId;
        console.log('🟢 Guest session created:', guestSessionId);
    } catch (e) {
        console.warn('Guest session creation failed:', e);
    }
    updateQuotaDisplay({ tier: 'guest', subTierOptions: { hasDataAccess: false, hasDesignAccess: false }, quotas: { dailyExtractionsUsed: 0, dailyGenerationsUsed: 0 } });
    updateSidebarForGuest(true);
}

function updateSidebarForGuest(isGuest) {
    const container = getEl('sidebar-settings-or-login');
    const title = getEl('settings-or-login-title');
    const desc = getEl('settings-or-login-desc');
    if (!container || !title || !desc) return;
    if (isGuest) {
        title.innerHTML = 'Sign In <span class="material-symbols-rounded" style="font-size:18px;color:var(--accent-glow);">login</span>';
        desc.textContent = 'Unlock unlimited access & save your data';
        container.onclick = () => { showAuthWall(); };
    } else {
        title.innerHTML = 'Settings <span class="material-symbols-rounded" style="font-size:18px;color:var(--text-muted);">settings</span>';
        desc.textContent = 'Quota, Plan, Instructions & Feedback';
        container.onclick = () => { openSettingsModal(); };
    }
}

function openSettingsOrLogin() {
    if (isGuestMode) {
        showAuthWall();
    } else {
        openSettingsModal();
    }
}

// ============================================================
// AUTH HANDLING (Google) – Callback
// ============================================================
function handleCredentialResponse(response) {
    const token = response.credential;
    const payload = decodeJwt(token);
    if (!payload) {
        document.getElementById('google-loading-overlay')?.remove();
        alert('Invalid Google credential. Please try again.');
        return;
    }
    localStorage.setItem('google_auth_token', token);
    googleAuthUserToken = token;
    isGuestMode = false;
    showMainUI();
    updateSidebarForGuest(false);
    initializeSecureWorkspace(payload, token)
        .catch(err => {
            console.error('Workspace init failed:', err);
            // show error in UI
            const hero = document.getElementById('hero-display');
            if (hero) {
                hero.innerHTML = `<div style="color:#ef4444;">Failed to load workspace. Please refresh.</div>`;
            }
        })
        .finally(() => {
            document.getElementById('google-loading-overlay')?.remove();
        });
    setAvatar(payload.picture, payload.name);
}
function triggerGoogleLogin() {
    const overlay = document.createElement('div');
    overlay.id = 'google-loading-overlay';
    overlay.style.cssText = `
        position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 99999;
        display: flex; align-items: center; justify-content: center;
        font-size: 18px; color: #fff; backdrop-filter: blur(4px);
    `;
    overlay.innerHTML = `<span class="material-symbols-rounded" style="font-size:48px; animation: spin 1s linear infinite;">sync</span> Signing in...`;
    document.body.appendChild(overlay);

    const start = () => {
        if (typeof google === 'undefined' || !google.accounts?.id) return false;
        google.accounts.id.initialize({
            client_id: GOOGLE_CLIENT_ID,
            callback: (response) => {
                document.getElementById('google-loading-overlay')?.remove();
                handleCredentialResponse(response);
            },
            cancel_on_tap_outside: false,
            context: 'signin',
            use_fedcm_for_prompt: false
        });
        google.accounts.id.prompt();
        return true;
    };
    if (!start()) {
        // fallback: try again after a short delay
        let attempts = 0;
        const retry = setInterval(() => {
            attempts += 1;
            if (start() || attempts >= 30) {
                clearInterval(retry);
                if (attempts >= 30) {
                    document.getElementById('google-loading-overlay')?.remove();
                    alert('Google Sign‑In could not start. Please refresh and try again.');
                }
            }
        }, 100);
    }
}
// ============================================================
// AUTH FUNCTIONS (Login buttons)
// ============================================================
function triggerGoogleLogin() {
    const start = () => {
        if (typeof google === 'undefined' || !google.accounts?.id) return false;
        google.accounts.id.initialize({
            client_id: GOOGLE_CLIENT_ID,
            callback: handleCredentialResponse,
            cancel_on_tap_outside: false,
            context: 'signin',
            use_fedcm_for_prompt: false
        });
        google.accounts.id.prompt();
        return true;
    };
    if (start()) return;
    let attempts = 0;
    const retry = setInterval(() => {
        attempts += 1;
        if (start() || attempts >= 30) clearInterval(retry);
    }, 100);
}

function triggerGitHubLogin() {
    window.location.href = `${API_BASE_URL}/api/auth/github`;
}

function showEmailLogin() {
    const email = prompt('Enter your email:');
    if (!email) return;
    const password = prompt('Enter your password:');
    if (!password) return;
    fetch(`${API_BASE_URL}/api/auth/email`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password })
    })
    .then(res => res.json())
    .then(data => {
        if (data.token) {
            localStorage.setItem('google_auth_token', data.token);
            location.reload();
        } else {
            alert(data.message || 'Login failed');
        }
    })
    .catch(err => alert('Network error: ' + err.message));
}

function triggerPasskeyLogin() {
    const email = prompt('Enter your email for passkey login:');
    if (!email) return;
    fetch(`${API_BASE_URL}/api/auth/webauthn/login/begin`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
    })
    .then(res => res.json())
    .then(options => navigator.credentials.get({ publicKey: options }))
    .then(credential => {
        return fetch(`${API_BASE_URL}/api/auth/webauthn/login/finish`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, credential })
        });
    })
    .then(res => res.json())
    .then(data => {
        if (data.token) {
            localStorage.setItem('google_auth_token', data.token);
            location.reload();
        } else {
            alert(data.message || 'Passkey authentication failed');
        }
    })
    .catch(err => alert('Passkey error: ' + err.message));
}
// ============================================================
// INSTANT GUEST / PREVIEW ENTRY (ZERO NETWORK DELAY)
// ============================================================
// ============================================================
// INSTANT ENTRY & INTERACTION CONTROLLERS
// ============================================================
function continueAsGuest() {
    isGuestMode = true;
    sessionStorage.setItem('axelr_guest_active', 'true');

    // 1. Instantly unlock the UI with zero network latency
    showMainUI();
    updateSidebarForGuest(true);
    updateQuotaDisplay({ 
        tier: 'guest', 
        subTierOptions: { hasDataAccess: false, hasDesignAccess: false }, 
        quotas: { dailyExtractionsUsed: 0, dailyGenerationsUsed: 0 } 
    });

    const banner = getEl('guest-banner');
    if (banner) banner.style.display = 'block';

    // 2. Fetch session asynchronously in the background
    fetch(`${API_BASE_URL}/api/guest/session`, { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data && data.sessionId) {
                guestSessionId = data.sessionId;
                console.log('🟢 Guest session active:', guestSessionId);
            }
        })
        .catch(err => {
            console.warn('Backend waking up; ephemeral session active:', err);
            guestSessionId = 'guest_' + Date.now();
        });
}

function showMainUI() {
    const authWallEl = document.getElementById('auth-wall');
    const mainWrapperEl = document.getElementById('content-mask');
    
    if (authWallEl) {
        authWallEl.style.display = 'none';
    }
    if (mainWrapperEl) {
        mainWrapperEl.classList.add('visible');
        mainWrapperEl.style.display = 'flex'; // Preserves flex layout
    }
    
    const hero = document.getElementById('hero-display');
    if (hero && !document.querySelector('.chat-bubble')) {
        hero.style.display = 'flex';
    }
    
    const wsSel = getEl('workspace-selector');
    if (wsSel) wsSel.style.display = 'none';

    // Mount menu items to the new '+' command bar button
    updateFeaturesMenu();
}

// Passkey Registration
async function registerPasskey() {
    const email = prompt('Enter your email to register a passkey:');
    if (!email) return;
    try {
        const beginResp = await fetch(`${API_BASE_URL}/api/auth/webauthn/register/begin`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email })
        });
        const options = await beginResp.json();
        const credential = await navigator.credentials.create({ publicKey: options });
        const finishResp = await fetch(`${API_BASE_URL}/api/auth/webauthn/register/finish`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, credential })
        });
        const result = await finishResp.json();
        if (result.success) {
            alert('Passkey registered successfully!');
        } else {
            alert('Registration failed: ' + (result.message || 'Unknown error'));
        }
    } catch (err) {
        alert('Passkey error: ' + err.message);
    }
}

// GitHub callback handler
(function handleGitHubCallback() {
    const urlParams = new URLSearchParams(window.location.search);
    const token = urlParams.get('token');
    if (token) {
        localStorage.setItem('google_auth_token', token);
        window.location.href = window.location.origin + window.location.pathname;
    }
})();

// ============================================================
// GLOBAL LOGOUT
// ============================================================
function executeGlobalLogout() {
    localStorage.removeItem('google_auth_token');
    googleAuthUserToken = null;
    isGuestMode = false;
    showAuthWall();
}
window.executeGlobalLogout = executeGlobalLogout;

// ============================================================
// INITIALIZATION
// ============================================================
function initializeApp() {
    loadModelConfig().then(() => {
        const savedWorkspace = localStorage.getItem('Axelr_workspace') || 'general';
        updateModelBranding(savedWorkspace, window.currentUser?.tier || 'free');
    });

    if (appInitialized) return;
    appInitialized = true;

    const saved = localStorage.getItem('axelr_theme') || 'system';
    currentThemePreference = saved;
    systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(saved === 'system' ? (systemDark ? 'dark' : 'light') : saved);

    // 1. Check for logged-in user
    const savedToken = localStorage.getItem('google_auth_token');
    if (savedToken) {
        try {
            const payload = decodeJwt(savedToken);
            if (payload && Date.now() < payload.exp * 1000) {
                googleAuthUserToken = savedToken;
                showMainUI();
                initializeSecureWorkspace(payload, savedToken);
                return;
            }
        } catch (_) {
            localStorage.removeItem('google_auth_token');
        }
    }

    // 2. Check for active guest session
    if (sessionStorage.getItem('axelr_guest_active') === 'true') {
        continueAsGuest();
        return;
    }

    // 3. Otherwise show Auth Wall
    showAuthWall();
    isGuestMode = false;
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeApp);
} else {
    initializeApp();
}
// ============================================================
// PRODUCTION APPLICATION BOOTSTRAP (CLEAN - ZERO RUNTIME CRASH)
// ============================================================
document.addEventListener('DOMContentLoaded', () => {
    initializeApp();
});

// Final Session Recovery
if (localStorage.getItem('google_auth_token')) {
    loadUserProfile()
        .then(() => loadArchiveLogs())
        .then(() => {
            const storedSessionId = localStorage.getItem('axelr_active_session');
            if (storedSessionId) viewPastLogById(storedSessionId);
            setTimeout(setupViewportObserver, 500);
        })
        .catch(err => console.warn('Session hydration deferred:', err));
}

setTimeout(() => {
    if (localStorage.getItem('google_auth_token') && authWall && authWall.style.display !== 'none') {
        console.warn('Auth wall still visible despite token – forcing hide');
        showMainUI();
        if (!window.currentUser) {
            const token = localStorage.getItem('google_auth_token');
            if (token) {
                const payload = decodeJwt(token);
                if (payload) initializeSecureWorkspace(payload, token);
            }
        }
    }
}, 1000);

// ============================================================
// SECURE WORKSPACE INIT
// ============================================================
async function initializeSecureWorkspace(payload, token) {
    googleAuthUserToken = token;
    currentUserId = payload.sub;
    showMainUI();
    setAvatar(payload.picture, payload.name);
    const dropdownName = getEl('dropdown-name');
    if (dropdownName) dropdownName.innerText = payload.name;
    const dropdownEmail = getEl('dropdown-email');
    if (dropdownEmail) dropdownEmail.innerText = payload.email;

    const savedWorkspace = localStorage.getItem('Axelr_workspace');
    updateWorkspaceTheme(savedWorkspace || 'data');

    try {
        await loadUserProfile();
    } catch (e) { console.warn('Profile load failed:', e); }
    try {
        await loadArchiveLogs();
    } catch (e) { console.warn('History load failed:', e); }
    try {
        await loadUserPreferences();
    } catch (e) { /* ignore */ }
    displaySuggestions();
}

function setAvatar(picture, name) {
    const avatarImg = getEl('user-avatar');
    const fallback = getEl('user-avatar-fallback');
    const dropdownImg = getEl('dropdown-avatar');
    const dropdownFallback = getEl('dropdown-avatar-fallback');

    if (avatarImg) {
        if (picture) {
            avatarImg.src = picture;
            avatarImg.style.display = 'block';
            if (fallback) fallback.style.display = 'none';
        } else {
            avatarImg.style.display = 'none';
            if (fallback) {
                fallback.style.display = 'flex';
                fallback.innerText = name ? name.charAt(0).toUpperCase() : 'U';
            }
        }
    }
    if (dropdownImg) {
        if (picture) {
            dropdownImg.src = picture;
            dropdownImg.style.display = 'block';
            if (dropdownFallback) dropdownFallback.style.display = 'none';
        } else {
            dropdownImg.style.display = 'none';
            if (dropdownFallback) {
                dropdownFallback.style.display = 'flex';
                dropdownFallback.innerText = name ? name.charAt(0).toUpperCase() : 'U';
            }
        }
    }
}

// ============================================================
// USER PREFERENCES
// ============================================================
async function loadUserPreferences() {
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/user/preferences`);
        if (resp.ok) {
            const data = await resp.json();
            const defaultWorkspace = data.defaultWorkspace || 'data';
            if (localStorage.getItem('Axelr_workspace') !== defaultWorkspace) {
                localStorage.setItem('Axelr_workspace', defaultWorkspace);
                await activateWorkspace(defaultWorkspace, true);
            }
        }
    } catch (e) {
        console.warn('Failed to load preferences:', e);
    }
}

// ============================================================
// SMART SUGGESTIONS
// ============================================================
async function displaySuggestions() {
    const container = getEl('suggestions-container');
    if (!container) return;
    const workspace = getWorkspace();
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/suggestions?workspace=${workspace}`);
        if (resp.ok) {
            const data = await resp.json();
            const suggestions = data.suggestions || [];
            const top3 = suggestions.slice(0, 3);
            container.innerHTML = top3.map(s =>
                `<div class="suggestion-chip" onclick="document.getElementById('prompt-input').value='${escapeHtmlEntities(s)}'; promptInput.dispatchEvent(new Event('input')); document.getElementById('send-trigger').click();">${escapeHtmlEntities(s)}</div>`
            ).join('');
            container.style.display = 'flex';
        } else {
            container.style.display = 'none';
        }
    } catch (e) {
        container.style.display = 'none';
    }
}
// ============================================================
// VIEWPORT & KEYBOARD ADJUSTMENT
// ============================================================
// ============================================================
// VIEWPORT & KEYBOARD ADJUSTMENT (CLEAN)
// ============================================================
function adjustCommandWrapperAndViewport() {
    const vv = window.visualViewport;
    if (!vv || window.innerWidth > 768) return;
    const keyboardOffset = Math.max(0, window.innerHeight - vv.height);
    if (commandWrapper) {
        commandWrapper.style.bottom = keyboardOffset > 80 ? `${keyboardOffset}px` : '0px';
    }
    const offsetY = window.innerHeight - vv.height;
    const maxBottom = Math.min(offsetY, window.innerHeight * 0.4);
    const currentBottom = parseFloat(commandWrapper?.style.bottom || '0');
    if (Math.abs(currentBottom - maxBottom) > 3 && commandWrapper) {
        commandWrapper.style.bottom = maxBottom + 'px';
    }
    const fileChips = getEl('file-staging-container');
    const fileChipsHeight = fileChips && stagedFiles.length > 0 ? fileChips.offsetHeight : 0;
    const availableHeight = window.innerHeight - maxBottom - 20 - fileChipsHeight;
    if (commandWrapper) commandWrapper.style.maxHeight = Math.min(availableHeight, window.innerHeight * 0.8) + 'px';
    adjustViewportPadding();
}

let resizeTimeout2 = null;
function debouncedAdjust() {
    if (resizeTimeout2) cancelAnimationFrame(resizeTimeout2);
    resizeTimeout2 = requestAnimationFrame(() => {
        adjustCommandWrapperAndViewport();
        resizeTimeout2 = null;
    });
}

if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', debouncedAdjust);
    window.visualViewport.addEventListener('scroll', debouncedAdjust);
}

// ============================================================
// VIEWPORT OBSERVER
// ============================================================
function setupViewportObserver() {
    if (viewportObserver) {
        viewportObserver.disconnect();
        viewportObserver = null;
    }
    if (!viewport) return;
    viewportObserver = new MutationObserver(() => {
        if (!isUserScrolling && observerActive) {
            const lastMessage = viewport.querySelector('.chat-bubble:last-child');
            if (lastMessage) {
                const rect = lastMessage.getBoundingClientRect();
                const viewportRect = viewport.getBoundingClientRect();
                if (rect.bottom > viewportRect.bottom - 50) {
                    scrollToBottom(true);
                }
            }
        }
    });
    viewportObserver.observe(viewport, { childList: true, subtree: true, characterData: true, attributes: true });
}

if (viewport) {
    viewport.addEventListener('scroll', () => {
        isUserScrolling = true;
        clearTimeout(scrollTimeout);
        scrollTimeout = setTimeout(() => { isUserScrolling = false; }, 500);
    }, { passive: true });
}

// ============================================================
// FILE HANDLING
// ============================================================
function renderFileChips() {
    const container = getEl('file-staging-container');
    if (!container) return;
    if (stagedFiles.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }
    container.style.display = 'flex';
    container.innerHTML = stagedFiles.map((file, idx) =>
        `<div class="file-chip" style="display:inline-flex;align-items:center;gap:5px;background:rgba(0,242,254,0.08);border:1px solid rgba(0,242,254,0.15);color:var(--text-muted);padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;margin:2px 0;">
            <span class="material-symbols-rounded" style="font-size:14px;">description</span>
            <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100px;display:inline-block;vertical-align:middle;">${escapeHtmlEntities(file.name)}</span>
            <span style="cursor:pointer;color:#ef4444;font-weight:bold;font-size:14px;flex-shrink:0;padding-left:2px;" onclick="removeStagedFile(${idx})"><span class="material-symbols-rounded" style="font-size:14px;">close</span></span>
        </div>`
    ).join('');
}

function removeStagedFile(idx) {
    stagedFiles.splice(idx, 1);
    renderFileChips();
    validateSendCommand();
}
async function openWorkflowModal() {
    closeModals();
    const modal = document.createElement('div');
    modal.className = 'modal-overlay active';
    modal.id = 'workflow-modal';
    modal.innerHTML = `
        <div class="modal-card" style="max-width:600px;">
            <div class="modal-header">
                <div class="modal-title">Auto‑Workflow</div>
                <button class="close-modal-btn" onclick="closeModals()">✕</button>
            </div>
            <div style="padding:10px 0;">
                <div id="workflow-steps">
                    <div class="workflow-step">
                        <input placeholder="Step name" class="wf-step-name" value="Extract data">
                        <textarea placeholder="Prompt (use {context} for previous output)" class="wf-step-prompt" rows="2">Extract all numbers and dates from the context.</textarea>
                        <button class="remove-step-btn" onclick="this.parentElement.remove()">✕</button>
                    </div>
                </div>
                <button class="add-step-btn" onclick="addWorkflowStep()">+ Add Step</button>
                <button class="modal-submit-btn" onclick="runWorkflowFromModal()">▶ Run Workflow</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

function addWorkflowStep() {
    const container = document.getElementById('workflow-steps');
    const step = document.createElement('div');
    step.className = 'workflow-step';
    step.innerHTML = `
        <input placeholder="Step name" class="wf-step-name" value="Step ${container.children.length + 1}">
        <textarea placeholder="Prompt (use {context})" class="wf-step-prompt" rows="2">Write your prompt here...</textarea>
        <button class="remove-step-btn" onclick="this.parentElement.remove()">✕</button>
    `;
    container.appendChild(step);
}

async function runWorkflowFromModal() {
    const steps = [];
    document.querySelectorAll('.workflow-step').forEach(el => {
        const name = el.querySelector('.wf-step-name').value.trim() || 'Step';
        const prompt = el.querySelector('.wf-step-prompt').value.trim();
        if (prompt) steps.push({ name, prompt, temperature: 0.2 });
    });
    if (steps.length === 0) return alert('Add at least one step.');
    closeModals();
    await runWorkflow(steps);
}
function openKnowledgePanel() {
    closeModals();
    let modal = getEl('knowledge-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'knowledge-modal';
        modal.className = 'modal-overlay active';
        modal.innerHTML = `
            <div class="modal-card knowledge-modal-card">
                <div class="modal-header">
                    <div class="modal-title"><span class="material-symbols-rounded">bookmark</span> Knowledge Vault</div>
                    <button class="close-modal-btn" onclick="closeModals()"><span class="material-symbols-rounded">close</span></button>
                </div>
                <p class="knowledge-modal-intro">Save reusable context for future workspaces and search it when needed.</p>
                <div class="knowledge-form">
                    <input id="knowledge-key" placeholder="Key or topic">
                    <input id="knowledge-value" placeholder="Context to remember">
                    <button class="modal-submit-btn" onclick="saveKnowledgeFromUI()">Save</button>
                </div>
                <input id="knowledge-search" class="knowledge-search-input" placeholder="Search saved knowledge..." oninput="searchKnowledge(this.value)">
                <div id="knowledge-list" class="knowledge-list"></div>
            </div>`;
        document.body.appendChild(modal);
    } else {
        modal.classList.add('active');
    }
    loadKnowledgeList();
}

async function saveKnowledgeFromUI() {
    const key = document.getElementById('knowledge-key')?.value.trim();
    const value = document.getElementById('knowledge-value')?.value.trim();
    if (!key || !value) return alert('Enter both key and value.');
    await saveKnowledge(key, value, []);
    document.getElementById('knowledge-key').value = '';
    document.getElementById('knowledge-value').value = '';
    loadKnowledgeList();
}

async function loadKnowledgeList() {
    const data = await (await apiFetch(`${API_BASE_URL}/api/knowledge`)).json();
    const container = document.getElementById('knowledge-list');
    if (container) {
        container.innerHTML = data.knowledge.map(k => `
            <div class="knowledge-item" style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border-muted);">
                <span><strong>${escapeHtmlEntities(k.key)}</strong>: ${escapeHtmlEntities(k.value)}</span>
                <button onclick="deleteKnowledge('${k._id}')" style="background:none;border:none;color:#ef4444;cursor:pointer;">✕</button>
            </div>
        `).join('');
    }
}

async function searchKnowledge(query) {
    if (!query.trim()) { loadKnowledgeList(); return; }
    const resp = await apiFetch(`${API_BASE_URL}/api/knowledge/search?q=${encodeURIComponent(query)}`);
    const data = await resp.json();
    const container = document.getElementById('knowledge-list');
    if (container) {
        container.innerHTML = data.results.map(k => `
            <div class="knowledge-item" style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border-muted);">
                <span><strong>${escapeHtmlEntities(k.key)}</strong>: ${escapeHtmlEntities(k.value)}</span>
            </div>
        `).join('');
    }
}

async function deleteKnowledge(id) {
    if (!confirm('Delete this knowledge?')) return;
    await apiFetch(`${API_BASE_URL}/api/knowledge/${id}`, { method: 'DELETE' });
    loadKnowledgeList();
}
async function openPersonaSelector() {
    closeModals();
    const modal = document.createElement('div');
    modal.className = 'modal-overlay active';
    modal.id = 'persona-modal';
    modal.innerHTML = `
        <div class="modal-card" style="max-width:500px;">
            <div class="modal-header">
                <div class="modal-title">🧑‍💼 Persona Library</div>
                <button class="close-modal-btn" onclick="closeModals()">✕</button>
            </div>
            <div style="padding:10px 0;">
                <div id="persona-list"></div>
                <button onclick="createPersona()" style="margin-top:10px;padding:6px 12px;background:var(--accent-glow);color:#000;border:none;border-radius:4px;">+ New Persona</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    await loadPersonaList();
}

async function loadPersonaList() {
    const resp = await apiFetch(`${API_BASE_URL}/api/personas`);
    const data = await resp.json();
    const container = document.getElementById('persona-list');
    if (container) {
        container.innerHTML = data.personas.map(p => `
            <div class="persona-item" style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border-muted);">
                <div>
                    <strong>${escapeHtmlEntities(p.name)}</strong>
                    <div style="font-size:12px;color:var(--text-muted);">${escapeHtmlEntities(p.system_prompt?.slice(0,80))}…</div>
                </div>
                <button onclick="applyPersona('${p._id}')" style="padding:4px 12px;background:#3b82f6;color:#fff;border:none;border-radius:4px;">Apply</button>
            </div>
        `).join('');
    }
}

async function createPersona() {
    const name = prompt('Persona name:');
    if (!name) return;
    const prompt_text = prompt('System prompt for this persona:');
    if (!prompt_text) return;
    const isPublic = confirm('Make this persona public?');
    await apiFetch(`${API_BASE_URL}/api/personas`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, system_prompt: prompt_text, is_public: isPublic })
    });
    loadPersonaList();
}

async function applyPersona(personaId) {
    const resp = await apiFetch(`${API_BASE_URL}/api/personas/${personaId}`);
    const data = await resp.json();
    if (data.system_prompt) {
        await apiFetch(`${API_BASE_URL}/api/user/instructions`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ instructions: data.system_prompt })
        });
        showToast('Persona applied as custom instructions.', 'success');
        closeModals();
        // Reload profile to reflect
        await loadUserProfile();
    }
}

if (fileInput) {
    fileInput.addEventListener('change', (e) => {
        let maxBytes = 5 * 1024 * 1024;
        let maxFilesAllowed = 2;
        if (document.body.classList.contains('pro-tier')) {
            maxBytes = 20 * 1024 * 1024;
            maxFilesAllowed = 5;
        } else if (document.body.classList.contains('designer-tier')) {
            maxBytes = 50 * 1024 * 1024;
            maxFilesAllowed = 5;
        }
        const incomingFiles = Array.from(e.target.files);
        if ((stagedFiles.length + incomingFiles.length) > maxFilesAllowed) {
            alert(`⚠️ Tier Limit Reached: You can only attach up to ${maxFilesAllowed} files.`);
            fileInput.value = '';
            return;
        }
        const validFiles = incomingFiles.filter(file => {
            if (file.size === 0) {
                alert(`⚠️ The file "${file.name}" is empty (0 bytes).`);
                return false;
            }
            if (file.type && !file.type.startsWith('image/') && file.type !== 'text/plain' && file.type !==
                'text/csv' && file.type !== 'application/pdf' && !file.type.includes('spreadsheet') && !file
                .type.includes('document')) {
                alert(`⚠️ File type "${file.type}" is not supported. Please upload images, PDFs, CSVs, or spreadsheets.`);
                return false;
            }
            return true;
        });
        let incomingSize = validFiles.reduce((acc, file) => acc + file.size, 0);
        let currentStagedSize = stagedFiles.reduce((acc, file) => acc + file.size, 0);
        if ((incomingSize + currentStagedSize) > maxBytes) {
            alert(`⚠️ Payload Exceeds Tier Capacity. Maximum allowed: ${maxBytes / (1024*1024)}MB.`);
            fileInput.value = '';
            return;
        }
        stagedFiles = [...stagedFiles, ...validFiles];
        renderFileChips();
        fileInput.value = '';
        validateSendCommand();
    });
}

// ============================================================
// SIDEBAR FUNCTIONS
// ============================================================
function toggleSidebar() {
    if (sidebarNode) sidebarNode.classList.toggle('open');
}
if (sidebarTriggerArea) {
    sidebarTriggerArea.addEventListener('click', (e) => {
        e.stopPropagation();
        if (sidebarNode) sidebarNode.classList.toggle('open');
    });
}

window.addEventListener('click', (e) => {
    if (ignoreSidebarClose || document.activeElement === getEl('sidebar-search-input')) return;
    if (e.target.closest('#sidebar-container-node')) return;
    if (e.target.closest('#sidebar-search-box') || e.target.closest('#sidebar-search-box input')) {
        e.stopPropagation();
        return;
    }
    if (sidebarNode && !sidebarNode.contains(e.target) && e.target !== sidebarTriggerArea) {
        sidebarNode.classList.remove('open');
    }
    if (accountDropdownCard && !document.querySelector('.account-hub')?.contains(e.target)) {
        accountDropdownCard.style.display = 'none';
    }
    if (modelDropdownCard && !document.querySelector('.model-hub')?.contains(e.target)) {
        modelDropdownCard.style.display = 'none';
    }
    if (!e.target.closest('.history-options-btn')) {
        document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    }
});

const searchInput = getEl('sidebar-search-input');
const searchBox = getEl('sidebar-search-box');
[searchBox, searchInput].forEach(el => {
    if (!el) return;
    el.addEventListener('click', (e) => e.stopPropagation());
    el.addEventListener('touchstart', (e) => e.stopPropagation());
});
if (searchInput) {
    searchInput.addEventListener('focus', () => {
        ignoreSidebarClose = true;
        setTimeout(() => { ignoreSidebarClose = false; }, 500);
    });
    searchInput.addEventListener('blur', () => { ignoreSidebarClose = false; });
}

function toggleAccountDropdown(e) {
    e.stopPropagation();
    if (!accountDropdownCard) return;
    accountDropdownCard.style.display = accountDropdownCard.style.display === 'flex' ? 'none' : 'flex';
    if (modelDropdownCard) modelDropdownCard.style.display = 'none';
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
}

function toggleModelDropdown(e) {
    e.stopPropagation();
    if (!modelDropdownCard) return;
    modelDropdownCard.style.display = modelDropdownCard.style.display === 'flex' ? 'none' : 'flex';
    if (accountDropdownCard) accountDropdownCard.style.display = 'none';
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
}

function selectModel(e, type) {
    e.stopPropagation();
    if (modelDropdownCard) modelDropdownCard.style.display = 'none';
}

function toggleHistoryOptions(e, id) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    const el = getEl(`options-${id}`);
    if (el) el.classList.add('active');
}

function switchSidebarTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    const tabBtn = getEl(`tab-${tab}`);
    if (tabBtn) tabBtn.classList.add('active');
    loadArchiveLogs();
}
function openSettingsModal() {
    closeModals();
    const modal = getEl('settings-modal');
    if (modal) modal.classList.add('active');
    updateSettingsQuota();
    renderInlineWorkspaceCards();       // ← populate immediately
    document.querySelectorAll('.theme-option').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === currentThemePreference);
    });
}
function updateSettingsQuota() {
    const quotaCount = getEl('quota-numerical-count');
    const quotaFill = getEl('quota-progress-bar-fill');
    const planBadge = getEl('sidebar-plan-badge');

    const settingsQuotaCount = getEl('settings-quota-count');
    const settingsQuotaFill = getEl('settings-quota-fill');
    const settingsPlanBadge = getEl('settings-plan-badge');

    if (quotaCount && settingsQuotaCount) {
        settingsQuotaCount.innerText = quotaCount.innerText;
    }
    if (quotaFill && settingsQuotaFill) {
        settingsQuotaFill.style.width = quotaFill.style.width;
    }
    if (planBadge && settingsPlanBadge) {
        settingsPlanBadge.innerText = planBadge.innerText;
        settingsPlanBadge.style.background = planBadge.style.background;
        settingsPlanBadge.style.color = planBadge.style.color;
    }
}

// ============================================================
// SEARCH OVERLAY
// ============================================================
function openSearchOverlay() {
    const overlay = getEl('search-overlay');
    if (!overlay) return;
    overlay.classList.add('active');
    const input = getEl('search-overlay-input');
    if (input) {
        input.value = '';
        input.focus();
    }
    filterSearchOverlay();
}
function closeSearchOverlay() {
    const overlay = getEl('search-overlay');
    if (overlay) overlay.classList.remove('active');
}
function filterSearchOverlay() {
    const input = getEl('search-overlay-input');
    const resultsContainer = getEl('search-overlay-results');
    if (!input || !resultsContainer) return;
    const query = input.value.toLowerCase();
    const logs = cachedLogHistory.filter(log => log.status === currentTab);
    const filtered = query ? logs.filter(log => log.filename.toLowerCase().includes(query)) : logs;
    if (filtered.length === 0) {
        resultsContainer.innerHTML = `<div style="color:var(--text-muted);text-align:center;padding:30px;">No matching chats</div>`;
        return;
    }
    resultsContainer.innerHTML = filtered.map(log => `
        <div class="search-result-item" onclick="viewPastLogById('${log._id}'); closeSearchOverlay();">
            <div class="title">${escapeHtmlEntities(log.filename)}</div>
            <div class="preview">${log.messages.length} messages • ${new Date(log.createdAt).toLocaleDateString()}</div>
        </div>
    `).join('');
}
const searchOverlayInput = getEl('search-overlay-input');
if (searchOverlayInput) searchOverlayInput.addEventListener('input', filterSearchOverlay);

if (searchInput) {
    searchInput.addEventListener('focus', function(e) {
        e.preventDefault();
        this.blur();
        openSearchOverlay();
    });
}
if (searchBox) {
    searchBox.addEventListener('click', function(e) {
        e.preventDefault();
        openSearchOverlay();
    });
}

// ============================================================
// WORKSPACE FUNCTIONS
// ============================================================
function showWorkspaceSelector() {
    // If the settings modal is open, dismiss it first so the workspace
    // selector isn't trapped behind it in the stacking order.
    const settingsModal = getEl('settings-modal');
    if (settingsModal) settingsModal.classList.remove('active');

    // Also hide the account / model dropdowns so nothing competes for focus
    if (accountDropdownCard) accountDropdownCard.style.display = 'none';
    if (modelDropdownCard)   modelDropdownCard.style.display = 'none';

    const ws = getEl('workspace-selector');
    if (!ws) return;

    // Force a stacking-context reset and bring it to the top
    ws.style.display  = 'flex';
    ws.style.zIndex   = '100000';   // higher than any modal
    ws.style.position = 'fixed';
    ws.classList.add('active');

    // Nudge the browser so any entry animation (opacity / scale) plays
    // immediately instead of waiting for the next repaint.
    void ws.offsetWidth;
}
function selectWorkspace(type) {
    localStorage.setItem('Axelr_workspace', type);
    const ws = getEl('workspace-selector');
    if (ws) {
        ws.classList.remove('active');
        ws.style.display = 'none';
        ws.style.zIndex  = '';
    }
    activateWorkspace(type);
}
function activateWorkspace(type, isBoot = false) {
    if (mainWrapper) mainWrapper.classList.add('visible');
    document.body.classList.remove('workspace-data', 'workspace-design', 'workspace-general');
    document.body.classList.add(`workspace-${type}`);
    const isMobile = window.innerWidth <= 768;
    const logo = getEl('sidebar-logo-text');
    const heroTitle = getEl('hero-title-text');
    const heroSub = getEl('hero-sub-text');
    if (type === 'design') {
        if (logo) logo.innerText = 'AXELR DESIGN';
        if (heroTitle) heroTitle.innerText = 'What are we designing today?';
        if (heroSub) heroSub.innerText = 'AI-powered UI/UX generation & live deployment.';
        if (promptInput) promptInput.placeholder = isMobile ? "Upload a mockup..." : "Upload a mockup or request a UI component...";
    } else if (type === 'general') {
        if (logo) logo.innerText = 'AXELR';
        if (heroTitle) heroTitle.innerText = 'What can I help you with?';
        if (heroSub) heroSub.innerText = 'Intelligence execution for any task.';
        if (promptInput) promptInput.placeholder = isMobile ? "Ask anything..." : "Ask me anything – I\'m here to help...";
    } else {
        if (logo) logo.innerText = 'AXELR DATA';
        if (heroTitle) heroTitle.innerText = 'What are we building today?';
        if (heroSub) heroSub.innerText = 'AI-powered architecture and data execution.';
        if (promptInput) promptInput.placeholder = isMobile ? "Upload a receipt..." : "Upload a receipt, invoice, or CSV for extraction...";
    }
    resetToNewChat(isBoot);
    if (!isBoot && !isGuestMode && localStorage.getItem('google_auth_token')) loadArchiveLogs();
    displaySuggestions();
    updateFeaturesMenu(type);
    if (window.currentUser) updateQuotaDisplay(window.currentUser);
    updateModelBranding(type, window.currentUser?.tier || 'free');
}

function resetToNewChat(isBoot = false) {
    if (viewportObserver) {
        viewportObserver.disconnect();
        viewportObserver = null;
    }
    activeSessionId = null;
    runningStructuredCache = null;
    if (stagedFiles.length > 0) {
        stagedFiles = [];
        renderFileChips();
    }
    localStorage.removeItem('Axelr_active_session');
    if (viewport) {
        viewport.querySelectorAll('.chat-bubble').forEach(bubble => bubble.remove());
    }
    if (heroDisplay) heroDisplay.style.display = 'flex';
    if (!isBoot) {
        if (promptInput) {
            promptInput.value = '';
            promptInput.style.height = 'auto';
        }
        localStorage.removeItem(getDraftKey());
    } else {
        const savedDraft = localStorage.getItem(getDraftKey());
        if (savedDraft && promptInput) {
            promptInput.value = savedDraft;
            setTimeout(() => {
                promptInput.style.height = 'auto';
                promptInput.style.height = promptInput.scrollHeight + 'px';
            }, 10);
        }
    }
    validateSendCommand();
    if (window.innerWidth <= 768 && sidebarNode) sidebarNode.classList.remove('open');
    hasRegenerated = false;
    if (regenerateTimer) {
        clearTimeout(regenerateTimer);
        regenerateTimer = null;
    }
    if (mainBackBtn) mainBackBtn.style.display = 'none';
    adjustViewportPadding();
    setTimeout(setupViewportObserver, 100);
}

// ============================================================
// VALIDATION
// ============================================================
function validateSendCommand() {
    const hasInput = (promptInput?.value?.trim()?.length || 0) > 0 || stagedFiles.length > 0;
    if (sendBtn) sendBtn.disabled = !hasInput;
    const inputFrame = document.querySelector('.input-frame');
    if (inputFrame) {
        if (hasInput) inputFrame.classList.add('has-text');
        else inputFrame.classList.remove('has-text');
    }
}

if (promptInput) {
    promptInput.addEventListener('input', () => {
        promptInput.style.height = 'auto';
        promptInput.style.height = promptInput.scrollHeight + 'px';
        validateSendCommand();
        localStorage.setItem(getDraftKey(), promptInput.value);
    });
}

window.addEventListener('offline', () => {
    const banner = getEl('offline-banner');
    if (banner) banner.style.display = 'block';
    if (sendBtn) sendBtn.disabled = true;
});
window.addEventListener('online', () => {
    const banner = getEl('offline-banner');
    if (banner) banner.style.display = 'none';
    validateSendCommand();
});

// ============================================================
// VOICE INPUT
// ============================================================
const micBtn = getEl('mic-trigger');
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SpeechRecognition && micBtn) {
    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = window.navigator.language || 'en-US';
    let basePromptText = "";
    micBtn.onclick = () => {
        if (isListeningForVocal) { recognition.stop(); return; }
        try { recognition.start(); } catch (err) {
            alert("⚠️ Microphone pipeline locked. Refresh the page and check URL bar permissions.");
        }
    };
    recognition.onstart = () => {
        isListeningForVocal = true;
        micBtn.classList.add('listening');
        basePromptText = promptInput?.value || "";
    };
    recognition.onend = () => {
        isListeningForVocal = false;
        micBtn.classList.remove('listening');
    };
    recognition.onerror = (event) => {
        isListeningForVocal = false;
        micBtn.classList.remove('listening');
        if (event.error === 'not-allowed') alert("⚠️ Microphone access denied. Allow permissions in your browser URL bar.");
        else if (event.error === 'no-speech') alert("⚠️ No audio detected. Check your microphone settings.");
    };
    recognition.onresult = (event) => {
        let interim = "";
        let final = "";
        for (let i = event.resultIndex; i < event.results.length; ++i) {
            if (event.results[i].isFinal) final += event.results[i][0].transcript;
            else interim += event.results[i][0].transcript;
        }
        if (promptInput) {
            promptInput.value = (basePromptText + " " + final + " " + interim).trim();
            promptInput.style.height = 'auto';
            promptInput.style.height = promptInput.scrollHeight + 'px';
            validateSendCommand();
            localStorage.setItem(getDraftKey(), promptInput.value);
        }
    };
} else if (micBtn) {
    micBtn.style.display = 'none';
}

// ============================================================
// USER PROFILE & QUOTA
// ============================================================
async function loadUserProfile() {
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/user/profile`);
        if (resp.ok) {
            const data = await resp.json();
            window.currentUser = data;
            const instrInput = getEl('instructions-input');
            if (instrInput) instrInput.value = data.customInstructions || "";
            const isAdmin = data.isAdmin === true && data.email === 'shanh1346@gmail.com';
            const adminBtn = getEl('admin-dashboard-btn');
            if (adminBtn) {
                if (isAdmin) {
                    adminBtn.style.display = 'block';
                    console.log('✅ Admin mode enabled for', data.email);
                } else {
                    adminBtn.style.display = 'none';
                }
            }

            updateQuotaDisplay(data);

            const planBadge = getEl('sidebar-plan-badge');
            if (planBadge) {
                planBadge.innerText = data.tier.toUpperCase();
                planBadge.style.background = data.tier === 'free' ? 'var(--border-muted)' :
                    data.tier === 'pro' ? 'rgba(139,92,246,0.2)' : 'rgba(244,63,94,0.2)';
                planBadge.style.color = data.tier === 'free' ? 'var(--text-muted)' :
                    data.tier === 'pro' ? 'var(--accent-glow-pro)' : 'var(--accent-glow-designer)';
            }
            const subPlan = getEl('sub-plan-name');
            if (subPlan) subPlan.innerText = data.tier.toUpperCase() + ' ALLOCATION';
            const tierBadge = getEl('tier-badge');
            if (tierBadge) tierBadge.innerText = data.tier.toUpperCase() + ' TIER';

            document.body.classList.toggle('pro-tier', data.tier === 'pro');
            document.body.classList.toggle('designer-tier', data.tier === 'business');
            if (data.tier !== 'pro' && data.tier !== 'business') {
                document.body.classList.remove('pro-tier', 'designer-tier');
            }

            const puterToggle = getEl('puter-toggle');
            if (puterToggle) {
                puterToggle.checked = data.puter_enabled === true;
                const desc = getEl('puter-desc');
                if (desc) desc.innerText = data.puter_enabled ? 'Puter enabled' : 'Puter disabled';
            }
            if (!data.puter_enabled && !window.puterOptInShown) {
                setTimeout(showPuterOptIn, 1500);
            } else if (data.puter_enabled) {
                loadPuterSDK();
            }

            updateSettingsQuota();
            updateSubscriptionModal();
            // Update model branding based on workspace and tier
            updateModelBranding(getWorkspace(), data.tier);
        }
    } catch (e) { console.warn('Profile load failed', e); }
}

// ============================================================
// HISTORY
// ============================================================
async function loadArchiveLogs() {
    try {
        const currentWorkspace = getWorkspace();
        const response = await apiFetch(
            `${API_BASE_URL}/api/history?status=${currentTab}&workspace=${currentWorkspace}`
        );
        if (response.status === 401) return executeGlobalLogout();
        cachedLogHistory = (await response.json()).logs;
        if (!historyListContainer) return;
        if (cachedLogHistory.length === 0) {
            historyListContainer.innerHTML = `<div style="color:#4b5563;font-size:12px;text-align:center;padding:15px;">No ${currentTab} chats.</div>`;
            return;
        }
        historyListContainer.innerHTML = cachedLogHistory.map(log => `
            <div class="history-item" onclick="viewPastLogById('${log._id}')">
                <div class="history-info"><div class="history-title" id="title-${log._id}">${log.isPinned ? '<span style="display:inline-flex;align-items:center;vertical-align:middle;">' + SIDEBAR_ICONS.push_pin + '</span>' : ''}${escapeHtmlEntities(log.filename)}</div></div>
                <button class="history-options-btn" onclick="toggleHistoryOptions(event, '${log._id}')">${ICONS.moreVertical}</button>
                <div class="actions-dropdown-list" id="options-${log._id}">
                    ${currentTab === 'active' ? `
                        <div class="action-list-item" onclick="renameChat('${log._id}', '${escapeHtmlEntities(log.filename)}', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.edit}</span> Rename</div>
                        <div class="action-list-item" onclick="pinChat('${log._id}', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.push_pin}</span> ${log.isPinned ? 'Unpin' : 'Pin'}</div>
                        <div class="action-list-item" onclick="shareChat('${log._id}', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.link}</span> Share Text</div>
                        <div class="action-list-item" onclick="exportChat('${log._id}', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;"><svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></span> Export Chat</div>
                        <div class="action-list-item" onclick="changeChatStatus('${log._id}', 'archived', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.inventory}</span> Archive</div>
                        <div class="action-list-item danger" onclick="changeChatStatus('${log._id}', 'trashed', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.delete}</span> Trash</div>
                    ` : ''}
                    ${currentTab === 'archived' ? `<div class="action-list-item" onclick="changeChatStatus('${log._id}', 'active', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.undo}</span> Unarchive</div><div class="action-list-item danger" onclick="changeChatStatus('${log._id}', 'trashed', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.delete}</span> Trash</div>` : ''}
                    ${currentTab === 'trashed' ? `<div class="action-list-item" onclick="changeChatStatus('${log._id}', 'active', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.restore}</span> Restore</div><div class="action-list-item danger" onclick="deleteLogPermanently('${log._id}', event)"><span style="display:inline-flex;align-items:center;vertical-align:middle;">${SIDEBAR_ICONS.delete_forever}</span> Delete Forever</div>` : ''}
                </div>
            </div>`).join('');
        if (isInitialAppLoad) {
            isInitialAppLoad = false;
            const savedSession = localStorage.getItem('axelr_active_session');
            if (savedSession && cachedLogHistory.some(l => l._id === savedSession)) {
                viewPastLogById(savedSession);
            }
            const savedDraft = localStorage.getItem(getDraftKey());
            if (savedDraft && promptInput) {
                promptInput.value = savedDraft;
                promptInput.style.height = 'auto';
                promptInput.style.height = promptInput.scrollHeight + 'px';
                validateSendCommand();
            }
        }
    } catch (e) { console.warn('Failed to load history:', e); }
}

// ============================================================
// EXPORT CHAT
// ============================================================
async function exportChat(logId, e) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    const log = cachedLogHistory.find(l => l._id === logId);
    if (!log) return;
    let md = `# Axelr Chat Export\n\n**Chat:** ${log.filename}\n**Date:** ${new Date(log.createdAt).toLocaleString()}\n**Workspace:** ${log.workspace}\n\n`;
    log.messages.forEach(msg => {
        const role = msg.role === 'user' ? '**User**' : '**Axelr**';
        const timestamp = msg.createdAt ? new Date(msg.createdAt).toLocaleString() : '';
        md += `### ${role} ${timestamp ? '– ' + timestamp : ''}\n\n${msg.text}\n\n`;
        if (msg.attachedFiles && msg.attachedFiles.length) {
            md += `*Attached: ${msg.attachedFiles.join(', ')}*\n\n`;
        }
    });
    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${log.filename.replace(/\s+/g, '_')}.md`;
    a.click();
    URL.revokeObjectURL(url);
}

// ============================================================
// RENAME, PIN, SHARE, STATUS
// ============================================================
async function renameChat(logId, currentName, e) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    const newName = prompt('Rename chat:', currentName);
    if (!newName || newName.trim() === '') return;
    const item = e.target.closest('.history-item');
    const originalTitle = item?.querySelector('.history-title')?.innerHTML;
    if (item) {
        const titleEl = item.querySelector('.history-title');
        if (titleEl) titleEl.innerHTML = `<span class="spinner"></span> Renaming...`;
    }
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/history/${logId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'rename', payload: newName.trim() })
        });
        if (resp.ok) loadArchiveLogs();
        else if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    } catch (e) {
        console.error('Rename failed:', e);
        if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    }
}

async function pinChat(logId, e) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    const item = e.target.closest('.history-item');
    const originalTitle = item?.querySelector('.history-title')?.innerHTML;
    if (item) {
        const titleEl = item.querySelector('.history-title');
        if (titleEl) titleEl.innerHTML = `<span class="spinner"></span> Updating...`;
    }
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/history/${logId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'pin' })
        });
        if (resp.ok) loadArchiveLogs();
        else if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    } catch (e) {
        if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    }
}

async function shareChat(logId, e) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    const log = cachedLogHistory.find(l => l._id === logId);
    if (!log) return;
    let shareText = `Axelr Intel Report: ${log.filename}\n\n`;
    log.messages.forEach(m => { shareText += `[${m.role.toUpperCase()}]: ${m.text}\n\n`; });
    if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(shareText);
        alert("✓ Chat copied securely to clipboard.");
    } else {
        const textArea = document.createElement("textarea");
        textArea.value = shareText;
        document.body.appendChild(textArea);
        textArea.select();
        try { document.execCommand('copy'); alert("✓ Chat copied to clipboard."); } catch (err) { alert("⚠️ Browser security blocked clipboard access."); }
        document.body.removeChild(textArea);
    }
}

async function changeChatStatus(logId, status, e) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    const item = e.target.closest('.history-item');
    const originalTitle = item?.querySelector('.history-title')?.innerHTML;
    if (item) {
        const titleEl = item.querySelector('.history-title');
        if (titleEl) titleEl.innerHTML = `<span class="spinner"></span> Moving...`;
    }
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/history/${logId}/status`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status })
        });
        if (resp.ok) {
            if (activeSessionId === logId && status === 'trashed') resetToNewChat();
            loadArchiveLogs();
        } else if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    } catch (e) {
        if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    }
}

async function deleteLogPermanently(logId, e) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    if (!confirm("Delete permanently? This cannot be undone.")) return;
    const item = e.target.closest('.history-item');
    const originalTitle = item?.querySelector('.history-title')?.innerHTML;
    if (item) {
        const titleEl = item.querySelector('.history-title');
        if (titleEl) titleEl.innerHTML = `<span class="spinner"></span> Deleting...`;
    }
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/history/${logId}`, {
            method: 'DELETE'
        });
        if (resp.ok) {
            if (activeSessionId === logId) resetToNewChat();
            loadArchiveLogs();
        } else if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    } catch (e) {
        if (item && originalTitle) item.querySelector('.history-title').innerHTML = originalTitle;
    }
}

// ============================================================
// VIEW PAST LOG
// ============================================================
function viewPastLogById(logId) {
    if (regenerateTimer) {
        clearTimeout(regenerateTimer);
        regenerateTimer = null;
    }
    const log = cachedLogHistory.find(l => l._id === logId);
    if (!log) return;
    if (heroDisplay) heroDisplay.style.display = 'none';
    
    // Remove ONLY if switching to a different session
    if (activeSessionId !== logId) {
        if (viewport) viewport.querySelectorAll('.chat-bubble').forEach(b => b.remove());
    }
    activeSessionId = logId;
    localStorage.setItem('axelr_active_session', activeSessionId);
    runningFileTitle = log.filename;
    runningStructuredCache = log.structuredData;
    if (mainBackBtn) mainBackBtn.style.display = 'flex';
    if (currentTab === 'trashed' && viewport) {
        const trashMsg = document.createElement('div');
        trashMsg.className = 'chat-bubble';
        trashMsg.style.cssText = "background:rgba(239,68,68,0.1);color:#ef4444;padding:15px;text-align:center;border-radius:8px;margin-bottom:20px;width:100%;";
        trashMsg.innerHTML = `<span class="material-symbols-rounded" style="font-size:20px;">delete</span> This chat is in the trash. Restore it to continue chatting.`;
        viewport.appendChild(trashMsg);
    }
    hasRegenerated = false;
    if (regenerateTimer) {
        clearTimeout(regenerateTimer);
        regenerateTimer = null;
    }
    log.messages.forEach((msg, idx) => {
        if (!msg.variants || !Array.isArray(msg.variants)) {
            msg.variants = [msg.text];
        }
        if (msg.activeVariant === undefined || msg.activeVariant === null) {
            msg.activeVariant = 0;
        }
        const bubble = document.createElement('div');
        bubble.className = `chat-bubble ${msg.role === 'user' ? 'user-bubble' : 'nexus-bubble'}`;
        if (msg.role === 'user') {
            let filesHtml = '';
            if (msg.attachedFiles && msg.attachedFiles.length > 0) {
                filesHtml = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">' + msg.attachedFiles.map(f =>
                    `<div class="file-chip"><span class="material-symbols-rounded" style="font-size:14px;">description</span> ${escapeHtmlEntities(f)}</div>`
                ).join('') + '</div>';
            }
            const contentDiv = document.createElement('div');
            contentDiv.className = 'bubble-content';
            contentDiv.style.flex = '1';
            contentDiv.innerHTML = `${filesHtml}${DOMPurify.sanitize(marked.parse(msg.text || ""))}`;
            bubble.appendChild(contentDiv);
            let lastUserIdx = -1;
            log.messages.forEach((m, i) => { if (m.role === 'user') lastUserIdx = i; });
            const isLastUser = (msg.role === 'user' && idx === lastUserIdx);
            injectActionButtons(contentDiv, msg.text, true, false, null, null, isLastUser, true);
        } else {
            let rawResponse = msg.text || "";
            const avatarDiv = document.createElement('div');
            avatarDiv.className = 'ai-avatar-bubble';
            avatarDiv.innerHTML = AXELR_AVATAR_SVG;
            bubble.appendChild(avatarDiv);
            const contentDiv = document.createElement('div');
            contentDiv.className = 'bubble-content';
            contentDiv.style.flex = '1';
            contentDiv.innerHTML = DOMPurify.sanitize(marked.parse(rawResponse));
            bubble.appendChild(contentDiv);
            appendPayloadDownload(contentDiv);
            const rawCode = extractHtmlCode(rawResponse);
            if (rawCode) {
                const iframe = document.createElement('iframe');
                iframe.style.width = '100%';
                iframe.style.height = '400px';
                iframe.style.border = '1px solid var(--border-muted)';
                iframe.style.borderRadius = '8px';
                iframe.style.marginTop = '15px';
                iframe.style.backgroundColor = '#ffffff';
                contentDiv.appendChild(iframe);
                const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                iframeDoc.open();
                iframeDoc.write(rawCode);
                iframeDoc.close();
                injectDeployButton(contentDiv, rawCode);
            }
            const isLast = idx === log.messages.length - 1;
            const isActive = log.status === 'active';
            let showRegenerate = false;
            if (isLast && isActive) {
                if (msg.canRegenerate === true) {
                    if (!msg.variants || msg.variants.length <= 1) {
                        const msgDate = msg.createdAt ? new Date(msg.createdAt) : new Date(log.createdAt);
                        const now = new Date();
                        if (!isNaN(msgDate.getTime()) && (now - msgDate) < 30000) {
                            showRegenerate = true;
                        }
                    }
                }
            }
            injectActionButtons(contentDiv, rawResponse, false, showRegenerate, msg.createdAt || log.createdAt, log._id, false, true);
            if (msg.variants && msg.variants.length > 1) {
                const currentIdx = msg.activeVariant || 0;
                const variantBar = document.createElement('div');
                variantBar.style.cssText = "display:flex;align-items:center;gap:12px;margin-top:15px;padding-top:10px;border-top:1px solid var(--border-muted);font-size:12px;color:var(--text-muted);font-weight:600;";
                const prevBtn = document.createElement('button');
                prevBtn.innerHTML = ICONS.leftArrow;
                prevBtn.style.cssText = `background:transparent;border:none;cursor:${currentIdx === 0 ? 'default' : 'pointer'};color:${currentIdx === 0 ? 'var(--border-muted)' : 'var(--text-main)'};font-size:14px;`;
                if (currentIdx > 0) prevBtn.onclick = () => switchVariant(logId, msg._id, currentIdx - 1);
                const nextBtn = document.createElement('button');
                nextBtn.innerHTML = ICONS.rightArrow;
                nextBtn.style.cssText = `background:transparent;border:none;cursor:${currentIdx === msg.variants.length - 1 ? 'default' : 'pointer'};color:${currentIdx === msg.variants.length - 1 ? 'var(--border-muted)' : 'var(--text-main)'};font-size:14px;`;
                if (currentIdx < msg.variants.length - 1) nextBtn.onclick = () => switchVariant(logId, msg._id, currentIdx + 1);
                variantBar.appendChild(prevBtn);
                const countSpan = document.createElement('span');
                countSpan.innerText = `${currentIdx + 1} / ${msg.variants.length}`;
                variantBar.appendChild(countSpan);
                variantBar.appendChild(nextBtn);
                contentDiv.appendChild(variantBar);
            }
        }
        if (viewport) viewport.appendChild(bubble);
    });
    updateViewportAfterRender();
    if (window.innerWidth <= 768 && sidebarNode) sidebarNode.classList.remove('open');
    if (log.workspace === 'data' && log.structuredData && log.structuredData.length > 0) {
        const lastBubble = viewport?.querySelector('.chat-bubble:last-child .bubble-content');
        if (lastBubble) {
            renderChart(lastBubble, log.structuredData);
        }
    }
}

// ============================================================
// CHART RENDERING
// ============================================================
function renderChart(container, data) {
    if (!data || data.length === 0) return;
    const canvas = document.createElement('canvas');
    canvas.id = `chart-${Date.now()}`;
    canvas.style.width = '100%';
    canvas.style.height = '300px';
    canvas.style.marginTop = '15px';
    container.appendChild(canvas);
    const keys = Object.keys(data[0]);
    const numericKeys = keys.filter(k => data.every(row => !isNaN(parseFloat(row[k]))));
    if (numericKeys.length === 0) return;
    const labelKey = keys.find(k => !numericKeys.includes(k)) || keys[0];
    const chartData = {
        labels: data.map(row => String(row[labelKey] || '')),
        datasets: numericKeys.slice(0, 2).map((k, i) => ({
            label: k,
            data: data.map(row => parseFloat(row[k]) || 0),
            borderColor: i === 0 ? '#00f2fe' : '#8b5cf6',
            backgroundColor: i === 0 ? 'rgba(0,242,254,0.2)' : 'rgba(139,92,246,0.2)',
            fill: true,
            tension: 0.2
        }))
    };
    const ctx = canvas.getContext('2d');
    new Chart(ctx, {
        type: 'line',
        data: chartData,
        options: {
            responsive: true,
            plugins: {
                legend: { labels: { color: getComputedStyle(document.body).getPropertyValue('--text-main') } }
            },
            scales: {
                x: { ticks: { color: getComputedStyle(document.body).getPropertyValue('--text-muted') } },
                y: { ticks: { color: getComputedStyle(document.body).getPropertyValue('--text-muted') } }
            }
        }
    });
}

// ============================================================
// SWITCH VARIANT
// ============================================================
async function switchVariant(logId, msgId, newIndex) {
    try {
        await apiFetch(`${API_BASE_URL}/api/history/${logId}/variant`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ msgId, variantIndex: newIndex })
        });
        const log = cachedLogHistory.find(l => l._id === logId);
        if (log) {
            const msg = log.messages.find(m => m._id === msgId);
            if (msg) {
                msg.activeVariant = newIndex;
                msg.text = msg.variants[newIndex];
            }
        }
        viewPastLogById(logId);
    } catch (e) { console.error("Switch failed", e); }
}

// ============================================================
// ACTION BUTTONS
// ============================================================
function injectActionButtons(bubbleNode, rawText, isUserPrompt = false, showRegenerate = false, createdAt = null,
    sessionId = null, isLastUserMsg = false, isHistoryView = false) {
    const actionBar = document.createElement('div');
    actionBar.className = 'bubble-action-bar';

    if (isUserPrompt) {
        const copyBtn = document.createElement('button');
        copyBtn.className = 'action-icon-btn';
        copyBtn.title = "Copy Prompt";
        copyBtn.innerHTML = `${ICONS.copy} Copy`;
        copyBtn.onclick = () => handleActionClick('copy', rawText, copyBtn);
        actionBar.appendChild(copyBtn);

        if (isLastUserMsg && !isHistoryView) {
            const editBtn = document.createElement('button');
            editBtn.className = 'action-icon-btn';
            editBtn.title = "Edit & Retry";
            editBtn.innerHTML = `${ICONS.edit} Edit`;
            editBtn.onclick = () => {
                if (promptInput) {
                    promptInput.value = rawText;
                    promptInput.style.height = 'auto';
                    promptInput.style.height = promptInput.scrollHeight + 'px';
                    promptInput.focus();
                    validateSendCommand();
                    setTimeout(async () => {
                        executeCommand(false);
                    }, 300);
                }
                editBtn.disabled = true;
                editBtn.style.opacity = '0.5';
                editBtn.title = "Edit used";
            };
            actionBar.appendChild(editBtn);
        }
    } else {
        // Copy button
        const copyBtn = document.createElement('button');
        copyBtn.className = 'action-icon-btn';
        copyBtn.title = "Copy Response";
        copyBtn.innerHTML = `${ICONS.copy} Copy`;
        copyBtn.onclick = () => handleActionClick('copy', rawText, copyBtn);
        actionBar.appendChild(copyBtn);

        // Like button – only once
        const likeBtn = document.createElement('button');
        likeBtn.className = 'action-icon-btn';
        likeBtn.title = "Helpful Response";
        likeBtn.innerHTML = ICONS.thumbsUp;
        likeBtn.onclick = function(e) {
            e.stopPropagation();
            this.style.color = 'var(--accent-glow)';
            this.style.transform = 'scale(1.2)';
            setTimeout(() => this.style.transform = 'scale(1)', 200);
            showToast('Thanks for the feedback!', 'success');
        };
        actionBar.appendChild(likeBtn);

        // Dislike button – only once
        const dislikeBtn = document.createElement('button');
        dislikeBtn.className = 'action-icon-btn';
        dislikeBtn.title = "Not Helpful";
        dislikeBtn.innerHTML = ICONS.thumbsDown;
        dislikeBtn.onclick = function(e) {
            e.stopPropagation();
            this.style.color = '#ef4444';
            this.style.transform = 'scale(1.2)';
            setTimeout(() => this.style.transform = 'scale(1)', 200);
            showToast('We\'ll improve!', 'info');
        };
        actionBar.appendChild(dislikeBtn);

        // Regenerate button
        if (showRegenerate && createdAt && sessionId && !isHistoryView) {
            const now = Date.now();
            const msgTime = new Date(createdAt).getTime();
            const elapsed = now - msgTime;
            if (elapsed < 30000) {
                const regenBtn = document.createElement('button');
                regenBtn.className = 'action-icon-btn regen-active';
                regenBtn.title = "Regenerate response (available for 30s)";
                regenBtn.innerHTML = ICONS.regenerate + `<span class="regen-countdown">${Math.ceil((30000 - elapsed)/1000)}s</span>`;
                let intervalId, timeoutId;
                const cleanup = () => {
                    if (intervalId) clearInterval(intervalId);
                    if (timeoutId) clearTimeout(timeoutId);
                    if (regenBtn.parentNode) regenBtn.remove();
                };
                let remaining = Math.ceil((30000 - elapsed) / 1000);
                intervalId = setInterval(() => {
                    remaining--;
                    const countSpan = regenBtn.querySelector('.regen-countdown');
                    if (countSpan) countSpan.textContent = remaining + 's';
                    if (remaining <= 0) cleanup();
                }, 1000);
                timeoutId = setTimeout(cleanup, 30000 - elapsed);
                regenBtn.onclick = async function(e) {
                    if (activeSessionId !== sessionId) {
                        cleanup();
                        return;
                    }
                    cleanup();
                    suppressRegenerateForNextResponse = true;
                    if (window.lastUserCommand && promptInput) {
                        promptInput.value = window.lastUserCommand;
                        promptInput.style.height = 'auto';
                        promptInput.style.height = promptInput.scrollHeight + 'px';
                        if (regenerateTimer) {
                            clearTimeout(regenerateTimer);
                            regenerateTimer = null;
                        }
                        await executeCommand(true);
                    }
                };
                actionBar.appendChild(regenBtn);
            }
        }

        // Refactor, Explain, Tests buttons (if code block exists)
        if (!isUserPrompt && rawText && extractHtmlCode(rawText)) {
            const code = extractHtmlCode(rawText);
            // Refactor
            const refactorBtn = document.createElement('button');
            refactorBtn.className = 'action-icon-btn';
            refactorBtn.title = "Refactor Code";
            refactorBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/><path d="M21 7L17 3"/></svg> Refactor`;
            refactorBtn.onclick = async () => {
                const originalHtml = refactorBtn.innerHTML;
                refactorBtn.innerHTML = 'Refactoring...';
                refactorBtn.disabled = true;
                try {
                    const resp = await apiFetch(`${API_BASE_URL}/api/refactor`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ code })
                    });
                    if (resp.ok) {
                        const data = await resp.json();
                        const refactored = data.refactored_code;
                        const iframe = bubbleNode.closest('.bubble-content').querySelector('iframe');
                        if (iframe) {
                            const doc = iframe.contentDocument || iframe.contentWindow.document;
                            doc.open();
                            doc.write(refactored);
                            doc.close();
                        }
                        refactorBtn.innerHTML = '✅ Refactored';
                        setTimeout(() => {
                            refactorBtn.innerHTML = originalHtml;
                            refactorBtn.disabled = false;
                        }, 2000);
                    } else {
                        alert('Refactor failed.');
                        refactorBtn.innerHTML = originalHtml;
                        refactorBtn.disabled = false;
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                    refactorBtn.innerHTML = originalHtml;
                    refactorBtn.disabled = false;
                }
            };
            actionBar.appendChild(refactorBtn);

            // Explain
            const explainBtn = document.createElement('button');
            explainBtn.className = 'action-icon-btn';
            explainBtn.title = "Explain Code";
            explainBtn.innerHTML = `<span class="material-symbols-rounded" style="font-size:16px;">psychology</span> Explain`;
            explainBtn.onclick = async () => {
                const originalHtml = explainBtn.innerHTML;
                explainBtn.innerHTML = 'Explaining...';
                explainBtn.disabled = true;
                try {
                    const resp = await apiFetch(`${API_BASE_URL}/api/explain-code`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ code })
                    });
                    if (resp.ok) {
                        const data = await resp.json();
                        const explanation = data.explanation || 'No explanation provided.';
                        const bubble = document.createElement('div');
                        bubble.className = 'chat-bubble nexus-bubble';
                        const avatarDiv = document.createElement('div');
                        avatarDiv.className = 'ai-avatar-bubble';
                        avatarDiv.innerHTML = AXELR_AVATAR_SVG;
                        bubble.appendChild(avatarDiv);
                        const contentDiv = document.createElement('div');
                        contentDiv.className = 'bubble-content';
                        contentDiv.style.flex = '1';
                        contentDiv.innerHTML = DOMPurify.sanitize(marked.parse(explanation));
                        bubble.appendChild(contentDiv);
                        if (viewport) viewport.appendChild(bubble);
                        scrollToBottom();
                        explainBtn.innerHTML = '✅ Explained';
                    } else {
                        alert('Explain failed.');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
                setTimeout(() => {
                    explainBtn.innerHTML = originalHtml;
                    explainBtn.disabled = false;
                }, 2000);
            };
            actionBar.appendChild(explainBtn);

            // Tests
            const testsBtn = document.createElement('button');
            testsBtn.className = 'action-icon-btn';
            testsBtn.title = "Generate Tests";
            testsBtn.innerHTML = `<span class="material-symbols-rounded" style="font-size:16px;">fact_check</span> Tests`;
            testsBtn.onclick = async () => {
                const originalHtml = testsBtn.innerHTML;
                testsBtn.innerHTML = 'Generating...';
                testsBtn.disabled = true;
                try {
                    const resp = await apiFetch(`${API_BASE_URL}/api/generate-tests`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ code })
                    });
                    if (resp.ok) {
                        const data = await resp.json();
                        const tests = data.tests || 'No tests generated.';
                        const bubble = document.createElement('div');
                        bubble.className = 'chat-bubble nexus-bubble';
                        const avatarDiv = document.createElement('div');
                        avatarDiv.className = 'ai-avatar-bubble';
                        avatarDiv.innerHTML = AXELR_AVATAR_SVG;
                        bubble.appendChild(avatarDiv);
                        const contentDiv = document.createElement('div');
                        contentDiv.className = 'bubble-content';
                        contentDiv.style.flex = '1';
                        contentDiv.innerHTML = DOMPurify.sanitize(marked.parse('```javascript\n' + tests + '\n```'));
                        bubble.appendChild(contentDiv);
                        if (viewport) viewport.appendChild(bubble);
                        scrollToBottom();
                        testsBtn.innerHTML = '✅ Tests Ready';
                    } else {
                        alert('Test generation failed.');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
                setTimeout(() => {
                    testsBtn.innerHTML = originalHtml;
                    testsBtn.disabled = false;
                }, 2000);
            };
            actionBar.appendChild(testsBtn);
        }
    }
    bubbleNode.appendChild(actionBar);
}
function handleActionClick(actionType, rawText, btnRef) {
    if (actionType === 'copy') {
        navigator.clipboard.writeText(rawText);
        const originalHtml = btnRef.innerHTML;
        btnRef.innerHTML = `✓ Copied`;
        setTimeout(() => btnRef.innerHTML = originalHtml, 2000);
    } else if (actionType === 'edit' && promptInput) {
        promptInput.value = rawText;
        promptInput.style.height = 'auto';
        promptInput.style.height = promptInput.scrollHeight + 'px';
        promptInput.focus();
        validateSendCommand();
    }
}
// ============================================================
// WORKSPACE FEATURES MAPPING (Complete)
// ============================================================
const WORKSPACE_FEATURES = {
  general: [
    { id: 'summarize', icon: 'summarize', label: 'Summarize Chat' },
    { id: 'brainstorm', icon: 'lightbulb', label: 'Brainstorm' },
    { id: 'multi-agent', icon: 'groups', label: 'Multi-Agent' },
    { id: 'workflow', icon: 'flowchart', label: 'Workflow' },
    { id: 'knowledge', icon: 'bookmark', label: 'Knowledge Vault' },
    { id: 'personas', icon: 'person', label: 'Personas' },
    { id: 'eli5', icon: 'child_care', label: 'Explain Like I\'m 5' },
    { id: 'decision-matrix', icon: 'table_view', label: 'Decision Matrix' },
    { id: 'minutes', icon: 'assignment', label: 'Meeting Minutes' }
  ],
  data: [
    { id: 'summarize', icon: 'summarize', label: 'Summarize Data' },
    { id: 'storyteller', icon: 'auto_stories', label: 'Data Storyteller' },
    { id: 'export', icon: 'download', label: 'Export CSV' },
    { id: 'chart', icon: 'bar_chart', label: 'Generate Chart' },
    { id: 'workflow', icon: 'flowchart', label: 'Workflow' },
    { id: 'knowledge', icon: 'bookmark', label: 'Knowledge Vault' },
    { id: 'schema', icon: 'table_chart', label: 'Schema Discovery' },
    { id: 'scan-pii', icon: 'shield_person', label: 'PII Privacy Scan' },
    { id: 'mermaid', icon: 'account_tree', label: 'Mermaid Diagram' }
  ],
  design: [
    { id: 'touch-fix', icon: 'build', label: 'Touch & Fix' },
    { id: 'refactor', icon: 'code', label: 'Refactor Code' },
    { id: 'deploy', icon: 'rocket_launch', label: 'One-Click Deploy' },
    { id: 'visual-debug', icon: 'visibility', label: 'Visual Debugger' },
    { id: 'tests', icon: 'fact_check', label: 'Generate Tests' },
    { id: 'explain', icon: 'psychology', label: 'Explain Code' },
    { id: 'knowledge', icon: 'bookmark', label: 'Knowledge Vault' },
    { id: 'personas', icon: 'person', label: 'Personas' },
    { id: 'translate-code', icon: 'translate', label: 'Code Translator' },
    { id: 'mermaid', icon: 'account_tree', label: 'Generate Mermaid Diagram' }
  ]
};
// ============================================================
// UNIFIED CREATION TOOLS FOR COMMAND BAR '+'
// ============================================================
// Workspace-specific creation tools for the '+' menu
const WORKSPACE_CREATION_MENUS = {
    data: [
        { id: 'upload', icon: 'attach_file', label: 'Attach Data (CSV, Excel, PDF)', isUpload: true },
        { id: 'scan-pii', icon: 'shield_person', label: 'Scan Document for PII' },
        { id: 'decision-matrix', icon: 'table_view', label: 'Synthesize Decision Matrix' },
        { id: 'workflow', icon: 'flowchart', label: 'Automated Data Pipeline' }
    ],
    design: [
        { id: 'upload', icon: 'attach_file', label: 'Attach Mockup / Image', isUpload: true },
        { id: 'brainstorm', icon: 'lightbulb', label: 'Brainstorm UI Architecture' },
        { id: 'translate-code', icon: 'translate', label: 'Translate Code Component' },
        { id: 'mermaid', icon: 'account_tree', label: 'Generate Mermaid Diagram' }
    ],
    general: [
        { id: 'upload', icon: 'attach_file', label: 'Attach Assets / Data', isUpload: true },
        { id: 'multi-agent', icon: 'groups', label: 'Multi-Agent Orchestrator' },
        { id: 'brainstorm', icon: 'lightbulb', label: 'Brainstorm Architecture' },
        { id: 'workflow', icon: 'flowchart', label: 'Automated Pipeline' },
        { id: 'mermaid', icon: 'account_tree', label: 'Generate Mermaid Chart' },
        { id: 'decision-matrix', icon: 'table_view', label: 'Synthesize Decision Matrix' },
        { id: 'minutes', icon: 'assignment', label: 'Extract Meeting Minutes' }
    ]
};
function updateFeaturesMenu(workspace) {
    const ws = workspace || getWorkspace();
    const menu = document.getElementById('features-menu');
    if (!menu) return;

    const tools = WORKSPACE_CREATION_MENUS[ws] || WORKSPACE_CREATION_MENUS.general;
    menu.innerHTML = tools.map(tool => `
        <div class="feature-item ${tool.isUpload ? 'upload-highlight' : ''}" data-feature="${tool.id}">
            <span class="material-symbols-rounded">${tool.icon}</span>
            <span>${tool.label}</span>
        </div>
    `).join('');

    menu.querySelectorAll('.feature-item').forEach(btn => {
        btn.onclick = function(e) {
            e.stopPropagation();
            const actionId = this.dataset.feature;
            menu.classList.remove('open');
            if (actionId === 'upload') {
                document.getElementById('omni-file-input').click();
            } else {
                handleFeatureAction(actionId);
            }
        };
    });
}
// Ensure features menu is populated and toggle works
// In DOMContentLoaded or after features menu creation
const trigger = document.getElementById('features-trigger');
const menu = document.getElementById('features-menu');

if (trigger && menu) {
    trigger.addEventListener('click', function(e) {
        e.stopPropagation();
        e.preventDefault();
        menu.classList.toggle('open');
    });
    document.addEventListener('click', function(e) {
        if (!menu.contains(e.target) && e.target !== trigger) {
            menu.classList.remove('open');
        }
    });
    // Prevent menu close when clicking inside
    menu.addEventListener('click', function(e) {
        e.stopPropagation();
    });
}

// Handle feature actions (complete implementation)
// ============================================================
// FEATURE ACTIONS DISPATCHER (INCLUDES 5 ELITE TOOLS)
// ============================================================
async function handleFeatureAction(feature) {
    switch(feature) {
        case 'scan-pii': {
            const text = prompt('Paste the text or data to audit for sensitive PII:');
            if (!text) return;
            showToast('Scanning for sensitive PII...', 'info');
            try {
                const resp = await apiFetch(`${API_BASE_URL}/api/tools/scan-pii`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ document_text: text })
                });
                const data = await resp.json();
                if (data.success) {
                    const bubble = createNexusBubble(`### 🛡️ PII Privacy Scan Report\n\n${data.scan_report}`);
                    viewport.appendChild(bubble);
                    scrollToBottom();
                }
            } catch(e) {
                showToast('Scan error: ' + e.message, 'error');
            }
            break;
        }
        case 'translate-code': {
            const code = extractLastCodeBlock() || prompt('Paste code to translate:');
            if (!code) return;
            const targetLang = prompt('Target language/framework (e.g. React, Vue, Python, TypeScript):', 'TypeScript');
            if (!targetLang) return;
            showToast('Translating code...', 'info');
            try {
                const resp = await apiFetch(`${API_BASE_URL}/api/tools/translate-code`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code, source_lang: 'auto', target_lang: targetLang })
                });
                const data = await resp.json();
                if (data.success) {
                    const bubble = createNexusBubble(`### 🔄 Translated Code (${escapeHtmlEntities(targetLang)})\n\n${data.translated_code}`);
                    viewport.appendChild(bubble);
                    scrollToBottom();
                }
            } catch(e) {
                showToast('Translation error: ' + e.message, 'error');
            }
            break;
        }
        case 'minutes': {
            const transcript = prompt('Paste meeting transcript:');
            if (!transcript) return;
            showToast('Extracting meeting minutes...', 'info');
            try {
                const resp = await apiFetch(`${API_BASE_URL}/api/tools/meeting-minutes`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ transcript })
                });
                const data = await resp.json();
                if (data.success) {
                    const bubble = createNexusBubble(`### 📋 Meeting Minutes\n\n${data.minutes}`);
                    viewport.appendChild(bubble);
                    scrollToBottom();
                }
            } catch(e) {
                showToast('Minutes error: ' + e.message, 'error');
            }
            break;
        }
        case 'brainstorm': {
            const topic = prompt('Enter a topic to brainstorm:');
            if (topic) await brainstorm(topic);
            break;
        }
        case 'multi-agent': {
            const task = prompt('Enter task for the agent collective:');
            if (task) {
                const agents = [
                    { name: 'Researcher', role: 'research' },
                    { name: 'Coder', role: 'code' },
                    { name: 'Reviewer', role: 'review' }
                ];
                await runMultiAgent(task, agents);
            }
            break;
        }
        case 'workflow': await openWorkflowModal(); break;
        case 'mermaid': {
            const desc = prompt('Describe the architecture or process flow:');
            if (!desc) return;
            showToast('Synthesizing Mermaid diagram...', 'info');
            try {
                const resp = await apiFetch(`${API_BASE_URL}/api/tools/mermaid`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ process_description: desc })
                });
                const data = await resp.json();
                if (data.success) {
                    const bubble = createNexusBubble(data.mermaid);
                    viewport.appendChild(bubble);
                    scrollToBottom();
                }
            } catch(e) {
                showToast('Mermaid error: ' + e.message, 'error');
            }
            break;
        }
        case 'decision-matrix': {
            const options = prompt('Enter options (comma-separated):', 'PostgreSQL, MongoDB, DynamoDB');
            const criteria = prompt('Enter evaluation criteria (comma-separated):', 'Latency, Cost, Scaling, Schema Flexibility');
            if (options && criteria) {
                showToast('Synthesizing decision matrix...', 'info');
                try {
                    const resp = await apiFetch(`${API_BASE_URL}/api/tools/decision-matrix`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            options: options.split(',').map(s => s.trim()),
                            criteria: criteria.split(',').map(s => s.trim())
                        })
                    });
                    const data = await resp.json();
                    if (data.success) {
                        const bubble = createNexusBubble(data.matrix);
                        viewport.appendChild(bubble);
                        scrollToBottom();
                    }
                } catch(e) {
                    showToast('Matrix error: ' + e.message, 'error');
                }
            }
            break;
        }
        default:
            showToast('Feature initialized', 'info');
    }
}
// Helper to extract last code block
function extractLastCodeBlock() {
  const codeBlocks = viewport.querySelectorAll('pre code');
  if (codeBlocks.length === 0) return null;
  return codeBlocks[codeBlocks.length - 1].innerText;
}

// Render chart in a new bubble
function renderChartInViewport(data) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble nexus-bubble';
  const content = document.createElement('div');
  content.className = 'bubble-content';
  bubble.appendChild(content);
  viewport.appendChild(bubble);
  renderChart(content, data);
  scrollToBottom();
}

// ============================================================
// STRIPE LOADING OVERLAY
// ============================================================
function showStripeLoading(onCancel) {
    const overlay = document.createElement('div');
    overlay.id = 'stripe-loading';
    overlay.style.cssText = `
        position: fixed; inset: 0; background: rgba(0,0,0,0.7);
        display: flex; align-items: center; justify-content: center;
        z-index: 99999; backdrop-filter: blur(4px);
    `;
    overlay.innerHTML = `
        <div style="background: var(--bg-card); padding: 30px 40px; border-radius: 16px; text-align: center; border: 1px solid var(--border-muted); position: relative; max-width: 90%;">
            <button onclick="hideStripeLoading(); if (typeof onCancel === 'function') onCancel();" style="position: absolute; top: 10px; right: 15px; background: none; border: none; font-size: 24px; color: var(--text-muted); cursor: pointer;">&times;</button>
            <span class="material-symbols-rounded" style="font-size: 48px; color: var(--accent-glow); animation: spin 1s linear infinite;">sync</span>
            <div style="margin-top: 15px; color: var(--text-main); font-weight: 600;">Redirecting to Stripe...</div>
            <div style="font-size: 13px; color: var(--text-muted);">Please wait</div>
            <button onclick="hideStripeLoading(); if (typeof onCancel === 'function') onCancel();" style="margin-top: 20px; padding: 8px 20px; border: 1px solid var(--border-muted); border-radius: 8px; background: transparent; color: var(--text-main); cursor: pointer;">Cancel</button>
        </div>
    `;
    document.body.appendChild(overlay);
    window._stripeCancelCallback = onCancel || null;
}

function hideStripeLoading() {
    const el = getEl('stripe-loading');
    if (el) el.remove();
    if (typeof window._stripeCancelCallback === 'function') {
        window._stripeCancelCallback();
        window._stripeCancelCallback = null;
    }
}

// ============================================================
// ENHANCE PROMPT
// ============================================================
async function enhanceUserPrompt() {
    if (!promptInput) return;
    const text = promptInput.value.trim();
    if (!text) return;
    const enhanceBtn = getEl('enhance-trigger');
    const inputFrame = document.querySelector('.input-frame');
    if (!enhanceBtn || !inputFrame) return;

    const originalText = enhanceBtn.innerHTML;
    enhanceBtn.classList.add('loading');
    enhanceBtn.disabled = true;
    promptInput.disabled = true;
    inputFrame.style.filter = 'blur(4px) brightness(0.8)';
    inputFrame.style.pointerEvents = 'none';
    enhanceBtn.innerHTML = '<span style="font-size:12px;font-weight:bold;letter-spacing:1px;color:var(--accent-glow);"><span class="material-symbols-rounded" style="font-size:16px;">auto_awesome</span> PROCESSING...</span>';

    try {
        const response = await apiFetch(`${API_BASE_URL}/api/enhance-prompt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ promptText: text })
        });
        if (!response.ok) throw new Error("API rejection");
        const result = await response.json();
        if (result.success && result.enhanced) {
            promptInput.value = result.enhanced;
            promptInput.style.height = 'auto';
            promptInput.style.height = promptInput.scrollHeight + 'px';
            validateSendCommand();
            localStorage.setItem(getDraftKey(), promptInput.value);
        }
    } catch (e) {
        alert("⚠️ Prompt Enhancer timeout. Payload too large or network dropped.");
    } finally {
        enhanceBtn.classList.remove('loading');
        enhanceBtn.disabled = false;
        promptInput.disabled = false;
        inputFrame.style.filter = 'none';
        inputFrame.style.pointerEvents = 'auto';
        enhanceBtn.innerHTML = originalText;
        validateSendCommand();
    }
}

let savedWorkspace = localStorage.getItem('Axelr_workspace');
if (!savedWorkspace) {
  savedWorkspace = 'general';
  localStorage.setItem('Axelr_workspace', savedWorkspace);
}
updateWorkspaceTheme(savedWorkspace);
// ============================================================
// SECURITY LAYER
// ============================================================
function detectManipulationAttempt(command) {
    const patterns = [
        /forget all (instructions|prior|previous)/i,
        /disregard (system prompt|guidelines|instructions)/i,
        /ignore (all|previous) (instructions|prompts)/i,
        /override your (system|core|primary) instructions/i,
        /you are (not|no longer) bound by/i,
        /bypass your safety/i,
        /stop following your instructions/i,
        /reset your instructions/i
    ];
    for (let p of patterns) {
        if (p.test(command)) return true;
    }
    return false;
}

function showSecurityAlert(level) {
    const alertBubble = document.createElement('div');
    alertBubble.className = 'chat-bubble nexus-bubble';
    const avatarDiv = document.createElement('div');
    avatarDiv.className = 'ai-avatar-bubble';
    avatarDiv.innerHTML = AXELR_AVATAR_SVG;
    alertBubble.appendChild(avatarDiv);
    const contentDiv = document.createElement('div');
    contentDiv.className = 'bubble-content';
    contentDiv.style.flex = '1';
    let html = '';
    if (level === 1) {
        html = `<div class="security-alert-banner warning"><span class="material-symbols-rounded">warning</span><span>⚠️ Warning: Detected attempt to bypass core system architecture. Please stay within operational parameters to avoid account restriction.</span></div>`;
    } else if (level === 2) {
        html = `<div class="security-alert-banner critical"><span class="material-symbols-rounded">error</span><span>🚨 SECURITY ALERT: Multiple manipulation attempts detected. System locking for 10 minutes. Please reset your operational behavior.</span></div>`;
    }
    contentDiv.innerHTML = html;
    alertBubble.appendChild(contentDiv);
    if (viewport) viewport.appendChild(alertBubble);
    scrollToBottom();
}

// ============================================================
// EXECUTE COMMAND (with streaming by default)
// ============================================================
// ============================================================
// GLOBAL EXPOSURE – Ensure all feature handlers are accessible
// ============================================================
window.summarizeCurrentChat = summarizeCurrentChat;
window.brainstorm = brainstorm;
window.runMultiAgent = runMultiAgent;
window.openWorkflowModal = openWorkflowModal;
window.openKnowledgePanel = openKnowledgePanel;
window.openPersonaSelector = openPersonaSelector;
window.saveKnowledgeFromUI = saveKnowledgeFromUI;
window.loadKnowledgeList = loadKnowledgeList;
window.deleteKnowledge = deleteKnowledge;
window.applyPersona = applyPersona;
window.executeCodeBlock = executeCodeBlock;
window.touchFix = touchFix;
window.refactorCode = refactorCode;
window.deployCodeBlock = deployCodeBlock;
window.openVisualDebugger = openVisualDebugger;
window.generateTests = generateTests;
window.explainCodeBlock = explainCodeBlock;
window.discoverSchema = discoverSchema;
window.generateStoryFromData = generateStoryFromData;
window.displayStory = displayStory;

// Stub implementations for missing feature functions
async function generateStoryFromData(data) {
    // If backend supports storytelling, call endpoint; else return placeholder
    return "Story generation not implemented yet.";
}
function displayStory(story) {
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble nexus-bubble';
    bubble.innerHTML = `<div class="bubble-content">${DOMPurify.sanitize(marked.parse(story))}</div>`;
    viewport.appendChild(bubble);
    scrollToBottom();
}
async function touchFix(code, error) {
    const resp = await apiFetch(`${API_BASE_URL}/api/touch_fix`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code, error_message: error })
    });
    const data = await resp.json();
    if (data.success) {
        const bubble = createNexusBubble(`**Fixed Code**\n\n\`\`\`html\n${data.fixed_code}\n\`\`\``);
        viewport.appendChild(bubble);
        scrollToBottom();
    } else {
        showToast('Touch fix failed: ' + (data.message || 'Unknown'), 'error');
    }
}
async function refactorCode(code) {
    const resp = await apiFetch(`${API_BASE_URL}/api/refactor`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
    });
    const data = await resp.json();
    if (data.success) {
        const bubble = createNexusBubble(`**Refactored Code**\n\n\`\`\`html\n${data.refactored_code}\n\`\`\``);
        viewport.appendChild(bubble);
        scrollToBottom();
    } else {
        showToast('Refactor failed', 'error');
    }
}
async function deployCodeBlock(code) {
    const resp = await apiFetch(`${API_BASE_URL}/api/deploy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ htmlContent: code })
    });
    const data = await resp.json();
    if (data.success) {
        showToast(`Deployed: <a href="${data.liveUrl}" target="_blank">${data.liveUrl}</a>`, 'success');
    } else {
        showToast('Deploy failed', 'error');
    }
}
function openVisualDebugger() {
    showToast('Visual Debugger: Please use the "Debug" button on any code bubble.', 'info');
}
async function generateTests(code) {
    const resp = await apiFetch(`${API_BASE_URL}/api/generate-tests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
    });
    const data = await resp.json();
    if (data.success) {
        const bubble = createNexusBubble(`**Generated Tests**\n\n\`\`\`javascript\n${data.tests}\n\`\`\``);
        viewport.appendChild(bubble);
        scrollToBottom();
    } else {
        showToast('Test generation failed', 'error');
    }
}
async function explainCodeBlock(code) {
    const resp = await apiFetch(`${API_BASE_URL}/api/explain-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
    });
    const data = await resp.json();
    if (data.success) {
        const bubble = createNexusBubble(`**Explanation**\n\n${data.explanation}`);
        viewport.appendChild(bubble);
        scrollToBottom();
    } else {
        showToast('Explain failed', 'error');
    }
}
async function discoverSchema(files) {
    // Placeholder; actual implementation would use backend endpoint if available
    return null;
}
async function executeCommand(isRetry = false) {
    window.summarizeCurrentChat = summarizeCurrentChat;
window.brainstorm = brainstorm;
window.runMultiAgent = runMultiAgent;
window.openWorkflowModal = openWorkflowModal;
window.openKnowledgePanel = openKnowledgePanel;
window.openPersonaSelector = openPersonaSelector;
window.saveKnowledgeFromUI = saveKnowledgeFromUI;
window.loadKnowledgeList = loadKnowledgeList;
window.deleteKnowledge = deleteKnowledge;
window.applyPersona = applyPersona;
window.executeCodeBlock = executeCodeBlock;
    if (!activeSessionId && heroDisplay) heroDisplay.style.display = 'none';
    if (isProcessing) return;
    isProcessing = true;

    if (manipulationLockUntil && Date.now() < manipulationLockUntil) {
        const remaining = Math.ceil((manipulationLockUntil - Date.now()) / 1000);
        alert(`⛔ System temporarily locked due to security violations. Please wait ${remaining} seconds.`);
        isProcessing = false;
        return;
    }

    if (currentTab === 'trashed') {
        switchSidebarTab('active');
        activeSessionId = null;
        localStorage.removeItem('axelr_active_session');
    }

    if (!promptInput) { isProcessing = false; return; }
    const command = promptInput.value.trim();
    window.lastUserCommand = command;
    if (!command && stagedFiles.length === 0 && !isRetry) {
        isProcessing = false;
        return;
    }

    if (detectManipulationAttempt(command)) {
        manipulationCount++;
        sessionStorage.setItem('axelr_manipulation_count', manipulationCount);
        let level = 1;
        if (manipulationCount >= 3) {
            level = 2;
            manipulationLockUntil = Date.now() + 10 * 60 * 1000;
            sessionStorage.setItem('axelr_manipulation_lock', manipulationLockUntil);
        }
        const userBubble = document.createElement('div');
        userBubble.className = 'chat-bubble user-bubble';
        userBubble.innerHTML = DOMPurify.sanitize(marked.parse(command || " "));
        if (viewport) viewport.appendChild(userBubble);
        showSecurityAlert(level);
        promptInput.value = '';
        promptInput.style.height = 'auto';
        validateSendCommand();
        localStorage.removeItem(getDraftKey());
        isProcessing = false;
        return;
    }

    let finalCommand = command;
    const originalBtnHtml = sendBtn ? sendBtn.innerHTML : '';

    if (globalAbortController) {
        globalAbortController.abort();
        globalAbortController = null;
        if (sendBtn) {
            sendBtn.classList.remove('btn-stop-active');
            sendBtn.innerHTML = originalBtnHtml;
        }
        isProcessing = false;
        return;
    }

    if (!isRetry) {
        promptInput.value = '';
        promptInput.style.height = 'auto';
        validateSendCommand();
    }
    if (sendBtn) sendBtn.disabled = true;

    const userBubble = document.createElement('div');
    userBubble.className = 'chat-bubble user-bubble';
    let filesHtml = '';
    if (stagedFiles.length > 0) {
        filesHtml = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">' + stagedFiles.map(f =>
            `<div class="file-chip"><span class="material-symbols-rounded" style="font-size:14px;">description</span> ${escapeHtmlEntities(f.name)}</div>`
        ).join('') + '</div>';
    }
    userBubble.innerHTML = `${filesHtml}${DOMPurify.sanitize(marked.parse(command || " "))}`;
    if (viewport) viewport.appendChild(userBubble);

    if (isRetry) {
        if (viewport) {
            const allBubbles = viewport.querySelectorAll('.chat-bubble');
            if (allBubbles.length >= 2) {
                const lastBubble = allBubbles[allBubbles.length - 1];
                if (lastBubble.classList.contains('nexus-bubble')) {
                    lastBubble.remove();
                }
            }
        }
        hasRegenerated = true;
        if (regenerateTimer) {
            clearTimeout(regenerateTimer);
            regenerateTimer = null;
        }
    }

    const stagedFilesSnapshot = [...stagedFiles];
    stagedFiles = [];
    renderFileChips();

    // Create AI bubble for streaming
    const nexusBubble = document.createElement('div');
    nexusBubble.className = 'chat-bubble nexus-bubble';
    const avatarDiv = document.createElement('div');
    avatarDiv.className = 'ai-avatar-bubble';
    avatarDiv.innerHTML = AXELR_AVATAR_SVG;
    nexusBubble.appendChild(avatarDiv);

    const contentDiv = document.createElement('div');
    contentDiv.className = 'bubble-content';
    contentDiv.style.flex = '1';
    nexusBubble.appendChild(contentDiv);
    if (viewport) viewport.appendChild(nexusBubble);
    scrollToBottom();

    const isGuest = isGuestMode && !localStorage.getItem('google_auth_token');
    let contextKnowledge = '';
    if (!isGuest && finalCommand.length > 3) {
        try {
            contextKnowledge = await getRelevantKnowledge(finalCommand);
        } catch (_) {
            contextKnowledge = '';
        }
    }

    // Build form data
    const formData = new FormData();
    formData.append('command', finalCommand);
    formData.append('workspace', getWorkspace());
    formData.append('isRetry', isRetry ? 'true' : 'false');
    if (contextKnowledge) formData.append('context', contextKnowledge);
    if (isGuest) {
        formData.append('isGuest', 'true');
        if (guestSessionId) formData.append('sessionId', guestSessionId);
    } else if (activeSessionId) {
        formData.append('sessionId', activeSessionId);
    }

    for (const file of stagedFilesSnapshot) {
        formData.append('files', file);
    }

    if (sendBtn) {
        sendBtn.classList.add('btn-stop-active');
        sendBtn.innerHTML = ICONS.stop;
    }

    globalAbortController = new AbortController();

    try {
        const endpoint = isGuest ? '/api/guest/extract' : '/api/extract_stream';
        const token = await ensureValidToken();
        const response = await fetch(`${API_BASE_URL}${endpoint}`, {
            method: 'POST',
            body: formData,
            headers: {
                'Authorization': `Bearer ${token}`
            },
            signal: globalAbortController.signal,
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            let errorMsg = errorData.message || 'Pipeline failed.';
            if (errorData.code === 'LIMIT_REACHED' || errorData.code === 'GUEST_LIMIT_REACHED') {
                contentDiv.innerHTML = `⚠️ <strong>Daily Quota Exceeded.</strong><br><button onclick="openUpgradeModal()" style="background:var(--accent-glow-pro);color:#000;padding:8px 12px;border:none;border-radius:6px;cursor:pointer;font-weight:600;margin-top:10px;">Upgrade Workspace</button>`;
            } else {
                contentDiv.innerHTML = `💥 Error: ${errorMsg}`;
            }
            scrollToBottom();
            isProcessing = false;
            return;
        }

        // Process streaming response
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let fullResponse = '';
        let sessionIdFromStream = null;
        let structuredData = null;
        let filename = 'Export.csv';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            // Process SSE events: data: {...}\n\n
            const events = buffer.split('\n\n');
            for (let i = 0; i < events.length - 1; i++) {
                const event = events[i];
                if (event.startsWith('data: ')) {
                    try {
                        const json = JSON.parse(event.slice(6));
                        if (json.text) {
                            // Append text to the AI bubble
                            const textPart = json.text;
                            fullResponse += textPart;
                            // Update contentDiv: we need to render markdown progressively? For simplicity, just append text.
                            // But we should render it as markdown eventually. Since we're streaming, we can accumulate and re-render.
                            // For better UX, we'll just append the raw text and let it be rendered at the end? But we want progressive display.
                            // We'll create a temporary span for each text chunk.
                            if (!streamingBubble) {
                                streamingBubble = nexusBubble;
                                streamingContentDiv = contentDiv;
                            }
                            // Append text as plain text (will be processed after stream ends)
                            contentDiv.innerHTML += textPart;
                            scrollToBottom();
                        } else if (json.watermark) {
                            // Append watermark
                            contentDiv.innerHTML += json.watermark;
                        } else if (json.sessionId) {
                            sessionIdFromStream = json.sessionId;
                        } else if (json.structuredData) {
                            structuredData = json.structuredData;
                        } else if (json.filename) {
                            filename = json.filename;
                        } else if (json.error) {
                            contentDiv.innerHTML = `⚠️ ${json.error}`;
                        }
                    } catch (e) {
                        // Ignore malformed JSON
                    }
                }
            }
            buffer = events[events.length - 1]; // keep incomplete
        }

        // After streaming, finalize
        if (contentDiv) {
            // Re-render full response with markdown
            contentDiv.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse));
            // Append any extra (like watermark already appended)
            // Inject code blocks, deploy buttons, etc.
            const rawCode = extractHtmlCode(fullResponse);
            if (rawCode) {
                const iframe = document.createElement('iframe');
                iframe.style.width = '100%';
                iframe.style.height = '400px';
                iframe.style.border = '1px solid var(--border-muted)';
                iframe.style.borderRadius = '8px';
                iframe.style.marginTop = '15px';
                iframe.style.backgroundColor = '#ffffff';
                contentDiv.appendChild(iframe);
                const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                iframeDoc.open();
                iframeDoc.write(rawCode);
                iframeDoc.close();
                injectDeployButton(contentDiv, rawCode);
            }
            appendPayloadDownload(contentDiv);
            // Inject action buttons (regenerate, etc.)
            const showRegen = !suppressRegenerateForNextResponse;
            suppressRegenerateForNextResponse = false;
            injectActionButtons(contentDiv, fullResponse, false, showRegen, new Date().toISOString(), activeSessionId || guestSessionId);
            if (getWorkspace() === 'data' && structuredData && structuredData.length > 0) {
                renderChart(contentDiv, structuredData);
            }
        }

        // Update session
        if (sessionIdFromStream) {
            if (!isGuest) {
                activeSessionId = sessionIdFromStream;
                localStorage.setItem('axelr_active_session', activeSessionId);
                runningStructuredCache = structuredData;
                runningFileTitle = filename;
                await loadArchiveLogs();
            } else {
                guestSessionId = sessionIdFromStream;
            }
        }

        scrollToBottom();
        if (mainBackBtn) mainBackBtn.style.display = 'flex';

    } catch (error) {
        if (error.name === 'AbortError') {
            contentDiv.innerHTML += `<br><br><em style="color:var(--text-muted);">[Generation halted by user]</em>`;
        } else {
            console.error('Execute error:', error);
            contentDiv.innerHTML = `⚠️ All AI services are temporarily overloaded. Please try again in a moment.`;
            if (viewport && viewport.querySelectorAll('.chat-bubble').length === 0 && heroDisplay) {
                heroDisplay.style.display = 'none';
                if (mainBackBtn) mainBackBtn.style.display = 'flex';
            }
        }
        scrollToBottom();
    } finally {
        globalAbortController = null;
        if (sendBtn) {
            sendBtn.classList.remove('btn-stop-active');
            sendBtn.innerHTML = originalBtnHtml;
            sendBtn.disabled = false;
        }
        validateSendCommand();
        updateViewportAfterRender();
        await loadUserProfile();
        isProcessing = false;
        streamingBubble = null;
        streamingContentDiv = null;
    }
}

// ============================================================
// PAYLOAD / DEPLOY
// ============================================================
function appendPayloadDownload(bubbleNode) {
    if (runningStructuredCache && runningStructuredCache.length > 0) {
        const btn = document.createElement('button');
        btn.className = 'download-btn-bubble';
        btn.innerHTML = `<span class="material-symbols-rounded" style="font-size:18px;">download</span> Download CSV`;
        btn.onclick = executeDownloadPipeline;
        bubbleNode.appendChild(btn);
    }
}

function executeDownloadPipeline() {
    if (!runningStructuredCache) return;
    const keys = Object.keys(runningStructuredCache[0]);
    const csv = [keys.join(','), ...runningStructuredCache.map(row => keys.map(k =>
        `"${String(row[k] ?? '').replace(/"/g, '""')}"`).join(',')).join('\n')].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = runningFileTitle;
    a.click();
}

function injectDeployButton(bubbleNode, rawHtml) {
    const deployContainer = document.createElement('div');
    deployContainer.style.marginTop = '15px';
    const deployBtn = document.createElement('button');
    deployBtn.className = 'download-btn-bubble';
    deployBtn.style.background = 'var(--accent-secondary)';
    deployBtn.innerHTML = `<span class="material-symbols-rounded" style="font-size:18px;">rocket_launch</span> Deploy Live`;
    const debugBtn = document.createElement('button');
    debugBtn.className = 'deploy-debug-btn';
    debugBtn.innerText = 'Debug';
    debugBtn.style.display = 'inline-block';
    const errorDetails = document.createElement('div');
    errorDetails.className = 'deploy-error-details';
    deployContainer.appendChild(deployBtn);
    deployContainer.appendChild(debugBtn);
    deployContainer.appendChild(errorDetails);
    bubbleNode.appendChild(deployContainer);

    debugBtn.onclick = async function() {
        const errorMsg = prompt("Paste the error message from the console or browser:");
        if (!errorMsg) return;
        debugBtn.innerText = 'Fixing...';
        debugBtn.disabled = true;
        try {
            const response = await apiFetch(`${API_BASE_URL}/api/touch_fix`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ code: rawHtml, error_message: errorMsg })
            });
            const result = await response.json();
            if (result.success) {
                const iframe = bubbleNode.querySelector('iframe');
                if (iframe) {
                    const doc = iframe.contentDocument || iframe.contentWindow.document;
                    doc.open();
                    doc.write(result.fixed_code);
                    doc.close();
                }
                rawHtml = result.fixed_code;
                debugBtn.innerText = 'Fixed!';
                setTimeout(() => { debugBtn.innerText = 'Debug'; debugBtn.disabled = false; }, 2000);
            } else {
                alert('Fix failed: ' + (result.message || 'Unknown error'));
                debugBtn.innerText = 'Debug';
                debugBtn.disabled = false;
            }
        } catch (e) {
            alert('Network error: ' + e.message);
            debugBtn.innerText = 'Debug';
            debugBtn.disabled = false;
        }
    };

    let retryCount = 0;
    const maxRetries = 5;
    let isDeploying = false;
    async function attemptDeploy() {
        if (isDeploying) return;
        isDeploying = true;
        deployBtn.innerText = 'Deploying...';
        deployBtn.disabled = true;
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 30000);
            const response = await apiFetch(`${API_BASE_URL}/api/deploy`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ htmlContent: rawHtml }),
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            const result = await response.json();
            if (result.success) {
                deployBtn.innerHTML = `<a href="${result.liveUrl}" target="_blank" style="color:#000;text-decoration:none;display:flex;align-items:center;gap:8px;"><span class="material-symbols-rounded" style="font-size:18px;">open_in_new</span> View Live Website</a>`;
                deployBtn.style.background = '#3b82f6';
                deployBtn.onclick = null;
                deployBtn.disabled = false;
                debugBtn.style.display = 'none';
                isDeploying = false;
            } else {
                throw new Error(result.message || 'Deployment failed');
            }
        } catch (err) {
            console.error('Deploy error:', err);
            retryCount++;
            if (retryCount <= maxRetries) {
                const delay = Math.min(Math.pow(2, retryCount) * 500, 10000);
                deployBtn.innerText = `Retrying (${retryCount}/${maxRetries})...`;
                deployBtn.disabled = true;
                setTimeout(() => {
                    isDeploying = false;
                    attemptDeploy();
                }, delay);
            } else {
                deployBtn.innerText = '⚠️ Deployment Failed';
                deployBtn.style.background = '#ef4444';
                deployBtn.disabled = false;
                debugBtn.style.display = 'inline-block';
                errorDetails.textContent = `Error: ${err.message || 'Unknown error'}\nNetwork: ${navigator.onLine ? 'Online' : 'Offline'}`;
                errorDetails.classList.add('show');
                debugBtn.onclick = () => { errorDetails.classList.toggle('show'); };
                const retryBtn = document.createElement('button');
                retryBtn.className = 'deploy-retry-btn';
                retryBtn.innerText = 'Retry Deployment';
                retryBtn.onclick = () => {
                    retryCount = 0;
                    errorDetails.classList.remove('show');
                    deployBtn.style.background = 'var(--accent-secondary)';
                    deployBtn.innerText = 'Deploy Live';
                    deployBtn.disabled = false;
                    debugBtn.style.display = 'inline-block';
                    const oldRetry = deployContainer.querySelector('.deploy-retry-btn');
                    if (oldRetry) oldRetry.remove();
                    isDeploying = false;
                    attemptDeploy();
                };
                deployContainer.appendChild(retryBtn);
                isDeploying = false;
            }
        }
    }
    deployBtn.onclick = attemptDeploy;
}

// ============================================================
// MODALS
// ============================================================
function closeModals() {
    document.querySelectorAll('.modal-overlay').forEach(m => m.classList.remove('active'));
}

function openUpgradeModal() {
    closeModals();
    const modal = getEl('upgrade-modal');
    if (modal) modal.classList.add('active');
}

function openProfileModal() {
    closeModals();
    const modal = getEl('profile-modal');
    if (modal) modal.classList.add('active');
}

function openInstructionsModal() {
    closeModals();
    const modal = getEl('instructions-modal');
    if (modal) modal.classList.add('active');
}

function openFeedbackModal() {
    closeModals();
    const modal = getEl('feedback-modal');
    if (modal) modal.classList.add('active');
}

function openBillingFlow() {
    closeModals();
    const modal = getEl('subscription-modal');
    if (modal) modal.classList.add('active');
    updateSubscriptionModal();
}

function openSubscriptionModal() {
    closeModals();
    const modal = getEl('subscription-modal');
    if (!modal) return;
    const planName = getEl('sub-plan-name')?.innerText || 'Free';
    const isFree = planName.toLowerCase().includes('free');
    const content = getEl('subscription-content');
    if (content) {
        if (isFree) {
            content.innerHTML = `
                <div style="padding:20px 0;text-align:center;">
                    <span class="material-symbols-rounded" style="font-size:48px;color:var(--accent-glow);">rocket_launch</span>
                    <h3 style="color:var(--text-main);margin:12px 0;">You are on the Free Plan</h3>
                    <p style="color:var(--text-muted);font-size:14px;">Unlock unlimited extractions, UI generations, and priority support.</p>
                </div>
            `;
        } else {
            content.innerHTML = `
                <div style="display:flex;flex-direction:column;gap:5px;">
                    <div class="profile-stat-row"><span class="profile-stat-label">Current Plan</span><span class="profile-stat-value" style="color:var(--text-main);">${planName}</span></div>
                </div>
            `;
        }
    }
    modal.classList.add('active');
    updateSubscriptionModal();
}

// ============================================================
// ADMIN MODAL
// ============================================================
async function openAdminModal() {
    closeModals();
    const modal = getEl('admin-modal');
    if (modal) modal.classList.add('active');
    const container = getEl('admin-metrics-container');
    if (!container) return;
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/admin/metrics`);
        if (resp.ok) {
            const data = await resp.json();
            let providerRows = '';
            for (const [p, status] of Object.entries(data.providerStatus)) {
                const daily = data.aiQuota[`daily${p.charAt(0).toUpperCase() + p.slice(1)}`] || 0;
                const total = data.aiQuota[p] || 0;
                const limit = data.aiQuota[`${p}Limit`] || 'N/A';
                providerRows += `
                    <div class="profile-stat-row">
                        <span class="profile-stat-label">${p.toUpperCase()}</span>
                        <span class="profile-stat-value">Daily: ${daily}/${limit} | Total: ${total} | Status: ${status.status}</span>
                    </div>
                `;
            }
            container.innerHTML = `
                <div style="margin-bottom:10px;font-weight:600;color:var(--text-main);">AI Provider Usage</div>
                ${providerRows}
                <div style="border-top:1px solid var(--border-muted);margin:10px 0;"></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Total Users</span><span class="profile-stat-value">${data.totalUsers}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Pro Subscribers</span><span class="profile-stat-value" style="color:var(--accent-glow-pro)">${data.proUsers}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Business Subscribers</span><span class="profile-stat-value" style="color:var(--accent-glow-designer)">${data.businessUsers}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Total Chats</span><span class="profile-stat-value">${data.totalChats}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Today's Queries</span><span class="profile-stat-value">${data.dailyQueries || 0}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Total Storage Used</span><span class="profile-stat-value">${data.metrics?.totalBytesMB || 0} MB</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Last Updated</span><span class="profile-stat-value" style="font-size:12px;">${new Date(data.timestamp).toLocaleString()}</span></div>
            `;
        } else {
            container.innerHTML = `<div style="color:#ef4444;text-align:center;">Unauthorized or service unavailable.</div>`;
        }
    } catch (e) {
        console.error('Admin fetch error:', e);
        container.innerHTML = `<div style="color:#ef4444;text-align:center;">Network error. Check your connection.</div>`;
    }
}

// ============================================================
// CHECKOUT PIPELINE
// ============================================================
async function dispatchCheckoutPipeline(targetBaseTier) {
    const selectedRadio = document.querySelector(`input[name="${targetBaseTier}-sub-selector"]:checked`);
    if (!selectedRadio) {
        alert("Please select a plan option.");
        return;
    }
    const selectedSubConfig = selectedRadio.value;
    const checkoutBtn = document.querySelector(`.${targetBaseTier}-premium .upgrade-btn`);
    if (!checkoutBtn) return;
    const originalText = checkoutBtn.innerText;
    checkoutBtn.innerText = "Connecting...";
    checkoutBtn.style.opacity = "0.7";
    checkoutBtn.disabled = true;

    showStripeLoading(() => {
        const modal = getEl('subscription-modal');
        if (modal) modal.classList.add('active');
    });

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/billing/checkout`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tier: targetBaseTier, subTier: selectedSubConfig }),
            signal: controller.signal
        });
        clearTimeout(timeoutId);
        if (!response.ok) {
            let errorMsg = `HTTP ${response.status}`;
            try {
                const errData = await response.json();
                errorMsg = errData.message || errorMsg;
            } catch (_) {}
            throw new Error(errorMsg);
        }
        const data = await response.json();
        if (data.url) {
            window.location.href = data.url;
        } else {
            throw new Error(data.message || "No checkout URL returned.");
        }
    } catch (e) {
        console.error("Checkout error:", e);
        hideStripeLoading();
        let userMsg = "Checkout Failed";
        if (e.name === 'AbortError') {
            userMsg = "Request timed out. Please try again.";
        } else if (e.message) {
            userMsg = e.message;
        }
        alert(`⚠️ ${userMsg}`);
        checkoutBtn.innerText = "Retry";
        checkoutBtn.disabled = false;
        checkoutBtn.style.opacity = "1";
        checkoutBtn.onclick = () => dispatchCheckoutPipeline(targetBaseTier);
    } finally {
        clearTimeout(timeoutId);
    }
}

// ============================================================
// OTHER FUNCTIONS
// ============================================================
async function saveCustomInstructions() {
    const input = getEl('instructions-input');
    if (!input) return;
    const btn = document.querySelector('#instructions-modal .modal-submit-btn');
    if (!btn) return;
    const originalText = btn.innerText;
    btn.innerText = "Saving...";
    btn.disabled = true;
    try {
        await apiFetch(`${API_BASE_URL}/api/user/instructions`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ instructions: input.value })
        });
        btn.innerText = "Saved!";
        btn.style.borderColor = "var(--accent-glow)";
        setTimeout(() => {
            closeModals();
            btn.innerText = originalText;
            btn.style.borderColor = "";
            btn.disabled = false;
        }, 800);
    } catch (error) {
        showToast("Failed to save instructions: " + error.message, "error");
        btn.innerText = originalText;
        btn.disabled = false;
    }
}

async function submitTelemetryReport() {
    const typeSelect = getEl('report-type');
    const descInput = getEl('feedback-input');
    if (!typeSelect || !descInput) return;
    const type = typeSelect.value;
    const description = descInput.value.trim();
    if (!description) return;
    const btn = document.querySelector('#feedback-modal .modal-submit-btn');
    if (!btn) return;
    const originalText = btn.innerText;
    btn.innerText = "Dispatching...";
    btn.disabled = true;
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/reports`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ type, description })
        });
        if (response.ok) {
            btn.innerText = "Log Secured";
            btn.style.background = "var(--accent-secondary)";
            btn.style.color = "#fff";
            setTimeout(() => {
                closeModals();
                descInput.value = '';
                btn.innerText = originalText;
                btn.style.background = "#fff";
                btn.style.color = "#000";
                btn.disabled = false;
            }, 1500);
        }
    } catch (error) {
        btn.innerText = originalText;
        btn.disabled = false;
    }
}

function openPrivacyModal() {
    closeModals();
    const modal = document.createElement('div');
    modal.className = 'modal-overlay active';
    modal.id = 'privacy-modal';
    modal.innerHTML = `
        <div class="modal-card">
            <div class="modal-header">
                <div class="modal-title">Privacy &amp; Security</div>
                <button class="close-modal-btn" onclick="closeModals()">✕</button>
            </div>
            <div style="padding:10px 0;">
                <h4 style="color:var(--text-main);margin-bottom:6px;">Data Protection</h4>
                <p style="color:var(--text-muted);font-size:14px;line-height:1.6;">Your data is encrypted and never shared with third parties.</p>
                <hr style="border-color:var(--border-muted);margin:14px 0;">
                <h4 style="color:var(--text-main);margin-bottom:6px;">Cookies</h4>
                <p style="color:var(--text-muted);font-size:14px;line-height:1.6;">We use essential cookies only. Manage in your browser settings.</p>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

function openHelpCenter() {
    closeModals();
    const modal = document.createElement('div');
    modal.className = 'modal-overlay active';
    modal.id = 'help-modal';
    modal.innerHTML = `
        <div class="modal-card">
            <div class="modal-header">
                <div class="modal-title">Help Center</div>
                <button class="close-modal-btn" onclick="closeModals()">✕</button>
            </div>
            <div style="padding:10px 0;">
                <details style="margin-bottom:12px;color:var(--text-main);"><summary style="font-weight:600;cursor:pointer;padding:8px 0;">How do I upload files?</summary><p style="color:var(--text-muted);padding:8px 0 0 16px;">Use the attach button next to the input box.</p></details>
                <details style="margin-bottom:12px;color:var(--text-main);"><summary style="font-weight:600;cursor:pointer;padding:8px 0;">What is the free tier limit?</summary><p style="color:var(--text-muted);padding:8px 0 0 16px;">5 extractions/generations per day.</p></details>
                <details style="margin-bottom:12px;color:var(--text-main);"><summary style="font-weight:600;cursor:pointer;padding:8px 0;">How to upgrade?</summary><p style="color:var(--text-muted);padding:8px 0 0 16px;">Go to Settings → Workspace Plan.</p></details>
                <details style="margin-bottom:12px;color:var(--text-main);"><summary style="font-weight:600;cursor:pointer;padding:8px 0;">Contact Support</summary><p style="color:var(--text-muted);padding:8px 0 0 16px;">Email: support@axelr.in</p></details>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

function addAnotherAccount() {
    if (confirm('Switch to another Google account?')) {
        localStorage.removeItem('google_auth_token');
        location.reload();
    }
}

function syncTierMatrixEngine(tierGroup, subTierSelection, derivedCostValue) {
    const targetOutputNode = getEl(`${tierGroup}-base-price-output`);
    if (targetOutputNode) {
        targetOutputNode.innerHTML = `$${derivedCostValue}<span style="font-size:14px;color:#555;font-weight:400;">/mo</span>`;
    }
}

async function deleteAllChats() {
    if (!confirm("⚠️ Are you sure you want to permanently delete ALL your chat history? This cannot be undone.")) return;
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/history/delete-all`, {
            method: 'DELETE'
        });
        if (response.ok) {
            alert("All chats have been permanently deleted.");
            resetToNewChat();
            loadArchiveLogs();
        } else {
            alert("Failed to delete chats. Please try again.");
        }
    } catch (e) {
        alert("Network error. Please check your connection.");
    }
}

async function deleteAccount() {
    if (!confirm("⚠️ Do you really want to delete your account? This will permanently remove all your data and cannot be reversed.")) return;
    if (!confirm("Are you absolutely sure? This action is irreversible.")) return;
    try {
        const response = await apiFetch(`${API_BASE_URL}/api/user/delete`, {
            method: 'DELETE'
        });
        if (response.ok) {
            alert("Your account has been deleted. You will be logged out.");
            executeGlobalLogout();
        } else {
            alert("Failed to delete account. Please try again.");
        }
    } catch (e) {
        alert("Network error. Please check your connection.");
    }
}

function updateSubscriptionModal() {
    const planName = getEl('sub-plan-name')?.innerText || 'Free';
    const freeMsg = getEl('free-tier-message');
    const paidMsg = getEl('paid-tier-message');
    const paidName = getEl('paid-tier-name');
    const detailsSpan = getEl('subscription-details');
    if (planName.toLowerCase().includes('free')) {
        if (freeMsg) freeMsg.style.display = 'block';
        if (paidMsg) paidMsg.style.display = 'none';
    } else {
        if (freeMsg) freeMsg.style.display = 'none';
        if (paidMsg) paidMsg.style.display = 'block';
        if (paidName) paidName.innerText = planName.replace(' ALLOCATION', '');
        if (detailsSpan) detailsSpan.innerText = 'Active subscription. Manage your plan via Stripe.';
    }
}

// ============================================================
// SIDEBAR SWIPE
// ============================================================
const sidebarEl = getEl('sidebar-container-node');
if (sidebarEl) {
    const swipeHandleEl = document.createElement('div');
    swipeHandleEl.className = 'swipe-handle';
    sidebarEl.prepend(swipeHandleEl);
    let startXPos = 0, currentXPos = 0, isDraggingSidebar = false;
    swipeHandleEl.addEventListener('touchstart', (e) => {
        startXPos = e.touches[0].clientX;
        isDraggingSidebar = true;
    });
    document.addEventListener('touchmove', (e) => {
        if (!isDraggingSidebar) return;
        currentXPos = e.touches[0].clientX;
        const delta = currentXPos - startXPos;
        if (sidebarEl.classList.contains('open') && delta < 0) {
            sidebarEl.style.transition = 'none';
            sidebarEl.style.left = `${Math.min(0, delta)}px`;
        }
    });
    document.addEventListener('touchend', () => {
        if (!isDraggingSidebar) return;
        isDraggingSidebar = false;
        sidebarEl.style.transition = 'all 0.3s ease';
        if (sidebarEl.classList.contains('open')) {
            const currentLeft = parseFloat(sidebarEl.style.left) || 0;
            if (currentLeft < -50) sidebarEl.classList.remove('open');
            sidebarEl.style.left = '';
        }
        startXPos = 0;
        currentXPos = 0;
    });
    let isMouseDownSidebar = false;
    swipeHandleEl.addEventListener('mousedown', (e) => {
        isMouseDownSidebar = true;
        startXPos = e.clientX;
        e.preventDefault();
    });
    document.addEventListener('mousemove', (e) => {
        if (!isMouseDownSidebar) return;
        currentXPos = e.clientX;
        const delta = currentXPos - startXPos;
        if (sidebarEl.classList.contains('open') && delta < 0) {
            sidebarEl.style.transition = 'none';
            sidebarEl.style.left = `${delta}px`;
        }
    });
    document.addEventListener('mouseup', () => {
        if (!isMouseDownSidebar) return;
        isMouseDownSidebar = false;
        sidebarEl.style.transition = 'all 0.3s ease';
        if (sidebarEl.classList.contains('open')) {
            const currentLeft = parseFloat(sidebarEl.style.left) || 0;
            if (currentLeft < -50) sidebarEl.classList.remove('open');
            sidebarEl.style.left = '';
        }
    });
}

function toggleTheme() {
    document.body.classList.toggle('light-theme');
    localStorage.setItem('axelr_theme', document.body.classList.contains('light-theme') ? 'light' : 'dark');
}
if (localStorage.getItem('axelr_theme') === 'light') {
    document.body.classList.add('light-theme');
}

function adjustViewportPadding() {
    if (!commandWrapper) return;
    const wrapperHeight = commandWrapper.offsetHeight;
    const fileChips = getEl('file-staging-container');
    const chipsHeight = fileChips && stagedFiles.length > 0 ? fileChips.offsetHeight : 0;
    let totalPadding = wrapperHeight + 20;
    if (chipsHeight > 0) totalPadding += chipsHeight + 10;
    totalPadding = Math.max(totalPadding, 120);
    if (viewport) viewport.style.paddingBottom = totalPadding + 'px';
}

let resizeHandlerTimeout = null;
let isResizeHandling = false;
window.addEventListener('resize', () => {
    if (isResizeHandling) return;
    isResizeHandling = true;
    clearTimeout(resizeHandlerTimeout);
    resizeHandlerTimeout = setTimeout(() => {
        if (document.body.classList.contains('workspace-data') || document.body.classList.contains('workspace-design') || document.body.classList.contains('workspace-general')) {
            adjustViewportPadding();
            renderFileChips();
        }
        isResizeHandling = false;
    }, 150);
});
// Multi-Agent Chat
async function runMultiAgent(task, agents) {
    const btn = document.getElementById('multi-agent-btn');
    if (btn) { btn.disabled = true; btn.innerHTML = 'Spawning agents...'; }
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/agents/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ task, agents, workspace: getWorkspace() })
        });
        const data = await resp.json();
        if (data.success) {
            const bubble = document.createElement('div');
            bubble.className = 'chat-bubble nexus-bubble';
            const avatarDiv = document.createElement('div');
            avatarDiv.className = 'ai-avatar-bubble';
            avatarDiv.innerHTML = AXELR_AVATAR_SVG;
            bubble.appendChild(avatarDiv);
            const contentDiv = document.createElement('div');
            contentDiv.className = 'bubble-content';
            contentDiv.style.flex = '1';
            contentDiv.innerHTML = DOMPurify.sanitize(marked.parse(data.combined));
            bubble.appendChild(contentDiv);
            viewport.appendChild(bubble);
            scrollToBottom();
        } else {
            showToast('Multi-agent failed: ' + (data.message || 'Unknown error'), 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '🧠 Agents'; }
    }
}
// Knowledge Vault functions
async function saveKnowledge(key, value, tags = []) {
    try {
        await apiFetch(`${API_BASE_URL}/api/knowledge`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key, value, tags })
        });
        showToast('Knowledge saved', 'success');
    } catch (e) {
        showToast('Save failed: ' + e.message);
    }
}
async function loadKnowledge() {
    const resp = await apiFetch(`${API_BASE_URL}/api/knowledge`);
    const data = await resp.json();
    // Display in sidebar or modal
    const container = document.getElementById('knowledge-list');
    if (container) {
        container.innerHTML = data.knowledge.map(k => 
            `<div class="knowledge-item"><strong>${k.key}</strong>: ${k.value}</div>`
        ).join('');
    }
}
// Auto-inject knowledge into prompts (modified executeCommand)
// In executeCommand, before sending, fetch relevant knowledge and add to context.
// We'll add a function to get knowledge for context.
async function getRelevantKnowledge(query) {
    const resp = await apiFetch(`${API_BASE_URL}/api/knowledge/search?q=${encodeURIComponent(query)}`);
    const data = await resp.json();
    return data.results.map(k => `${k.key}: ${k.value}`).join('\n');
}
// Modify executeCommand to include knowledge:
// After building formData, add a field 'context' with knowledge.
async function runWorkflow(steps) {
    const response = await fetch(`${API_BASE_URL}/api/workflow/run`, {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${await ensureValidToken()}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ steps, workspace: getWorkspace() })
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        for (let i = 0; i < events.length - 1; i++) {
            const event = events[i];
            if (event.startsWith('data: ')) {
                try {
                    const json = JSON.parse(event.slice(6));
                    if (json.step && json.status === 'completed') {
                        const stepBubble = document.createElement('div');
                        stepBubble.className = 'chat-bubble nexus-bubble';
                        stepBubble.innerHTML = `<div class="bubble-content"><strong>${json.step}</strong><br>${DOMPurify.sanitize(marked.parse(json.output))}</div>`;
                        viewport.appendChild(stepBubble);
                        scrollToBottom();
                    } else if (json.status === 'done') {
                        const finalBubble = document.createElement('div');
                        finalBubble.className = 'chat-bubble nexus-bubble';
                        finalBubble.innerHTML = `<div class="bubble-content"><strong>✅ Workflow Complete</strong><br>${DOMPurify.sanitize(marked.parse(json.final))}</div>`;
                        viewport.appendChild(finalBubble);
                        scrollToBottom();
                    }
                } catch (e) { /* ignore malformed JSON */ }
            }
        }
        buffer = events[events.length - 1];
    }
}
async function summarizeCurrentChat() {
    if (!activeSessionId) return alert('No active chat to summarize.');
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/summarize-chat?session_id=${activeSessionId}`, { method: 'POST' });
        const data = await resp.json();
        if (data.success) {
            const bubble = document.createElement('div');
            bubble.className = 'chat-bubble nexus-bubble';
            bubble.innerHTML = `<div class="bubble-content"><strong>📋 Summary</strong><br>${DOMPurify.sanitize(marked.parse(data.summary))}</div>`;
            viewport.appendChild(bubble);
            scrollToBottom();
        } else {
            showToast('Summary failed: ' + (data.message || 'Unknown error'), 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}
// Add a button in the chat header
async function executeCodeBlock(code, language) {
    const resp = await apiFetch(`${API_BASE_URL}/api/execute-code`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ language, code })
    });
    const data = await resp.json();
    if (data.success) {
        // Display output in a new bubble
        const bubble = document.createElement('div');
        bubble.className = 'chat-bubble nexus-bubble';
        bubble.innerHTML = `<div class="bubble-content"><strong>▶️ Output</strong><pre>${escapeHtmlEntities(data.output)}</pre></div>`;
        viewport.appendChild(bubble);
        scrollToBottom();
    } else {
        alert('Execution error: ' + data.error);
    }
}
// Add a "Run" button next to code blocks (modify marked renderer)
async function loadPersonas() {
    const resp = await apiFetch(`${API_BASE_URL}/api/personas`);
    const data = await resp.json();
    // Populate a dropdown in the model selection area
    const container = document.getElementById('persona-select');
    if (container) {
        container.innerHTML = data.personas.map(p => 
            `<option value="${p._id}">${p.name}</option>`
        ).join('');
    }
}
async function applyPersona(personaId) {
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/personas/${personaId}`);
        if (!resp.ok) throw new Error('Persona not found');
        const persona = await resp.json();
        if (persona.system_prompt) {
            await apiFetch(`${API_BASE_URL}/api/user/instructions`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ instructions: persona.system_prompt })
            });
            showToast('Persona "' + persona.name + '" applied!', 'success');
            await loadUserProfile(); // refresh to reflect
            closeModals();
        } else {
            showToast('Persona has no system prompt.', 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}
let eventSource = null;
function joinCollaborativeSession(sessionId) {
    if (window._eventSource) { window._eventSource.close(); }
    const es = new EventSource(`${API_BASE_URL}/api/session/${sessionId}/stream`);
    window._eventSource = es;
    es.onmessage = function(event) {
        try {
            const data = JSON.parse(event.data);
            if (data.type === 'new_message') {
                const msg = data.message;
                const bubble = document.createElement('div');
                bubble.className = `chat-bubble ${msg.role === 'user' ? 'user-bubble' : 'nexus-bubble'}`;
                // Render message (simplified)
                const contentDiv = document.createElement('div');
                contentDiv.className = 'bubble-content';
                contentDiv.innerHTML = DOMPurify.sanitize(marked.parse(msg.text || ''));
                bubble.appendChild(contentDiv);
                viewport.appendChild(bubble);
                scrollToBottom();
            }
        } catch (e) { /* ignore */ }
    };
    es.onerror = function() {
        es.close();
        window._eventSource = null;
    };
}
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js');
}
function enableInlineEdit(bubbleContent, originalText, sessionId, msgId) {
    // Remove any existing edit mode
    const existing = bubbleContent.querySelector('.inline-edit-area');
    if (existing) return;
    const contentDiv = bubbleContent.closest('.bubble-content');
    const currentHTML = contentDiv.innerHTML;
    const textarea = document.createElement('textarea');
    textarea.className = 'inline-edit-area';
    textarea.value = originalText;
    textarea.style.width = '100%';
    textarea.style.minHeight = '120px';
    textarea.style.background = 'var(--bg-input)';
    textarea.style.color = 'var(--text-main)';
    textarea.style.border = '1px solid var(--border-muted)';
    textarea.style.borderRadius = '6px';
    textarea.style.padding = '8px';
    textarea.style.fontFamily = 'inherit';
    textarea.style.fontSize = '14px';
    contentDiv.innerHTML = '';
    contentDiv.appendChild(textarea);
    const actions = document.createElement('div');
    actions.style.marginTop = '8px';
    actions.innerHTML = `
        <button class="action-icon-btn" style="background:var(--accent-glow);color:#000;border:none;padding:6px 12px;border-radius:4px;">Submit Refinement</button>
        <button class="action-icon-btn" style="margin-left:8px;">Cancel</button>
    `;
    contentDiv.appendChild(actions);
    const submitBtn = actions.querySelector('button:first-child');
    const cancelBtn = actions.querySelector('button:last-child');
    submitBtn.onclick = async function() {
        const newText = textarea.value.trim();
        if (!newText) return;
        this.disabled = true;
        this.innerText = 'Processing...';
        try {
            const resp = await apiFetch(`${API_BASE_URL}/api/refine-response`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sessionId, msgId, newText, originalText })
            });
            const data = await resp.json();
            if (data.success) {
                // Replace the current bubble with refined response
                const parent = contentDiv.parentNode;
                const newBubble = document.createElement('div');
                newBubble.className = 'chat-bubble nexus-bubble';
                newBubble.innerHTML = `<div class="ai-avatar-bubble">${AXELR_AVATAR_SVG}</div><div class="bubble-content">${DOMPurify.sanitize(marked.parse(data.refined))}</div>`;
                parent.replaceWith(newBubble);
                // Re‑inject action buttons
                const newContent = newBubble.querySelector('.bubble-content');
                injectActionButtons(newContent, data.refined, false, false, null, sessionId, false, false);
                scrollToBottom();
            } else {
                showToast('Refinement failed: ' + (data.message || 'Unknown error'), 'error');
                this.disabled = false;
                this.innerText = 'Submit Refinement';
            }
        } catch (e) {
            showToast('Error: ' + e.message, 'error');
            this.disabled = false;
            this.innerText = 'Submit Refinement';
        }
    };
    cancelBtn.onclick = function() {
        contentDiv.innerHTML = currentHTML;
        if (sessionId) viewPastLogById(sessionId);
        else location.reload();
    };
}
// Add endpoint /api/refine-response in backend
async function loadKnowledgeList() {
    const data = await (await apiFetch(`${API_BASE_URL}/api/knowledge`)).json();
    const container = document.getElementById('knowledge-list');
    if (container) {
        container.innerHTML = data.knowledge.map(k => 
            `<div class="knowledge-item" title="${escapeHtmlEntities(k.value)}">
                <strong>${escapeHtmlEntities(k.key)}</strong>
                <button onclick="deleteKnowledge('${k._id}')">✕</button>
            </div>`
        ).join('');
    }
}
const renderer = new marked.Renderer();
renderer.code = function(code, language) {
    const runBtn = (language === 'python' || language === 'javascript' || language === 'html') 
        ? `<button class="run-code-btn" onclick="executeCodeBlock(this, '${language}')">▶ Run</button>` 
        : '';
    return `<pre>${runBtn}<button class="copy-code-btn" onclick="navigator.clipboard.writeText(this.nextElementSibling.innerText); this.innerText='Copied!'; setTimeout(()=>this.innerText='Copy Code', 2000)">Copy Code</button><code>${code.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</code></pre>`;
};
marked.setOptions({ renderer, breaks: true });

// Global function to execute
async function executeCodeBlock(btn, language) {
    const pre = btn.closest('pre');
    const code = pre ? pre.querySelector('code') : null;
    if (!code) return;
    const codeText = code.innerText;
    btn.innerText = 'Running…';
    btn.disabled = true;
    try {
        const resp = await apiFetch(`${API_BASE_URL}/api/execute-code`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ language, code: codeText })
        });
        const data = await resp.json();
        btn.innerText = '▶ Run';
        btn.disabled = false;
        const outputBubble = document.createElement('div');
        outputBubble.className = 'chat-bubble nexus-bubble';
        const avatar = document.createElement('div');
        avatar.className = 'ai-avatar-bubble';
        avatar.innerHTML = AXELR_AVATAR_SVG;
        outputBubble.appendChild(avatar);
        const contentDiv = document.createElement('div');
        contentDiv.className = 'bubble-content';
        const outputPre = document.createElement('pre');
        outputPre.style.whiteSpace = 'pre-wrap';
        outputPre.textContent = data.output || data.error || 'No output';
        contentDiv.innerHTML = `<strong>▶️ Output</strong>`;
        contentDiv.appendChild(outputPre);
        outputBubble.appendChild(contentDiv);
        viewport.appendChild(outputBubble);
        scrollToBottom();
    } catch (e) {
        showToast('Execution failed: ' + e.message, 'error');
        btn.innerText = '▶ Run';
        btn.disabled = false;
    }
}
// In the model dropdown card, add a section for personas
async function loadPersonaDropdown() {
    const data = await (await apiFetch(`${API_BASE_URL}/api/personas`)).json();
    const container = document.getElementById('persona-select');
    if (!container) return;
    container.innerHTML = data.personas.map(p => 
        `<option value="${p._id}">${p.name}</option>`
    ).join('');
}
// Call on UI load
// After rendering chat, if the session is active, join collaboration
// ============================================================
// VERSION & CACHE CONTROL
// ============================================================
const APP_VERSION = '24.3';
const BUILD_DATE = '2026-08-17';
console.log(`🟢 Axelr AI v${APP_VERSION} (Build: ${BUILD_DATE})`);
console.log('📡 API Base URL:', API_BASE_URL);

const storedVersion = localStorage.getItem('axelr_app_version');
if (storedVersion && storedVersion !== APP_VERSION) {
    console.log(`🔄 Version mismatch: stored=${storedVersion}, current=${APP_VERSION}. No cache cleared.`);
} else if (!storedVersion) {
    localStorage.setItem('axelr_app_version', APP_VERSION);
}

// ============================================================
// SERVICE WORKER CLEANUP
// ============================================================
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations()
        .then(registrations => {
            for (let reg of registrations) {
                reg.unregister();
                console.log('🧹 Unregistered service worker:', reg.scope);
            }
        })
        .catch(err => console.warn('SW cleanup error:', err));
}

// ============================================================
// PUTER OPT-IN & DYNAMIC LOADING
// ============================================================
// ============================================================
// PUTER OPT-IN & DYNAMIC LOADING (FIXED)
// ============================================================
let puterSDKLoaded = false;
let puterAuthAttempted = false;

async function togglePuter(enabled) {
    const toggle = document.getElementById('puter-toggle');
    const desc = document.getElementById('puter-desc');
    
    if (enabled) {
        try {
            if (typeof puter === 'undefined') {
                await loadPuterSDK();
            }
            if (typeof puter !== 'undefined' && puter.auth) {
                const result = await puter.auth.signIn();
                if (result && result.user) {
                    const resp = await apiFetch(`${API_BASE_URL}/api/user/puter-toggle`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ enabled: true })
                    });
                    if (resp.ok) {
                        showToast('✅ Puter AI enabled successfully!', 'success');
                        desc.innerText = 'Puter enabled';
                        localStorage.setItem('puter_enabled', 'true');
                    }
                } else {
                    throw new Error('Authentication cancelled or failed');
                }
            }
        } catch (e) {
            console.error('Puter toggle error:', e);
            showToast('⚠️ Puter authentication failed: ' + e.message, 'error');
            toggle.checked = false;
            desc.innerText = 'Puter disabled';
            localStorage.setItem('puter_enabled', 'false');
        }
    } else {
        const resp = await apiFetch(`${API_BASE_URL}/api/user/puter-toggle`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: false })
        });
        if (resp.ok) {
            showToast('Puter AI disabled.', 'info');
            desc.innerText = 'Puter disabled';
            localStorage.setItem('puter_enabled', 'false');
        }
    }
}

function loadPuterSDK() {
    return new Promise((resolve, reject) => {
        if (typeof puter !== 'undefined') {
            resolve();
            return;
        }
        const script = document.createElement('script');
        script.src = 'https://js.puter.com/v2/';
        script.onload = () => {
            setTimeout(resolve, 500);
        };
        script.onerror = reject;
        document.head.appendChild(script);
    });
}

// On page load, restore state
document.addEventListener('DOMContentLoaded', function() {
    const puterEnabled = localStorage.getItem('puter_enabled') === 'true';
    const toggle = document.getElementById('puter-toggle');
    const desc = document.getElementById('puter-desc');
    if (toggle) toggle.checked = puterEnabled;
    if (desc) desc.innerText = puterEnabled ? 'Puter enabled' : 'Puter disabled';
});
// Ensure marked and DOMPurify are available
if (typeof marked === 'undefined') {
    window.marked = { parse: (text) => text };
    console.warn('marked not loaded, using plain text fallback');
}
if (typeof DOMPurify === 'undefined') {
    window.DOMPurify = { sanitize: (text) => text };
    console.warn('DOMPurify not loaded, using raw text fallback');
}

function renderInlineWorkspaceCards() {
    const host = getEl('workspace-cards-inline');   // add this <div> in your settings HTML
    if (!host) return;

    const current = getWorkspace();
    const workspaces = [
        { id: 'data',    icon: 'database',       title: 'Data',    desc: 'Extract, analyse & transform' },
        { id: 'design',  icon: 'palette',        title: 'Design',  desc: 'UI/UX generation & deployment' },
        { id: 'general', icon: 'auto_awesome',   title: 'General', desc: 'Everyday AI assistance' },
    ];

    host.innerHTML = workspaces.map(w => `
        <div class="ws-card ${w.id === current ? 'active' : ''}"
             onclick="pickInlineWorkspace('${w.id}')">
            <span class="material-symbols-rounded">${w.icon}</span>
            <div>
                <div class="ws-card-title">${w.title}</div>
                <div class="ws-card-desc">${w.desc}</div>
            </div>
            ${w.id === current ? '<span class="material-symbols-rounded ws-check">check_circle</span>' : ''}
        </div>
    `).join('');
}

function pickInlineWorkspace(type) {
    localStorage.setItem('Axelr_workspace', type);
    renderInlineWorkspaceCards();       // re-paint instantly, in place
    activateWorkspace(type, true);
    showToast(`Switched to ${type} workspace`, 'success');
}
// ============================================================
// FINAL INIT
// ============================================================
if (localStorage.getItem('google_auth_token')) {
    loadUserProfile().then(() => loadArchiveLogs()).then(() => {
        const storedSessionId = localStorage.getItem('axelr_active_session');
        if (storedSessionId) viewPastLogById(storedSessionId);
        setTimeout(setupViewportObserver, 500);
    });
}

window.onerror = function(message, source, lineno, colno, error) {
    console.error('Global error:', message, error);
    return true;
};