import json
import logging
import ipaddress

from .helpers import make_event_id, get_event_type
from bbot.core.errors import *
from bbot.core.helpers import (
    extract_words,
    tldextract,
    split_host_port,
    host_in_host,
    is_domain,
    is_subdomain,
    is_ip,
    make_netloc,
    validate_port,
    make_ip_type,
    smart_decode,
    regexes,
    clean_url,
)


log = logging.getLogger("bbot.core.event")


class BaseEvent:

    _dummy = False
    _omit = False

    def __init__(
        self,
        data,
        event_type=None,
        source=None,
        module=None,
        scan=None,
        tags=None,
        confidence=100,
        _dummy=False,
        _internal=None,
    ):

        if tags is None:
            tags = set()

        self._data = None
        self.type = event_type
        self.tags = set(tags)
        self.confidence = int(confidence)

        # for creating one-off events without enforcing source requirement
        self._dummy = _dummy
        self._internal = False

        self.module = module
        self.scan = scan
        if (not self.scan) and (not self._dummy):
            raise ValidationError(f"Must specify scan")

        # check type blacklist
        if self.scan is not None:
            omit_event_types = self.scan.config.get("omit_event_types", [])
            if omit_event_types and self.type in omit_event_types:
                self._omit = True

        self._scope_distance = -1

        self._source = None
        self.source_id = None
        self.source = source
        if (not self.source) and (not self._dummy):
            raise ValidationError(f"Must specify event source")

        try:
            self.data = self._sanitize_data(data)
        except Exception as e:
            import traceback

            log.debug(traceback.format_exc())
            raise ValidationError(f'Error sanitizing event data "{data}" for type "{self.type}": {e}')

        if not self.data:
            raise ValidationError(f'Invalid event data "{data}" for type "{self.type}"')

        if not self._dummy:
            self._setup()

        self._id = None
        self._hash = None
        self.__host = None
        self._port = None
        self.__words = None
        self._made_internal = False
        # whether to force-send to output modules
        self._force_output = False

        # internal events are not ingested by output modules
        if not self._dummy:
            if _internal or source._internal:
                self.make_internal()

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, data):
        self._hash = None
        self._id = None
        self._data = data

    @property
    def host(self):
        """
        An abbreviated representation of the data that allows comparison with other events.
        For host types, this is a hostname.
        This allows comparison of an email or a URL with a domain, and vice versa
            bob@evilcorp.com        --> evilcorp.com
            https://evilcorp.com    --> evilcorp.com
            evilcorp.com:80         --> evilcorp.com

        For IP_* types, this is an instantiated object representing the event's data
        E.g. for IP_ADDRESS, it could be an ipaddress.IPv4Address() or IPv6Address() object
        """
        if self.__host is None:
            self.__host = self._host()
        return self.__host

    @property
    def port(self):
        self.host
        return self._port

    @property
    def host_stem(self):
        """
        An abbreviated representation of hostname that removes the TLD
            E.g. www.evilcorp.com --> www.evilcorp
        """
        if self.host and type(self.host) == str:
            parsed = tldextract(self.data)
            return f".".join(parsed.subdomain.split(".") + parsed.domain.split(".")).strip(".")
        else:
            return f"{self.host}"

    @property
    def words(self):
        if self.__words is None:
            self.__words = set(self._words())
        return self.__words

    def _words(self):
        return set()

    @property
    def id(self):
        if self._id is None:
            self._id = make_event_id(self.data, self.type)
        return self._id

    @property
    def scope_distance(self):
        return self._scope_distance

    @scope_distance.setter
    def scope_distance(self, scope_distance):
        if scope_distance >= 0:
            new_scope_distance = None
            if self.scope_distance == -1:
                new_scope_distance = scope_distance
            else:
                new_scope_distance = min(self.scope_distance, scope_distance)
            self._scope_distance = new_scope_distance
            for t in list(self.tags):
                if t.startswith("distance-"):
                    self.tags.remove(t)
            self.tags.add(f"distance-{new_scope_distance}")

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, source):
        if is_event(source):
            self._source = source
            if source.scope_distance >= 0:
                new_scope_distance = source.scope_distance + 1
                self.scope_distance = new_scope_distance
            self.source_id = str(source.id)
            if source._omit:
                self.source = source.source
        elif not self._dummy:
            log.warning(f"Must set valid source on {self}: (got: {source})")

    def make_internal(self):
        if not self._made_internal:
            self._internal = True
            self.tags.add("internal")
            self._made_internal = True

    def unmake_internal(self, set_scope_distance=None, force_output=False, emit_trail=True):
        source_trail = []
        if self._made_internal:
            if set_scope_distance is not None:
                self.scope_distance = set_scope_distance
            self._internal = False
            self.tags.remove("internal")
            if force_output:
                self._force_output = True
            self._made_internal = False

        if getattr(self.source, "_internal", False):
            source_scope_distance = None
            if set_scope_distance is not None:
                source_scope_distance = set_scope_distance + 1
            source_trail += self.source.unmake_internal(
                set_scope_distance=source_scope_distance, force_output=force_output
            )
            source_trail.append(self.source)

        if emit_trail and self.scan:
            for e in source_trail:
                self.scan.manager.emit_event(e)

        return source_trail

    def make_in_scope(self):
        source_trail = []
        # keep the event internal if it came from an internal module and isn't a DNS_NAME
        if getattr(self.module, "_type", "") != "internal" or self.type in ("DNS_NAME",):
            source_trail = self.unmake_internal(set_scope_distance=0, force_output=True, emit_trail=True)
        self.tags.add("in_scope")
        self.scope_distance = 0
        return source_trail

    def _host(self):
        return ""

    def _sanitize_data(self, data):
        return data

    def _setup(self):
        """
        Perform optional setup, e.g. adding custom tags
        """

    def __contains__(self, other):
        """
        Allows events to be compared using the "in" operator:
        E.g.:
            if some_event in other_event:
                ...
        """
        try:
            other = make_event(other, dummy=True)
        except ValidationError:
            return False
        # if hashes match
        if other == self:
            return True
        # if hosts match
        if self.host and other.host:
            if self.host == other.host:
                return True
            # hostnames and IPs
            return host_in_host(other.host, self.host)
        return False

    @property
    def json(self):
        j = dict()
        for i in ("type", "data", "id"):
            v = getattr(self, i, "")
            if v:
                j.update({i: v})
        j["scope_distance"] = self.scope_distance
        source = getattr(self, "source_id", "")
        if source:
            j["source"] = source
        if self.tags:
            j.update({"tags": list(self.tags)})
        if self.module:
            j.update({"module": str(self.module)})
        # normalize non-primitive python objects
        for k, v in list(j.items()):
            if type(v) not in (str, int, bool, type(None)):
                try:
                    j[k] = json.dumps(v)
                except Exception:
                    j[k] = smart_decode(v)
        return j

    def __iter__(self):
        yield from self.json.items()

    def __eq__(self, other):
        try:
            other = make_event(other, dummy=True)
        except ValidationError:
            return False
        return hash(self) == hash(other)

    def __hash__(self):
        if self._hash is None:
            self._hash = hash(self.id)
        return self._hash

    def __str__(self):
        d = str(self.data)
        return f'{self.type}("{d[:50]}{("..." if len(d) > 50 else "")}", tags={self.tags})'

    def __repr__(self):
        return str(self)


