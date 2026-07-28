// ============================================================
// CONFIGURATION
// ============================================================
const API_BASE_URL = window.location.hostname === "localhost" ? "http://localhost:5000" :
    "https://zelrex-backend.onrender.com";

const AXELR_AVATAR_SVG =
    `<svg viewBox="0 0 100 100" width="22" height="22" fill="none" xmlns="http://www.w3.org/2000/svg"><path d="M50 15 L20 32.5 L20 67.5 L50 85" stroke="#ffffff" stroke-width="6" stroke-linejoin="bevel" fill="rgba(255,255,255,0.05)"/><path d="M50 15 L80 32.5 L50 50 L80 67.5 L50 85" stroke="currentColor" stroke-width="6" stroke-linejoin="bevel" fill="none"/><path d="M20 32.5 L50 50 L20 67.5" stroke="#ffffff" stroke-width="3" stroke-linejoin="bevel" opacity="0.5"/></svg>`;

const ICONS = {
    copy: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`,
    edit: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>`,
    thumbsUp: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4M7 10H4a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3"/></svg>`,
    thumbsDown: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V2M7 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20M17 14h3a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2h-3"/></svg>`,
    regenerate: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 3v6h-6"/><path d="M3 21v-6h6"/><path d="M18.364 5.636a9 9 0 1 1-12.728 12.728"/></svg>`,
    stop: `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>`,
    leftArrow: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>`,
    rightArrow: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>`,
    moreVertical: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="12" cy="19" r="1"/></svg>`
};

const SIDEBAR_ICONS = {
    edit: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>`,
    link: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>`,
    delete: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`,
    inventory: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="7" width="20" height="14" rx="2" ry="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>`,
    undo: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/></svg>`,
    restore: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 7h-3a5 5 0 0 0-5-5 5 5 0 0 0-5 5H4"/><path d="M4 7v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7"/><line x1="12" y1="12" x2="12" y2="16"/><line x1="8" y1="12" x2="8" y2="12"/></svg>`,
    delete_forever: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>`,
    push_pin: `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"/><path d="M5 12h14"/><path d="M7 12v-2a5 5 0 0 1 10 0v2"/><circle cx="12" cy="7" r="4"/></svg>`
};

// ============================================================
// DOM REFS
// ============================================================
const promptInput = document.getElementById('prompt-input');
const fileInput = document.getElementById('omni-file-input');
const fileStagingContainer = document.getElementById('file-staging-container');
const viewport = document.getElementById('viewport');
const historyListContainer = document.getElementById('history-list-container');
const accountDropdownCard = document.getElementById('account-dropdown-card');
const modelDropdownCard = document.getElementById('model-dropdown-card');
const sidebarTriggerArea = document.getElementById('sidebar-trigger-area');
const sidebarNode = document.getElementById('sidebar-container-node');
const sendBtn = document.getElementById('send-trigger');
const commandWrapper = document.getElementById('command-wrapper');
const mainWrapper = document.getElementById('content-mask');

// ============================================================
// STATE
// ============================================================
let stagedFiles = [];
let cachedLogHistory = [];
let activeSessionId = null;
let runningStructuredCache = null;
let runningFileTitle = 'Export.csv';
let googleAuthUserToken = null;
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

// ============================================================
// SCROLL FUNCTIONS - DEFINED EARLY
// ============================================================
function scrollToBottom(smooth = true) {
    if (!viewport) return;
    if (isUserScrolling) return;
    const lastMessage = viewport.querySelector('.chat-bubble:last-child');
    if (lastMessage) {
        try {
            lastMessage.scrollIntoView({ 
                behavior: smooth ? 'smooth' : 'auto', 
                block: 'end' 
            });
        } catch (e) {
            viewport.scrollTop = viewport.scrollHeight;
        }
    }
}

function updateViewportAfterRender() {
    if (window._viewportUpdateId) {
        cancelAnimationFrame(window._viewportUpdateId);
    }
    window._viewportUpdateId = requestAnimationFrame(() => {
        if (!isUserScrolling) {
            scrollToBottom(true);
        }
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
        if (content.startsWith("<") && (content.includes("<html") || content.includes("<!DOCTYPE") || content
                .includes("<div") || content.includes("<body"))) {
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
    const desc = document.getElementById('theme-desc');
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
    if (currentThemePreference === 'system') {
        applyTheme(systemDark ? 'dark' : 'light');
    }
});

// ============================================================
// SINGLE INITIALIZATION POINT
// ============================================================
let appInitialized = false;

function initializeApp() {
    if (appInitialized) return;
    appInitialized = true;

    // Theme
    const saved = localStorage.getItem('axelr_theme') || 'system';
    currentThemePreference = saved;
    systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (saved === 'system') {
        applyTheme(systemDark ? 'dark' : 'light');
    } else {
        applyTheme(saved);
    }

    // Auth check
    const savedToken = localStorage.getItem('google_auth_token');
    if (savedToken) {
        try {
            const payload = decodeJwt(savedToken);
            if (Date.now() < payload.exp * 1000) {
                initializeSecureWorkspace(payload, savedToken);
                return;
            }
        } catch (e) {
            localStorage.removeItem('google_auth_token');
        }
    }

    // Show auth wall
    document.getElementById('auth-wall').style.display = 'flex';
}

// Run once when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initializeApp);
} else {
    initializeApp();
}

// ============================================================
// VIEWPORT & KEYBOARD ADJUSTMENT - FIXED
// ============================================================
function adjustCommandWrapperAndViewport() {
    const vv = window.visualViewport;
    if (!vv) return;
    
    const offsetY = window.innerHeight - vv.height;
    const maxBottom = Math.min(offsetY, window.innerHeight * 0.4);
    
    const currentBottom = parseFloat(commandWrapper.style.bottom || '0');
    if (Math.abs(currentBottom - maxBottom) > 3) {
        commandWrapper.style.bottom = maxBottom + 'px';
    }
    
    const fileChips = document.getElementById('file-staging-container');
    const fileChipsHeight = fileChips && stagedFiles.length > 0 ? fileChips.offsetHeight : 0;
    const availableHeight = window.innerHeight - maxBottom - 20 - fileChipsHeight;
    commandWrapper.style.maxHeight = Math.min(availableHeight, window.innerHeight * 0.8) + 'px';
    
    adjustViewportPadding();
}

let resizeTimeout2 = null;
function debouncedAdjust() {
    if (resizeTimeout2) {
        cancelAnimationFrame(resizeTimeout2);
    }
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
// VIEWPORT OBSERVER - FIXED (debounced)
// ============================================================
function setupViewportObserver() {
    if (viewportObserver) {
        viewportObserver.disconnect();
        viewportObserver = null;
    }
    viewportObserver = new MutationObserver(() => {
        // Use debounce to avoid excessive calls
        clearTimeout(window._viewportObserverTimeout);
        window._viewportObserverTimeout = setTimeout(() => {
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
        }, 100);
    });
    viewportObserver.observe(viewport, { 
        childList: true, 
        subtree: true, 
        characterData: true,
        attributes: true 
    });
}

// ============================================================
// FILE HANDLING - FIXED
// ============================================================
function renderFileChips() {
    const container = document.getElementById('file-staging-container');
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
    container.style.display = 'flex';
}

function removeStagedFile(idx) {
    stagedFiles.splice(idx, 1);
    renderFileChips();
    validateSendCommand();
}

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

// ============================================================
// SCROLL HANDLING
// ============================================================
viewport.addEventListener('scroll', () => {
    isUserScrolling = true;
    clearTimeout(scrollTimeout);
    scrollTimeout = setTimeout(() => {
        isUserScrolling = false;
    }, 500);
}, { passive: true });

// ============================================================
// SIDEBAR FUNCTIONS
// ============================================================
function toggleSidebar() {
    sidebarNode.classList.toggle('open');
}

sidebarTriggerArea.addEventListener('click', (e) => {
    e.stopPropagation();
    sidebarNode.classList.toggle('open');
});

window.addEventListener('click', (e) => {
    if (ignoreSidebarClose || document.activeElement === document.getElementById('sidebar-search-input')) {
        return;
    }
    if (e.target.closest('#sidebar-container-node')) return;
    if (e.target.closest('#sidebar-search-box') || e.target.closest('#sidebar-search-box input')) {
        e.stopPropagation();
        return;
    }
    if (!sidebarNode.contains(e.target) && e.target !== sidebarTriggerArea) sidebarNode.classList.remove('open');
    if (!document.querySelector('.account-hub').contains(e.target)) accountDropdownCard.style.display = 'none';
    if (!document.querySelector('.model-hub').contains(e.target)) modelDropdownCard.style.display = 'none';
    if (!e.target.closest('.history-options-btn')) document.querySelectorAll('.actions-dropdown-list').forEach(d =>
        d.classList.remove('active'));
});

const searchInput = document.getElementById('sidebar-search-input');
const searchBox = document.getElementById('sidebar-search-box');
[searchBox, searchInput].forEach(el => {
    el.addEventListener('click', (e) => e.stopPropagation());
    el.addEventListener('touchstart', (e) => e.stopPropagation());
});

searchInput.addEventListener('focus', () => {
    ignoreSidebarClose = true;
    setTimeout(() => { ignoreSidebarClose = false; }, 500);
});
searchInput.addEventListener('blur', () => {
    ignoreSidebarClose = false;
});

function toggleAccountDropdown(e) {
    e.stopPropagation();
    accountDropdownCard.style.display = accountDropdownCard.style.display === 'flex' ? 'none' : 'flex';
    modelDropdownCard.style.display = 'none';
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
}

function toggleModelDropdown(e) {
    e.stopPropagation();
    modelDropdownCard.style.display = modelDropdownCard.style.display === 'flex' ? 'none' : 'flex';
    accountDropdownCard.style.display = 'none';
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
}

function selectModel(e, type) {
    e.stopPropagation();
    modelDropdownCard.style.display = 'none';
}

function toggleHistoryOptions(e, id) {
    e.stopPropagation();
    document.querySelectorAll('.actions-dropdown-list').forEach(d => d.classList.remove('active'));
    document.getElementById(`options-${id}`).classList.add('active');
}

function switchSidebarTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => {
        b.classList.remove('active');
    });
    const activeBtn = document.getElementById(`tab-${tab}`);
    activeBtn.classList.add('active');
    loadArchiveLogs();
}

