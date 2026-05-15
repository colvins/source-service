package com.github.catvod.spider;

import android.content.Context;

import com.github.catvod.crawler.Spider;

import java.util.Map;

public class Init {

    private ClassLoader classLoader = getClass().getClassLoader();
    private Object loader;
    private Context context;

    private static class Loader {
        static volatile Init instance = new Init();
    }

    public static ClassLoader classLoader() {
        return get().classLoader;
    }

    public static Context context() {
        return get().context;
    }

    public static Init get() {
        return Loader.instance;
    }

    public static synchronized Spider getSpider(String className) {
        return (Spider) DexNative.getSpider(loader(), className);
    }

    public static void init(Context context) {
        get().context = context;
        get().loader = DexNative.getLoader(context);
    }

    public static Object loader() {
        return get().loader;
    }

    public static Object[] proxyInvoke(Map<String, String> params) {
        return DexNative.proxyInvoke(loader(), params);
    }
}
