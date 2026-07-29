// ==========================================
// AXELR AI - PRODUCTION SERVER v4.3.4
// ==========================================
// Required environment variables:
// MONGO_URI, GOOGLE_CLIENT_ID, ORCHESTRATOR_URL
// Optional: STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
//           SMTP_USER, SMTP_PASS, ADMIN_EMAIL
// ==========================================

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
const crypto = require('crypto');
const nodemailer = require('nodemailer');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 5000;
const logger = console;

// ==========================================
// ENVIRONMENT VALIDATION
// ==========================================
const REQUIRED_ENV = ['MONGO_URI', 'GOOGLE_CLIENT_ID', 'ORCHESTRATOR_URL'];
const missing = REQUIRED_ENV.filter(key => !process.env[key]);
if (missing.length) {
    logger.error(`❌ Missing required env vars: ${missing.join(', ')}`);
    process.exit(1);
}

let ORCHESTRATOR_URL = process.env.ORCHESTRATOR_URL.replace(/\/+$/, '');
if (!ORCHESTRATOR_URL.endsWith('/api/route')) {
    ORCHESTRATOR_URL = ORCHESTRATOR_URL + '/api/route';
}
logger.info(`🔗 Orchestrator URL: ${ORCHESTRATOR_URL}`);

const GOOGLE_CLIENT_ID = process.env.GOOGLE_CLIENT_ID;
const ADMIN_EMAIL = process.env.ADMIN_EMAIL || 'shanh1346@gmail.com';

// ==========================================
// EMAIL SYSTEM
// ==========================================
let transporter = null;
try {
    if (process.env.SMTP_USER && process.env.SMTP_PASS) {
        transporter = nodemailer.createTransport({
            host: process.env.SMTP_HOST || 'smtp.gmail.com',
            port: parseInt(process.env.SMTP_PORT) || 587,
            secure: process.env.SMTP_SECURE === 'true',
            auth: {
                user: process.env.SMTP_USER,
                pass: process.env.SMTP_PASS
            }
        });
        logger.info('✅ Email transporter initialized');
    } else {
        logger.warn('⚠️ SMTP not configured - email features disabled');
    }
} catch (e) {
    logger.warn('⚠️ Email unavailable - email features disabled');
}

// ==========================================
// STRIPE
// ==========================================
let stripe = null;
try {
    if (process.env.STRIPE_SECRET_KEY && process.env.STRIPE_SECRET_KEY !== '' &&
        !process.env.STRIPE_SECRET_KEY.includes('test_secret')) {
        stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
        logger.info('✅ Stripe initialized');
    } else {
        logger.warn('⚠️ STRIPE_SECRET_KEY not set or invalid - payment features disabled');
    }
} catch (e) {
    logger.warn('⚠️ Stripe unavailable - payment features disabled');
}

// ==========================================
// MIDDLEWARE
// ==========================================
app.set('trust proxy', 1);

const allowedOrigins = [
    'https://axelr.in',
    'https://www.axelr.in',
    'https://axelr-frontend.pages.dev',
    'http://localhost:3000',
    'http://localhost:5000',
    'http://localhost:5001',
    process.env.CLIENT_APP_URL
].filter(Boolean);

app.use(cors({
    origin: (origin, cb) => {
        if (!origin || allowedOrigins.includes(origin) || process.env.NODE_ENV === 'development') {
            cb(null, true);
        } else {
            logger.warn(`CORS blocked: ${origin}`);
            cb(new Error('CORS blocked'), false);
        }
    },
    methods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
    credentials: true,
    maxAge: 86400
}));

app.use(compression());
app.use(express.json({ limit: '10mb' }));
app.use(express.urlencoded({ extended: true, limit: '10mb' }));

// ==========================================
// HELMET - Secure CSP
// ==========================================
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
            scriptSrc: ["'self'", (req, res) => `'nonce-${res.locals.nonce}'`, "https://accounts.google.com", "https://cdn.jsdelivr.net", "https://cdnjs.cloudflare.com", "https://www.googletagmanager.com", "https://js.stripe.com"],
            frameSrc: ["'self'", "https://accounts.google.com", "https://js.stripe.com"],
            connectSrc: ["'self'", "https://api.netlify.com", "https://api.vercel.com", "https://generativelanguage.googleapis.com", "https://openrouter.ai"],
            imgSrc: ["'self'", "data:", "https://*.googleusercontent.com", "https://*.googleapis.com"],
            styleSrc: ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
            fontSrc: ["'self'", "https://fonts.gstatic.com"],
            objectSrc: ["'none'"],
            baseUri: ["'self'"],
            formAction: ["'self'"],
        },
    },
    hsts: { maxAge: 31536000, includeSubDomains: true, preload: true }
}));

// ==========================================
// RATE LIMITING
// ==========================================
const globalLimiter = rateLimit({
    windowMs: 15 * 60 * 1000,
    max: 200,
    message: { success: false, code: 'RATE_LIMIT', message: "Too many requests. Please slow down." },
});
app.use('/api/', globalLimiter);

const deployLimiter = rateLimit({
    windowMs: 60 * 60 * 1000,
    max: 20,
    message: { success: false, code: 'DEPLOY_LIMIT', message: "Deployment limit reached. Try again in an hour." },
});
app.use('/api/deploy', deployLimiter);

// ==========================================
// DATABASE SCHEMAS
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
    stripeSubscriptionId: { type: String, sparse: true },
    subTierOptions: {
        hasDataAccess: { type: Boolean, default: false },
        hasDesignAccess: { type: Boolean, default: false }
    },
    quotas: {
        dailyExtractionsUsed: { type: Number, default: 0 },
        dailyGenerationsUsed: { type: Number, default: 0 },
        dailyEnhancementsUsed: { type: Number, default: 0 },
        monthlyEnhancementsLimit: { type: Number, default: 3 },
        lastQuotaReset: { type: Date, default: Date.now }
    },
    tokenUsage: {
        totalPromptTokens: { type: Number, default: 0 },
        totalCompletionTokens: { type: Number, default: 0 },
        dailyPromptTokens: { type: Number, default: 0 },
        dailyCompletionTokens: { type: Number, default: 0 },
        lastTokenReset: { type: Date, default: Date.now },
    },
    isAdmin: { type: Boolean, default: false },
    dailyGroqQuota: { type: Number, default: 0 },
    dailyOpenRouterQuota: { type: Number, default: 0 },
    lastAiQuotaReset: { type: Date, default: Date.now },
}, { timestamps: true });

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
        activeVariant: { type: Number, default: 0 },
        createdAt: { type: Date, default: Date.now }
    }],
    structuredData: { type: Array, default: [] },
    createdAt: { type: Date, default: Date.now },
    trashedAt: { type: Date }
}, { timestamps: true });

