#
# The Python Imaging Library.
# $Id$
#
# TIFF file handling
#
# TIFF is a flexible, if somewhat aged, image file format originally
# defined by Aldus.  Although TIFF supports a wide variety of pixel
# layouts and compression methods, the name doesn't really stand for
# "thousands of incompatible file formats," it just feels that way.
#
# To read TIFF data from a stream, the stream must be seekable.  For
# progressive decoding, make sure to use TIFF files where the tag
# directory is placed first in the file.
#
# History:
# 1995-09-01 fl   Created
# 1996-05-04 fl   Handle JPEGTABLES tag
# 1996-05-18 fl   Fixed COLORMAP support
# 1997-01-05 fl   Fixed PREDICTOR support
# 1997-08-27 fl   Added support for rational tags (from Perry Stoll)
# 1998-01-10 fl   Fixed seek/tell (from Jan Blom)
# 1998-07-15 fl   Use private names for internal variables
# 1999-06-13 fl   Rewritten for PIL 1.0 (1.0)
# 2000-10-11 fl   Additional fixes for Python 2.0 (1.1)
# 2001-04-17 fl   Fixed rewind support (seek to frame 0) (1.2)
# 2001-05-12 fl   Added write support for more tags (from Greg Couch) (1.3)
# 2001-12-18 fl   Added workaround for broken Matrox library
# 2002-01-18 fl   Don't mess up if photometric tag is missing (D. Alan Stewart)
# 2003-05-19 fl   Check FILLORDER tag
# 2003-09-26 fl   Added RGBa support
# 2004-02-24 fl   Added DPI support; fixed rational write support
# 2005-02-07 fl   Added workaround for broken Corel Draw 10 files
# 2006-01-09 fl   Added support for float/double tags (from Russell Nelson)
#
# Copyright (c) 1997-2006 by Secret Labs AB.  All rights reserved.
# Copyright (c) 1995-1997 by Fredrik Lundh
#
# See the README file for information on usage and redistribution.
#
from __future__ import annotations

import io
import itertools
import logging
import math
import os
import struct
import warnings
from collections.abc import Callable, MutableMapping
from fractions import Fraction
from numbers import Number, Rational
from typing import IO, Any, cast

from . import ExifTags, Image, ImageFile, ImageOps, ImagePalette, TiffTags
from ._binary import i16be as i16
from ._binary import i32be as i32
from ._binary import o8
from ._util import DeferredError, is_path
from .TiffTags import TYPES

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import NoReturn

    from ._typing import Buffer, IntegralLike, StrOrBytesPath

logger = logging.getLogger(__name__)

# Set these to true to force use of libtiff for reading or writing.
READ_LIBTIFF = False
WRITE_LIBTIFF = False
STRIP_SIZE = 65536

II = b"II"  # little-endian (Intel style)
MM = b"MM"  # big-endian (Motorola style)

#
# --------------------------------------------------------------------
# Read TIFF files

# a few tag names, just to make the code below a bit more readable
OSUBFILETYPE = 255
IMAGEWIDTH = 256
IMAGELENGTH = 257
BITSPERSAMPLE = 258
COMPRESSION = 259
PHOTOMETRIC_INTERPRETATION = 262
FILLORDER = 266
IMAGEDESCRIPTION = 270
STRIPOFFSETS = 273
SAMPLESPERPIXEL = 277
ROWSPERSTRIP = 278
STRIPBYTECOUNTS = 279
X_RESOLUTION = 282
Y_RESOLUTION = 283
PLANAR_CONFIGURATION = 284
RESOLUTION_UNIT = 296
TRANSFERFUNCTION = 301
SOFTWARE = 305
DATE_TIME = 306
ARTIST = 315
PREDICTOR = 317
COLORMAP = 320
TILEWIDTH = 322
TILELENGTH = 323
TILEOFFSETS = 324
TILEBYTECOUNTS = 325
SUBIFD = 330
EXTRASAMPLES = 338
SAMPLEFORMAT = 339
JPEGTABLES = 347
YCBCRSUBSAMPLING = 530
REFERENCEBLACKWHITE = 532
COPYRIGHT = 33432
IPTC_NAA_CHUNK = 33723  # newsphoto properties
PHOTOSHOP_CHUNK = 34377  # photoshop properties
ICCPROFILE = 34675
EXIFIFD = 34665
XMP = 700
JPEGQUALITY = 65537  # pseudo-tag by libtiff

# https://github.com/imagej/ImageJA/blob/master/src/main/java/ij/io/TiffDecoder.java
IMAGEJ_META_DATA_BYTE_COUNTS = 50838
IMAGEJ_META_DATA = 50839

COMPRESSION_INFO = {
    # Compression => pil compression name
    1: "raw",
    2: "tiff_ccitt",
    3: "group3",
    4: "group4",
    5: "tiff_lzw",
    6: "tiff_jpeg",  # obsolete
    7: "jpeg",
    8: "tiff_adobe_deflate",
    32771: "tiff_raw_16",  # 16-bit padding
    32773: "packbits",
    32809: "tiff_thunderscan",
    32946: "tiff_deflate",
    34676: "tiff_sgilog",
    34677: "tiff_sgilog24",
    34925: "lzma",
    50000: "zstd",
    50001: "webp",
}

COMPRESSION_INFO_REV = {v: k for k, v in COMPRESSION_INFO.items()}

