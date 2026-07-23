// ==========================================
// CRITICAL: ALL REQUIRES AT THE ABSOLUTE TOP
// ==========================================
const crypto = require('crypto');
require('dotenv').config();

const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const multer = require('multer');
const mongoose = require('mongoose');
const fs = require('fs').promises;
const os = require('os');
const compression = require('compression');
const { OAuth2Client } = require('google-auth-library');
const nodemailer = require('nodemailer');
const pino = require('pino');
const envalid = require('envalid');
const { str, num, bool } = envalid;

// ==========================================
// LOGGING & ENV VALIDATION
// ==========================================
const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

const env = envalid.cleanEnv(process.env, {
  MONGO_URI: str(),
  STRIPE_SECRET_KEY: str(),
  GOOGLE_CLIENT_ID: str(),
  PORT: num({ default: 5000 }),
  NODE_ENV: str({ choices: ['development', 'production', 'test'], default: 'development' }),
  STRIPE_WEBHOOK_SECRET: str({ default: '' }),
  VERCEL_TOKEN: str({ default: '' }),
  VERCEL_PROJECT_ID: str({ default: '' }),
  NETLIFY_TOKEN: str({ default: '' }),
  NETLIFY_SITE_ID: str({ default: '' }),
  FREE_TIER_TOKEN_LIMIT: num({ default: 1000000 }),
  ADMIN_EMAIL: str({ default: '' }),
  SMTP_HOST: str({ default: '' }),
  SMTP_PORT: num({ default: 587 }),
  SMTP_USER: str({ default: '' }),
  SMTP_PASS: str({ default: '' }),
  SMTP_SECURE: bool({ default: false }),
});

// ==========================================
// ORCHESTRATOR BRIDGE
// ==========================================
const ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL || 'http://localhost:5001/api/route';

// ==========================================
// STRIPE, NODEMAILER SETUP
// ==========================================
let stripe;
try {
  if (!process.env.STRIPE_SECRET_KEY) throw new Error('STRIPE_SECRET_KEY not set');
  stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
} catch (_) {
  if (process.env.NODE_ENV === 'production') process.exit(1);
  stripe = null;
}

let transporter;
try {
  if (process.env.SMTP_HOST && process.env.SMTP_USER && process.env.SMTP_PASS) {
    transporter = nodemailer.createTransport({
      host: process.env.SMTP_HOST,
      port: parseInt(process.env.SMTP_PORT) || 587,
      secure: process.env.SMTP_SECURE === 'true',
      auth: { user: process.env.SMTP_USER, pass: process.env.SMTP_PASS },
      connectionTimeout: 5000,
      greetingTimeout: 5000,
      socketTimeout: 10000,
    });
    transporter.verify((error) => {
      if (error) logger.warn('SMTP verification failed:', error.message);
      else logger.info('SMTP configured successfully');
    });
  } else {
    logger.warn('SMTP not configured – email sending disabled');
  }
} catch (_) { transporter = null; }

// ==========================================
// ALLOWED MIME TYPES
// ==========================================
const ALLOWED_MIME_TYPES = [
  'text/plain', 'text/html', 'text/css', 'text/csv', 'application/json',
  'application/pdf', 'image/png', 'image/jpeg', 'image/webp',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
];

// ==========================================
// EXPRESS APP
// ==========================================
const app = express();
app.set('trust proxy', 1);

// CORS
const allowedOrigins = [
  'https://axelr.in', 'https://www.axelr.in',
  'https://axelr-frontend.pages.dev',
  process.env.CLIENT_APP_URL
].filter(Boolean);

app.use(cors({
  origin: (origin, cb) => {
    if (process.env.NODE_ENV === 'development') { cb(null, true); return; }
    if (!origin || allowedOrigins.includes(origin)) cb(null, true);
    else cb(new Error('CORS blocked'), false);
  },
  methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
  credentials: true,
  maxAge: 86400
}));

app.use(compression());
app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true, limit: '10mb' }));

