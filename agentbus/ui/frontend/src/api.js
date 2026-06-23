// Thin REST/WS client for the AgentBus UI backend.

async function jget(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

async function jsend(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `${path}: ${r.status}`);
  return data;
}

export const api = {
  health: () => jget("/api/health"),
  graph: () => jget("/api/graph"),
  topics: () => jget("/api/topics"),
  nodes: () => jget("/api/nodes"),
  history: (topic, n = 30) =>
    jget(`/api/history?topic=${encodeURIComponent(topic)}&n=${n}`),
  latestOffset: () => jget("/api/latest-offset"),
  config: () => jget("/api/config"),
  putConfig: (cfg) => jsend("PUT", "/api/config", cfg),
  manifest: () => jget("/api/manifest"),
  createTopic: (spec) => jsend("POST", "/api/topics", spec),
  createNode: (spec) => jsend("POST", "/api/nodes", spec),
  apply: () => jsend("POST", "/api/apply"),
  builder: (message) => jsend("POST", "/api/builder", { message }),
};

// Open a live message stream. Returns the WebSocket; caller wires onmessage.
export function openStream({ pattern = "/**", fromOffset = null } = {}) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ pattern });
  if (fromOffset !== null) params.set("from_offset", String(fromOffset));
  return new WebSocket(`${proto}://${location.host}/ws/stream?${params}`);
}