OPEN_INFO = {
    # (ByteOrder, PhotoInterpretation, SampleFormat, FillOrder, BitsPerSample,
    #  ExtraSamples) => mode, rawmode
    (II, 0, (1,), 1, (1,), ()): ("1", "1;I"),
    (MM, 0, (1,), 1, (1,), ()): ("1", "1;I"),
    (II, 0, (1,), 2, (1,), ()): ("1", "1;IR"),
    (MM, 0, (1,), 2, (1,), ()): ("1", "1;IR"),
    (II, 1, (1,), 1, (1,), ()): ("1", "1"),
    (MM, 1, (1,), 1, (1,), ()): ("1", "1"),
    (II, 1, (1,), 2, (1,), ()): ("1", "1;R"),
    (MM, 1, (1,), 2, (1,), ()): ("1", "1;R"),
    (II, 0, (1,), 1, (2,), ()): ("L", "L;2I"),
    (MM, 0, (1,), 1, (2,), ()): ("L", "L;2I"),
    (II, 0, (1,), 2, (2,), ()): ("L", "L;2IR"),
    (MM, 0, (1,), 2, (2,), ()): ("L", "L;2IR"),
    (II, 1, (1,), 1, (2,), ()): ("L", "L;2"),
    (MM, 1, (1,), 1, (2,), ()): ("L", "L;2"),
    (II, 1, (1,), 2, (2,), ()): ("L", "L;2R"),
    (MM, 1, (1,), 2, (2,), ()): ("L", "L;2R"),
    (II, 0, (1,), 1, (4,), ()): ("L", "L;4I"),
    (MM, 0, (1,), 1, (4,), ()): ("L", "L;4I"),
    (II, 0, (1,), 2, (4,), ()): ("L", "L;4IR"),
    (MM, 0, (1,), 2, (4,), ()): ("L", "L;4IR"),
    (II, 1, (1,), 1, (4,), ()): ("L", "L;4"),
    (MM, 1, (1,), 1, (4,), ()): ("L", "L;4"),
    (II, 1, (1,), 2, (4,), ()): ("L", "L;4R"),
    (MM, 1, (1,), 2, (4,), ()): ("L", "L;4R"),
    (II, 0, (1,), 1, (8,), ()): ("L", "L;I"),
    (MM, 0, (1,), 1, (8,), ()): ("L", "L;I"),
    (II, 0, (1,), 2, (8,), ()): ("L", "L;IR"),
    (MM, 0, (1,), 2, (8,), ()): ("L", "L;IR"),
    (II, 1, (1,), 1, (8,), ()): ("L", "L"),
    (MM, 1, (1,), 1, (8,), ()): ("L", "L"),
    (II, 1, (2,), 1, (8,), ()): ("L", "L"),
    (MM, 1, (2,), 1, (8,), ()): ("L", "L"),
    (II, 1, (1,), 2, (8,), ()): ("L", "L;R"),
    (MM, 1, (1,), 2, (8,), ()): ("L", "L;R"),
    (II, 1, (1,), 1, (12,), ()): ("I;16", "I;12"),
    (II, 0, (1,), 1, (16,), ()): ("I;16", "I;16"),
    (II, 1, (1,), 1, (16,), ()): ("I;16", "I;16"),
    (MM, 1, (1,), 1, (16,), ()): ("I;16B", "I;16B"),
    (II, 1, (1,), 2, (16,), ()): ("I;16", "I;16R"),
    (II, 1, (2,), 1, (16,), ()): ("I", "I;16S"),
    (MM, 1, (2,), 1, (16,), ()): ("I", "I;16BS"),
    (II, 0, (3,), 1, (32,), ()): ("F", "F;32F"),
    (MM, 0, (3,), 1, (32,), ()): ("F", "F;32BF"),
    (II, 1, (1,), 1, (32,), ()): ("I", "I;32N"),
    (II, 1, (2,), 1, (32,), ()): ("I", "I;32S"),
    (MM, 1, (2,), 1, (32,), ()): ("I", "I;32BS"),
    (II, 1, (3,), 1, (32,), ()): ("F", "F;32F"),
    (MM, 1, (3,), 1, (32,), ()): ("F", "F;32BF"),
    (II, 1, (1,), 1, (8, 8), (2,)): ("LA", "LA"),
    (MM, 1, (1,), 1, (8, 8), (2,)): ("LA", "LA"),
    (II, 2, (1,), 1, (8, 8, 8), ()): ("RGB", "RGB"),
    (MM, 2, (1,), 1, (8, 8, 8), ()): ("RGB", "RGB"),
    (II, 2, (1,), 2, (8, 8, 8), ()): ("RGB", "RGB;R"),
    (MM, 2, (1,), 2, (8, 8, 8), ()): ("RGB", "RGB;R"),
    (II, 2, (1,), 1, (8, 8, 8, 8), ()): ("RGBA", "RGBA"),  # missing ExtraSamples
    (MM, 2, (1,), 1, (8, 8, 8, 8), ()): ("RGBA", "RGBA"),  # missing ExtraSamples
    (II, 2, (1,), 1, (8, 8, 8, 8), (0,)): ("RGB", "RGBX"),
    (MM, 2, (1,), 1, (8, 8, 8, 8), (0,)): ("RGB", "RGBX"),
    (II, 2, (1,), 1, (8, 8, 8, 8, 8), (0, 0)): ("RGB", "RGBXX"),
    (MM, 2, (1,), 1, (8, 8, 8, 8, 8), (0, 0)): ("RGB", "RGBXX"),
    (II, 2, (1,), 1, (8, 8, 8, 8, 8, 8), (0, 0, 0)): ("RGB", "RGBXXX"),
    (MM, 2, (1,), 1, (8, 8, 8, 8, 8, 8), (0, 0, 0)): ("RGB", "RGBXXX"),
    (II, 2, (1,), 1, (8, 8, 8, 8), (1,)): ("RGBA", "RGBa"),
    (MM, 2, (1,), 1, (8, 8, 8, 8), (1,)): ("RGBA", "RGBa"),
    (II, 2, (1,), 1, (8, 8, 8, 8, 8), (1, 0)): ("RGBA", "RGBaX"),
    (MM, 2, (1,), 1, (8, 8, 8, 8, 8), (1, 0)): ("RGBA", "RGBaX"),
    (II, 2, (1,), 1, (8, 8, 8, 8, 8, 8), (1, 0, 0)): ("RGBA", "RGBaXX"),
    (MM, 2, (1,), 1, (8, 8, 8, 8, 8, 8), (1, 0, 0)): ("RGBA", "RGBaXX"),
    (II, 2, (1,), 1, (8, 8, 8, 8), (2,)): ("RGBA", "RGBA"),
    (MM, 2, (1,), 1, (8, 8, 8, 8), (2,)): ("RGBA", "RGBA"),
    (II, 2, (1,), 1, (8, 8, 8, 8, 8), (2, 0)): ("RGBA", "RGBAX"),
    (MM, 2, (1,), 1, (8, 8, 8, 8, 8), (2, 0)): ("RGBA", "RGBAX"),
    (II, 2, (1,), 1, (8, 8, 8, 8, 8, 8), (2, 0, 0)): ("RGBA", "RGBAXX"),
    (MM, 2, (1,), 1, (8, 8, 8, 8, 8, 8), (2, 0, 0)): ("RGBA", "RGBAXX"),
    (II, 2, (1,), 1, (8, 8, 8, 8), (999,)): ("RGBA", "RGBA"),  # Corel Draw 10
    (MM, 2, (1,), 1, (8, 8, 8, 8), (999,)): ("RGBA", "RGBA"),  # Corel Draw 10
    (II, 2, (1,), 1, (16, 16, 16), ()): ("RGB", "RGB;16L"),
    (MM, 2, (1,), 1, (16, 16, 16), ()): ("RGB", "RGB;16B"),
    (II, 2, (1,), 1, (16, 16, 16, 16), ()): ("RGBA", "RGBA;16L"),
    (MM, 2, (1,), 1, (16, 16, 16, 16), ()): ("RGBA", "RGBA;16B"),
    (II, 2, (1,), 1, (16, 16, 16, 16), (0,)): ("RGB", "RGBX;16L"),
    (MM, 2, (1,), 1, (16, 16, 16, 16), (0,)): ("RGB", "RGBX;16B"),
    (II, 2, (1,), 1, (16, 16, 16, 16), (1,)): ("RGBA", "RGBa;16L"),
    (MM, 2, (1,), 1, (16, 16, 16, 16), (1,)): ("RGBA", "RGBa;16B"),
    (II, 2, (1,), 1, (16, 16, 16, 16), (2,)): ("RGBA", "RGBA;16L"),
    (MM, 2, (1,), 1, (16, 16, 16, 16), (2,)): ("RGBA", "RGBA;16B"),
    (II, 3, (1,), 1, (1,), ()): ("P", "P;1"),
    (MM, 3, (1,), 1, (1,), ()): ("P", "P;1"),
    (II, 3, (1,), 2, (1,), ()): ("P", "P;1R"),
    (MM, 3, (1,), 2, (1,), ()): ("P", "P;1R"),
    (II, 3, (1,), 1, (2,), ()): ("P", "P;2"),
    (MM, 3, (1,), 1, (2,), ()): ("P", "P;2"),
    (II, 3, (1,), 2, (2,), ()): ("P", "P;2R"),
    (MM, 3, (1,), 2, (2,), ()): ("P", "P;2R"),
    (II, 3, (1,), 1, (4,), ()): ("P", "P;4"),
    (MM, 3, (1,), 1, (4,), ()): ("P", "P;4"),
    (II, 3, (1,), 2, (4,), ()): ("P", "P;4R"),
    (MM, 3, (1,), 2, (4,), ()): ("P", "P;4R"),
    (II, 3, (1,), 1, (8,), ()): ("P", "P"),
    (MM, 3, (1,), 1, (8,), ()): ("P", "P"),
    (II, 3, (1,), 1, (8, 8), (0,)): ("P", "PX"),
    (II, 3, (1,), 1, (8, 8), (2,)): ("PA", "PA"),
    (MM, 3, (1,), 1, (8, 8), (2,)): ("PA", "PA"),
    (II, 3, (1,), 2, (8,), ()): ("P", "P;R"),
    (MM, 3, (1,), 2, (8,), ()): ("P", "P;R"),
    (II, 5, (1,), 1, (8, 8, 8, 8), ()): ("CMYK", "CMYK"),
    (MM, 5, (1,), 1, (8, 8, 8, 8), ()): ("CMYK", "CMYK"),
    (II, 5, (1,), 1, (8, 8, 8, 8, 8), (0,)): ("CMYK", "CMYKX"),
    (MM, 5, (1,), 1, (8, 8, 8, 8, 8), (0,)): ("CMYK", "CMYKX"),
    (II, 5, (1,), 1, (8, 8, 8, 8, 8, 8), (0, 0)): ("CMYK", "CMYKXX"),
    (MM, 5, (1,), 1, (8, 8, 8, 8, 8, 8), (0, 0)): ("CMYK", "CMYKXX"),
    (II, 5, (1,), 1, (16, 16, 16, 16), ()): ("CMYK", "CMYK;16L"),
    (MM, 5, (1,), 1, (16, 16, 16, 16), ()): ("CMYK", "CMYK;16B"),
    (II, 6, (1,), 1, (8,), ()): ("L", "L"),
    (MM, 6, (1,), 1, (8,), ()): ("L", "L"),
    # JPEG compressed images handled by LibTiff and auto-converted to RGBX
    # Minimal Baseline TIFF requires YCbCr images to have 3 SamplesPerPixel
    (II, 6, (1,), 1, (8, 8, 8), ()): ("RGB", "RGBX"),
    (MM, 6, (1,), 1, (8, 8, 8), ()): ("RGB", "RGBX"),
    (II, 8, (1,), 1, (8, 8, 8), ()): ("LAB", "LAB"),
    (MM, 8, (1,), 1, (8, 8, 8), ()): ("LAB", "LAB"),
}

MAX_SAMPLESPERPIXEL = max(len(key_tp[4]) for key_tp in OPEN_INFO)

PREFIXES = [
    b"MM\x00\x2a",  # Valid TIFF header with big-endian byte order
    b"II\x2a\x00",  # Valid TIFF header with little-endian byte order
    b"MM\x2a\x00",  # Invalid TIFF header, assume big-endian
    b"II\x00\x2a",  # Invalid TIFF header, assume little-endian
    b"MM\x00\x2b",  # BigTIFF with big-endian byte order
    b"II\x2b\x00",  # BigTIFF with little-endian byte order
]


def _accept(prefix: bytes) -> bool:
    return prefix.startswith(tuple(PREFIXES))


def _limit_rational(
    val: float | Fraction | IFDRational, max_val: int
) -> tuple[IntegralLike, IntegralLike]:
    inv = abs(val) > 1
    n_d = IFDRational(1 / val if inv else val).limit_rational(max_val)
    return n_d[::-1] if inv else n_d


def _limit_signed_rational(
    val: IFDRational, max_val: int, min_val: int
) -> tuple[IntegralLike, IntegralLike]:
    frac = Fraction(val)
    n_d: tuple[IntegralLike, IntegralLike] = frac.numerator, frac.denominator

    if min(float(i) for i in n_d) < min_val:
        n_d = _limit_rational(val, abs(min_val))

    n_d_float = tuple(float(i) for i in n_d)
    if max(n_d_float) > max_val:
        n_d = _limit_rational(n_d_float[0] / n_d_float[1], max_val)

    return n_d


##
# Wrapper for TIFF IFDs.

_load_dispatch = {}
_write_dispatch = {}


def _delegate(op: str) -> Any:
    def delegate(
        self: IFDRational, *args: tuple[float, ...]
    ) -> bool | float | Fraction:
        return getattr(self._val, op)(*args)

    return delegate


class IFDRational(Rational):
    """Implements a rational class where 0/0 is a legal value to match
    the in the wild use of exif rationals.

    e.g., DigitalZoomRatio - 0.00/0.00  indicates that no digital zoom was used
    """

    """ If the denominator is 0, store this as a float('nan'), otherwise store
    as a fractions.Fraction(). Delegate as appropriate

    """

    __slots__ = ("_numerator", "_denominator", "_val")

    def __init__(
        self, value: float | Fraction | IFDRational, denominator: int = 1
    ) -> None:
        """
        :param value: either an integer numerator, a
        float/rational/other number, or an IFDRational
        :param denominator: Optional integer denominator
        """
        self._val: Fraction | float
        if isinstance(value, IFDRational):
            self._numerator = value.numerator
            self._denominator = value.denominator
            self._val = value._val
            return

        if isinstance(value, Fraction):
            self._numerator = value.numerator
            self._denominator = value.denominator
        else:
            if TYPE_CHECKING:
                self._numerator = cast(IntegralLike, value)
            else:
                self._numerator = value
            self._denominator = denominator

        if denominator == 0:
            self._val = float("nan")
        elif denominator == 1:
            self._val = Fraction(value)
        elif int(value) == value:
            self._val = Fraction(int(value), denominator)
        else:
            self._val = Fraction(value / denominator)

    @property
    def numerator(self) -> IntegralLike:
        return self._numerator

    @property
    def denominator(self) -> int:
        return self._denominator

    def limit_rational(self, max_denominator: int) -> tuple[IntegralLike, int]:
        """

        :param max_denominator: Integer, the maximum denominator value
        :returns: Tuple of (numerator, denominator)
        """

        if self.denominator == 0:
            return self.numerator, self.denominator

        assert isinstance(self._val, Fraction)
        f = self._val.limit_denominator(max_denominator)
        return f.numerator, f.denominator

    def __repr__(self) -> str:
        return str(float(self._val))

    def __hash__(self) -> int:  # type: ignore[override]
        return self._val.__hash__()

    def __eq__(self, other: object) -> bool:
        val = self._val
        if isinstance(other, IFDRational):
            other = other._val
        if isinstance(other, float):
            val = float(val)
        return val == other

    def __getstate__(self) -> list[float | Fraction | IntegralLike]:
        return [self._val, self._numerator, self._denominator]

    def __setstate__(self, state: list[float | Fraction | IntegralLike]) -> None:
        IFDRational.__init__(self, 0)
        _val, _numerator, _denominator = state
        assert isinstance(_val, (float, Fraction))
        self._val = _val
        if TYPE_CHECKING:
            self._numerator = cast(IntegralLike, _numerator)
        else:
            self._numerator = _numerator
        assert isinstance(_denominator, int)
        self._denominator = _denominator

    """ a = ['add','radd', 'sub', 'rsub', 'mul', 'rmul',
             'truediv', 'rtruediv', 'floordiv', 'rfloordiv',
             'mod','rmod', 'pow','rpow', 'pos', 'neg',
             'abs', 'trunc', 'lt', 'gt', 'le', 'ge', 'bool',
             'ceil', 'floor', 'round']
        print("\n".join("__%s__ = _delegate('__%s__')" % (s,s) for s in a))
        """

    __add__ = _delegate("__add__")
    __radd__ = _delegate("__radd__")
    __sub__ = _delegate("__sub__")
    __rsub__ = _delegate("__rsub__")
    __mul__ = _delegate("__mul__")
    __rmul__ = _delegate("__rmul__")
    __truediv__ = _delegate("__truediv__")
    __rtruediv__ = _delegate("__rtruediv__")
    __floordiv__ = _delegate("__floordiv__")
    __rfloordiv__ = _delegate("__rfloordiv__")
    __mod__ = _delegate("__mod__")
    __rmod__ = _delegate("__rmod__")
    __pow__ = _delegate("__pow__")
    __rpow__ = _delegate("__rpow__")
    __pos__ = _delegate("__pos__")
    __neg__ = _delegate("__neg__")
    __abs__ = _delegate("__abs__")
    __trunc__ = _delegate("__trunc__")
    __lt__ = _delegate("__lt__")
    __gt__ = _delegate("__gt__")
    __le__ = _delegate("__le__")
    __ge__ = _delegate("__ge__")
    __bool__ = _delegate("__bool__")
    __ceil__ = _delegate("__ceil__")
    __floor__ = _delegate("__floor__")
    __round__ = _delegate("__round__")
    # Python >= 3.11
    if hasattr(Fraction, "__int__"):
        __int__ = _delegate("__int__")


