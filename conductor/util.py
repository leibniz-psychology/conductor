"""
Miscellaneous utility functions
"""

import platform, re, os
from collections import namedtuple

async def copy (source, dest):
	try:
		while True:
			buf = await source.read (4096)
			if not buf:
				dest.close ()
				break
			foo = dest.write (buf)
			assert foo is None
	except ConnectionResetError:
		# this is fine
		pass

SockInfo = namedtuple ('SockInfo', ['num', 'refCount', 'protocol', 'flags', 'type', 'st', 'inode', 'path'])

def listSockets ():
	"""
	Get a list of open sockets
	"""

	assert platform.system () == 'Linux'

	with open ('/proc/net/unix') as fd:
		header = re.split (r'\s+', fd.readline ().strip ())
		assert [x.lower() for x in header] == [x.lower () for x in SockInfo._fields], (header, SockInfo._fields)
		for l in fd:
			l = l.strip ()
			value = re.split (r'\s+', l, maxsplit=len (header)-1)
			while len (value) < len (header):
				value.append (None)
			d = dict (zip (SockInfo._fields, value))
			d['inode'] = int (d['inode'])
			s = SockInfo (**d)
			yield s

def listProcesses ():
	"""
	Get a list of processes and their open fds
	"""

	assert platform.system () == 'Linux'

	for pid in os.listdir ('/proc'):
		if pid.isdigit ():
			fddir = os.path.join ('/proc', pid, 'fd')
			files = []
			try:
				for fdlink in os.listdir (fddir):
					try:
						dest = os.readlink (os.path.join (fddir, fdlink))
						files.append (dest)
					except FileNotFoundError:
						# gone already
						pass
			except PermissionError:
				pass
			yield (int (pid), files)

def socketListener (path):
	"""
	Check if someone is listening on socket path and return its PID if possible
	"""

	inodes = list (filter (lambda x: x.path == path, listSockets ()))
	if not inodes:
		raise KeyError ('path not found')
	assert len (inodes) <= 1

	inode = inodes[0].inode

	for pid, files in listProcesses ():
		for f in files:
			if f == f'socket:[{inode}]':
				return pid
	# no process information available
	return None

