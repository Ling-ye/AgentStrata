import http from "node:http";
import { spawn } from "node:child_process";
import { pathToFileURL } from "node:url";

const DEFAULT_USAGE_URL = "https://api.tavily.com/usage";
const DEFAULT_UPSTREAM_URL = "http://127.0.0.1:8002";
const DEFAULT_PORT = 8001;
const GATED_TOOLS = new Set(["tavily_search", "tavily_extract"]);

function positiveNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function quotaExhausted(usage) {
  const key = usage?.key;
  const account = usage?.account;
  if (!key || !account) {
    throw new Error("Tavily usage response is missing key/account");
  }
  const limits = [
    [key.usage, key.limit],
    [account.plan_usage, account.plan_limit],
  ].filter(([, limit]) => limit !== null && limit !== undefined && limit !== "");
  if (limits.length === 0) {
    throw new Error("Tavily usage response contains no finite limits");
  }
  return limits.some(([usageValue, limitValue]) => {
    const current = Number(usageValue);
    const limit = Number(limitValue);
    if (!Number.isFinite(current) || !Number.isFinite(limit)) {
      throw new Error("Tavily usage response contains invalid limits");
    }
    return current >= limit;
  });
}

export class TavilyQuotaGate {
  constructor({
    apiKey,
    usageUrl = DEFAULT_USAGE_URL,
    availableTtlMs = 300_000,
    exhaustedTtlMs = 1_800_000,
    failureTtlMs = 30_000,
    timeoutMs = 5_000,
    fetchImpl = globalThis.fetch,
    clock = Date.now,
  }) {
    this.apiKey = apiKey;
    this.usageUrl = usageUrl;
    this.availableTtlMs = availableTtlMs;
    this.exhaustedTtlMs = exhaustedTtlMs;
    this.failureTtlMs = failureTtlMs;
    this.timeoutMs = timeoutMs;
    this.fetchImpl = fetchImpl;
    this.clock = clock;
    this.cache = null;
    this.refreshPromise = null;
  }

  async status() {
    const now = this.clock();
    if (this.cache && now < this.cache.expiresAt) {
      return this.cache.status;
    }
    if (!this.refreshPromise) {
      this.refreshPromise = this.#refresh().finally(() => {
        this.refreshPromise = null;
      });
    }
    return this.refreshPromise;
  }

  async #refresh() {
    if (!this.apiKey) {
      return this.#remember("unknown", this.failureTtlMs);
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetchImpl(this.usageUrl, {
        method: "GET",
        headers: {
          Accept: "application/json",
          Authorization: `Bearer ${this.apiKey}`,
        },
        signal: controller.signal,
      });
      if (!response.ok) {
        throw new Error(`Tavily usage HTTP ${response.status}`);
      }
      const usage = await response.json();
      if (quotaExhausted(usage)) {
        return this.#remember("exhausted", this.exhaustedTtlMs);
      }
      return this.#remember("available", this.availableTtlMs);
    } catch (error) {
      console.warn(`[tavily-quota-proxy] usage check failed; fail-open: ${error.message}`);
      return this.#remember("unknown", this.failureTtlMs);
    } finally {
      clearTimeout(timeout);
    }
  }

  #remember(status, ttlMs) {
    this.cache = { status, expiresAt: this.clock() + ttlMs };
    return status;
  }
}

export function shouldCheckQuota(payload) {
  return (
    payload?.method === "tools/call" &&
    GATED_TOOLS.has(String(payload?.params?.name || ""))
  );
}