function openSettingsModal() {
    closeModals();
    document.getElementById('settings-modal').classList.add('active');
    updateSettingsQuota();
    document.querySelectorAll('.theme-option').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === currentThemePreference);
    });
}

function updateSettingsQuota() {
    const quotaCount = document.getElementById('quota-numerical-count');
    const quotaFill = document.getElementById('quota-progress-bar-fill');
    const planBadge = document.getElementById('settings-plan-badge');
    if (quotaCount) {
        document.getElementById('settings-quota-count').innerText = quotaCount.innerText;
    }
    if (quotaFill) {
        document.getElementById('settings-quota-fill').style.width = quotaFill.style.width;
    }
    if (planBadge) {
        document.getElementById('settings-plan-badge').innerText = planBadge.innerText;
        document.getElementById('settings-plan-badge').style.background = planBadge.style.background;
        document.getElementById('settings-plan-badge').style.color = planBadge.style.color;
    }
}

// ============================================================
// SEARCH OVERLAY
// ============================================================
function openSearchOverlay() {
    const overlay = document.getElementById('search-overlay');
    overlay.classList.add('active');
    const input = document.getElementById('search-overlay-input');
    input.value = '';
    input.focus();
    filterSearchOverlay();
}

function closeSearchOverlay() {
    document.getElementById('search-overlay').classList.remove('active');
}

function filterSearchOverlay() {
    const query = document.getElementById('search-overlay-input').value.toLowerCase();
    const resultsContainer = document.getElementById('search-overlay-results');
    const logs = cachedLogHistory.filter(log => log.status === currentTab);
    const filtered = query ? logs.filter(log => log.filename.toLowerCase().includes(query)) : logs;
    if (filtered.length === 0) {
        resultsContainer.innerHTML =
            `<div style="color:var(--text-muted);text-align:center;padding:30px;">No matching chats</div>`;
        return;
    }
    resultsContainer.innerHTML = filtered.map(log => `
        <div class="search-result-item" onclick="viewPastLogById('${log._id}'); closeSearchOverlay();">
            <div class="title">${escapeHtmlEntities(log.filename)}</div>
            <div class="preview">${log.messages.length} messages • ${new Date(log.createdAt).toLocaleDateString()}</div>
        </div>
    `).join('');
}

document.getElementById('search-overlay-input').addEventListener('input', filterSearchOverlay);

const sidebarSearchInput = document.getElementById('sidebar-search-input');
sidebarSearchInput.addEventListener('focus', function(e) {
    e.preventDefault();
    this.blur();
    openSearchOverlay();
});
document.getElementById('sidebar-search-box').addEventListener('click', function(e) {
    e.preventDefault();
    openSearchOverlay();
});

// ============================================================
// AUTH FUNCTIONS
// ============================================================
function decodeJwt(token) {
    const base64Url = token.split('.')[1];
    const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(decodeURIComponent(atob(base64).split('').map(c => '%' + ('00' + c.charCodeAt(0).toString(16))
        .slice(-2)).join('')));
}

function handleCredentialResponse(response) {
    const token = response.credential;
    const payload = decodeJwt(token);
    localStorage.setItem('google_auth_token', token);
    initializeSecureWorkspace(payload, token);
    const avatarSrc = payload.picture;
    const avatarImg = document.getElementById('user-avatar');
    const fallback = document.getElementById('user-avatar-fallback');
    avatarImg.src = avatarSrc;
    fallback.innerText = payload.name.charAt(0).toUpperCase();
    const dropdownImg = document.getElementById('dropdown-avatar');
    const dropdownFallback = document.getElementById('dropdown-avatar-fallback');
    dropdownImg.src = avatarSrc;
    dropdownFallback.innerText = payload.name.charAt(0).toUpperCase();
}

// ============================================================
// WORKSPACE FUNCTIONS
// ============================================================
function showWorkspaceSelector() {
    document.getElementById('workspace-selector').style.display = 'flex';
}

function selectWorkspace(type) {
    localStorage.setItem('Axelr_workspace', type);
    document.getElementById('workspace-selector').style.display = 'none';
    activateWorkspace(type);
}