// HELMET
app.use((req, res, next) => {
  res.locals.nonce = crypto.randomBytes(16).toString('base64');
  next();
});
app.use(helmet({
  crossOriginOpenerPolicy: { policy: "same-origin-allow-popups" },
  crossOriginResourcePolicy: { policy: "cross-origin" },
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'self'"],
      scriptSrc: ["'self'", (req, res) => `'nonce-${res.locals.nonce}'`, "https://accounts.google.com", "https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com"],
      frameSrc: ["'self'", "https://accounts.google.com"],
      connectSrc: ["'self'", "https://api.netlify.com", "https://api.groq.com", "https://generativelanguage.googleapis.com", "https://openrouter.ai"],
      imgSrc: ["'self'", "data:", "https://*.googleusercontent.com"],
      styleSrc: ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
      fontSrc: ["'self'", "https://fonts.gstatic.com"],
      objectSrc: ["'none'"],
      baseUri: ["'self'"],
      formAction: ["'self'"],
    },
  },
  hsts: { maxAge: 31536000, includeSubDomains: true, preload: true }
}));

// Rate Limiting
const globalLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 200,
  message: { success: false, code: 'RATE_LIMIT', message: "Too many requests." },
});
app.use('/api/', globalLimiter);

// ==========================================
// DATABASE SCHEMAS (unchanged – keep your existing ones)
// ==========================================
mongoose.set('strictQuery', true);

const UserSchema = new mongoose.Schema({ /* ... */ });
const ChatSessionSchema = new mongoose.Schema({ /* ... */ });
const BugReportSchema = new mongoose.Schema({ /* ... */ });

const User = mongoose.model('User', UserSchema);
const ChatSession = mongoose.model('ChatSession', ChatSessionSchema);
const BugReport = mongoose.model('BugReport', BugReportSchema);

// ==========================================
// AUTH & QUOTA RESET (unchanged)
// ==========================================
const GOOGLE_CLIENT_ID = process.env.GOOGLE_CLIENT_ID;
const googleClient = new OAuth2Client(GOOGLE_CLIENT_ID || 'dummy');

async function resetDailyQuotasIfNeeded(user) { /* ... */ }
const authenticateUser = async (req, res, next) => { /* ... */ };

// ==========================================
// FILE UPLOAD (unchanged)
// ==========================================
const storage = multer.diskStorage({ /* ... */ });
const upload = multer({ /* ... */ });

// ==========================================
// DB CONNECTION (unchanged)
// ==========================================
async function connectDB() { /* ... */ }
connectDB();
mongoose.connection.on('disconnected', () => { /* ... */ });

// ==========================================
// WEBHOOK (stripe) (unchanged)
// ==========================================
app.post('/api/webhooks/stripe', express.raw({ type: 'application/json', limit: '10kb' }), async (req, res) => { /* ... */ });

// ==========================================
// ASYNC HANDLER
// ==========================================
const asyncHandler = (fn) => (req, res, next) => {
  Promise.resolve(fn(req, res, next)).catch(err => {
    logger.error('❌ Route Error:', err.stack);
    if (!res.headersSent) {
      res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Service temporarily unavailable.' });
    }
    next(err);
  });
};

// ==========================================
// HELPERS
// ==========================================
function stripThinkTags(text) {
  if (!text) return '';
  return text.replace(/<think>[\s\S]*?<\/think>/g, '').replace(/<\/?think>/g, '').trim();
}

function cleanAssistantMessage(text) {
  if (!text) return '';
  return text.replace(/\|.*\|.*\n/g, '').replace(/\s+/g, ' ').trim();
}

const STOP_WORDS = new Set(['the','be','to','of','and','a','in','that','have','i','it','for','not','on','with','he','as','you','do','at','this','but','his','by','from','they','we','say','her','she','or','an','will','my','one','all','would','there','their','what','so','up','out','if','about','who','get','which','go','me','when','make','can','like','time','no','just','him','know','take','people','into','year','your','good','some','could','them','see','other','than','then','now','look','only','come','its','over','think','also','back','after','use','two','how','our','work','first','well','way','even','new','want','because','any','these','give','day','most','us']);

