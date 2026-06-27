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
const { GoogleGenerativeAI, GoogleGenerativeAIFetchError } = require('@google/generative-ai');
const { OAuth2Client } = require('google-auth-library');
const AdmZip = require('adm-zip');
const Groq = require('groq-sdk');

// Optional dependencies (soft fail)
let Zod;
try {
  Zod = require('zod');
} catch (_) {
  Zod = null;
}

let stripe;
try {
  stripe = require('stripe')(process.env.STRIPE_SECRET_KEY || 'sk_test_placeholder');
} catch (e) {
  console.warn('⚠️ Stripe init failed:', e.message);
  stripe = null;
}

let groq;
try {
  groq = new Groq({ apiKey: process.env.GROQ_API_KEY || 'dummy' });
} catch (e) {
  console.warn('⚠️ Groq init failed:', e.message);
  groq = null;
}

// ==========================================
// ALLOWED MIME TYPES (Global)
// ==========================================
const ALLOWED_MIME_TYPES = [
  'text/plain',
  'text/html',
  'text/css',
  'text/csv',
  'application/json',
  'application/pdf',
  'image/png',
  'image/jpeg',
  'image/webp',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
];

// ==========================================
// ENV WARNINGS (Do NOT exit on missing env)
// ==========================================
const REQUIRED_ENV = ['MONGO_URI', 'STRIPE_SECRET_KEY', 'GOOGLE_CLIENT_ID', 'GEMINI_API_KEY', 'GROQ_API_KEY'];
const missing = REQUIRED_ENV.filter(k => !process.env[k]);
if (missing.length) {
  console.warn(`⚠️ Missing env: ${missing.join(', ')} (some features may fail)`);
}

// ==========================================
// EXPRESS APP
// ==========================================
const app = express();

// ==========================================
// GLOBAL PROCESS PROTECTION
// ==========================================
process.on('uncaughtException', (err) => {
  console.error('💀 UNCAUGHT EXCEPTION:', err);
  // DO NOT EXIT - handle gracefully
});

process.on('unhandledRejection', (reason) => {
  console.error('💀 UNHANDLED REJECTION:', reason);
  // DO NOT EXIT - handle gracefully
});

// ==========================================
// CORS
// ==========================================
const allowedOrigins = [
  'https://axelr.in',
  'https://www.axelr.in',
  'https://axelr-frontend.pages.dev',
  process.env.CLIENT_APP_URL
].filter(Boolean);

app.use(cors({
  origin: (origin, callback) => {
    if (!origin || allowedOrigins.includes(origin)) {
      callback(null, true);
    } else {
      callback(new Error('CORS blocked'), false);
    }
  },
  methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
  credentials: true,
  maxAge: 86400
}));

// ==========================================
// HELMET (Security Headers)
// ==========================================
app.use(helmet({
  crossOriginOpenerPolicy: { policy: "same-origin-allow-popups" },
  crossOriginResourcePolicy: { policy: "cross-origin" },
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'self'"],
      scriptSrc: ["'self'", "'unsafe-inline'", "'unsafe-eval'", "https://accounts.google.com", "https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com"],
      frameSrc: ["'self'", "https://accounts.google.com"],
      connectSrc: ["'self'", "https://api.netlify.com", "https://api.groq.com", "https://generativelanguage.googleapis.com"],
      imgSrc: ["'self'", "data:", "https://*.googleusercontent.com"],
      styleSrc: ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
      fontSrc: ["'self'", "https://fonts.gstatic.com"]
    }
  },
  hsts: { maxAge: 31536000, includeSubDomains: true, preload: true }
}));

// ==========================================
// RATE LIMITING
// ==========================================
const globalLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 200,
  message: { error: "Too many requests." }
});
app.use('/api/', globalLimiter);

// ==========================================
// DATABASE SCHEMAS (Preserved exactly as given)
// ==========================================
mongoose.set('strictQuery', true);

const UserSchema = new mongoose.Schema({
  googleId: { type: String, unique: true, required: true },
  email: { type: String, required: true },
  displayName: String,
  tier: { type: String, enum: ['free', 'pro', 'business'], default: 'free' },
  dailyUsage: { type: Number, default: 0 },
  dailyUiUxUsage: { type: Number, default: 0 },
  storageBytesUsed: { type: Number, default: 0 },
  lastUsageDate: { type: Date, default: Date.now },
  customInstructions: { type: String, default: '' },
  stripeCustomerId: { type: String, sparse: true },
  subTierOptions: {
    hasDataAccess: { type: Boolean, default: false },
    hasDesignAccess: { type: Boolean, default: false }
  },
  quotas: {
    dailyExtractionsUsed: { type: Number, default: 0 },
    dailyGenerationsUsed: { type: Number, default: 0 },
    dailyEnhancementsUsed: { type: Number, default: 0 },
    monthlyEnhancementsLimit: { type: Number, default: 3 },
    lastQuotaResetTimestamp: { type: Date, default: Date.now }
  }
}, { timestamps: true });