class DefaultEvent(BaseEvent):
    def _sanitize_data(self, data):
        return data


class IP_ADDRESS(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ip = ipaddress.ip_address(self.data)
        self.tags.add(f"ipv{ip.version}")

    def _sanitize_data(self, data):
        return str(ipaddress.ip_address(str(data)))

    def _host(self):
        return ipaddress.ip_address(self.data)


class IP_RANGE(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        net = ipaddress.ip_network(self.data, strict=False)
        self.tags.add(f"ipv{net.version}")

    def _sanitize_data(self, data):
        return str(ipaddress.ip_network(str(data), strict=False))

    def _host(self):
        return ipaddress.ip_network(self.data)


class DNS_NAME(BaseEvent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if is_subdomain(self.data):
            self.tags.add("subdomain")
        elif is_domain(self.data):
            self.tags.add("domain")

    def _sanitize_data(self, data):
        sanitized = str(data).strip().lower()
        if sanitized != ".":
            sanitized = sanitized.rstrip(".")
        match_1 = any(r.match(sanitized) for r in regexes.event_type_regexes["DNS_NAME"])
        match_2 = regexes._hostname_regex.match(sanitized)
        if match_1 or match_2:
            return sanitized

    def _host(self):
        return self.data

    def _words(self):
        if not "wildcard" in self.tags:
            return extract_words(self.host_stem)
        return set()


class OPEN_TCP_PORT(BaseEvent):
    def _sanitize_data(self, data):
        host, port = split_host_port(data)
        if host and validate_port(port):
            return make_netloc(host, port)

    def _host(self):
        host, self._port = split_host_port(self.data)
        return host

    def _words(self):
        if not is_ip(self.host):
            return extract_words(self.host_stem)
        return set()


class URL_UNVERIFIED(BaseEvent):
    def _sanitize_data(self, data):
        if not any(r.match(data) for r in regexes.event_type_regexes["URL"]):
            return None
        self.parsed = clean_url(data)

        # tag as dir or endpoint
        if str(self.parsed.path.endswith("/")):
            self.tags.add("dir")
        else:
            self.tags.add("endpoint")

        scan = getattr(self, "scan", None)
        if scan is not None:
            for ext in scan.config.get("url_extension_blacklist", []):
                ext = str(ext).lower()
                if str(self.parsed.path).lower().endswith(f".{ext}"):
                    self.tags.add("blacklisted")

        data = self.parsed.geturl()
        return data

    def with_port(self):
        netloc_with_port = make_netloc(self.host, self.port)
        return self.parsed._replace(netloc=netloc_with_port)

    def _host(self):
        return make_ip_type(self.parsed.hostname)

    @property
    def port(self):
        if self.parsed.port is not None:
            return self.parsed.port
        elif self.parsed.scheme == "https":
            return 443
        elif self.parsed.scheme == "http":
            return 80

    def _words(self):
        if not is_ip(self.host):
            return extract_words(self.host_stem)
        return set()


class URL(URL_UNVERIFIED):
    def _sanitize_data(self, data):
        if not self._dummy and not any(t.startswith("status-") for t in self.tags):
            raise ValidationError(
                'Must specify HTTP status tag for URL event, e.g. "status-200". Use URL_UNVERIFIED if the URL is unvisited.'
            )
        return super()._sanitize_data(data)


class URL_HINT(URL_UNVERIFIED):
    pass


class EMAIL_ADDRESS(BaseEvent):
    def _host(self):
        data = str(self.data).split("@")[-1]
        host, self._port = split_host_port(data)
        return host

    def _words(self):
        return extract_words(self.host_stem)


class HTTP_RESPONSE(URL_UNVERIFIED):
    def _sanitize_data(self, data):
        url = data.get("url", "")
        if not any(r.match(url) for r in regexes.event_type_regexes["URL"]):
            return None
        self.parsed = clean_url(url)
        return data

    def _words(self):
        return set()


def make_event(
    data, event_type=None, source=None, module=None, scan=None, tags=None, confidence=100, dummy=False, internal=None
):
    """
    If data is already an event, simply return it
    """

    if is_event(data):
        if scan is not None and not data.scan:
            data.scan = scan
        if module is not None:
            data.module = module
        if source is not None:
            data.set_source(source)
        if internal == True and not data._made_internal:
            data.make_internal()
        event_type = data.type
        return data
    else:
        if event_type is None:
            event_type = get_event_type(data)
            if not dummy:
                log.debug(f'Autodetected event type "{event_type}" based on data: "{data}"')
        if event_type is None:
            raise ValidationError(f'Unable to autodetect event type from "{data}"')

        event_type = str(event_type).strip().upper()

        # Catch these common whoopsies
        # DNS_NAME <--> IP_ADDRESS confusion
        if event_type in ("DNS_NAME", "IP_ADDRESS"):
            data_is_ip = is_ip(data)
            if event_type == "DNS_NAME" and data_is_ip:
                event_type = "IP_ADDRESS"
            elif event_type == "IP_ADDRESS" and not data_is_ip:
                event_type = "DNS_NAME"

        event_class = globals().get(event_type, DefaultEvent)

        return event_class(
            data,
            event_type=event_type,
            source=source,
            module=module,
            scan=scan,
            tags=tags,
            confidence=confidence,
            _dummy=dummy,
            _internal=internal,
        )


def is_event(e):
    return BaseEvent in e.__class__.__mro__