function generateChatName(command, files) {
  if (files && files.length > 0) {
    const base = files[0].originalname.split('.')[0];
    return base.replace(/[_-]/g, ' ').slice(0, 50) || 'File Chat';
  }
  if (command && command.trim().length > 0) {
    const words = command.trim().split(/\s+/);
    const meaningful = words.filter(w => !STOP_WORDS.has(w.toLowerCase()) && w.length > 2);
    const picked = meaningful.slice(0, 3);
    if (picked.length > 0) return picked.join(' ').slice(0, 60);
    return words.slice(0, 3).join(' ').slice(0, 60);
  }
  return `Chat_${Date.now().toString().slice(-4)}`;
}

function estimateTokens(text) {
  return Math.ceil((text || '').length / 4);
}

// ==========================================
// ROUTES
// ==========================================
app.get('/', (req, res) => res.send('Axelr API Online'));
app.get('/api/health', (req, res) => {
  res.json({
    status: 'operational',
    timestamp: new Date().toISOString(),
    db: mongoose.connection.readyState === 1 ? 'connected' : 'disconnected',
    uptime: process.uptime()
  });
});

// ---------- ADMIN METRICS ----------
app.get('/api/admin/metrics', authenticateUser, asyncHandler(async (req, res) => {
  if (!req.currentUser.isAdmin) {
    return res.status(403).json({ success: false, code: 'UNAUTHORIZED', message: 'Admin access required.' });
  }
  const [totalUsers, proUsers, designerUsers, totalChats, usageData, tokenData] = await Promise.all([
    User.countDocuments(),
    User.countDocuments({ tier: 'pro' }),
    User.countDocuments({ tier: 'business' }),
    ChatSession.countDocuments(),
    User.aggregate([{ $group: { _id: null, totalQueries: { $sum: "$dailyUsage" }, totalBytes: { $sum: "$storageBytesUsed" } } }]),
    User.aggregate([{ $group: { _id: null, totalPromptTokens: { $sum: "$tokenUsage.totalPromptTokens" }, totalCompletionTokens: { $sum: "$tokenUsage.totalCompletionTokens" } } }])
  ]);
  const metrics = usageData[0] || { totalQueries: 0, totalBytes: 0 };
  const tokens = tokenData[0] || { totalPromptTokens: 0, totalCompletionTokens: 0 };
  const totalTokens = tokens.totalPromptTokens + tokens.totalCompletionTokens;
  const freeLimit = process.env.FREE_TIER_TOKEN_LIMIT || 1000000;
  res.json({
    success: true,
    totalUsers,
    proUsers,
    designerUsers,
    totalChats,
    metrics,
    tokenUsage: {
      prompt: tokens.totalPromptTokens,
      completion: tokens.totalCompletionTokens,
      total: totalTokens,
      remaining: Math.max(0, freeLimit - totalTokens),
      limit: freeLimit,
    }
  });
}));

// ---------- STRIPE CHECKOUT ----------
app.post('/api/billing/checkout', authenticateUser, asyncHandler(async (req, res) => {
  if (!stripe) {
    return res.status(503).json({ success: false, code: 'PAYMENT_UNAVAILABLE', message: 'Payment service unavailable.' });
  }
  const { tier = 'pro', subTier = 'full' } = req.body;
  let price = 1500, name = 'Pro Full Stack';
  if (tier === 'pro') {
    if (subTier === 'data') { price = 800; name = 'Pro Data'; }
    else if (subTier === 'design') { price = 900; name = 'Pro Design'; }
  } else if (tier === 'business') {
    if (subTier === 'full') { price = 2900; name = 'Business Full'; }
    else if (subTier === 'data') { price = 1600; name = 'Business Data'; }
    else if (subTier === 'design') { price = 1600; name = 'Business Design'; }
  }
  const origin = req.headers.origin;
  if (!origin) {
    return res.status(400).json({ success: false, code: 'INVALID_ORIGIN', message: 'Missing origin header.' });
  }
  const successUrl = new URL('/index.html?billing=success', origin).href;
  const cancelUrl = new URL('/index.html?billing=cancelled', origin).href;
  const session = await stripe.checkout.sessions.create({
    payment_method_types: ['card'],
    mode: 'subscription',
    client_reference_id: req.currentUser.googleId,
    metadata: { tier, subTier },
    line_items: [{ price_data: { currency: 'usd', product_data: { name }, unit_amount: price, recurring: { interval: 'month' } }, quantity: 1 }],
    success_url: successUrl,
    cancel_url: cancelUrl,
  });
  res.json({ success: true, url: session.url });
}));