function activateWorkspace(type, isBoot = false) {
    mainWrapper.classList.add('visible');
    document.body.classList.remove('workspace-data', 'workspace-design');
    document.body.classList.add(`workspace-${type}`);
    const isMobile = window.innerWidth <= 768;
    if (type === 'design') {
        document.getElementById('sidebar-logo-text').innerText = 'AXELR DESIGN';
        document.getElementById('hero-title-text').innerText = 'What are we designing today?';
        document.getElementById('hero-sub-text').innerText = 'AI-powered UI/UX generation & live deployment.';
        promptInput.placeholder = isMobile ? "Upload a mockup..." :
            "Upload a mockup or request a UI component...";
    } else {
        document.getElementById('sidebar-logo-text').innerText = 'AXELR DATA';
        document.getElementById('hero-title-text').innerText = 'What are we building today?';
        document.getElementById('hero-sub-text').innerText = 'AI-powered architecture and data execution.';
        promptInput.placeholder = isMobile ? "Upload a receipt..." :
            "Upload a receipt, invoice, or CSV for extraction...";
    }
    resetToNewChat(isBoot);
    if (!isBoot) loadArchiveLogs();
}

function resetToNewChat(isBoot = false) {
    // Disconnect observer during reset
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
    document.querySelectorAll('.chat-bubble').forEach(bubble => bubble.remove());
    
    const hero = document.getElementById('hero-display');
    if (hero) hero.style.display = 'flex';
    
    if (!isBoot) {
        promptInput.value = '';
        promptInput.style.height = 'auto';
        localStorage.removeItem(getDraftKey());
    } else {
        const savedDraft = localStorage.getItem(getDraftKey());
        if (savedDraft) {
            promptInput.value = savedDraft;
            setTimeout(() => {
                promptInput.style.height = 'auto';
                promptInput.style.height = promptInput.scrollHeight + 'px';
            }, 10);
        }
    }
    validateSendCommand();
    if (window.innerWidth <= 768) sidebarNode.classList.remove('open');
    hasRegenerated = false;
    if (regenerateTimer) {
        clearTimeout(regenerateTimer);
        regenerateTimer = null;
    }
    const mainBackBtn = document.getElementById('main-back-btn');
    if (mainBackBtn) mainBackBtn.style.display = 'none';
    adjustViewportPadding();
    
    setTimeout(setupViewportObserver, 100);
}

// ============================================================
// VALIDATION
// ============================================================
function validateSendCommand() {
    const hasInput = promptInput.value.trim().length > 0 || stagedFiles.length > 0;
    document.getElementById('send-trigger').disabled = !hasInput;
    const inputFrame = document.querySelector('.input-frame');
    if (hasInput) {
        inputFrame.classList.add('has-text');
    } else {
        inputFrame.classList.remove('has-text');
    }
}

promptInput.addEventListener('input', () => {
    promptInput.style.height = 'auto';
    promptInput.style.height = promptInput.scrollHeight + 'px';
    validateSendCommand();
    localStorage.setItem(getDraftKey(), promptInput.value);
});

window.addEventListener('offline', () => {
    document.getElementById('offline-banner').style.display = 'block';
    document.getElementById('send-trigger').disabled = true;
});
window.addEventListener('online', () => {
    document.getElementById('offline-banner').style.display = 'none';
    validateSendCommand();
});

// ============================================================
// VOICE INPUT
// ============================================================
const micBtn = document.getElementById('mic-trigger');
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (SpeechRecognition) {
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
        basePromptText = promptInput.value;
    };
    recognition.onend = () => {
        isListeningForVocal = false;
        micBtn.classList.remove('listening');
    };
    recognition.onerror = (event) => {
        isListeningForVocal = false;
        micBtn.classList.remove('listening');
        if (event.error === 'not-allowed') alert(
            "⚠️ Microphone access denied. Allow permissions in your browser URL bar.");
        else if (event.error === 'no-speech') alert("⚠️ No audio detected. Check your microphone settings.");
    };
    recognition.onresult = (event) => {
        let interim = "";
        let final = "";
        for (let i = event.resultIndex; i < event.results.length; ++i) {
            if (event.results[i].isFinal) final += event.results[i][0].transcript;
            else interim += event.results[i][0].transcript;
        }
        promptInput.value = (basePromptText + " " + final + " " + interim).trim();
        promptInput.style.height = 'auto';
        promptInput.style.height = promptInput.scrollHeight + 'px';
        validateSendCommand();
        localStorage.setItem(getDraftKey(), promptInput.value);
    };
} else {
    micBtn.style.display = 'none';
}

// ============================================================
// MARKED RENDERER
// ============================================================
const renderer = new marked.Renderer();
renderer.code = function(code, language) {
    return `<pre><button class="copy-code-btn" onclick="navigator.clipboard.writeText(this.nextElementSibling.innerText); this.innerText='Copied!'; setTimeout(()=>this.innerText='Copy Code', 2000)">Copy Code</button><code>${code.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</code></pre>`;
};
marked.setOptions({ renderer: renderer, breaks: true });

// ============================================================
// USER PROFILE & QUOTA
// ============================================================
async function loadUserProfile() {
    try {
        const resp = await fetch(`${API_BASE_URL}/api/user/profile`, {
            headers: { 'Authorization': `Bearer ${googleAuthUserToken}` }
        });
        if (resp.ok) {
            const data = await resp.json();
            document.getElementById('instructions-input').value = data.customInstructions || "";

            // Admin check - ONLY for shanh1346@gmail.com
            const isAdmin = data.isAdmin === true && data.email === 'shanh1346@gmail.com';
            if (isAdmin) {
                document.getElementById('admin-dashboard-btn').style.display = 'block';
                console.log('✅ Admin mode enabled for', data.email);
            } else {
                document.getElementById('admin-dashboard-btn').style.display = 'none';
            }

            const currentWorkspace = getWorkspace();
            const isDesign = currentWorkspace === 'design';
            const isFree = data.tier === 'free';
            const isPro = data.tier === 'pro';
            const isBusiness = data.tier === 'business';

            let used = 0, limit = 0;

            if (isFree) {
                used = Math.max(0, data.dailyUsage || 0);
                limit = 5;
            } else {
                const hasData = data.subTierOptions?.hasDataAccess || false;
                const hasDesign = data.subTierOptions?.hasDesignAccess || false;
                let subTierType = 'full';
                if (hasData && !hasDesign) subTierType = 'data';
                else if (!hasData && hasDesign) subTierType = 'design';

                let dataLimit = 0, uiLimit = 0;
                if (isPro) {
                    if (subTierType === 'full') { dataLimit = 20; uiLimit = 15; }
                    else if (subTierType === 'data') { dataLimit = 19; uiLimit = 0; }
                    else if (subTierType === 'design') { dataLimit = 0; uiLimit = 13; }
                } else if (isBusiness) {
                    if (subTierType === 'full') { dataLimit = 30; uiLimit = 25; }
                    else if (subTierType === 'data') { dataLimit = 28; uiLimit = 0; }
                    else if (subTierType === 'design') { dataLimit = 0; uiLimit = 20; }
                }
                if (isDesign) {
                    used = Math.max(0, data.quotas?.dailyGenerationsUsed || 0);
                    limit = uiLimit;
                } else {
                    used = Math.max(0, data.quotas?.dailyExtractionsUsed || 0);
                    limit = dataLimit;
                }
            }

            const percentage = limit > 0 ? Math.min((used / limit) * 100, 100) : 0;
            document.getElementById('quota-numerical-count').innerText =
                `${used}/${limit} Used (${Math.round(percentage)}%)`;
            document.getElementById('quota-progress-bar-fill').style.width = `${percentage}%`;

            const planBadge = document.getElementById('sidebar-plan-badge');
            if (planBadge) {
                planBadge.innerText = data.tier.toUpperCase();
                planBadge.style.background = data.tier === 'free' ? 'var(--border-muted)' :
                    data.tier === 'pro' ? 'rgba(139,92,246,0.2)' : 'rgba(244,63,94,0.2)';
                planBadge.style.color = data.tier === 'free' ? 'var(--text-muted)' :
                    data.tier === 'pro' ? 'var(--accent-glow-pro)' : 'var(--accent-glow-designer)';
            }
            document.getElementById('sub-plan-name').innerText = data.tier.toUpperCase() + ' ALLOCATION';
            document.getElementById('tier-badge').innerText = data.tier.toUpperCase() + ' TIER';
            if (data.tier === 'pro') document.body.classList.add('pro-tier');
            else if (data.tier === 'business') document.body.classList.add('designer-tier');
            else { document.body.classList.remove('pro-tier', 'designer-tier'); }

            updateSettingsQuota();
            updateSubscriptionModal();
        }
    } catch (e) { console.warn('Profile load failed', e); }
}