export function quotaErrorPayload(requestId) {
  return {
    jsonrpc: "2.0",
    id: requestId ?? null,
    result: {
      content: [
        {
          type: "text",
          text: JSON.stringify({
            ok: false,
            error_code: "mcp_quota_exceeded",
            message:
              "Tavily plan usage limit is exhausted; skip Tavily and fall back to SearXNG.",
          }),
        },
      ],
      isError: true,
    },
  };
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function copyResponseHeaders(upstream, response) {
  for (const [name, value] of upstream.headers.entries()) {
    if (!["connection", "content-length", "transfer-encoding"].includes(name.toLowerCase())) {
      response.setHeader(name, value);
    }
  }
}

export function createProxyServer({ gate, upstreamUrl = DEFAULT_UPSTREAM_URL }) {
  return http.createServer(async (request, response) => {
    try {
      const path = request.url || "/";
      if (request.method === "GET" && path === "/health") {
        const upstream = await fetch(`${upstreamUrl}/mcp`, { method: "GET" });
        response.writeHead(upstream.status < 500 ? 200 : 503, {
          "Content-Type": "application/json",
        });
        response.end(JSON.stringify({ ok: upstream.status < 500 }));
        return;
      }

      const body = await readBody(request);
      let payload = null;
      if (request.method === "POST" && path.split("?")[0] === "/mcp") {
        try {
          payload = JSON.parse(body.toString("utf8"));
        } catch {
          payload = null;
        }
      }
      if (shouldCheckQuota(payload) && (await gate.status()) === "exhausted") {
        response.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
        response.end(JSON.stringify(quotaErrorPayload(payload.id)));
        return;
      }

      const headers = {};
      for (const [name, value] of Object.entries(request.headers)) {
        if (value !== undefined && !["host", "content-length", "connection"].includes(name)) {
          headers[name] = Array.isArray(value) ? value.join(", ") : value;
        }
      }
      const upstream = await fetch(`${upstreamUrl}${path}`, {
        method: request.method,
        headers,
        body: ["GET", "HEAD"].includes(request.method || "") ? undefined : body,
      });
      const upstreamBody = Buffer.from(await upstream.arrayBuffer());
      copyResponseHeaders(upstream, response);
      response.writeHead(upstream.status);
      response.end(upstreamBody);
    } catch (error) {
      response.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
      response.end(JSON.stringify({ ok: false, error: `Tavily proxy error: ${error.message}` }));
    }
  });
}

function start() {
  const upstreamPort = 8002;
  const child = spawn(
    "npx",
    [
      "-y",
      "supergateway",
      "--stdio",
      "npx -y tavily-mcp@latest",
      "--outputTransport",
      "streamableHttp",
      "--port",
      String(upstreamPort),
      "--streamableHttpPath",
      "/mcp",
    ],
    { stdio: "inherit", env: process.env },
  );
  child.on("exit", (code, signal) => {
    console.error(`[tavily-quota-proxy] upstream exited code=${code} signal=${signal}`);
    process.exit(code || 1);
  });

  const gate = new TavilyQuotaGate({
    apiKey: process.env.TAVILY_API_KEY,
    usageUrl: process.env.TAVILY_USAGE_URL || DEFAULT_USAGE_URL,
    availableTtlMs:
      positiveNumber(process.env.TAVILY_USAGE_AVAILABLE_TTL_SECONDS, 300) * 1000,
    exhaustedTtlMs:
      positiveNumber(process.env.TAVILY_USAGE_EXHAUSTED_TTL_SECONDS, 1800) * 1000,
    failureTtlMs:
      positiveNumber(process.env.TAVILY_USAGE_FAILURE_TTL_SECONDS, 30) * 1000,
    timeoutMs: positiveNumber(process.env.TAVILY_USAGE_TIMEOUT_SECONDS, 5) * 1000,
  });
  const server = createProxyServer({
    gate,
    upstreamUrl: `http://127.0.0.1:${upstreamPort}`,
  });
  server.listen(DEFAULT_PORT, "0.0.0.0", () => {
    console.error(`[tavily-quota-proxy] listening on 0.0.0.0:${DEFAULT_PORT}`);
  });

  const shutdown = () => {
    server.close(() => process.exit(0));
    child.kill("SIGTERM");
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  start();
}
