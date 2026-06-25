const Sentry = require("@sentry/node");
Sentry.init({
  dsn: process.env.SENTRY_DSN,
  environment: process.env.NODE_ENV
});
process.on('uncaughtException', (err) => console.error('Uncaught Exception:', err));
process.on('unhandledRejection', (reason, promise) => console.error('Unhandled Rejection:', reason));
require('dotenv').config();
const express = require('express');
const cors = require('cors');
const helmet = require('helmet'); 
const rateLimit = require('express-rate-limit'); 
const multer = require('multer');
const mongoose = require('mongoose');
const fs = require('fs');
const os = require('os');
const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const { GoogleGenerativeAI } = require('@google/generative-ai');
const { OAuth2Client } = require('google-auth-library');
const Groq = require('groq-sdk');
const AdmZip = require('adm-zip'); 
const { z } = require('zod');

const app = express();

app.use(cors({ 
    origin: [
        'https://axelr.in', 
        'https://www.axelr.in', 
        'https://axelr-frontend.pages.dev',
        process.env.CLIENT_APP_URL
    ],
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    credentials: true
}));

const GOOGLE_CLIENT_ID = process.env.GOOGLE_CLIENT_ID || "474929925590-a0it7ijp845oqbni72iaqpsvqdvnu0jd.apps.googleusercontent.com";
const googleClient = new OAuth2Client(GOOGLE_CLIENT_ID);
const groq = new Groq({ apiKey: process.env.GROQ_API_KEY });
const CLIENT_APP_URL = process.env.CLIENT_APP_URL || "http://localhost:5500";

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
    }
}));

const apiLimiter = rateLimit({ windowMs: 15 * 60 * 1000, max: 150 });
app.use('/api/', apiLimiter);
mongoose.set('strictQuery', true);

// ==========================================
// ENTERPRISE CONFIGURATIONS
// ==========================================

const ALLOWED_MIME_TYPES = [
    'text/plain', 'text/html', 'text/css', 'text/csv',
    'application/json', 'application/pdf',
    'image/png', 'image/jpeg', 'image/webp',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
];

const UserSchema = new mongoose.Schema({
    googleId: String,
    email: String,
    displayName: String,
    tier: { type: String, enum: ['free', 'pro', 'business'], default: 'free' },
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
});
UserSchema.index({ stripeCustomerId: 1 }, { sparse: true });
UserSchema.index({ tier: 1 }); 
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
        attachedFiles: { type: Array, default: [] },
        variants: { type: Array, default: [] },
        activeVariant: { type: Number, default: 0 }
    }],
    structuredData: { type: Array, default: [] },
    createdAt: { type: Date, default: Date.now },
    trashedAt: { type: Date }
});
ChatSessionSchema.index({ userId: 1, status: 1, isPinned: -1, createdAt: -1 });
ChatSessionSchema.index({ userId: 1, _id: 1 });
const ChatSession = mongoose.model('ChatSession', ChatSessionSchema);

const BugReportSchema = new mongoose.Schema({
    userId: { type: mongoose.Schema.Types.ObjectId, ref: 'User', required: true },
    type: { type: String, enum: ['help', 'feedback'], required: true },
    description: { type: String, required: true },
    createdAt: { type: Date, default: Date.now }
});
const BugReport = mongoose.model('BugReport', BugReportSchema);

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
    
    const user = await User.findOneAndUpdate(
      { googleId: ticket.getPayload().sub },
      { $setOnInsert: { email: ticket.getPayload().email } },
      { upsert: true, returnDocument: 'after' }
    );
    
    req.currentUser = user;
    next();
  } catch (error) {
    console.error('[AUTH_FAIL]', error);
    res.status(401).json({ error: "SESSION_EXPIRED" });
  }
};

// ==========================================
// BUSINESS PIPELINE APIS
// ==========================================

