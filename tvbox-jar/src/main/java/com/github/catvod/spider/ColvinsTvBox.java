package com.github.catvod.spider;

import android.content.Context;

import com.colvins.tvbox.HttpBridge;
import com.colvins.tvbox.PlayBundle;
import com.colvins.tvbox.SourceConfig;
import com.github.catvod.crawler.Spider;
import com.google.gson.Gson;

import java.io.ByteArrayInputStream;
import java.net.URLEncoder;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class ColvinsTvBox extends Spider {

    private static final Gson GSON = new Gson();

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
        playCache.put(cacheKey(flag, id), bundle);
        StringBuilder urls = new StringBuilder();
        for (int i = 0; i < bundle.candidates().size(); i++) {
            PlayBundle.Candidate candidate = bundle.candidates().get(i);
            if (i > 0) urls.append(",");
            urls.append("\"").append(escape(candidate.name())).append("\",");
            urls.append("\"").append(escape(proxyUrl(flag, id, i))).append("\"");
        }
        return "{\"parse\":0,\"url\":[" + urls + "]}";
    }

    @Override
    public Object[] proxy(Map<String, String> params) throws Exception {
        try {
            String token = getOrEmpty(params, "token");
            String[] parts = HttpBridge.decodePlayToken(token);
            String flag = parts[0];
            String playId = parts[1];
            int index = Integer.parseInt(parts[2]);
            PlayBundle bundle = playCache.get(cacheKey(flag, playId));
            if (bundle == null) {
                bundle = HttpBridge.getPlayBundle(config, flag, playId);
                playCache.put(cacheKey(flag, playId), bundle);
            }
            if (index < 0 || index >= bundle.candidates().size()) {
                return errorProxy("播放候选不存在");
            }
            return HttpBridge.openProxy(bundle.candidates().get(index), params);
        } catch (Exception error) {
            return errorProxy(error.getMessage() == null ? "本地代理失败" : error.getMessage());
        }
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

    private String proxyUrl(String flag, String playId, int candidateIndex) {
        return "proxy://do=csp&siteKey=" + encode(siteKey)
            + "&token=" + encode(HttpBridge.encodePlayToken(flag, playId, candidateIndex));
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
