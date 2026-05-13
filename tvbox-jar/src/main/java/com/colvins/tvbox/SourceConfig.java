package com.colvins.tvbox;

import java.net.URLDecoder;
import java.util.LinkedHashMap;
import java.util.Map;

public final class SourceConfig {

    private final String baseUrl;
    private final int sourceId;
    private final String sourceName;

    private SourceConfig(String baseUrl, int sourceId, String sourceName) {
        this.baseUrl = baseUrl;
        this.sourceId = sourceId;
        this.sourceName = sourceName;
    }

    public static SourceConfig parse(String ext) {
        Map<String, String> values = new LinkedHashMap<>();
        for (String pair : String.valueOf(ext == null ? "" : ext).split("&")) {
            if (pair == null || pair.trim().isEmpty()) continue;
            int index = pair.indexOf('=');
            if (index < 0) continue;
            String key = decode(pair.substring(0, index));
            String value = decode(pair.substring(index + 1));
            values.put(key, value);
        }
        String baseUrl = trimTrailingSlash(getOrEmpty(values, "baseUrl"));
        int sourceId = Integer.parseInt(getOrEmpty(values, "sourceId").isEmpty() ? "0" : getOrEmpty(values, "sourceId"));
        String sourceName = getOrEmpty(values, "sourceName").isEmpty() ? "Colvins Source" : getOrEmpty(values, "sourceName");
        if (baseUrl.isEmpty() || sourceId <= 0) {
            throw new IllegalArgumentException("invalid tvbox ext config");
        }
        return new SourceConfig(baseUrl, sourceId, sourceName);
    }

    private static String decode(String value) {
        try {
            return URLDecoder.decode(value == null ? "" : value, "UTF-8");
        } catch (Exception error) {
            throw new IllegalStateException("invalid ext encoding", error);
        }
    }

    private static String trimTrailingSlash(String value) {
        if (value == null) return "";
        return value.endsWith("/") ? value.substring(0, value.length() - 1) : value;
    }

    public String baseUrl() {
        return baseUrl;
    }

    public int sourceId() {
        return sourceId;
    }

    public String sourceName() {
        return sourceName;
    }

    private static String getOrEmpty(Map<String, String> values, String key) {
        if (values == null || key == null) return "";
        String value = values.get(key);
        return value == null ? "" : value;
    }
}
