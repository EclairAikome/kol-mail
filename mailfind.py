"""Find a mailbox's IMAP and SMTP servers from its address alone, so the login page asks for an address and a
password and nothing else.

What is looked at, in this order, until one receiving server answers:

1. The domain's MX records, against a short table of the big hosted-mail services (Google Workspace, Alibaba Mail,
   Tencent Exmail, Amazon WorkMail, ...). A company address on such a service is the usual case.
2. Autoconfig, the way Thunderbird does it: the domain's own file (https only), then Thunderbird's public database,
   asked about the domain and about the MX's domain.
3. The usual names in front of the domain itself: imap. / mail. for receiving, smtp. / mail. for sending.

A receiving server is only offered after it answered as an IMAP server over TLS with a certificate valid for its
name (for the usual names that is the whole evidence; for the others it keeps a stale table row or database entry
from ending the search). The password is never part of any of this: the caller logs in afterwards, with the
servers found here.

Whom each source can be trusted for. MX records come over plain DNS, which someone on the network can forge, so
they only ever select a row of the table (fixed, well-known hosts) or a database entry: no host name is built from
them. The domain's own autoconfig file and the usual names are vouched for by a certificate for the domain; the
database is Thunderbird's. An autoconfig answer may still name any host, so servers that are neither from the
table nor under the address's own domain are shown to the user before the password goes there (needs_ok).

Only the domain (what follows the @) ever leaves this computer for these lookups: to the system's DNS resolver (or,
when that gives nothing, a public DNS-over-HTTPS service), to the domain's own web server and to Thunderbird's
database. Never the address in front of the @.

Run as a script it prints what it found as JSON (the installers use that): python mailfind.py name@example.com
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import ssl
import subprocess
import sys
import xml.etree.ElementTree as ET

import httpx

import accounts

# a DNS name whose last label starts with a letter: never an IP address, never anything after the name
HOST = re.compile(r"(?=.{4,253}\Z)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*"
                  r"\.[a-z](?:[a-z0-9-]*[a-z0-9])\Z")
IMAP_PORTS, SMTP_PORTS = {993: "ssl", 143: "starttls"}, {465: "ssl", 587: "starttls"}  # what an autoconfig file may name
LOOKUP_TIMEOUT = 8     # nslookup, seconds
HTTP_TIMEOUT = 6       # one DNS-over-HTTPS or autoconfig request
PROBE_TIMEOUT = 6      # one connection to a server that may be the one
TOTAL_TIMEOUT = 50     # the whole search; the page says "一两分钟" for search plus login
MAX_XML = 64 * 1024

# MX host -> (IMAP host, port, security, SMTP host, port, security); {0} is the pattern's first group (a region).
HOSTED = [
    (r"(^|\.)google(mail)?\.com$", ("imap.gmail.com", 993, "ssl", "smtp.gmail.com", 465, "ssl")),
    (r"(^|\.)mxhichina\.com$|(^|\.)qiye\.aliyun\.com$",
     ("imap.qiye.aliyun.com", 993, "ssl", "smtp.qiye.aliyun.com", 465, "ssl")),
    (r"^mx\d*\.([a-z0-9-]+)\.alibabacloud\.com$",
     ("imap.{0}.alibabacloud.com", 993, "ssl", "smtp.{0}.alibabacloud.com", 465, "ssl")),
    (r"^mxbiz\d*\.qq\.com$", ("imap.exmail.qq.com", 993, "ssl", "smtp.exmail.qq.com", 465, "ssl")),
    (r"^inbound-smtp\.([a-z0-9-]+)\.amazonaws\.com$",
     ("imap.mail.{0}.awsapps.com", 993, "ssl", "smtp.mail.{0}.awsapps.com", 465, "ssl")),
    (r"(^|\.)messagingengine\.com$", ("imap.fastmail.com", 993, "ssl", "smtp.fastmail.com", 465, "ssl")),
    (r"(^|\.)mail\.icloud\.com$", ("imap.mail.me.com", 993, "ssl", "smtp.mail.me.com", 587, "starttls")),
    (r"(^|\.)yandex\.(net|ru)$", ("imap.yandex.com", 993, "ssl", "smtp.yandex.com", 465, "ssl")),
    (r"(^|\.)feishu\.cn$", ("imap.feishu.cn", 993, "ssl", "smtp.feishu.cn", 465, "ssl")),
    (r"(^|\.)larksuite\.com$", ("imap.larksuite.com", 993, "ssl", "smtp.larksuite.com", 465, "ssl")),
]
# Microsoft 365 takes no password login from mail programs any more (OAuth only), whatever the tenant's domain.
OAUTH_MX = re.compile(r"(^|\.)protection\.outlook\.com$")
OAUTH_ONLY = ("这个邮箱由微软 365（Outlook）托管，只能用微软账号网页登录（OAuth），KOL Mail 暂时接不了。"
              "邮箱其实不在微软那里的话，点开下面的“手动填写服务器”自己填")
DOH = ("https://dns.alidns.com/resolve", "https://cloudflare-dns.com/dns-query", "https://dns.google/resolve")
ISPDB = "https://autoconfig.thunderbird.net/v1.1/"
SECOND_LEVEL = {"com", "co", "net", "org", "gov", "edu", "ac"}  # example.com.cn, example.co.uk


def servers(row: tuple, source: str) -> dict:
    ih, ip, isec, sh, sp, ssec = row
    return {"imap_host": ih, "imap_port": ip, "imap_security": isec, "smtp_host": sh, "smtp_port": sp,
            "smtp_security": ssec, "source": source}


# ---------------------------------------------------------------- MX records

def parse_nslookup(text: str) -> list[str]:
    """The mail exchangers in nslookup's answer, best preference first. Windows prints
    "MX preference = 5, mail exchanger = mx1.example.com", macOS "mail exchanger = 5 mx1.example.com."."""
    found = []
    for line in text.splitlines():
        m = re.search(r"mail exchanger\s*=\s*(?:(\d+)\s+)?([A-Za-z0-9.-]+)", line)
        if not m:
            continue
        windows = re.search(r"MX preference\s*=\s*(\d+)", line)
        host = m.group(2).rstrip(".").lower()
        if HOST.match(host):
            found.append((int(m.group(1) or (windows.group(1) if windows else 0)), host))
    return list(dict.fromkeys(h for _, h in sorted(found)))


async def nslookup_mx(domain: str) -> list[str]:
    """Through the system's own resolver: the one that works on a company network, and it tells nobody new."""
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    tool = "nslookup"
    if os.name == "nt":  # the system's own, whatever the folder we run in holds
        system = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nslookup.exe")
        tool = system if os.path.isfile(system) else tool
    try:
        proc = await asyncio.create_subprocess_exec(
            tool, "-type=mx", domain + ".", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **extra)
    except OSError:
        return []
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), LOOKUP_TIMEOUT)
    except asyncio.TimeoutError:
        return []
    finally:  # timed out here, or the whole search was given up: it is not left running
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
    return parse_nslookup(out.decode("utf-8", "replace"))