_LoaderFunc = Callable[["ImageFileDirectory_v2", bytes, bool], Any]


def _register_loader(idx: int, size: int) -> Callable[[_LoaderFunc], _LoaderFunc]:
    def decorator(func: _LoaderFunc) -> _LoaderFunc:
        from .TiffTags import TYPES

        if func.__name__.startswith("load_"):
            TYPES[idx] = func.__name__[5:].replace("_", " ")
        _load_dispatch[idx] = size, func  # noqa: F821
        return func

    return decorator


def _register_writer(idx: int) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        _write_dispatch[idx] = func  # noqa: F821
        return func

    return decorator


def _register_basic(idx_fmt_name: tuple[int, str, str]) -> None:
    from .TiffTags import TYPES

    idx, fmt, name = idx_fmt_name
    TYPES[idx] = name
    size = struct.calcsize(f"={fmt}")

    def basic_handler(
        self: ImageFileDirectory_v2, data: bytes, legacy_api: bool = True
    ) -> tuple[Any, ...]:
        return self._unpack(f"{len(data) // size}{fmt}", data)

    _load_dispatch[idx] = size, basic_handler  # noqa: F821
    _write_dispatch[idx] = lambda self, *values: (  # noqa: F821
        b"".join(self._pack(fmt, value) for value in values)
    )


if TYPE_CHECKING:
    _IFDv2Base = MutableMapping[int, Any]
else:
    _IFDv2Base = MutableMapping


