package com.github.catvod.spider;

import android.content.Context;

import com.colvins.tvbox.HttpBridge;
import com.colvins.tvbox.SourceConfig;
import com.colvins.tvbox.PlayBundle;
import com.google.gson.Gson;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.ByteArrayInputStream;
import java.net.URLEncoder;
import android.util.Base64;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

import org.json.JSONObject;

public class ColvinsRuntimeSpider extends com.github.catvod.crawler.Spider {

    private final String entryClassName;
    private SourceConfig config;

    private static final Gson GSON = new Gson();
    private static final String TVBOX_BUILD_ID = "20260516-quark-kaiser-sdk25-split-1";
    // proxy() 回调仍保留，用于非网盘资源的服务端代理场景
    private static final Map<String, PlayBundle.Candidate> PROXY_CACHE = new ConcurrentHashMap<>();

    public ColvinsRuntimeSpider(String entryClassName) {
        this.entryClassName = entryClassName == null ? "" : entryClassName;
    }

    @Override
    public void init(Context context, String extend) throws Exception {
        Init.init(context);
        this.config = SourceConfig.parse(extend);
    }

    private SourceConfig config() {
        return config;
    }

    @Override
    public String action(String action) throws Exception {
        return "{\"code\":0,\"msg\":\"\"}";
    }

    @Override
    public void destroy() {
    }

