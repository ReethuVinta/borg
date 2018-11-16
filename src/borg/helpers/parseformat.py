import argparse
import hashlib
import json
import os
import os.path
import re
import socket
import stat
import uuid
from binascii import hexlify
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from functools import partial
from string import Formatter

from ..logger import create_logger
logger = create_logger()

from .errors import Error
from .fs import get_keys_dir
from .time import OutputTimestamp, format_time, to_localtime, safe_timestamp, safe_s
from .. import __version__ as borg_version
from .. import __version_tuple__ as borg_version_tuple
from ..constants import *  # NOQA


def bin_to_hex(binary):
    return hexlify(binary).decode('ascii')


def safe_decode(s, coding='utf-8', errors='surrogateescape'):
    """decode bytes to str, with round-tripping "invalid" bytes"""
    if s is None:
        return None
    return s.decode(coding, errors)


def safe_encode(s, coding='utf-8', errors='surrogateescape'):
    """encode str to bytes, with round-tripping "invalid" bytes"""
    if s is None:
        return None
    return s.encode(coding, errors)


def remove_surrogates(s, errors='replace'):
    """Replace surrogates generated by fsdecode with '?'"""
    return s.encode('utf-8', errors).decode('utf-8')


def decode_dict(d, keys, encoding='utf-8', errors='surrogateescape'):
    for key in keys:
        if isinstance(d.get(key), bytes):
            d[key] = d[key].decode(encoding, errors)
    return d


def positive_int_validator(value):
    """argparse type for positive integers"""
    int_value = int(value)
    if int_value <= 0:
        raise argparse.ArgumentTypeError('A positive integer is required: %s' % value)
    return int_value


def interval(s):
    """Convert a string representing a valid interval to a number of hours."""
    multiplier = {'H': 1, 'd': 24, 'w': 24 * 7, 'm': 24 * 31, 'y': 24 * 365}

    if s.endswith(tuple(multiplier.keys())):
        number = s[:-1]
        suffix = s[-1]
    else:
        # range suffixes in ascending multiplier order
        ranges = [k for k, v in sorted(multiplier.items(), key=lambda t: t[1])]
        raise argparse.ArgumentTypeError(
            'Unexpected interval time unit "%s": expected one of %r' % (s[-1], ranges))

    try:
        hours = int(number) * multiplier[suffix]
    except ValueError:
        hours = -1

    if hours <= 0:
        raise argparse.ArgumentTypeError(
            'Unexpected interval number "%s": expected an integer greater than 0' % number)

    return hours


def timestamp(s):
    """Convert a --timestamp=s argument to a datetime object"""
    try:
        # is it pointing to a file / directory?
        ts = safe_s(os.stat(s).st_mtime)
        return datetime.utcfromtimestamp(ts)
    except OSError:
        # didn't work, try parsing as timestamp. UTC, no TZ, no microsecs support.
        for format in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S+00:00',
                       '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S',
                       '%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M',
                       '%Y-%m-%d', '%Y-%j',
                       ):
            try:
                return datetime.strptime(s, format)
            except ValueError:
                continue
        raise ValueError


def ChunkerParams(s):
    if s.strip().lower() == "default":
        return CHUNKER_PARAMS
    chunk_min, chunk_max, chunk_mask, window_size = s.split(',')
    if int(chunk_max) > 23:
        raise ValueError('max. chunk size exponent must not be more than 23 (2^23 = 8MiB max. chunk size)')
    return int(chunk_min), int(chunk_max), int(chunk_mask), int(window_size)


def FilesCacheMode(s):
    ENTRIES_MAP = dict(ctime='c', mtime='m', size='s', inode='i', rechunk='r', disabled='d')
    VALID_MODES = ('cis', 'ims', 'cs', 'ms', 'cr', 'mr', 'd')  # letters in alpha order
    entries = set(s.strip().split(','))
    if not entries <= set(ENTRIES_MAP):
        raise ValueError('cache mode must be a comma-separated list of: %s' % ','.join(sorted(ENTRIES_MAP)))
    short_entries = {ENTRIES_MAP[entry] for entry in entries}
    mode = ''.join(sorted(short_entries))
    if mode not in VALID_MODES:
        raise ValueError('cache mode short must be one of: %s' % ','.join(VALID_MODES))
    return mode


