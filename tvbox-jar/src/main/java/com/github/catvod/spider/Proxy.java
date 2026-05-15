package com.github.catvod.spider;

import java.io.ByteArrayInputStream;
import java.lang.reflect.Method;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.Map;

public class Proxy {

    private static int port = -1;
    private static Method hostGetUrl;

    public static Object[] proxy(Map<String, String> params) {
        return Init.proxyInvoke(params);
    }

    public static String getUrl() {
        String hostUrl = hostProxyUrl();
        if (hostUrl != null && hostUrl.startsWith("http")) return trimQuery(hostUrl);
        adjustPort();
        int resolved = port > 0 ? port : 9978;
        return "http://127.0.0.1:" + resolved + "/proxy";
    }

    private static String hostProxyUrl() {
        try {
            if (hostGetUrl == null) {
                Class<?> clz = Class.forName("com.github.catvod.Proxy");
                hostGetUrl = clz.getMethod("getUrl", boolean.class);
            }
            Object value = hostGetUrl.invoke(null, true);
            return value == null ? null : String.valueOf(value);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static String trimQuery(String value) {
        int index = value.indexOf('?');
        return index >= 0 ? value.substring(0, index) : value;
    }

    private static void adjustPort() {
        if (port > 0) return;
        for (int p = 9978; p < 10000; p++) {
            if (ping(p)) {
                port = p;
                return;
            }
        }
        for (int p = 8964; p < 9978; p++) {
            if (ping(p)) {
                port = p;
                return;
            }
        }
    }

    private static boolean ping(int p) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL("http://127.0.0.1:" + p + "/proxy?do=ck").openConnection();
            connection.setConnectTimeout(120);
            connection.setReadTimeout(120);
            return connection.getResponseCode() == 200;
        } catch (Throwable ignored) {
            return false;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    public static Object[] error(String message) {
        try {
            return new Object[]{502, "text/plain; charset=utf-8", new ByteArrayInputStream((message == null ? "" : message).getBytes("UTF-8"))};
        } catch (Exception error) {
            return new Object[]{502, "text/plain; charset=utf-8", new ByteArrayInputStream(new byte[0])};
        }
    }
}
