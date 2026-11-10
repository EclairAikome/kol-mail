"""Signing in with an address and a password only: the mail servers are found from the address (mailfind.py), and
the login adds a mailbox with what was found."""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import accounts  # noqa: E402
import app  # noqa: E402
import autoreply  # noqa: E402
import mailfind  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402
from test_accounts import PRIMARY, FakeCli  # noqa: E402

WINDOWS = """Server:  UnKnown
Address:  10.0.0.1

Non-authoritative answer:
example.com\tMX preference = 10, mail exchanger = mx2.us.alibabacloud.com
example.com\tMX preference = 5, mail exchanger = mx1.us.alibabacloud.com

mx1.us.alibabacloud.com\tinternet address = 203.0.113.7
"""
MAC = """Server:\t\t192.0.2.1
Address:\t192.0.2.1#53

Non-authoritative answer:
example.com\tmail exchanger = 20 alt1.aspmx.l.google.com.
example.com\tmail exchanger = 1 aspmx.l.google.com.
"""
CONFIG = b"""<?xml version="1.0"?>
<clientConfig version="1.1"><emailProvider id="example.com">
  <incomingServer type="pop3"><hostname>pop.example.com</hostname><port>995</port><socketType>SSL</socketType></incomingServer>
  <incomingServer type="imap"><hostname>imap.example.com</hostname><port>143</port><socketType>STARTTLS</socketType></incomingServer>
  <incomingServer type="imap"><hostname>imap.%EMAILDOMAIN%</hostname><port>993</port><socketType>SSL</socketType></incomingServer>
  <outgoingServer type="smtp"><hostname>smtp.example.com</hostname><port>587</port><socketType>STARTTLS</socketType></outgoingServer>
</emailProvider></clientConfig>"""


def test_mail_exchangers_are_read_from_either_nslookup():
    assert mailfind.parse_nslookup(WINDOWS) == ["mx1.us.alibabacloud.com", "mx2.us.alibabacloud.com"]  # best first
    assert mailfind.parse_nslookup(MAC) == ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]
    assert mailfind.parse_nslookup("*** UnKnown can't find example.com: Non-existent domain") == []
    assert mailfind.parse_nslookup("x\tmail exchanger = 5 bad_host!.example.com") == []


def test_mail_exchangers_are_read_from_a_dns_over_https_answer():
    answer = {"Status": 0, "Answer": [{"type": 15, "data": "10 mx2.example.net."}, {"type": 5, "data": "x.example.net."},
                                      {"type": 15, "data": "5 MX1.example.net."}, {"type": 15, "data": "oops"}]}
    assert mailfind.parse_doh(answer) == ["mx1.example.net", "mx2.example.net"]
    assert mailfind.parse_doh({"Status": 3}) == []          # no such domain: an answer, nothing to ask elsewhere
    assert mailfind.parse_doh({"Status": 2}) is None and mailfind.parse_doh("junk") is None  # ask the next service


@pytest.mark.parametrize("mx, imap, smtp, port", [
    ("aspmx.l.google.com", "imap.gmail.com", "smtp.gmail.com", 465),
    ("mx1.us.alibabacloud.com", "imap.us.alibabacloud.com", "smtp.us.alibabacloud.com", 465),
    ("mxn.mxhichina.com", "imap.qiye.aliyun.com", "smtp.qiye.aliyun.com", 465),
    ("mxbiz1.qq.com", "imap.exmail.qq.com", "smtp.exmail.qq.com", 465),
    ("inbound-smtp.eu-west-1.amazonaws.com", "imap.mail.eu-west-1.awsapps.com", "smtp.mail.eu-west-1.awsapps.com", 465),
    ("mx01.mail.icloud.com", "imap.mail.me.com", "smtp.mail.me.com", 587),
])
def test_a_hosted_mail_service_is_known_by_its_mail_exchanger(mx, imap, smtp, port):
    found = mailfind.from_mx([mx, "backup.example.net"])
    assert (found["imap_host"], found["smtp_host"], found["smtp_port"], found["source"]) == (imap, smtp, port, "mx")
    assert found["imap_port"] == 993 and found["imap_security"] == "ssl"


