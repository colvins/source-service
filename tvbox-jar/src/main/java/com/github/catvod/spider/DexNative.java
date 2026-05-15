package com.github.catvod.spider;

import com.github.catvod.crawler.Spider;

import java.util.Map;

public class DexNative {

    public static Object getLoader(Object context) {
        return context;
    }

    public static Object getSpider(Object loader, String className) {
        return new ColvinsRuntimeSpider(className);
    }

    public static Object[] proxyInvoke(Object loader, Object params) {
        if (!(params instanceof Map)) return ColvinsRuntimeSpider.errorProxy("proxy params missing");
        return ColvinsRuntimeSpider.proxyInvoke((Map<String, String>) params);
    }
}