def parse_doh(data) -> list[str] | None:
    """The mail exchangers in a DNS-over-HTTPS JSON answer; None when the answer says nothing either way."""
    if not isinstance(data, dict) or data.get("Status") not in (0, 3):  # 3: no such domain, which is an answer
        return None
    found = []
    for a in data.get("Answer") or []:
        if not isinstance(a, dict) or a.get("type") != 15:
            continue
        m = re.fullmatch(r"\s*(\d+)\s+([A-Za-z0-9.-]+)\s*", str(a.get("data") or ""))
        if m and HOST.match(m.group(2).rstrip(".").lower()):
            found.append((int(m.group(1)), m.group(2).rstrip(".").lower()))
    return list(dict.fromkeys(h for _, h in sorted(found)))


async def doh_mx(domain: str, client: httpx.AsyncClient) -> list[str]:
    for url in DOH:
        try:
            r = await client.get(url, params={"name": domain, "type": "MX"}, headers={"accept": "application/dns-json"})
            hosts = parse_doh(r.json()) if r.status_code == 200 else None
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            continue
        if hosts is not None:
            return hosts
    return []


async def mx_hosts(domain: str, client: httpx.AsyncClient) -> list[str]:
    return await nslookup_mx(domain) or await doh_mx(domain, client)


def from_mx(hosts: list[str]) -> dict | None:
    """The hosted-mail service these MX records belong to, when it is one of the table's."""
    for host in hosts:
        if OAUTH_MX.search(host):
            raise accounts.AccountError(OAUTH_ONLY)
        for pattern, row in HOSTED:
            m = re.search(pattern, host)
            if m:
                region = m.group(1) if m.re.groups and row[0].count("{0}") else ""
                return servers(tuple(x.format(region) if isinstance(x, str) else x for x in row), "mx")
    return None


