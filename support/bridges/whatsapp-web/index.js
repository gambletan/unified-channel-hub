/**
 * WhatsApp Web Bridge — wraps whatsapp-web.js with HTTP API.
 *
 * Exposes:
 *   GET  /qr          → current QR code (data URI or "authenticated")
 *   GET  /status      → { authenticated, phone, name }
 *   POST /send        → { to: "phone", text: "msg" } → send message
 *   GET  /messages    → long-poll for incoming messages (SSE)
 *
 * Usage:
 *   npm install
 *   node index.js [--port 8084] [--data ./data]
 *
 * The Python adapter connects to this bridge via HTTP.
 */

const { Client, LocalAuth } = require("whatsapp-web.js");
const express = require("express");
const qrcode = require("qrcode");
const path = require("path");

// Parse CLI args
const args = process.argv.slice(2);
const getArg = (flag, def) => {
  const idx = args.indexOf(flag);
  return idx >= 0 && args[idx + 1] ? args[idx + 1] : def;
};

const PORT = parseInt(getArg("--port", "8084"));
const DATA_DIR = getArg("--data", path.join(__dirname, "data"));

// State
let currentQR = null;
let authenticated = false;
let clientInfo = {};
const messageQueue = []; // incoming messages buffer
const sseClients = []; // SSE connections

// WhatsApp client
const client = new Client({
  authStrategy: new LocalAuth({ dataPath: DATA_DIR }),
  puppeteer: {
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

client.on("qr", async (qr) => {
  currentQR = await qrcode.toDataURL(qr);
  authenticated = false;
  console.log("[whatsapp-bridge] QR code generated, waiting for scan...");
});

client.on("authenticated", () => {
  console.log("[whatsapp-bridge] Authenticated");
  authenticated = true;
  currentQR = null;
});

client.on("ready", () => {
  const info = client.info;
  clientInfo = {
    phone: info?.wid?.user || "",
    name: info?.pushname || "",
    platform: info?.platform || "",
  };
  console.log(
    `[whatsapp-bridge] Ready: ${clientInfo.name} (${clientInfo.phone})`
  );
});

client.on("disconnected", (reason) => {
  console.log("[whatsapp-bridge] Disconnected:", reason);
  authenticated = false;
  clientInfo = {};
});

client.on("message", (msg) => {
  // Skip status messages and self messages
  if (msg.from === "status@broadcast") return;
  if (msg.fromMe) return;

  const event = {
    id: msg.id._serialized,
    from: msg.from.replace("@c.us", ""),
    chat_id: msg.from,
    text: msg.body || "",
    type: msg.type, // chat, image, video, audio, document, etc.
    timestamp: msg.timestamp,
    sender_name: msg._data?.notifyName || "",
    has_media: msg.hasMedia,
    is_group: msg.from.endsWith("@g.us"),
  };

  // Push to queue for SSE clients
  messageQueue.push(event);

  // Notify SSE clients
  sseClients.forEach((res) => {
    res.write(`data: ${JSON.stringify(event)}\n\n`);
  });
});

// HTTP API
const app = express();
app.use(express.json());

// QR code endpoint (JSON)
app.get("/qr", (req, res) => {
  if (authenticated) {
    return res.json({ status: "authenticated", phone: clientInfo.phone });
  }
  if (currentQR) {
    return res.json({ status: "qr", qr: currentQR });
  }
  return res.json({ status: "initializing" });
});

// QR code page (human-friendly)
app.get("/scan", (req, res) => {
  res.send(`<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WhatsApp Scan</title>
<style>body{font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#075e54;margin:0}
.card{background:#fff;border-radius:16px;padding:40px;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.3)}
h2{margin-bottom:16px}img{border-radius:8px}
.ok{color:#25d366;font-size:48px}</style></head>
<body><div class="card" id="card"><h2>📱 WhatsApp 扫码连接</h2><p id="msg">加载中...</p></div>
<script>
async function poll(){
  try{
    const r=await fetch('/qr');const d=await r.json();
    if(d.status==='authenticated'){
      document.getElementById('card').innerHTML='<p class="ok">✅</p><h2>已连接</h2><p>'+d.phone+'</p>';return;
    }
    if(d.status==='qr'){
      document.getElementById('card').innerHTML='<h2>📱 用 WhatsApp 扫码</h2><img src="'+d.qr+'" width="280"><p style="color:#999;margin-top:12px">打开 WhatsApp → 设置 → 关联设备 → 扫描</p>';
    }else{
      document.getElementById('msg').textContent='初始化中，请稍候...';
    }
  }catch(e){document.getElementById('msg').textContent='Bridge 未启动';}
  setTimeout(poll,3000);
}
poll();
</script></body></html>`);
});

// Status
app.get("/status", (req, res) => {
  res.json({
    authenticated,
    ...clientInfo,
    uptime: process.uptime(),
  });
});

// Send message
app.post("/send", async (req, res) => {
  const { to, text } = req.body;
  if (!to || !text) {
    return res.status(400).json({ error: "to and text required" });
  }
  if (!authenticated) {
    return res.status(503).json({ error: "not authenticated" });
  }
  try {
    // Ensure @c.us suffix for individual chats
    const chatId = to.includes("@") ? to : `${to}@c.us`;
    const sent = await client.sendMessage(chatId, text);
    res.json({ ok: true, id: sent.id._serialized });
  } catch (e) {
    console.error("[whatsapp-bridge] Send error:", e.message);
    res.status(500).json({ error: e.message });
  }
});

// SSE endpoint for incoming messages
app.get("/messages", (req, res) => {
  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
  });

  // Send any buffered messages
  while (messageQueue.length > 0) {
    const msg = messageQueue.shift();
    res.write(`data: ${JSON.stringify(msg)}\n\n`);
  }

  sseClients.push(res);

  req.on("close", () => {
    const idx = sseClients.indexOf(res);
    if (idx >= 0) sseClients.splice(idx, 1);
  });
});

// Health
app.get("/health", (req, res) => {
  res.json({ ok: true, authenticated });
});

// Start
app.listen(PORT, () => {
  console.log(`[whatsapp-bridge] HTTP API on port ${PORT}`);
});

console.log("[whatsapp-bridge] Initializing WhatsApp client...");
client.initialize();