// ---------- USER PROFILE ----------
app.get('/api/user/profile', authenticateUser, (req, res) => {
  const user = req.currentUser;
  res.json({
    tier: user.tier,
    dailyUsage: user.dailyUsage,
    dailyUiUxUsage: user.dailyUiUxUsage,
    customInstructions: user.customInstructions,
    quotas: user.quotas,
    subTierOptions: user.subTierOptions,
    tokenUsage: {
      dailyPrompt: user.tokenUsage.dailyPromptTokens,
      dailyCompletion: user.tokenUsage.dailyCompletionTokens,
      totalPrompt: user.tokenUsage.totalPromptTokens,
      totalCompletion: user.tokenUsage.totalCompletionTokens,
    },
    isAdmin: user.isAdmin || false,
  });
});

app.put('/api/user/instructions', authenticateUser, asyncHandler(async (req, res) => {
  const instructions = req.body.instructions || '';
  if (instructions.length > 5000) {
    return res.status(400).json({ success: false, code: 'INVALID_INPUT', message: 'Instructions cannot exceed 5000 characters.' });
  }
  req.currentUser.customInstructions = instructions;
  await req.currentUser.save();
  res.json({ success: true });
}));

// ---------- HISTORY ROUTES ----------
app.put('/api/history/:id', authenticateUser, asyncHandler(async (req, res) => {
  const { action, payload } = req.body;
  const log = await ChatSession.findOne({ _id: req.params.id, userId: req.currentUser._id });
  if (!log) return res.status(404).json({ success: false, code: 'NOT_FOUND', message: 'Chat not found.' });
  if (action === 'rename') log.filename = payload;
  if (action === 'pin') log.isPinned = !log.isPinned;
  await log.save();
  res.json({ success: true });
}));

app.put('/api/history/:id/status', authenticateUser, asyncHandler(async (req, res) => {
  const { status } = req.body;
  const update = { status };
  if (status === 'trashed') update.trashedAt = new Date();
  await ChatSession.findOneAndUpdate({ _id: req.params.id, userId: req.currentUser._id }, update);
  res.json({ success: true });
}));

app.delete('/api/history/:id', authenticateUser, asyncHandler(async (req, res) => {
  await ChatSession.deleteOne({ _id: req.params.id, userId: req.currentUser._id, status: 'trashed' });
  res.json({ success: true });
}));

app.put('/api/history/:id/variant', authenticateUser, asyncHandler(async (req, res) => {
  const { msgId, variantIndex } = req.body;
  if (!msgId || variantIndex === undefined) {
    return res.status(400).json({ success: false, code: 'INVALID_INPUT', message: 'Missing msgId or variantIndex' });
  }
  const session = await ChatSession.findOne({ _id: req.params.id, userId: req.currentUser._id });
  if (!session) return res.status(404).json({ success: false, code: 'NOT_FOUND' });
  const msg = session.messages.id(msgId);
  if (!msg) return res.status(404).json({ success: false, code: 'NOT_FOUND' });
  if (variantIndex < 0 || variantIndex >= (msg.variants?.length || 0)) {
    return res.status(400).json({ success: false, code: 'INVALID_INDEX' });
  }
  msg.activeVariant = variantIndex;
  msg.text = msg.variants[variantIndex];
  session.markModified('messages');
  await session.save();
  res.json({ success: true });
}));