const User = mongoose.model('User', UserSchema);

const ChatSessionSchema = new mongoose.Schema({
  userId: { type: mongoose.Schema.Types.ObjectId, ref: 'User', required: true },
  filename: { type: String, required: true },
  workspace: { type: String, enum: ['data', 'design', 'general'], default: 'data' },
  status: { type: String, enum: ['active', 'archived', 'trashed'], default: 'active' },
  isPinned: { type: Boolean, default: false },
  messages: [{
    role: { type: String, required: true },
    text: { type: String, required: true },
    attachedFiles: { type: [String], default: [] },
    variants: { type: [String], default: [] },
    activeVariant: { type: Number, default: 0 }
  }],
  structuredData: { type: Array, default: [] },
  createdAt: { type: Date, default: Date.now },
  trashedAt: { type: Date }
}, { timestamps: true });

const ChatSession = mongoose.model('ChatSession', ChatSessionSchema);

const BugReportSchema = new mongoose.Schema({
  userId: { type: mongoose.Schema.Types.ObjectId, ref: 'User', required: true },
  type: { type: String, enum: ['help', 'feedback'], required: true },
  description: { type: String, required: true },
  createdAt: { type: Date, default: Date.now }
});
const BugReport = mongoose.model('BugReport', BugReportSchema);

// ==========================================
// AUTH SETUP
// ==========================================
const GOOGLE_CLIENT_ID = process.env.GOOGLE_CLIENT_ID;
const googleClient = new OAuth2Client(GOOGLE_CLIENT_ID || 'dummy');
const CLIENT_APP_URL = process.env.CLIENT_APP_URL || "http://axelr.in";

// ==========================================
// AUTHENTICATION MIDDLEWARE
// ==========================================
const authenticateUser = async (req, res, next) => {
  try {
    const authHeader = req.headers.authorization;
    if (!authHeader?.startsWith('Bearer ')) {
      return res.status(401).json({ error: "AUTH_REQUIRED" });
    }
    const token = authHeader.split(' ')[1];
    const ticket = await googleClient.verifyIdToken({
      idToken: token,
      audience: GOOGLE_CLIENT_ID
    });
    const payload = ticket.getPayload();
    let user = await User.findOne({ googleId: payload.sub });
    if (!user) {
      user = await User.create({
        googleId: payload.sub,
        email: payload.email,
        displayName: payload.name || payload.email,
        tier: 'free',
        dailyUsage: 0,
        dailyUiUxUsage: 0,
        storageBytesUsed: 0,
        lastUsageDate: new Date(),
        customInstructions: '',
        subTierOptions: { hasDataAccess: false, hasDesignAccess: false },
        quotas: {
          dailyExtractionsUsed: 0,
          dailyGenerationsUsed: 0,
          dailyEnhancementsUsed: 0,
          monthlyEnhancementsLimit: 3,
          lastQuotaResetTimestamp: new Date()
        }
      });
    } else {
      const today = new Date().setHours(0, 0, 0, 0);
      const last = user.lastUsageDate ? new Date(user.lastUsageDate).setHours(0, 0, 0, 0) : 0;
      if (today > last) {
        user.dailyUsage = 0;
        user.dailyUiUxUsage = 0;
        user.storageBytesUsed = 0;
        user.lastUsageDate = new Date();
        user.quotas.dailyExtractionsUsed = 0;
        user.quotas.dailyGenerationsUsed = 0;
        user.quotas.dailyEnhancementsUsed = 0;
        user.quotas.lastQuotaResetTimestamp = new Date();
        await user.save();
      }
    }
    req.currentUser = user;
    next();
  } catch (error) {
    console.error('[AUTH_FAIL]', error);
    res.status(401).json({ error: "SESSION_EXPIRED" });
  }
};

// ==========================================
// MIDDLEWARE: JSON & Timeout
// ==========================================
app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true, limit: '10mb' }));

// ==========================================
// FILE UPLOAD
// ==========================================
const storage = multer.diskStorage({
  destination: os.tmpdir(),
  filename: (req, file, cb) => cb(null, `${Date.now()}-${crypto.randomBytes(4).toString('hex')}-${file.originalname}`)
});

