interface Env {
  UPSTASH_REDIS_REST_URL: string;
  UPSTASH_REDIS_REST_TOKEN: string;
  RENDER_HEALTH_URL: string;
  RATE_LIMIT_MAX_REQUESTS: string;
  RATE_LIMIT_WINDOW_SECONDS: string;
  RATE_LIMIT_TIER_PREMIUM_MAX: string;
  RATE_LIMIT_TIER_FREE_MAX: string;
}

interface RateLimitResponse {
  success: boolean;
  limit: number;
  remaining: number;
  reset: number;
  tier: string;
}

const REDIS_SCRIPT = `
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local max = tonumber(ARGV[3])

local current = redis.call('ZRANGEBYSCORE', key, now - window, now)
local count = #current

if count < max then
  redis.call('ZADD', key, now, now)
  redis.call('EXPIRE', key, window)
  return {1, max - count, now + window}
else
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')[2]
  return {0, 0, tonumber(oldest) + window}
end
`;

async function rateLimit(env: Env, identifier: string, tier: string = 'free'): Promise<RateLimitResponse> {
  const now = Math.floor(Date.now() / 1000);
  const window = parseInt(env.RATE_LIMIT_WINDOW_SECONDS || '60');
  
  let max: number;
  if (tier === 'premium') {
    max = parseInt(env.RATE_LIMIT_TIER_PREMIUM_MAX || env.RATE_LIMIT_MAX_REQUESTS || '1000');
  } else {
    max = parseInt(env.RATE_LIMIT_TIER_FREE_MAX || env.RATE_LIMIT_MAX_REQUESTS || '100');
  }

  const response = await fetch(`${env.UPSTASH_REDIS_REST_URL}/eval`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${env.UPSTASH_REDIS_REST_TOKEN}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      script: REDIS_SCRIPT,
      keys: [identifier],
      args: [now.toString(), window.toString(), max.toString()]
    })
  });

  const result = await response.json();
  const [success, remaining, reset] = result.result;

  return {
    success: success === 1,
    limit: max,
    remaining,
    reset,
    tier
  };
}

function getTierFromToken(authHeader: string | null): string {
  if (!authHeader) return 'free';
  if (authHeader.toLowerCase().startsWith('bearer premium')) {
    return 'premium';
  }
  return 'free';
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === '/health') {
      return new Response(JSON.stringify({ status: 'ok', shield: 'operational' }), {
        headers: { 'Content-Type': 'application/json' }
      });
    }

    const ip = request.headers.get('CF-Connecting-IP') || 'unknown';
    const authHeader = request.headers.get('Authorization');
    const tier = getTierFromToken(authHeader);
    
    const token = authHeader || ip;
    const identifier = `rate:${Buffer.from(token).toString('base64')}`;

    const limitResult = await rateLimit(env, identifier, tier);

    if (!limitResult.success) {
      return new Response(JSON.stringify({
        error: 'Too Many Requests',
        limit: limitResult.limit,
        retry_after: limitResult.reset - Math.floor(Date.now() / 1000),
        tier: limitResult.tier
      }), {
        status: 429,
        headers: {
          'Content-Type': 'application/json',
          'X-RateLimit-Limit': limitResult.limit.toString(),
          'X-RateLimit-Remaining': '0',
          'X-RateLimit-Reset': limitResult.reset.toString(),
          'Retry-After': (limitResult.reset - Math.floor(Date.now() / 1000)).toString(),
          'X-RateLimit-Tier': limitResult.tier
        }
      });
    }

    const newHeaders = new Headers();
    newHeaders.set('X-RateLimit-Limit', limitResult.limit.toString());
    newHeaders.set('X-RateLimit-Remaining', limitResult.remaining.toString());
    newHeaders.set('X-RateLimit-Reset', limitResult.reset.toString());
    newHeaders.set('X-RateLimit-Tier', limitResult.tier);

    const response = await fetch(request, {
      headers: request.headers
    });

    response.headers.forEach((value, key) => {
      if (!key.toLowerCase().startsWith('x-ratelimit')) {
        newHeaders.set(key, value);
      }
    });

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: newHeaders
    });
  },

  async scheduled(event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil((async () => {
      try {
        const healthUrl = env.RENDER_HEALTH_URL || 'https://axelr-backend.onrender.com/health';
        await fetch(healthUrl, {
          method: 'GET',
          headers: { 
            'User-Agent': 'Cloudflare-Worker-KeepAlive',
            'CF-Edge-IP': '127.0.0.1'
          },
          signal: AbortSignal.timeout(5000)
        });
        console.log('Keepalive ping successful');
      } catch (e) {
        console.error('Keepalive ping failed:', e);
      }
    })());
  }
};