def test_other_mail_exchangers_are_not_guessed_at_and_microsoft_says_why():
    assert mailfind.from_mx(["mail.example.org"]) is None and mailfind.from_mx([]) is None
    assert mailfind.from_mx(["notgoogle.com.example.org"]) is None  # the name must end there, not just hold it
    with pytest.raises(accounts.AccountError, match="OAuth"):
        mailfind.from_mx(["example-org.mail.protection.outlook.com"])


def test_an_autoconfig_file_gives_encrypted_imap_and_smtp():
    found = mailfind.parse_autoconfig(CONFIG, "example.com")
    assert found == {"imap_host": "imap.example.com", "imap_port": 993, "imap_security": "ssl",
                     "smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_security": "starttls", "source": "autoconfig"}
    plain = CONFIG.replace(b"STARTTLS", b"plain").replace(b"<socketType>SSL", b"<socketType>plain")
    assert mailfind.parse_autoconfig(plain, "example.com") is None  # nothing unencrypted
    assert mailfind.parse_autoconfig(CONFIG.replace(b"smtp", b"pop3"), "example.com") is None  # no way to send
    assert mailfind.parse_autoconfig(b"<html>not found</html>", "example.com") is None
    assert mailfind.parse_autoconfig(b"<clientConfig", "example.com") is None
    assert mailfind.parse_autoconfig(CONFIG.replace(b"imap.example.com", b"imap.exa mple.com; x"), "example.com")["imap_host"] \
        == "imap.example.com"  # the bad host is skipped, the next server of that kind is taken


def test_an_autoconfig_file_that_declares_entities_is_not_read():
    bomb = b'<?xml version="1.0"?><!DOCTYPE c [<!ENTITY a "aaaa">]>' + CONFIG.split(b"?>", 1)[1]
    assert mailfind.parse_autoconfig(bomb, "example.com") is None
    assert mailfind.parse_autoconfig(CONFIG + b" " * mailfind.MAX_XML, "example.com") is None  # too big to be one


def test_an_autoconfig_file_cannot_point_at_other_ports_or_hide_a_doctype_in_another_encoding():
    assert mailfind.parse_autoconfig(CONFIG.replace(b"<port>993<", b"<port>6379<"), "example.com")["imap_port"] == 143
    odd = CONFIG.replace(b"<port>993<", b"<port>6379<").replace(b"<port>143<", b"<port>22<")
    assert mailfind.parse_autoconfig(odd, "example.com") is None  # no mail port left for receiving
    assert mailfind.parse_autoconfig(CONFIG.replace(b"<port>587<", b"<port>465<"), "example.com") is None  # 465 is not STARTTLS
    wide = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE c [<!ENTITY h "imap.example.net">]>'
            + CONFIG.decode().split("?>", 1)[1].replace("imap.%EMAILDOMAIN%", "&h;")).encode("utf-16")
    assert mailfind.parse_autoconfig(wide, "example.com") is None
    assert mailfind.parse_autoconfig(CONFIG.replace(b"imap.example.com", b"imap.caf\xe9.com"), "example.com") is None  # not UTF-8


def test_a_host_is_a_name_never_an_address_and_nothing_follows_it():
    for good in ("imap.example.com", "mx1.us.alibabacloud.com", "a.xn--p1ai", "mail.example.co.uk"):
        assert mailfind.HOST.match(good)
    for bad in ("127.0.0.1", "169.254.169.254", "imap.example.com\n", "imap.example.com/x", "-x.example.com",
                "localhost", "imap..example.com", "imap.example.1"):
        assert not mailfind.HOST.match(bad)
    wide = CONFIG.replace(b"imap.%EMAILDOMAIN%", b"10.0.0.5").replace(b"imap.example.com", b"192.168.1.1")
    assert mailfind.parse_autoconfig(wide, "example.com") is None


def test_the_usual_names_are_in_front_of_the_domain_itself_never_next_to_the_mail_exchanger():
    assert mailfind.candidates("example.com") == (["imap.example.com", "mail.example.com"],
                                                 ["smtp.example.com", "mail.example.com"])
    assert mailfind.base_of("mx1.us.alibabacloud.com") == "alibabacloud.com"
    assert mailfind.base_of("mx.example.com.cn") == "example.com.cn" and mailfind.base_of("example.org") == "example.org"


