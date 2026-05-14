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
            + "/play-options?flag=" + encode(flag)
            + "&id=" + encode(playId);
        HttpResponse response = getJson(url);
        if (response.statusCode() < 200 || response.statusCode() >= 300) {
            throw new IllegalStateException("play-options request failed: HTTP " + response.statusCode());
        }
        JsonObject root = JsonParser.parseString(response.body()).getAsJsonObject();
        String message = root.has("message") ? safeString(root.get("message")) : "";
        List<PlayBundle.Candidate> candidates = new ArrayList<>();
        JsonArray items = root.has("candidates") && root.get("candidates").isJsonArray() ? root.getAsJsonArray("candidates") : new JsonArray();
        for (JsonElement item : items) {
            if (!item.isJsonObject()) continue;
            JsonObject object = item.getAsJsonObject();
            String name = safeString(object.get("name"));
            String candidateUrl = safeString(object.get("url"));
            String format = safeString(object.get("format"));
            if (!candidateUrl.startsWith("http://") && !candidateUrl.startsWith("https://")) continue;
            Map<String, String> headers = object.has("headers") && object.get("headers").isJsonObject()
                ? GSON.fromJson(object.get("headers"), Map.class)
                : Collections.emptyMap();
            candidates.add(new PlayBundle.Candidate(isBlank(name) ? "直连" : name, candidateUrl, normalizeHeaders(headers), format));
        }
        return new PlayBundle(candidates, message);
    }

    public static Object[] openProxy(PlayBundle.Candidate candidate, Map<String, String> params) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) URI.create(sanitizeUrl(candidate.url())).toURL().openConnection();
        connection.setInstanceFollowRedirects(true);
        connection.setConnectTimeout(15000);
        connection.setReadTimeout(30000);
        connection.setRequestMethod("GET");
        for (Map.Entry<String, String> header : candidate.headers().entrySet()) {
            if (header.getKey() == null || isBlank(header.getKey())) continue;
            connection.setRequestProperty(header.getKey(), header.getValue());
        }
        String range = getHeaderIgnoreCase(params, "Range");
        if (!isBlank(range)) {
            connection.setRequestProperty("Range", range);
        }

        int status = connection.getResponseCode();
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

    private static String getHeaderIgnoreCase(Map<String, String> values, String key) {
        if (values == null || key == null) return "";
        String direct = values.get(key);
        if (direct != null) return direct;
        for (Map.Entry<String, String> entry : values.entrySet()) {
            if (entry.getKey() != null && key.equalsIgnoreCase(entry.getKey())) {
                return entry.getValue() == null ? "" : entry.getValue();
            }
        }
        return "";
    }

    private static String sanitizeUrl(String value) {
        String text = nullToEmpty(value);
        StringBuilder builder = new StringBuilder(text.length());
        for (int index = 0; index < text.length(); ) {
            int codePoint = text.codePointAt(index);
            index += Character.charCount(codePoint);
            if (codePoint <= 0x20 || codePoint >= 0x7f) {
                appendEncodedCodePoint(builder, codePoint);
            } else {
                builder.appendCodePoint(codePoint);
            }
        }
        return builder.toString();
    }

    private static void appendEncodedCodePoint(StringBuilder builder, int codePoint) {
        try {
            byte[] bytes = new String(Character.toChars(codePoint)).getBytes("UTF-8");
            char[] hex = "0123456789ABCDEF".toCharArray();
            for (byte value : bytes) {
                int unsigned = value & 0xff;
                builder.append('%');
                builder.append(hex[unsigned >> 4]);
                builder.append(hex[unsigned & 0x0f]);
            }
        } catch (Exception error) {
            builder.appendCodePoint(codePoint);
        }
    }

    private static boolean isBlank(String value) {
        return value == null || value.trim().isEmpty();
    }

    private static String nullToEmpty(String value) {
        return value == null ? "" : value;
    }

    private static byte[] utf8(String value) throws Exception {
        return nullToEmpty(value).getBytes("UTF-8");
    }
}