class ImageFileDirectory_v2(_IFDv2Base):
    """This class represents a TIFF tag directory.  To speed things up, we
    don't decode tags unless they're asked for.

    Exposes a dictionary interface of the tags in the directory::

        ifd = ImageFileDirectory_v2()
        ifd[key] = 'Some Data'
        ifd.tagtype[key] = TiffTags.ASCII
        print(ifd[key])
        'Some Data'

    Individual values are returned as the strings or numbers, sequences are
    returned as tuples of the values.

    The tiff metadata type of each item is stored in a dictionary of
    tag types in
    :attr:`~PIL.TiffImagePlugin.ImageFileDirectory_v2.tagtype`. The types
    are read from a tiff file, guessed from the type added, or added
    manually.

    Data Structures:

        * ``self.tagtype = {}``

          * Key: numerical TIFF tag number
          * Value: integer corresponding to the data type from
            :py:data:`.TiffTags.TYPES`

          .. versionadded:: 3.0.0

    'Internal' data structures:

        * ``self._tags_v2 = {}``

          * Key: numerical TIFF tag number
          * Value: decoded data, as tuple for multiple values

        * ``self._tagdata = {}``

          * Key: numerical TIFF tag number
          * Value: undecoded byte string from file

        * ``self._tags_v1 = {}``

          * Key: numerical TIFF tag number
          * Value: decoded data in the v1 format

    Tags will be found in the private attributes ``self._tagdata``, and in
    ``self._tags_v2`` once decoded.

    ``self.legacy_api`` is a value for internal use, and shouldn't be changed
    from outside code. In cooperation with
    :py:class:`~PIL.TiffImagePlugin.ImageFileDirectory_v1`, if ``legacy_api``
    is true, then decoded tags will be populated into both ``_tags_v1`` and
    ``_tags_v2``. ``_tags_v2`` will be used if this IFD is used in the TIFF
    save routine. Tags should be read from ``_tags_v1`` if
    ``legacy_api == true``.

    """

    _load_dispatch: dict[int, tuple[int, _LoaderFunc]] = {}
    _write_dispatch: dict[int, Callable[..., Any]] = {}

    def __init__(
        self,
        ifh: bytes = b"II\x2a\x00\x00\x00\x00\x00",
        prefix: bytes | None = None,
        group: int | None = None,
    ) -> None:
        """Initialize an ImageFileDirectory.

        To construct an ImageFileDirectory from a real file, pass the 8-byte
        magic header to the constructor.  To only set the endianness, pass it
        as the 'prefix' keyword argument.

        :param ifh: One of the accepted magic headers (cf. PREFIXES); also sets
              endianness.
        :param prefix: Override the endianness of the file.
        """
        if not _accept(ifh):
            msg = f"not a TIFF file (header {repr(ifh)} not valid)"
            raise SyntaxError(msg)
        self._prefix = prefix if prefix is not None else ifh[:2]
        if self._prefix == MM:
            self._endian = ">"
        elif self._prefix == II:
            self._endian = "<"
        else:
            msg = "not a TIFF IFD"
            raise SyntaxError(msg)
        self._bigtiff = ifh[2] == 43
        self.group = group
        self.tagtype: dict[int, int] = {}
        """ Dictionary of tag types """
        self.reset()
        self.next = (
            self._unpack("Q", ifh[8:])[0]
            if self._bigtiff
            else self._unpack("L", ifh[4:])[0]
        )
        self._legacy_api = False

    prefix = property(lambda self: self._prefix)
    offset = property(lambda self: self._offset)

    @property
    def legacy_api(self) -> bool:
        return self._legacy_api

    @legacy_api.setter
    def legacy_api(self, value: bool) -> NoReturn:
        msg = "Not allowing setting of legacy api"
        raise Exception(msg)

    def reset(self) -> None:
        self._tags_v1: dict[int, Any] = {}  # will remain empty if legacy_api is false
        self._tags_v2: dict[int, Any] = {}  # main tag storage
        self._tagdata: dict[int, bytes] = {}
        self.tagtype = {}  # added 2008-06-05 by Florian Hoech
        self._next = None
        self._offset: int | None = None

    def __str__(self) -> str:
        return str(dict(self))

    def named(self) -> dict[str, Any]:
        """
        :returns: dict of name|key: value

        Returns the complete tag dictionary, with named tags where possible.
        """
        return {
            TiffTags.lookup(code, self.group).name: value
            for code, value in self.items()
        }

    def __len__(self) -> int:
        return len(set(self._tagdata) | set(self._tags_v2))

    def __getitem__(self, tag: int) -> Any:
        if tag not in self._tags_v2:  # unpack on the fly
            data = self._tagdata[tag]
            typ = self.tagtype[tag]
            size, handler = self._load_dispatch[typ]
            self[tag] = handler(self, data, self.legacy_api)  # check type
        val = self._tags_v2[tag]
        if self.legacy_api and not isinstance(val, (tuple, bytes)):
            val = (val,)
        return val

    def __contains__(self, tag: object) -> bool:
        return tag in self._tags_v2 or tag in self._tagdata

    def __setitem__(self, tag: int, value: Any) -> None:
        self._setitem(tag, value, self.legacy_api)

    def _setitem(self, tag: int, value: Any, legacy_api: bool) -> None:
        basetypes = (Number, bytes, str)

        info = TiffTags.lookup(tag, self.group)
        values = [value] if isinstance(value, basetypes) else value

        if tag not in self.tagtype:
            if info.type:
                self.tagtype[tag] = info.type
            else:
                self.tagtype[tag] = TiffTags.UNDEFINED
                if all(isinstance(v, IFDRational) for v in values):
                    for v in values:
                        assert isinstance(v, IFDRational)
                        if v < 0:
                            self.tagtype[tag] = TiffTags.SIGNED_RATIONAL
                            break
                    else:
                        self.tagtype[tag] = TiffTags.RATIONAL
                elif all(isinstance(v, int) for v in values):
                    short = True
                    signed_short = True
                    long = True
                    for v in values:
                        assert isinstance(v, int)
                        if short and not (0 <= v < 2**16):
                            short = False
                        if signed_short and not (-(2**15) < v < 2**15):
                            signed_short = False
                        if long and v < 0:
                            long = False
                    if short:
                        self.tagtype[tag] = TiffTags.SHORT
                    elif signed_short:
                        self.tagtype[tag] = TiffTags.SIGNED_SHORT
                    elif long:
                        self.tagtype[tag] = TiffTags.LONG
                    else:
                        self.tagtype[tag] = TiffTags.SIGNED_LONG
                elif all(isinstance(v, float) for v in values):
                    self.tagtype[tag] = TiffTags.DOUBLE
                elif all(isinstance(v, str) for v in values):
                    self.tagtype[tag] = TiffTags.ASCII
                elif all(isinstance(v, bytes) for v in values):
                    self.tagtype[tag] = TiffTags.BYTE

        if self.tagtype[tag] == TiffTags.UNDEFINED:
            values = [
                v.encode("ascii", "replace") if isinstance(v, str) else v
                for v in values
            ]
        elif self.tagtype[tag] == TiffTags.RATIONAL:
            values = [float(v) if isinstance(v, int) else v for v in values]

        is_ifd = self.tagtype[tag] == TiffTags.LONG and isinstance(values, dict)
        if not is_ifd:
            values = tuple(
                info.cvt_enum(value) if isinstance(value, str) else value
                for value in values
            )

        dest = self._tags_v1 if legacy_api else self._tags_v2

        # Three branches:
        # Spec'd length == 1, Actual length 1, store as element
        # Spec'd length == 1, Actual > 1, Warn and truncate. Formerly barfed.
        # No Spec, Actual length 1, Formerly (<4.2) returned a 1 element tuple.
        # Don't mess with the legacy api, since it's frozen.
        if not is_ifd and (
            (info.length == 1)
            or self.tagtype[tag] == TiffTags.BYTE
            or (info.length is None and len(values) == 1 and not legacy_api)
        ):
            # Don't mess with the legacy api, since it's frozen.
            if legacy_api and self.tagtype[tag] in [
                TiffTags.RATIONAL,
                TiffTags.SIGNED_RATIONAL,
            ]:  # rationals
                values = (values,)
            try:
                (dest[tag],) = values
            except ValueError:
                # We've got a builtin tag with 1 expected entry
                warnings.warn(
                    f"Metadata Warning, tag {tag} had too many entries: "
                    f"{len(values)}, expected 1"
                )
                dest[tag] = values[0]

        else:
            # Spec'd length > 1 or undefined
            # Unspec'd, and length > 1
            dest[tag] = values

    def __delitem__(self, tag: int) -> None:
        self._tags_v2.pop(tag, None)
        self._tags_v1.pop(tag, None)
        self._tagdata.pop(tag, None)

    def __iter__(self) -> Iterator[int]:
        return iter(set(self._tagdata) | set(self._tags_v2))

    def _unpack(self, fmt: str, data: bytes) -> tuple[Any, ...]:
        return struct.unpack(self._endian + fmt, data)

    def _pack(self, fmt: str, *values: Any) -> bytes:
        return struct.pack(self._endian + fmt, *values)

    list(
        map(
            _register_basic,
            [
                (TiffTags.SHORT, "H", "short"),
                (TiffTags.LONG, "L", "long"),
                (TiffTags.SIGNED_BYTE, "b", "signed byte"),
                (TiffTags.SIGNED_SHORT, "h", "signed short"),
                (TiffTags.SIGNED_LONG, "l", "signed long"),
                (TiffTags.FLOAT, "f", "float"),
                (TiffTags.DOUBLE, "d", "double"),
                (TiffTags.IFD, "L", "long"),
                (TiffTags.LONG8, "Q", "long8"),
            ],
        )
    )

    @_register_loader(1, 1)  # Basic type, except for the legacy API.
    def load_byte(self, data: bytes, legacy_api: bool = True) -> bytes:
        return data

    @_register_writer(1)  # Basic type, except for the legacy API.
    def write_byte(self, data: bytes | int | IFDRational) -> bytes:
        if isinstance(data, IFDRational):
            data = int(data)
        if isinstance(data, int):
            data = bytes((data,))
        return data

    @_register_loader(2, 1)
    def load_string(self, data: bytes, legacy_api: bool = True) -> str:
        if data.endswith(b"\0"):
            data = data[:-1]
        return data.decode("latin-1", "replace")

    @_register_writer(2)
    def write_string(self, value: str | bytes | int) -> bytes:
        # remerge of https://github.com/python-pillow/Pillow/pull/1416
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, bytes):
            value = value.encode("ascii", "replace")
        return value + b"\0"

    @_register_loader(5, 8)
    def load_rational(
        self, data: bytes, legacy_api: bool = True
    ) -> tuple[tuple[int, int] | IFDRational, ...]:
        vals = self._unpack(f"{len(data) // 4}L", data)

        def combine(a: int, b: int) -> tuple[int, int] | IFDRational:
            return (a, b) if legacy_api else IFDRational(a, b)

        return tuple(combine(num, denom) for num, denom in zip(vals[::2], vals[1::2]))

    @_register_writer(5)
    def write_rational(self, *values: IFDRational) -> bytes:
        return b"".join(
            self._pack("2L", *_limit_rational(frac, 2**32 - 1)) for frac in values
        )

    @_register_loader(7, 1)
    def load_undefined(self, data: bytes, legacy_api: bool = True) -> bytes:
        return data

    @_register_writer(7)
    def write_undefined(self, value: bytes | int | IFDRational) -> bytes:
        if isinstance(value, IFDRational):
            value = int(value)
        if isinstance(value, int):
            value = str(value).encode("ascii", "replace")
        return value

    @_register_loader(10, 8)
    def load_signed_rational(
        self, data: bytes, legacy_api: bool = True
    ) -> tuple[tuple[int, int] | IFDRational, ...]:
        vals = self._unpack(f"{len(data) // 4}l", data)

        def combine(a: int, b: int) -> tuple[int, int] | IFDRational:
            return (a, b) if legacy_api else IFDRational(a, b)

        return tuple(combine(num, denom) for num, denom in zip(vals[::2], vals[1::2]))

    @_register_writer(10)
    def write_signed_rational(self, *values: IFDRational) -> bytes:
        return b"".join(
            self._pack("2l", *_limit_signed_rational(frac, 2**31 - 1, -(2**31)))
            for frac in values
        )

    def _ensure_read(self, fp: IO[bytes], size: int) -> bytes:
        ret = fp.read(size)
        if len(ret) != size:
            msg = (
                "Corrupt EXIF data.  "
                f"Expecting to read {size} bytes but only got {len(ret)}. "
            )
            raise OSError(msg)
        return ret

    def load(self, fp: IO[bytes]) -> None:
        self.reset()
        self._offset = fp.tell()

        try:
            tag_count = (
                self._unpack("Q", self._ensure_read(fp, 8))
                if self._bigtiff
                else self._unpack("H", self._ensure_read(fp, 2))
            )[0]
            for i in range(tag_count):
                tag, typ, count, data = (
                    self._unpack("HHQ8s", self._ensure_read(fp, 20))
                    if self._bigtiff
                    else self._unpack("HHL4s", self._ensure_read(fp, 12))
                )

                tagname = TiffTags.lookup(tag, self.group).name
                typname = TYPES.get(typ, "unknown")
                msg = f"tag: {tagname} ({tag}) - type: {typname} ({typ})"

                try:
                    unit_size, handler = self._load_dispatch[typ]
                except KeyError:
                    logger.debug("%s - unsupported type %s", msg, typ)
                    continue  # ignore unsupported type
                size = count * unit_size
                if size > (8 if self._bigtiff else 4):
                    here = fp.tell()
                    (offset,) = self._unpack("Q" if self._bigtiff else "L", data)
                    msg += f" Tag Location: {here} - Data Location: {offset}"
                    fp.seek(offset)
                    data = ImageFile._safe_read(fp, size)
                    fp.seek(here)
                else:
                    data = data[:size]

                if len(data) != size:
                    warnings.warn(
                        "Possibly corrupt EXIF data.  "
                        f"Expecting to read {size} bytes but only got {len(data)}."
                        f" Skipping tag {tag}"
                    )
                    logger.debug(msg)
                    continue

                if not data:
                    logger.debug(msg)
                    continue

                self._tagdata[tag] = data
                self.tagtype[tag] = typ

                msg += " - value: "
                msg += f"<table: {size} bytes>" if size > 32 else repr(data)

                logger.debug(msg)

            (self.next,) = (
                self._unpack("Q", self._ensure_read(fp, 8))
                if self._bigtiff
                else self._unpack("L", self._ensure_read(fp, 4))
            )
        except OSError as msg:
            warnings.warn(str(msg))
            return

    def _get_ifh(self) -> bytes:
        ifh = self._prefix + self._pack("H", 43 if self._bigtiff else 42)
        if self._bigtiff:
            ifh += self._pack("HH", 8, 0)
        ifh += self._pack("Q", 16) if self._bigtiff else self._pack("L", 8)

        return ifh

    def tobytes(self, offset: int = 0) -> bytes:
        # FIXME What about tagdata?
        result = self._pack("Q" if self._bigtiff else "H", len(self._tags_v2))

        entries: list[tuple[int, int, int, bytes, bytes]] = []

        fmt = "Q" if self._bigtiff else "L"
        fmt_size = 8 if self._bigtiff else 4
        offset += (
            len(result) + len(self._tags_v2) * (20 if self._bigtiff else 12) + fmt_size
        )
        stripoffsets = None

        # pass 1: convert tags to binary format
        # always write tags in ascending order
        for tag, value in sorted(self._tags_v2.items()):
            if tag == STRIPOFFSETS:
                stripoffsets = len(entries)
            typ = self.tagtype[tag]
            logger.debug("Tag %s, Type: %s, Value: %s", tag, typ, repr(value))
            is_ifd = typ == TiffTags.LONG and isinstance(value, dict)
            if is_ifd:
                ifd = ImageFileDirectory_v2(self._get_ifh(), group=tag)
                values = self._tags_v2[tag]
                for ifd_tag, ifd_value in values.items():
                    ifd[ifd_tag] = ifd_value
                data = ifd.tobytes(offset)
            else:
                values = value if isinstance(value, tuple) else (value,)
                data = self._write_dispatch[typ](self, *values)

            tagname = TiffTags.lookup(tag, self.group).name
            typname = "ifd" if is_ifd else TYPES.get(typ, "unknown")
            msg = f"save: {tagname} ({tag}) - type: {typname} ({typ}) - value: "
            msg += f"<table: {len(data)} bytes>" if len(data) >= 16 else str(values)
            logger.debug(msg)

            # count is sum of lengths for string and arbitrary data
            if is_ifd:
                count = 1
            elif typ in [TiffTags.BYTE, TiffTags.ASCII, TiffTags.UNDEFINED]:
                count = len(data)
            else:
                count = len(values)
            # figure out if data fits into the entry
            if len(data) <= fmt_size:
                entries.append((tag, typ, count, data.ljust(fmt_size, b"\0"), b""))
            else:
                entries.append((tag, typ, count, self._pack(fmt, offset), data))
                offset += (len(data) + 1) // 2 * 2  # pad to word

        # update strip offset data to point beyond auxiliary data
        if stripoffsets is not None:
            tag, typ, count, value, data = entries[stripoffsets]
            if data:
                size, handler = self._load_dispatch[typ]
                values = [val + offset for val in handler(self, data, self.legacy_api)]
                data = self._write_dispatch[typ](self, *values)
            else:
                value = self._pack(fmt, self._unpack(fmt, value)[0] + offset)
            entries[stripoffsets] = tag, typ, count, value, data

        # pass 2: write entries to file
        for tag, typ, count, value, data in entries:
            logger.debug("%s %s %s %s %s", tag, typ, count, repr(value), repr(data))
            result += self._pack(
                "HHQ8s" if self._bigtiff else "HHL4s", tag, typ, count, value
            )

        # -- overwrite here for multi-page --
        result += self._pack(fmt, 0)  # end of entries

        # pass 3: write auxiliary data to file
        for tag, typ, count, value, data in entries:
            result += data
            if len(data) & 1:
                result += b"\0"

        return result

    def save(self, fp: IO[bytes]) -> int:
        if fp.tell() == 0:  # skip TIFF header on subsequent pages
            fp.write(self._get_ifh())

        offset = fp.tell()
        result = self.tobytes(offset)
        fp.write(result)
        return offset + len(result)


ImageFileDirectory_v2._load_dispatch = _load_dispatch
ImageFileDirectory_v2._write_dispatch = _write_dispatch
for idx, name in TYPES.items():
    name = name.replace(" ", "_")
    setattr(ImageFileDirectory_v2, f"load_{name}", _load_dispatch[idx][1])
    setattr(ImageFileDirectory_v2, f"write_{name}", _write_dispatch[idx])
del _load_dispatch, _write_dispatch, idx, name