const BugReportSchema = new mongoose.Schema({
    userId: { type: mongoose.Schema.Types.ObjectId, ref: 'User', required: true },
    type: { type: String, enum: ['help', 'feedback'], required: true },
    description: { type: String, required: true },
    createdAt: { type: Date, default: Date.now }
});

const User = mongoose.model('User', UserSchema);
const ChatSession = mongoose.model('ChatSession', ChatSessionSchema);
const BugReport = mongoose.model('BugReport', BugReportSchema);

// ==========================================
// DATABASE CONNECTION with retry
// ==========================================
async function connectDB(retries = 5) {
    for (let i = 0; i < retries; i++) {
        try {
            await mongoose.connect(process.env.MONGO_URI, {
                maxPoolSize: 10,
                serverSelectionTimeoutMS: 5000,
                socketTimeoutMS: 45000,
                family: 4
            });
            logger.info('🗄️ DB CONNECTED');
            return;
        } catch (err) {
            logger.error(`DB connection attempt ${i + 1}/${retries} failed:`, err.message);
            await new Promise(r => setTimeout(r, 2000));
        }
    }
    logger.error('💥 CRITICAL: Failed to connect to database after all retries');
    process.exit(1);
}
connectDB();

// ==========================================
// AUTHENTICATION
// ==========================================
const googleClient = new OAuth2Client(GOOGLE_CLIENT_ID);

const authenticateUser = async (req, res, next) => {
    try {
        const authHeader = req.headers.authorization;
        if (!authHeader?.startsWith('Bearer ')) {
            return res.status(401).json({ success: false, code: 'AUTH_REQUIRED', message: 'Authentication required.' });
        }
        const token = authHeader.split(' ')[1];
        const ticket = await googleClient.verifyIdToken({ idToken: token, audience: GOOGLE_CLIENT_ID });
        const payload = ticket.getPayload();

        let user = await User.findOne({ googleId: payload.sub });
        const isAdmin = payload.email === ADMIN_EMAIL;

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
                    lastQuotaReset: new Date()
                },
                tokenUsage: {
                    totalPromptTokens: 0,
                    totalCompletionTokens: 0,
                    dailyPromptTokens: 0,
                    dailyCompletionTokens: 0,
                    lastTokenReset: new Date()
                },
                isAdmin,
                dailyGroqQuota: 0,
                dailyOpenRouterQuota: 0,
                lastAiQuotaReset: new Date(),
            });
            logger.info(`🆕 New user created: ${payload.email}`);
        } else {
            if (user.isAdmin !== isAdmin) {
                user.isAdmin = isAdmin;
                await user.save();
                logger.info(`🔑 Admin status updated for ${payload.email}: ${isAdmin}`);
            }
            // Reset daily quotas
            const now = new Date();
            const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
            const lastReset = user.quotas.lastQuotaReset ? new Date(user.quotas.lastQuotaReset) : new Date(0);
            const lastResetDay = new Date(lastReset.getFullYear(), lastReset.getMonth(), lastReset.getDate());

            if (today > lastResetDay) {
                user.dailyUsage = 0;
                user.dailyUiUxUsage = 0;
                user.quotas.dailyExtractionsUsed = 0;
                user.quotas.dailyGenerationsUsed = 0;
                user.quotas.dailyEnhancementsUsed = 0;
                user.quotas.lastQuotaReset = new Date();
                user.tokenUsage.dailyPromptTokens = 0;
                user.tokenUsage.dailyCompletionTokens = 0;
                user.tokenUsage.lastTokenReset = new Date();
                user.dailyGroqQuota = 0;
                user.dailyOpenRouterQuota = 0;
                user.lastAiQuotaReset = new Date();
                await user.save();
            }
        }
        req.currentUser = user;
        next();
    } catch (error) {
        logger.error('[AUTH_FAIL]', error.message);
        res.status(401).json({ success: false, code: 'SESSION_EXPIRED', message: 'Invalid or expired session.' });
    }
};

