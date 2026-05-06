const API_BASE_URL = process.env.OMNIBOX_API_URL || "";

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
  const result = await callAPI("/drive/info", { shareURL });
  return result.data || {};
}

async function getDriveFileList(shareURL, pdirFid = "0") {
  const result = await callAPI("/drive/file-list", { shareURL, pdirFid });
  return result.data || { files: [], total: 0, has_more: false };
}

async function getDriveVideoPlayInfo(shareURL, fid, flag = "", getTranscodeUrls = true) {
  const result = await callAPI("/drive/video-play-info", { shareURL, fid, flag, getTranscodeUrls });
  const data = result.data || { url: "", header: {}, danmaku: [] };
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
    if (name.includes("4k")) return 0;
    if (name.includes("super")) return 1;
    if (name.includes("high")) return 2;
    if (name.includes("1080")) return 3;
    if (name.includes("low")) return 4;
    if (name.includes("raw") || name.includes("原")) return 9;
    return 5;
  };
  return [...urls].sort((a, b) => rank(a) - rank(b));
}

async function getDriveFileRawUrl(shareURL, fid) {
  const result = await callAPI("/drive/file-raw-url", { shareURL, fid });
  return result.data || { url: "" };
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
