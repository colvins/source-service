const CONTEXT = JSON.parse(process.env.COLVINS_CONTEXT || "{}");
const API_BASE_URL = process.env.COLVINS_SOURCE_API_URL || CONTEXT.baseURL || process.env.OMNIBOX_API_URL || "";

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

function toOmniBoxDriveFile(file) {
  const isDir = Boolean(file && file.isDir);
  const ref = {
    fid: file.fid || "",
    name: file.name || "",
    path: file.path || "",
    shareFidToken: file.shareFidToken || "",
    parentFid: file.parentFid || "0",
  };
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
    pdir_fid: file.parentFid || "0",
    path: file.path || "",
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
  return null;
}

async function setCache() {
  return false;
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
  const files = (result.files || []).map(toOmniBoxDriveFile);
  return { files, total: files.length, has_more: false, raw: result };
}

async function getDriveVideoPlayInfo(shareURL, fileOrFid, flag = "", getTranscodeUrls = true) {
  const provider = driveProviderFromShareURL(shareURL);
  const decoded = typeof fileOrFid === "string" ? decodeDriveFileRef(fileOrFid) : null;
  const file = decoded || (typeof fileOrFid === "object" && fileOrFid ? fileOrFid : {});
  const fid = file.raw_fid || file.fid || file.id || fileOrFid;
  const result = await callSourceService(`/api/drive/${provider}/share/play`, {
    shareURL,
    fid,
    flag: flag || file.name || "",
    filePath: file.path || "",
    shareFidToken: file.shareFidToken || file.share_fid_token || "",
    pdirFid: file.parentFid || file.pdirFid || "0",
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
    return {
      ...data,
      urls: preferPlayableTranscodeUrls(data.urls),
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