def test_a_forged_mail_exchanger_cannot_name_the_server(monkeypatch):
    """Found in review: MX records come over plain DNS. Hosts built from one (imap.<where the MX points>) would
    be the forger's, with a valid certificate of their own, and the password would go there."""
    world = World(monkeypatch, mx=["mx.evil.example.net"], up=[("imap.evil.example.net", 993), ("mail.evil.example.net", 993),
                                                              ("smtp.evil.example.net", 465), ("mail.evil.example.net", 465)])
    with pytest.raises(accounts.AccountError) as e:
        asyncio.run(mailfind.discover("li@example.com"))
    assert e.value.code == "servers_unknown" and not any("evil" in host for host, _, _ in world.probed)


def test_servers_outside_the_address_own_domain_are_shown_first_unless_the_table_names_them():
    table = mailfind.from_mx(["aspmx.l.google.com"])
    assert not mailfind.needs_ok(table, "li@example.com")                 # a fixed, well-known host
    own = mailfind.servers(("mail.example.com", 993, "ssl", "smtp.example.com", 465, "ssl"), "guess")
    assert not mailfind.needs_ok(own, "li@example.com") and not mailfind.needs_ok(own, "li@sub.example.com")
    for row in (("imap.hoster.example.net", 993, "ssl", "smtp.example.com", 465, "ssl"),
                ("imap.example.com", 993, "ssl", "smtp.hoster.example.net", 465, "ssl")):
        assert mailfind.needs_ok(mailfind.servers(row, "autoconfig"), "li@example.com")


class World:
    """What the search may see: the MX records, the files on the web, and which servers answer."""

    def __init__(self, monkeypatch, mx=(), files=None, up=()):
        self.asked, self.probed, self.files, self.up = [], [], files or {}, set(up)

        async def mx_hosts(domain, client):
            self.asked.append(("mx", domain))
            return list(mx)

        async def fetch(client, url):
            self.asked.append(("get", url))
            return self.files.get(url)

        async def answers(host, port, security, kind):
            self.probed.append((host, port, kind))
            return (host, port) in self.up
        monkeypatch.setattr(mailfind, "mx_hosts", mx_hosts)
        monkeypatch.setattr(mailfind, "fetch", fetch)
        monkeypatch.setattr(mailfind, "answers", answers)


def test_a_company_address_on_a_hosted_service_is_found_by_its_mx(monkeypatch):
    world = World(monkeypatch, mx=["mx1.us.alibabacloud.com"], up=[("imap.us.alibabacloud.com", 993)])
    found = asyncio.run(mailfind.discover("Li.Na@Example.com"))
    assert found["imap_host"] == "imap.us.alibabacloud.com" and found["source"] == "mx"
    assert world.asked == [("mx", "example.com")]  # nothing else was needed, so nothing else was asked


def test_the_domains_own_autoconfig_comes_before_the_public_database(monkeypatch):
    own = "https://autoconfig.example.com/mail/config-v1.1.xml"
    public = mailfind.ISPDB + "example.com"
    world = World(monkeypatch, mx=["mx.example.com"], up=[("imap.example.com", 993), ("imap.elsewhere.example.net", 993)],
                  files={own: CONFIG, public: CONFIG.replace(b"imap.%EMAILDOMAIN%", b"imap.elsewhere.example.net")})
    assert asyncio.run(mailfind.discover("li@example.com"))["imap_host"] == "imap.example.com"
    world.files.pop(own)
    assert asyncio.run(mailfind.discover("li@example.com"))["imap_host"] == "imap.elsewhere.example.net"


def test_a_server_that_does_not_answer_is_passed_over_for_the_next_way(monkeypatch):
    # the table's server is unreachable from here, mail.example.com answers on both ports
    World(monkeypatch, mx=["aspmx.l.google.com"], up=[("mail.example.com", 993), ("mail.example.com", 587)])
    found = asyncio.run(mailfind.discover("li@example.com"))
    assert (found["imap_host"], found["smtp_host"], found["smtp_port"], found["smtp_security"], found["source"]) == \
        ("mail.example.com", "mail.example.com", 587, "starttls", "guess")


def test_nothing_found_says_so_with_a_code_the_page_knows(monkeypatch):
    World(monkeypatch)
    with pytest.raises(accounts.AccountError, match="没能自动找到 example.com") as e:
        asyncio.run(mailfind.discover("li@example.com"))
    assert e.value.code == "servers_unknown"
    World(monkeypatch, mx=["aspmx.l.google.com"])  # known, but this computer cannot reach it
    with pytest.raises(accounts.AccountError, match="imap.gmail.com") as e:
        asyncio.run(mailfind.discover("li@example.com"))
    assert e.value.code == "servers_unreachable"
    World(monkeypatch, up=[("imap.example.com", 993)])  # receiving alone is no mailbox to reply from
    with pytest.raises(accounts.AccountError):
        asyncio.run(mailfind.discover("li@example.com"))


