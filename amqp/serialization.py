"""
Convert between bytestreams and higher-level AMQP types.

2007-11-05 Barry Pederson <bp@barryp.org>

"""
# Copyright (C) 2007 Barry Pederson <bp@barryp.org>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301
from __future__ import absolute_import

import sys

from datetime import datetime
from decimal import Decimal
from io import BytesIO
from struct import pack, unpack
from time import mktime

from .exceptions import FrameSyntaxError
from .five import int_types, long_t, string, string_t, items

IS_PY3K = sys.version_info[0] >= 3

if IS_PY3K:
    def byte(n):
        return bytes([n])
else:
    byte = chr


ILLEGAL_TABLE_TYPE_WITH_KEY = """\
Table type {0!r} for key {1!r} not handled by amqp. [value: {2!r}]
"""

ILLEGAL_TABLE_TYPE = """\
    Table type {0!r} not handled by amqp. [value: {1!r}]
"""


class AMQPReader(object):
    """Read higher-level AMQP types from a bytestream."""
    def __init__(self, source):
        """Source should be either a file-like object with a read() method, or
        a plain (non-unicode) string."""
        if isinstance(source, bytes):
            self.input = BytesIO(source)
        elif hasattr(source, 'read'):
            self.input = source
        else:
            raise ValueError(
                'AMQPReader needs a file-like object or plain string')

        self.bitcount = self.bits = 0

    def close(self):
        self.input.close()

    def read(self, n):
        """Read n bytes."""
        self.bitcount = self.bits = 0
        return self.input.read(n)

    def read_bit(self):
        """Read a single boolean value."""
        if not self.bitcount:
            self.bits = ord(self.input.read(1))
            self.bitcount = 8
        result = (self.bits & 1) == 1
        self.bits >>= 1
        self.bitcount -= 1
        return result

    def read_octet(self):
        """Read one byte, return as an integer"""
        self.bitcount = self.bits = 0
        return unpack('B', self.input.read(1))[0]

    def read_short(self):
        """Read an unsigned 16-bit integer"""
        self.bitcount = self.bits = 0
        return unpack('>H', self.input.read(2))[0]

    def read_long(self):
        """Read an unsigned 32-bit integer"""
        self.bitcount = self.bits = 0
        return unpack('>I', self.input.read(4))[0]

    def read_longlong(self):
        """Read an unsigned 64-bit integer"""
        self.bitcount = self.bits = 0
        return unpack('>Q', self.input.read(8))[0]

    def read_float(self):
        """Read float value."""
        self.bitcount = self.bits = 0
        return unpack('>d', self.input.read(8))[0]

    def read_shortstr(self):
        """Read a short string that's stored in up to 255 bytes.

        The encoding isn't specified in the AMQP spec, so
        assume it's utf-8

        """
        self.bitcount = self.bits = 0
        slen = unpack('B', self.input.read(1))[0]
        return self.input.read(slen).decode('utf-8')

    def read_longstr(self):
        """Read a string that's up to 2**32 bytes.

        The encoding isn't specified in the AMQP spec, so
        assume it's utf-8

        """
        self.bitcount = self.bits = 0
        slen = unpack('>I', self.input.read(4))[0]
        return self.input.read(slen).decode('utf-8')

    def read_table(self):
        """Read an AMQP table, and return as a Python dictionary."""
        self.bitcount = self.bits = 0
        tlen = unpack('>I', self.input.read(4))[0]
        table_data = AMQPReader(self.input.read(tlen))
        result = {}
        while table_data.input.tell() < tlen:
            name = table_data.read_shortstr()
            val = table_data.read_item()
            result[name] = val
        return result

    def read_item(self, ord=ord):
        ftype = ord(self.input.read(1))

        # 'S': long string
        if ftype == 83:
            val = self.read_longstr()
        # 's': short string
        elif ftype == 115:
            val = self.read_shortstr()
        # 'b': short-short int
        elif ftype == 98:
            val, = unpack('>B', self.input.read(1))
        # 'B': short-short unsigned int
        elif ftype == 66:
            val, = unpack('>b', self.input.read(1))
        # 'U': short int
        elif ftype == 85:
            val, = unpack('>h', self.input.read(2))
        # 'u': short unsigned int
        elif ftype == 117:
            val, = unpack('>H', self.input.read(2))
        # 'I': long int
        elif ftype == 73:
            val, = unpack('>i', self.input.read(4))
        # 'i': long unsigned int
        elif ftype == 105:  # 'l'
            val, = unpack('>I', self.input.read(4))
        # 'L': long long int
        elif ftype == 76:
            val, = unpack('>q', self.input.read(8))
        # 'l': long long unsigned int
        elif ftype == 108:
            val, = unpack('>Q', self.input.read(8))
        # 'f': float
        elif ftype == 102:
            val, = unpack('>f', self.input.read(4))
        # 'd': double
        elif ftype == 100:
            val = self.read_float()
        # 'D': decimal
        elif ftype == 68:
            d = self.read_octet()
            n, = unpack('>i', self.input.read(4))
            val = Decimal(n) / Decimal(10 ** d)
        # 'F': table
        elif ftype == 70:
            val = self.read_table()  # recurse
        # 'A': array
        elif ftype == 65:
            val = self.read_array()
        # 't' (bool)
        elif ftype == 116:
            val = self.read_bit()
        # 'T': timestamp
        elif ftype == 84:
            val = self.read_timestamp()
        # 'V': void
        elif ftype == 86:
            val = None
        else:
            raise FrameSyntaxError(
                'Unknown value in table: {0!r} ({1!r})'.format(
                    ftype, type(ftype)))
        return val

    def read_array(self):
        array_length = unpack('>I', self.input.read(4))[0]
        array_data = AMQPReader(self.input.read(array_length))
        result = []
        while array_data.input.tell() < array_length:
            val = array_data.read_item()
            result.append(val)
        return result

    def read_timestamp(self):
        """Read and AMQP timestamp, which is a 64-bit integer representing
        seconds since the Unix epoch in 1-second resolution.

        Return as a Python datetime.datetime object,
        expressed as localtime.

        """
        return datetime.fromtimestamp(self.read_longlong())


