import asyncio
import base64
import importlib.util
import ipaddress
import json
import logging
import random
import re
import socket
import subprocess
import time
from pathlib import Path

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_SOURCES = (
    "https://raw.githubusercontent.com/barry-far/V2ray-Config/main/Sub1.txt",
    "https://raw.githubusercontent.com/peasoft/NoMoreWalls/master/list_raw.txt",
    "https://raw.githubusercontent.com/Surfboardv2ray/TGParse/main/splitted/mixed",
    "https://raw.githubusercontent.com/MrPooyaX/SansorchiFucker/main/data.txt",
    "https://raw.githubusercontent.com/mahdibland/V2RayAggregator/master/sub/sub_merge.txt",
    "https://raw.githubusercontent.com/ALIILAPRO/v2rayNG-Config/main/server.txt",
    "https://raw.githubusercontent.com/MhdiTaheri/V2rayCollector/main/sub/mix",
)

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
VENDOR = PROJECT / "vpn" / "sub"
LOCAL_FILE = VENDOR / "sub_plain.txt"
POOL_FILE = ROOT / "data" / "keys_pool.json"
GEO_CACHE = ROOT / "data" / "geo_cache.json"

LINK_RE = re.compile(r"(?:vless|vmess|trojan|ss)://[^\s\"'<>`]+")
CACHE_TTL = 1800
PUBLIC_SAMPLE = 16
TEST_WORKERS = 6
MAX_SERVERS = 5
_PORT = iter(range(24000, 30000))


def _load_vendor():
    spec = importlib.util.spec_from_file_location(
        "update_sub", VENDOR / "update_sub.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    vendor = _load_vendor()
    vendor.MAX_LATENCY = max(vendor.MAX_LATENCY, 2.5)
except Exception as exc:
    log.warning("Не удалось загрузить проверялку конфигов: %s", exc)
    vendor = None


def _decode(raw: bytes) -> str:
    text = raw.decode("utf-8", "ignore").strip()
    if "://" in text:
        return text
    try:
        decoded = base64.b64decode(text + "===").decode("utf-8", "ignore")
    except Exception:
        return text
    return decoded if "://" in decoded else text


def parse_links(text: str) -> list[str]:
    links = []
    for link in LINK_RE.findall(text):
        link = link.rstrip(".,;)\"'")
        if link not in links:
            links.append(link)
    return links


def link_name(link: str) -> str:
    name = ""
    if "#" in link:
        import urllib.parse

        name = urllib.parse.unquote(link.split("#", 1)[1]).strip()
    name = name.split("&", 1)[0]
    name = name.split("---", 1)[0]
    name = re.sub(r"^Telegram-*", "", name).strip(" -_")
    name = "".join(ch for ch in name if ch.isalnum() or ch in " -_.")
    name = re.sub(r"\s+", " ", name).strip()
    return (name or "Server")[:28]


def supported(link: str) -> bool:
    if link.count("#") > 1:
        return False
    if not link.startswith("vless://"):
        return True
    import urllib.parse

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    net = query.get("type", ["tcp"])[0]
    security = query.get("security", [""])[0]
    if net not in ("tcp", "ws", "grpc"):
        return False
    return security in ("", "none", "tls", "reality")


def is_public_host(link: str) -> bool:
    hp = vendor.hostport(link) if vendor else None
    if not hp:
        m = re.match(r"^[a-z0-9]+://(?:[^@/]+@)?([^:/?#]+):(\d+)", link, re.I)
        if not m:
            return False
        hp = (m.group(1), int(m.group(2)))
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(hp[0]))
    except (OSError, ValueError):
        return True
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
    )


def load_local() -> list[str]:
    if not LOCAL_FILE.exists():
        return []
    try:
        return parse_links(LOCAL_FILE.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return []


def load_geo_cache() -> dict:
    if not GEO_CACHE.exists():
        return {}
    try:
        data = json.loads(GEO_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_geo_cache(cache: dict) -> None:
    try:
        GEO_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GEO_CACHE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(GEO_CACHE)
    except OSError:
        pass


def rename_link(link: str, name: str) -> str:
    import urllib.parse

    base = link.split("#", 1)[0]
    return f"{base}#{urllib.parse.quote(name)}"


def probe_country(port: int) -> str:
    urls = (
        "http://ip-api.com/json/?fields=status,country,countryCode&lang=ru",
        "https://ipinfo.io/json",
    )
    for url in urls:
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "--max-time",
                    "6",
                    "-x",
                    f"socks5h://127.0.0.1:{port}",
                    url,
                ],
                capture_output=True,
                timeout=9,
                **vendor.NO_WINDOW,
            )
            data = json.loads(result.stdout.decode("utf-8", "ignore") or "{}")
            country = (data.get("country") or "").strip()
            if country:
                return country
        except Exception:
            continue
    return ""


