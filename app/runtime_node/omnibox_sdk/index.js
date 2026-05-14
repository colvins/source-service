const CONTEXT = JSON.parse(process.env.COLVINS_CONTEXT || "{}");
const API_BASE_URL = process.env.COLVINS_SOURCE_API_URL || CONTEXT.baseURL || process.env.OMNIBOX_API_URL || "";
const RUNTIME_CACHE_DIR = process.env.COLVINS_RUNTIME_CACHE_DIR || "";
const driveFolderPathCache = new Map();
const crypto = require("crypto");
const fs = require("fs");

async function callAPI(endpoint, data = {}) {
  if (!API_BASE_URL) {
    return { data: null };
  }
  const response = await request(`${API_BASE_URL}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: data,
    timeout: 60000,
  });
  if (response.statusCode < 200 || response.statusCode >= 300) {
    throw new Error(`API ${endpoint} failed: HTTP ${response.statusCode}`);
  }
  const result = JSON.parse(response.body || "{}");
  if (result.success === false) {
    throw new Error(result.message || `API ${endpoint} failed`);
  }
  return result;
}

async function callSourceService(endpoint, data = {}) {
  if (!API_BASE_URL) {
    return {};
  }
  const response = await request(`${API_BASE_URL}${endpoint}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: data,
    timeout: 60000,
  });
  if (response.statusCode < 200 || response.statusCode >= 300) {
    throw new Error(`Source Service ${endpoint} failed: HTTP ${response.statusCode}`);
  }
  return JSON.parse(response.body || "{}");
}

function driveProviderFromShareURL(shareURL) {
  const text = String(shareURL || "").toLowerCase();
  if (text.includes("quark.cn")) return "quark";
  if (text.includes("uc.cn") || text.includes("ucdisk.cn")) return "uc";
  if (text.includes("baidu.com") || text.includes("yun.baidu.com")) return "baidu";
  if (text.includes("aliyundrive.com") || text.includes("alipan.com")) return "ali";
  if (text.includes("115.com")) return "115";
  if (text.includes("123pan.com")) return "123";
  if (text.includes("xunlei.com")) return "thunder";
  if (text.includes("cloud.189.cn")) return "tianyi";
  return "quark";
}

function driveDisplayName(provider) {
  const names = {
    quark: "夸克网盘",
    uc: "UC网盘",
    baidu: "百度网盘",
    ali: "阿里网盘",
    "115": "115网盘",
    "123": "123网盘",
    thunder: "迅雷网盘",
    tianyi: "天翼网盘",
  };
  return names[provider] || "网盘";
}

function encodeDriveFileRef(file) {
  const json = JSON.stringify(file || {});
  return `colvins:${Buffer.from(json, "utf8").toString("base64url")}`;
}

function decodeDriveFileRef(value) {
  const text = String(value || "");
  if (!text.startsWith("colvins:")) {
    return null;
  }
  try {
    return JSON.parse(Buffer.from(text.slice("colvins:".length), "base64url").toString("utf8"));
  } catch {
    return null;
  }
}

function driveFolderCacheKey(shareURL, fid) {
  return `${shareURL}::${fid || "0"}`;
}

function toOmniBoxDriveFile(shareURL, file, pdirFid = "0") {
  const isDir = Boolean(file && file.isDir);
  const parentPath = driveFolderPathCache.get(driveFolderCacheKey(shareURL, pdirFid)) || "";
  const filePath = [parentPath, file.path || file.name || ""].filter(Boolean).join("/");
  const ref = {
    fid: file.fid || "",
    name: file.name || "",
    path: filePath,
    shareFidToken: file.shareFidToken || "",
    parentFid: pdirFid || "0",
  };
  if (isDir && ref.fid) {
    driveFolderPathCache.set(driveFolderCacheKey(shareURL, ref.fid), filePath);
  }
  const fileId = isDir ? ref.fid : encodeDriveFileRef(ref);
  return {
    ...file,
    fid: fileId,
    file_id: fileId,
    raw_fid: ref.fid,
    file_name: file.name || "",
    name: file.name || "",
    size: file.size || 0,
    file_size: file.size || 0,
    file: !isDir,
    dir: isDir,
    is_dir: isDir,
    file_type: isDir ? "folder" : "file",
    format_type: file.isVideo ? "video" : "",
    share_fid_token: file.shareFidToken || "",
    pdir_fid: pdirFid || "0",
    path: filePath,
  };
}

