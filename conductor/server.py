import asyncio, socket, os, struct, stat, json, logging, argparse, functools, traceback
from http.cookies import BaseCookie
from collections import namedtuple

import aionotify
from yarl import URL
from multidict import CIMultiDict

from .nss import getUser
from .util import copy

logger = logging.getLogger (__name__)

def handleException (func):
	""" XXX: Should probably use some http library here? """
	@functools.wraps(func)
	async def wrapped(*args):
		 # Some fancy foo stuff
		try:
			return await func(*args)
		except Exception as e:
			reader, writer = args
			traceback.print_exc()
			logger.error (f'exception {e.args}')
			writer.write (b'HTTP/1.0 500 Server Error\r\n\r\n')
			writer.close ()
	return wrapped

# a route from key.domain to socket with auth
Route = namedtuple ('Route', ['socket', 'key', 'auth'])

class Conductor:
	__slots__ = ('routes', )

	def __init__ (self):
		self.routes = {}

	@handleException
	async def _connectCb (self, reader, writer):
		# simple HTTP parsing
		l = (await reader.readline ()).rstrip (b'\r\n')
		method, path, proto = l.split (b' ')

		headers = CIMultiDict ()
		while True:
			if len (headers) > 100:
				raise Exception ('too many headers')

			l = (await reader.readline ()).rstrip (b'\r\n')
			# end of headers?
			if l == b'\r\n' or not l:
				break
			try:
				key, value = l.decode ('utf-8').split (':', 1)
				headers.add (key.strip (), value.strip ())
			except ValueError:
				logger.error (f'cannot parse {l}')

		logger.debug (f'{path} {method} got headers {headers}')

		host = ''
		try:
			host, _ = headers['host'].split ('.', 1)
			route = self.routes[host]
		except (KeyError, ValueError):
			logger.info (f'cannot find route for host {host}')
			# XXX: add url as parameter to the redirect url, so the target can then
			# redirect back after starting the instance
			url = URL ('http://localhost:8000/') / 'project' / host / 'start'
			writer.write (b'HTTP/1.0 302 Found\r\nLocation: ' + str (url).encode ('utf8') + b'\r\n\r\n')
			writer.close ()
			return

		# check authorization
		cookies = BaseCookie()
		try:
			cookies.load (headers['Cookie'])
		except KeyError:
			# will be rejected later
			pass
		authorized = False
		for c in cookies.values ():
			if c.key == 'authorization' and c.value == route.auth:
				authorized = True
		try:
			del cookies['authorization']
			headers['Cookie'] = cookies.output (header='', sep='')
		except KeyError:
			# nonexistent cookie is fine
			pass

		# is this an authorization request?
		authPrefix = b'/_auth-conductor/'
		if path.startswith (authPrefix):
			authdata = path[len (authPrefix):]
			logger.info (f'authorization request for {host}')
			writer.write (b'\r\n'.join ([
					b'HTTP/1.0 302 Found',
					b'Location: /',
					b'Set-Cookie: authorization=' + authdata + b'; HttpOnly; Path=/',
					b'Cache-Control: no-store',
					b'',
					b'Follow the white rabbit.']))
			writer.close ()
			return

		if not authorized:
			logger.info (f'not authorized, cookies sent {cookies.values()}')
			writer.write (b'HTTP/1.0 403 Unauthorized\r\nContent-Type: plain/text\r\n\r\nUnauthorized')
			writer.close ()
			return

		# try opening the socket
		try:
			sockreader, sockwriter = await asyncio.open_unix_connection (path=route.socket)
		except (ConnectionRefusedError, FileNotFoundError, PermissionError):
			logger.info (f'route is broken')
			del self.routes[host]
			# XXX: add url as parameter to the redirect url, so the target can then
			# redirect back after starting the instance
			url = URL ('http://localhost:8000/') / 'project' / host / 'start'
			writer.write (b'HTTP/1.0 302 Found\r\nLocation: ' + str (url).encode ('utf8') + b'\r\n\r\n')
			writer.close ()
			return

		# some headers are fixed
		# not parsing body, so we cannot handle more than one request per connection
		# XXX: this is super-inefficient
		if 'Upgrade' not in headers or headers['Upgrade'].lower () != 'websocket':
			headers['Connection'] = 'close'

		# write http banner plus headers
		sockwriter.write (method + b' ' + path + b' ' + proto + b'\r\n')
		for k, v in headers.items ():
			sockwriter.write (f'{k}: {v}\r\n'.encode ('utf-8'))
		sockwriter.write (b'\r\n')
		# then copy the body
		a = asyncio.ensure_future (copy (sockreader, writer))
		b = asyncio.ensure_future (copy (reader, sockwriter))
		await asyncio.wait ([a, b])

	async def run (self, port=None, sock=None):
		"""
		Start public proxy worker
		"""

		logger.info (f'starting server on port {port}')
		server = await asyncio.start_server (self._connectCb, port=port, sock=sock)
		await server.serve_forever ()

	def addRoute (self, route):
		# XXX: delete old config?
		self.routes[route.key] = route
		logger.info (f'configured route {route.key} → {route.socket}')