async def subscription_alive(url: str, proxy: str | None = None) -> bool:
    candidates = [url]
    if not url.endswith("/"):
        candidates.append(url + "/")
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for candidate in candidates:
            try:
                async with session.get(
                    candidate,
                    headers={"User-Agent": "Happ/2.0"},
                    proxy=proxy,
                ) as resp:
                    if resp.status != 200:
                        continue
                    raw = await resp.read()
            except Exception:
                continue
            links = parse_links(_decode(raw))
            if links:
                return True
            if raw.strip().startswith((b"[", b"{")):
                return True
    return False


class KeyPool:
    def __init__(self, urls: tuple[str, ...], proxy: str | None = None) -> None:
        self.urls = urls
        self.proxy = proxy
        self._servers: list[dict] = []
        self._updated = 0.0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._load_persisted()

    def _load_persisted(self) -> None:
        if not POOL_FILE.exists():
            return
        try:
            data = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        servers = data.get("servers")
        if isinstance(servers, list):
            self._servers = [s for s in servers if s.get("url")]
            self._updated = float(data.get("timestamp", 0))

    def _save_persisted(self) -> None:
        try:
            POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = POOL_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"timestamp": time.time(), "servers": self._servers},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(POOL_FILE)
        except OSError as exc:
            log.warning("Не удалось сохранить пул ключей: %s", exc)

    async def _fetch_one(self, session: aiohttp.ClientSession, url: str) -> str:
        try:
            async with session.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, proxy=self.proxy
            ) as resp:
                if resp.status != 200:
                    log.warning("Ключи: %s вернул %s", url, resp.status)
                    return ""
                return _decode(await resp.read())
        except Exception as exc:
            log.warning("Ключи: не удалось скачать %s (%s)", url, exc)
            return ""

    async def _candidates(self) -> list[str]:
        candidates = []
        for link in load_local():
            if link not in candidates:
                candidates.append(link)

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            texts = await asyncio.gather(
                *(self._fetch_one(session, url) for url in self.urls)
            )
        online = []
        for text in texts:
            for link in parse_links(text):
                if link not in candidates and link not in online:
                    online.append(link)
        random.shuffle(online)

        sample = online[:80]
        sem = asyncio.Semaphore(40)

        async def tcp_ok(link: str) -> bool:
            hp = vendor.hostport(link) if vendor else None
            if not hp:
                return False
            async with sem:
                try:
                    _, writer = await asyncio.wait_for(
                        asyncio.open_connection(*hp), timeout=2.5
                    )
                    writer.close()
                    return True
                except Exception:
                    return False

        results = await asyncio.gather(*(tcp_ok(link) for link in sample))
        alive = [link for link, ok in zip(sample, results) if ok]
        log.info("Ключи: онлайн %s, по TCP доступно %s", len(online), len(alive))
        return candidates + alive[:PUBLIC_SAMPLE]

    async def _test(self, link: str) -> dict | None:
        if vendor is None:
            return None

        def run() -> dict | None:
            if not supported(link):
                return None
            if not is_public_host(link):
                return None
            cfg = vendor.parse_link(link)
            if not cfg:
                return None
            port = next(_PORT)
            cfg["tag"] = "bot-out"
            cfg_path = VENDOR / f"_bot{port}.json"
            cfg_path.write_text(
                json.dumps(
                    {
                        "log": {"level": "fatal"},
                        "inbounds": [
                            {
                                "type": "mixed",
                                "tag": "in",
                                "listen": "127.0.0.1",
                                "listen_port": port,
                            }
                        ],
                        "outbounds": [cfg],
                        "route": {"final": "bot-out"},
                    }
                ),
                encoding="utf-8",
            )
            proc = subprocess.Popen(
                [str(vendor.SINGBOX), "run", "-c", str(cfg_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **vendor.NO_WINDOW,
            )
            try:
                if not vendor.wait_port(port):
                    return None
                latency = vendor.curl_time(port, vendor.GSTATIC)
                if latency is None or latency > vendor.MAX_LATENCY:
                    return None
                if vendor.curl_time(port, vendor.YOUTUBE) is None:
                    return None
                return {
                    "url": link,
                    "name": link_name(link),
                    "ms": int(latency * 1000),
                    "country": probe_country(port),
                }
            finally:
                proc.kill()
                try:
                    cfg_path.unlink(missing_ok=True)
                except OSError:
                    pass

        try:
            return await asyncio.wait_for(asyncio.to_thread(run), timeout=30)
        except Exception:
            return None

    async def _resolve(self, session: aiohttp.ClientSession, host: str) -> str | None:
        try:
            return await asyncio.to_thread(socket.gethostbyname, host)
        except OSError:
            pass
        try:
            async with session.get(
                "https://dns.google/resolve",
                params={"name": host, "type": "A"},
                proxy=self.proxy,
            ) as resp:
                data = await resp.json(content_type=None)
            for answer in data.get("Answer") or []:
                if answer.get("type") == 1:
                    return answer.get("data")
        except Exception:
            pass
        return None

    async def _enrich(self, servers: list[dict]) -> list[dict]:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            hosts = []
            for server in servers:
                hp = vendor.hostport(server["url"]) if vendor else None
                hosts.append(hp[0] if hp else "")

            async def resolve(host: str) -> str | None:
                if not host:
                    return None
                return await self._resolve(session, host)

            ips = list(await asyncio.gather(*(resolve(h) for h in hosts)))

            cache = load_geo_cache()
            missing = [ip for ip in dict.fromkeys(ips) if ip and ip not in cache]
            if missing:
                try:
                    async with session.post(
                        "http://ip-api.com/batch"
                        "?fields=query,countryCode,country&lang=ru",
                        json=missing[:100],
                        proxy=self.proxy,
                    ) as resp:
                        raw = await resp.read()
                    data = json.loads(raw.decode("utf-8", "ignore"))
                    for item in data:
                        country = item.get("country")
                        if country:
                            cache[item.get("query", "")] = {
                                "country": country,
                                "code": item.get("countryCode", ""),
                            }
                except Exception as exc:
                    log.warning("Гео: не удалось определить страны (%s)", exc)
                save_geo_cache(cache)

        enriched = []
        for server, ip in zip(servers, ips):
            info = cache.get(ip) or {}
            country = server.get("country") or info.get("country") or ""
            if server["name"].lower().startswith("home"):
                display = f"Мой сервер ({country})" if country else "Мой сервер"
            else:
                display = country or server["name"]
            server = dict(server)
            server["country"] = country
            server["name"] = display
            server["url"] = rename_link(server["url"], f"{display} {server['ms']}ms")
            enriched.append(server)
        return enriched

    async def refresh(self) -> None:
        async with self._lock:
            try:
                candidates = await self._candidates()
            except Exception as exc:
                log.warning("Ключи: не удалось получить список (%s)", exc)
                candidates = load_local()

            unique = []
            for link in candidates:
                if link not in unique:
                    unique.append(link)

            log.info("Ключи: проверяю %s конфигов", len(unique))
            sem = asyncio.Semaphore(TEST_WORKERS)

            async def checked(link: str) -> dict | None:
                async with sem:
                    return await self._test(link)

            results = await asyncio.gather(*(checked(link) for link in unique))
            working = sorted(
                (r for r in results if r),
                key=lambda s: (
                    0 if s["name"].lower().startswith("home") else 1,
                    s["ms"],
                ),
            )

            if working:
                self._servers = await self._enrich(working[:10])
                self._save_persisted()
                log.info(
                    "Ключи: рабочих серверов %s (%s)",
                    len(working),
                    ", ".join(f"{s['name']} {s['ms']}ms" for s in working[:5]),
                )
            elif self._servers:
                log.warning("Ключи: новых рабочих нет, оставляю прежний пул")
            else:
                log.error("Ключи: рабочих серверов не найдено")

            self._updated = time.monotonic()
            self._ready.set()

    async def get_servers(self, limit: int = MAX_SERVERS) -> list[dict]:
        if not self._servers:
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=120)
            except asyncio.TimeoutError:
                log.warning("Ключи: проверка не успела завершиться")
        return self._servers[:limit]

    def start_refresh(self) -> asyncio.Task:
        return asyncio.create_task(self.refresh(), name="key-pool-refresh")