# Legacy ImageFileDirectory support.
class ImageFileDirectory_v1(ImageFileDirectory_v2):
    """This class represents the **legacy** interface to a TIFF tag directory.

    Exposes a dictionary interface of the tags in the directory::

        ifd = ImageFileDirectory_v1()
        ifd[key] = 'Some Data'
        ifd.tagtype[key] = TiffTags.ASCII
        print(ifd[key])
        ('Some Data',)

    Also contains a dictionary of tag types as read from the tiff image file,
    :attr:`~PIL.TiffImagePlugin.ImageFileDirectory_v1.tagtype`.

    Values are returned as a tuple.

    ..  deprecated:: 3.0.0
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._legacy_api = True

    tags = property(lambda self: self._tags_v1)
    tagdata = property(lambda self: self._tagdata)

    # defined in ImageFileDirectory_v2
    tagtype: dict[int, int]
    """Dictionary of tag types"""

    @classmethod
    def from_v2(cls, original: ImageFileDirectory_v2) -> ImageFileDirectory_v1:
        """Returns an
        :py:class:`~PIL.TiffImagePlugin.ImageFileDirectory_v1`
        instance with the same data as is contained in the original
        :py:class:`~PIL.TiffImagePlugin.ImageFileDirectory_v2`
        instance.

        :returns: :py:class:`~PIL.TiffImagePlugin.ImageFileDirectory_v1`

        """

        ifd = cls(prefix=original.prefix)
        ifd._tagdata = original._tagdata
        ifd.tagtype = original.tagtype
        ifd.next = original.next  # an indicator for multipage tiffs
        return ifd

    def to_v2(self) -> ImageFileDirectory_v2:
        """Returns an
        :py:class:`~PIL.TiffImagePlugin.ImageFileDirectory_v2`
        instance with the same data as is contained in the original
        :py:class:`~PIL.TiffImagePlugin.ImageFileDirectory_v1`
        instance.

        :returns: :py:class:`~PIL.TiffImagePlugin.ImageFileDirectory_v2`

        """

        ifd = ImageFileDirectory_v2(prefix=self.prefix)
        ifd._tagdata = dict(self._tagdata)
        ifd.tagtype = dict(self.tagtype)
        ifd._tags_v2 = dict(self._tags_v2)
        return ifd

    def __contains__(self, tag: object) -> bool:
        return tag in self._tags_v1 or tag in self._tagdata

    def __len__(self) -> int:
        return len(set(self._tagdata) | set(self._tags_v1))

    def __iter__(self) -> Iterator[int]:
        return iter(set(self._tagdata) | set(self._tags_v1))

    def __setitem__(self, tag: int, value: Any) -> None:
        for legacy_api in (False, True):
            self._setitem(tag, value, legacy_api)

    def __getitem__(self, tag: int) -> Any:
        if tag not in self._tags_v1:  # unpack on the fly
            data = self._tagdata[tag]
            typ = self.tagtype[tag]
            size, handler = self._load_dispatch[typ]
            for legacy in (False, True):
                self._setitem(tag, handler(self, data, legacy), legacy)
        val = self._tags_v1[tag]
        if not isinstance(val, (tuple, bytes)):
            val = (val,)
        return val


# undone -- switch this pointer
ImageFileDirectory = ImageFileDirectory_v1


##
# Image plugin for TIFF files.


class TiffImageFile(ImageFile.ImageFile):
    format = "TIFF"
    format_description = "Adobe TIFF"
    _close_exclusive_fp_after_loading = False

    def __init__(
        self,
        fp: StrOrBytesPath | IO[bytes],
        filename: str | bytes | None = None,
    ) -> None:
        self.tag_v2: ImageFileDirectory_v2
        """ Image file directory (tag dictionary) """

        self.tag: ImageFileDirectory_v1
        """ Legacy tag entries """

        super().__init__(fp, filename)

    def _open(self) -> None:
        """Open the first image in a TIFF file"""

        # Header
        ifh = self.fp.read(8)
        if ifh[2] == 43:
            ifh += self.fp.read(8)

        self.tag_v2 = ImageFileDirectory_v2(ifh)

        # setup frame pointers
        self.__first = self.__next = self.tag_v2.next
        self.__frame = -1
        self._fp = self.fp
        self._frame_pos: list[int] = []
        self._n_frames: int | None = None

        logger.debug("*** TiffImageFile._open ***")
        logger.debug("- __first: %s", self.__first)
        logger.debug("- ifh: %s", repr(ifh))  # Use repr to avoid str(bytes)

        # and load the first frame
        self._seek(0)

    @property
    def n_frames(self) -> int:
        current_n_frames = self._n_frames
        if current_n_frames is None:
            current = self.tell()
            self._seek(len(self._frame_pos))
            while self._n_frames is None:
                self._seek(self.tell() + 1)
            self.seek(current)
        assert self._n_frames is not None
        return self._n_frames

    def seek(self, frame: int) -> None:
        """Select a given frame as current image"""
        if not self._seek_check(frame):
            return
        self._seek(frame)
        if self._im is not None and (
            self.im.size != self._tile_size
            or self.im.mode != self.mode
            or self.readonly
        ):
            self._im = None

    def _seek(self, frame: int) -> None:
        if isinstance(self._fp, DeferredError):
            raise self._fp.ex
        self.fp = self._fp

        while len(self._frame_pos) <= frame:
            if not self.__next:
                msg = "no more images in TIFF file"
                raise EOFError(msg)
            logger.debug(
                "Seeking to frame %s, on frame %s, __next %s, location: %s",
                frame,
                self.__frame,
                self.__next,
                self.fp.tell(),
            )
            if self.__next >= 2**63:
                msg = "Unable to seek to frame"
                raise ValueError(msg)
            self.fp.seek(self.__next)
            self._frame_pos.append(self.__next)
            logger.debug("Loading tags, location: %s", self.fp.tell())
            self.tag_v2.load(self.fp)
            if self.tag_v2.next in self._frame_pos:
                # This IFD has already been processed
                # Declare this to be the end of the image
                self.__next = 0
            else:
                self.__next = self.tag_v2.next
            if self.__next == 0:
                self._n_frames = frame + 1
            if len(self._frame_pos) == 1:
                self.is_animated = self.__next != 0
            self.__frame += 1
        self.fp.seek(self._frame_pos[frame])
        self.tag_v2.load(self.fp)
        if XMP in self.tag_v2:
            xmp = self.tag_v2[XMP]
            if isinstance(xmp, tuple) and len(xmp) == 1:
                xmp = xmp[0]
            self.info["xmp"] = xmp
        elif "xmp" in self.info:
            del self.info["xmp"]
        self._reload_exif()
        # fill the legacy tag/ifd entries
        self.tag = self.ifd = ImageFileDirectory_v1.from_v2(self.tag_v2)
        self.__frame = frame
        self._setup()

    def tell(self) -> int:
        """Return the current frame number"""
        return self.__frame

    def get_photoshop_blocks(self) -> dict[int, dict[str, bytes]]:
        """
        Returns a dictionary of Photoshop "Image Resource Blocks".
        The keys are the image resource ID. For more information, see
        https://www.adobe.com/devnet-apps/photoshop/fileformatashtml/#50577409_pgfId-1037727

        :returns: Photoshop "Image Resource Blocks" in a dictionary.
        """
        blocks = {}
        val = self.tag_v2.get(ExifTags.Base.ImageResources)
        if val:
            while val.startswith(b"8BIM"):
                id = i16(val[4:6])
                n = math.ceil((val[6] + 1) / 2) * 2
                size = i32(val[6 + n : 10 + n])
                data = val[10 + n : 10 + n + size]
                blocks[id] = {"data": data}

                val = val[math.ceil((10 + n + size) / 2) * 2 :]
        return blocks

    def load(self) -> Image.core.PixelAccess | None:
        if self.tile and self.use_load_libtiff:
            return self._load_libtiff()
        return super().load()

    def load_prepare(self) -> None:
        if self._im is None:
            Image._decompression_bomb_check(self._tile_size)
            self.im = Image.core.new(self.mode, self._tile_size)
        ImageFile.ImageFile.load_prepare(self)

    def load_end(self) -> None:
        # allow closing if we're on the first frame, there's no next
        # This is the ImageFile.load path only, libtiff specific below.
        if not self.is_animated:
            self._close_exclusive_fp_after_loading = True

            # load IFD data from fp before it is closed
            exif = self.getexif()
            for key in TiffTags.TAGS_V2_GROUPS:
                if key not in exif:
                    continue
                exif.get_ifd(key)

        ImageOps.exif_transpose(self, in_place=True)
        if ExifTags.Base.Orientation in self.tag_v2:
            del self.tag_v2[ExifTags.Base.Orientation]

    def _load_libtiff(self) -> Image.core.PixelAccess | None:
        """Overload method triggered when we detect a compressed tiff
        Calls out to libtiff"""

        Image.Image.load(self)

        self.load_prepare()

        if not len(self.tile) == 1:
            msg = "Not exactly one tile"
            raise OSError(msg)

        # (self._compression, (extents tuple),
        #   0, (rawmode, self._compression, fp))
        extents = self.tile[0][1]
        args = self.tile[0][3]

        # To be nice on memory footprint, if there's a
        # file descriptor, use that instead of reading
        # into a string in python.
        try:
            fp = hasattr(self.fp, "fileno") and self.fp.fileno()
            # flush the file descriptor, prevents error on pypy 2.4+
            # should also eliminate the need for fp.tell
            # in _seek
            if hasattr(self.fp, "flush"):
                self.fp.flush()
        except OSError:
            # io.BytesIO have a fileno, but returns an OSError if
            # it doesn't use a file descriptor.
            fp = False

        if fp:
            assert isinstance(args, tuple)
            args_list = list(args)
            args_list[2] = fp
            args = tuple(args_list)

        decoder = Image._getdecoder(self.mode, "libtiff", args, self.decoderconfig)
        try:
            decoder.setimage(self.im, extents)
        except ValueError as e:
            msg = "Couldn't set the image"
            raise OSError(msg) from e

        close_self_fp = self._exclusive_fp and not self.is_animated
        if hasattr(self.fp, "getvalue"):
            # We've got a stringio like thing passed in. Yay for all in memory.
            # The decoder needs the entire file in one shot, so there's not
            # a lot we can do here other than give it the entire file.
            # unless we could do something like get the address of the
            # underlying string for stringio.
            #
            # Rearranging for supporting byteio items, since they have a fileno
            # that returns an OSError if there's no underlying fp. Easier to
            # deal with here by reordering.
            logger.debug("have getvalue. just sending in a string from getvalue")
            n, err = decoder.decode(self.fp.getvalue())
        elif fp:
            # we've got a actual file on disk, pass in the fp.
            logger.debug("have fileno, calling fileno version of the decoder.")
            if not close_self_fp:
                self.fp.seek(0)
            # Save and restore the file position, because libtiff will move it
            # outside of the Python runtime, and that will confuse
            # io.BufferedReader and possible others.
            # NOTE: This must use os.lseek(), and not fp.tell()/fp.seek(),
            # because the buffer read head already may not equal the actual
            # file position, and fp.seek() may just adjust it's internal
            # pointer and not actually seek the OS file handle.
            pos = os.lseek(fp, 0, os.SEEK_CUR)
            # 4 bytes, otherwise the trace might error out
            n, err = decoder.decode(b"fpfp")
            os.lseek(fp, pos, os.SEEK_SET)
        else:
            # we have something else.
            logger.debug("don't have fileno or getvalue. just reading")
            self.fp.seek(0)
            # UNDONE -- so much for that buffer size thing.
            n, err = decoder.decode(self.fp.read())

        self.tile = []
        self.readonly = 0

        self.load_end()

        if close_self_fp:
            self.fp.close()
            self.fp = None  # might be shared

        if err < 0:
            msg = f"decoder error {err}"
            raise OSError(msg)

        return Image.Image.load(self)

    def _setup(self) -> None:
        """Setup this image object based on current tags"""

        if 0xBC01 in self.tag_v2:
            msg = "Windows Media Photo files not yet supported"
            raise OSError(msg)

        # extract relevant tags
        self._compression = COMPRESSION_INFO[self.tag_v2.get(COMPRESSION, 1)]
        self._planar_configuration = self.tag_v2.get(PLANAR_CONFIGURATION, 1)

        # photometric is a required tag, but not everyone is reading
        # the specification
        photo = self.tag_v2.get(PHOTOMETRIC_INTERPRETATION, 0)

        # old style jpeg compression images most certainly are YCbCr
        if self._compression == "tiff_jpeg":
            photo = 6

        fillorder = self.tag_v2.get(FILLORDER, 1)

        logger.debug("*** Summary ***")
        logger.debug("- compression: %s", self._compression)
        logger.debug("- photometric_interpretation: %s", photo)
        logger.debug("- planar_configuration: %s", self._planar_configuration)
        logger.debug("- fill_order: %s", fillorder)
        logger.debug("- YCbCr subsampling: %s", self.tag_v2.get(YCBCRSUBSAMPLING))

        # size
        try:
            xsize = self.tag_v2[IMAGEWIDTH]
            ysize = self.tag_v2[IMAGELENGTH]
        except KeyError as e:
            msg = "Missing dimensions"
            raise TypeError(msg) from e
        if not isinstance(xsize, int) or not isinstance(ysize, int):
            msg = "Invalid dimensions"
            raise ValueError(msg)
        self._tile_size = xsize, ysize
        orientation = self.tag_v2.get(ExifTags.Base.Orientation)
        if orientation in (5, 6, 7, 8):
            self._size = ysize, xsize
        else:
            self._size = xsize, ysize

        logger.debug("- size: %s", self.size)

        sample_format = self.tag_v2.get(SAMPLEFORMAT, (1,))
        if len(sample_format) > 1 and max(sample_format) == min(sample_format) == 1:
            # SAMPLEFORMAT is properly per band, so an RGB image will
            # be (1,1,1).  But, we don't support per band pixel types,
            # and anything more than one band is a uint8. So, just
            # take the first element. Revisit this if adding support
            # for more exotic images.
            sample_format = (1,)

        bps_tuple = self.tag_v2.get(BITSPERSAMPLE, (1,))
        extra_tuple = self.tag_v2.get(EXTRASAMPLES, ())
        if photo in (2, 6, 8):  # RGB, YCbCr, LAB
            bps_count = 3
        elif photo == 5:  # CMYK
            bps_count = 4
        else:
            bps_count = 1
        bps_count += len(extra_tuple)
        bps_actual_count = len(bps_tuple)
        samples_per_pixel = self.tag_v2.get(
            SAMPLESPERPIXEL,
            3 if self._compression == "tiff_jpeg" and photo in (2, 6) else 1,
        )

        if samples_per_pixel > MAX_SAMPLESPERPIXEL:
            # DOS check, samples_per_pixel can be a Long, and we extend the tuple below
            logger.error(
                "More samples per pixel than can be decoded: %s", samples_per_pixel
            )
            msg = "Invalid value for samples per pixel"
            raise SyntaxError(msg)

        if samples_per_pixel < bps_actual_count:
            # If a file has more values in bps_tuple than expected,
            # remove the excess.
            bps_tuple = bps_tuple[:samples_per_pixel]
        elif samples_per_pixel > bps_actual_count and bps_actual_count == 1:
            # If a file has only one value in bps_tuple, when it should have more,
            # presume it is the same number of bits for all of the samples.
            bps_tuple = bps_tuple * samples_per_pixel

        if len(bps_tuple) != samples_per_pixel:
            msg = "unknown data organization"
            raise SyntaxError(msg)

        # mode: check photometric interpretation and bits per pixel
        key = (
            self.tag_v2.prefix,
            photo,
            sample_format,
            fillorder,
            bps_tuple,
            extra_tuple,
        )
        logger.debug("format key: %s", key)
        try:
            self._mode, rawmode = OPEN_INFO[key]
        except KeyError as e:
            logger.debug("- unsupported format")
            msg = "unknown pixel mode"
            raise SyntaxError(msg) from e

        logger.debug("- raw mode: %s", rawmode)
        logger.debug("- pil mode: %s", self.mode)

        self.info["compression"] = self._compression

        xres = self.tag_v2.get(X_RESOLUTION, 1)
        yres = self.tag_v2.get(Y_RESOLUTION, 1)

        if xres and yres:
            resunit = self.tag_v2.get(RESOLUTION_UNIT)
            if resunit == 2:  # dots per inch
                self.info["dpi"] = (xres, yres)
            elif resunit == 3:  # dots per centimeter. convert to dpi
                self.info["dpi"] = (xres * 2.54, yres * 2.54)
            elif resunit is None:  # used to default to 1, but now 2)
                self.info["dpi"] = (xres, yres)
                # For backward compatibility,
                # we also preserve the old behavior
                self.info["resolution"] = xres, yres
            else:  # No absolute unit of measurement
                self.info["resolution"] = xres, yres

        # build tile descriptors
        x = y = layer = 0
        self.tile = []
        self.use_load_libtiff = READ_LIBTIFF or self._compression != "raw"
        if self.use_load_libtiff:
            # Decoder expects entire file as one tile.
            # There's a buffer size limit in load (64k)
            # so large g4 images will fail if we use that
            # function.
            #
            # Setup the one tile for the whole image, then
            # use the _load_libtiff function.

            # libtiff handles the fillmode for us, so 1;IR should
            # actually be 1;I. Including the R double reverses the
            # bits, so stripes of the image are reversed.  See
            # https://github.com/python-pillow/Pillow/issues/279
            if fillorder == 2:
                # Replace fillorder with fillorder=1
                key = key[:3] + (1,) + key[4:]
                logger.debug("format key: %s", key)
                # this should always work, since all the
                # fillorder==2 modes have a corresponding
                # fillorder=1 mode
                self._mode, rawmode = OPEN_INFO[key]
            # YCbCr images with new jpeg compression with pixels in one plane
            # unpacked straight into RGB values
            if (
                photo == 6
                and self._compression == "jpeg"
                and self._planar_configuration == 1
            ):
                rawmode = "RGB"
            # libtiff always returns the bytes in native order.
            # we're expecting image byte order. So, if the rawmode
            # contains I;16, we need to convert from native to image
            # byte order.
            elif rawmode == "I;16":
                rawmode = "I;16N"
            elif rawmode.endswith((";16B", ";16L")):
                rawmode = rawmode[:-1] + "N"

            # Offset in the tile tuple is 0, we go from 0,0 to
            # w,h, and we only do this once -- eds
            a = (rawmode, self._compression, False, self.tag_v2.offset)
            self.tile.append(ImageFile._Tile("libtiff", (0, 0, xsize, ysize), 0, a))

        elif STRIPOFFSETS in self.tag_v2 or TILEOFFSETS in self.tag_v2:
            # striped image
            if STRIPOFFSETS in self.tag_v2:
                offsets = self.tag_v2[STRIPOFFSETS]
                h = self.tag_v2.get(ROWSPERSTRIP, ysize)
                w = xsize
            else:
                # tiled image
                offsets = self.tag_v2[TILEOFFSETS]
                tilewidth = self.tag_v2.get(TILEWIDTH)
                h = self.tag_v2.get(TILELENGTH)
                if not isinstance(tilewidth, int) or not isinstance(h, int):
                    msg = "Invalid tile dimensions"
                    raise ValueError(msg)
                w = tilewidth

            if w == xsize and h == ysize and self._planar_configuration != 2:
                # Every tile covers the image. Only use the last offset
                offsets = offsets[-1:]

            for offset in offsets:
                if x + w > xsize:
                    stride = w * sum(bps_tuple) / 8  # bytes per line
                else:
                    stride = 0

                tile_rawmode = rawmode
                if self._planar_configuration == 2:
                    # each band on it's own layer
                    tile_rawmode = rawmode[layer]
                    # adjust stride width accordingly
                    stride /= bps_count

                args = (tile_rawmode, int(stride), 1)
                self.tile.append(
                    ImageFile._Tile(
                        self._compression,
                        (x, y, min(x + w, xsize), min(y + h, ysize)),
                        offset,
                        args,
                    )
                )
                x += w
                if x >= xsize:
                    x, y = 0, y + h
                    if y >= ysize:
                        y = 0
                        layer += 1
        else:
            logger.debug("- unsupported data organization")
            msg = "unknown data organization"
            raise SyntaxError(msg)

        # Fix up info.
        if ICCPROFILE in self.tag_v2:
            self.info["icc_profile"] = self.tag_v2[ICCPROFILE]

        # fixup palette descriptor

        if self.mode in ["P", "PA"]:
            palette = [o8(b // 256) for b in self.tag_v2[COLORMAP]]
            self.palette = ImagePalette.raw("RGB;L", b"".join(palette))


#
# --------------------------------------------------------------------
# Write TIFF files

# little endian is default except for image modes with
# explicit big endian byte-order

SAVE_INFO = {
    # mode => rawmode, byteorder, photometrics,
    #           sampleformat, bitspersample, extra
    "1": ("1", II, 1, 1, (1,), None),
    "L": ("L", II, 1, 1, (8,), None),
    "LA": ("LA", II, 1, 1, (8, 8), 2),
    "P": ("P", II, 3, 1, (8,), None),
    "PA": ("PA", II, 3, 1, (8, 8), 2),
    "I": ("I;32S", II, 1, 2, (32,), None),
    "I;16": ("I;16", II, 1, 1, (16,), None),
    "I;16L": ("I;16L", II, 1, 1, (16,), None),
    "F": ("F;32F", II, 1, 3, (32,), None),
    "RGB": ("RGB", II, 2, 1, (8, 8, 8), None),
    "RGBX": ("RGBX", II, 2, 1, (8, 8, 8, 8), 0),
    "RGBA": ("RGBA", II, 2, 1, (8, 8, 8, 8), 2),
    "CMYK": ("CMYK", II, 5, 1, (8, 8, 8, 8), None),
    "YCbCr": ("YCbCr", II, 6, 1, (8, 8, 8), None),
    "LAB": ("LAB", II, 8, 1, (8, 8, 8), None),
    "I;16B": ("I;16B", MM, 1, 1, (16,), None),
}


def _save(im: Image.Image, fp: IO[bytes], filename: str | bytes) -> None:
    try:
        rawmode, prefix, photo, format, bits, extra = SAVE_INFO[im.mode]
    except KeyError as e:
        msg = f"cannot write mode {im.mode} as TIFF"
        raise OSError(msg) from e

    encoderinfo = im.encoderinfo
    encoderconfig = im.encoderconfig

    ifd = ImageFileDirectory_v2(prefix=prefix)
    if encoderinfo.get("big_tiff"):
        ifd._bigtiff = True

    try:
        compression = encoderinfo["compression"]
    except KeyError:
        compression = im.info.get("compression")
        if isinstance(compression, int):
            # compression value may be from BMP. Ignore it
            compression = None
    if compression is None:
        compression = "raw"
    elif compression == "tiff_jpeg":
        # OJPEG is obsolete, so use new-style JPEG compression instead
        compression = "jpeg"
    elif compression == "tiff_deflate":
        compression = "tiff_adobe_deflate"

    libtiff = WRITE_LIBTIFF or compression != "raw"

    # required for color libtiff images
    ifd[PLANAR_CONFIGURATION] = 1

    ifd[IMAGEWIDTH] = im.size[0]
    ifd[IMAGELENGTH] = im.size[1]

    # write any arbitrary tags passed in as an ImageFileDirectory
    if "tiffinfo" in encoderinfo:
        info = encoderinfo["tiffinfo"]
    elif "exif" in encoderinfo:
        info = encoderinfo["exif"]
        if isinstance(info, bytes):
            exif = Image.Exif()
            exif.load(info)
            info = exif
    else:
        info = {}
    logger.debug("Tiffinfo Keys: %s", list(info))
    if isinstance(info, ImageFileDirectory_v1):
        info = info.to_v2()
    for key in info:
        if isinstance(info, Image.Exif) and key in TiffTags.TAGS_V2_GROUPS:
            ifd[key] = info.get_ifd(key)
        else:
            ifd[key] = info.get(key)
        try:
            ifd.tagtype[key] = info.tagtype[key]
        except Exception:
            pass  # might not be an IFD. Might not have populated type

    legacy_ifd = {}
    if hasattr(im, "tag"):
        legacy_ifd = im.tag.to_v2()

    supplied_tags = {**legacy_ifd, **getattr(im, "tag_v2", {})}
    for tag in (
        # IFD offset that may not be correct in the saved image
        EXIFIFD,
        # Determined by the image format and should not be copied from legacy_ifd.
        SAMPLEFORMAT,
    ):
        if tag in supplied_tags:
            del supplied_tags[tag]

    # additions written by Greg Couch, gregc@cgl.ucsf.edu
    # inspired by image-sig posting from Kevin Cazabon, kcazabon@home.com
    if hasattr(im, "tag_v2"):
        # preserve tags from original TIFF image file
        for key in (
            RESOLUTION_UNIT,
            X_RESOLUTION,
            Y_RESOLUTION,
            IPTC_NAA_CHUNK,
            PHOTOSHOP_CHUNK,
            XMP,
        ):
            if key in im.tag_v2:
                if key == IPTC_NAA_CHUNK and im.tag_v2.tagtype[key] not in (
                    TiffTags.BYTE,
                    TiffTags.UNDEFINED,
                ):
                    del supplied_tags[key]
                else:
                    ifd[key] = im.tag_v2[key]
                    ifd.tagtype[key] = im.tag_v2.tagtype[key]

    # preserve ICC profile (should also work when saving other formats
    # which support profiles as TIFF) -- 2008-06-06 Florian Hoech
    icc = encoderinfo.get("icc_profile", im.info.get("icc_profile"))
    if icc:
        ifd[ICCPROFILE] = icc

    for key, name in [
        (IMAGEDESCRIPTION, "description"),
        (X_RESOLUTION, "resolution"),
        (Y_RESOLUTION, "resolution"),
        (X_RESOLUTION, "x_resolution"),
        (Y_RESOLUTION, "y_resolution"),
        (RESOLUTION_UNIT, "resolution_unit"),
        (SOFTWARE, "software"),
        (DATE_TIME, "date_time"),
        (ARTIST, "artist"),
        (COPYRIGHT, "copyright"),
    ]:
        if name in encoderinfo:
            ifd[key] = encoderinfo[name]

    dpi = encoderinfo.get("dpi")
    if dpi:
        ifd[RESOLUTION_UNIT] = 2
        ifd[X_RESOLUTION] = dpi[0]
        ifd[Y_RESOLUTION] = dpi[1]

    if bits != (1,):
        ifd[BITSPERSAMPLE] = bits
        if len(bits) != 1:
            ifd[SAMPLESPERPIXEL] = len(bits)
    if extra is not None:
        ifd[EXTRASAMPLES] = extra
    if format != 1:
        ifd[SAMPLEFORMAT] = format

    if PHOTOMETRIC_INTERPRETATION not in ifd:
        ifd[PHOTOMETRIC_INTERPRETATION] = photo
    elif im.mode in ("1", "L") and ifd[PHOTOMETRIC_INTERPRETATION] == 0:
        if im.mode == "1":
            inverted_im = im.copy()
            px = inverted_im.load()
            if px is not None:
                for y in range(inverted_im.height):
                    for x in range(inverted_im.width):
                        px[x, y] = 0 if px[x, y] == 255 else 255
                im = inverted_im
        else:
            im = ImageOps.invert(im)

    if im.mode in ["P", "PA"]:
        lut = im.im.getpalette("RGB", "RGB;L")
        colormap = []
        colors = len(lut) // 3
        for i in range(3):
            colormap += [v * 256 for v in lut[colors * i : colors * (i + 1)]]
            colormap += [0] * (256 - colors)
        ifd[COLORMAP] = colormap
    # data orientation
    w, h = ifd[IMAGEWIDTH], ifd[IMAGELENGTH]
    stride = len(bits) * ((w * bits[0] + 7) // 8)
    if ROWSPERSTRIP not in ifd:
        # aim for given strip size (64 KB by default) when using libtiff writer
        if libtiff:
            im_strip_size = encoderinfo.get("strip_size", STRIP_SIZE)
            rows_per_strip = 1 if stride == 0 else min(im_strip_size // stride, h)
            # JPEG encoder expects multiple of 8 rows
            if compression == "jpeg":
                rows_per_strip = min(((rows_per_strip + 7) // 8) * 8, h)
        else:
            rows_per_strip = h
        if rows_per_strip == 0:
            rows_per_strip = 1
        ifd[ROWSPERSTRIP] = rows_per_strip
    strip_byte_counts = 1 if stride == 0 else stride * ifd[ROWSPERSTRIP]
    strips_per_image = (h + ifd[ROWSPERSTRIP] - 1) // ifd[ROWSPERSTRIP]
    if strip_byte_counts >= 2**16:
        ifd.tagtype[STRIPBYTECOUNTS] = TiffTags.LONG
    ifd[STRIPBYTECOUNTS] = (strip_byte_counts,) * (strips_per_image - 1) + (
        stride * h - strip_byte_counts * (strips_per_image - 1),
    )
    ifd[STRIPOFFSETS] = tuple(
        range(0, strip_byte_counts * strips_per_image, strip_byte_counts)
    )  # this is adjusted by IFD writer
    # no compression by default:
    ifd[COMPRESSION] = COMPRESSION_INFO_REV.get(compression, 1)

    if im.mode == "YCbCr":
        for tag, default_value in {
            YCBCRSUBSAMPLING: (1, 1),
            REFERENCEBLACKWHITE: (0, 255, 128, 255, 128, 255),
        }.items():
            ifd.setdefault(tag, default_value)

    blocklist = [TILEWIDTH, TILELENGTH, TILEOFFSETS, TILEBYTECOUNTS]
    if libtiff:
        if "quality" in encoderinfo:
            quality = encoderinfo["quality"]
            if not isinstance(quality, int) or quality < 0 or quality > 100:
                msg = "Invalid quality setting"
                raise ValueError(msg)
            if compression != "jpeg":
                msg = "quality setting only supported for 'jpeg' compression"
                raise ValueError(msg)
            ifd[JPEGQUALITY] = quality

        logger.debug("Saving using libtiff encoder")
        logger.debug("Items: %s", sorted(ifd.items()))
        _fp = 0
        if hasattr(fp, "fileno"):
            try:
                fp.seek(0)
                _fp = fp.fileno()
            except io.UnsupportedOperation:
                pass

        # optional types for non core tags
        types = {}
        # STRIPOFFSETS and STRIPBYTECOUNTS are added by the library
        # based on the data in the strip.
        # OSUBFILETYPE is deprecated.
        # The other tags expect arrays with a certain length (fixed or depending on
        # BITSPERSAMPLE, etc), passing arrays with a different length will result in
        # segfaults. Block these tags until we add extra validation.
        # SUBIFD may also cause a segfault.
        blocklist += [
            OSUBFILETYPE,
            REFERENCEBLACKWHITE,
            STRIPBYTECOUNTS,
            STRIPOFFSETS,
            TRANSFERFUNCTION,
            SUBIFD,
        ]

        # bits per sample is a single short in the tiff directory, not a list.
        atts: dict[int, Any] = {BITSPERSAMPLE: bits[0]}
        # Merge the ones that we have with (optional) more bits from
        # the original file, e.g x,y resolution so that we can
        # save(load('')) == original file.
        for tag, value in itertools.chain(ifd.items(), supplied_tags.items()):
            # Libtiff can only process certain core items without adding
            # them to the custom dictionary.
            # Custom items are supported for int, float, unicode, string and byte
            # values. Other types and tuples require a tagtype.
            if tag not in TiffTags.LIBTIFF_CORE:
                if tag in TiffTags.TAGS_V2_GROUPS:
                    types[tag] = TiffTags.LONG8
                elif tag in ifd.tagtype:
                    types[tag] = ifd.tagtype[tag]
                elif not (isinstance(value, (int, float, str, bytes))):
                    continue
                else:
                    type = TiffTags.lookup(tag).type
                    if type:
                        types[tag] = type
            if tag not in atts and tag not in blocklist:
                if isinstance(value, str):
                    atts[tag] = value.encode("ascii", "replace") + b"\0"
                elif isinstance(value, IFDRational):
                    atts[tag] = float(value)
                else:
                    atts[tag] = value

        if SAMPLEFORMAT in atts and len(atts[SAMPLEFORMAT]) == 1:
            atts[SAMPLEFORMAT] = atts[SAMPLEFORMAT][0]

        logger.debug("Converted items: %s", sorted(atts.items()))

        # libtiff always expects the bytes in native order.
        # we're storing image byte order. So, if the rawmode
        # contains I;16, we need to convert from native to image
        # byte order.
        if im.mode in ("I;16", "I;16B", "I;16L"):
            rawmode = "I;16N"

        # Pass tags as sorted list so that the tags are set in a fixed order.
        # This is required by libtiff for some tags. For example, the JPEGQUALITY
        # pseudo tag requires that the COMPRESS tag was already set.
        tags = list(atts.items())
        tags.sort()
        a = (rawmode, compression, _fp, filename, tags, types)
        encoder = Image._getencoder(im.mode, "libtiff", a, encoderconfig)
        encoder.setimage(im.im, (0, 0) + im.size)
        while True:
            errcode, data = encoder.encode(ImageFile.MAXBLOCK)[1:]
            if not _fp:
                fp.write(data)
            if errcode:
                break
        if errcode < 0:
            msg = f"encoder error {errcode} when writing image file"
            raise OSError(msg)

    else:
        for tag in blocklist:
            del ifd[tag]
        offset = ifd.save(fp)

        ImageFile._save(
            im,
            fp,
            [ImageFile._Tile("raw", (0, 0) + im.size, offset, (rawmode, stride, 1))],
        )

    # -- helper for multi-page save --
    if "_debug_multipage" in encoderinfo:
        # just to access o32 and o16 (using correct byte order)
        setattr(im, "_debug_multipage", ifd)


class AppendingTiffWriter(io.BytesIO):
    fieldSizes = [
        0,  # None
        1,  # byte
        1,  # ascii
        2,  # short
        4,  # long
        8,  # rational
        1,  # sbyte
        1,  # undefined
        2,  # sshort
        4,  # slong
        8,  # srational
        4,  # float
        8,  # double
        4,  # ifd
        2,  # unicode
        4,  # complex
        8,  # long8
    ]

    Tags = {
        273,  # StripOffsets
        288,  # FreeOffsets
        324,  # TileOffsets
        519,  # JPEGQTables
        520,  # JPEGDCTables
        521,  # JPEGACTables
    }

    def __init__(self, fn: StrOrBytesPath | IO[bytes], new: bool = False) -> None:
        self.f: IO[bytes]
        if is_path(fn):
            self.name = fn
            self.close_fp = True
            try:
                self.f = open(fn, "w+b" if new else "r+b")
            except OSError:
                self.f = open(fn, "w+b")
        else:
            self.f = cast(IO[bytes], fn)
            self.close_fp = False
        self.beginning = self.f.tell()
        self.setup()

    def setup(self) -> None:
        # Reset everything.
        self.f.seek(self.beginning, os.SEEK_SET)

        self.whereToWriteNewIFDOffset: int | None = None
        self.offsetOfNewPage = 0

        self.IIMM = iimm = self.f.read(4)
        self._bigtiff = b"\x2b" in iimm
        if not iimm:
            # empty file - first page
            self.isFirst = True
            return

        self.isFirst = False
        if iimm not in PREFIXES:
            msg = "Invalid TIFF file header"
            raise RuntimeError(msg)

        self.setEndian("<" if iimm.startswith(II) else ">")

        if self._bigtiff:
            self.f.seek(4, os.SEEK_CUR)
        self.skipIFDs()
        self.goToEnd()

    def finalize(self) -> None:
        if self.isFirst:
            return

        # fix offsets
        self.f.seek(self.offsetOfNewPage)

        iimm = self.f.read(4)
        if not iimm:
            # Make it easy to finish a frame without committing to a new one.
            return

        if iimm != self.IIMM:
            msg = "IIMM of new page doesn't match IIMM of first page"
            raise RuntimeError(msg)

        if self._bigtiff:
            self.f.seek(4, os.SEEK_CUR)
        ifd_offset = self._read(8 if self._bigtiff else 4)
        ifd_offset += self.offsetOfNewPage
        assert self.whereToWriteNewIFDOffset is not None
        self.f.seek(self.whereToWriteNewIFDOffset)
        self._write(ifd_offset, 8 if self._bigtiff else 4)
        self.f.seek(ifd_offset)
        self.fixIFD()

    def newFrame(self) -> None:
        # Call this to finish a frame.
        self.finalize()
        self.setup()

    def __enter__(self) -> AppendingTiffWriter:
        return self

    def __exit__(self, *args: object) -> None:
        if self.close_fp:
            self.close()

    def tell(self) -> int:
        return self.f.tell() - self.offsetOfNewPage

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """
        :param offset: Distance to seek.
        :param whence: Whether the distance is relative to the start,
                       end or current position.
        :returns: The resulting position, relative to the start.
        """
        if whence == os.SEEK_SET:
            offset += self.offsetOfNewPage

        self.f.seek(offset, whence)
        return self.tell()

    def goToEnd(self) -> None:
        self.f.seek(0, os.SEEK_END)
        pos = self.f.tell()

        # pad to 16 byte boundary
        pad_bytes = 16 - pos % 16
        if 0 < pad_bytes < 16:
            self.f.write(bytes(pad_bytes))
        self.offsetOfNewPage = self.f.tell()

    def setEndian(self, endian: str) -> None:
        self.endian = endian
        self.longFmt = f"{self.endian}L"
        self.shortFmt = f"{self.endian}H"
        self.tagFormat = f"{self.endian}HH" + ("Q" if self._bigtiff else "L")

    def skipIFDs(self) -> None:
        while True:
            ifd_offset = self._read(8 if self._bigtiff else 4)
            if ifd_offset == 0:
                self.whereToWriteNewIFDOffset = self.f.tell() - (
                    8 if self._bigtiff else 4
                )
                break

            self.f.seek(ifd_offset)
            num_tags = self._read(8 if self._bigtiff else 2)
            self.f.seek(num_tags * (20 if self._bigtiff else 12), os.SEEK_CUR)

    def write(self, data: Buffer, /) -> int:
        return self.f.write(data)

    def _fmt(self, field_size: int) -> str:
        try:
            return {2: "H", 4: "L", 8: "Q"}[field_size]
        except KeyError:
            msg = "offset is not supported"
            raise RuntimeError(msg)

    def _read(self, field_size: int) -> int:
        (value,) = struct.unpack(
            self.endian + self._fmt(field_size), self.f.read(field_size)
        )
        return value

    def readShort(self) -> int:
        return self._read(2)

    def readLong(self) -> int:
        return self._read(4)

    @staticmethod
    def _verify_bytes_written(bytes_written: int | None, expected: int) -> None:
        if bytes_written is not None and bytes_written != expected:
            msg = f"wrote only {bytes_written} bytes but wanted {expected}"
            raise RuntimeError(msg)

    def _rewriteLast(
        self, value: int, field_size: int, new_field_size: int = 0
    ) -> None:
        self.f.seek(-field_size, os.SEEK_CUR)
        if not new_field_size:
            new_field_size = field_size
        bytes_written = self.f.write(
            struct.pack(self.endian + self._fmt(new_field_size), value)
        )
        self._verify_bytes_written(bytes_written, new_field_size)

    def rewriteLastShortToLong(self, value: int) -> None:
        self._rewriteLast(value, 2, 4)

    def rewriteLastShort(self, value: int) -> None:
        return self._rewriteLast(value, 2)

    def rewriteLastLong(self, value: int) -> None:
        return self._rewriteLast(value, 4)

    def _write(self, value: int, field_size: int) -> None:
        bytes_written = self.f.write(
            struct.pack(self.endian + self._fmt(field_size), value)
        )
        self._verify_bytes_written(bytes_written, field_size)

    def writeShort(self, value: int) -> None:
        self._write(value, 2)

    def writeLong(self, value: int) -> None:
        self._write(value, 4)

    def close(self) -> None:
        self.finalize()
        if self.close_fp:
            self.f.close()

    def fixIFD(self) -> None:
        num_tags = self._read(8 if self._bigtiff else 2)

        for i in range(num_tags):
            tag, field_type, count = struct.unpack(
                self.tagFormat, self.f.read(12 if self._bigtiff else 8)
            )

            field_size = self.fieldSizes[field_type]
            total_size = field_size * count
            fmt_size = 8 if self._bigtiff else 4
            is_local = total_size <= fmt_size
            if not is_local:
                offset = self._read(fmt_size) + self.offsetOfNewPage
                self._rewriteLast(offset, fmt_size)

            if tag in self.Tags:
                cur_pos = self.f.tell()

                logger.debug(
                    "fixIFD: %s (%d) - type: %s (%d) - type size: %d - count: %d",
                    TiffTags.lookup(tag).name,
                    tag,
                    TYPES.get(field_type, "unknown"),
                    field_type,
                    field_size,
                    count,
                )

                if is_local:
                    self._fixOffsets(count, field_size)
                    self.f.seek(cur_pos + fmt_size)
                else:
                    self.f.seek(offset)
                    self._fixOffsets(count, field_size)
                    self.f.seek(cur_pos)

            elif is_local:
                # skip the locally stored value that is not an offset
                self.f.seek(fmt_size, os.SEEK_CUR)

    def _fixOffsets(self, count: int, field_size: int) -> None:
        for i in range(count):
            offset = self._read(field_size)
            offset += self.offsetOfNewPage

            new_field_size = 0
            if self._bigtiff and field_size in (2, 4) and offset >= 2**32:
                # offset is now too large - we must convert long to long8
                new_field_size = 8
            elif field_size == 2 and offset >= 2**16:
                # offset is now too large - we must convert short to long
                new_field_size = 4
            if new_field_size:
                if count != 1:
                    msg = "not implemented"
                    raise RuntimeError(msg)  # XXX TODO

                # simple case - the offset is just one and therefore it is
                # local (not referenced with another offset)
                self._rewriteLast(offset, field_size, new_field_size)
                # Move back past the new offset, past 'count', and before 'field_type'
                rewind = -new_field_size - 4 - 2
                self.f.seek(rewind, os.SEEK_CUR)
                self.writeShort(new_field_size)  # rewrite the type
                self.f.seek(2 - rewind, os.SEEK_CUR)
            else:
                self._rewriteLast(offset, field_size)

    def fixOffsets(
        self, count: int, isShort: bool = False, isLong: bool = False
    ) -> None:
        if isShort:
            field_size = 2
        elif isLong:
            field_size = 4
        else:
            field_size = 0
        return self._fixOffsets(count, field_size)


def _save_all(im: Image.Image, fp: IO[bytes], filename: str | bytes) -> None:
    append_images = list(im.encoderinfo.get("append_images", []))
    if not hasattr(im, "n_frames") and not append_images:
        return _save(im, fp, filename)

    cur_idx = im.tell()
    try:
        with AppendingTiffWriter(fp) as tf:
            for ims in [im] + append_images:
                encoderinfo = ims._attach_default_encoderinfo(im)
                if not hasattr(ims, "encoderconfig"):
                    ims.encoderconfig = ()
                nfr = getattr(ims, "n_frames", 1)

                for idx in range(nfr):
                    ims.seek(idx)
                    ims.load()
                    _save(ims, tf, filename)
                    tf.newFrame()
                ims.encoderinfo = encoderinfo
    finally:
        im.seek(cur_idx)


#
# --------------------------------------------------------------------
# Register

Image.register_open(TiffImageFile.format, TiffImageFile, _accept)
Image.register_save(TiffImageFile.format, _save)
Image.register_save_all(TiffImageFile.format, _save_all)

Image.register_extensions(TiffImageFile.format, [".tif", ".tiff"])

Image.register_mime(TiffImageFile.format, "image/tiff")
