"""Measure ten sequential resource downloads using the standard library."""

import argparse
import http.client
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DOWNLOAD_COUNT = 10
BLOCK_SIZE = 64 * 1024
TIMEOUT = 30
MIB = 1024 * 1024


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Измерить скорость 10 загрузок ресурса.")
    parser.add_argument("url", help="URL ресурса (HTTP или HTTPS)")
    args = parser.parse_args(argv)
    try:
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in args.url):
            raise ValueError("URL содержит пробельные или управляющие символы")
        parts = urllib.parse.urlsplit(args.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("нужен HTTP/HTTPS URL с именем хоста")
        if parts.username is not None or parts.password is not None:
            raise ValueError("авторизация в URL не поддерживается")
        if parts.port == 0 or parts.netloc.endswith(":"):
            raise ValueError("некорректный порт")
        # urllib expects an ASCII request target; encode non-ASCII URLs explicitly.
        args.url.encode("ascii")
        args.url = urllib.parse.urlunsplit(parts._replace(fragment=""))
    except (ValueError, UnicodeError) as exc:
        parser.error(f"Некорректный URL: {exc}")
    return args


def download_once(url):
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"}, method="GET")
    size = 0
    start = time.perf_counter()
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        while True:
            block = response.read(BLOCK_SIZE)
            if not block:
                break
            size += len(block)
        duration = time.perf_counter() - start
        length = response.headers.get("Content-Length", "").strip()
        if (not response.chunked and response.status not in (204, 304)
                and re.fullmatch(r"[0-9]+", length) and size != int(length)):
            raise OSError(f"неполный ответ: прочитано {size} из {length} байт")
    return duration, size


def calculate_metrics(results):
    if len(results) != DOWNLOAD_COUNT:
        raise ValueError(f"требуется {DOWNLOAD_COUNT} успешных загрузок")
    total_time = sum(duration for duration, _ in results)
    total_bytes = sum(size for _, size in results)
    if total_time <= 0:
        raise ValueError("суммарное время измерения должно быть положительным")
    return total_time / DOWNLOAD_COUNT, total_bytes, total_bytes / total_time / MIB


def main(argv=None):
    args = parse_args(argv)
    results = []
    number = 1
    try:
        for number in range(1, DOWNLOAD_COUNT + 1):
            results.append(download_once(args.url))
    except KeyboardInterrupt:
        print(f"Загрузка {number}/10: прервано пользователем. Замер не завершён.", file=sys.stderr)
        return 130
    except urllib.error.HTTPError as exc:
        exc.close()
        print(f"Загрузка {number}/10: HTTP {exc.code}. Замер не завершён.", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else str(exc)
        print(f"Загрузка {number}/10: {reason}. Замер не завершён.", file=sys.stderr)
        return 1
    try:
        average, size, speed = calculate_metrics(results)
    except ValueError as exc:
        print(f"Ошибка измерения: {exc}. Замер не завершён.", file=sys.stderr)
        return 1
    print(f"Успешных загрузок: {DOWNLOAD_COUNT}")
    print(f"Среднее время запроса: {average:.3f} с")
    print(f"Общий объём: {size} байт")
    print(f"Средняя скорость: {speed:.3f} MiB/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