// ==========================================
// STRIPE WEBHOOK
// ==========================================
app.post('/api/webhooks/stripe', express.raw({ type: 'application/json', limit: '10kb' }), async (req, res) => {
    try {
        if (!stripe) {
            logger.warn('Stripe not initialized, webhook ignored');
            return res.json({ received: true, note: 'Stripe disabled' });
        }

        const sig = req.headers['stripe-signature'];
        let event;

        if (process.env.STRIPE_WEBHOOK_SECRET && process.env.STRIPE_WEBHOOK_SECRET !== '') {
            try {
                event = stripe.webhooks.constructEvent(req.body, sig, process.env.STRIPE_WEBHOOK_SECRET);
            } catch (err) {
                logger.warn('Webhook signature verification failed:', err.message);
                event = JSON.parse(req.body.toString());
            }
        } else {
            event = JSON.parse(req.body.toString());
        }

        logger.info('Webhook received:', event.type);

        if (event.type === 'checkout.session.completed') {
            const session = event.data.object;
            const user = await User.findOne({ googleId: session.client_reference_id });
            if (user) {
                const tier = session.metadata.tier || 'pro';
                user.tier = tier;
                user.stripeCustomerId = session.customer;
                if (session.subscription) user.stripeSubscriptionId = session.subscription;
                user.subTierOptions = {
                    hasDataAccess: (session.metadata.subTier === 'full' || session.metadata.subTier === 'data'),
                    hasDesignAccess: (session.metadata.subTier === 'full' || session.metadata.subTier === 'design')
                };
                await user.save();
                logger.info(`✅ User ${user.email} upgraded to ${tier}`);

                if (transporter) {
                    try {
                        await transporter.sendMail({
                            from: process.env.SMTP_USER,
                            to: user.email,
                            subject: '🎉 Axelr AI - Subscription Upgrade Confirmed',
                            html: `
                                <h2>Welcome to ${tier.toUpperCase()} Tier!</h2>
                                <p>Your Axelr AI workspace has been successfully upgraded.</p>
                                <p><strong>Plan:</strong> ${tier}</p>
                                <p><strong>Features:</strong></p>
                                <ul>
                                    <li>Data Access: ${user.subTierOptions.hasDataAccess ? '✅' : '❌'}</li>
                                    <li>Design Access: ${user.subTierOptions.hasDesignAccess ? '✅' : '❌'}</li>
                                </ul>
                                <p>Thank you for choosing Axelr AI!</p>
                            `
                        });
                    } catch (emailErr) {
                        logger.warn('Upgrade email failed:', emailErr.message);
                    }
                }
            }
        } else if (event.type === 'customer.subscription.deleted') {
            const subscription = event.data.object;
            const user = await User.findOneAndUpdate(
                { stripeSubscriptionId: subscription.id },
                { tier: 'free', subTierOptions: { hasDataAccess: false, hasDesignAccess: false } }
            );
            if (user) {
                logger.info(`🗑️ Subscription cancelled for ${user.email}`);
                if (transporter) {
                    try {
                        await transporter.sendMail({
                            from: process.env.SMTP_USER,
                            to: user.email,
                            subject: 'Axelr AI - Subscription Cancelled',
                            html: `
                                <h2>Subscription Cancelled</h2>
                                <p>Your Axelr AI subscription has been cancelled.</p>
                                <p>You are now on the Free tier.</p>
                            `
                        });
                    } catch (emailErr) {
                        logger.warn('Cancellation email failed:', emailErr.message);
                    }
                }
            }
        }

        res.json({ received: true });
    } catch (error) {
        logger.error('Webhook error:', error.message);
        res.status(400).json({ received: false, error: error.message });
    }
});

// ==========================================
// ROUTES
// ==========================================
app.get('/', (req, res) => res.send('Axelr API Online'));

app.get('/api/health', async (req, res) => {
    const dbStatus = mongoose.connection.readyState === 1 ? 'connected' : 'disconnected';
    const orchestratorStatus = await testOrchestratorHealth();
    res.json({
        status: dbStatus === 'connected' ? 'operational' : 'degraded',
        timestamp: new Date().toISOString(),
        db: dbStatus,
        orchestrator: orchestratorStatus,
        stripe: stripe !== null,
        email: transporter !== null,
        uptime: process.uptime()
    });
});

async function testOrchestratorHealth() {
    try {
        const response = await fetch(`${ORCHESTRATOR_URL}`, { signal: AbortSignal.timeout(3000) });
        return response.ok ? 'connected' : 'unhealthy';
    } catch {
        return 'unreachable';
    }
}

// ==========================================
// ADMIN METRICS - EXTENDED with daily breakdowns
// ==========================================
app.get('/api/admin/metrics', authenticateUser, async (req, res) => {
    try {
        // STRICT ADMIN CHECK
        if (!req.currentUser.isAdmin || req.currentUser.email !== ADMIN_EMAIL) {
            return res.status(403).json({
                success: false,
                code: 'UNAUTHORIZED',
                message: 'Admin access restricted to authorized personnel only.'
            });
        }

        // Build today's date range
        const now = new Date();
        const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate());

        // Aggregations
        const [
            totalUsers,
            proUsers,
            businessUsers,
            totalChats,
            totalUsageAgg,
            totalTokenAgg,
            dailyUsageAgg,
            dailyGroqAgg,
            dailyOpenRouterAgg,
            dailyTokenAgg
        ] = await Promise.all([
            User.countDocuments(),
            User.countDocuments({ tier: 'pro' }),
            User.countDocuments({ tier: 'business' }),
            ChatSession.countDocuments(),
            // Overall metrics
            User.aggregate([
                { $group: { _id: null, totalQueries: { $sum: "$dailyUsage" }, totalBytes: { $sum: "$storageBytesUsed" } } }
            ]),
            User.aggregate([
                { $group: { _id: null, totalPrompt: { $sum: "$tokenUsage.totalPromptTokens" }, totalCompletion: { $sum: "$tokenUsage.totalCompletionTokens" } } }
            ]),
            // Today's metrics
            User.aggregate([
                { $match: { lastUsageDate: { $gte: startOfDay } } },
                { $group: { _id: null, dailyQueries: { $sum: "$dailyUsage" } } }
            ]),
            User.aggregate([
                { $match: { lastAiQuotaReset: { $gte: startOfDay } } },
                { $group: { _id: null, dailyGroq: { $sum: "$dailyGroqQuota" }, dailyOpenRouter: { $sum: "$dailyOpenRouterQuota" } } }
            ]),
            User.aggregate([
                { $match: { lastAiQuotaReset: { $gte: startOfDay } } },
                { $group: { _id: null, dailyPrompt: { $sum: "$tokenUsage.dailyPromptTokens" }, dailyCompletion: { $sum: "$tokenUsage.dailyCompletionTokens" } } }
            ])
        ]);

        const overall = totalUsageAgg[0] || { totalQueries: 0, totalBytes: 0 };
        const tokens = totalTokenAgg[0] || { totalPrompt: 0, totalCompletion: 0 };
        const daily = dailyUsageAgg[0] || { dailyQueries: 0 };
        const dailyAI = dailyGroqAgg[0] || { dailyGroq: 0, dailyOpenRouter: 0 };
        const dailyTokens = dailyTokenAgg[0] || { dailyPrompt: 0, dailyCompletion: 0 };

        const totalTokens = tokens.totalPrompt + tokens.totalCompletion;
        const freeLimit = process.env.FREE_TIER_TOKEN_LIMIT || 1000000;

        // Also get daily Groq/OpenRouter from lastAiQuotaReset field (we already have dailyAI)
        // But we also need total Groq/OpenRouter across all time, which we already have in aiQuotaAgg
        // We'll add that separately.

        // Actually we need overall Groq/OpenRouter totals, not just daily.
        const overallAI = await User.aggregate([
            { $group: { _id: null, totalGroq: { $sum: "$dailyGroqQuota" }, totalOpenRouter: { $sum: "$dailyOpenRouterQuota" } } }
        ]);
        const overallAIStats = overallAI[0] || { totalGroq: 0, totalOpenRouter: 0 };

        const recentUsers = await User.find()
            .sort({ createdAt: -1 })
            .limit(10)
            .select('email displayName tier createdAt');

        res.json({
            success: true,
            totalUsers,
            proUsers,
            businessUsers,
            totalChats,
            metrics: {
                totalQueries: overall.totalQueries,
                totalBytes: overall.totalBytes,
                dailyQueries: daily.dailyQueries,
            },
            tokenUsage: {
                prompt: tokens.totalPrompt,
                completion: tokens.totalCompletion,
                total: totalTokens,
                remaining: Math.max(0, freeLimit - totalTokens),
                limit: freeLimit,
                dailyPrompt: dailyTokens.dailyPrompt,
                dailyCompletion: dailyTokens.dailyCompletion,
            },
            aiQuota: {
                groq: overallAIStats.totalGroq,
                openRouter: overallAIStats.totalOpenRouter,
                dailyGroq: dailyAI.dailyGroq,
                dailyOpenRouter: dailyAI.dailyOpenRouter,
            },
            recentUsers,
            timestamp: new Date().toISOString()
        });
    } catch (err) {
        logger.error('Admin metrics error:', err.message);
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to fetch metrics.' });
    }
});

