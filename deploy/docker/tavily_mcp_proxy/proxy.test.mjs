import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import {
  TavilyQuotaGate,
  createProxyServer,
  quotaErrorPayload,
  shouldCheckQuota,
} from "./proxy.mjs";

function usage({ keyUsage = 1, keyLimit = 1000, planUsage = 2, planLimit = 1000 } = {}) {
  return {
    key: { usage: keyUsage, limit: keyLimit },
    account: {
      plan_usage: planUsage,
      plan_limit: planLimit,
      paygo_usage: 0,
      paygo_limit: 0,
    },
  };
}

function response(payload, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => payload };
}

test("checks both Tavily search and extract", () => {
  assert.equal(
    shouldCheckQuota({ method: "tools/call", params: { name: "tavily_search" } }),
    true,
  );
  assert.equal(
    shouldCheckQuota({ method: "tools/call", params: { name: "tavily_extract" } }),
    true,
  );
  assert.equal(shouldCheckQuota({ method: "tools/list", params: {} }), false);
});

test("caches available usage for five minutes", async () => {
  let now = 1000;
  let calls = 0;
  const gate = new TavilyQuotaGate({
    apiKey: "test",
    clock: () => now,
    fetchImpl: async () => {
      calls += 1;
      return response(usage());
    },
  });

  assert.equal(await gate.status(), "available");
  now += 299_999;
  assert.equal(await gate.status(), "available");
  assert.equal(calls, 1);
  now += 2;
  assert.equal(await gate.status(), "available");
  assert.equal(calls, 2);
});

test("treats plan exhaustion as exhausted and ignores PayGo", async () => {
  const exhaustedUsage = usage({ planUsage: 1000, planLimit: 1000 });
  exhaustedUsage.key.limit = null;
  exhaustedUsage.account.paygo_limit = 10000;
  const gate = new TavilyQuotaGate({
    apiKey: "test",
    fetchImpl: async () => response(exhaustedUsage),
  });
  assert.equal(await gate.status(), "exhausted");
});

test("treats API key exhaustion as exhausted", async () => {
  const gate = new TavilyQuotaGate({
    apiKey: "test",
    fetchImpl: async () => response(usage({ keyUsage: 1000, keyLimit: 1000 })),
  });
  assert.equal(await gate.status(), "exhausted");
});

test("fails open and caches usage check failures briefly", async () => {
  let calls = 0;
  const gate = new TavilyQuotaGate({
    apiKey: "test",
    fetchImpl: async () => {
      calls += 1;
      throw new Error("timeout");
    },
  });

  assert.equal(await gate.status(), "unknown");
  assert.equal(await gate.status(), "unknown");
  assert.equal(calls, 1);
});

test("deduplicates concurrent usage refreshes", async () => {
  let calls = 0;
  const gate = new TavilyQuotaGate({
    apiKey: "test",
    fetchImpl: async () => {
      calls += 1;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return response(usage());
    },
  });
  assert.deepEqual(await Promise.all([gate.status(), gate.status()]), [
    "available",
    "available",
  ]);
  assert.equal(calls, 1);
});

test("quota error is a standard MCP error result", () => {
  const payload = quotaErrorPayload(7);
  assert.equal(payload.id, 7);
  assert.equal(payload.result.isError, true);
  const error = JSON.parse(payload.result.content[0].text);
  assert.equal(error.error_code, "mcp_quota_exceeded");
});

test("exhausted quota blocks the upstream Tavily tool call", async (context) => {
  let upstreamCalls = 0;
  const upstream = http.createServer((request, response) => {
    upstreamCalls += 1;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ jsonrpc: "2.0", id: 1, result: {} }));
  });
  await listen(upstream);
  context.after(() => upstream.close());

  const proxy = createProxyServer({
    gate: { status: async () => "exhausted" },
    upstreamUrl: localUrl(upstream),
  });
  await listen(proxy);
  context.after(() => proxy.close());

  const result = await fetch(`${localUrl(proxy)}/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 4,
      method: "tools/call",
      params: { name: "tavily_search", arguments: { query: "latest" } },
    }),
  });
  const payload = await result.json();
  assert.equal(payload.result.isError, true);
  assert.equal(upstreamCalls, 0);
});

async function listen(server) {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
}

function localUrl(server) {
  return `http://127.0.0.1:${server.address().port}`;
}