async function request(url, options = {}) {
  const timeout = Number(options.timeout || 30000);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const fetchOptions = { ...options, signal: controller.signal };
  delete fetchOptions.timeout;

  if (fetchOptions.body && typeof fetchOptions.body === "object" && !(fetchOptions.body instanceof Uint8Array)) {
    fetchOptions.body = JSON.stringify(fetchOptions.body);
    fetchOptions.headers = {
      "Content-Type": "application/json",
      ...(fetchOptions.headers || {}),
    };
  }

  const response = await fetch(url, fetchOptions).finally(() => clearTimeout(timer));
  const body = await response.text();
  const headers = {};
  response.headers.forEach((value, key) => {
    headers[key] = value;
  });
  return {
    statusCode: response.status,
    body,
    headers,
  };
}

async function log(level, message) {
  process.stderr.write(`[${level}] ${message}\n`);
}

async function processScraping() {
  return {};
}

async function getScrapeMetadata() {
  return { scrapeData: null, videoMappings: null };
}

async function getDanmakuByFileName() {
  return [];
}

async function addPlayHistory() {
  return false;
}

async function sniffVideo() {
  return null;
}

async function getCache(key) {
  if (!RUNTIME_CACHE_DIR || !key) return null;
  try {
    const filePath = getRuntimeCacheFilePath(key);
    if (!fs.existsSync(filePath)) return null;
    const payload = JSON.parse(fs.readFileSync(filePath, "utf8"));
    if (!payload || typeof payload !== "object") return null;
    if (payload.expiresAt && Date.now() > payload.expiresAt) {
      fs.unlinkSync(filePath);
      return null;
    }
    return payload.value === undefined ? null : payload.value;
  } catch (error) {
    await log("warn", `读取运行时缓存失败: key=${key}, error=${error.message}`);
    return null;
  }
}

async function setCache(key, value, exSeconds = 0) {
  if (!RUNTIME_CACHE_DIR || !key) return false;
  try {
    fs.mkdirSync(RUNTIME_CACHE_DIR, { recursive: true });
    const filePath = getRuntimeCacheFilePath(key);
    const ttl = Number(exSeconds || 0);
    const payload = {
      expiresAt: ttl > 0 ? Date.now() + ttl * 1000 : 0,
      value,
    };
    fs.writeFileSync(filePath, JSON.stringify(payload), "utf8");
    return true;
  } catch (error) {
    await log("warn", `写入运行时缓存失败: key=${key}, error=${error.message}`);
    return false;
  }
}

function getRuntimeCacheFilePath(key) {
  const digest = crypto.createHash("sha1").update(String(key)).digest("hex");
  return `${RUNTIME_CACHE_DIR}/${digest}.json`;
}

async function getDriveInfoByShareURL(shareURL) {
  const provider = driveProviderFromShareURL(shareURL);
  const parsed = await callSourceService(`/api/drive/${provider}/share/parse`, { shareURL });
  return {
    ...parsed,
    driveType: provider,
    displayName: driveDisplayName(provider),
  };
}

async function getDriveFileList(shareURL, pdirFid = "0") {
  const provider = driveProviderFromShareURL(shareURL);
  const result = await callSourceService(`/api/drive/${provider}/share/files`, { shareURL, pdirFid });
  const files = (result.files || []).map((file) => toOmniBoxDriveFile(shareURL, file, pdirFid));
  return { files, total: files.length, has_more: false, raw: result };
}