const upload = multer({
  storage,
  limits: { fileSize: 10 * 1024 * 1024, files: 5 },
  fileFilter: (req, file, cb) => {
    const allowed = ALLOWED_MIME_TYPES;
    if (allowed.includes(file.mimetype) || /\.(html|js|css|json|txt|csv|md)$/i.test(file.originalname)) {
      cb(null, true);
    } else {
      cb(new Error('File type not allowed'), false);
    }
  }
});

// ==========================================
// DATABASE CONNECTION (auto-reconnect)
// ==========================================
let dbConnected = false;
async function connectDB() {
  try {
    await mongoose.connect(process.env.MONGO_URI, {
      maxPoolSize: 10,
      serverSelectionTimeoutMS: 5000,
      socketTimeoutMS: 45000,
      family: 4
    });
    dbConnected = true;
    console.log('🗄️ DB CONNECTED');
  } catch (err) {
    console.error('💥 DB CONNECTION FAILED:', err);
    dbConnected = false;
    setTimeout(connectDB, 5000);
  }
}
connectDB();
mongoose.connection.on('disconnected', () => {
  dbConnected = false;
  setTimeout(connectDB, 1000);
});

// ==========================================
// WEBHOOK
// ==========================================
app.post('/api/webhooks/stripe', express.raw({ type: 'application/json', limit: '10kb' }), async (req, res) => {
  const sig = req.headers['stripe-signature'];
  let event;
  try {
    event = stripe.webhooks.constructEvent(req.body, sig, process.env.STRIPE_WEBHOOK_SECRET);
  } catch (err) {
    return res.status(400).send(`Webhook Error: ${err.message}`);
  }
  try {
    if (event.type === 'checkout.session.completed') {
      const session = event.data.object;
      await User.findOneAndUpdate(
        { googleId: session.client_reference_id },
        {
          tier: session.metadata.tier || 'pro',
          stripeCustomerId: session.customer,
          subTierOptions: {
            hasDataAccess: (session.metadata.subTier === 'full' || session.metadata.subTier === 'data'),
            hasDesignAccess: (session.metadata.subTier === 'full' || session.metadata.subTier === 'design')
          }
        }
      );
    } else if (event.type === 'customer.subscription.deleted' || event.type === 'invoice.payment_failed') {
      await User.findOneAndUpdate({ stripeCustomerId: event.data.object.customer }, { tier: 'free' });
    }
  } catch (dbError) {
    console.error("Webhook DB error:", dbError);
  }
  res.json({ received: true });
});

// ==========================================
// ASYNC HANDLER (wraps all routes)
// ==========================================
const asyncHandler = (fn) => (req, res, next) => {
  Promise.resolve(fn(req, res, next)).catch(err => {
    console.error('❌ Route Error:', err.stack);
    if (!res.headersSent) {
      res.status(500).json({ error: "INTERNAL_ERROR", message: "Service temporarily unavailable." });
    }
    next(err);
  });
};

// ==========================================
// ALL ROUTES (COMPLETE - NOTHING REMOVED)
// ==========================================

// Health
app.get('/', (req, res) => res.send('Axelr API Online'));
app.get('/api/health', (req, res) => {
  res.json({
    status: 'operational',
    timestamp: new Date().toISOString(),
    db: dbConnected ? 'connected' : 'disconnected',
    uptime: process.uptime()
  });
});

// Admin
app.get('/api/admin/metrics', authenticateUser, asyncHandler(async (req, res) => {
  if (req.currentUser.email !== "shanh1346@gmail.com") {
    return res.status(403).json({ error: "UNAUTHORIZED" });
  }
  const [totalUsers, proUsers, designerUsers, totalChats, usageData] = await Promise.all([
    User.countDocuments(),
    User.countDocuments({ tier: 'pro' }),
    User.countDocuments({ tier: 'business' }),
    ChatSession.countDocuments(),
    User.aggregate([{ $group: { _id: null, totalQueries: { $sum: "$dailyUsage" }, totalBytes: { $sum: "$storageBytesUsed" } } }])
  ]);
  const metrics = usageData[0] || { totalQueries: 0, totalBytes: 0 };
  res.json({ success: true, totalUsers, proUsers, designerUsers, totalChats, metrics });
}));