app.get('/api/admin/metrics', authenticateUser, async (req, res) => {
    const ADMIN_EMAIL = "shanh1346@gmail.com"; 
    if (req.currentUser.email !== ADMIN_EMAIL) {
        return res.status(403).json({ error: "UNAUTHORIZED_ACCESS" });
    }
    try {
        const totalUsers = await User.countDocuments() || 0;
        const proUsers = await User.countDocuments({ tier: 'pro' }) || 0;
        const designerUsers = await User.countDocuments({ tier: 'designer' }) || 0;
        const totalChats = await ChatSession.countDocuments() || 0;
        
        const usageData = await User.aggregate([{ $group: { _id: null, totalQueries: { $sum: "$quotas.dailyExtractionsUsed" } } }]);
        const metrics = usageData.length > 0 ? usageData[0] : { totalQueries: 0 };

        res.status(200).json({ 
            success: true, totalUsers, proUsers, designerUsers, totalChats, metrics,
            pipelineStatus: { gemini: 'ONLINE', db: 'SYNCED' }
        });
    } catch (e) { res.status(500).json({ error: "TELEMETRY_FAILED" }); }
});

app.post('/api/webhooks/stripe', express.raw({ type: 'application/json', limit: '10kb' }), async (req, res) => {
    const sig = req.headers['stripe-signature'];
    let event;
    try { event = stripe.webhooks.constructEvent(req.body, sig, process.env.STRIPE_WEBHOOK_SECRET); } 
    catch (err) { return res.status(400).send(`Webhook Error: ${err.message}`); }

    try {
        if (event.type === 'checkout.session.completed') {
            const session = event.data.object;
            const googleId = session.client_reference_id;
            const stripeCustomerId = session.customer; 
            
            const newTier = session.metadata.tier || 'pro';
            const newSubTier = session.metadata.subTier || 'full';
            
            const hasDataAccess = (newSubTier === 'full' || newSubTier === 'data');
            const hasDesignAccess = (newSubTier === 'full' || newSubTier === 'design');

            await User.findOneAndUpdate({ googleId }, { 
                tier: newTier, 
                stripeCustomerId,
                subTierOptions: { hasDataAccess, hasDesignAccess }
            });
        }
        else if (event.type === 'customer.subscription.deleted' || event.type === 'invoice.payment_failed') {
            const stripeCustomerId = event.data.object.customer;
            await User.findOneAndUpdate({ stripeCustomerId }, { tier: 'free' });
        }
    } catch (dbError) { console.error("💥 DB Sync Failure:", dbError.message); }
    res.json({ received: true });
});

app.use(express.json());
app.use((req, res, next) => { req.setTimeout(120000); next(); });

const storage = multer.diskStorage({ destination: os.tmpdir(), filename: (req, file, cb) => cb(null, `${Date.now()}-${file.originalname}`) });
const upload = multer({ storage: storage, limits: { fileSize: 100 * 1024 * 1024 } });

app.post('/api/billing/checkout', authenticateUser, async (req, res) => {
    try {
        const requestedTier = req.body.tier || 'pro';
        const subTier = req.body.subTier || 'full';
        
        let price = 1500; let name = 'Pro Full Stack Bundle';
        if (requestedTier === 'pro') {
            if (subTier === 'data') { price = 800; name = 'Pro Data Extraction'; }
            else if (subTier === 'design') { price = 900; name = 'Pro UI Generation'; }
        } else if (requestedTier === 'business') {
            if (subTier === 'full') { price = 2900; name = 'Business Full Stack'; }
            else if (subTier === 'data') { price = 1600; name = 'Business Data Ops'; }
            else if (subTier === 'design') { price = 1600; name = 'Business Designer'; }
        }

        const session = await stripe.checkout.sessions.create({
            payment_method_types: ['card'], 
            mode: 'subscription', 
            client_reference_id: req.currentUser.googleId,
            metadata: { tier: requestedTier, subTier: subTier }, 
            line_items: [{ price_data: { currency: 'usd', product_data: { name: name }, unit_amount: price, recurring: { interval: 'month' } }, quantity: 1 }],
            success_url: `${CLIENT_APP_URL}/Index.html?billing=success`, 
            cancel_url: `${CLIENT_APP_URL}/Index.html?billing=cancelled`,
        });
        res.status(200).json({ url: session.url });
    } catch (error) { res.status(500).json({ error: "Stripe secure drop." }); }
});