def base_of(host: str) -> str:
    """The registered domain a host belongs to, near enough: the last two labels, three under com.cn and the like."""
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


# ---------------------------------------------------------------- autoconfig

def parse_autoconfig(raw: bytes, domain: str) -> dict | None:
    """IMAP and SMTP servers out of a Thunderbird autoconfig file; None when it names none we can use. Only
    encrypted connections count, and a file that declares entities is not read at all."""
    if len(raw) > MAX_XML or b"\0" in raw or re.search(rb"<!\s*(DOCTYPE|ENTITY)", raw, re.IGNORECASE):
        return None  # a zero byte: UTF-16 or UTF-32, where the check for a DOCTYPE would see nothing
    try:
        raw.decode("utf-8")
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError):
        return None

    def pick(tag: str, kind: str, ports: dict):
        best = None
        for node in root.iter(tag):
            if (node.get("type") or "").lower() != kind:
                continue
            host = (node.findtext("hostname") or "").strip().lower().replace("%emaildomain%", domain)
            socket = (node.findtext("socketType") or "").strip().upper()
            security = {"SSL": "ssl", "STARTTLS": "starttls"}.get(socket)
            if not security or not HOST.match(host):
                continue
            try:
                port = int((node.findtext("port") or "").strip() or next(p for p, s in ports.items() if s == security))
            except ValueError:
                continue
            if ports.get(port) != security:  # the mail ports only: a file cannot point us at any port of any host
                continue
            if security == "ssl":
                return host, port, security
            best = best or (host, port, security)
        return best

    incoming, outgoing = pick("incomingServer", "imap", IMAP_PORTS), pick("outgoingServer", "smtp", SMTP_PORTS)
    if not incoming or not outgoing:
        return None
    return servers((*incoming, *outgoing), "autoconfig")


async def fetch(client: httpx.AsyncClient, url: str) -> bytes | None:
    """A small file over https, without following redirects; None for anything else."""
    try:
        async with client.stream("GET", url, headers={"accept-encoding": "identity"}) as r:  # the cap is on what is read
            if r.status_code != 200:
                return None
            raw = b""
            async for chunk in r.aiter_bytes():
                raw += chunk
                if len(raw) > MAX_XML:
                    return None
            return raw
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        return None


async def autoconfig(domain: str, mx: list[str], client: httpx.AsyncClient) -> list[dict]:
    """What the autoconfig sources say, the domain's own word first."""
    urls = [f"https://autoconfig.{domain}/mail/config-v1.1.xml",
            f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml", ISPDB + domain]
    if mx and base_of(mx[0]) != domain:
        urls.append(ISPDB + base_of(mx[0]))
    files = await asyncio.gather(*(fetch(client, u) for u in urls))
    found = [parse_autoconfig(raw, domain) for raw in files if raw]
    return [f for f in found if f]


# ---------------------------------------------------------------- does a server answer

async def answers(host: str, port: int, security: str, kind: str) -> bool:
    """True when this host and port greet as an IMAP (kind "imap") or SMTP server. With security "ssl" the
    certificate must be valid for the host; "starttls" only reads the greeting a server sends before any
    encryption (nothing is sent to it either way)."""
    writer = None
    try:
        tls = {"ssl": ssl.create_default_context(), "server_hostname": host} if security == "ssl" else {}
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, **tls), PROBE_TIMEOUT)
        line = await asyncio.wait_for(reader.readline(), PROBE_TIMEOUT)
        return line.startswith((b"* OK", b"* PREAUTH")) if kind == "imap" else line.startswith(b"220")
    except (OSError, asyncio.TimeoutError, ValueError):  # ssl.SSLError is an OSError; ValueError: an endless line
        return False
    finally:
        if writer is not None:
            writer.close()


