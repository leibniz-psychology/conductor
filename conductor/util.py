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

"""
Miscellaneous utility functions
"""

import platform, re, os, asyncio, logging, sys
from collections import namedtuple
import structlog

async def copy (source, dest):
	bufsize = 256*1024
	copied = 0
	while True:
		buf = await source.read (bufsize)
		if not buf:
			break
		foo = dest.write (buf)
		assert foo is None
		await dest.drain ()
		copied += len (buf)
	return copied

async def proxy (endpointa, endpointb, logger, beforeABClose=None):
	"""
	Bi-directional proxying for tuple of (StreamReader, StreamWriter, str)
	"""

	eaReader, eaWriter, eaName = endpointa
	ebReader, ebWriter, ebName = endpointb

	# proxy everything
	try:
		a = asyncio.ensure_future (copy (eaReader, ebWriter))
		b = asyncio.ensure_future (copy (ebReader, eaWriter))
		pending = [a, b]
		while pending:
			done, pending = await asyncio.wait (pending, return_when=asyncio.FIRST_COMPLETED)
			logger.debug ('proxy_wait_returned', done=done, pending=pending)
			if a in done:
				if beforeABClose is not None:
					await beforeABClose (a.result ())
				logger.debug ('proxy_copy_done', source=eaName, destination=ebName)
				ebWriter.close ()
			if b in done:
				logger.debug ('proxy_copy_done', source=ebName, destination=eaName)
				eaWriter.close ()
		# discard results
		a.result ()
		b.result ()
	except BrokenPipeError:
		logger.debug ('proxy_broken_pipe')
	except ConnectionResetError:
		logger.debug ('proxy_connetion_reset')
	except Exception as e:
		logger.debug ('proxy_exception', error=e)
		traceback.print_exc()
	finally:
		logger.debug ('proxy_finally')
		# make sure they are really closed
		eaWriter.close ()
		ebWriter.close ()
		c = asyncio.ensure_future (eaWriter.wait_closed ())
		d = asyncio.ensure_future (ebWriter.wait_closed ())
		await asyncio.wait ([c, d])
		# discard results
		try:
			c.result ()
			d.result ()
		except Exception as e:
			pass
		logger.debug ('proxy_bye')

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
	Return PIDs of everyone listening on socket ``path``.
	"""

	inodes = list (filter (lambda x: x.path == path, listSockets ()))
	if not inodes:
		raise KeyError ('path not found')

	ret = []
	for x in inodes:
		inode = x.inode

		for pid, files in listProcesses ():
			for f in files:
				if f == f'socket:[{inode}]':
					ret.append (pid)
	return ret

class StructLogHandler (logging.Handler):
	def __init__ (self, level=logging.NOTSET, logger=None):
		super ().__init__ (level=level)
		self.logger = logger

	""" Forward messages from Python’s own logging module to structlog """
	def emit (self, record):
		lvl = record.levelname.lower ()
		f = getattr (self.logger, lvl)
		f ('logging.' + record.name, message=record.getMessage (), exc_info=record.exc_info)

def configureStructlog (verbose):
	level = logging.DEBUG if verbose else logging.INFO
	structlog.configure (
		wrapper_class=structlog.make_filtering_bound_logger(level),
		processors=[
			structlog.threadlocal.merge_threadlocal_context,
			structlog.processors.add_log_level,
			structlog.processors.format_exc_info,
			structlog.processors.TimeStamper(fmt="iso", utc=False),
			structlog.processors.JSONRenderer(),
		],
		logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
	)
	logger = structlog.get_logger ()

	# Forward Python logging to structlog
	rootLogger = logging.getLogger ()
	structHandler = StructLogHandler (level=level, logger=logger)
	rootLogger.addHandler (structHandler)
	rootLogger.setLevel (level)

