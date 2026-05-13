package com.github.catvod.spider;

import android.content.Context;

import com.colvins.tvbox.HttpBridge;
import com.colvins.tvbox.PlayBundle;
import com.colvins.tvbox.SourceConfig;
import com.github.catvod.crawler.Spider;
import com.google.gson.Gson;

import java.io.ByteArrayInputStream;
import java.net.URLEncoder;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class ColvinsTvBox extends Spider {

    private static final Gson GSON = new Gson();
    private static final Map<String, PlayBundle.Candidate> PROXY_CACHE = new ConcurrentHashMap<>();

    private SourceConfig config;
    private final Map<String, PlayBundle> playCache = new LinkedHashMap<String, PlayBundle>() {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, PlayBundle> eldest) {
            return size() > 32;
        }
    };

    @Override
    public void init(Context context, String extend) {
        this.config = SourceConfig.parse(extend);
    }

    @Override
    public String homeContent(boolean filter) throws Exception {
        return fetchRuntime("/api/tvbox/source/" + config.sourceId() + "/home?filter=" + (filter ? "1" : "0"));
    }

    @Override
    public String categoryContent(String tid, String pg, boolean filter, HashMap<String, String> extend) throws Exception {
        return fetchRuntime(
            "/api/tvbox/source/" + config.sourceId() + "/category?tid=" + encode(tid) + "&pg=" + encode(pg) + "&filter=" + (filter ? "1" : "0")
        );
    }

    @Override
    public String detailContent(List<String> ids) throws Exception {
        String id = ids == null || ids.isEmpty() ? "" : ids.get(0);
        return fetchRuntime("/api/tvbox/source/" + config.sourceId() + "/detail?id=" + encode(id));
    }

    @Override
    public String searchContent(String key, boolean quick) throws Exception {
        return searchContent(key, quick, "1");
    }

    @Override
    public String searchContent(String key, boolean quick, String pg) throws Exception {
        return fetchRuntime(
            "/api/tvbox/source/" + config.sourceId() + "/search?wd=" + encode(key) + "&pg=" + encode(pg) + "&quick=" + (quick ? "1" : "0")
        );
    }

    @Override
    public String playerContent(String flag, String id, List<String> vipFlags) throws Exception {
        PlayBundle bundle = HttpBridge.getPlayBundle(config, flag, id);
        if (bundle.candidates().isEmpty()) {
            return "{\"parse\":0,\"url\":\"\",\"msg\":\"" + escape(isBlank(bundle.message()) ? "当前线路没有可播放直连地址" : bundle.message()) + "\"}";
        }
        PlayBundle.Candidate candidate = selectCandidate(flag, bundle);
        PlayBundle selected = new PlayBundle(Collections.singletonList(candidate), bundle.message());
        playCache.put(cacheKey(flag, id), selected);
        if (isProxyFlag(flag)) {
            String token = encodeProxyToken(config, flag, id, 0);
            PROXY_CACHE.put(token, candidate);
            return "{\"parse\":0,\"url\":\"" + escape(proxyUrl(token)) + "\"}";
        }
        return directResult(candidate, bundle.message());
    }

    @Override
    public Object[] proxy(Map<String, String> params) throws Exception {
        return proxyFromParams(params);
    }

    private Object[] errorProxy(String message) {
        try {
            return new Object[]{502, "text/plain; charset=utf-8", new ByteArrayInputStream((message == null ? "" : message).getBytes("UTF-8"))};
        } catch (Exception error) {
            return new Object[]{502, "text/plain; charset=utf-8", new ByteArrayInputStream(new byte[0])};
        }
    }

    private String fetchRuntime(String path) throws Exception {
        HttpBridge.HttpResponse response = HttpBridge.getJson(config.baseUrl() + path);
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            return "{\"list\":[],\"msg\":\"请求失败: HTTP " + response.statusCode() + "\"}";
        }
        return unwrapEnvelope(response.body());
    }

    private String unwrapEnvelope(String body) {
        Map<?, ?> parsed = GSON.fromJson(body, Map.class);
        Object data = parsed == null ? null : parsed.get("data");
        return data == null ? body : GSON.toJson(data);
    }

    private String proxyUrl(String token) {
        String resolvedSiteKey = "csp_source_" + config.sourceId();
        return "proxy://do=csp&siteKey=" + encode(resolvedSiteKey)
            + "&token=" + encode(token);
    }

    private PlayBundle.Candidate selectCandidate(String flag, PlayBundle bundle) {
        if (bundle.candidates().isEmpty()) throw new IllegalStateException("播放候选不存在");
        PlayBundle.Candidate raw = null;
        for (PlayBundle.Candidate candidate : bundle.candidates()) {
            if ("raw".equalsIgnoreCase(candidate.name())) {
                raw = candidate;
                break;
            }
        }
        if (raw != null && (isProxyFlag(flag) || isDirectFlag(flag))) {
            return raw;
        }
        return raw != null ? raw : bundle.candidates().get(0);
    }

    private boolean isProxyFlag(String flag) {
        String text = flag == null ? "" : flag;
        return text.contains("本地代理") || text.contains("服务端代理");
    }

    private boolean isDirectFlag(String flag) {
        String text = flag == null ? "" : flag;
        return text.contains("直连");
    }

    private String directResult(PlayBundle.Candidate candidate, String message) {
        StringBuilder builder = new StringBuilder();
        builder.append("{\"parse\":0");
        builder.append(",\"url\":\"").append(escape(candidate.url())).append("\"");
        if (!isBlank(candidate.format())) {
            builder.append(",\"format\":\"").append(escape(candidate.format())).append("\"");
        }
        if (!candidate.headers().isEmpty()) {
            builder.append(",\"header\":").append(GSON.toJson(candidate.headers()));
        }
        if (!isBlank(message)) {
            builder.append(",\"msg\":\"").append(escape(message)).append("\"");
        }
        builder.append("}");
        return builder.toString();
    }

    public static Object[] proxyFromParams(Map<String, String> params) {
        try {
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

    private String getOrEmpty(Map<String, String> values, String key) {
        if (values == null || key == null) return "";
        String value = values.get(key);
        return value == null ? "" : value;
    }

    private boolean isBlank(String value) {
        return value == null || value.trim().isEmpty();
    }
}
