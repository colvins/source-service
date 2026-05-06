class OmniBox:
    @staticmethod
    async def request(url, options=None):
        import urllib.request

        options = options or {}
        method = options.get("method", "GET")
        headers = options.get("headers") or {}
        body = options.get("body")
        if isinstance(body, str):
            body = body.encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=30) as response:
            return {
                "statusCode": response.status,
                "body": response.read().decode("utf-8", "ignore"),
                "headers": dict(response.headers.items()),
            }

    @staticmethod
    async def log(level, message):
        print(f"[{level}] {message}")


def run(_exports):
    return None
