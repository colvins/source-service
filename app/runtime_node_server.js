/**
 * 常驻 Node.js 运行时服务
 * 监听 Unix socket（或 TCP 端口），接收脚本执行请求，返回结果
 * 脚本按 hash 缓存 require()，避免重复 I/O
 */
const http = require("http");
const path = require("path");
const fs = require("fs");
const vm = require("vm");

const PORT = parseInt(process.env.COLVINS_NODE_RUNTIME_PORT || "18900", 10);
const SHIM_DIR = process.env.COLVINS_JS_SHIM_DIR || "";
const NODE_GLOBAL_MODULE_DIR = "/usr/local/lib/node_modules";
const RUNTIME_CACHE_DIR = process.env.COLVINS_RUNTIME_CACHE_DIR || "/tmp/colvins_runtime_cache";

// 脚本模块缓存：hash -> module exports
const scriptCache = new Map();

function hashScript(content) {
  const crypto = require("crypto");
  return crypto.createHash("md5").update(content).digest("hex");
}

function loadScript(scriptContent, scriptHash) {
  if (scriptCache.has(scriptHash)) {
    return scriptCache.get(scriptHash);
  }
  // 写临时文件用于 require()（只在内容变化时写）
  const tmpPath = path.join(RUNTIME_CACHE_DIR, `script_${scriptHash}.js`);
  if (!fs.existsSync(tmpPath)) {
    fs.mkdirSync(RUNTIME_CACHE_DIR, { recursive: true });
    fs.writeFileSync(tmpPath, scriptContent, "utf-8");
  }
  // 设置 NODE_PATH 后 require
  const oldPaths = module.paths.slice();
  if (SHIM_DIR) module.paths.unshift(SHIM_DIR);
  if (NODE_GLOBAL_MODULE_DIR) module.paths.unshift(NODE_GLOBAL_MODULE_DIR);
  let mod;
  try {
    // 清除旧缓存确保重新加载
    delete require.cache[require.resolve(tmpPath)];
    mod = require(tmpPath);
  } finally {
    module.paths.length = 0;
    oldPaths.forEach((p) => module.paths.push(p));
  }
  scriptCache.set(scriptHash, mod);
  return mod;
}

async function runAction(scriptContent, action, params, context) {
  const scriptHash = hashScript(scriptContent);
  const mod = loadScript(scriptContent, scriptHash);
  const fn = mod && mod[action];
  if (typeof fn !== "function") {
    throw new Error(`source does not export function: ${action}`);
  }
  return await fn(params, context);
}

const server = http.createServer(async (req, res) => {
  if (req.method !== "POST" || req.url !== "/run") {
    res.writeHead(404);
    res.end("not found");
    return;
  }
  let body = "";
  req.on("data", (chunk) => { body += chunk; });
  req.on("end", async () => {
    try {
      const { scriptContent, action, params, context } = JSON.parse(body);
      const result = await runAction(scriptContent, action, params || {}, context || {});
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result || {}));
    } catch (error) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ __error: true, message: (error && error.message) || String(error), stack: (error && error.stack) || "" }));
    }
  });
});

server.listen(PORT, "127.0.0.1", () => {
  // 输出 ready 信号供 Python 侧检测
  process.stdout.write(`COLVINS_NODE_RUNTIME_READY port=${PORT}\n`);
});

server.on("error", (err) => {
  process.stderr.write(`node runtime server error: ${err}\n`);
  process.exit(1);
});