def candidates(domain: str) -> tuple[list[str], list[str]]:
    """The usual names for the receiving and the sending server, in front of the domain itself. Never next to the
    MX host: an MX record can be forged, and a name built from it would be wherever the forger says."""
    return [f"imap.{domain}", f"mail.{domain}"], [f"smtp.{domain}", f"mail.{domain}"]


async def guess(domain: str) -> dict | None:
    incoming, outgoing = candidates(domain)
    imap_ok, smtp_ssl, smtp_tls = await asyncio.gather(
        asyncio.gather(*(answers(h, 993, "ssl", "imap") for h in incoming)),
        asyncio.gather(*(answers(h, 465, "ssl", "smtp") for h in outgoing)),
        asyncio.gather(*(answers(h, 587, "starttls", "smtp") for h in outgoing)))
    imap = next((h for h, ok in zip(incoming, imap_ok) if ok), None)
    smtp = next(((h, 465, "ssl") for h, ok in zip(outgoing, smtp_ssl) if ok), None) \
        or next(((h, 587, "starttls") for h, ok in zip(outgoing, smtp_tls) if ok), None)
    if not imap or not smtp:
        return None
    return servers((imap, 993, "ssl", *smtp), "guess")


# ---------------------------------------------------------------- the search

def not_found(domain: str) -> accounts.AccountError:
    return accounts.AccountError(
        f"没能自动找到 {domain} 的收发信服务器。先看看邮箱地址有没有写错；地址没错的话，点开下面的“手动填写服务器”，"
        "填邮箱服务商给的收信（IMAP）和发信（SMTP）服务器", "servers_unknown")


async def search(domain: str) -> dict:
    unreachable = None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False) as client:
        mx = await mx_hosts(domain, client)
        known = [x for x in [from_mx(mx)] if x]
        for found in known:
            if await answers(found["imap_host"], found["imap_port"], found["imap_security"], "imap"):
                return found
            unreachable = unreachable or found["imap_host"]
        for found in await autoconfig(domain, mx, client):
            if await answers(found["imap_host"], found["imap_port"], found["imap_security"], "imap"):
                return found
            unreachable = unreachable or found["imap_host"]
    found = await guess(domain)
    if found:
        return found
    if unreachable:
        raise accounts.AccountError(
            f"这个邮箱的收信服务器应该是 {unreachable}，但这台电脑现在连不上它。检查一下网络（公司网络、代理、防火墙），"
            "稍后再试", "servers_unreachable")
    raise not_found(domain)


def needs_ok(found: dict, address: str) -> bool:
    """True when the user should see these servers before the password goes to them: they are not a row of the
    table and not under the address's own domain (an autoconfig file or database entry naming another host)."""
    own = base_of(address.rpartition("@")[2].lower())
    return found.get("source") != "mx" and any(base_of(found[k]) != own for k in ("imap_host", "smtp_host"))


async def discover(address: str) -> dict:
    """The servers of this address's mailbox, with "source" saying how they were found (mx, autoconfig, guess).
    Raises accounts.AccountError, code servers_unknown, when nothing was found."""
    domain = address.rpartition("@")[2].lower()
    if not HOST.match(domain):
        raise not_found(domain)
    try:
        return await asyncio.wait_for(search(domain), TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        raise not_found(domain) from None


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not accounts.EMAIL.fullmatch(argv[1]):
        print("usage: python mailfind.py name@example.com", file=sys.stderr)
        return 2
    address = argv[1].lower()
    try:
        try:
            found = accounts.servers_for(address, {}, None)
        except accounts.AccountError as e:
            if e.code != "servers_unknown":
                raise
            found = asyncio.run(discover(address))
    except accounts.AccountError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(found))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