async function refreshDriveFileRef(shareURL, file) {
  if (!file || typeof file !== "object") return file || {};
  const parentFid = file.parentFid || file.pdirFid || "0";
  try {
    const listing = await getDriveFileList(shareURL, parentFid);
    const files = Array.isArray(listing.files) ? listing.files : [];
    const match = files.find((item) => {
      if (!item || item.isDir) return false;
      if (file.raw_fid && item.raw_fid && item.raw_fid === file.raw_fid) return true;
      if (file.fid && item.raw_fid && item.raw_fid === file.fid) return true;
      if (file.name && item.name === file.name && parentFid === (item.parentFid || item.pdirFid || "0")) return true;
      if (file.path && item.path === file.path) return true;
      return false;
    });
    return match || file;
  } catch {
    return file;
  }
}

async function getDriveVideoPlayInfo(shareURL, fileOrFid, flag = "", getTranscodeUrls = true) {
  const provider = driveProviderFromShareURL(shareURL);
  const decoded = typeof fileOrFid === "string" ? decodeDriveFileRef(fileOrFid) : null;
  const file = decoded || (typeof fileOrFid === "object" && fileOrFid ? fileOrFid : {});
  const freshFile = await refreshDriveFileRef(shareURL, file);
  const fid = freshFile.raw_fid || freshFile.fid || freshFile.id || file.raw_fid || file.fid || file.id || fileOrFid;
  const result = await callSourceService(`/api/drive/${provider}/share/play`, {
    shareURL,
    fid,
    flag: flag || freshFile.name || file.name || "",
    filePath: freshFile.path || file.path || "",
    shareFidToken: freshFile.shareFidToken || freshFile.share_fid_token || file.shareFidToken || file.share_fid_token || "",
    pdirFid: freshFile.parentFid || freshFile.pdirFid || file.parentFid || file.pdirFid || "0",
    getTranscodeUrls,
  });
  const play = result.play || {};
  const selected = play.selected || {};
  const candidates = (play.candidates || []).map((item) => ({
    name: item.name || "",
    url: item.url || "",
    header: item.headers || {},
  }));
  const data = {
    url: candidates,
    directUrl: selected.url || "",
    header: selected.headers || {},
    headers: selected.headers || {},
    urls: candidates,
    proxyStreaming: false,
    raw: result,
  };
  if (Array.isArray(data.urls)) {
    const sorted = preferPlayableTranscodeUrls(data.urls);
    return {
      ...data,
      url: sorted,
      urls: sorted,
    };
  }
  if (Array.isArray(data.url)) {
    return {
      ...data,
      url: preferPlayableTranscodeUrls(data.url),
    };
  }
  if (data.url) {
    return {
      ...data,
      urls: [{ name: data.name || flag || "直连", url: data.url }],
    };
  }
  return data;
}

function preferPlayableTranscodeUrls(urls) {
  const rank = (item) => {
    const name = String(item && item.name ? item.name : "").toLowerCase();
    if (name.includes("raw") || name.includes("原")) return 0;
    if (name.includes("4k")) return 1;
    if (name.includes("super")) return 2;
    if (name.includes("high")) return 3;
    if (name.includes("1080")) return 4;
    if (name.includes("low")) return 5;
    return 6;
  };
  return [...urls].sort((a, b) => rank(a) - rank(b));
}

async function getDriveFileRawUrl(shareURL, fid) {
  const data = await getDriveVideoPlayInfo(shareURL, fid, "RAW", false);
  const first = Array.isArray(data.url) ? data.url[0] : null;
  return { url: data.directUrl || (first && first.url) || "", header: data.header || {}, raw: data.raw };
}

module.exports = {
  request,
  log,
  processScraping,
  getScrapeMetadata,
  getDanmakuByFileName,
  addPlayHistory,
  sniffVideo,
  getCache,
  setCache,
  getDriveInfoByShareURL,
  getDriveFileList,
  getDriveVideoPlayInfo,
  getDriveFileRawUrl,
};
