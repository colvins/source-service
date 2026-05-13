package com.github.catvod.spider;

import java.util.Map;

public class Proxy {

    public static Object[] proxy(Map<String, String> params) {
        return ColvinsTvBox.proxyFromParams(params);
    }
}