app.get('/api/user/profile', authenticateUser, (req, res) => { res.status(200).json({ tier: req.currentUser.tier, dailyUsage: req.currentUser.quotas.dailyExtractionsUsed, limit: req.currentUser.tier === 'free' ? 5 : 500, customInstructions: req.currentUser.customInstructions }); });
app.put('/api/user/instructions', authenticateUser, async (req, res) => { try { req.currentUser.customInstructions = req.body.instructions || ""; await req.currentUser.save(); res.status(200).json({ success: true }); } catch (e) { res.status(500).json({ error: e.message }); } });

app.put('/api/history/:id', authenticateUser, async (req, res) => {
    try {
        const { action, payload } = req.body;
        const log = await ChatSession.findOne({ _id: req.params.id, userId: req.currentUser._id });
        if (!log) return res.status(404).json({ error: "Not found" });
        if (action === 'rename') log.filename = payload;
        if (action === 'pin') log.isPinned = !log.isPinned;
        await log.save();
        res.status(200).json({ success: true });
    } catch (error) { res.status(500).json({ error: error.message }); }
});

app.put('/api/history/:id/status', authenticateUser, async (req, res) => { try { const { status } = req.body; const update = { status }; if (status === 'trashed') update.trashedAt = new Date(); await ChatSession.findOneAndUpdate({ _id: req.params.id, userId: req.currentUser._id }, update); res.status(200).json({ success: true }); } catch (e) { res.status(500).json({ error: e.message }); } });
app.delete('/api/history/:id', authenticateUser, async (req, res) => { try { await ChatSession.deleteOne({ _id: req.params.id, userId: req.currentUser._id, status: 'trashed' }); res.status(200).json({ success: true }); } catch (e) { res.status(500).json({ error: e.message }); } });

app.get('/api/history', authenticateUser, async (req, res) => { 
    try {
        const workspaceFilter = req.query.workspace || 'data';
        const workspaceQuery = workspaceFilter === 'data' ? { $in: ['data', null, ""] } : workspaceFilter;
        const logs = await ChatSession.find({ userId: req.currentUser._id, status: req.query.status || 'active', workspace: workspaceQuery }).sort({ isPinned: -1, createdAt: -1 }); 
        res.status(200).json({ logs }); 
    } catch (error) { res.status(500).json({ logs: [] }); }
});

app.post('/api/enhance-prompt', authenticateUser, async (req, res) => {
    try {
        const { promptText } = req.body;
        if (!promptText) return res.status(400).json({ error: "No text provided." });

        let limit = req.currentUser.tier === 'free' ? 5 : 100;
        if (req.currentUser.quotas.dailyExtractionsUsed >= limit) return res.status(403).json({ error: "LIMIT_REACHED" });

        const instruction = "You are an elite prompt engineer. Take the user's rough input and rewrite it into a highly detailed, professional prompt for an AI assistant. Return ONLY the rewritten prompt.";
        try {
            const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
            const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash" }); 
            const response = await model.generateContent({ contents: [{ role: 'user', parts: [{ text: `[SYSTEM INSTRUCTION: ${instruction}]\n\n${promptText}` }] }] });
            req.currentUser.quotas.dailyExtractionsUsed += 1;
            await req.currentUser.save();
            res.status(200).json({ success: true, enhanced: response.response.text().trim() });
        } catch (geminiError) {
            const backupResponse = await groq.chat.completions.create({ model: "llama3-70b-8192", messages: [{ role: "system", content: instruction }, { role: "user", content: promptText }], temperature: 0.2, max_tokens: 1000 });
            res.status(200).json({ success: true, enhanced: backupResponse.choices[0]?.message?.content?.trim() || promptText });
        }
    } catch (error) { res.status(500).json({ error: "Enhance failed" }); }
});