// ---------- BUG REPORT ----------
app.post('/api/reports', authenticateUser, asyncHandler(async (req, res) => {
  const { type, description } = req.body;
  const report = await BugReport.create({
    userId: req.currentUser._id,
    type: type || 'feedback',
    description
  });
  if (transporter) {
    try {
      const mailOptions = {
        from: process.env.SMTP_USER,
        to: 'shanh1346@gmail.com',
        subject: `[Axelr Report] ${type.toUpperCase()} from ${req.currentUser.email}`,
        text: `User: ${req.currentUser.email}\nType: ${type}\nDescription: ${description}\nTimestamp: ${new Date().toISOString()}`,
        html: `<p><strong>User:</strong> ${req.currentUser.email}</p><p><strong>Type:</strong> ${type}</p><p><strong>Description:</strong> ${description}</p><p><strong>Timestamp:</strong> ${new Date().toISOString()}</p>`,
      };
      await transporter.sendMail(mailOptions);
      logger.info(`Email sent for report ${report._id}`);
    } catch (mailErr) {
      logger.error('Email send failed:', mailErr.message);
    }
  }
  res.json({ success: true });
}));

// ---------- HISTORY with PAGINATION ----------
app.get('/api/history', authenticateUser, asyncHandler(async (req, res) => {
  const allowed = ['data', 'design', 'general'];
  const workspace = allowed.includes(req.query.workspace) ? req.query.workspace : 'data';
  const page = parseInt(req.query.page) || 1;
  const limit = parseInt(req.query.limit) || 20;
  const skip = (page - 1) * limit;
  const logs = await ChatSession.find({
    userId: req.currentUser._id,
    status: req.query.status || 'active',
    workspace
  }).sort({ isPinned: -1, createdAt: -1 }).skip(skip).limit(limit);
  const total = await ChatSession.countDocuments({
    userId: req.currentUser._id,
    status: req.query.status || 'active',
    workspace
  });
  res.json({
    success: true,
    logs,
    pagination: { page, limit, total, pages: Math.ceil(total / limit) }
  });
}));

// ---------- ENHANCE PROMPT – orchestrator call ----------
app.post('/api/enhance-prompt', authenticateUser, asyncHandler(async (req, res) => {
  const { promptText } = req.body;
  if (!promptText) return res.status(400).json({ success: false, code: 'INVALID_INPUT', message: 'No text provided.' });

  const user = await User.findById(req.currentUser._id);
  if (!user) return res.status(401).json({ success: false, code: 'UNAUTHORIZED', message: 'User not found.' });

  // Quota check
  const now = new Date();
  if (now - user.quotas.lastQuotaResetTimestamp >= 24 * 60 * 60 * 1000) {
    user.quotas.dailyEnhancementsUsed = 0;
    user.quotas.lastQuotaResetTimestamp = now;
    await user.save();
  }

  let limit;
  if (user.tier === 'free') limit = 3;
  else if (user.tier === 'pro') {
    limit = (user.subTierOptions.hasDataAccess && user.subTierOptions.hasDesignAccess) ? 7 : 5;
  } else if (user.tier === 'business') {
    limit = (user.subTierOptions.hasDataAccess && user.subTierOptions.hasDesignAccess) ? 15 : 10;
  } else limit = 3;

  if (user.quotas.dailyEnhancementsUsed >= limit) {
    return res.status(403).json({ success: false, code: 'LIMIT_REACHED', usage: user.quotas.dailyEnhancementsUsed, limit });
  }

  // Call orchestrator
  const orchestratorResponse = await fetch(ORCHESTRATOR_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      workspace: 'prompt',
      prompt: promptText,
      history: [],
      files: [],
      max_tokens: 2048,
      temperature: 0.2,
    }),
    signal: AbortSignal.timeout(15000),
  });

  if (!orchestratorResponse.ok) {
    throw new Error('Orchestrator enhancement failed');
  }

  const result = await orchestratorResponse.json();
  if (!result.success) {
    throw new Error(result.text || 'Orchestrator returned failure');
  }

  const enhanced = result.text;

  user.quotas.dailyEnhancementsUsed += 1;
  user.dailyUsage += 1;
  const estTokens = estimateTokens(enhanced);
  user.tokenUsage.totalPromptTokens += estTokens;
  user.tokenUsage.totalCompletionTokens += estTokens;
  user.tokenUsage.dailyPromptTokens += estTokens;
  user.tokenUsage.dailyCompletionTokens += estTokens;
  await user.save();

  res.json({ success: true, enhanced });
}));