// ==========================================
// STRIPE CHECKOUT
// ==========================================
app.post('/api/billing/checkout', authenticateUser, async (req, res) => {
    try {
        if (!stripe) {
            return res.status(503).json({
                success: false,
                code: 'PAYMENT_UNAVAILABLE',
                message: 'Payment service is currently unavailable. Please try again later.'
            });
        }

        const { tier = 'pro', subTier = 'full' } = req.body;

        const pricing = {
            pro: {
                full: { price: 1500, name: 'Pro Full Stack', features: '20 Data + 15 UI + 7 Enhancements' },
                data: { price: 800, name: 'Pro Data', features: '19 Data + 0 UI + 5 Enhancements' },
                design: { price: 900, name: 'Pro Design', features: '0 Data + 13 UI + 5 Enhancements' }
            },
            business: {
                full: { price: 2900, name: 'Business Full', features: '30 Data + 25 UI + 15 Enhancements' },
                data: { price: 1600, name: 'Business Data', features: '28 Data + 0 UI + 10 Enhancements' },
                design: { price: 1600, name: 'Business Design', features: '0 Data + 20 UI + 10 Enhancements' }
            }
        };

        const plan = pricing[tier]?.[subTier];
        if (!plan) {
            return res.status(400).json({
                success: false,
                code: 'INVALID_PLAN',
                message: 'Invalid plan selection.'
            });
        }

        const origin = req.headers.origin || 'https://axelr.in';

        const session = await stripe.checkout.sessions.create({
            payment_method_types: ['card'],
            mode: 'subscription',
            client_reference_id: req.currentUser.googleId,
            customer_email: req.currentUser.email,
            metadata: {
                tier,
                subTier,
                userId: req.currentUser._id.toString()
            },
            line_items: [{
                price_data: {
                    currency: 'usd',
                    product_data: {
                        name: plan.name,
                        description: plan.features
                    },
                    unit_amount: plan.price,
                    recurring: { interval: 'month' }
                },
                quantity: 1
            }],
            success_url: `${origin}/?billing=success&session_id={CHECKOUT_SESSION_ID}`,
            cancel_url: `${origin}/?billing=cancelled`,
            allow_promotion_codes: true,
        });

        if (!session.url) {
            throw new Error('No checkout URL returned');
        }

        res.json({ success: true, url: session.url });
    } catch (err) {
        logger.error('Checkout error:', err.message);
        res.status(500).json({
            success: false,
            code: 'CHECKOUT_FAILED',
            message: err.message || 'Failed to create checkout session. Please try again.'
        });
    }
});

// ==========================================
// USER PROFILE
// ==========================================
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
        email: user.email,
        stripeCustomerId: user.stripeCustomerId,
        stripeSubscriptionId: user.stripeSubscriptionId,
        dailyGroqQuota: user.dailyGroqQuota,
        dailyOpenRouterQuota: user.dailyOpenRouterQuota,
    });
});

app.put('/api/user/instructions', authenticateUser, async (req, res) => {
    try {
        const instructions = (req.body.instructions || '').slice(0, 5000);
        req.currentUser.customInstructions = instructions;
        await req.currentUser.save();
        res.json({ success: true });
    } catch (err) {
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to save instructions.' });
    }
});

app.delete('/api/user/delete', authenticateUser, async (req, res) => {
    try {
        await ChatSession.deleteMany({ userId: req.currentUser._id });
        await BugReport.deleteMany({ userId: req.currentUser._id });
        await User.deleteOne({ _id: req.currentUser._id });
        res.json({ success: true });
    } catch (err) {
        logger.error('Account deletion error:', err.message);
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to delete account.' });
    }
});

app.delete('/api/history/delete-all', authenticateUser, async (req, res) => {
    try {
        await ChatSession.deleteMany({ userId: req.currentUser._id });
        res.json({ success: true });
    } catch (err) {
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to delete chats.' });
    }
});

// ==========================================
// HISTORY ROUTES
// ==========================================
app.put('/api/history/:id', authenticateUser, async (req, res) => {
    try {
        const { action, payload } = req.body;
        const log = await ChatSession.findOne({ _id: req.params.id, userId: req.currentUser._id });
        if (!log) return res.status(404).json({ success: false, code: 'NOT_FOUND' });

        if (action === 'rename' && payload) log.filename = payload.slice(0, 100);
        if (action === 'pin') log.isPinned = !log.isPinned;
        await log.save();
        res.json({ success: true });
    } catch (err) {
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to update chat.' });
    }
});