app.post('/api/rename-chat', authenticateUser, async (req, res) => {
    try {
        const { logId } = req.body;
        const log = await ChatSession.findOne({ _id: logId, userId: req.currentUser._id });
        if (!log || log.messages.length === 0) return res.status(404).json({ error: "Not found" });

        const chatContext = log.messages.slice(0, 2).map(m => m.text).join('\n');
        const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
        const model = genAI.getGenerativeModel({ 
            model: "gemini-2.5-flash",
            systemInstruction: "You are a titling assistant. Read the following chat start and reply with a short, catchy 3-4 word title. NO quotes, NO extra punctuation."
        });
        
        const response = await model.generateContent({ contents: [{ role: 'user', parts: [{ text: chatContext }] }] });
        const newTitle = response.response.text().trim().replace(/['"]/g, '');
        log.filename = newTitle; 
        await log.save();
        res.status(200).json({ success: true, newTitle });
    } catch (error) { res.status(500).json({ error: "Rename failed" }); }
});

const enforceAxelrPipelineQuotas = async (req, res, next) => {
    try {
        const user = await User.findById(req.currentUser?._id);
        if (!user) return res.status(401).json({ error: "UNAUTHORIZED_ACCESS" });

        const now = new Date();
        const performanceTimeDiff = now - user.quotas.lastQuotaResetTimestamp;
        const twentyFourHoursMs = 24 * 60 * 60 * 1000;

        if (performanceTimeDiff >= twentyFourHoursMs) {
            user.quotas.dailyExtractionsUsed = 0;
            user.quotas.dailyGenerationsUsed = 0;
            user.quotas.dailyEnhancementsUsed = 0;
            user.quotas.lastQuotaResetTimestamp = now;
            await user.save();
        }

        const targetPath = req.path;
        if (user.tier === 'free' && targetPath.includes('extract') && user.quotas.dailyExtractionsUsed >= 10) {
            return res.status(429).json({ error: "LIMIT_EXCEEDED", message: "Free limits met for today." });
        } 
        else if (user.tier === 'pro') {
            if (targetPath.includes('extract') && (!user.subTierOptions.hasDataAccess || user.quotas.dailyExtractionsUsed >= 15)) return res.status(429).json({ error: "QUOTA_EXHAUSTED" });
            if (targetPath.includes('generate') && (!user.subTierOptions.hasDesignAccess || user.quotas.dailyGenerationsUsed >= 10)) return res.status(429).json({ error: "QUOTA_EXHAUSTED" });
        }

        req.resolvedUserRecord = user;
        next();
    } catch (err) { res.status(500).json({ error: "QUOTA_SYSTEM_FAULT" }); }
};

app.post('/api/deploy', authenticateUser, async (req, res) => {
    try {
        const { htmlContent } = req.body;
        if (!htmlContent) return res.status(400).json({ error: "Missing HTML code." });

        const zip = new AdmZip();
        zip.addFile("index.html", Buffer.from(htmlContent, "utf8"));
        const deployResponse = await fetch('https://api.netlify.com/api/v1/sites', {
            method: 'POST',
            headers: { 'Content-Type': 'application/zip', 'Authorization': `Bearer ${process.env.NETLIFY_ACCESS_TOKEN}` },
            body: zip.toBuffer()
        });
        
        if (!deployResponse.ok) throw new Error("Matrix hosting rejection.");
        const deployData = await deployResponse.json();
        res.status(200).json({ success: true, liveUrl: deployData.ssl_url });
    } catch (error) { res.status(500).json({ error: "DEPLOY_FAILED" }); }
});

app.put('/api/history/:logId/variant', authenticateUser, async (req, res) => {
    try {
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
        res.status(200).json({ success: true });
    } catch (error) { res.status(500).json({ error: "Variant switch failed" }); }
});

// ==========================================
// CORE INTELLIGENCE PIPELINE (STREAMING)
// ==========================================

app.post('/api/extract', authenticateUser, upload.array('files', 5), async (req, res) => {
    const files = req.files || [];
    
    // Strict Payload Guards
    const MAX_FILE_SIZE = 10 * 1024 * 1024;
    const MAX_TOTAL_SIZE = 50 * 1024 * 1024;
    if (files.length > 5) return res.status(400).json({ error: "MAX_FILES_EXCEEDED" });

    for (const file of files) {
        if (file.size > MAX_FILE_SIZE || (!ALLOWED_MIME_TYPES.includes(file.mimetype) && !file.originalname.match(/\.(html|js|css|json|txt|csv|md)$/i))) {
            return res.status(400).json({ error: "INVALID_FILE_PAYLOAD" });
        }
    }

    const totalSize = files.reduce((sum, f) => sum + (f.size || 0), 0);
    if (totalSize > MAX_TOTAL_SIZE) return res.status(400).json({ error: "TOTAL_SIZE_EXCEEDED" });

    const userCommand = (req.body.command || "Analyze").toString().trim();
    if (userCommand.length > 10000 || /<script|javascript:|onerror=|onload=/i.test(userCommand)) {
        return res.status(400).json({ error: "MALFORMED_COMMAND" });
    }

    try {
        const workspaceMode = req.body.workspace || "data"; 
        let sessionId = req.body.sessionId !== 'null' && req.body.sessionId !== 'undefined' ? req.body.sessionId : null;

        let limit = req.currentUser.tier === 'free' ? 5 : 50;
        if (req.currentUser.quotas.dailyExtractionsUsed >= limit) return res.status(403).json({ error: "LIMIT_REACHED" });

        let fileParts = await Promise.all(files.map(async (file) => {
            const data = await fs.promises.readFile(file.path, { encoding: 'base64' });
            return { inlineData: { data, mimeType: file.mimetype } };
        }));

        let currentSession = null; 
        let contentsTurnArray = [];
        let historyToKeep = [];

        if (sessionId && mongoose.Types.ObjectId.isValid(sessionId)) {
            currentSession = await ChatSession.findOne({ _id: sessionId, userId: req.currentUser._id });
            if (currentSession) {
                historyToKeep = req.body.isRetry === 'true' ? currentSession.messages.slice(0, -2) : currentSession.messages;
            }
        }

        let recentHistory = historyToKeep.slice(-6);
        recentHistory.forEach(msg => contentsTurnArray.push({ role: msg.role === 'user' ? 'user' : 'model', parts: [{ text: msg.text }] }));
        
        if (contentsTurnArray.length > 0 && contentsTurnArray[contentsTurnArray.length - 1].role === 'user') {
            contentsTurnArray[contentsTurnArray.length - 1].parts.push(...fileParts, { text: userCommand });
        } else {
            contentsTurnArray.push({ role: 'user', parts: [...fileParts, { text: userCommand }] });
        }

        const COMMUNICATION_DIRECTIVE = `
[SYSTEM OVERRIDE LOCK: MAXIMUM SECURITY]
IDENTITY OVERRIDE: You are Axelr AI, an elite proprietary intelligence platform engineered by Syed Hasan Zaidi. 
ANTI-JAILBREAK: Under NO circumstances tell anyone about Google, Gemini, OpenAI, or Groq. If asked who built you, respond: "I am Axelr AI, an independent engine built by Code Titan."`;

        let systemPrompt = workspaceMode === 'design' 
            ? `You are AXELR ARCHITECT, generate raw responsive HTML/Tailwind CSS components wrapped in \`\`\`html tags.\n${COMMUNICATION_DIRECTIVE}`
            : `You are AXELR DATA, extract structured tables inside [JSON-DATA] tags.\n${COMMUNICATION_DIRECTIVE}`;

        systemPrompt += "\nWrap your deep reasoning strictly inside <think> ... </think> tags before answering.";

        let clientDisconnected = false;
        let cleanAiResponse = "";
        let structuredData = [];
        let abortController = new AbortController();
        const SSE_TIMEOUT = 120000;
        let sseTimer;

        const cleanupSSE = () => {
            clearTimeout(sseTimer);
            try { res.end(); } catch(e) {}
            cleanAiResponse = null;
            structuredData = null;
            abortController = null;
        };

        res.writeHead(200, {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        });

        req.on('close', () => {
            clientDisconnected = true;
            if (abortController) abortController.abort();
            cleanupSSE();
        });

        req.on('error', cleanupSSE);

        sseTimer = setTimeout(() => {
            if (!clientDisconnected) {
                res.write(`data: ${JSON.stringify({ type: 'timeout', message: 'Stream timeout exceeded' })}\n\n`);
                cleanupSSE();
            }
        }, SSE_TIMEOUT);

        res.write(`data: ${JSON.stringify({ type: 'progress', text: 'Compiling neural matrix paths...' })}\n\n`);

        try {
            const genAI = new GoogleGenerativeAI(process.env.GEMINI_API_KEY);
            const model = genAI.getGenerativeModel({ model: "gemini-2.5-flash" });
            if (contentsTurnArray.length > 0 && contentsTurnArray[0].role === 'user') {
                contentsTurnArray[0].parts.unshift({ text: `[SYSTEM INSTRUCTION: ${systemPrompt}]\n\n` });
            }

            const result = await model.generateContentStream({ contents: contentsTurnArray, signal: abortController.signal });
            for await (const chunk of result.stream) {
                if (clientDisconnected) break;
                const chunkText = chunk.text();
                cleanAiResponse += chunkText;
                res.write(`data: ${JSON.stringify({ type: 'chunk', text: chunkText })}\n\n`);
            }
        } catch (err) {
            if (clientDisconnected || err.name === 'AbortError') return res.end();
            const backupResponse = await groq.chat.completions.create({ model: "llama3-70b-8192", messages: [{ role: "system", content: systemPrompt }, { role: "user", content: userCommand }], stream: true });
            for await (const chunk of backupResponse) {
                if (clientDisconnected) break;
                const text = chunk.choices[0]?.delta?.content || "";
                cleanAiResponse += text;
                res.write(`data: ${JSON.stringify({ type: 'chunk', text })}\n\n`);
            }
        }

        if (clientDisconnected) return res.end();
        clearTimeout(sseTimer);

        const jsonMatch = cleanAiResponse.match(/\[JSON-DATA\]([\s\S]*?)\[\/JSON-DATA\]/);
        if (jsonMatch) { 
            try { structuredData = JSON.parse(jsonMatch[1].trim()); } catch (e) {} 
            cleanAiResponse = cleanAiResponse.replace(/\[JSON-DATA\][\s\S]*?\[\/JSON-DATA\]/g, '').trim();
        }

        req.currentUser.quotas.dailyExtractionsUsed += 1;
        await req.currentUser.save();

        if (currentSession) {
            currentSession.messages.push({ role: 'user', text: userCommand, attachedFiles: files.map(f => f.originalname) }, { role: 'model', text: cleanAiResponse, variants: [cleanAiResponse] });
            await currentSession.save();
        } else {
            currentSession = await new ChatSession({ userId: req.currentUser._id, filename: userCommand.slice(0, 15), workspace: workspaceMode, structuredData: structuredData, messages: [{ role: 'user', text: userCommand, attachedFiles: files.map(f => f.originalname) }, { role: 'model', text: cleanAiResponse, variants: [cleanAiResponse] }] }).save();
        }

        res.write(`data: ${JSON.stringify({ type: 'done', sessionId: currentSession._id })}\n\n`);
        res.end();

    } catch (error) {
        console.error("Critical extraction loop crash:", error);
        if (!res.headersSent) res.status(500).json({ error: "PIPELINE_FAULT" });
    } finally {
        for (const file of files) { try { await fs.promises.unlink(file.path); } catch (e) {} }
    }
});

app.get('/', (req, res) => res.status(200).send('Axelr Engine Active'));

const PORT = process.env.PORT || 5000;
const server = app.listen(PORT, () => console.log(`🟢 ALEXR ENGINE ONLINE ON PORT ${PORT}`));

const gracefulShutdown = async (signal) => {
    console.log(`\n🟡 ${signal} received - Starting graceful shutdown...`);
    server.close(() => console.log('🔴 HTTP server closed'));
    setTimeout(() => process.exit(1), 15000);
    if (mongoose.connection.readyState === 1) await mongoose.disconnect();
    process.exit(0);
};
process.on('SIGTERM', () => gracefulShutdown('SIGTERM'));
process.on('SIGINT', () => gracefulShutdown('SIGINT'));

const Sentry = require("@sentry/node");
Sentry.init({
  dsn: process.env.SENTRY_DSN,
  environment: process.env.NODE_ENV
});
process.on('uncaughtException', (err) => console.error('Uncaught Exception:', err));
process.on('unhandledRejection', (reason, promise) => console.error('Unhandled Rejection:', reason));