def _flushbits(bits, write, pack=pack):
    if bits:
        write(pack('B' * len(bits), *bits))
        bits[:] = []
    return 0


def dumps(format, values):
    """"
    bit = b
    octet = o
    short = B
    long = l
    long long = L
    shortstr = s
    longstr = S
    table = F
    array = A

    """
    bitcount = 0
    bits = []
    out = BytesIO()
    write = out.write

    for i, val in enumerate(values):
        p = format[i]
        if p == 'b':
            val = 1 if val else 0
            shift = bitcount % 8
            if shift == 0:
                bits.append(0)
            bits[-1] |= (val << shift)
            bitcount += 1
        if p == 'o':
            bitcount = _flushbits(bits, write)
            write(pack('B', val))
        if p == 'B':
            bitcount = _flushbits(bits, write)
            write(pack('>H', int(val)))
        if p == 'l':
            bitcount = _flushbits(bits, write)
            write(pack('>I', val))
        if p == 'L':
            bitcount = _flushbits(bits, write)
            write(pack('>Q', val))
        if p == 's':
            val = val or ''
            bitcount = _flushbits(bits, write)
            if isinstance(val, string):
                val = val.encode('utf-8')
            write(pack('B', len(val)))
            write(val)
        if p == 'S':
            val = val or ''
            bitcount = _flushbits(bits, write)
            if isinstance(val, string):
                val = val.encode('utf-8')
            write(pack('>I', len(val)))
            write(val)
        if p == 'F':
            bitcount = _flushbits(bits, write)
            _write_table(val or {}, write, bits)
        if p == 'A':
            bitcount = _flushbits(bits, write)
            _write_array(val or [], write, bits)
        if p == 'T':
            write(pack('>q', long_t(mktime(val.timetuple()))))
    _flushbits(bits, write)

    return out.getvalue()


def _write_table(d, write, bits, pack=pack):
    out = BytesIO()
    twrite = out.write
    for k, v in items(d):
        if isinstance(k, string):
            k = k.encode('utf-8')
        twrite(pack('B', len(k)))
        twrite(k)
        try:
            _write_item(v, twrite, bits)
        except ValueError:
            raise FrameSyntaxError(
                ILLEGAL_TABLE_TYPE_WITH_KEY.format(type(v), k, v))
    table_data = out.getvalue()
    write(pack('>I', len(table_data)))
    write(table_data)


def _write_array(l, write, bits, pack=pack):
    out = BytesIO()
    awrite = out.write
    for v in l:
        try:
            _write_item(v, awrite, bits)
        except ValueError:
            raise FrameSyntaxError(
                ILLEGAL_TABLE_TYPE.format(type(v), v))
    array_data = out.getvalue()
    write(pack('>I', len(array_data)))
    write(array_data)