app.put('/api/history/:id/status', authenticateUser, async (req, res) => {
    try {
        const { status } = req.body;
        const update = { status };
        if (status === 'trashed') update.trashedAt = new Date();
        await ChatSession.findOneAndUpdate({ _id: req.params.id, userId: req.currentUser._id }, update);
        res.json({ success: true });
    } catch (err) {
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to update status.' });
    }
});

app.delete('/api/history/:id', authenticateUser, async (req, res) => {
    try {
        await ChatSession.deleteOne({ _id: req.params.id, userId: req.currentUser._id, status: 'trashed' });
        res.json({ success: true });
    } catch (err) {
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to delete chat.' });
    }
});

app.put('/api/history/:id/variant', authenticateUser, async (req, res) => {
    try {
        const { msgId, variantIndex } = req.body;
        if (!msgId || variantIndex === undefined) {
            return res.status(400).json({ success: false, code: 'INVALID_INPUT' });
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
    } catch (err) {
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to switch variant.' });
    }
});

app.get('/api/history', authenticateUser, async (req, res) => {
    try {
        const workspace = ['data', 'design', 'general'].includes(req.query.workspace) ? req.query.workspace : 'data';
        const status = req.query.status || 'active';
        const page = parseInt(req.query.page) || 1;
        const limit = parseInt(req.query.limit) || 20;
        const skip = (page - 1) * limit;

        const [logs, total] = await Promise.all([
            ChatSession.find({ userId: req.currentUser._id, status, workspace })
                .sort({ isPinned: -1, createdAt: -1 })
                .skip(skip)
                .limit(limit),
            ChatSession.countDocuments({ userId: req.currentUser._id, status, workspace })
        ]);

        res.json({
            success: true,
            logs,
            pagination: { page, limit, total, pages: Math.ceil(total / limit) }
        });
    } catch (err) {
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to fetch history.' });
    }
});

// ==========================================
// BUG REPORTS - with email
// ==========================================
app.post('/api/reports', authenticateUser, async (req, res) => {
    try {
        const report = await BugReport.create({
            userId: req.currentUser._id,
            type: req.body.type || 'feedback',
            description: (req.body.description || '').slice(0, 5000)
        });

        if (transporter) {
            try {
                const emailHtml = `
                    <h2>🔔 New ${report.type.toUpperCase()} Report</h2>
                    <p><strong>From:</strong> ${req.currentUser.displayName} (${req.currentUser.email})</p>
                    <p><strong>Type:</strong> ${report.type}</p>
                    <p><strong>Date:</strong> ${new Date().toLocaleString()}</p>
                    <p><strong>Description:</strong></p>
                    <p style="background:#f5f5f5;padding:15px;border-radius:8px;">${report.description}</p>
                    <hr>
                    <p><strong>User ID:</strong> ${req.currentUser._id}</p>
                    <p><strong>Tier:</strong> ${req.currentUser.tier}</p>
                `;

                await transporter.sendMail({
                    from: process.env.SMTP_USER,
                    to: ADMIN_EMAIL,
                    subject: `🔔 Axelr AI - New ${report.type.toUpperCase()} Report from ${req.currentUser.displayName}`,
                    html: emailHtml,
                    replyTo: req.currentUser.email
                });
                logger.info(`📧 Report email sent to admin for ${req.currentUser.email}`);
            } catch (emailErr) {
                logger.error('Report email failed:', emailErr.message);
            }
        } else {
            logger.warn('⚠️ Email not configured - report stored in DB only');
        }

        res.json({ success: true });
    } catch (err) {
        logger.error('Report error:', err.message);
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to submit report.' });
    }
});

// ==========================================
// PROMPT ENHANCER
// ==========================================
app.post('/api/enhance-prompt', authenticateUser, async (req, res) => {
    try {
        const { promptText } = req.body;
        if (!promptText) {
            return res.status(400).json({ success: false, code: 'INVALID_INPUT', message: 'No text provided.' });
        }

        const user = req.currentUser;
        const now = new Date();
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const lastReset = user.quotas.lastQuotaReset ? new Date(user.quotas.lastQuotaReset) : new Date(0);
        const lastResetDay = new Date(lastReset.getFullYear(), lastReset.getMonth(), lastReset.getDate());

        if (today > lastResetDay) {
            user.quotas.dailyEnhancementsUsed = 0;
            user.quotas.lastQuotaReset = new Date();
            await user.save();
        }

        let limit = 3;
        if (user.tier === 'pro') {
            limit = (user.subTierOptions.hasDataAccess && user.subTierOptions.hasDesignAccess) ? 7 : 5;
        } else if (user.tier === 'business') {
            limit = (user.subTierOptions.hasDataAccess && user.subTierOptions.hasDesignAccess) ? 15 : 10;
        }

        if (user.quotas.dailyEnhancementsUsed >= limit) {
            return res.status(403).json({
                success: false,
                code: 'LIMIT_REACHED',
                usage: user.quotas.dailyEnhancementsUsed,
                limit
            });
        }

        let enhanced = null;

        for (let attempt = 0; attempt < 2; attempt++) {
            try {
                const response = await fetch(ORCHESTRATOR_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        workspace: 'prompt',
                        prompt: promptText,
                        history: [],
                        files: [],
                        max_tokens: 2048,
                        temperature: 0.2,
                        tier: user.tier,
                    }),
                    signal: AbortSignal.timeout(15000),
                });

                if (!response.ok) {
                    throw new Error(`Orchestrator returned ${response.status}`);
                }

                const result = await response.json();
                if (result.success && result.text) {
                    enhanced = result.text;
                    break;
                }
                throw new Error(result.text || 'Orchestrator returned failure');
            } catch (err) {
                logger.warn(`Enhance attempt ${attempt + 1} failed:`, err.message);
                await new Promise(r => setTimeout(r, 1000));
            }
        }

        if (!enhanced) {
            enhanced = `You are AXELR AI - an elite executive assistant. Please provide a detailed response to: ${promptText}`;
        }

        user.quotas.dailyEnhancementsUsed += 1;
        user.dailyUsage += 1;
        await user.save();

        res.json({ success: true, enhanced });
    } catch (err) {
        logger.error('Enhance prompt error:', err.message);
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to enhance prompt.' });
    }
});