assert FilesCacheMode(DEFAULT_FILES_CACHE_MODE_UI) == DEFAULT_FILES_CACHE_MODE  # keep these 2 values in sync!


def partial_format(format, mapping):
    """
    Apply format.format_map(mapping) while preserving unknown keys

    Does not support attribute access, indexing and ![rsa] conversions
    """
    for key, value in mapping.items():
        key = re.escape(key)
        format = re.sub(r'(?<!\{)((\{%s\})|(\{%s:[^\}]*\}))' % (key, key),
                        lambda match: match.group(1).format_map(mapping),
                        format)
    return format


class DatetimeWrapper:
    def __init__(self, dt):
        self.dt = dt

    def __format__(self, format_spec):
        if format_spec == '':
            format_spec = ISO_FORMAT_NO_USECS
        return self.dt.__format__(format_spec)


class PlaceholderError(Error):
    """Formatting Error: "{}".format({}): {}({})"""


class InvalidPlaceholder(PlaceholderError):
    """Invalid placeholder "{}" in string: {}"""


def format_line(format, data):
    for _, key, _, conversion in Formatter().parse(format):
        if not key:
            continue
        if conversion or key not in data:
            raise InvalidPlaceholder(key, format)
    try:
        return format.format_map(data)
    except Exception as e:
        raise PlaceholderError(format, data, e.__class__.__name__, str(e))


def replace_placeholders(text):
    """Replace placeholders in text with their values."""
    from ..platform import fqdn, hostname, getosusername
    current_time = datetime.now(timezone.utc)
    data = {
        'pid': os.getpid(),
        'fqdn': fqdn,
        'reverse-fqdn': '.'.join(reversed(fqdn.split('.'))),
        'hostname': hostname,
        'now': DatetimeWrapper(current_time.astimezone(None)),
        'utcnow': DatetimeWrapper(current_time),
        'user': getosusername(),
        'uuid4': str(uuid.uuid4()),
        'borgversion': borg_version,
        'borgmajor': '%d' % borg_version_tuple[:1],
        'borgminor': '%d.%d' % borg_version_tuple[:2],
        'borgpatch': '%d.%d.%d' % borg_version_tuple[:3],
    }
    return format_line(text, data)


PrefixSpec = replace_placeholders


def SortBySpec(text):
    from .manifest import AI_HUMAN_SORT_KEYS
    for token in text.split(','):
        if token not in AI_HUMAN_SORT_KEYS:
            raise ValueError('Invalid sort key: %s' % token)
    return text.replace('timestamp', 'ts')


def format_file_size(v, precision=2, sign=False):
    """Format file size into a human friendly format
    """
    return sizeof_fmt_decimal(v, suffix='B', sep=' ', precision=precision, sign=sign)


class FileSize(int):
    def __format__(self, format_spec):
        return format_file_size(int(self)).__format__(format_spec)


def parse_file_size(s):
    """Return int from file size (1234, 55G, 1.7T)."""
    if not s:
        return int(s)  # will raise
    suffix = s[-1]
    power = 1000
    try:
        factor = {
            'K': power,
            'M': power**2,
            'G': power**3,
            'T': power**4,
            'P': power**5,
        }[suffix]
        s = s[:-1]
    except KeyError:
        factor = 1
    return int(float(s) * factor)


def sizeof_fmt(num, suffix='B', units=None, power=None, sep='', precision=2, sign=False):
    prefix = '+' if sign and num > 0 else ''

    for unit in units[:-1]:
        if abs(round(num, precision)) < power:
            if isinstance(num, int):
                return "{}{}{}{}{}".format(prefix, num, sep, unit, suffix)
            else:
                return "{}{:3.{}f}{}{}{}".format(prefix, num, precision, sep, unit, suffix)
        num /= float(power)
    return "{}{:.{}f}{}{}{}".format(prefix, num, precision, sep, units[-1], suffix)


