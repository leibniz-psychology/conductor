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