// ==========================================
// MULTER SETUP - WORKSPACE AWARE
// ==========================================
function getAllowedMimeTypes(workspace) {
    const dataTypes = [
        'text/csv', 'application/vnd.ms-excel', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'application/pdf', 'text/plain', 'application/json',
        'image/png', 'image/jpeg', 'image/webp'
    ];
    const designTypes = [
        'text/html', 'text/css', 'text/javascript', 'application/javascript', 'text/jsx',
        'text/tsx', 'text/typescript', 'application/typescript',
        'image/png', 'image/jpeg', 'image/webp', 'image/svg+xml',
        'text/plain', 'application/json'
    ];
    return workspace === 'design' ? designTypes : dataTypes;
}

function isAllowedFile(file, workspace) {
    const allowed = getAllowedMimeTypes(workspace);
    if (allowed.includes(file.mimetype)) return true;
    const ext = file.originalname.split('.').pop().toLowerCase();
    const allowedExts = workspace === 'design'
        ? ['html', 'css', 'js', 'jsx', 'ts', 'tsx', 'svg', 'json', 'txt']
        : ['csv', 'pdf', 'xlsx', 'xls', 'json', 'txt', 'png', 'jpg', 'jpeg', 'webp'];
    return allowedExts.includes(ext);
}

const storage = multer.diskStorage({
    destination: os.tmpdir(),
    filename: (req, file, cb) => cb(null, `${Date.now()}-${crypto.randomBytes(4).toString('hex')}-${file.originalname}`)
});

const upload = multer({
    storage,
    limits: { fileSize: 10 * 1024 * 1024, files: 5 },
    fileFilter: (req, file, cb) => {
        const workspace = req.body.workspace || 'data';
        if (isAllowedFile(file, workspace)) {
            cb(null, true);
        } else {
            cb(new Error(`File type not allowed in ${workspace} workspace`), false);
        }
    }
});

function estimateTokens(text) {
    return Math.ceil((text || '').length / 4);
}