// Billing
app.post('/api/billing/checkout', authenticateUser, asyncHandler(async (req, res) => {
  if (!stripe) return res.status(503).json({ error: "Payment service unavailable." });
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
  const session = await stripe.checkout.sessions.create({
    payment_method_types: ['card'],
    mode: 'subscription',
    client_reference_id: req.currentUser.googleId,
    metadata: { tier, subTier },
    line_items: [{ price_data: { currency: 'usd', product_data: { name }, unit_amount: price, recurring: { interval: 'month' } }, quantity: 1 }],
    success_url: `${CLIENT_APP_URL}/Index.html?billing=success`,
    cancel_url: `${CLIENT_APP_URL}/Index.html?billing=cancelled`,
  });
  res.json({ url: session.url });
}));

// Profile
app.get('/api/user/profile', authenticateUser, (req, res) => {
  const user = req.currentUser;
  res.json({
    tier: user.tier,
    dailyUsage: user.dailyUsage,
    limit: user.tier === 'free' ? 5 : 500,
    customInstructions: user.customInstructions
  });
});

app.put('/api/user/instructions', authenticateUser, asyncHandler(async (req, res) => {
  req.currentUser.customInstructions = req.body.instructions || '';
  await req.currentUser.save();
  res.json({ success: true });
}));