def test_only_the_domain_ever_leaves_for_the_lookups(monkeypatch):
    world = World(monkeypatch, mx=["mx.hoster.example.net"])
    with pytest.raises(accounts.AccountError):
        asyncio.run(mailfind.discover("very.secret.name@example.com"))
    seen = repr(world.asked) + repr(world.probed)
    assert "secret" not in seen and "example.com" in seen
    assert ("get", mailfind.ISPDB + "example.net") in world.asked  # the database is asked about the MX's domain too
    assert all(url.startswith("https://") for kind, url in world.asked if kind == "get")
    assert all(host.endswith(".example.com") for host, _, _ in world.probed)  # only names of the domain itself


def test_the_search_gives_up_in_time(monkeypatch):
    async def slow(domain):
        await asyncio.sleep(30)
    monkeypatch.setattr(mailfind, "search", slow)
    monkeypatch.setattr(mailfind, "TOTAL_TIMEOUT", 0.05)
    with pytest.raises(accounts.AccountError) as e:
        asyncio.run(mailfind.discover("li@example.com"))
    assert e.value.code == "servers_unknown"


def test_the_script_prints_what_it_found_for_the_installers(monkeypatch, capsys):
    async def found(address):
        return mailfind.servers(("imap.example.com", 993, "ssl", "smtp.example.com", 465, "ssl"), "mx")
    monkeypatch.setattr(mailfind, "discover", found)
    assert mailfind.main(["mailfind.py", "li@example.com"]) == 0
    assert '"imap_host": "imap.example.com"' in capsys.readouterr().out
    assert mailfind.main(["mailfind.py", "a@163.com"]) == 0 and "imap.163.com" in capsys.readouterr().out  # a preset
    assert mailfind.main(["mailfind.py", "not an address"]) == 2
    assert mailfind.main(["mailfind.py", "x@outlook.com"]) == 1 and "OAuth" in capsys.readouterr().err


# ---------------------------------------------------------------- the login and the add-mailbox dialog

@pytest.fixture
def client(monkeypatch):
    saved = {}
    monkeypatch.setattr(app, "ACCOUNTS", [PRIMARY])
    monkeypatch.setattr(app, "ACCOUNT", "me")
    monkeypatch.setattr(app, "restart_pending", False)
    monkeypatch.setattr(app.config, "update", lambda path, value: saved.__setitem__(tuple(path), value))
    monkeypatch.setattr(app.config, "update_many", lambda changes: [saved.__setitem__(tuple(p), v) for p, v in changes])

    async def no_reload():
        return None
    monkeypatch.setattr(app, "reload_mail_sessions", no_reload)
    return TestClient(app.starlette_app), saved


def finder(monkeypatch, result):
    calls = []

    async def discover(address):
        calls.append(address)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(app.mailfind, "discover", discover)
    return calls


# found by the table: nothing to confirm
FOUND = {"imap_host": "imap.hoster.example.net", "imap_port": 993, "imap_security": "ssl",
         "smtp_host": "smtp.hoster.example.net", "smtp_port": 587, "smtp_security": "starttls", "source": "mx"}
ELSEWHERE = {**FOUND, "source": "autoconfig"}  # named by a file, and not under example.org
OK = "imap.hoster.example.net smtp.hoster.example.net"


def test_an_address_and_a_password_are_enough(client, monkeypatch):
    c, saved = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    calls = finder(monkeypatch, FOUND)
    d = c.post("/api/accounts/add", json={"email": "Li@Example.org", "password": "pw"}).json()
    assert calls == ["li@example.org"] and cli.calls[0][3] == "imap.hoster.example.net"
    assert d["found"] == {"imap_host": "imap.hoster.example.net", "smtp_host": "smtp.hoster.example.net"}
    assert "pw" not in repr(saved)


