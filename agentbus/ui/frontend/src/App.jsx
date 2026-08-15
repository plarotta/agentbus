import React, { useCallback, useEffect, useRef, useState } from "react";
import ReactFlow, { Background, Controls, MarkerType } from "reactflow";
import "reactflow/dist/style.css";
import { api, openStream } from "./api.js";

const TABS = ["Graph", "Inspect", "Replay", "Design", "Builder"];

export default function App() {
  const [tab, setTab] = useState("Graph");
  const [health, setHealth] = useState(null);

  useEffect(() => {
    const tick = () => api.health().then(setHealth).catch(() => setHealth(null));
    tick();
    const id = setInterval(tick, 4000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          AgentBus<span className="mark">ui</span>
        </div>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t}
              className={t === tab ? "tab active" : "tab"}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
        </nav>
        <div className={"status " + (health?.running ? "up" : "down")}>
          {health?.running ? "bus running" : "bus down"}
        </div>
      </header>
      <main className="content">
        {tab === "Graph" && <GraphView />}
        {tab === "Inspect" && <InspectView />}
        {tab === "Replay" && <ReplayView />}
        {tab === "Design" && <DesignView />}
        {tab === "Builder" && <BuilderView />}
      </main>
    </div>
  );
}

// ── Graph ────────────────────────────────────────────────────────────────────

function layoutGraph(graph) {
  const nodes = [];
  const edges = [];
  const colW = 320;
  const rowH = 90;
  graph.nodes.forEach((n, i) => {
    nodes.push({
      id: "n:" + n.name,
      position: { x: 40, y: 40 + i * rowH },
      data: {
        label: (
          <div className="node-card">
            <div className="node-name">{n.name}</div>
            <div className="node-meta">
              {n.state.toLowerCase()} · {n.messages_received}↓ {n.messages_published}↑
            </div>
          </div>
        ),
      },
      className: "rf-node node-" + n.state.toLowerCase(),
    });
  });
  graph.topics.forEach((t, i) => {
    nodes.push({
      id: "t:" + t.name,
      position: { x: 40 + colW * 1.4, y: 40 + i * rowH },
      data: {
        label: (
          <div className="topic-card">
            <div className="topic-name">{t.name}</div>
            <div className="topic-meta">
              {t.schema_name} · {t.message_count} msgs
            </div>
          </div>
        ),
      },
      className: "rf-topic",
    });
  });
  graph.edges.forEach((e, i) => {
    const source = e.direction === "pub" ? "n:" + e.node : "t:" + e.topic;
    const target = e.direction === "pub" ? "t:" + e.topic : "n:" + e.node;
    edges.push({
      id: `e${i}`,
      source,
      target,
      animated: e.direction === "pub",
      markerEnd: { type: MarkerType.ArrowClosed, color: e.direction === "pub" ? "#2a43d0" : "#aaa494" },
      style: { stroke: e.direction === "pub" ? "#2a43d0" : "#aaa494", strokeWidth: 1.5 },
    });
  });
  return { nodes, edges };
}

function GraphView() {
  const [flow, setFlow] = useState({ nodes: [], edges: [] });
  const [err, setErr] = useState(null);

  useEffect(() => {
    const tick = () =>
      api
        .graph()
        .then((g) => {
          setFlow(layoutGraph(g));
          setErr(null);
        })
        .catch((e) => setErr(e.message));
    tick();
    const id = setInterval(tick, 2500);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="graph-wrap">
      {err && <div className="banner err">{err}</div>}
      <ReactFlow nodes={flow.nodes} edges={flow.edges} fitView proOptions={{ hideAttribution: true }}>
        <Background color="#d8d2c2" gap={26} size={1} />
        <Controls showInteractive={false} />
      </ReactFlow>
      <div className="legend">
        <span><i className="bar pub" /> publishes</span>
        <span><i className="bar sub" /> subscribes</span>
      </div>
    </div>
  );
}

// ── Inspect ──────────────────────────────────────────────────────────────────

function InspectView() {
  const [topics, setTopics] = useState([]);
  const [sel, setSel] = useState(null);
  const [rows, setRows] = useState([]);
  const [live, setLive] = useState(false);
  const wsRef = useRef(null);

  useEffect(() => {
    api.topics().then(setTopics).catch(() => {});
  }, []);

  const loadHistory = useCallback((topic) => {
    setSel(topic);
    api.history(topic, 50).then((ms) => setRows(ms.reverse())).catch(() => setRows([]));
  }, []);

  useEffect(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    if (live && sel) {
      const ws = openStream({ pattern: sel });
      ws.onmessage = (ev) => {
        const env = JSON.parse(ev.data);
        setRows((r) => [env, ...r].slice(0, 200));
      };
      wsRef.current = ws;
    }
    return () => wsRef.current && wsRef.current.close();
  }, [live, sel]);

  return (
    <div className="split">
      <aside className="list">
        <h3>Topics</h3>
        {topics.map((t) => (
          <button
            key={t.name}
            className={t.name === sel ? "row active" : "row"}
            onClick={() => loadHistory(t.name)}
          >
            <span className="mono">{t.name}</span>
            <span className="tag">{t.message_count}</span>
          </button>
        ))}
      </aside>
      <section className="detail">
        <div className="detail-head">
          <h3>{sel || "Select a topic"}</h3>
          {sel && (
            <label className="toggle">
              <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
              live tail
            </label>
          )}
        </div>
        <MessageTable rows={rows} />
      </section>
    </div>
  );
}

function MessageTable({ rows }) {
  return (
    <div className="msglist">
      {rows.length === 0 && <div className="muted">no messages</div>}
      {rows.map((m, i) => (
        <details key={m.id || i} className="msg">
          <summary>
            <span className="tag off">#{m.offset ?? "—"}</span>
            <span className="mono">{m.topic}</span>
            <span className="muted">{m.source_node}</span>
            <span className="muted ts">{(m.timestamp || "").slice(11, 19)}</span>
          </summary>
          <pre>{JSON.stringify(m.payload, null, 2)}</pre>
        </details>
      ))}
    </div>
  );
}

// ── Replay ───────────────────────────────────────────────────────────────────

function ReplayView() {
  const [pattern, setPattern] = useState("/**");
  const [fromOffset, setFromOffset] = useState(0);
  const [rows, setRows] = useState([]);
  const [latest, setLatest] = useState(null);
  const wsRef = useRef(null);

  useEffect(() => {
    api.latestOffset().then((d) => setLatest(d.offset)).catch(() => {});
  }, []);

  const start = () => {
    setRows([]);
    if (wsRef.current) wsRef.current.close();
    const ws = openStream({ pattern, fromOffset: Number(fromOffset) });
    ws.onmessage = (ev) => setRows((r) => [...r, JSON.parse(ev.data)]);
    wsRef.current = ws;
  };

  useEffect(() => () => wsRef.current && wsRef.current.close(), []);

  return (
    <div className="pane">
      <div className="toolbar">
        <label>
          pattern
          <input value={pattern} onChange={(e) => setPattern(e.target.value)} />
        </label>
        <label>
          from offset
          <input
            type="number"
            value={fromOffset}
            onChange={(e) => setFromOffset(e.target.value)}
          />
        </label>
        <span className="muted">latest: {latest ?? "—"}</span>
        <button className="primary" onClick={start}>
          Replay
        </button>
      </div>
      <MessageTable rows={rows} />
    </div>
  );
}

// ── Design ───────────────────────────────────────────────────────────────────

const EMPTY_FIELD = () => ({ name: "", type: "str" });

function DesignView() {
  const [msg, setMsg] = useState(null);

  return (
    <div className="design">
      {msg && <div className={"banner " + (msg.ok ? "ok" : "err")}>{msg.text}</div>}
      <div className="cards">
        <TopicForm onMsg={setMsg} />
        <NodeForm onMsg={setMsg} />
      </div>
      <ApplyBar onMsg={setMsg} />
    </div>
  );
}

function TopicForm({ onMsg }) {
  const [name, setName] = useState("/");
  const [cls, setCls] = useState("");
  const [retention, setRetention] = useState(0);
  const [fields, setFields] = useState([EMPTY_FIELD()]);

  const submit = async () => {
    try {
      await api.createTopic({
        name,
        schema_class: cls,
        fields: fields.filter((f) => f.name),
        retention: Number(retention),
      });
      onMsg({ ok: true, text: `Topic ${name} created (apply to activate)` });
    } catch (e) {
      onMsg({ ok: false, text: e.message });
    }
  };

  return (
    <div className="card">
      <h3>New topic</h3>
      <label>name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
      <label>schema class<input value={cls} onChange={(e) => setCls(e.target.value)} placeholder="OrderEvent" /></label>
      <label>retention<input type="number" value={retention} onChange={(e) => setRetention(e.target.value)} /></label>
      <div className="fields">
        <div className="fields-head">fields</div>
        {fields.map((f, i) => (
          <div className="field-row" key={i}>
            <input
              placeholder="name"
              value={f.name}
              onChange={(e) => setFields((fs) => fs.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
            />
            <select
              value={f.type}
              onChange={(e) => setFields((fs) => fs.map((x, j) => (j === i ? { ...x, type: e.target.value } : x)))}
            >
              {["str", "int", "float", "bool", "list", "dict"].map((t) => (
                <option key={t}>{t}</option>
              ))}
            </select>
          </div>
        ))}
        <button className="ghost" onClick={() => setFields((fs) => [...fs, EMPTY_FIELD()])}>+ field</button>
      </div>
      <button className="primary" onClick={submit}>Create topic</button>
    </div>
  );
}

function NodeForm({ onMsg }) {
  const [className, setClassName] = useState("");
  const [nodeName, setNodeName] = useState("");
  const [subs, setSubs] = useState("");
  const [pubs, setPubs] = useState("");
  const [body, setBody] = useState("self.logger.info('got %s', msg.topic)");

  const submit = async () => {
    try {
      await api.createNode({
        class_name: className,
        node_name: nodeName,
        subscriptions: subs.split(",").map((s) => s.trim()).filter(Boolean),
        publications: pubs.split(",").map((s) => s.trim()).filter(Boolean),
        on_message: body,
      });
      onMsg({ ok: true, text: `Node ${nodeName} created (apply to activate)` });
    } catch (e) {
      onMsg({ ok: false, text: e.message });
    }
  };

  return (
    <div className="card">
      <h3>New node</h3>
      <label>class name<input value={className} onChange={(e) => setClassName(e.target.value)} placeholder="OrderLogger" /></label>
      <label>node name<input value={nodeName} onChange={(e) => setNodeName(e.target.value)} placeholder="order_logger" /></label>
      <label>subscriptions<input value={subs} onChange={(e) => setSubs(e.target.value)} placeholder="/orders, /system/*" /></label>
      <label>publications<input value={pubs} onChange={(e) => setPubs(e.target.value)} placeholder="/outbound" /></label>
      <label>on_message body
        <textarea rows={6} value={body} onChange={(e) => setBody(e.target.value)} className="code" />
      </label>
      <button className="primary" onClick={submit}>Create node</button>
    </div>
  );
}

function ApplyBar({ onMsg }) {
  const [busy, setBusy] = useState(false);
  const apply = async () => {
    setBusy(true);
    try {
      await api.apply();
      onMsg({ ok: true, text: "Applied — bus rebuilt and restarted." });
    } catch (e) {
      onMsg({ ok: false, text: e.message });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="applybar">
      <span className="muted">Changes are written to agentbus.yaml + generated code. Apply to restart the bus.</span>
      <button className="apply" disabled={busy} onClick={apply}>
        {busy ? "Applying…" : "Apply changes"}
      </button>
    </div>
  );
}

// ── Builder ──────────────────────────────────────────────────────────────────

function BuilderView() {
  const [log, setLog] = useState([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  const send = async () => {
    const text = input.trim();
    if (!text) return;
    setInput("");
    setLog((l) => [...l, { role: "user", text }]);
    setBusy(true);
    try {
      const { response } = await api.builder(text);
      setLog((l) => [...l, { role: "assistant", text: response }]);
    } catch (e) {
      setLog((l) => [...l, { role: "error", text: e.message }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="builder">
      <div className="chatlog">
        {log.length === 0 && (
          <div className="muted hint">
            Ask the builder to design your workflow, e.g.{" "}
            <em>"add a topic /orders with an order_id, and a node that logs every order"</em>.
          </div>
        )}
        {log.map((m, i) => (
          <div key={i} className={"bubble " + m.role}>
            <div className="who">{m.role}</div>
            <div className="text">{m.text}</div>
          </div>
        ))}
        {busy && <div className="bubble assistant"><div className="who">assistant</div><div className="text muted">thinking…</div></div>}
      </div>
      <div className="composer">
        <textarea
          value={input}
          rows={2}
          placeholder="Describe what to build…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        <button className="primary" disabled={busy} onClick={send}>Send</button>
      </div>
    </div>
  );
}
