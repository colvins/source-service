package com.colvins.tvbox;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public final class HttpBridge {

    private static final Gson GSON = new Gson();

    public static final class HttpResponse {
        private final int statusCode;
        private final String body;

        public HttpResponse(int statusCode, String body) {
            this.statusCode = statusCode;
            this.body = body == null ? "" : body;
        }

        public int statusCode() {
            return statusCode;
        }

        public String body() {
            return body;
        }
    }

    private HttpBridge() {
    }

    public static HttpResponse getJson(String url) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) URI.create(url).toURL().openConnection();
        connection.setConnectTimeout(15000);
        connection.setReadTimeout(30000);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Accept", "application/json");
        int status = connection.getResponseCode();
        String body = readAll(status >= 400 ? connection.getErrorStream() : connection.getInputStream());
        return new HttpResponse(status, body);
    }

    public static PlayBundle getPlayBundle(SourceConfig config, String flag, String playId) throws Exception {
        String url = config.baseUrl()
            + "/api/tvbox/source/" + config.sourceId()
            + "/play?flag=" + encode(flag)
            + "&id=" + encode(playId);
        HttpResponse response = getJson(url);
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IllegalStateException("play request failed: HTTP " + response.statusCode());
        }
        JsonObject root = JsonParser.parseString(response.body()).getAsJsonObject();
        String message = root.has("message") ? safeString(root.get("message")) : safeString(root.get("msg"));
        List<PlayBundle.Candidate> candidates = new ArrayList<>();
        Map<String, String> defaultHeaders = root.has("header") && root.get("header").isJsonObject()
            ? normalizeHeaders(GSON.fromJson(root.get("header"), Map.class))
            : Collections.emptyMap();

        JsonArray items = root.has("urls") && root.get("urls").isJsonArray() ? root.getAsJsonArray("urls") : new JsonArray();
        if (items.size() == 0 && root.has("candidates") && root.get("candidates").isJsonArray()) {
            items = root.getAsJsonArray("candidates");
        }
        for (JsonElement item : items) {
            if (!item.isJsonObject()) continue;
            JsonObject object = item.getAsJsonObject();
            String name = safeString(object.get("name"));
            String candidateUrl = safeString(object.get("url"));
            String format = safeString(object.get("format"));
            if (!candidateUrl.startsWith("http://") && !candidateUrl.startsWith("https://")) continue;
            Map<String, String> headers = new LinkedHashMap<>(defaultHeaders);
            if (object.has("header") && object.get("header").isJsonObject()) {
                headers.putAll(normalizeHeaders(GSON.fromJson(object.get("header"), Map.class)));
            } else if (object.has("headers") && object.get("headers").isJsonObject()) {
                headers.putAll(normalizeHeaders(GSON.fromJson(object.get("headers"), Map.class)));
            }
            candidates.add(new PlayBundle.Candidate(isBlank(name) ? "直连" : name, candidateUrl, headers, format));
        }
        return new PlayBundle(candidates, message);
    }

    public static Object[] openProxy(PlayBundle.Candidate candidate, Map<String, String> params) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) URI.create(sanitizeUrl(candidate.url())).toURL().openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(15000);
        connection.setReadTimeout(30000);
        connection.setRequestMethod("GET");
        for (Map.Entry<String, String> header : candidate.headers().entrySet()) {
            if (header.getKey() == null || isBlank(header.getKey())) continue;
            connection.setRequestProperty(header.getKey(), header.getValue());
        }
        for (Map.Entry<String, String> header : passthroughHeaders(params).entrySet()) {
            if (header.getKey() == null || isBlank(header.getKey()) || isBlank(header.getValue())) continue;
            if (connection.getRequestProperty(header.getKey()) == null) {
                connection.setRequestProperty(header.getKey(), header.getValue());
            }
        }

        int status = connection.getResponseCode();
        String location = connection.getHeaderField("Location");
        if (isRedirectStatus(status) && !isBlank(location)) {
            Map<String, String> headers = new LinkedHashMap<>();
            headers.put("Location", sanitizeUrl(resolveUrl(candidate.url(), location)));
            return new Object[]{302, "text/plain", new ByteArrayInputStream(utf8("302 Found")), headers};
        }
        status = normalizeStatus(status);
        String mimeType = connection.getContentType();
        Map<String, String> headers = responseHeaders(connection);
        InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
        if (stream == null) {
            String body = "upstream returned empty body";
            return new Object[]{502, "text/plain; charset=utf-8", new ByteArrayInputStream(utf8(body))};
        }
        return new Object[]{status, isBlank(mimeType) ? "application/octet-stream" : mimeType, stream, headers};
    }

    public static String encodePlayToken(String flag, String playId, int candidateIndex) {
        return nullToEmpty(flag) + "\u0001" + nullToEmpty(playId) + "\u0001" + candidateIndex;
    }

    public static String[] decodePlayToken(String token) {
        String[] parts = nullToEmpty(token).split("\u0001", 3);
        if (parts.length != 3) {
            throw new IllegalArgumentException("invalid play token");
        }
        return parts;
    }

    private static Map<String, String> responseHeaders(HttpURLConnection connection) {
        Map<String, String> headers = new LinkedHashMap<>();
        for (Map.Entry<String, List<String>> entry : connection.getHeaderFields().entrySet()) {
            if (entry.getKey() == null || entry.getValue() == null || entry.getValue().isEmpty()) continue;
            headers.put(entry.getKey(), entry.getValue().get(0));
        }
        return headers;
    }

    private static Map<String, String> passthroughHeaders(Map<String, String> params) {
        Map<String, String> headers = new LinkedHashMap<>();
        copyHeader(params, headers, "range", "Range");
        copyHeader(params, headers, "accept", "Accept");
        copyHeader(params, headers, "accept-encoding", "Accept-Encoding");
        copyHeader(params, headers, "accept-language", "Accept-Language");
        copyHeader(params, headers, "connection", "Connection");
        copyHeader(params, headers, "icy-metadata", "Icy-Metadata");
        copyHeader(params, headers, "origin", "Origin");
        copyHeader(params, headers, "referer", "Referer");
        copyHeader(params, headers, "sec-fetch-dest", "Sec-Fetch-Dest");
        copyHeader(params, headers, "sec-fetch-mode", "Sec-Fetch-Mode");
        copyHeader(params, headers, "sec-fetch-site", "Sec-Fetch-Site");
        copyHeader(params, headers, "user-agent", "User-Agent");
        copyHeader(params, headers, "x-requested-with", "X-Requested-With");
        copyHeader(params, headers, "x-ulrp", "X-Ulrp");
        return headers;
    }

    private static void copyHeader(Map<String, String> source, Map<String, String> target, String sourceKey, String targetKey) {
        String value = getOrEmpty(source, sourceKey);
        if (!isBlank(value)) {
            target.put(targetKey, value);
        }
    }

    private static Map<String, String> normalizeHeaders(Map<String, String> headers) {
        Map<String, String> normalized = new LinkedHashMap<>();
        for (Map.Entry<String, String> entry : headers.entrySet()) {
            if (entry.getKey() == null || entry.getValue() == null) continue;
            normalized.put(entry.getKey(), entry.getValue());
        }
        return normalized;
    }

    private static String readAll(InputStream stream) throws Exception {
        if (stream == null) return "";
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192];
        int read;
        while ((read = stream.read(buffer)) != -1) {
            output.write(buffer, 0, read);
        }
        stream.close();
        return output.toString("UTF-8");
    }

    private static String encode(String value) {
        try {
            return URLEncoder.encode(nullToEmpty(value), "UTF-8");
        } catch (Exception error) {
            throw new IllegalStateException("failed to encode value", error);
        }
    }

    private static String safeString(JsonElement element) {
        return element == null || element.isJsonNull() ? "" : element.getAsString();
    }

    private static String getOrEmpty(Map<String, String> values, String key) {
        if (values == null || key == null) return "";
        String value = values.get(key);
        return value == null ? "" : value;
    }

    private static boolean isBlank(String value) {
        return value == null || value.trim().isEmpty();
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }

    private static String sanitizeUrl(String value) {
        return nullToEmpty(value).replace(" ", "%20");
    }

    private static String resolveUrl(String baseUrl, String location) {
        String target = sanitizeUrl(location);
        try {
            return URI.create(sanitizeUrl(baseUrl)).resolve(target).toString();
        } catch (Exception error) {
            return target;
        }
    }

    private static boolean isRedirectStatus(int status) {
        return status == 301 || status == 302 || status == 303 || status == 307 || status == 308;
    }

    private static int normalizeStatus(int status) {
        switch (status) {
            case 200:
            case 206:
            case 301:
            case 302:
            case 303:
            case 307:
            case 308:
            case 400:
            case 401:
            case 403:
            case 404:
            case 405:
            case 409:
            case 410:
            case 412:
            case 416:
            case 429:
            case 500:
            case 502:
            case 503:
            case 504:
                return status;
            default:
                return status >= 200 && status < 300 ? 200 : 502;
        }
    }

    private static byte[] utf8(String value) throws Exception {
        return nullToEmpty(value).getBytes("UTF-8");
    }
}
