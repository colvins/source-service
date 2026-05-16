/**
 * 常驻 Node.js 运行时服务
 * 监听 HTTP 端口，接收脚本执行请求，返回结果
 * 每次请求前刷新运行时环境变量并清空业务模块缓存，尽量贴近旧的单进程单请求语义
 */
const http = require("http");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");

const PORT = parseInt(process.env.COLVINS_NODE_RUNTIME_PORT || "18900", 10);
const RUNTIME_CACHE_DIR = process.env.COLVINS_RUNTIME_CACHE_DIR || "/tmp/colvins_runtime_cache";
const SERVER_FILE = __filename;

const scriptPathCache = new Map();

function hashScript(content) {
  return crypto.createHash("md5").update(content).digest("hex");
}

function ensureScriptPath(scriptContent, scriptHash) {
  if (scriptPathCache.has(scriptHash)) {
    return scriptPathCache.get(scriptHash);
  }
  const tmpPath = path.join(RUNTIME_CACHE_DIR, `script_${scriptHash}.js`);
  fs.mkdirSync(RUNTIME_CACHE_DIR, { recursive: true });
  if (!fs.existsSync(tmpPath)) {
    fs.writeFileSync(tmpPath, scriptContent, "utf-8");
  }
  scriptPathCache.set(scriptHash, tmpPath);
  return tmpPath;
}

function resetRuntimeModules() {
  const keep = new Set([SERVER_FILE]);
  for (const key of Object.keys(require.cache)) {
    if (keep.has(key)) continue;
    delete require.cache[key];
  }
}

function applyRuntimeEnv(action, params, context) {
  process.env.COLVINS_ACTION = action || "";
  process.env.COLVINS_PARAMS = JSON.stringify(params || {});
  process.env.COLVINS_CONTEXT = JSON.stringify(context || {});
  process.env.COLVINS_SOURCE_API_URL = String((context && context.baseURL) || "").trim();
}

async function runAction(scriptContent, action, params, context) {
  const scriptHash = hashScript(scriptContent);
  const scriptPath = ensureScriptPath(scriptContent, scriptHash);
  applyRuntimeEnv(action, params, context);
  resetRuntimeModules();
  const mod = require(scriptPath);
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
  req.on("data", (chunk) => {
    body += chunk;
  });
  req.on("end", async () => {
    try {
      const { scriptContent, action, params, context } = JSON.parse(body);
      const result = await runAction(scriptContent, action, params || {}, context || {});
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result || {}));
    } catch (error) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          __error: true,
          message: (error && error.message) || String(error),
          stack: (error && error.stack) || "",
        })
      );
    }
  });
});

server.listen(PORT, "127.0.0.1", () => {
  process.stdout.write(`COLVINS_NODE_RUNTIME_READY port=${PORT}\n`);
});

server.on("error", (err) => {
  process.stderr.write(`node runtime server error: ${err}\n`);
  process.exit(1);
});