// ============================================================
// HISTORY - with debounce to avoid multiple calls
// ============================================================
let historyLoading = false;

async function loadArchiveLogs() {
    if (historyLoading) return;
    historyLoading = true;
    try {
        const currentWorkspace = getWorkspace();
        const response = await fetch(
            `${API_BASE_URL}/api/history?status=${currentTab}&workspace=${currentWorkspace}`, {
                headers: { 'Authorization': `Bearer ${googleAuthUserToken}` }
            }
        );
        if (response.status === 401) return executeGlobalLogout();
        cachedLogHistory = (await response.json()).logs;
        if (cachedLogHistory.length === 0) {
            historyListContainer.innerHTML =
                `<div style="color:#4b5563;font-size:12px;text-align:center;padding:15px;">No ${currentTab} chats.</div>`;
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
            if (savedDraft) {
                promptInput.value = savedDraft;
                promptInput.style.height = 'auto';
                promptInput.style.height = promptInput.scrollHeight + 'px';
                validateSendCommand();
            }
        }
    } catch (e) {
        console.warn('loadArchiveLogs error:', e);
    } finally {
        historyLoading = false;
    }
}

// ============================================================
// RENAME, PIN, SHARE, STATUS FUNCTIONS
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
        const resp = await fetch(`${API_BASE_URL}/api/history/${logId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
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
        const resp = await fetch(`${API_BASE_URL}/api/history/${logId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
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
        try { document.execCommand('copy');
            alert("✓ Chat copied to clipboard."); } catch (err) { alert(
                "⚠️ Browser security blocked clipboard access."); }
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
        const resp = await fetch(`${API_BASE_URL}/api/history/${logId}/status`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
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
        const resp = await fetch(`${API_BASE_URL}/api/history/${logId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${googleAuthUserToken}` }
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
// VIEW PAST LOG - FIXED (no unnecessary re-renders)
// ============================================================
function viewPastLogById(logId) {
    if (regenerateTimer) {
        clearTimeout(regenerateTimer);
        regenerateTimer = null;
    }
    
    const log = cachedLogHistory.find(l => l._id === logId);
    if (!log) return;
    
    // If already viewing this session, do nothing
    if (activeSessionId === logId) return;
    
    const hero = document.getElementById('hero-display');
    if (hero) hero.style.display = 'none';
    
    // Clear existing bubbles
    document.querySelectorAll('.chat-bubble').forEach(b => b.remove());
    
    activeSessionId = logId;
    localStorage.setItem('axelr_active_session', activeSessionId);
    runningFileTitle = log.filename;
    runningStructuredCache = log.structuredData;
    
    const mainBackBtn = document.getElementById('main-back-btn');
    if (mainBackBtn) mainBackBtn.style.display = 'flex';
    
    if (currentTab === 'trashed') {
        const trashMsg = document.createElement('div');
        trashMsg.className = 'chat-bubble';
        trashMsg.style.cssText =
            "background:rgba(239,68,68,0.1);color:#ef4444;padding:15px;text-align:center;border-radius:8px;margin-bottom:20px;width:100%;";
        trashMsg.innerHTML =
            `<span class="material-symbols-rounded" style="font-size:20px;">delete</span> This chat is in the trash. Restore it to continue chatting.`;
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
                filesHtml = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">' + msg
                    .attachedFiles.map(f =>
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
            injectActionButtons(contentDiv, msg.text, true, false, null, null, isLastUser);
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
            const alreadyRegenerated = msg.variants && msg.variants.length > 1;
            const isActive = log.status === 'active';
            let showRegenerate = false;
            if (isLast && isActive && !alreadyRegenerated) {
                const msgDate = msg.createdAt ? new Date(msg.createdAt) : new Date(log.createdAt);
                const now = new Date();
                if (!isNaN(msgDate.getTime()) && (now - msgDate) < 30000) {
                    showRegenerate = true;
                }
            }
            injectActionButtons(contentDiv, rawResponse, false, showRegenerate, msg.createdAt || log.createdAt,
                log._id);

            if (msg.variants && msg.variants.length > 1) {
                const currentIdx = msg.activeVariant || 0;
                const variantBar = document.createElement('div');
                variantBar.style.cssText =
                    "display:flex;align-items:center;gap:12px;margin-top:15px;padding-top:10px;border-top:1px solid var(--border-muted);font-size:12px;color:var(--text-muted);font-weight:600;";
                const prevBtn = document.createElement('button');
                prevBtn.innerHTML = ICONS.leftArrow;
                prevBtn.style.cssText =
                    `background:transparent;border:none;cursor:${currentIdx === 0 ? 'default' : 'pointer'};color:${currentIdx === 0 ? 'var(--border-muted)' : 'var(--text-main)'};font-size:14px;`;
                if (currentIdx > 0) prevBtn.onclick = () => switchVariant(logId, msg._id, currentIdx - 1);
                const nextBtn = document.createElement('button');
                nextBtn.innerHTML = ICONS.rightArrow;
                nextBtn.style.cssText =
                    `background:transparent;border:none;cursor:${currentIdx === msg.variants.length - 1 ? 'default' : 'pointer'};color:${currentIdx === msg.variants.length - 1 ? 'var(--border-muted)' : 'var(--text-main)'};font-size:14px;`;
                if (currentIdx < msg.variants.length - 1) nextBtn.onclick = () => switchVariant(logId, msg
                    ._id, currentIdx + 1);
                variantBar.appendChild(prevBtn);
                const countSpan = document.createElement('span');
                countSpan.innerText = `${currentIdx + 1} / ${msg.variants.length}`;
                variantBar.appendChild(countSpan);
                variantBar.appendChild(nextBtn);
                contentDiv.appendChild(variantBar);
            }
        }
        viewport.appendChild(bubble);
    });
    
    updateViewportAfterRender();
    if (window.innerWidth <= 768) sidebarNode.classList.remove('open');
}

async function switchVariant(logId, msgId, newIndex) {
    try {
        await fetch(`${API_BASE_URL}/api/history/${logId}/variant`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
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
    sessionId = null, isLastUserMsg = false) {
    const actionBar = document.createElement('div');
    actionBar.className = 'bubble-action-bar';

    if (isUserPrompt) {
        const copyBtn = document.createElement('button');
        copyBtn.className = 'action-icon-btn';
        copyBtn.title = "Copy Prompt";
        copyBtn.innerHTML = `${ICONS.copy} Copy`;
        copyBtn.onclick = () => handleActionClick('copy', rawText, copyBtn);
        actionBar.appendChild(copyBtn);

        if (isLastUserMsg) {
            const editBtn = document.createElement('button');
            editBtn.className = 'action-icon-btn';
            editBtn.title = "Edit this prompt";
            editBtn.innerHTML = `${ICONS.edit} Edit`;
            editBtn.onclick = () => handleActionClick('edit', rawText, editBtn);
            actionBar.appendChild(editBtn);
        }
    } else {
        const copyBtn = document.createElement('button');
        copyBtn.className = 'action-icon-btn';
        copyBtn.title = "Copy Response";
        copyBtn.innerHTML = `${ICONS.copy} Copy`;
        copyBtn.onclick = () => handleActionClick('copy', rawText, copyBtn);

        const likeBtn = document.createElement('button');
        likeBtn.className = 'action-icon-btn';
        likeBtn.title = "Helpful Response";
        likeBtn.innerHTML = ICONS.thumbsUp;
        likeBtn.onclick = () => { likeBtn.style.color = 'var(--accent-glow)';
            dislikeBtn.style.color = 'var(--text-muted)'; };

        const dislikeBtn = document.createElement('button');
        dislikeBtn.className = 'action-icon-btn';
        dislikeBtn.title = "Not Helpful";
        dislikeBtn.innerHTML = ICONS.thumbsDown;
        dislikeBtn.onclick = () => { dislikeBtn.style.color = '#ef4444';
            likeBtn.style.color = 'var(--text-muted)'; };

        actionBar.appendChild(copyBtn);
        actionBar.appendChild(likeBtn);
        actionBar.appendChild(dislikeBtn);

        if (showRegenerate && createdAt && sessionId) {
            const now = Date.now();
            const msgTime = new Date(createdAt).getTime();
            const elapsed = now - msgTime;
            if (elapsed < 30000) {
                const regenBtn = document.createElement('button');
                regenBtn.className = 'action-icon-btn regen-active';
                regenBtn.title = "Regenerate response (available for 30s)";
                regenBtn.innerHTML = ICONS.regenerate +
                    `<span class="regen-countdown">${Math.ceil((30000 - elapsed)/1000)}s</span>`;

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

                regenBtn.onclick = function(e) {
                    if (activeSessionId !== sessionId) {
                        cleanup();
                        return;
                    }
                    cleanup();
                    if (window.lastUserCommand) {
                        document.getElementById('prompt-input').value = window.lastUserCommand;
                        document.getElementById('prompt-input').style.height = 'auto';
                        document.getElementById('prompt-input').style.height = document.getElementById(
                            'prompt-input').scrollHeight + 'px';
                        if (regenerateTimer) {
                            clearTimeout(regenerateTimer);
                            regenerateTimer = null;
                        }
                        executeCommand(true);
                    }
                };

                actionBar.appendChild(regenBtn);
            }
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
    } else if (actionType === 'edit') {
        promptInput.value = rawText;
        promptInput.style.height = 'auto';
        promptInput.style.height = promptInput.scrollHeight + 'px';
        promptInput.focus();
        validateSendCommand();
    }
}

// ============================================================
// ENHANCE PROMPT
// ============================================================
async function enhanceUserPrompt() {
    const text = promptInput.value.trim();
    if (!text) return;
    const enhanceBtn = document.getElementById('enhance-trigger');
    const inputFrame = document.querySelector('.input-frame');
    const originalText = enhanceBtn.innerHTML;
    enhanceBtn.classList.add('loading');
    enhanceBtn.disabled = true;
    promptInput.disabled = true;
    inputFrame.style.filter = 'blur(4px) brightness(0.8)';
    inputFrame.style.pointerEvents = 'none';
    enhanceBtn.innerHTML =
        '<span style="font-size:12px;font-weight:bold;letter-spacing:1px;color:var(--accent-glow);"><span class="material-symbols-rounded" style="font-size:16px;">auto_awesome</span> PROCESSING...</span>';
    try {
        const response = await fetch(`${API_BASE_URL}/api/enhance-prompt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
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
        if (p.test(command)) {
            return true;
        }
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
        html = `
            <div class="security-alert-banner warning">
                <span class="material-symbols-rounded">warning</span>
                <span>⚠️ Warning: Detected attempt to bypass core system architecture. Please stay within operational parameters to avoid account restriction.</span>
            </div>
        `;
    } else if (level === 2) {
        html = `
            <div class="security-alert-banner critical">
                <span class="material-symbols-rounded">error</span>
                <span>🚨 SECURITY ALERT: Multiple manipulation attempts detected. System locking for 10 minutes. Please reset your operational behavior.</span>
            </div>
        `;
    }
    contentDiv.innerHTML = html;
    alertBubble.appendChild(contentDiv);
    viewport.appendChild(alertBubble);
    scrollToBottom();
}

// ============================================================
// EXECUTE COMMAND - FIXED (error handling & UI)
// ============================================================
async function executeCommand(isRetry = false) {
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
        viewport.appendChild(userBubble);
        showSecurityAlert(level);
        promptInput.value = '';
        promptInput.style.height = 'auto';
        validateSendCommand();
        localStorage.removeItem(getDraftKey());
        isProcessing = false;
        return;
    }

    let finalCommand = command;
    const originalBtnHtml = sendBtn.innerHTML;

    if (globalAbortController) {
        globalAbortController.abort();
        globalAbortController = null;
        sendBtn.classList.remove('btn-stop-active');
        sendBtn.innerHTML = originalBtnHtml;
        isProcessing = false;
        return;
    }

    if (!isRetry) {
        promptInput.value = '';
        promptInput.style.height = 'auto';
        validateSendCommand();
    }
    sendBtn.disabled = true;

    // Hide hero on first command
    const hero = document.getElementById('hero-display');
    if (hero) hero.style.display = 'none';

    const userBubble = document.createElement('div');
    userBubble.className = 'chat-bubble user-bubble';
    let filesHtml = '';
    if (stagedFiles.length > 0) {
        filesHtml = '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;">' + stagedFiles.map(f =>
                `<div class="file-chip"><span class="material-symbols-rounded" style="font-size:14px;">description</span> ${escapeHtmlEntities(f.name)}</div>`
                ).join('') + '</div>';
    }
    userBubble.innerHTML = `${filesHtml}${DOMPurify.sanitize(marked.parse(command || " "))}`;
    viewport.appendChild(userBubble);

    if (isRetry) {
        const allBubbles = viewport.querySelectorAll('.chat-bubble');
        if (allBubbles.length >= 2) {
            const lastBubble = allBubbles[allBubbles.length - 1];
            if (lastBubble.classList.contains('nexus-bubble')) {
                lastBubble.remove();
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

    const nexusBubble = document.createElement('div');
    nexusBubble.className = 'chat-bubble nexus-bubble';
    const avatarDiv = document.createElement('div');
    avatarDiv.className = 'ai-avatar-bubble';
    avatarDiv.innerHTML = AXELR_AVATAR_SVG;
    nexusBubble.appendChild(avatarDiv);

    const contentDiv = document.createElement('div');
    contentDiv.className = 'bubble-content';
    contentDiv.style.flex = '1';

    const loader = document.createElement('div');
    loader.className = 'matrix-loader';
    for (let i = 0; i < 3; i++) {
        const dot = document.createElement('div');
        dot.className = 'matrix-dot';
        loader.appendChild(dot);
    }
    contentDiv.appendChild(loader);

    let extendedTimeout = setTimeout(() => {
        const extendedMsg = document.createElement('div');
        extendedMsg.className = 'thinking-extended';
        extendedMsg.innerText = 'Thinking a little bit longer... Don\'t close the tab.';
        contentDiv.appendChild(extendedMsg);
    }, 5000);

    nexusBubble.appendChild(contentDiv);
    viewport.appendChild(nexusBubble);
    scrollToBottom();

    const formData = new FormData();
    formData.append('command', finalCommand);
    formData.append('workspace', getWorkspace());
    formData.append('isRetry', isRetry ? 'true' : 'false');
    if (activeSessionId) formData.append('sessionId', activeSessionId);
    for (const file of stagedFilesSnapshot) {
        formData.append('files', file);
    }

    sendBtn.classList.add('btn-stop-active');
    sendBtn.innerHTML = ICONS.stop;

    globalAbortController = new AbortController();

    let responseReceived = false;
    const timeoutFallback = setTimeout(() => {
        if (!responseReceived) {
            contentDiv.innerHTML = `⚠️ The AI took too long to respond. Please try again.`;
            // Show hero again if no response
            if (hero) hero.style.display = 'flex';
            scrollToBottom();
            if (globalAbortController) globalAbortController.abort();
            sendBtn.classList.remove('btn-stop-active');
            sendBtn.disabled = false;
            isProcessing = false;
        }
    }, 30000);

    try {
        const response = await fetch(`${API_BASE_URL}/api/extract`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${googleAuthUserToken}` },
            body: formData,
            signal: globalAbortController.signal,
        });

        clearTimeout(extendedTimeout);

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            if (errorData.code === 'LIMIT_REACHED') {
                contentDiv.innerHTML =
                    `⚠️ <strong>Daily Quota Exceeded.</strong><br><button onclick="openUpgradeModal()" style="background:var(--accent-glow-pro);color:#000;padding:8px 12px;border:none;border-radius:6px;cursor:pointer;font-weight:600;margin-top:10px;">Upgrade Workspace</button>`;
            } else if (errorData.code === 'SUB_TIER_RESTRICTION') {
                contentDiv.innerHTML =
                    `⚠️ <strong>Access Restricted.</strong><br>${errorData.message || 'Your current plan does not include this workspace type.'}<br><button onclick="openUpgradeModal()" style="background:var(--accent-glow-pro);color:#000;padding:8px 12px;border:none;border-radius:6px;cursor:pointer;font-weight:600;margin-top:10px;">Upgrade Workspace</button>`;
            } else {
                contentDiv.innerHTML = `💥 Error: ${errorData.message || 'Pipeline failed.'}`;
            }
            // Show hero if error occurred and no messages yet
            if (!viewport.querySelector('.chat-bubble.user-bubble') && !viewport.querySelector('.chat-bubble.nexus-bubble')) {
                if (hero) hero.style.display = 'flex';
            }
            scrollToBottom();
            isProcessing = false;
            return;
        }

        const result = await response.json();
        responseReceived = true;

        if (result.success) {
            const fullResponse = result.text || "No response from AI.";
            const sessionId = result.sessionId;
            const structuredData = result.structuredData;
            const filename = result.filename || 'Export.csv';

            contentDiv.innerHTML = DOMPurify.sanitize(marked.parse(fullResponse));

            if (sessionId) {
                activeSessionId = sessionId;
                localStorage.setItem('axelr_active_session', activeSessionId);
                runningStructuredCache = structuredData;
                runningFileTitle = filename;
                await loadArchiveLogs();
                viewPastLogById(activeSessionId);
            }

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
            hasRegenerated = false;
            if (regenerateTimer) {
                clearTimeout(regenerateTimer);
                regenerateTimer = null;
            }

            const now = new Date().toISOString();
            injectActionButtons(contentDiv, fullResponse, false, true, now, activeSessionId);
            scrollToBottom();

            const mainBackBtn = document.getElementById('main-back-btn');
            if (mainBackBtn) mainBackBtn.style.display = 'flex';

        } else {
            contentDiv.innerHTML = `⚠️ ${result.message || 'Something went wrong.'}`;
            // Show hero if no messages
            if (!viewport.querySelector('.chat-bubble.nexus-bubble')) {
                if (hero) hero.style.display = 'flex';
            }
        }

    } catch (error) {
        clearTimeout(extendedTimeout);
        if (error.name === 'AbortError') {
            contentDiv.innerHTML +=
                `<br><br><em style="color:var(--text-muted);">[Generation halted by user]</em>`;
        } else {
            console.error('Execute error:', error);
            contentDiv.innerHTML = `⚠️ Network connection dropped. Please retry.`;
        }
        // Show hero if error occurred
        if (!viewport.querySelector('.chat-bubble.nexus-bubble')) {
            if (hero) hero.style.display = 'flex';
        }
        scrollToBottom();
    } finally {
        clearTimeout(timeoutFallback);
        clearTimeout(extendedTimeout);
        globalAbortController = null;
        sendBtn.classList.remove('btn-stop-active');
        sendBtn.innerHTML = originalBtnHtml;
        sendBtn.disabled = false;
        validateSendCommand();
        updateViewportAfterRender();
        await loadUserProfile();
        isProcessing = false;
    }
}

// ============================================================
// PAYLOAD / DEPLOY
// ============================================================
function appendPayloadDownload(bubbleNode) {
    if (runningStructuredCache && runningStructuredCache.length > 0) {
        const btn = document.createElement('button');
        btn.className = 'download-btn-bubble';
        btn.innerHTML =
            `<span class="material-symbols-rounded" style="font-size:18px;">download</span> Download CSV`;
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
    deployBtn.innerHTML =
        `<span class="material-symbols-rounded" style="font-size:18px;">rocket_launch</span> Deploy Live`;

    const debugBtn = document.createElement('button');
    debugBtn.className = 'deploy-debug-btn';
    debugBtn.innerText = 'Debug';
    debugBtn.style.display = 'none';
    const errorDetails = document.createElement('div');
    errorDetails.className = 'deploy-error-details';

    deployContainer.appendChild(deployBtn);
    deployContainer.appendChild(debugBtn);
    deployContainer.appendChild(errorDetails);
    bubbleNode.appendChild(deployContainer);

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
            const response = await fetch(`${API_BASE_URL}/api/deploy`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
                body: JSON.stringify({ htmlContent: rawHtml }),
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            const result = await response.json();
            if (result.success) {
                deployBtn.innerHTML =
                    `<a href="${result.liveUrl}" target="_blank" style="color:#000;text-decoration:none;display:flex;align-items:center;gap:8px;"><span class="material-symbols-rounded" style="font-size:18px;">open_in_new</span> View Live Website</a>`;
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
                debugBtn.onclick = () => {
                    errorDetails.classList.toggle('show');
                };
                const retryBtn = document.createElement('button');
                retryBtn.className = 'deploy-retry-btn';
                retryBtn.innerText = 'Retry Deployment';
                retryBtn.onclick = () => {
                    retryCount = 0;
                    errorDetails.classList.remove('show');
                    deployBtn.style.background = 'var(--accent-secondary)';
                    deployBtn.innerText = 'Deploy Live';
                    deployBtn.disabled = false;
                    debugBtn.style.display = 'none';
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
    document.getElementById('upgrade-modal').classList.add('active');
}

function openProfileModal() {
    closeModals();
    document.getElementById('profile-modal').classList.add('active');
}

function openInstructionsModal() {
    closeModals();
    document.getElementById('instructions-modal').classList.add('active');
}

function openFeedbackModal() {
    closeModals();
    document.getElementById('feedback-modal').classList.add('active');
}

function openBillingFlow() {
    closeModals();
    document.getElementById('subscription-modal').classList.add('active');
    updateSubscriptionModal();
}

async function openAdminModal() {
    closeModals();
    document.getElementById('admin-modal').classList.add('active');
    try {
        const resp = await fetch(`${API_BASE_URL}/api/admin/metrics`, {
            headers: { 'Authorization': `Bearer ${googleAuthUserToken}` }
        });
        if (resp.ok) {
            const data = await resp.json();
            document.getElementById('admin-metrics-container').innerHTML = `
                <div class="profile-stat-row"><span class="profile-stat-label">Total Users</span><span class="profile-stat-value">${data.totalUsers}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Pro Subscribers</span><span class="profile-stat-value" style="color:var(--accent-glow-pro)">${data.proUsers}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Designer Subscribers</span><span class="profile-stat-value" style="color:var(--accent-glow-designer)">${data.designerUsers}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Total Matrix Logs</span><span class="profile-stat-value">${data.totalChats}</span></div>
                <div style="border-top:1px solid var(--border-muted);margin:10px 0;"></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Total Queries Processed</span><span class="profile-stat-value">${data.metrics?.totalQueries || 0}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Total Storage Used</span><span class="profile-stat-value">${(data.metrics?.totalBytes || 0) / (1024*1024)} MB</span></div>
                <div style="border-top:1px solid var(--border-muted);margin:10px 0;"></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Total Tokens Used</span><span class="profile-stat-value">${data.tokenUsage?.total || 0}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Free Tier Remaining</span><span class="profile-stat-value" style="color:${data.tokenUsage?.remaining > 0 ? 'var(--accent-secondary)' : '#ef4444'};">${data.tokenUsage?.remaining || 0} / ${data.tokenUsage?.limit || 1000000}</span></div>
                <div style="border-top:1px solid var(--border-muted);margin:10px 0;"></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Daily AI Queries</span><span class="profile-stat-value">${data.dailyAIUsage?.totalQueries || 0}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Daily Prompt Tokens</span><span class="profile-stat-value">${data.dailyAIUsage?.prompt || 0}</span></div>
                <div class="profile-stat-row"><span class="profile-stat-label">Daily Completion Tokens</span><span class="profile-stat-value">${data.dailyAIUsage?.completion || 0}</span></div>
            `;
        } else {
            document.getElementById('admin-metrics-container').innerHTML =
                `<div style="color:#ef4444;text-align:center;">Unauthorized or service unavailable.</div>`;
        }
    } catch (e) {
        console.error('Admin fetch error:', e);
        document.getElementById('admin-metrics-container').innerHTML =
            `<div style="color:#ef4444;text-align:center;">Network error. Check your connection.</div>`;
    }
}

async function dispatchCheckoutPipeline(targetBaseTier) {
    const selectedRadio = document.querySelector(`input[name="${targetBaseTier}-sub-selector"]:checked`);
    if (!selectedRadio) {
        alert("Please select a plan option.");
        return;
    }
    const selectedSubConfig = selectedRadio.value;
    const checkoutBtn = document.querySelector(`.${targetBaseTier}-premium .upgrade-btn`);
    const originalText = checkoutBtn.innerText;
    checkoutBtn.innerText = "Connecting to Stripe...";
    checkoutBtn.style.opacity = "0.7";
    checkoutBtn.disabled = true;

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000);

    try {
        const response = await fetch(`${API_BASE_URL}/api/billing/checkout`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
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

async function saveCustomInstructions() {
    const input = document.getElementById('instructions-input');
    const btn = document.querySelector('#instructions-modal .modal-submit-btn');
    const originalText = btn.innerText;
    btn.innerText = "Saving...";
    btn.disabled = true;
    try {
        await fetch(`${API_BASE_URL}/api/user/instructions`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
            body: JSON.stringify({ instructions: input.value })
        });
        btn.innerText = "Saved!";
        btn.style.background = "var(--accent-glow)";
        btn.style.color = "#fff";
        setTimeout(() => {
            closeModals();
            btn.innerText = originalText;
            btn.style.background = "#fff";
            btn.style.color = "#000";
            btn.disabled = false;
        }, 1000);
    } catch (error) {
        btn.innerText = originalText;
        btn.disabled = false;
    }
}

async function submitTelemetryReport() {
    const type = document.getElementById('report-type').value;
    const description = document.getElementById('feedback-input').value.trim();
    if (!description) return;
    const btn = document.querySelector('#feedback-modal .modal-submit-btn');
    const originalText = btn.innerText;
    btn.innerText = "Dispatching...";
    btn.disabled = true;
    try {
        const response = await fetch(`${API_BASE_URL}/api/reports`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${googleAuthUserToken}` },
            body: JSON.stringify({ type, description })
        });
        if (response.ok) {
            btn.innerText = "Log Secured";
            btn.style.background = "var(--accent-secondary)";
            btn.style.color = "#fff";
            setTimeout(() => {
                closeModals();
                document.getElementById('feedback-input').value = '';
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

// ============================================================
// PRIVACY & HELP
// ============================================================
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

// ============================================================
// ADD ANOTHER ACCOUNT
// ============================================================
function addAnotherAccount() {
    if (confirm('Switch to another Google account?')) {
        localStorage.removeItem('google_auth_token');
        location.reload();
    }
}

// ============================================================
// PRICING / CHECKOUT
// ============================================================
function syncTierMatrixEngine(tierGroup, subTierSelection, derivedCostValue) {
    const targetOutputNode = document.getElementById(`${tierGroup}-base-price-output`);
    if (targetOutputNode) {
        targetOutputNode.innerHTML =
            `$${derivedCostValue}<span style="font-size:14px;color:#555;font-weight:400;">/mo</span>`;
    }
}

// ============================================================
// DELETE ALL CHATS & DELETE ACCOUNT
// ============================================================
async function deleteAllChats() {
    if (!confirm("⚠️ Are you sure you want to permanently delete ALL your chat history? This cannot be undone.")) {
        return;
    }
    try {
        const response = await fetch(`${API_BASE_URL}/api/history/delete-all`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${googleAuthUserToken}` }
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
    if (!confirm("⚠️ Do you really want to delete your account? This will permanently remove all your data and cannot be reversed.")) {
        return;
    }
    if (!confirm("Are you absolutely sure? This action is irreversible.")) {
        return;
    }
    try {
        const response = await fetch(`${API_BASE_URL}/api/user/delete`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${googleAuthUserToken}` }
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

// ============================================================
// SWIPE-TO-CLOSE SIDEBAR
// ============================================================
const sidebar = document.getElementById('sidebar-container-node');
const swipeHandle = document.createElement('div');
swipeHandle.className = 'swipe-handle';
sidebar.prepend(swipeHandle);

let startX = 0, currentX = 0, isDragging = false;

swipeHandle.addEventListener('touchstart', (e) => {
    startX = e.touches[0].clientX;
    isDragging = true;
});

document.addEventListener('touchmove', (e) => {
    if (!isDragging) return;
    currentX = e.touches[0].clientX;
    const delta = currentX - startX;
    if (sidebar.classList.contains('open') && delta < 0) {
        const newLeft = Math.min(0, delta);
        sidebar.style.transition = 'none';
        sidebar.style.left = `${newLeft}px`;
    }
});

document.addEventListener('touchend', () => {
    if (!isDragging) return;
    isDragging = false;
    sidebar.style.transition = 'all 0.3s ease';
    if (sidebar.classList.contains('open')) {
        const currentLeft = parseFloat(sidebar.style.left) || 0;
        if (currentLeft < -50) {
            sidebar.classList.remove('open');
        }
        sidebar.style.left = '';
    }
    startX = 0;
    currentX = 0;
});

let isMouseDown = false;
swipeHandle.addEventListener('mousedown', (e) => {
    isMouseDown = true;
    startX = e.clientX;
    e.preventDefault();
});
document.addEventListener('mousemove', (e) => {
    if (!isMouseDown) return;
    currentX = e.clientX;
    const delta = currentX - startX;
    if (sidebar.classList.contains('open') && delta < 0) {
        sidebar.style.transition = 'none';
        sidebar.style.left = `${delta}px`;
    }
});
document.addEventListener('mouseup', () => {
    if (!isMouseDown) return;
    isMouseDown = false;
    sidebar.style.transition = 'all 0.3s ease';
    if (sidebar.classList.contains('open')) {
        const currentLeft = parseFloat(sidebar.style.left) || 0;
        if (currentLeft < -50) {
            sidebar.classList.remove('open');
        }
        sidebar.style.left = '';
    }
});

function toggleTheme() {
    document.body.classList.toggle('light-theme');
    localStorage.setItem('axelr_theme', document.body.classList.contains('light-theme') ? 'light' : 'dark');
}
if (localStorage.getItem('axelr_theme') === 'light') {
    document.body.classList.add('light-theme');
}

// ============================================================
// SUBSCRIPTION MODAL
// ============================================================
function openSubscriptionModal() {
    closeModals();
    const modal = document.getElementById('subscription-modal');
    const planName = document.getElementById('sub-plan-name').innerText;
    const isFree = planName.toLowerCase().includes('free');
    const content = document.getElementById('subscription-content');
    
    if (isFree) {
        content.innerHTML = `
            <div style="padding:20px 0;text-align:center;">
                <span class="material-symbols-rounded" style="font-size:48px;color:var(--accent-glow);">rocket_launch</span>
                <h3 style="color:#fff;margin:12px 0;">You are on the Free Plan</h3>
                <p style="color:var(--text-muted);font-size:14px;">Unlock unlimited extractions, UI generations, and priority support.</p>
            </div>
        `;
    } else {
        content.innerHTML = `
            <div style="display:flex;flex-direction:column;gap:5px;">
                <div class="profile-stat-row"><span class="profile-stat-label">Current Plan</span><span class="profile-stat-value" style="color:#fff;">${planName}</span></div>
            </div>
        `;
    }
    modal.classList.add('active');
}

function updateSubscriptionModal() {
    const planName = document.getElementById('sub-plan-name').innerText;
    const freeMsg = document.getElementById('free-tier-message');
    const paidMsg = document.getElementById('paid-tier-message');
    const paidName = document.getElementById('paid-tier-name');
    const detailsSpan = document.getElementById('subscription-details');

    if (planName.toLowerCase().includes('free')) {
        freeMsg.style.display = 'block';
        paidMsg.style.display = 'none';
    } else {
        freeMsg.style.display = 'none';
        paidMsg.style.display = 'block';
        paidName.innerText = planName.replace(' ALLOCATION', '');
        detailsSpan.innerText = 'Active subscription. Manage your plan via Stripe.';
    }
}

// ============================================================
// VIEWPORT PADDING
// ============================================================
function adjustViewportPadding() {
    if (!commandWrapper) return;
    
    const wrapperHeight = commandWrapper.offsetHeight;
    const fileChips = document.getElementById('file-staging-container');
    const chipsHeight = fileChips && stagedFiles.length > 0 ? fileChips.offsetHeight : 0;
    
    let totalPadding = wrapperHeight + 20;
    if (chipsHeight > 0) {
        totalPadding += chipsHeight + 10;
    }
    
    const minPadding = 120;
    totalPadding = Math.max(totalPadding, minPadding);
    
    viewport.style.paddingBottom = totalPadding + 'px';
}

// ============================================================
// RESIZE HANDLER
// ============================================================
let resizeHandlerTimeout = null;
let isResizeHandling = false;

window.addEventListener('resize', () => {
    if (isResizeHandling) return;
    isResizeHandling = true;
    
    clearTimeout(resizeHandlerTimeout);
    resizeHandlerTimeout = setTimeout(() => {
        if (document.body.classList.contains('workspace-data') || document.body.classList.contains('workspace-design')) {
            adjustViewportPadding();
            renderFileChips();
        }
        isResizeHandling = false;
    }, 150);
});

// ============================================================
// INIT
// ============================================================
console.log('🟢 Axelr AI Frontend Loaded (v4.3.0)');
console.log('📡 API Base URL:', API_BASE_URL);

window.onerror = function(message, source, lineno, colno, error) {
    console.error('Global error:', message, error);
    return true;
};

const today = new Date();
today.setUTCHours(0, 0, 0, 0);
const lastVisit = localStorage.getItem('axelr_last_visit');
if (!lastVisit || new Date(lastVisit) < today) {
    localStorage.setItem('axelr_last_visit', today.toISOString());
}

// ============================================================
// VERSION & CACHE CONTROL - safe version check (no clear)
// ============================================================
const APP_VERSION = '4.3.0';
const BUILD_DATE = '2026-07-28';

console.log(`🟢 Axelr AI v${APP_VERSION} (Build: ${BUILD_DATE})`);
console.log('📡 API Base URL:', API_BASE_URL);

// Only log version, never clear localStorage
const storedVersion = localStorage.getItem('axelr_app_version');
if (storedVersion && storedVersion !== APP_VERSION) {
    console.log('🔄 Version mismatch detected (stored:', storedVersion, 'current:', APP_VERSION, ') - no action taken.');
} else if (!storedVersion) {
    localStorage.setItem('axelr_app_version', APP_VERSION);
}

// ============================================================
// SERVICE WORKER CLEANUP - SINGLE CLEAN VERSION
// ============================================================
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations()
        .then(registrations => {
            for (let registration of registrations) {
                registration.unregister();
                console.log('🧹 Unregistered service worker:', registration.scope);
            }
        })
        .catch(err => console.warn('SW cleanup error:', err));
}

// ============================================================
// FINAL INIT
// ============================================================
loadUserProfile().then(() => {
    loadArchiveLogs().then(() => {
        const storedSessionId = localStorage.getItem('axelr_active_session');
        if (storedSessionId) {
            viewPastLogById(storedSessionId);
        }
        setTimeout(setupViewportObserver, 500);
    });
});