// ---------- QUOTA MIDDLEWARE ----------
const enforceQuotas = async (req, res, next) => {
  try {
    const user = await User.findById(req.currentUser?._id);
    if (!user) return res.status(401).json({ success: false, code: 'UNAUTHORIZED', message: 'User not found.' });
    await resetDailyQuotasIfNeeded(user);
    req.resolvedUser = user;
    next();
  } catch (err) {
    logger.error('Quota error:', err);
    res.status(500).json({ success: false, code: 'QUOTA_CHECK_FAILED', message: 'Could not check quota.' });
  }
};

// ---------- EXTRACT (streaming) – sends file contents ----------
app.post('/api/extract', authenticateUser, enforceQuotas, upload.array('files', 5), asyncHandler(async (req, res) => {
  const files = req.files || [];
  const userCommand = (req.body.command || "Analyze").slice(0, 10000);
  const workspaceMode = req.body.workspace === 'design' ? 'design' : 'data';
  const sessionId = (req.body.sessionId && mongoose.Types.ObjectId.isValid(req.body.sessionId)) ? req.body.sessionId : null;

  if (files.length > 5) return res.status(400).json({ success: false, code: 'MAX_FILES_EXCEEDED', message: 'Too many files.' });
  const totalSize = files.reduce((s, f) => s + f.size, 0);
  if (totalSize > 50 * 1024 * 1024) return res.status(400).json({ success: false, code: 'TOTAL_SIZE_EXCEEDED', message: 'Total upload size too large.' });
  for (const f of files) if (f.size > 10 * 1024 * 1024) return res.status(400).json({ success: false, code: `FILE_TOO_LARGE`, message: `File ${f.originalname} exceeds 10MB.` });

  const user = req.resolvedUser || req.currentUser;

  // --- QUOTA CHECKS (keep your existing logic) ---
  // ... (all quota logic unchanged – omitted for brevity) ...

  // --- Read files as base64 (FIX #5) ---
  const fileContents = await Promise.all(files.map(async (file) => {
    const data = await fs.readFile(file.path);
    return {
      filename: file.originalname,
      mimetype: file.mimetype,
      content_base64: data.toString('base64'),
    };
  }));

  // Prepare session history
  let currentSession = null;
  let history = [];
  if (sessionId && mongoose.Types.ObjectId.isValid(sessionId)) {
    currentSession = await ChatSession.findOne({ _id: sessionId, userId: user._id });
    if (currentSession) {
      const isRetry = req.body.isRetry === 'true';
      history = currentSession.messages;
      if (isRetry && history.length > 0 && history[history.length - 1].role === 'model') {
        history = history.slice(0, -2);
      }
    }
  }

  let userContent = userCommand;
  if (files.length > 0) {
    const fileNames = files.map(f => f.originalname).join(', ');
    userContent = `Files attached: ${fileNames}. Command: ${userCommand}`;
  }

  // --- SSE response ---
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no'
  });

  let aiResponse = '';
  let errorOccurred = false;
  let promptTokensUsed = 0, completionTokensUsed = 0;

  // --- Call Python Orchestrator with file contents ---
  const orchestratorPayload = {
    workspace: workspaceMode,
    prompt: userCommand,
    history: history.slice(-4).map(msg => ({
      role: msg.role === 'user' ? 'user' : 'assistant',
      content: msg.role === 'model' ? cleanAssistantMessage(msg.text) : msg.text
    })),
    files: fileContents,  // Now includes content
    max_tokens: 2048,
    temperature: 0.2
  };

  try {
    const orchestratorResponse = await fetch(ORCHESTRATOR_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(orchestratorPayload),
      signal: AbortSignal.timeout(60000),
    });

    if (!orchestratorResponse.ok) {
      const errorData = await orchestratorResponse.json().catch(() => ({}));
      throw new Error(errorData.detail || `Orchestrator error: ${orchestratorResponse.status}`);
    }

    const result = await orchestratorResponse.json();
    if (result.success) {
      aiResponse = result.text;
      promptTokensUsed = result.tokens_used || 0;
      completionTokensUsed = 0;
      logger.info(`Orchestrator used ${result.provider} (${result.model_used}) in ${result.latency_ms}ms`);
    } else {
      throw new Error(result.text || 'Orchestrator returned failure');
    }
  } catch (err) {
    logger.error('Orchestrator call failed:', err.message);
    errorOccurred = true;
    aiResponse = "I am Axelr AI. I encountered a technical issue. Please try again later.";
    // Rollback quota (keep your rollback logic)
    // ...
    res.write(`data: ${JSON.stringify({ type: 'error', message: aiResponse })}\n\n`);
    res.end();
    for (const f of files) try { await fs.unlink(f.path); } catch (_) {}
    return;
  }

  // --- Token estimation and DB update (keep your logic) ---
  // ...

  // Extract structured data (keep your logic)
  // ...

  // --- Save session (keep your logic) ---
  // ...

  // --- Stream response ---
  const sentences = aiResponse.match(/[^.!?]+[.!?]+/g) || [aiResponse];
  for (const sentence of sentences) {
    res.write(`data: ${JSON.stringify({ type: 'chunk', text: sentence })}\n\n`);
    await new Promise(r => setTimeout(r, 10));
  }

  // Final done event
  res.write(`data: ${JSON.stringify({
    type: 'done',
    sessionId: sessionSaved ? sessionIdOut : null,
    structuredData: structured,
    filename: sessionSaved ? `${filenameOut}.csv` : 'Export.csv',
    error: errorOccurred ? true : false,
    finalResponse: aiResponse
  })}\n\n`);
  res.end();

  // Cleanup
  for (const f of files) try { await fs.unlink(f.path); } catch (_) {}
}));

