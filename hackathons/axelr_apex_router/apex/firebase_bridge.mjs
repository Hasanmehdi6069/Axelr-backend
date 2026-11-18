/**
 * Firebase AI Logic bridge — the ONLY JavaScript file in the engine.
 *
 * Why: Firebase AI Logic (Gemini Nano) is a JavaScript SDK. It cannot run
 * on-device from a Python process. This sidecar holds the real SDK and
 * exposes POST /infer. apex/tiers.py FirebaseTier calls it as the first
 * hop. On a real device, the same code path runs natively inside a
 * Chrome/Android WebView — the "on-device" claim is genuine there.
 *
 * Run:  node apex/firebase_bridge.mjs
 *
 * Env:
 *   FIREBASE_PORT         default 9091
 *   FIREBASE_API_KEY      required for the real SDK
 *   FIREBASE_PROJECT_ID
 *   FIREBASE_APP_ID
 */
import http from 'node:http';
import { initializeApp } from 'firebase/app';
import { getAI, getGenerativeModel, GoogleAIBackend } from 'firebase/ai';

const PORT = Number(process.env.FIREBASE_PORT || 9091);

let model = null;
let initErr = null;

function getModel() {
  if (model) return model;
  if (initErr) throw initErr;
  try {
    const app = initializeApp({
      apiKey: process.env.FIREBASE_API_KEY,
      projectId: process.env.FIREBASE_PROJECT_ID,
      appId: process.env.FIREBASE_APP_ID,
    }, 'apex-firebase-bridge');
    const ai = getAI(app, { backend: new GoogleAIBackend() });
    model = getGenerativeModel(ai, {
      model: 'gemini-nano',
      generationConfig: {
        responseMimeType: 'application/json',
        temperature: 0.2,
      },
    });
    return model;
  } catch (err) {
    initErr = err;
    throw err;
  }
}

const PROMPT = (p) => `Return STRICT JSON only:
{"taskType": "receipt|clause|classification|summarization|extraction|general",
 "confidence": <0..1>, "text": "<answer>"}

Input:
${p}`;

function parseJSON(raw) {
  if (typeof raw !== 'string') return {};
  const cleaned = raw.replace(/^```(?:json)?\s*/i, '')
                     .replace(/```\s*$/, '').trim();
  try { return JSON.parse(cleaned); } catch {}
  const m = raw.match(/\{[\s\S]*\}/);
  if (m) { try { return JSON.parse(m[0]); } catch {} }
  return {};
}

function heuristics(prompt) {
  const p = prompt.toLowerCase();
  if (/receipt|invoice|\$/.test(p))
    return { taskType: 'receipt', confidence: 0.92 };
  if (/clause|lessee|shall|agreement/.test(p))
    return { taskType: 'clause', confidence: 0.61 };
  if (/extract|date|party/.test(p))
    return { taskType: 'extraction', confidence: 0.88 };
  if (/summari[sz]e/.test(p))
    return { taskType: 'summarization', confidence: 0.78 };
  return { taskType: 'general', confidence: 0.5 };
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, sdk_loaded: !initErr }));
    return;
  }
  if (req.method !== 'POST' || req.url !== '/infer') {
    res.writeHead(404); res.end('not found'); return;
  }
  let body = '';
  for await (const chunk of req) body += chunk;
  let input;
  try { input = JSON.parse(body || '{}'); }
  catch { res.writeHead(400); res.end('bad json'); return; }

  const prompt = String(input.prompt || '');
  try {
    const m = getModel();
    const result = await m.generateContent(PROMPT(prompt));
    const parsed = parseJSON(result.response.text());
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      text: parsed.text || '',
      confidence: typeof parsed.confidence === 'number' ? parsed.confidence : 0.5,
      taskType: parsed.taskType || 'general',
      simulated: false,
    }));
  } catch (err) {
    const h = heuristics(prompt);
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ...h, text: `[simulated] ${h.taskType}`,
      simulated: true, reason: err.message,
    }));
  }
});

server.listen(PORT, () => {
  console.log(`[firebase-bridge] :${PORT} (sdk=${initErr ? 'missing' : 'ready'})`);
});