def sizeof_fmt_iec(num, suffix='B', sep='', precision=2, sign=False):
    return sizeof_fmt(num, suffix=suffix, sep=sep, precision=precision, sign=sign,
                      units=['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi', 'Yi'], power=1024)


def sizeof_fmt_decimal(num, suffix='B', sep='', precision=2, sign=False):
    return sizeof_fmt(num, suffix=suffix, sep=sep, precision=precision, sign=sign,
                      units=['', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y'], power=1000)


def format_archive(archive):
    return '%-36s %s [%s]' % (
        archive.name,
        format_time(to_localtime(archive.ts)),
        bin_to_hex(archive.id),
    )


def parse_stringified_list(s):
    l = re.split(" *, *", s)
    return [item for item in l if item != '']


class Location:
    """Object representing a repository / archive location
    """
    proto = user = _host = port = path = archive = None

    # user must not contain "@", ":" or "/".
    # Quoting adduser error message:
    # "To avoid problems, the username should consist only of letters, digits,
    # underscores, periods, at signs and dashes, and not start with a dash
    # (as defined by IEEE Std 1003.1-2001)."
    # We use "@" as separator between username and hostname, so we must
    # disallow it within the pure username part.
    optional_user_re = r"""
        (?:(?P<user>[^@:/]+)@)?
    """

    # path must not contain :: (it ends at :: or string end), but may contain single colons.
    # to avoid ambiguities with other regexes, it must also not start with ":" nor with "//" nor with "ssh://".
    scp_path_re = r"""
        (?!(:|//|ssh://))                                   # not starting with ":" or // or ssh://
        (?P<path>([^:]|(:(?!:)))+)                          # any chars, but no "::"
        """

    # file_path must not contain :: (it ends at :: or string end), but may contain single colons.
    # it must start with a / and that slash is part of the path.
    file_path_re = r"""
        (?P<path>(([^/]*)/([^:]|(:(?!:)))+))                # start opt. servername, then /, then any chars, but no "::"
        """

    # abs_path must not contain :: (it ends at :: or string end), but may contain single colons.
    # it must start with a / and that slash is part of the path.
    abs_path_re = r"""
        (?P<path>(/([^:]|(:(?!:)))+))                       # start with /, then any chars, but no "::"
        """

    # optional ::archive_name at the end, archive name must not contain "/".
    # borg mount's FUSE filesystem creates one level of directories from
    # the archive names and of course "/" is not valid in a directory name.
    optional_archive_re = r"""
        (?:
            ::                                              # "::" as separator
            (?P<archive>[^/]+)                              # archive name must not contain "/"
        )?$"""                                              # must match until the end

    # regexes for misc. kinds of supported location specifiers:
    ssh_re = re.compile(r"""
        (?P<proto>ssh)://                                   # ssh://
        """ + optional_user_re + r"""                       # user@  (optional)
        (?P<host>([^:/]+|\[[0-9a-fA-F:.]+\]))(?::(?P<port>\d+))?  # host or host:port or [ipv6] or [ipv6]:port
        """ + abs_path_re + optional_archive_re, re.VERBOSE)  # path or path::archive

    file_re = re.compile(r"""
        (?P<proto>file)://                                  # file://
        """ + file_path_re + optional_archive_re, re.VERBOSE)  # servername/path, path or path::archive

    # note: scp_re is also use for local paths
    scp_re = re.compile(r"""
        (
            """ + optional_user_re + r"""                   # user@  (optional)
            (?P<host>([^:/]+|\[[0-9a-fA-F:.]+\])):          # host: (don't match / or [ipv6] in host to disambiguate from file:)
        )?                                                  # user@host: part is optional
        """ + scp_path_re + optional_archive_re, re.VERBOSE)  # path with optional archive

    # get the repo from BORG_REPO env and the optional archive from param.
    # if the syntax requires giving REPOSITORY (see "borg mount"),
    # use "::" to let it use the env var.
    # if REPOSITORY argument is optional, it'll automatically use the env.
    env_re = re.compile(r"""                                # the repo part is fetched from BORG_REPO
        (?:::$)                                             # just "::" is ok (when a pos. arg is required, no archive)
        |                                                   # or
        """ + optional_archive_re, re.VERBOSE)              # archive name (optional, may be empty)

    def __init__(self, text=''):
        self.orig = text
        if not self.parse(self.orig):
            raise ValueError('Location: parse failed: %s' % self.orig)

    def parse(self, text):
        text = replace_placeholders(text)
        valid = self._parse(text)
        if valid:
            return True
        m = self.env_re.match(text)
        if not m:
            return False
        repo = os.environ.get('BORG_REPO')
        if repo is None:
            return False
        valid = self._parse(repo)
        if not valid:
            return False
        self.archive = m.group('archive')
        return True

    def _parse(self, text):
        def normpath_special(p):
            # avoid that normpath strips away our relative path hack and even makes p absolute
            relative = p.startswith('/./')
            p = os.path.normpath(p)
            return ('/.' + p) if relative else p

        m = self.ssh_re.match(text)
        if m:
            self.proto = m.group('proto')
            self.user = m.group('user')
            self._host = m.group('host')
            self.port = m.group('port') and int(m.group('port')) or None
            self.path = normpath_special(m.group('path'))
            self.archive = m.group('archive')
            return True
        m = self.file_re.match(text)
        if m:
            self.proto = m.group('proto')
            self.path = normpath_special(m.group('path'))
            self.archive = m.group('archive')
            return True
        m = self.scp_re.match(text)
        if m:
            self.user = m.group('user')
            self._host = m.group('host')
            self.path = normpath_special(m.group('path'))
            self.archive = m.group('archive')
            self.proto = self._host and 'ssh' or 'file'
            return True
        return False

    def __str__(self):
        items = [
            'proto=%r' % self.proto,
            'user=%r' % self.user,
            'host=%r' % self.host,
            'port=%r' % self.port,
            'path=%r' % self.path,
            'archive=%r' % self.archive,
        ]
        return ', '.join(items)

    def to_key_filename(self):
        name = re.sub(r'[^\w]', '_', self.path).strip('_')
        if self.proto != 'file':
            name = re.sub(r'[^\w]', '_', self.host) + '__' + name
        if len(name) > 100:
            # Limit file names to some reasonable length. Most file systems
            # limit them to 255 [unit of choice]; due to variations in unicode
            # handling we truncate to 100 *characters*.
            name = name[:100]
        return os.path.join(get_keys_dir(), name)

    def __repr__(self):
        return "Location(%s)" % self

    @property
    def host(self):
        # strip square brackets used for IPv6 addrs
        if self._host is not None:
            return self._host.lstrip('[').rstrip(']')

    def canonical_path(self):
        if self.proto == 'file':
            return self.path
        else:
            if self.path and self.path.startswith('~'):
                path = '/' + self.path  # /~/x = path x relative to home dir
            elif self.path and not self.path.startswith('/'):
                path = '/./' + self.path  # /./x = path x relative to cwd
            else:
                path = self.path
            return 'ssh://{}{}{}{}'.format('{}@'.format(self.user) if self.user else '',
                                           self._host,  # needed for ipv6 addrs
                                           ':{}'.format(self.port) if self.port else '',
                                           path)


def location_validator(archive=None, proto=None):
    def validator(text):
        try:
            loc = Location(text)
        except ValueError:
            raise argparse.ArgumentTypeError('Invalid location format: "%s"' % text) from None
        if archive is True and not loc.archive:
            raise argparse.ArgumentTypeError('"%s": No archive specified' % text)
        elif archive is False and loc.archive:
            raise argparse.ArgumentTypeError('"%s": No archive can be specified' % text)
        if proto is not None and loc.proto != proto:
            if proto == 'file':
                raise argparse.ArgumentTypeError('"%s": Repository must be local' % text)
            else:
                raise argparse.ArgumentTypeError('"%s": Repository must be remote' % text)
        return loc
    return validator


def archivename_validator():
    def validator(text):
        if '/' in text or '::' in text or not text:
            raise argparse.ArgumentTypeError('Invalid repository name: "%s"' % text)
        return text
    return validator


class BaseFormatter:
    FIXED_KEYS = {
        # Formatting aids
        'LF': '\n',
        'SPACE': ' ',
        'TAB': '\t',
        'CR': '\r',
        'NUL': '\0',
        'NEWLINE': os.linesep,
        'NL': os.linesep,
    }

    def get_item_data(self, item):
        raise NotImplementedError

    def format_item(self, item):
        return self.format.format_map(self.get_item_data(item))

    @staticmethod
    def keys_help():
        return "- NEWLINE: OS dependent line separator\n" \
               "- NL: alias of NEWLINE\n" \
               "- NUL: NUL character for creating print0 / xargs -0 like output, see barchive/bpath\n" \
               "- SPACE\n" \
               "- TAB\n" \
               "- CR\n" \
               "- LF"


class ArchiveFormatter(BaseFormatter):
    KEY_DESCRIPTIONS = {
        'archive': 'archive name interpreted as text (might be missing non-text characters, see barchive)',
        'name': 'alias of "archive"',
        'barchive': 'verbatim archive name, can contain any character except NUL',
        'comment': 'archive comment interpreted as text (might be missing non-text characters, see bcomment)',
        'bcomment': 'verbatim archive comment, can contain any character except NUL',
        # *start* is the key used by borg-info for this timestamp, this makes the formats more compatible
        'start': 'time (start) of creation of the archive',
        'time': 'alias of "start"',
        'end': 'time (end) of creation of the archive',
        'id': 'internal ID of the archive',
    }
    KEY_GROUPS = (
        ('archive', 'name', 'barchive', 'comment', 'bcomment', 'id'),
        ('start', 'time', 'end'),
    )

    @classmethod
    def available_keys(cls):
        from .manifest import ArchiveInfo
        fake_archive_info = ArchiveInfo('archivename', b'\1'*32, datetime(1970, 1, 1, tzinfo=timezone.utc))
        formatter = cls('', None, None, None)
        keys = []
        keys.extend(formatter.call_keys.keys())
        keys.extend(formatter.get_item_data(fake_archive_info).keys())
        return keys

    @classmethod
    def keys_help(cls):
        help = []
        keys = cls.available_keys()
        for key in cls.FIXED_KEYS:
            keys.remove(key)

        for group in cls.KEY_GROUPS:
            for key in group:
                keys.remove(key)
                text = "- " + key
                if key in cls.KEY_DESCRIPTIONS:
                    text += ": " + cls.KEY_DESCRIPTIONS[key]
                help.append(text)
            help.append("")
        assert not keys, str(keys)
        return "\n".join(help)

    def __init__(self, format, repository, manifest, key, *, json=False):
        self.repository = repository
        self.manifest = manifest
        self.key = key
        self.name = None
        self.id = None
        self._archive = None
        self.json = json
        static_keys = {}  # here could be stuff on repo level, above archive level
        static_keys.update(self.FIXED_KEYS)
        self.format = partial_format(format, static_keys)
        self.format_keys = {f[1] for f in Formatter().parse(format)}
        self.call_keys = {
            'comment': partial(self.get_comment, rs=True),
            'bcomment': partial(self.get_comment, rs=False),
            'end': self.get_ts_end,
        }
        self.used_call_keys = set(self.call_keys) & self.format_keys
        if self.json:
            self.item_data = {}
            self.format_item = self.format_item_json
        else:
            self.item_data = static_keys

    def format_item_json(self, item):
        return json.dumps(self.get_item_data(item), cls=BorgJsonEncoder) + '\n'

    def get_item_data(self, archive_info):
        self.name = archive_info.name
        self.id = archive_info.id
        item_data = {}
        item_data.update(self.item_data)
        item_data.update({
            'name': remove_surrogates(archive_info.name),
            'archive': remove_surrogates(archive_info.name),
            'barchive': archive_info.name,
            'id': bin_to_hex(archive_info.id),
            'time': self.format_time(archive_info.ts),
            'start': self.format_time(archive_info.ts),
        })
        for key in self.used_call_keys:
            item_data[key] = self.call_keys[key]()
        return item_data

    @property
    def archive(self):
        """lazy load / update loaded archive"""
        if self._archive is None or self._archive.id != self.id:
            from ..archive import Archive
            self._archive = Archive(self.repository, self.key, self.manifest, self.name)
        return self._archive

    def get_comment(self, rs):
        return remove_surrogates(self.archive.comment) if rs else self.archive.comment

    def get_ts_end(self):
        return self.format_time(self.archive.ts_end)

    def format_time(self, ts):
        return OutputTimestamp(ts)


class ItemFormatter(BaseFormatter):
    KEY_DESCRIPTIONS = {
        'bpath': 'verbatim POSIX path, can contain any character except NUL',
        'path': 'path interpreted as text (might be missing non-text characters, see bpath)',
        'source': 'link target for links (identical to linktarget)',
        'extra': 'prepends {source} with " -> " for soft links and " link to " for hard links',
        'csize': 'compressed size',
        'dsize': 'deduplicated size',
        'dcsize': 'deduplicated compressed size',
        'num_chunks': 'number of chunks in this file',
        'unique_chunks': 'number of unique chunks in this file',
        'health': 'either "healthy" (file ok) or "broken" (if file has all-zero replacement chunks)',
    }
    KEY_GROUPS = (
        ('type', 'mode', 'uid', 'gid', 'user', 'group', 'path', 'bpath', 'source', 'linktarget', 'flags'),
        ('size', 'csize', 'dsize', 'dcsize', 'num_chunks', 'unique_chunks'),
        ('mtime', 'ctime', 'atime', 'isomtime', 'isoctime', 'isoatime'),
        tuple(sorted(hashlib.algorithms_guaranteed)),
        ('archiveid', 'archivename', 'extra'),
        ('health', )
    )

    KEYS_REQUIRING_CACHE = (
        'dsize', 'dcsize', 'unique_chunks',
    )

    @classmethod
    def available_keys(cls):
        class FakeArchive:
            fpr = name = ""

        from ..item import Item
        fake_item = Item(mode=0, path='', user='', group='', mtime=0, uid=0, gid=0)
        formatter = cls(FakeArchive, "")
        keys = []
        keys.extend(formatter.call_keys.keys())
        keys.extend(formatter.get_item_data(fake_item).keys())
        return keys

    @classmethod
    def keys_help(cls):
        help = []
        keys = cls.available_keys()
        for key in cls.FIXED_KEYS:
            keys.remove(key)

        for group in cls.KEY_GROUPS:
            for key in group:
                keys.remove(key)
                text = "- " + key
                if key in cls.KEY_DESCRIPTIONS:
                    text += ": " + cls.KEY_DESCRIPTIONS[key]
                help.append(text)
            help.append("")
        assert not keys, str(keys)
        return "\n".join(help)

    @classmethod
    def format_needs_cache(cls, format):
        format_keys = {f[1] for f in Formatter().parse(format)}
        return any(key in cls.KEYS_REQUIRING_CACHE for key in format_keys)

    def __init__(self, archive, format, *, json_lines=False):
        self.archive = archive
        self.json_lines = json_lines
        static_keys = {
            'archivename': archive.name,
            'archiveid': archive.fpr,
        }
        static_keys.update(self.FIXED_KEYS)
        if self.json_lines:
            self.item_data = {}
            self.format_item = self.format_item_json
        else:
            self.item_data = static_keys
        self.format = partial_format(format, static_keys)
        self.format_keys = {f[1] for f in Formatter().parse(format)}
        self.call_keys = {
            'size': self.calculate_size,
            'csize': self.calculate_csize,
            'dsize': partial(self.sum_unique_chunks_metadata, lambda chunk: chunk.size),
            'dcsize': partial(self.sum_unique_chunks_metadata, lambda chunk: chunk.csize),
            'num_chunks': self.calculate_num_chunks,
            'unique_chunks': partial(self.sum_unique_chunks_metadata, lambda chunk: 1),
            'isomtime': partial(self.format_iso_time, 'mtime'),
            'isoctime': partial(self.format_iso_time, 'ctime'),
            'isoatime': partial(self.format_iso_time, 'atime'),
            'mtime': partial(self.format_time, 'mtime'),
            'ctime': partial(self.format_time, 'ctime'),
            'atime': partial(self.format_time, 'atime'),
        }
        for hash_function in hashlib.algorithms_guaranteed:
            self.add_key(hash_function, partial(self.hash_item, hash_function))
        self.used_call_keys = set(self.call_keys) & self.format_keys

    def format_item_json(self, item):
        return json.dumps(self.get_item_data(item), cls=BorgJsonEncoder) + '\n'

    def add_key(self, key, callable_with_item):
        self.call_keys[key] = callable_with_item
        self.used_call_keys = set(self.call_keys) & self.format_keys

    def get_item_data(self, item):
        item_data = {}
        item_data.update(self.item_data)
        mode = stat.filemode(item.mode)
        item_type = mode[0]

        source = item.get('source', '')
        extra = ''
        if source:
            source = remove_surrogates(source)
            if item_type == 'l':
                extra = ' -> %s' % source
            else:
                mode = 'h' + mode[1:]
                extra = ' link to %s' % source
        item_data['type'] = item_type
        item_data['mode'] = mode
        item_data['user'] = item.user or item.uid
        item_data['group'] = item.group or item.gid
        item_data['uid'] = item.uid
        item_data['gid'] = item.gid
        item_data['path'] = remove_surrogates(item.path)
        if self.json_lines:
            item_data['healthy'] = 'chunks_healthy' not in item
        else:
            item_data['bpath'] = item.path
            item_data['extra'] = extra
            item_data['health'] = 'broken' if 'chunks_healthy' in item else 'healthy'
        item_data['source'] = source
        item_data['linktarget'] = source
        item_data['flags'] = item.get('bsdflags')
        for key in self.used_call_keys:
            item_data[key] = self.call_keys[key](item)
        return item_data

    def sum_unique_chunks_metadata(self, metadata_func, item):
        """
        sum unique chunks metadata, a unique chunk is a chunk which is referenced globally as often as it is in the
        item

        item: The item to sum its unique chunks' metadata
        metadata_func: A function that takes a parameter of type ChunkIndexEntry and returns a number, used to return
                       the metadata needed from the chunk
        """
        chunk_index = self.archive.cache.chunks
        chunks = item.get('chunks', [])
        chunks_counter = Counter(c.id for c in chunks)
        return sum(metadata_func(c) for c in chunks if chunk_index[c.id].refcount == chunks_counter[c.id])

    def calculate_num_chunks(self, item):
        return len(item.get('chunks', []))

    def calculate_size(self, item):
        # note: does not support hardlink slaves, they will be size 0
        return item.get_size(compressed=False)

    def calculate_csize(self, item):
        # note: does not support hardlink slaves, they will be csize 0
        return item.get_size(compressed=True)

    def hash_item(self, hash_function, item):
        if 'chunks' not in item:
            return ""
        hash = hashlib.new(hash_function)
        for data in self.archive.pipeline.fetch_many([c.id for c in item.chunks]):
            hash.update(data)
        return hash.hexdigest()

    def format_time(self, key, item):
        return OutputTimestamp(safe_timestamp(item.get(key) or item.mtime))

    def format_iso_time(self, key, item):
        return self.format_time(key, item).isoformat()


def file_status(mode):
    if stat.S_ISREG(mode):
        return 'A'
    elif stat.S_ISDIR(mode):
        return 'd'
    elif stat.S_ISBLK(mode):
        return 'b'
    elif stat.S_ISCHR(mode):
        return 'c'
    elif stat.S_ISLNK(mode):
        return 's'
    elif stat.S_ISFIFO(mode):
        return 'f'
    return '?'


def clean_lines(lines, lstrip=None, rstrip=None, remove_empty=True, remove_comments=True):
    """
    clean lines (usually read from a config file):

    1. strip whitespace (left and right), 2. remove empty lines, 3. remove comments.

    note: only "pure comment lines" are supported, no support for "trailing comments".

    :param lines: input line iterator (e.g. list or open text file) that gives unclean input lines
    :param lstrip: lstrip call arguments or False, if lstripping is not desired
    :param rstrip: rstrip call arguments or False, if rstripping is not desired
    :param remove_comments: remove comment lines (lines starting with "#")
    :param remove_empty: remove empty lines
    :return: yields processed lines
    """
    for line in lines:
        if lstrip is not False:
            line = line.lstrip(lstrip)
        if rstrip is not False:
            line = line.rstrip(rstrip)
        if remove_empty and not line:
            continue
        if remove_comments and line.startswith('#'):
            continue
        yield line


def swidth_slice(string, max_width):
    """
    Return a slice of *max_width* cells from *string*.

    Negative *max_width* means from the end of string.

    *max_width* is in units of character cells (or "columns").
    Latin characters are usually one cell wide, many CJK characters are two cells wide.
    """
    from ..platform import swidth
    reverse = max_width < 0
    max_width = abs(max_width)
    if reverse:
        string = reversed(string)
    current_swidth = 0
    result = []
    for character in string:
        current_swidth += swidth(character)
        if current_swidth > max_width:
            break
        result.append(character)
    if reverse:
        result.reverse()
    return ''.join(result)


def ellipsis_truncate(msg, space):
    """
    shorten a long string by adding ellipsis between it and return it, example:
    this_is_a_very_long_string -------> this_is..._string
    """
    from ..platform import swidth
    ellipsis_width = swidth('...')
    msg_width = swidth(msg)
    if space < 8:
        # if there is very little space, just show ...
        return '...' + ' ' * (space - ellipsis_width)
    if space < ellipsis_width + msg_width:
        return '%s...%s' % (swidth_slice(msg, space // 2 - ellipsis_width),
                            swidth_slice(msg, -space // 2))
    return msg + ' ' * (space - msg_width)


class BorgJsonEncoder(json.JSONEncoder):
    def default(self, o):
        from ..repository import Repository
        from ..remote import RemoteRepository
        from ..archive import Archive
        from ..cache import LocalCache, AdHocCache
        if isinstance(o, Repository) or isinstance(o, RemoteRepository):
            return {
                'id': bin_to_hex(o.id),
                'location': o._location.canonical_path(),
            }
        if isinstance(o, Archive):
            return o.info()
        if isinstance(o, LocalCache):
            return {
                'path': o.path,
                'stats': o.stats(),
            }
        if isinstance(o, AdHocCache):
            return {
                'stats': o.stats(),
            }
        if callable(getattr(o, 'to_json', None)):
            return o.to_json()
        return super().default(o)


def basic_json_data(manifest, *, cache=None, extra=None):
    key = manifest.key
    data = extra or {}
    data.update({
        'repository': BorgJsonEncoder().default(manifest.repository),
        'encryption': {
            'mode': key.ARG_NAME,
        },
    })
    data['repository']['last_modified'] = OutputTimestamp(manifest.last_timestamp.replace(tzinfo=timezone.utc))
    if key.NAME.startswith('key file'):
        data['encryption']['keyfile'] = key.find_key()
    if cache:
        data['cache'] = cache
    return data


def json_dump(obj):
    """Dump using BorgJSONEncoder."""
    return json.dumps(obj, sort_keys=True, indent=4, cls=BorgJsonEncoder)


def json_print(obj):
    print(json_dump(obj))


def prepare_dump_dict(d):
    def decode_bytes(value):
        # this should somehow be reversible later, but usual strings should
        # look nice and chunk ids should mostly show in hex. Use a special
        # inband signaling character (ASCII DEL) to distinguish between
        # decoded and hex mode.
        if not value.startswith(b'\x7f'):
            try:
                value = value.decode()
                return value
            except UnicodeDecodeError:
                pass
        return '\u007f' + bin_to_hex(value)

    def decode_tuple(t):
        res = []
        for value in t:
            if isinstance(value, dict):
                value = decode(value)
            elif isinstance(value, tuple) or isinstance(value, list):
                value = decode_tuple(value)
            elif isinstance(value, bytes):
                value = decode_bytes(value)
            res.append(value)
        return res

    def decode(d):
        res = OrderedDict()
        for key, value in d.items():
            if isinstance(value, dict):
                value = decode(value)
            elif isinstance(value, (tuple, list)):
                value = decode_tuple(value)
            elif isinstance(value, bytes):
                value = decode_bytes(value)
            if isinstance(key, bytes):
                key = key.decode()
            res[key] = value
        return res

    return decode(d)