function generateChatName(command, files) {
    const STOP_WORDS = new Set(['the','be','to','of','and','a','in','that','have','i','it','for','not','on','with','he','as','you','do','at','this','but','his','by','from','they','we','say','her','she','or','an','will','my','one','all','would','there','their','what','so','up','out','if','about','who','get','which','go','me','when','make','can','like','time','no','just','him','know','take','people','into','year','your','good','some','could','them','see','other','than','then','now','look','only','come','its','over','think','also','back','after','use','two','how','our','work','first','well','way','even','new','want','because','any','these','give','day','most','us']);

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

function cleanAssistantMessage(text) {
    if (!text) return '';
    return text.replace(/\|.*\|.*\n/g, '').replace(/\s+/g, ' ').trim();
}

// ==========================================
// EXTRACT - with workspace-aware file checks
// ==========================================
app.post('/api/extract', authenticateUser, upload.array('files', 5), async (req, res) => {
    const files = req.files || [];
    const userCommand = (req.body.command || "Analyze").slice(0, 10000);
    const workspaceMode = req.body.workspace === 'design' ? 'design' : 'data';
    const sessionId = (req.body.sessionId && mongoose.Types.ObjectId.isValid(req.body.sessionId)) ? req.body.sessionId : null;

    let cleanupFiles = async () => {
        for (const f of files) {
            try { await fs.unlink(f.path); } catch (_) {}
        }
    };

    try {
        if (files.length > 5) {
            await cleanupFiles();
            return res.status(400).json({ success: false, code: 'MAX_FILES_EXCEEDED', message: 'Too many files.' });
        }

        const totalSize = files.reduce((s, f) => s + f.size, 0);
        if (totalSize > 50 * 1024 * 1024) {
            await cleanupFiles();
            return res.status(400).json({ success: false, code: 'TOTAL_SIZE_EXCEEDED', message: 'Total upload size too large.' });
        }

        for (const f of files) {
            if (f.size > 10 * 1024 * 1024) {
                await cleanupFiles();
                return res.status(400).json({ success: false, code: 'FILE_TOO_LARGE', message: `File ${f.originalname} exceeds 10MB.` });
            }
        }

        const user = req.currentUser;

        // --- QUOTA CHECKS ---
        const isFree = user.tier === 'free';
        const isPro = user.tier === 'pro';
        const isBusiness = user.tier === 'business';
        const hasData = user.subTierOptions.hasDataAccess;
        const hasDesign = user.subTierOptions.hasDesignAccess;
        let subTierType = 'full';
        if (hasData && !hasDesign) subTierType = 'data';
        else if (!hasData && hasDesign) subTierType = 'design';

        let dataLimit = 5, uiLimit = 0;
        if (isPro) {
            if (subTierType === 'full') { dataLimit = 20; uiLimit = 15; }
            else if (subTierType === 'data') { dataLimit = 19; uiLimit = 0; }
            else if (subTierType === 'design') { dataLimit = 0; uiLimit = 13; }
        } else if (isBusiness) {
            if (subTierType === 'full') { dataLimit = 30; uiLimit = 25; }
            else if (subTierType === 'data') { dataLimit = 28; uiLimit = 0; }
            else if (subTierType === 'design') { dataLimit = 0; uiLimit = 20; }
        }

        const isDesign = workspaceMode === 'design';
        let used, limit;
        if (isFree) {
            used = user.dailyUsage;
            limit = dataLimit;
        } else {
            if (isDesign && !hasDesign) {
                await cleanupFiles();
                return res.status(403).json({ success: false, code: 'SUB_TIER_RESTRICTION', message: 'UI generation not included in your plan.' });
            }
            if (!isDesign && !hasData) {
                await cleanupFiles();
                return res.status(403).json({ success: false, code: 'SUB_TIER_RESTRICTION', message: 'Data extraction not included in your plan.' });
            }
            const quotaField = isDesign ? 'dailyGenerationsUsed' : 'dailyExtractionsUsed';
            used = user.quotas[quotaField];
            limit = isDesign ? uiLimit : dataLimit;
        }
        if (used < 0) used = 0;

        if (used >= limit) {
            await cleanupFiles();
            return res.status(403).json({ success: false, code: 'LIMIT_REACHED', usage: used, limit });
        }

        let byteLimit = 5 * 1024 * 1024;
        if (isPro) byteLimit = 20 * 1024 * 1024;
        else if (isBusiness) byteLimit = 50 * 1024 * 1024;

        if ((user.storageBytesUsed + totalSize) > byteLimit) {
            await cleanupFiles();
            return res.status(403).json({
                success: false,
                code: 'STORAGE_LIMIT_REACHED',
                message: `Storage quota exceeded. Maximum ${byteLimit / (1024*1024)}MB.`
            });
        }

        // --- Read files as base64 ---
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
        if (sessionId) {
            currentSession = await ChatSession.findOne({ _id: sessionId, userId: user._id });
            if (currentSession) {
                const isRetry = req.body.isRetry === 'true';
                history = currentSession.messages;
                if (isRetry && history.length > 0 && history[history.length - 1].role === 'model') {
                    history = history.slice(0, -2);
                }
            }
        }

        // --- Call orchestrator ---
        let aiResponse = '';
        let orchestratorSuccess = false;
        let providerInfo = '';
        let modelUsed = '';

        for (let attempt = 0; attempt < 2; attempt++) {
            try {
                const orchestratorPayload = {
                    workspace: workspaceMode,
                    prompt: userCommand,
                    history: history.slice(-4).map(msg => ({
                        role: msg.role === 'user' ? 'user' : 'assistant',
                        content: msg.role === 'model' ? cleanAssistantMessage(msg.text) : msg.text
                    })),
                    files: fileContents,
                    max_tokens: 2048,
                    temperature: 0.2,
                    tier: user.tier,
                };

                const response = await fetch(ORCHESTRATOR_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(orchestratorPayload),
                    signal: AbortSignal.timeout(60000),
                });

                if (!response.ok) {
                    throw new Error(`Orchestrator returned ${response.status}`);
                }

                const result = await response.json();
                if (result.success && result.text) {
                    aiResponse = result.text;
                    orchestratorSuccess = true;
                    providerInfo = result.provider || 'unknown';
                    modelUsed = result.model_used || 'unknown';
                    logger.info(`Orchestrator used ${result.provider} (${result.model_used}) in ${result.latency_ms}ms`);
                    break;
                }
                throw new Error(result.text || 'Orchestrator returned failure');
            } catch (err) {
                logger.warn(`Extract attempt ${attempt + 1} failed:`, err.message);
                await new Promise(r => setTimeout(r, 1000));
            }
        }

        if (!orchestratorSuccess) {
            aiResponse = "I am Axelr AI. I encountered a technical issue. Please try again later.";
            await cleanupFiles();
            return res.status(503).json({
                success: false,
                code: 'ORCHESTRATOR_FAILED',
                message: 'AI service temporarily unavailable. Please try again.'
            });
        }

        // --- ✅ SUCCESS: Now increment quotas ---
        const promptTextTokens = estimateTokens(userCommand);
        const fileTokens = files.reduce((sum, f) => sum + estimateTokens(f.originalname) + Math.ceil(f.size / 4), 0);
        const completionTokens = estimateTokens(aiResponse);

        const isGroq = providerInfo === 'groq' || providerInfo === 'groq-fallback';
        const isOpenRouter = providerInfo === 'openrouter' || providerInfo === 'openrouter-fallback';

        const updateFields = {
            $inc: {
                'tokenUsage.totalPromptTokens': promptTextTokens + fileTokens,
                'tokenUsage.totalCompletionTokens': completionTokens,
                'tokenUsage.dailyPromptTokens': promptTextTokens + fileTokens,
                'tokenUsage.dailyCompletionTokens': completionTokens,
                [isDesign ? 'quotas.dailyGenerationsUsed' : 'quotas.dailyExtractionsUsed']: 1,
                dailyUsage: 1,
                storageBytesUsed: totalSize,
            }
        };

        if (isGroq) {
            updateFields.$inc.dailyGroqQuota = 1;
        } else if (isOpenRouter) {
            updateFields.$inc.dailyOpenRouterQuota = 1;
        }

        await User.updateOne({ _id: user._id }, updateFields);

        // Extract structured data if any
        let structured = [];
        const jsonMatch = aiResponse.match(/\[JSON-DATA\]([\s\S]*?)\[\/JSON-DATA\]/);
        if (jsonMatch) {
            try { structured = JSON.parse(jsonMatch[1].trim()); } catch (e) { structured = []; }
            aiResponse = aiResponse.replace(/\[JSON-DATA\][\s\S]*?\[\/JSON-DATA\]/g, '').trim();
        }
        if (!aiResponse.trim()) aiResponse = "I am Axelr AI. How can I help you?";

        // --- Save session ---
        let sessionIdOut = null;
        let filenameOut = 'Export.csv';
        let sessionSaved = false;

        try {
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
                        { role: 'user', text: userCommand, attachedFiles: files.map(f => f.originalname) },
                        { role: 'model', text: aiResponse, variants: [aiResponse], activeVariant: 0, createdAt: new Date() }
                    );
                }
                currentSession.structuredData = structured;
                await currentSession.save();
                sessionSaved = true;
                sessionIdOut = currentSession._id;
                filenameOut = currentSession.filename;
            } else {
                const filename = generateChatName(userCommand, files);
                currentSession = await ChatSession.create({
                    userId: user._id,
                    filename,
                    workspace: workspaceMode,
                    structuredData: structured,
                    messages: [
                        { role: 'user', text: userCommand, attachedFiles: files.map(f => f.originalname) },
                        { role: 'model', text: aiResponse, variants: [aiResponse], activeVariant: 0, createdAt: new Date() }
                    ]
                });
                sessionSaved = true;
                sessionIdOut = currentSession._id;
                filenameOut = currentSession.filename;
            }
        } catch (saveErr) {
            logger.error('Session save error:', saveErr.message);
        }

        await cleanupFiles();

        res.json({
            success: true,
            text: aiResponse,
            sessionId: sessionSaved ? sessionIdOut : null,
            structuredData: structured,
            filename: sessionSaved ? `${filenameOut}.csv` : 'Export.csv',
            provider: providerInfo,
            model: modelUsed,
        });

    } catch (err) {
        logger.error('Extract error:', err.message);
        await cleanupFiles();
        res.status(500).json({ success: false, code: 'INTERNAL_ERROR', message: 'Failed to process request.' });
    }
});

