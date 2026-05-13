package com.colvins.tvbox;

import java.util.Collections;
import java.util.List;
import java.util.Map;

public final class PlayBundle {

    public static final class Candidate {
        private final String name;
        private final String url;
        private final Map<String, String> headers;
        private final String format;

        public Candidate(String name, String url, Map<String, String> headers, String format) {
            this.name = name;
            this.url = url;
            this.headers = headers == null ? Collections.emptyMap() : headers;
            this.format = format == null ? "" : format;
        }

        public String name() {
            return name;
        }

        public String url() {
            return url;
        }

        public Map<String, String> headers() {
            return headers;
        }

        public String format() {
            return format;
        }
    }

    private final List<Candidate> candidates;
    private final String message;

    public PlayBundle(List<Candidate> candidates, String message) {
        this.candidates = candidates == null ? Collections.emptyList() : candidates;
        this.message = message == null ? "" : message;
    }

    public List<Candidate> candidates() {
        return candidates;
    }

    public String message() {
        return message;
    }
}