    private final Map<String, PlayBundle> playCache = new LinkedHashMap<String, PlayBundle>() {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, PlayBundle> eldest) {
            return size() > 32;
        }
    };

    @Override
    public String homeContent(boolean filter) throws Exception {
        return fetchRuntime("/api/tvbox/source/" + config().sourceId() + "/home?filter=" + (filter ? "1" : "0"));
    }

    @Override
    public String homeVideoContent() throws Exception {
        String body = fetchRuntime("/api/tvbox/source/" + config().sourceId() + "/home?filter=0&homeVideo=1");
        try {
            JsonElement root = new JsonParser().parse(body == null ? "" : body);
            if (root == null || !root.isJsonObject()) return "{\"list\":[]}";
            JsonElement list = root.getAsJsonObject().get("list");
            return list == null || list.isJsonNull() ? "{\"list\":[]}" : "{\"list\":" + GSON.toJson(list) + "}";
        } catch (Exception error) {
            return "{\"list\":[]}";
        }
    }

    @Override
    public String categoryContent(String tid, String pg, boolean filter, HashMap<String, String> extend) throws Exception {
        return fetchRuntime(
            "/api/tvbox/source/" + config().sourceId() + "/category?tid=" + encode(tid) + "&pg=" + encode(pg) + "&filter=" + (filter ? "1" : "0")
        );
    }

    @Override
    public String detailContent(List<String> ids) throws Exception {
        String id = ids == null || ids.isEmpty() ? "" : ids.get(0);
        return fetchRuntime("/api/tvbox/source/" + config().sourceId() + "/detail?id=" + encode(id));
    }

    @Override
    public String searchContent(String key, boolean quick) throws Exception {
        return searchContent(key, quick, "1");
    }

    @Override
    public String searchContent(String key, boolean quick, String pg) throws Exception {
        return fetchRuntime(
            "/api/tvbox/source/" + config().sourceId() + "/search?wd=" + encode(key) + "&pg=" + encode(pg) + "&quick=" + (quick ? "1" : "0")
        );
    }

    @Override
    public String playerContent(String flag, String id, List<String> vipFlags) throws Exception {
        PlayBundle bundle = HttpBridge.getPlayBundle(config(), flag, id);
        if (bundle.candidates().isEmpty()) {
            return "{\"parse\":0,\"url\":\"\",\"msg\":\"" + escape(isBlank(bundle.message()) ? "当前线路没有可播放直连地址" : bundle.message()) + "\"}";
        }

        if (isDrivePlayId(id)) {
            PlayBundle.Candidate preferred = selectRawCandidate(bundle);
            if (preferred == null) preferred = bundle.candidates().get(0);
            String driveType = driveType(id);
            String playbackUrl;
            if ("quark".equals(driveType)) {
                playbackUrl = preferred.url();
            } else {
                String token = encodeProxyToken(config(), flag, id, bundle.candidates().indexOf(preferred));
                PROXY_CACHE.put(token, preferred);
                playbackUrl = proxyUrl(token);
            }
            String kaiserUrl = buildKaiserUrl(playbackUrl, driveType);
            String result = kaiserResult(kaiserUrl, preferred.headers(), bundle.message());
            logDrivePlayback(flag, id, driveType, playbackUrl, kaiserUrl, preferred.headers(), result);
            return result;
        }

        if (isProxyFlag(flag)) {
            // 显式要求服务端代理（"服务端代理" flag）时走 proxy://
            PlayBundle.Candidate candidate = selectRawCandidate(bundle);
            if (candidate == null) candidate = bundle.candidates().get(0);
            playCache.put(cacheKey(flag, id), new PlayBundle(java.util.Collections.singletonList(candidate), bundle.message()));
            String token = encodeProxyToken(config(), flag, id, 0);
            PROXY_CACHE.put(token, candidate);
            return "{\"parse\":0,\"url\":\"" + escape(proxyUrl(token)) + "\"}";
        }

        // 普通资源直连
        PlayBundle.Candidate candidate = selectTranscodeCandidate(bundle);
        if (candidate == null) candidate = bundle.candidates().get(0);
        return directResult(candidate, bundle.message());
    }

    @Override
    public Object[] proxy(Map<String, String> params) throws Exception {
        return proxyInvoke(params);
    }

    // ── kaiser 本地多线程代理 ──────────────────────────────────────────────

    /**
     * 构造 TVBox kaiser 本地代理 URL。
     * kaiser 和 proxy 共享同一个本地服务端口，端口由宿主动态分配。
     */
    private String buildKaiserUrl(String rawUrl, String driveType) {
        String kaiserBase = resolveKaiserBaseUrl();
        return kaiserBase
            + "?url=" + encode(rawUrl)
            + "&thread=16"
            + "&chunk=512"
            + "&key=" + driveType
            + "&type=" + driveType;
    }

    private String resolveKaiserBaseUrl() {
        if (isPortOpen("127.0.0.1", 8096, 150)) {
            return "http://127.0.0.1:8096/kaiser";
        }
        String hostBase = resolveHostKaiserBase();
        if (!isBlank(hostBase)) return hostBase;
        return "http://127.0.0.1:8096/kaiser";
    }

    private String resolveHostKaiserBase() {
        try {
            String proxyBase = Proxy.getUrl();
            int proxyIndex = proxyBase.indexOf("/proxy");
            if (proxyIndex >= 0) {
                return proxyBase.substring(0, proxyIndex) + "/kaiser";
            }
            return proxyBase + "/kaiser";
        } catch (Throwable error) {
            return "";
        }
    }

    private int sdkInt() {
        try {
            Class<?> versionClass = Class.forName("android.os.Build$VERSION");
            return versionClass.getField("SDK_INT").getInt(null);
        } catch (Throwable error) {
            return 0;
        }
    }

    private boolean isPortOpen(String host, int port, int timeoutMs) {
        java.net.Socket socket = null;
        try {
            socket = new java.net.Socket();
            socket.connect(new java.net.InetSocketAddress(host, port), timeoutMs);
            return true;
        } catch (Exception error) {
            return false;
        } finally {
            if (socket != null) {
                try {
                    socket.close();
                } catch (Exception ignored) {
                }
            }
        }
    }


    private String quarkProxyUrl(String url, Map<String, String> headers) throws Exception {
        String encodedUrl = Base64.encodeToString(nullSafe(url).getBytes("UTF-8"), Base64.NO_WRAP);
        String encodedHeader = Base64.encodeToString(serializeHeaderJson(headers).getBytes("UTF-8"), Base64.NO_WRAP);
        return "proxy://do=quark&type=video&url=" + encode(encodedUrl) + "&header=" + encode(encodedHeader);
    }

    /** 从 play_id 推断网盘类型（quark / baidu） */
    private String driveType(String id) {
        if (id != null && id.contains("pan.baidu.com/")) return "baidu";
        return "quark";
    }

    /** 构造 kaiser 播放结果 JSON，带 header */
    private String kaiserResult(String kaiserUrl, Map<String, String> headers, String message) {
        StringBuilder builder = new StringBuilder();
        builder.append("{\"parse\":0");
        builder.append(",\"url\":\"").append(escape(kaiserUrl)).append("\"");
        if (!headers.isEmpty()) {
            builder.append(",\"header\":").append(serializeHeaderJson(headers));
        }
        if (!isBlank(message)) {
            builder.append(",\"msg\":\"").append(escape(message)).append("\"");
        }
        builder.append("}");
        return builder.toString();
    }

    private void logDrivePlayback(String flag, String id, String driveType, String playbackUrl, String kaiserUrl, Map<String, String> headers, String result) {
        try {
            StringBuilder builder = new StringBuilder();
            builder.append("sdk=").append(sdkInt());
            builder.append(" driveType=").append(driveType);
            builder.append(" flag=").append(nullSafe(flag));
            builder.append(" idHash=").append(Integer.toHexString(nullSafe(id).hashCode()));
            builder.append(" port8096=").append(isPortOpen("127.0.0.1", 8096, 100));
            builder.append(" hostKaiser=").append(resolveHostKaiserBase());
            builder.append(" playbackUrl=").append(playbackUrl);
            builder.append(" kaiserUrl=").append(kaiserUrl);
            builder.append(" headers=").append(serializeHeaderJson(headers));
            builder.append(" result=").append(result);
            System.out.println("ColvinsTV " + builder.toString());
        } catch (Throwable ignored) {
        }
    }

    private String simpleUrlResult(String url, String message) {
        StringBuilder builder = new StringBuilder();
        builder.append("{\"parse\":0");
        builder.append(",\"url\":\"").append(escape(url)).append("\"");
        if (!isBlank(message)) {
            builder.append(",\"msg\":\"").append(escape(message)).append("\"");
        }
        builder.append("}");
        return builder.toString();
    }

    // ── candidate 选择 ────────────────────────────────────────────────────

    /** 选 raw candidate（原始下载链接，适合 kaiser 多线程） */
    private PlayBundle.Candidate selectRawCandidate(PlayBundle bundle) {
        for (PlayBundle.Candidate c : bundle.candidates()) {
            if ("raw".equalsIgnoreCase(c.name())) return c;
        }
        return null;
    }

    /** 选转码 candidate，优先级 4k > super > high > low */
    private PlayBundle.Candidate selectTranscodeCandidate(PlayBundle bundle) {
        String[] preferred = {"high", "low", "super", "4k"};
        for (String name : preferred) {
            for (PlayBundle.Candidate c : bundle.candidates()) {
                if (name.equalsIgnoreCase(c.name())) return c;
            }
        }
        return null;
    }

    // ── 工具方法 ──────────────────────────────────────────────────────────

    private String fetchRuntime(String path) throws Exception {
        String separator = path.contains("?") ? "&" : "?";
        HttpBridge.HttpResponse response = HttpBridge.getJson(config().baseUrl() + path + separator + "jarBuild=" + TVBOX_BUILD_ID + "&entry=" + encode(entryClassName));
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            return "{\"list\":[],\"msg\":\"请求失败: HTTP " + response.statusCode() + "\"}";
        }
        return unwrapEnvelope(response.body());
    }

    private String unwrapEnvelope(String body) {
        try {
            JsonElement root = new JsonParser().parse(body == null ? "" : body);
            if (root == null || !root.isJsonObject()) return body;
            JsonObject object = root.getAsJsonObject();
            JsonElement data = object.get("data");
            return data == null || data.isJsonNull() ? body : GSON.toJson(data);
        } catch (Exception error) {
            return body;
        }
    }

    private String proxyUrl(String token) {
        String resolvedSiteKey = isBlank(siteKey) ? ("source_" + config().sourceId() + "_20260515b") : siteKey;
        return "proxy://do=csp&siteKey=" + encode(resolvedSiteKey)
            + "&token=" + encode(token);
    }

    private boolean isProxyFlag(String flag) {
        String text = flag == null ? "" : flag;
        return text.contains("服务端代理");
    }

    private boolean isDrivePlayId(String value) {
        String text = value == null ? "" : value;
        return text.contains("|colvins:") && (text.contains("pan.quark.cn/") || text.contains("pan.baidu.com/"));
    }

    private String serializeHeaderJson(Map<String, String> headers) {
        StringBuilder builder = new StringBuilder();
        builder.append("{");
        boolean first = true;
        if (headers != null) {
            for (Map.Entry<String, String> entry : headers.entrySet()) {
                if (entry.getKey() == null || entry.getValue() == null) continue;
                if (!first) builder.append(",");
                builder.append("\"").append(escape(entry.getKey())).append("\":\"").append(escape(entry.getValue())).append("\"");
                first = false;
            }
        }
        builder.append("}");
        return builder.toString();
    }

    private String directResult(PlayBundle.Candidate candidate, String message) {
        StringBuilder builder = new StringBuilder();
        builder.append("{\"parse\":0");
        builder.append(",\"url\":\"").append(escape(candidate.url())).append("\"");
        if (!isBlank(candidate.format())) {
            builder.append(",\"format\":\"").append(escape(candidate.format())).append("\"");
        }
        if (!candidate.headers().isEmpty()) {
            builder.append(",\"header\":").append(serializeHeaderJson(candidate.headers()));
        }
        if (!isBlank(message)) {
            builder.append(",\"msg\":\"").append(escape(message)).append("\"");
        }
        builder.append("}");
        return builder.toString();
    }

    public static Object[] proxyInvoke(Map<String, String> params) {
        try {
            String action = getOrEmptyStatic(params, "do");
            String type = getOrEmptyStatic(params, "type");
            if ("ck".equals(action)) {
                return new Object[]{200, "text/plain; charset=utf-8", new ByteArrayInputStream("ok".getBytes("UTF-8"))};
            }
            if ("quark".equals(action) && "video".equals(type)) {
                String url = new String(Base64.decode(getOrEmptyStatic(params, "url"), Base64.DEFAULT), "UTF-8");
                Map<String, String> headers = jsonObjectToHeaderMap(new JSONObject(new String(Base64.decode(getOrEmptyStatic(params, "header"), Base64.DEFAULT), "UTF-8")));
                String[] passthrough = {"Range", "Accept", "Accept-Encoding", "Accept-Language", "Cookie", "Origin", "Referer", "Sec-Ch-Ua", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua-Platform", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site", "User-Agent"};
                for (Map.Entry<String, String> entry : params.entrySet()) {
                    for (String key : passthrough) {
                        if (key.equalsIgnoreCase(entry.getKey()) && entry.getValue() != null && entry.getValue().length() > 0) headers.put(key, entry.getValue());
                    }
                }
                return HttpBridge.openProxy(new PlayBundle.Candidate("quark", url, normalizeHeaderMap(headers), ""), params);
            }
            String token = getOrEmptyStatic(params, "token");
            PlayBundle.Candidate candidate = PROXY_CACHE.get(token);
            if (candidate == null) {
                String[] parts = decodeProxyToken(token);
                SourceConfig config = SourceConfig.parse("baseUrl=" + encodeStatic(parts[0]) + "&sourceId=" + encodeStatic(parts[1]) + "&sourceName=Colvins");
                PlayBundle bundle = HttpBridge.getPlayBundle(config, parts[2], parts[3]);
                int index = Integer.parseInt(parts[4]);
                if (index < 0 || index >= bundle.candidates().size()) return errorProxyStatic("播放候选不存在");
                candidate = bundle.candidates().get(index);
                PROXY_CACHE.put(token, candidate);
            }
            return HttpBridge.openProxy(candidate, params);
        } catch (Exception error) {
            return errorProxyStatic(error.getMessage() == null ? "本地代理失败" : error.getMessage());
        }
    }

    public static String encodeProxyToken(SourceConfig config, String flag, String playId, int candidateIndex) {
        return config.baseUrl() + "\u0001" + config.sourceId() + "\u0001" + nullSafe(flag) + "\u0001" + nullSafe(playId) + "\u0001" + candidateIndex;
    }

    private static String[] decodeProxyToken(String token) {
        String[] parts = nullSafe(token).split("\u0001", 5);
        if (parts.length != 5) throw new IllegalArgumentException("invalid proxy token");
        return parts;
    }

    private static Map<String, String> normalizeHeaderMap(Map<String, String> headers) {
        Map<String, String> normalized = new HashMap<>();
        if (headers == null) return normalized;
        for (Map.Entry<String, String> entry : headers.entrySet()) {
            if (entry.getKey() == null || entry.getValue() == null) continue;
            normalized.put(entry.getKey(), entry.getValue());
        }
        return normalized;
    }

    private static Map<String, String> jsonObjectToHeaderMap(JSONObject object) {
        Map<String, String> headers = new HashMap<>();
        if (object == null) return headers;
        putJsonHeader(headers, object, "User-Agent");
        putJsonHeader(headers, object, "Referer");
        putJsonHeader(headers, object, "Cookie");
        putJsonHeader(headers, object, "Origin");
        putJsonHeader(headers, object, "Accept");
        putJsonHeader(headers, object, "Accept-Language");
        putJsonHeader(headers, object, "Accept-Encoding");
        putJsonHeader(headers, object, "Content-Type");
        putJsonHeader(headers, object, "Range");
        return headers;
    }

    private static void putJsonHeader(Map<String, String> headers, JSONObject object, String key) {
        if (headers == null || object == null || key == null) return;
        String value = object.optString(key, "");
        if (value == null || value.trim().isEmpty()) return;
        headers.put(key, value);
    }

    private static Object[] errorProxyStatic(String message) {
        try {
            return new Object[]{502, "text/plain; charset=utf-8", new ByteArrayInputStream((message == null ? "" : message).getBytes("UTF-8"))};
        } catch (Exception error) {
            return new Object[]{502, "text/plain; charset=utf-8", new ByteArrayInputStream(new byte[0])};
        }
    }

    private static String getOrEmptyStatic(Map<String, String> values, String key) {
        if (values == null || key == null) return "";
        String value = values.get(key);
        return value == null ? "" : value;
    }

    private static String encodeStatic(String value) {
        try {
            return URLEncoder.encode(value == null ? "" : value, "UTF-8");
        } catch (Exception error) {
            throw new IllegalStateException("failed to encode value", error);
        }
    }

    private static String nullSafe(String value) {
        return value == null ? "" : value;
    }

    private String cacheKey(String flag, String id) {
        return flag + "\n" + id;
    }

    private String encode(String value) {
        try {
            return URLEncoder.encode(value == null ? "" : value, "UTF-8");
        } catch (Exception error) {
            throw new IllegalStateException("failed to encode value", error);
        }
    }

    private String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private boolean isBlank(String value) {
        return value == null || value.trim().isEmpty();
    }

    public static Object[] errorProxy(String message) {
        return errorProxyStatic(message);
    }
}