// ==========================================
// DEPLOY
// ==========================================
const { JSDOM } = require('jsdom');
const createDOMPurify = require('dompurify');
const window = new JSDOM('').window;
const DOMPurify = createDOMPurify(window);

app.post('/api/deploy', authenticateUser, async (req, res) => {
    try {
        const { htmlContent } = req.body;
        if (!htmlContent) {
            return res.status(400).json({ success: false, message: 'Missing HTML content' });
        }

        if (!htmlContent.includes('<html') || !htmlContent.includes('</html>')) {
            return res.status(400).json({ success: false, message: 'Generated HTML is incomplete.' });
        }

        const sanitized = DOMPurify.sanitize(htmlContent, {
            ALLOWED_TAGS: [
                'html', 'head', 'body', 'div', 'span', 'p', 'a', 'img', 'button', 'input', 'form', 'table',
                'tr', 'td', 'th', 'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'u',
                'br', 'hr', 'section', 'article', 'header', 'footer', 'nav', 'main', 'aside', 'figure',
                'figcaption', 'mark', 'small', 'sub', 'sup', 'code', 'pre', 'blockquote', 'cite', 'label',
                'select', 'option', 'textarea', 'style', 'link', 'meta', 'title'
            ],
            ALLOWED_ATTR: [
                'href', 'src', 'alt', 'title', 'class', 'id', 'style', 'rel', 'type', 'media', 'name',
                'value', 'placeholder', 'for', 'width', 'height', 'colspan', 'rowspan'
            ],
        });

        // Try Vercel deployment
        const vercelToken = process.env.VERCEL_TOKEN;
        const vercelProjectId = process.env.VERCEL_PROJECT_ID;
        if (vercelToken && vercelProjectId) {
            try {
                const formData = new FormData();
                const blob = new Blob([sanitized], { type: 'text/html; charset=utf-8' });
                formData.append('file', blob, 'index.html');

                const response = await fetch(`https://api.vercel.com/v1/deployments?projectId=${vercelProjectId}`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${vercelToken}` },
                    body: formData,
                    signal: AbortSignal.timeout(30000),
                });

                const result = await response.json();
                if (result.url) {
                    return res.json({ success: true, liveUrl: `https://${result.url}` });
                }
            } catch (err) {
                logger.warn('Vercel deploy failed:', err.message);
            }
        }

        // Try Netlify deployment
        const netlifyToken = process.env.NETLIFY_TOKEN;
        const netlifySiteId = process.env.NETLIFY_SITE_ID;
        if (netlifyToken && netlifySiteId) {
            try {
                const formData = new FormData();
                const blob = new Blob([sanitized], { type: 'text/html; charset=utf-8' });
                formData.append('file', blob, 'index.html');

                const response = await fetch(`https://api.netlify.com/api/v1/sites/${netlifySiteId}/deploys`, {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${netlifyToken}` },
                    body: formData,
                    signal: AbortSignal.timeout(30000),
                });

                const result = await response.json();
                if (result.deploy_url) {
                    return res.json({ success: true, liveUrl: result.deploy_url });
                }
            } catch (err) {
                logger.warn('Netlify deploy failed:', err.message);
            }
        }

        const dataUri = `data:text/html;charset=utf-8,${encodeURIComponent(sanitized)}`;
        return res.json({
            success: true,
            liveUrl: dataUri,
            message: 'Preview available via data URI.'
        });

    } catch (err) {
        logger.error('Deploy error:', err.message);
        res.status(500).json({ success: false, message: 'Deployment failed: ' + err.message });
    }
});

// ==========================================
// 404 & ERROR HANDLING
// ==========================================
app.use((req, res) => {
    res.status(404).json({ success: false, code: 'NOT_FOUND', message: 'Endpoint not found.' });
});

app.use((err, req, res, next) => {
    logger.error('Global error:', err.message);
    if (!res.headersSent) {
        res.status(500).json({
            success: false,
            code: 'INTERNAL_ERROR',
            message: process.env.NODE_ENV === 'production' ? 'Service unavailable' : err.message
        });
    }
});

// ==========================================
// GRACEFUL SHUTDOWN
// ==========================================
let shuttingDown = false;

const gracefulShutdown = async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    logger.info('🛑 Shutting down...');
    server.close(async () => {
        try {
            await mongoose.connection.close();
            logger.info('✅ Database connection closed.');
        } catch (_) {}
        logger.info('✅ Shutdown complete.');
        process.exit(0);
    });
    setTimeout(() => {
        logger.error('⚠️ Forced shutdown.');
        process.exit(1);
    }, 10000);
};

process.on('SIGTERM', gracefulShutdown);
process.on('SIGINT', gracefulShutdown);

// ==========================================
// START SERVER
// ==========================================
const server = app.listen(PORT, () => {
    logger.info(`🟢 AXELR FORTRESS ONLINE ON PORT ${PORT} (${process.env.NODE_ENV || 'development'})`);
    logger.info(`🔗 Orchestrator: ${ORCHESTRATOR_URL}`);
    logger.info(`📧 Email: ${transporter ? '✅' : '❌'}`);
    logger.info(`💳 Stripe: ${stripe ? '✅' : '❌'}`);
    logger.info(`👑 Admin: ${ADMIN_EMAIL}`);
});