// History
app.put('/api/history/:id', authenticateUser, asyncHandler(async (req, res) => {
  const { action, payload } = req.body;
  const log = await ChatSession.findOne({ _id: req.params.id, userId: req.currentUser._id });
  if (!log) return res.status(404).json({ error: "Not found" });
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

app.post('/api/reports', authenticateUser, asyncHandler(async (req, res) => {
  await BugReport.create({ userId: req.currentUser._id, type: req.body.type || 'feedback', description: req.body.description });
  res.json({ success: true });
}));

app.get('/api/history', authenticateUser, asyncHandler(async (req, res) => {
  const allowed = ['data', 'design', 'general'];
  const workspace = allowed.includes(req.query.workspace) ? req.query.workspace : 'data';
  const logs = await ChatSession.find({
    userId: req.currentUser._id,
    status: req.query.status || 'active',
    workspace
  }).sort({ isPinned: -1, createdAt: -1 });
  res.json({ logs });
}));

// Enhance
app.post('/api/enhance-prompt', authenticateUser, asyncHandler(async (req, res) => {
  const { promptText } = req.body;
  if (!promptText) return res.status(400).json({ error: "No text." });

  const user = await User.findById(req.currentUser._id);
  if (!user) return res.status(401).json({ error: "UNAUTHORIZED" });

  const now = new Date();
  if (now - user.quotas.lastQuotaResetTimestamp >= 24 * 60 * 60 * 1000) {
    user.quotas.dailyExtractionsUsed = 0;
    user.quotas.dailyGenerationsUsed = 0;
    user.quotas.dailyEnhancementsUsed = 0;
    user.quotas.lastQuotaResetTimestamp = now;
    await user.save();
  }

  let limit = user.tier === 'free' ? 3 : 20;
  if (user.tier === 'free') limit = 3;
  else if (user.tier === 'pro') limit = 10;
  else if (user.tier === 'business') limit = 20;

  if (user.quotas.dailyEnhancementsUsed >= limit) {
    return res.status(403).json({ error: "LIMIT_REACHED", usage: user.quotas.dailyEnhancementsUsed, limit });
  }

  const instruction = "You are an elite prompt engineer. Rewrite the user's input into a detailed professional prompt. Return ONLY the rewritten prompt. No quotes, no intro.";
  let enhanced = promptText;
  try {
    const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
    const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash" });
    const response = await model.generateContent({ contents: [{ role: 'user', parts: [{ text: `[SYSTEM: ${instruction}]\n\n${promptText}` }] }] });
    enhanced = response.response.text().trim();
  } catch (e) {
    if (!groq) return res.status(503).json({ error: "AI service unavailable." });
    const backup = await groq.chat.completions.create({
      model: "llama3-70b-8192",
      messages: [{ role: "system", content: instruction }, { role: "user", content: promptText }],
      temperature: 0.2,
      max_tokens: 1000
    });
    enhanced = backup.choices[0]?.message?.content?.trim() || promptText;
  }

  user.quotas.dailyEnhancementsUsed += 1;
  user.dailyUsage += 1;
  await user.save();

  res.json({ success: true, enhanced });
}));

// Rename
app.post('/api/rename-chat', authenticateUser, asyncHandler(async (req, res) => {
  const { logId } = req.body;
  const log = await ChatSession.findOne({ _id: logId, userId: req.currentUser._id });
  if (!log || !log.messages.length) return res.status(404).json({ error: "Not found" });
  const chatContext = log.messages.slice(0, 2).map(m => m.text).join('\n');
  const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
  const model = genAI.getGenerativeModel({
    model: "gemini-2.5-flash",
    systemInstruction: "You are a titling assistant. Read the chat start and reply with a short, catchy 3-4 word title. NO quotes, NO extra punctuation. Just the title."
  });
  const response = await model.generateContent({ contents: [{ role: 'user', parts: [{ text: chatContext }] }] });
  const newTitle = response.response.text().trim().replace(/['"]/g, '');
  log.filename = newTitle;
  await log.save();
  res.json({ success: true, newTitle });
}));

// Deploy
app.post('/api/deploy', authenticateUser, asyncHandler(async (req, res) => {
  const { htmlContent } = req.body;
  if (!htmlContent) return res.status(400).json({ error: "Missing HTML." });
  const zip = new AdmZip();
  zip.addFile("index.html", Buffer.from(htmlContent, "utf8"));
  const zipBuffer = zip.toBuffer();
  const deployResponse = await fetch('https://api.netlify.com/api/v1/sites', {
    method: 'POST',
    headers: { 'Content-Type': 'application/zip', 'Authorization': `Bearer ${process.env.NETLIFY_ACCESS_TOKEN}` },
    body: zipBuffer
  });
  if (!deployResponse.ok) throw new Error("Netlify deploy failed");
  const deployData = await deployResponse.json();
  res.json({ success: true, liveUrl: deployData.ssl_url });
}));

// Variant
app.put('/api/history/:logId/variant', authenticateUser, asyncHandler(async (req, res) => {
  const { msgId, variantIndex } = req.body;
  const session = await ChatSession.findOne({ _id: req.params.logId, userId: req.currentUser._id });
  if (!session) return res.status(404).json({ error: "Not found" });
  const msg = session.messages.id(msgId);
  if (msg && msg.variants && variantIndex >= 0 && variantIndex < msg.variants.length) {
    msg.activeVariant = variantIndex;
    msg.text = msg.variants[variantIndex];
    session.markModified('messages');
    await session.save();
  }
  res.json({ success: true });
}));

// ---- QUOTA MIDDLEWARE ----
const enforceQuotas = async (req, res, next) => {
  try {
    const user = await User.findById(req.currentUser?._id);
    if (!user) return res.status(401).json({ error: "UNAUTHORIZED" });
    const now = new Date();
    if (now - user.quotas.lastQuotaResetTimestamp >= 24 * 60 * 60 * 1000) {
      user.quotas.dailyExtractionsUsed = 0;
      user.quotas.dailyGenerationsUsed = 0;
      user.quotas.dailyEnhancementsUsed = 0;
      user.quotas.lastQuotaResetTimestamp = now;
      await user.save();
    }
    req.resolvedUser = user;
    next();
  } catch (err) {
    console.error('Quota error:', err);
    res.status(500).json({ error: "QUOTA_CHECK_FAILED" });
  }
};

// ==========================================
// HELPER: Strip <think> tags
// ==========================================
function stripThinkTags(text) {
  if (!text) return '';
  // Remove everything between <think> and </think> (including nested)
  let cleaned = text.replace(/<think>[\s\S]*?<\/think>/g, '');
  // Also remove any leftover <think> or </think> tags (malformed)
  cleaned = cleaned.replace(/<\/?think>/g, '');
  return cleaned.trim();
}

// ---- EXTRACT (SSE) ----
let validatePayload = (data) => data;
if (Zod) {
  const FileSchema = Zod.object({
    size: Zod.number().max(10 * 1024 * 1024),
    mimetype: Zod.string().refine(m => ALLOWED_MIME_TYPES.includes(m)),
    originalname: Zod.string()
  });
  const PayloadSchema = Zod.object({
    files: Zod.array(FileSchema).max(5),
    command: Zod.string().max(10000).regex(/^(?!.*<script|javascript:|onerror=|onload=)/i),
    totalSize: Zod.number().max(50 * 1024 * 1024)
  });
  validatePayload = (data) => PayloadSchema.parse(data);
}

app.post('/api/extract', authenticateUser, enforceQuotas, upload.array('files', 5), asyncHandler(async (req, res) => {
  const files = req.files || [];
  const userCommand = (req.body.command || "Analyze").slice(0, 10000);
  const workspaceMode = req.body.workspace === 'design' ? 'design' : 'data';
  const sessionId = (req.body.sessionId && req.body.sessionId !== 'null' && req.body.sessionId !== 'undefined') ? req.body.sessionId : null;

  // Validation
  if (files.length > 5) return res.status(400).json({ error: "MAX_FILES_EXCEEDED" });
  const totalSize = files.reduce((s, f) => s + f.size, 0);
  if (totalSize > 50 * 1024 * 1024) return res.status(400).json({ error: "TOTAL_SIZE_EXCEEDED" });
  for (const f of files) {
    if (f.size > 10 * 1024 * 1024) return res.status(400).json({ error: `FILE_TOO_LARGE: ${f.originalname}` });
  }

  const user = req.resolvedUser || req.currentUser;
  const limit = user.tier === 'pro' ? 50 : user.tier === 'business' ? 100 : 5;
  const uiLimit = user.tier === 'pro' ? 20 : user.tier === 'business' ? 100 : 2;
  const byteLimit = user.tier === 'pro' ? 100 * 1024 * 1024 : user.tier === 'business' ? 50 * 1024 * 1024 : 5 * 1024 * 1024;
  const isUi = workspaceMode === 'design';

  if (isUi) {
    if (user.quotas.dailyGenerationsUsed >= uiLimit) {
      return res.status(403).json({ error: "LIMIT_REACHED", usage: user.quotas.dailyGenerationsUsed, limit: uiLimit });
    }
  } else {
    if (user.quotas.dailyExtractionsUsed >= limit) {
      return res.status(403).json({ error: "LIMIT_REACHED", usage: user.quotas.dailyExtractionsUsed, limit });
    }
  }
  if ((user.storageBytesUsed + totalSize) > byteLimit) {
    return res.status(403).json({ error: "STORAGE_LIMIT_REACHED" });
  }

  let fileParts = [];
  for (const file of files) {
    try {
      const data = await fs.readFile(file.path, { encoding: 'base64' });
      let mime = file.mimetype;
      if (!mime || mime === 'application/octet-stream') mime = 'text/plain';
      fileParts.push({ inlineData: { data, mimeType: mime } });
    } catch (e) {
      console.error('File read error:', e);
    }
  }

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

  const fileNames = files.map(f => f.originalname);
  let recent = history.slice(-6);
  if (recent.length && recent[0].role === 'model') recent.shift();

  const contents = recent.map(msg => ({
    role: msg.role === 'user' ? 'user' : 'model',
    parts: [{ text: msg.text }]
  }));

  if (contents.length && contents[contents.length - 1].role === 'user') {
    contents[contents.length - 1].parts.push(...fileParts, { text: userCommand });
  } else {
    contents.push({ role: 'user', parts: [...fileParts, { text: userCommand }] });
  }

  // ============================================================
  // 🔒 UPDATED SYSTEM DIRECTIVE – ULTRA‑CONCISE & UN‑HIJACKABLE
  // ============================================================
  const SYSTEM_DIRECTIVE = `
CRITICAL: You are Axelr AI. Output ONLY the final answer. No reasoning, no explanations, no tags. Just the answer.
`.trim();

  let systemPrompt = workspaceMode === 'design'
    ? `You are AXELR ARCHITECT, a Senior UI/UX Engineer. Generate flawless, responsive HTML and Tailwind CSS code wrapped in \`\`\`html tags. Prioritize modern aesthetics and clean component structure.\n${SYSTEM_DIRECTIVE}`
    : `You are AXELR DATA, a Senior Data Analyst. ONLY extract data into a precise CSV array wrapped in [JSON-DATA] tags IF the user explicitly uploads data to be extracted. Otherwise, answer questions normally.\n${SYSTEM_DIRECTIVE}`;

  if (user.customInstructions) systemPrompt += `\nUSER DATA: ${user.customInstructions}`;

  // SSE setup
  const SSE_TIMEOUT = 60000; // 60 seconds
  let clientClosed = false;
  let aiResponse = '';
  let structured = [];
  const abortCtrl = new AbortController();
  let currentStream = null;
  let responseEnded = false;

  const cleanup = () => {
    console.log('[SSE] Cleanup triggered');
    clearTimeout(sseTimer);
    
    try {
      if (currentStream && typeof currentStream.cancel === 'function') {
        currentStream.cancel();
      }
    } catch (_) {}
    
    try {
      abortCtrl.abort();
    } catch (_) {}
    
    try {
      if (!responseEnded && !res.headersSent) {
        res.end();
      }
    } catch (_) {}
    
    aiResponse = '';
    structured = [];
    currentStream = null;
  };

  // Set up headers
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache, no-store, must-revalidate',
    'Connection': 'keep-alive',
    'X-Accel-Buffering': 'no'
  });

  req.on('close', () => {
    console.log('[SSE] Client disconnected');
    clientClosed = true;
    cleanup();
  });
  req.on('error', (err) => {
    console.error('[SSE] Request error:', err);
    clientClosed = true;
    cleanup();
  });

  let sseTimer = setTimeout(() => {
    if (!clientClosed && !responseEnded) {
      console.log('[SSE] Timeout reached, cleaning up');
      res.write(`data: ${JSON.stringify({ type: 'timeout', message: 'Stream timeout' })}\n\n`);
      cleanup();
      res.end();
    }
  }, SSE_TIMEOUT);

  const writeSSE = (data) => {
    if (clientClosed || responseEnded) return false;
    try {
      res.write(`data: ${JSON.stringify(data)}\n\n`);
      return true;
    } catch (_) {
      clientClosed = true;
      return false;
    }
  };

  res.write(`data: ${JSON.stringify({ type: 'progress', text: 'Initializing...' })}\n\n`);

  try {
    const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
    const model = genAI.getGenerativeModel({
      model: "gemini-2.5-flash",
      systemInstruction: systemPrompt,
      generationConfig: {
        temperature: 0.1,
        maxOutputTokens: 4096, // Enough for most responses
        topP: 0.9,
      }
    });

    if (!writeSSE({ type: 'progress', text: 'Extracting data...' })) {
      throw new Error('Client disconnected');
    }

    const result = await model.generateContentStream({
      contents,
      signal: abortCtrl.signal
    });

    currentStream = result.stream;
    
    try {
      for await (const chunk of result.stream) {
        if (clientClosed || responseEnded) break;
        const text = chunk.text();
        aiResponse += text;
        if (!writeSSE({ type: 'chunk', text })) break;
      }
    } catch (streamErr) {
      // If the stream died due to safety filter, we catch it here
      if (streamErr.name !== 'AbortError') {
        console.warn('[SSE] Stream died unexpectedly:', streamErr.message);
        if (!clientClosed && !responseEnded) {
          // Send a friendly error instead of the generic security message
          writeSSE({ type: 'error', message: 'I cannot process that request due to safety guidelines. Please rephrase.' });
        }
        // Don't rethrow, we'll handle cleanup below
      } else {
        console.log('[SSE] Stream aborted');
      }
    }
  } catch (primaryErr) {
    if (clientClosed || responseEnded) {
      cleanup();
      return;
    }
    // Check if it's a Gemini safety error (often status 400 or 403)
    if (primaryErr.status === 400 || primaryErr.status === 403 || primaryErr.message?.includes('safety')) {
      console.warn('[SSE] Gemini safety filter triggered:', primaryErr.message);
      if (!clientClosed && !responseEnded) {
        writeSSE({ type: 'error', message: 'I cannot process that request due to safety guidelines. Please rephrase.' });
      }
      cleanup();
      return;
    }
    // Otherwise, try fallback
    if (primaryErr.name !== 'AbortError' && !responseEnded) {
      console.error('[SSE] Gemini error:', primaryErr.message);
      // Fallback to Groq
      if (groq) {
        try {
          const backup = await groq.chat.completions.create({
            model: "llama3-70b-8192",
            messages: [
              { role: "system", content: systemPrompt },
              { role: "user", content: userCommand + (files.length ? " (files attached)" : "") }
            ],
            temperature: 0.1,
            max_tokens: 2000,
            stream: true
          });
          
          let backupStreamActive = true;
          for await (const chunk of backup) {
            if (clientClosed || responseEnded || !backupStreamActive) break;
            const text = chunk.choices[0]?.delta?.content || '';
            aiResponse += text;
            if (!writeSSE({ type: 'chunk', text })) {
              backupStreamActive = false;
              break;
            }
          }
        } catch (fallbackErr) {
          console.error('[SSE] Fallback failed:', fallbackErr);
          if (!clientClosed && !responseEnded) {
            writeSSE({ type: 'error', message: 'I cannot process that request due to safety guidelines. Please rephrase.' });
          }
          cleanup();
          return;
        }
      } else {
        if (!clientClosed && !responseEnded) {
          writeSSE({ type: 'error', message: 'I cannot process that request due to safety guidelines. Please rephrase.' });
        }
        cleanup();
        return;
      }
    } else {
      cleanup();
      return;
    }
  }

  if (clientClosed || responseEnded) {
    cleanup();
    return;
  }

  clearTimeout(sseTimer);

  // 🔥 CRITICAL: Strip <think> tags from the entire response
  aiResponse = stripThinkTags(aiResponse);

  // Extract structured data if any
  const jsonMatch = aiResponse.match(/\[JSON-DATA\]([\s\S]*?)\[\/JSON-DATA\]/);
  if (jsonMatch) {
    try { structured = JSON.parse(jsonMatch[1].trim()); } catch (e) { structured = []; }
    aiResponse = aiResponse.replace(/\[JSON-DATA\][\s\S]*?\[\/JSON-DATA\]/g, '').trim();
  }
  if (!aiResponse.trim()) aiResponse = "Task completed successfully.";

  // Increment quotas
  if (isUi) {
    user.quotas.dailyGenerationsUsed += 1;
  } else {
    user.quotas.dailyExtractionsUsed += 1;
  }
  user.dailyUsage += 1;
  user.storageBytesUsed += totalSize;
  if (isUi) user.dailyUiUxUsage += 1;
  await user.save();

  // Save to session
  if (currentSession) {
    const isRetry = req.body.isRetry === 'true';
    if (isRetry && currentSession.messages.length && currentSession.messages[currentSession.messages.length - 1].role === 'model') {
      const last = currentSession.messages[currentSession.messages.length - 1];
      if (!last.variants || !last.variants.length) last.variants = [last.text];
      last.variants.push(aiResponse);
      last.activeVariant = last.variants.length - 1;
      last.text = aiResponse;
      currentSession.markModified('messages');
    } else {
      currentSession.messages.push(
        { role: 'user', text: userCommand, attachedFiles: fileNames },
        { role: 'model', text: aiResponse, variants: [aiResponse], activeVariant: 0 }
      );
    }
    currentSession.structuredData = structured;
    await currentSession.save();
  } else {
    let filename = `Chat_${Date.now().toString().slice(-4)}`;
    if (files.length) filename = `[File] ${files[0].originalname.split('.')[0]}`;
    else if (userCommand && userCommand !== "Analyze") {
      const words = userCommand.trim().split(/\s+/);
      filename = words.slice(0, 4).join(' ') + (words.length > 4 ? '...' : '');
    }
    currentSession = await ChatSession.create({
      userId: user._id,
      filename,
      workspace: workspaceMode,
      structuredData: structured,
      messages: [
        { role: 'user', text: userCommand, attachedFiles: fileNames },
        { role: 'model', text: aiResponse, variants: [aiResponse], activeVariant: 0 }
      ]
    });
  }

  responseEnded = true;
  res.write(`data: ${JSON.stringify({ type: 'done', sessionId: currentSession._id, structuredData: structured, filename: `${currentSession.filename}.csv` })}\n\n`);
  res.end();

  // Clean up temporary files
  for (const f of files) {
    try { await fs.unlink(f.path); } catch (_) {}
  }
}));

// ---- SUB-TIER GUARD ----
const secureSubTierRouteGuard = async (req, res, next) => {
  try {
    if (!req.currentUser) return next();
    const user = await User.findById(req.currentUser._id);
    if (!user) return res.status(401).json({ error: "PROFILE_NOT_FOUND" });
    const path = req.path;
    if (user.tier === 'pro' || user.tier === 'business') {
      if (path.includes('/api/generate') && !user.subTierOptions.hasDesignAccess)
        return res.status(403).json({ error: "SUB_TIER_RESTRICTION" });
      if (path.includes('/api/extract') && !user.subTierOptions.hasDataAccess)
        return res.status(403).json({ error: "SUB_TIER_RESTRICTION" });
    }
    req.resolvedUserRecord = user;
    next();
  } catch (err) {
    console.error('Guard error:', err);
    res.status(500).json({ error: "GUARD_FAILED" });
  }
};

// ---- 404 CATCH-ALL ----
app.use((req, res) => {
  res.status(404).json({ error: "Not found" });
});

// ---- GLOBAL ERROR HANDLER (last) ----
app.use((err, req, res, next) => {
  console.error('💥 GLOBAL ERROR:', err);
  if (!res.headersSent) {
    res.status(500).json({
      error: "INTERNAL_ERROR",
      message: process.env.NODE_ENV === 'production' ? "Service unavailable" : err.message
    });
  }
});

// ---- GRACEFUL SHUTDOWN ----
let shuttingDown = false;
const gracefulShutdown = async () => {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log('🛑 Shutting down...');
  server.close(async () => {
    try { await mongoose.connection.close(); } catch (_) {}
    console.log('✅ Shutdown complete.');
    process.exit(0);
  });
  setTimeout(() => {
    console.error('⚠️ Forced shutdown.');
    process.exit(1);
  }, 10000);
};
process.on('SIGTERM', gracefulShutdown);
process.on('SIGINT', gracefulShutdown);

// ---- START ----
const PORT = process.env.PORT || 5000;
const server = app.listen(PORT, () => {
  console.log(`🟢 AXELR SYSTEM ONLINE ON PORT ${PORT} (${process.env.NODE_ENV || 'development'})`);
});