def test_servers_named_from_outside_are_shown_before_the_password_goes_there(client, monkeypatch):
    """Found in review: an autoconfig file may name any host. Nothing is stored or sent until the user has seen
    the servers and asked again with exactly those."""
    c, saved = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    finder(monkeypatch, ELSEWHERE)
    body = {"email": "li@example.org", "password": "pw"}
    d = c.post("/api/accounts/add", json=body).json()
    assert d == {"confirm": {"imap_host": "imap.hoster.example.net", "smtp_host": "smtp.hoster.example.net", "token": OK}}
    assert not cli.calls and not saved and len(app.ACCOUNTS) == 1
    assert "confirm" in c.post("/api/accounts/add", json={**body, "servers_ok": "imap.other.example.net x"}).json()
    assert not cli.calls  # agreeing to other servers is no agreement to these
    d = c.post("/api/accounts/add", json={**body, "servers_ok": OK}).json()
    assert d["added"] and cli.calls[0][3] == "imap.hoster.example.net"


def test_nothing_is_looked_for_when_the_servers_are_known(client, monkeypatch):
    c, _ = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    calls = finder(monkeypatch, FOUND)
    assert c.post("/api/accounts/add", json={"email": "li@163.com", "password": "pw"}).json()["found"] is None  # a preset
    assert c.post("/api/accounts/add", json={"email": "team@example.com", "password": "pw"}).status_code == 200  # the company's
    typed = {"email": "li@example.org", "password": "pw", "imap_host": "mail.example.org", "smtp_host": "mail.example.org"}
    assert c.post("/api/accounts/add", json=typed).status_code == 200  # typed by hand
    assert not calls and [call[3] for call in cli.calls] == ["imap.163.com", "imap.example.com", "mail.example.org"]


def test_servers_that_are_not_found_leave_nothing_behind_and_say_what_to_do(client, monkeypatch):
    c, saved = client
    cli = FakeCli()
    monkeypatch.setattr(app, "mail_cli", cli)
    finder(monkeypatch, mailfind.not_found("example.org"))
    r = c.post("/api/accounts/add", json={"email": "li@example.org", "password": "pw"})
    assert r.status_code == 400 and "没能自动找到" in r.json()["error"] and "手动填写服务器" in r.json()["error"]
    assert not cli.calls and not saved  # the password went nowhere


def test_a_refusal_names_the_server_that_was_found(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr(app, "mail_cli", FakeCli(receiving="Authentication or provider policy rejected"))
    finder(monkeypatch, FOUND)
    error = c.post("/api/accounts/add", json={"email": "li@example.org", "password": "wrong"}).json()["error"]
    assert "没接受这个密码" in error and "imap.hoster.example.net" in error
    monkeypatch.setattr(app, "mail_cli", FakeCli(sending="smtp refused"))
    d = c.post("/api/accounts/add", json={"email": "li@example.org", "password": "pw"}).json()
    assert "smtp.hoster.example.net" in d["warning"]


def test_the_login_screen_signs_in_with_what_was_found(client, monkeypatch):
    c, saved = client
    monkeypatch.setattr(app, "LOGGED_OUT", True)
    monkeypatch.setattr(app, "ACCOUNTS", [])
    monkeypatch.setattr(app, "mail_cli", FakeCli())
    monkeypatch.setattr(app, "spawn", lambda coro, what: coro.close())
    finder(monkeypatch, FOUND)
    s = c.get("/api/status").json()
    assert s["login_needed"] and "domain" not in s["login"]  # the page asks for the whole address
    d = c.post("/api/accounts/add", json={"email": "li@example.org", "password": "pw", "full_name": "Li Na"}).json()
    assert d["restarting"] and d["found"]["imap_host"] == "imap.hoster.example.net"
    assert saved[("mail", "address")] == "li@example.org"


# ---------------------------------------------------------------- whose senders are colleagues

def test_a_public_mailbox_is_never_the_companys_domain():
    assert autoreply.own_domain("", "me@example.com") == "@example.com"   # a company address: its domain
    assert autoreply.own_domain("@Example.org", "me@" + "gmail.com") == "@example.org"  # the setting wins
    for domain in ("gmail.com", "163.com", "qq.com", "outlook.com", "yandex.ru"):
        assert autoreply.own_domain("", "me@" + domain) == ""  # or every KOL on Gmail would be a colleague
        # Found in review: an installer run on its console path had written the public domain as the setting
        assert autoreply.own_domain("@" + domain, "me@" + domain) == ""
        assert autoreply.own_domain("@" + domain, "me@example.com") == "@example.com"
    assert autoreply.own_domain("", "") == ""
