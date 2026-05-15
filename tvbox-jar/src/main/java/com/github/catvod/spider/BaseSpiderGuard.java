package com.github.catvod.spider;

import android.content.Context;

import com.github.catvod.crawler.Spider;

import java.util.HashMap;
import java.util.List;

public class BaseSpiderGuard extends Spider {

    private Spider delegate = Init.getSpider(getClass().getName());

    @Override
    public String action(String action) throws Exception {
        return delegate.action(action);
    }

    @Override
    public String categoryContent(String tid, String pg, boolean filter, HashMap<String, String> extend) throws Exception {
        return delegate.categoryContent(tid, pg, filter, extend);
    }

    @Override
    public void destroy() {
        delegate.destroy();
    }

    @Override
    public String detailContent(List<String> ids) throws Exception {
        return delegate.detailContent(ids);
    }

    @Override
    public String homeContent(boolean filter) throws Exception {
        return delegate.homeContent(filter);
    }

    @Override
    public String homeVideoContent() throws Exception {
        return delegate.homeVideoContent();
    }

    @Override
    public void init(Context context, String extend) throws Exception {
        delegate.siteKey = siteKey;
        delegate.init(context, extend);
    }

    @Override
    public boolean isVideoFormat(String url) throws Exception {
        return delegate.isVideoFormat(url);
    }

    @Override
    public boolean manualVideoCheck() throws Exception {
        return delegate.manualVideoCheck();
    }

    @Override
    public String playerContent(String flag, String id, List<String> vipFlags) throws Exception {
        return delegate.playerContent(flag, id, vipFlags);
    }

    @Override
    public Object[] proxy(java.util.Map<String, String> params) throws Exception {
        return delegate.proxy(params);
    }

    @Override
    public String searchContent(String key, boolean quick) throws Exception {
        return delegate.searchContent(key, quick);
    }

    @Override
    public String searchContent(String key, boolean quick, String pg) throws Exception {
        return delegate.searchContent(key, quick, pg);
    }
}