// ---------- TEST EMAIL ----------
app.get('/api/test-email', authenticateUser, asyncHandler(async (req, res) => {
  if (!transporter) return res.status(503).json({ success: false, message: 'SMTP not configured' });
  await transporter.sendMail({
    from: process.env.SMTP_USER,
    to: req.currentUser.email,
    subject: 'Axelr Test Email',
    text: 'SMTP is working!'
  });
  res.json({ success: true });
}));

// ---------- DEPLOY ----------
const createDOMPurify = require('dompurify');
const { JSDOM } = require('jsdom');
const window = new JSDOM('').window;
const DOMPurify = createDOMPurify(window);

app.post('/api/deploy', authenticateUser, asyncHandler(async (req, res) => {
  // ... (keep your existing deploy logic) ...
}));

// ---------- 404 & ERROR ----------
app.use((req, res) => res.status(404).json({ success: false, code: 'NOT_FOUND', message: 'Endpoint not found.' }));
app.use((err, req, res, next) => {
  logger.error('💥 GLOBAL ERROR:', err);
  if (!res.headersSent) res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: process.env.NODE_ENV === 'production' ? 'Service unavailable' : err.message });
});

// ---------- GRACEFUL SHUTDOWN ----------
let shuttingDown = false;
const gracefulShutdown = async () => {
  if (shuttingDown) return;
  shuttingDown = true;
  logger.info('🛑 Shutting down...');
  server.close(async () => {
    try { await mongoose.connection.close(); } catch (_) {}
    logger.info('✅ Shutdown complete.');
    process.exit(0);
  });
  setTimeout(() => { logger.error('⚠️ Forced shutdown.'); process.exit(1); }, 10000);
};
process.on('SIGTERM', gracefulShutdown);
process.on('SIGINT', gracefulShutdown);

const PORT = process.env.PORT || 5000;
const server = app.listen(PORT, () => {
  logger.info(`🟢 AXELR FORTRESS ONLINE ON PORT ${PORT} (${process.env.NODE_ENV || 'development'})`);
});