class Forest:
	__slots__ = ('path', 'proxy')

	def __init__ (self, path, proxy):
		self.path = os.path.realpath (path)
		self.proxy = proxy

		self._forestPermissionCheck ()

	def _forestPermissionCheck (self):
		# make sure the forest dir has appropriate permissions
		fstat = os.stat (self.path, follow_symlinks=False)
		permissions = stat.S_IMODE (fstat.st_mode)
		expect = 0o3713
		if permissions != expect:
			logger.error (f'Permissions on forest directory {self.path} are not correct. Is 0{permissions:o} should be 0{expect:o}')
			raise Exception ()

	async def addConfig (self, f):
		"""
		Add file f relative to forest path to proxy configuration
		"""

		configFile = os.path.join (self.path, f)
		if not configFile.endswith ('.json'):
			# skip files that are not configs
			logger.error (f'{configFile} does not end with .json')
			return

		try:
			cstat = os.stat (configFile, follow_symlinks=False)
			# don’t read if it is world-readable -> leaked credentials
			permissions = stat.S_IMODE (cstat.st_mode)
			if not stat.S_ISREG (cstat.st_mode):
				raise Exception (f'{configFile} not a regular file')
			if permissions & 0o4:
				raise Exception (f'{configFile} world-readable, ignoring')

			with open (configFile) as fd:
				o = json.load (fd)

			# socket checks:
			socketPath = os.path.realpath (os.path.join (self.path, o['socket']))
			if not os.path.dirname (socketPath).startswith (self.path):
				raise Exception (f'{socketPath} not in forest dir {self.path}')
			sstat = os.stat (socketPath, follow_symlinks=False)
			permissions = stat.S_IMODE (sstat.st_mode)
			# neither world-read nor writeable
			if permissions & 0o6:
				raise Exception (f'{socketPath} world-read- or -writeable')
			# same owner
			if cstat.st_uid != sstat.st_uid:
				raise Exception (f'{socketPath} socket owner does not match')
			if not stat.S_ISSOCK (sstat.st_mode):
				raise Exception (f'{socketPath} not a socket')

			user = getUser (cstat.st_uid)
			route = Route (socket=socketPath, key=user['name'], auth=o['auth'])

			self.proxy.addRoute (route)
		except Exception as e:
			logger.error ('addConfig for {f} failed: {e}')
			#os.unlink (configFile)

	async def run (self):
		"""
		Start directory watcher worker
		"""

		# add existing files
		logger.info (f'reading existing configs in {self.path}')
		for f in os.listdir (self.path):
			await self.addConfig (f)

		logger.info ('starting watcher')
		watcher = aionotify.Watcher()
		watcher.watch (path=self.path, flags=aionotify.Flags.MOVED_TO)

		await watcher.setup(asyncio.get_event_loop ())
		while True:
			event = await watcher.get_event()
			if event.flags == aionotify.Flags.MOVED_TO:
				await self.addConfig (event.name)
			else:
				assert False
		watcher.close ()

def main ():
	parser = argparse.ArgumentParser(description='conductor server part')
	parser.add_argument ('-p', '--port', default=8888, type=int, help='Listen port')
	parser.add_argument ('-s', '--sock', default=None, help='Listen port')
	parser.add_argument ('-v', '--verbose', action='store_true', help='Verbose output')
	parser.add_argument ('forest', default='forest', help='Local forest path')

	args = parser.parse_args()
	if args.verbose:
		logging.basicConfig (level=logging.DEBUG)
	else:
		logging.basicConfig (level=logging.WARN)

	if args.sock:
		sock = socket.socket (socket.AF_UNIX)
		if os.path.exists (args.sock):
			os.unlink (args.sock)
		sock.bind (args.sock)
		os.chmod (args.sock, 0o660)
		port = None
	else:
		sock = None
		port = args.port

	loop = asyncio.get_event_loop ()
	proxy = Conductor ()
	loop.create_task (proxy.run (port, sock))
	forest = Forest (args.forest, proxy)
	loop.run_until_complete (forest.run ())

