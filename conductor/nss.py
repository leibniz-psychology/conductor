# Copyright 2019–2020 Leibniz Institute for Psychology
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from ctypes import CDLL, Structure, c_char_p, c_int, POINTER
from ctypes.util import find_library

class PasswdStruct(Structure):
	"""Struct `passwd` from `<pwd.h>`."""
	_fields_ = [
		('name', c_char_p),
		('password', c_char_p),
		('uid', c_int),
		('gid', c_int),
		('gecos', c_char_p),
		('dir', c_char_p),
		('shell', c_char_p),
	]

libc = CDLL (find_library ('c'))

getpwnam = libc.getpwnam
getpwnam.argtypes = (c_char_p,)
getpwnam.restype = POINTER(PasswdStruct)

getpwuid = libc.getpwuid
getpwuid.argtypes = (c_int,)
getpwuid.restype = POINTER(PasswdStruct)

def getUser (x):
	if isinstance (x, str):
		entry = getpwnam (x.encode ('utf-8'))
	elif isinstance (x, int):
		entry = getpwuid (x)
	else:
		raise ValueError ('invalid input')

	try:
		contents = entry.contents
	except ValueError:
		raise KeyError ('user not found') from None
	return dict (name=contents.name.decode ('utf-8'),
			homedir=contents.dir.decode ('utf-8'),
			uid=contents.uid,
			gid=contents.gid,
			)