def _write_item(v, write, bits, pack=pack,
                string_t=string_t, bytes=bytes, string=string, bool=bool,
                float=float, int_types=int_types, Decimal=Decimal,
                datetime=datetime, dict=dict, list=list, tuple=tuple,
                None_t=None):
    if isinstance(v, (string_t, bytes)):
        if isinstance(v, string):
            v = v.encode('utf-8')
        write(pack('>cI', b'S', len(v)))
        write(v)
    elif isinstance(v, bool):
        write(pack('>cB', b't', int(v)))
    elif isinstance(v, float):
        write(pack('>cd', b'd', v))
    elif isinstance(v, int_types):
        write(pack('>ci', b'I', v))
    elif isinstance(v, Decimal):
        sign, digits, exponent = v.as_tuple()
        v = 0
        for d in digits:
            v = (v * 10) + d
        if sign:
            v = -v
        write('>cBi', b'D', -exponent, v)
    elif isinstance(v, datetime):
        # ## FIXME timezone ?
        write(pack('>cq', b'T', long_t(mktime(v.timetuple()))))
    elif isinstance(v, dict):
        write(b'F')
        _write_table(v, write, bits)
    elif isinstance(v, (list, tuple)):
        write(b'A')
        _write_array(v, write, bits)
    elif v is None_t:
        write(b'V')
    else:
        raise ValueError()


class GenericContent(object):
    """Abstract base class for AMQP content.

    Subclasses should override the PROPERTIES attribute.

    """
    PROPERTIES = [('dummy', 's')]

    def __init__(self, **props):
        """Save the properties appropriate to this AMQP content type
        in a 'properties' dictionary."""
        d = {}
        for propname, _ in self.PROPERTIES:
            if propname in props:
                d[propname] = props[propname]
            # FIXME: should we ignore unknown properties?

        self.properties = d

    def __eq__(self, other):
        """Check if this object has the same properties as another
        content object."""
        try:
            return self.properties == other.properties
        except AttributeError:
            return NotImplemented

    def __getattr__(self, name):
        """Look for additional properties in the 'properties'
        dictionary, and if present - the 'delivery_info'
        dictionary."""
        if name == '__setstate__':
            # Allows pickling/unpickling to work
            raise AttributeError('__setstate__')

        if name in self.properties:
            return self.properties[name]

        if 'delivery_info' in self.__dict__ \
                and name in self.delivery_info:
            return self.delivery_info[name]

        raise AttributeError(name)

    def _load_properties(self, raw_bytes):
        """Given the raw bytes containing the property-flags and property-list
        from a content-frame-header, parse and insert into a dictionary
        stored in this object as an attribute named 'properties'."""
        r = AMQPReader(raw_bytes)

        spec_to_m = {
            's': r.read_shortstr,
            'F': r.read_table,
            'o': r.read_octet,
            'T': r.read_timestamp,
        }

        #
        # Read 16-bit shorts until we get one with a low bit set to zero
        #
        flags = []
        while 1:
            flag_bits = r.read_short()
            flags.append(flag_bits)
            if flag_bits & 1 == 0:
                break

        shift = 0
        d = {}
        for key, proptype in self.PROPERTIES:
            if shift == 0:
                if not flags:
                    break
                flag_bits, flags = flags[0], flags[1:]
                shift = 15
            if flag_bits & (1 << shift):
                d[key] = spec_to_m[proptype]()
            shift -= 1

        self.properties = d

    def _serialize_properties(self):
        """serialize the 'properties' attribute (a dictionary) into
        the raw bytes making up a set of property flags and a
        property list, suitable for putting into a content frame header."""
        shift = 15
        flag_bits = 0
        flags = []
        sformat, svalues = [], []
        for key, proptype in self.PROPERTIES:
            val = self.properties.get(key, None)
            if val is not None:
                if shift == 0:
                    flags.append(flag_bits)
                    flag_bits = 0
                    shift = 15

                flag_bits |= (1 << shift)
                if proptype != 'bit':
                    sformat.append(proptype)
                    svalues.append(val)

            shift -= 1
        flags.append(flag_bits)
        result = BytesIO()
        for flag_bits in flags:
            result.write(pack('>H', flag_bits))
        result.write(dumps(''.join(sformat), svalues))
        return result.getvalue()
